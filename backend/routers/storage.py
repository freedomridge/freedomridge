"""
Storage configuration API endpoints.

Manages S3 bucket configurations stored in the database.
Requires the storage_configs table (see schema migration below).
"""

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
    boto3 = None  # type: ignore[assignment]  # sentinel when boto3 absent; endpoints check HAS_BOTO3 first

from ..crypto import decrypt_secret, encrypt_secret
from ..database import db
from .auth import get_current_user

router = APIRouter(prefix="/api/storage", tags=["storage"])


# --- Pydantic models ---


class StorageConfigCreate(BaseModel):
    name: str
    storage_type: str = "s3"
    bucket_name: str
    region: str = "us-east-1"
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    is_public_bucket: bool = False
    is_default: bool = False
    # Glob-style extra buckets this config's credentials should serve, e.g.
    # ["client-*", "logs-2025-??"]. Matched with fnmatch when the literal
    # bucket_name lookup misses.
    bucket_patterns: List[str] = []
    # False: the config serves every dataset. True: only the datasets in dataset_ids.
    restricted: bool = False
    dataset_ids: List[int] = []


class StorageConfigUpdate(BaseModel):
    name: Optional[str] = None
    bucket_name: Optional[str] = None
    region: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    is_public_bucket: Optional[bool] = None
    is_default: Optional[bool] = None
    bucket_patterns: Optional[List[str]] = None
    restricted: Optional[bool] = None
    # Replaces the list. Omitted: a restricted config keeps its list; switching to unrestricted clears it.
    dataset_ids: Optional[List[int]] = None


class StorageConfigResponse(BaseModel):
    id: int
    name: str
    storage_type: str
    bucket_name: str
    region: str
    access_key_id: Optional[str]
    endpoint_url: Optional[str]
    is_public_bucket: bool
    is_default: bool
    bucket_patterns: List[str] = []
    created_at: datetime
    restricted: bool = False
    dataset_ids: List[int] = []

    class Config:
        from_attributes = True


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    bucket_location: Optional[str] = None
    sample_objects: Optional[List[str]] = None


# --- Helpers ---


def _mask_key(access_key: Optional[str]) -> Optional[str]:
    if access_key and len(access_key) > 8:
        return f"{access_key[:4]}...{access_key[-4:]}"
    return access_key


def _clean_patterns(patterns: Optional[List[str]]) -> List[str]:
    """Normalize user-supplied glob patterns: strip whitespace, drop empties,
    de-dupe while preserving order."""
    if not patterns:
        return []
    seen = set()
    out: List[str] = []
    for p in patterns:
        p = (p or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


_RESPONSE_COLUMNS = """id, name, storage_type, bucket_name, region,
               access_key_id, endpoint_url, is_public_bucket,
               is_default, bucket_patterns, created_at, restricted"""


def _row_to_response(row, dataset_ids: List[int]) -> StorageConfigResponse:
    return StorageConfigResponse(
        id=row["id"],
        name=row["name"],
        storage_type=row["storage_type"],
        bucket_name=row["bucket_name"],
        region=row["region"],
        access_key_id=_mask_key(row["access_key_id"]),
        endpoint_url=row["endpoint_url"],
        is_public_bucket=row["is_public_bucket"],
        is_default=row["is_default"],
        bucket_patterns=list(row["bucket_patterns"] or []),
        created_at=row["created_at"],
        restricted=row["restricted"],
        dataset_ids=dataset_ids,
    )


async def _dataset_ids(connection, config_ids: List[int]) -> Dict[int, List[int]]:
    """The datasets each restricted config serves, by config id."""
    rows = await connection.fetch(
        """
        SELECT storage_config_id, array_agg(dataset_id ORDER BY dataset_id) AS ids
        FROM storage_config_datasets WHERE storage_config_id = ANY($1::int[])
        GROUP BY storage_config_id
        """,
        config_ids,
    )
    return {row["storage_config_id"]: list(row["ids"]) for row in rows}


async def _check_scope(
    connection,
    config_id: Optional[int],
    is_default: bool,
    restricted: bool,
    dataset_ids: List[int],
) -> None:
    """Refuse a dataset scope the media proxy couldn't honour as written."""
    if not restricted:
        if dataset_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="dataset_ids only apply to a restricted config; an unrestricted config serves every dataset",
            )
        return
    if is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The default storage config can't be restricted: "
            "it serves every dataset without a config of its own",
        )
    found = await connection.fetch("SELECT id FROM datasets WHERE id = ANY($1::int[])", dataset_ids)
    missing = sorted(set(dataset_ids) - {row["id"] for row in found})
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown dataset ids: {missing}")
    if config_id is not None:
        # A dataset assigned this config (datasets.storage_config_id) must stay on its list, or its own
        # storage config would stop serving it.
        unlisted = await connection.fetch(
            "SELECT name FROM datasets WHERE storage_config_id = $1 AND NOT (id = ANY($2::int[])) ORDER BY name",
            config_id,
            dataset_ids,
        )
        if unlisted:
            names = ", ".join(row["name"] for row in unlisted)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"These datasets use this storage config as their own, so it must list them: {names}. "
                "Add them, or assign them another config first.",
            )


async def _set_dataset_ids(connection, config_id: int, restricted: bool, dataset_ids: List[int]) -> None:
    await connection.execute("DELETE FROM storage_config_datasets WHERE storage_config_id = $1", config_id)
    if restricted and dataset_ids:
        await connection.execute(
            "INSERT INTO storage_config_datasets (storage_config_id, dataset_id) SELECT $1, unnest($2::int[])",
            config_id,
            dataset_ids,
        )


async def require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required for storage configuration",
        )
    return user


# --- Endpoints ---


@router.get("/configs", response_model=List[StorageConfigResponse])
async def list_storage_configs(user: dict = Depends(require_admin)):
    rows = await db.fetch(f"""
        SELECT {_RESPONSE_COLUMNS}
        FROM storage_configs
        ORDER BY is_default DESC, name
    """)
    scopes = await _dataset_ids(db, [row["id"] for row in rows])
    return [_row_to_response(row, scopes.get(row["id"], [])) for row in rows]


@router.post("/configs", response_model=StorageConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_storage_config(
    config: StorageConfigCreate,
    user: dict = Depends(require_admin),
):
    encrypted_secret = encrypt_secret(config.secret_access_key) if config.secret_access_key else None
    dataset_ids = sorted(set(config.dataset_ids))
    try:
        async with db.pool.acquire() as connection, connection.transaction():
            await _check_scope(connection, None, config.is_default, config.restricted, dataset_ids)
            row = await connection.fetchrow(
                f"""
                INSERT INTO storage_configs
                    (name, storage_type, bucket_name, region, access_key_id,
                     secret_access_key_encrypted, endpoint_url, is_public_bucket,
                     is_default, bucket_patterns, created_by_user_id, restricted)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING {_RESPONSE_COLUMNS}
            """,
                config.name,
                config.storage_type,
                config.bucket_name,
                config.region,
                config.access_key_id,
                encrypted_secret,
                config.endpoint_url,
                config.is_public_bucket,
                config.is_default,
                _clean_patterns(config.bucket_patterns),
                user.get("id"),
                config.restricted,
            )
            await _set_dataset_ids(connection, row["id"], config.restricted, dataset_ids)
        return _row_to_response(row, dataset_ids)
    except HTTPException:
        raise
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Storage config with name '{config.name}' already exists",
            )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put("/configs/{config_id}", response_model=StorageConfigResponse)
async def update_storage_config(
    config_id: int,
    config: StorageConfigUpdate,
    user: dict = Depends(require_admin),
):
    updates = []
    values = []
    idx = 1

    for field, column in [
        ("name", "name"),
        ("bucket_name", "bucket_name"),
        ("region", "region"),
        ("access_key_id", "access_key_id"),
        ("endpoint_url", "endpoint_url"),
        ("is_public_bucket", "is_public_bucket"),
        ("is_default", "is_default"),
        ("restricted", "restricted"),
    ]:
        val = getattr(config, field)
        if val is not None:
            updates.append(f"{column} = ${idx}")
            values.append(val)
            idx += 1

    if config.secret_access_key is not None:
        updates.append(f"secret_access_key_encrypted = ${idx}")
        values.append(encrypt_secret(config.secret_access_key))
        idx += 1

    if config.bucket_patterns is not None:
        updates.append(f"bucket_patterns = ${idx}")
        values.append(_clean_patterns(config.bucket_patterns))
        idx += 1

    if not updates and config.dataset_ids is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    async with db.pool.acquire() as connection, connection.transaction():
        current = await connection.fetchrow(
            "SELECT is_default, restricted FROM storage_configs WHERE id = $1 FOR UPDATE",
            config_id,
        )
        if not current:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Storage config {config_id} not found")
        restricted = current["restricted"] if config.restricted is None else config.restricted
        is_default = bool(current["is_default"]) if config.is_default is None else config.is_default
        if config.dataset_ids is not None:
            dataset_ids = sorted(set(config.dataset_ids))
        elif restricted:
            dataset_ids = (await _dataset_ids(connection, [config_id])).get(config_id, [])
        else:
            dataset_ids = []
        await _check_scope(connection, config_id, is_default, restricted, dataset_ids)

        if updates:
            values.append(config_id)
            row = await connection.fetchrow(
                f"""
                UPDATE storage_configs
                SET {', '.join(updates)}
                WHERE id = ${idx}
                RETURNING {_RESPONSE_COLUMNS}
            """,
                *values,
            )
        else:
            row = await connection.fetchrow(f"SELECT {_RESPONSE_COLUMNS} FROM storage_configs WHERE id = $1", config_id)
        await _set_dataset_ids(connection, config_id, restricted, dataset_ids)
    # Media requests re-read storage_configs on every request, and each worker rebuilds its cached S3
    # client when updated_at changes, so this applies on every worker without an explicit flush.
    return _row_to_response(row, dataset_ids)


@router.delete("/configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_storage_config(config_id: int, user: dict = Depends(require_admin)):
    # Refuse to delete the default out from under the rest of the system. The
    # admin must explicitly pick a new default (or clear is_default) first,
    # otherwise image serving silently falls back to env-var creds.
    existing = await db.fetchrow(
        "SELECT is_default FROM storage_configs WHERE id = $1",
        config_id,
    )
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Storage config {config_id} not found")
    if existing["is_default"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the default storage config. Mark another config as default first.",
        )
    # Datasets that referenced this config FK-cascade to NULL, and its dataset list is deleted with it.
    # Media requests re-read storage_configs, so every worker stops using it at once.
    await db.execute("DELETE FROM storage_configs WHERE id = $1", config_id)


@router.post("/configs/{config_id}/test", response_model=TestConnectionResponse)
async def test_storage_connection(config_id: int, user: dict = Depends(require_admin)):
    if not HAS_BOTO3:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="boto3 is not installed. Install with: pip install boto3",
        )

    row = await db.fetchrow(
        """
        SELECT bucket_name, region, access_key_id, secret_access_key_encrypted,
               endpoint_url, is_public_bucket
        FROM storage_configs WHERE id = $1
    """,
        config_id,
    )

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Storage config {config_id} not found")

    bucket_name = row["bucket_name"]
    region = row["region"]
    access_key_id = row["access_key_id"]
    encrypted_secret = row["secret_access_key_encrypted"]
    endpoint_url = row["endpoint_url"]
    is_public = row["is_public_bucket"]

    try:
        client_kwargs = {"service_name": "s3", "region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if not is_public and access_key_id and encrypted_secret:
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = decrypt_secret(encrypted_secret)

        s3_client = boto3.client(**client_kwargs)

        try:
            loc = s3_client.get_bucket_location(Bucket=bucket_name)
            bucket_location = loc.get("LocationConstraint") or "us-east-1"
        except ClientError as e:
            if e.response["Error"]["Code"] == "AccessDenied":
                bucket_location = "unknown (no GetBucketLocation permission)"
            else:
                raise

        list_resp = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=5)
        sample_objects = [obj["Key"] for obj in list_resp.get("Contents", [])]

        return TestConnectionResponse(
            success=True,
            message=f"Successfully connected to bucket '{bucket_name}'",
            bucket_location=bucket_location,
            sample_objects=sample_objects or None,
        )
    except NoCredentialsError:
        return TestConnectionResponse(success=False, message="No credentials provided and bucket is not public")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        return TestConnectionResponse(success=False, message=f"AWS Error ({code}): {msg}")
    except Exception as e:
        return TestConnectionResponse(success=False, message=f"Connection failed: {str(e)}")


@router.get("/configs/{config_id}", response_model=StorageConfigResponse)
async def get_storage_config(config_id: int, user: dict = Depends(require_admin)):
    row = await db.fetchrow(f"SELECT {_RESPONSE_COLUMNS} FROM storage_configs WHERE id = $1", config_id)

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Storage config {config_id} not found")
    return _row_to_response(row, (await _dataset_ids(db, [config_id])).get(config_id, []))

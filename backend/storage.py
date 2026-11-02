import configparser
import fnmatch
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

# Make boto3 optional - only needed for S3
try:
    import boto3
    from botocore.client import Config

    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False
    boto3 = None  # type: ignore[assignment]  # sentinel when boto3 absent; S3 paths check storage_type first
    Config = None  # type: ignore[assignment,misc]  # same optional-dependency sentinel


# How long a per-dataset StorageBackend stays in `_storage_cache` before being
# rebuilt from the DB. Explicit `invalidate_storage_cache(config_id)` calls from
# the storage router make admin updates instant; this TTL is just a safety net
# for multi-worker drift and direct DB edits. 15 min keeps DB+decrypt overhead
# negligible while bounding staleness on credential rotations.
STORAGE_CONFIG_CACHE_TTL = 15 * 60


class StorageConfig:
    """Configuration for file storage (local or S3)"""

    def __init__(self):
        self.storage_type = os.getenv("STORAGE_TYPE", "local")  # 'local' or 's3'
        self.local_data_root = os.getenv("FRIDGE_DATA_ROOT", os.path.join(os.getcwd(), "data"))

        # S3 Configuration
        self.s3_bucket = os.getenv("S3_BUCKET")
        self.s3_region = os.getenv("S3_REGION", "us-east-1")
        self.s3_access_key = os.getenv("S3_ACCESS_KEY_ID")
        self.s3_secret_key = os.getenv("S3_SECRET_ACCESS_KEY")
        self.s3_endpoint_url = os.getenv("S3_ENDPOINT_URL")  # For MinIO or custom S3

        # S3 Local Cache (hybrid mode)
        # If set, check this path for S3 files before falling back to S3
        # Example: /mnt/s3-cache or /Users/.../s3-mirror
        self.s3_local_cache_path = os.getenv("S3_LOCAL_CACHE_PATH")

        # Env-var configs always carry credentials (or use the ambient chain);
        # the "public bucket / anonymous" mode is only expressed via the DB.
        self.is_public_bucket = False

        # Try loading from INI file if environment vars not set
        if not self.s3_access_key:
            self._load_from_ini()

    @classmethod
    def from_db_row(cls, row: dict, decrypted_secret: Optional[str]) -> "StorageConfig":
        """
        Construct a config from a `storage_configs` row plus the already-decrypted
        secret. Does NOT read environment variables — these credentials are entirely
        DB-driven. Always represents an S3-style backend (only type the table stores).
        """
        c = cls.__new__(cls)
        c.storage_type = 's3'
        c.local_data_root = os.getenv("FRIDGE_DATA_ROOT", os.path.join(os.getcwd(), "data"))
        c.s3_bucket = row['bucket_name']
        c.s3_region = row.get('region') or 'us-east-1'
        c.s3_access_key = row.get('access_key_id')
        c.s3_secret_key = decrypted_secret or None
        c.s3_endpoint_url = row.get('endpoint_url')
        c.s3_local_cache_path = os.getenv("S3_LOCAL_CACHE_PATH")
        # When true, the serving client issues anonymous (unsigned) requests
        # instead of signing with (possibly absent) credentials.
        c.is_public_bucket = bool(row.get('is_public_bucket'))
        return c

    def _load_from_ini(self):
        """Load S3 credentials from ~/.aws/credentials or custom INI file"""
        ini_path = os.getenv("FRIDGE_CONFIG", str(Path.home() / ".aws" / "credentials"))

        if not os.path.exists(ini_path):
            return

        config = configparser.ConfigParser()
        config.read(ini_path)

        # Try 'fridge' section first, then 'default'
        section = 'fridge' if config.has_section('fridge') else 'default'

        if config.has_section(section):
            self.s3_access_key = config.get(section, 'aws_access_key_id', fallback=None)
            self.s3_secret_key = config.get(section, 'aws_secret_access_key', fallback=None)
            self.s3_region = config.get(section, 'region', fallback=self.s3_region)

            # Freedom Ridge-specific settings
            if config.has_option(section, 's3_bucket'):
                self.s3_bucket = config.get(section, 's3_bucket')
            if config.has_option(section, 's3_endpoint_url'):
                self.s3_endpoint_url = config.get(section, 's3_endpoint_url')


class StorageBackend:
    """Unified interface for local and S3 storage"""

    def __init__(self, config: Optional[StorageConfig] = None):
        self.config = config or StorageConfig()
        self._s3_client: Optional["S3Client"] = None

    @property
    def s3_client(self):
        """Lazy initialization of S3 client.

        Tuned for serving lots of small images under concurrent load:
          - max_pool_connections=50: default is 10, which bottlenecks when a
            grid fires 100+ thumbnails in parallel.
          - connect_timeout=5 / read_timeout=30: bounds how long a hung S3
            response can pin a worker thread before failing fast.
        """
        if self._s3_client is None and self.config.storage_type == 's3':
            base_cfg: dict[str, Any] = dict(max_pool_connections=50, connect_timeout=5, read_timeout=30)
            if getattr(self.config, 'is_public_bucket', False):
                # Public bucket → anonymous (unsigned) requests. Passing None
                # credentials instead would fall back to the ambient chain (env,
                # ~/.aws, EC2 instance role) and then either raise
                # NoCredentialsError or sign with the wrong identity — which is
                # exactly why public-bucket media used to 500. UNSIGNED makes the
                # GETs genuinely anonymous, matching what the connection test does.
                from botocore import UNSIGNED

                self._s3_client = boto3.client(
                    's3',
                    region_name=self.config.s3_region,
                    endpoint_url=self.config.s3_endpoint_url,
                    config=Config(signature_version=UNSIGNED, **base_cfg),
                )
            else:
                self._s3_client = boto3.client(
                    's3',
                    aws_access_key_id=self.config.s3_access_key,
                    aws_secret_access_key=self.config.s3_secret_key,
                    region_name=self.config.s3_region,
                    endpoint_url=self.config.s3_endpoint_url,
                    config=Config(**base_cfg),
                )
        return self._s3_client

    def get_file_url(self, filepath: str) -> str:
        """
        Convert internal filepath to public URL.

        Hybrid Mode: For S3 paths, checks local cache first.
        - If file exists in S3_LOCAL_CACHE_PATH, returns local URL (fast)
        - Otherwise returns S3 URL (slower but works)

        Args:
            filepath: Either absolute local path or s3://bucket/key format

        Returns:
            URL accessible by frontend (via nginx proxy)
        """
        # S3 URL - check local cache first (hybrid mode)
        if filepath.startswith('s3://'):
            # Extract bucket and key
            parts = filepath[5:].split('/', 1)
            key = parts[1] if len(parts) > 1 else ''

            # Hybrid mode: Check if file exists in local cache
            if self.config.s3_local_cache_path:
                local_cache_file = os.path.join(self.config.s3_local_cache_path, key)
                if os.path.exists(local_cache_file):
                    # File exists locally! Return local URL
                    rel_path = os.path.relpath(local_cache_file, self.config.s3_local_cache_path)
                    return f"/media/{rel_path}"

            # Fall back to S3 proxy URL
            return f"/media/s3/{key}"

        # Local file - maintain backward compatibility
        # Keep same URL format as before (/media/path)
        else:
            # Already a URL - return as-is
            if filepath.startswith('/media/'):
                return filepath

            # Convert absolute path to relative URL
            if os.path.isabs(filepath):
                # Check if this is a host path that needs translation to container path
                host_data_root = os.getenv("HOST_DATA_ROOT")
                if host_data_root and filepath.startswith(host_data_root):
                    # Translate host path to container path
                    rel_path = os.path.relpath(filepath, host_data_root)
                    return f"/media/{rel_path}"

                # Try to make it relative to data_root
                try:
                    if filepath.startswith(self.config.local_data_root):
                        rel_path = os.path.relpath(filepath, self.config.local_data_root)
                        return f"/media/{rel_path}"
                    else:
                        # Path outside data_root - just use basename
                        return f"/media/{os.path.basename(filepath)}"
                except Exception:
                    return f"/media/{os.path.basename(filepath)}"
            else:
                # Already relative path
                return f"/media/{filepath}"

    def generate_presigned_url(self, s3_key: str, expiration: int = 3600) -> str:
        """
        Generate presigned URL for private S3 objects.
        Used when nginx can't proxy (e.g., private buckets with IAM)

        Args:
            s3_key: S3 object key
            expiration: URL validity in seconds (default 1 hour)

        Returns:
            Presigned URL
        """
        if self.config.storage_type != 's3':
            raise ValueError("Presigned URLs only available for S3 storage")

        url = self.s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.config.s3_bucket, 'Key': s3_key},
            ExpiresIn=expiration,
        )
        return url

    def file_exists(self, filepath: str) -> bool:
        """Check if file exists in storage backend"""
        if filepath.startswith('s3://'):
            # S3 check
            parts = filepath[5:].split('/', 1)
            bucket = parts[0]
            key = parts[1] if len(parts) > 1 else ''

            try:
                self.s3_client.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                return False
        else:
            # Local file check
            full_path = filepath if os.path.isabs(filepath) else os.path.join(self.config.local_data_root, filepath)
            return os.path.exists(full_path)

    def normalize_filepath(self, filepath: str) -> str:
        """
        Normalize filepath for storage in database.

        - Local files: Store as absolute paths
        - S3 files: Store as s3://bucket/key

        This allows mixing local and S3 files in same dataset.
        """
        if filepath.startswith('s3://'):
            return filepath
        elif filepath.startswith('http://') or filepath.startswith('https://'):
            # Convert HTTP S3 URL to s3:// format if possible
            if self.config.s3_bucket and self.config.s3_bucket in filepath:
                # Extract key from URL
                # Example: https://bucket.s3.amazonaws.com/path/to/file.jpg -> s3://bucket/path/to/file.jpg
                try:
                    from urllib.parse import urlparse

                    parsed = urlparse(filepath)
                    path = parsed.path.lstrip('/')
                    return f"s3://{self.config.s3_bucket}/{path}"
                except Exception:
                    return filepath
            return filepath
        else:
            # Local file - convert to absolute path
            if os.path.isabs(filepath):
                return filepath
            else:
                return os.path.join(self.config.local_data_root, filepath)


# Global instance
storage = StorageBackend()


def get_storage() -> StorageBackend:
    """Dependency injection helper for FastAPI"""
    return storage


# --- Per-dataset storage backend lookup ---
#
# Two-tier fallback:
#   1. dataset.storage_config_id set → use that storage_configs row
#   2. else fall back to is_default=TRUE storage_configs row
#   3. else fall back to env-var global singleton
#
# Cached for STORAGE_CONFIG_CACHE_TTL to avoid rebuilding boto3 clients
# (DB query + Fernet decrypt + boto3 init) on every image request.

# Cache: storage_config_id -> (StorageBackend, created_at_epoch_seconds)
_storage_cache: dict[int, tuple[StorageBackend, float]] = {}

# Cache: bucket_name -> (config_id or None, created_at). Lets the hot
# image-fetch path skip the storage_configs SELECT when the same bucket is
# hit repeatedly. A None config_id caches "no matching row" so we don't
# re-query for buckets the system doesn't know about either. Flushed on any
# storage_configs mutation via invalidate_bucket_route_cache().
_bucket_route_cache: dict[str, tuple[Optional[int], float]] = {}


def invalidate_storage_cache(config_id: int) -> None:
    """Drop a cached StorageBackend so the next request rebuilds from DB.

    Called by the storage router whenever a config row is updated or deleted,
    so credential rotations take effect immediately for *this* worker. Other
    workers will pick up the change at most STORAGE_CONFIG_CACHE_TTL later.
    """
    _storage_cache.pop(config_id, None)


def invalidate_bucket_route_cache() -> None:
    """Wipe the bucket_name -> config_id cache.

    Called whenever a storage_configs row is created, updated, or deleted.
    Any of those can change which row a given bucket routes to (new exact
    match, changed bucket_name, added/removed pattern), and we don't know
    from one row's id which buckets were affected — simpler to flush.
    """
    _bucket_route_cache.clear()


async def _backend_from_config_id(config_id: int) -> Optional[StorageBackend]:
    """Build (or fetch from cache) a StorageBackend for a given storage_configs.id.

    Returns None if the row no longer exists (caller falls back further).
    """
    cached = _storage_cache.get(config_id)
    if cached is not None:
        backend, created_at = cached
        if time.time() - created_at < STORAGE_CONFIG_CACHE_TTL:
            return backend

    # Avoid module-load-time circular import: db is a runtime singleton.
    from .crypto import decrypt_secret
    from .database import db

    row = await db.fetchrow(
        """
        SELECT id, bucket_name, region, access_key_id, secret_access_key_encrypted,
               endpoint_url, is_public_bucket
        FROM storage_configs
        WHERE id = $1
        """,
        config_id,
    )
    if not row:
        return None

    secret_enc = row['secret_access_key_encrypted']
    secret = decrypt_secret(secret_enc) if secret_enc else None

    config = StorageConfig.from_db_row(dict(row), secret)
    backend = StorageBackend(config=config)
    _storage_cache[config_id] = (backend, time.time())
    return backend


async def _backend_from_bucket(bucket: str) -> Optional[StorageBackend]:
    """Find a StorageBackend whose configured bucket_name matches `bucket`.

    Resolution order for `bucket`:
      1. Exact match on storage_configs.bucket_name (indexed).
      2. Glob match against any row's bucket_patterns (fnmatch, e.g. "client-*").

    Result is memoized in _bucket_route_cache so repeated image requests for
    the same bucket skip the DB entirely. Cache is flushed whenever any
    storage_configs row is written (see invalidate_bucket_route_cache).
    """
    cached = _bucket_route_cache.get(bucket)
    if cached is not None:
        cfg_id, created_at = cached
        if time.time() - created_at < STORAGE_CONFIG_CACHE_TTL:
            if cfg_id is None:
                return None
            return await _backend_from_config_id(cfg_id)

    from .database import db

    # 1. Exact bucket_name match — cheap, uses idx_storage_configs_bucket_name.
    row = await db.fetchrow(
        "SELECT id FROM storage_configs WHERE bucket_name = $1 ORDER BY id LIMIT 1",
        bucket,
    )
    config_id: Optional[int] = row['id'] if row else None

    # 2. Pattern fallback. We do glob matching in Python (fnmatch) rather than
    #    SQL LIKE so users can type familiar wildcards ("*", "?") instead of
    #    SQL syntax. storage_configs is tiny (one row per bucket config), so
    #    scanning rows with non-empty patterns is effectively free.
    if config_id is None:
        pattern_rows = await db.fetch(
            "SELECT id, bucket_patterns FROM storage_configs WHERE array_length(bucket_patterns, 1) > 0 ORDER BY id",
        )
        for r in pattern_rows:
            for pat in r['bucket_patterns'] or []:
                if fnmatch.fnmatchcase(bucket, pat):
                    config_id = r['id']
                    break
            if config_id is not None:
                break

    _bucket_route_cache[bucket] = (config_id, time.time())
    if config_id is None:
        return None
    return await _backend_from_config_id(config_id)


async def get_storage_for_s3_uri(dataset_id: int, bucket: str) -> StorageBackend:
    """Route an `s3://{bucket}/...` fetch to the right credentials.

    Lookup chain (first match wins):
      1. storage_configs row with `bucket_name = bucket` (exact match)
      2. dataset's `storage_config_id` (may legitimately have creds that cover
         multiple buckets under one IAM role)
      3. `is_default = TRUE` storage_config
      4. env-var global singleton (pre-existing behavior)

    The bucket-first ordering is what makes multi-bucket datasets work: a user
    configures one storage_config per bucket, and every s3:// path auto-routes
    to the credentials for that bucket without touching the dataset config.
    """
    backend = await _backend_from_bucket(bucket)
    if backend is not None:
        return backend
    return await get_storage_for_dataset(dataset_id)


async def is_bucket_authorized(dataset_id: int, bucket: str) -> bool:
    """Whether `dataset_id` may fetch from `bucket`.

    Authorized iff the bucket is explicitly configured (an exact `bucket_name`
    or a `bucket_patterns` glob), OR it is the bucket named on the storage config
    that serves this dataset (its own config, the default, or the env-var global).

    This blocks the confused-deputy / SSRF case: a user who can set a sample's
    media path to ``s3://some-other-bucket/key`` would otherwise make the server
    fetch it with our default IAM credentials via the fallback chain.
    """
    if not bucket:
        return False
    # Explicitly configured for this exact bucket or a matching glob pattern.
    if await _backend_from_bucket(bucket) is not None:
        return True
    # Otherwise it must be the bucket the dataset's resolved backend is for.
    backend = await get_storage_for_dataset(dataset_id)
    return bool(backend and backend.config and backend.config.s3_bucket == bucket)


async def get_storage_for_dataset(dataset_id: int) -> StorageBackend:
    """Return the StorageBackend that should serve media for `dataset_id`.

    Used as the fallback when no bucket-specific match exists, and for callers
    that genuinely just want "this dataset's default storage" (e.g., uploads
    that will go into the dataset's primary bucket).

    Lookup chain:
      1. datasets.storage_config_id → that storage_configs row (cached)
      2. is_default=TRUE storage_configs row (cached)
      3. env-var global singleton (today's behavior, preserved for backward compat)
    """
    from .database import db

    ds = await db.fetchrow(
        "SELECT storage_config_id FROM datasets WHERE id = $1",
        dataset_id,
    )
    if not ds:
        # Caller (the proxy) already verified the sample exists; if the dataset
        # vanished between lookups, fall back rather than 500.
        return storage

    config_id = ds['storage_config_id']
    if config_id is not None:
        backend = await _backend_from_config_id(config_id)
        if backend is not None:
            return backend
        # Config FK was set but the row is gone — try default instead.

    default_row = await db.fetchrow("SELECT id FROM storage_configs WHERE is_default = TRUE LIMIT 1")
    if default_row is not None:
        backend = await _backend_from_config_id(default_row['id'])
        if backend is not None:
            return backend

    return storage

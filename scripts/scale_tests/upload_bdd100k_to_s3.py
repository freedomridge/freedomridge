#!/usr/bin/env python3
"""Upload the local BDD100K images to S3 for a Freedom Ridge *cloud* deployment.

Freedom Ridge can serve images two ways:
  * Local — images on disk under FRIDGE_DATA_ROOT (see the local steps in
    examples/bdd100k/README.md). No S3 needed.
  * Cloud — images in S3, served via the per-dataset storage config. This is the
    deployment people will hit to try Freedom Ridge online.

This script handles the cloud case. It mirrors the local image tree to
``s3://BUCKET/PREFIX/<rel-path>`` so the object keys match EXACTLY what
``examples/bdd100k/ingest.py --s3-bucket BUCKET --s3-prefix PREFIX`` writes as each
sample's filepath. Only the IMAGES are uploaded — BDD's annotations/telemetry are
read locally at ingest time and never served, so they stay on disk.

AWS credentials come from the usual chain (env vars, ~/.aws, or an instance role).
With ``--create-fridge-storage-config`` the same bucket/region/credentials are also
registered as a Freedom Ridge storage config (its secret is encrypted server-side),
and the new config id is printed for you to pass to the ingest with
--storage-config-id.

Note on prefix: the key is PREFIX/<path-relative-to-data_root>, and the relative
path already starts with ``bdd100k/`` (images live at data_root/bdd100k/...). So the
default (no prefix) gives clean keys ``s3://BUCKET/bdd100k/100k_images/...``. Only
pass --s3-prefix to namespace under something else (e.g. one bucket, many datasets).
Whatever you choose, pass the SAME --s3-prefix to the ingest.

Typical workflow:
    # 1. Dry-run — see the file→key mapping and counts, upload nothing
    python upload_bdd100k_to_s3.py --s3-bucket my-bdd-bucket --dry-run

    # 2. Upload, and register the storage config in one go
    python upload_bdd100k_to_s3.py --s3-bucket my-bdd-bucket \
        --region us-east-1 --create-fridge-storage-config
    #    -> prints:  storage_config_id = N

    # 3. Ingest with s3:// filepaths, pointing at that config
    python examples/bdd100k/ingest.py --s3-bucket my-bdd-bucket \
        --storage-config-id N
"""

import argparse
import os
import sys
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples._shared.importer import login  # noqa: E402

DEFAULT_DATA_ROOT = os.path.join(os.path.dirname(__file__), "../../data")
DEFAULT_API_URL = os.getenv("FRIDGE_API_URL", "http://localhost/api")
_IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def _s3_key(image_path: str, data_root: str, prefix: str) -> str:
    """Key for an image = PREFIX/<path relative to data_root>.

    MUST stay in lockstep with the s3:// filepath that examples/bdd100k/ingest.py
    builds (key = f"{prefix}/{rel_path}", rel_path = relpath(image, data_root)).
    """
    rel = os.path.relpath(os.path.abspath(image_path), os.path.abspath(data_root))
    return f"{prefix.strip('/')}/{rel}" if prefix else rel


def _iter_images(images_root: str):
    for dirpath, _dirs, files in os.walk(images_root):
        for f in files:
            if f.lower().endswith(_IMAGE_EXTS):
                yield os.path.join(dirpath, f)


def upload(
    data_root: str,
    bucket: str,
    prefix: str,
    region: str,
    limit: int = 0,
    dry_run: bool = False,
    overwrite: bool = False,
    workers: int = 16,
):
    import boto3
    from botocore.exceptions import ClientError

    images_root = os.path.join(data_root, "bdd100k", "100k_images")
    if not os.path.isdir(images_root):
        print(
            f"❌ Image directory not found: {images_root}\n"
            f"   Download BDD100K first (see README) so {images_root} exists.",
        )
        return

    files = list(_iter_images(images_root))
    if limit and limit > 0:
        files = files[:limit]
    print(f"Found {len(files):,} images under {images_root}")
    if not files:
        return

    sample_key = _s3_key(files[0], data_root, prefix)
    print(f"Example mapping:\n  {files[0]}\n    -> s3://{bucket}/{sample_key}")

    if dry_run:
        print(
            f"\n[dry-run] would upload {len(files):,} objects to s3://{bucket}/"
            f"{(prefix.strip('/') + '/') if prefix else ''} (nothing written)",
        )
        return

    try:
        import tqdm

        pbar = tqdm.tqdm(total=len(files), desc="Uploading", unit="img")
    except ImportError:
        pbar = None

    s3 = boto3.client("s3", region_name=region)
    counts = {"uploaded": 0, "skipped": 0, "errors": 0}
    first_errors: list[str] = []

    def _one(path: str):
        key = _s3_key(path, data_root, prefix)
        if not overwrite:
            try:
                s3.head_object(Bucket=bucket, Key=key)
                return ("skipped", key)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                    raise
        s3.upload_file(path, bucket, key)
        return ("uploaded", key)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, p): p for p in files}
        for fut in as_completed(futs):
            try:
                status, _key = fut.result()
                counts[status] += 1
            except Exception as e:
                counts["errors"] += 1
                if len(first_errors) < 10:
                    first_errors.append(f"{futs[fut]}: {e}")
            if pbar:
                pbar.update(1)
    if pbar:
        pbar.close()

    print(f"\nDone: {counts['uploaded']} uploaded, {counts['skipped']} already present, {counts['errors']} errors")
    for line in first_errors:
        print(f"  {line}")


def create_fridge_storage_config(
    api_url: str,
    username: str,
    password: str,
    bucket: str,
    region: str,
    is_public_bucket: bool,
) -> int | None:
    """Register the bucket as a Freedom Ridge storage config and return its id.

    For a private bucket, the local AWS credentials (from boto3's chain) are sent so
    Freedom Ridge can read it; they're encrypted server-side with
    STORAGE_ENCRYPTION_KEY.
    For a public-read bucket pass --is-public-bucket and no credentials are stored.
    """
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "python"))
    from fridge import connect, get_client

    connect(api_url)
    client = get_client()
    login(client, Namespace(username=username, password=password))

    payload = {
        "name": f"bdd100k-{bucket}",
        "storage_type": "s3",
        "bucket_name": bucket,
        "region": region,
        "is_public_bucket": is_public_bucket,
    }
    if not is_public_bucket:
        import boto3

        creds = boto3.Session().get_credentials()
        if creds is None:
            print(
                "⚠️  No AWS credentials found for the storage config; the bucket "
                "must be public, or set up credentials. Skipping config creation.",
            )
            return None
        frozen = creds.get_frozen_credentials()
        payload["access_key_id"] = frozen.access_key
        payload["secret_access_key"] = frozen.secret_key

    # storage router is mounted at /api/storage; base_url already ends in /api.
    resp = client.session.post(f"{client.base_url}/storage/configs", json=payload)
    if resp.status_code not in (200, 201):
        print(f"❌ Failed to create storage config: {resp.status_code} {resp.text[:300]}")
        return None
    cfg = resp.json()
    print(f"✓ Created storage config '{cfg['name']}' -> storage_config_id = {cfg['id']}")
    return cfg["id"]


def main():
    p = argparse.ArgumentParser(description="Upload BDD100K images to S3 for a Freedom Ridge cloud deploy")
    p.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="Root containing bdd100k/100k_images/")
    p.add_argument("--s3-bucket", required=True, help="Target S3 bucket")
    p.add_argument("--s3-prefix", default="", help="Key prefix (e.g. 'bdd100k'); must match the ingest")
    p.add_argument("--region", default="us-east-1", help="S3 region")
    p.add_argument("--limit", type=int, default=0, help="Max images (0 = all)")
    p.add_argument("--overwrite", action="store_true", help="Re-upload even if the key already exists")
    p.add_argument("--workers", type=int, default=16, help="Parallel upload workers")
    p.add_argument("--dry-run", action="store_true", help="List the mapping and counts, upload nothing")
    p.add_argument(
        "--create-fridge-storage-config",
        action="store_true",
        help="Also register the bucket as a Freedom Ridge storage config and print its id",
    )
    p.add_argument(
        "--is-public-bucket",
        action="store_true",
        help="The bucket allows public/anonymous reads (no credentials stored in Freedom Ridge)",
    )
    p.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="Freedom Ridge API URL (for --create-fridge-storage-config)",
    )
    p.add_argument("--username", default=os.getenv("FRIDGE_USERNAME", "admin"))
    p.add_argument("--password", default=os.getenv("FRIDGE_PASSWORD"))
    args = p.parse_args()

    upload(
        data_root=args.data_root,
        bucket=args.s3_bucket,
        prefix=args.s3_prefix,
        region=args.region,
        limit=args.limit,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        workers=args.workers,
    )

    if args.create_fridge_storage_config and not args.dry_run:
        cid = create_fridge_storage_config(
            api_url=args.api_url,
            username=args.username,
            password=args.password,
            bucket=args.s3_bucket,
            region=args.region,
            is_public_bucket=args.is_public_bucket,
        )
        if cid is not None:
            print(
                f"\nNext: ingest with\n  python examples/bdd100k/ingest.py "
                f"--s3-bucket {args.s3_bucket} --s3-prefix {args.s3_prefix} "
                f"--storage-config-id {cid} --compute-embeddings",
            )


if __name__ == "__main__":
    main()

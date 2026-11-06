import configparser
import fnmatch
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Optional

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


# --- Per-dataset storage routing ---
#
# Which credentials serve s3://{bucket}/... for a dataset is decided on every request from the
# storage_configs table (a handful of rows), so a config added, changed, restricted or deleted through
# one backend worker applies on every worker at once. Only the S3 client is cached, one per config, and
# it is rebuilt when the row's updated_at changes (a trigger bumps it on every UPDATE).
#
# A config serves every dataset unless it is restricted; then it serves only the datasets listed in
# storage_config_datasets.


class StorageRoute(NamedTuple):
    """The credentials that serve a bucket for a dataset: a storage_configs row, or the env-var backend."""

    config_id: Optional[int]  # None: the env-var backend (`storage`)
    updated_at: Optional[datetime]


ENV_ROUTE = StorageRoute(None, None)

# config id -> (backend, the row's updated_at when it was built)
_backend_cache: dict[int, tuple[StorageBackend, datetime]] = {}

_ROUTE_SQL = """
    SELECT c.id, c.bucket_name, c.bucket_patterns, c.is_default, c.restricted, c.updated_at,
           c.id = (SELECT storage_config_id FROM datasets WHERE id = $1) AS assigned,
           s.dataset_id IS NOT NULL AS listed
    FROM storage_configs c
    LEFT JOIN storage_config_datasets s ON s.storage_config_id = c.id AND s.dataset_id = $1
"""


def _covers(row: Any, bucket: str) -> bool:
    return row['bucket_name'] == bucket or any(fnmatch.fnmatchcase(bucket, p) for p in row['bucket_patterns'] or [])


async def route_s3_bucket(dataset_id: int, bucket: str) -> Optional[StorageRoute]:
    """Which credentials serve s3://{bucket}/... for this dataset, or None when it may not read the bucket.

    This is the SSRF / confused-deputy guard as well as the router: a user who can set a sample's media
    path must not be able to make the server fetch an arbitrary bucket with stored credentials.

    Candidates are the configs that cover the bucket (its bucket_name, or a bucket_patterns glob) and
    that this dataset may use (unrestricted, or restricted with the dataset listed). The most specific
    wins: the dataset's own config (datasets.storage_config_id), then a restricted config listing it,
    then one serving every dataset; an exact bucket_name beats a pattern; then the oldest. With no
    candidate, the env-var backend serves its own bucket, but only to a dataset that has neither a
    config of its own nor a default config to fall back to.
    """
    from .database import db

    rows = await db.fetch(_ROUTE_SQL, dataset_id)
    usable = [r for r in rows if _covers(r, bucket) and (not r['restricted'] or r['listed'])]
    if usable:
        best = min(usable, key=lambda r: (not r['assigned'], not r['restricted'], r['bucket_name'] != bucket, r['id']))
        return StorageRoute(best['id'], best['updated_at'])
    if any(r['assigned'] or r['is_default'] for r in rows):
        return None
    return ENV_ROUTE if storage.config.s3_bucket == bucket else None


async def backend_for_route(route: StorageRoute) -> Optional[StorageBackend]:
    """The StorageBackend for a route just chosen by route_s3_bucket (one cached S3 client per config,
    rebuilt when the row's updated_at changes), or None if the config was deleted in between."""
    if route.config_id is None:
        return storage
    cached = _backend_cache.get(route.config_id)
    if cached is not None and cached[1] == route.updated_at:
        return cached[0]

    # Avoid module-load-time circular import: db is a runtime singleton.
    from .crypto import decrypt_secret
    from .database import db

    row = await db.fetchrow(
        """
        SELECT bucket_name, region, access_key_id, secret_access_key_encrypted, endpoint_url,
               is_public_bucket, updated_at
        FROM storage_configs WHERE id = $1
        """,
        route.config_id,
    )
    if row is None:
        return None
    secret_enc = row['secret_access_key_encrypted']
    secret = decrypt_secret(secret_enc) if secret_enc else None
    backend = StorageBackend(config=StorageConfig.from_db_row(dict(row), secret))
    _backend_cache[route.config_id] = (backend, row['updated_at'])
    return backend

"""
Encryption helpers for secrets stored in the database.

Uses Fernet (AES-128-CBC + HMAC) with a key derived from STORAGE_ENCRYPTION_KEY
via PBKDF2/SHA256. Lives in its own module so both `backend.storage` and
`backend.routers.storage` can import it without creating a circular import
(routers depend on storage).

Set STORAGE_ENCRYPTION_KEY in production. The default exists only so dev
installs work out of the box.
"""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .security import production

# "aurora" is the project's former name. This default and the salt below feed the key derivation,
# so changing either makes every value already encrypted unreadable.
_DEFAULT_ENCRYPTION_KEY = "aurora-default-key-change-me"

logger = logging.getLogger(__name__)


def get_encryption_key() -> bytes:
    secret = os.getenv("STORAGE_ENCRYPTION_KEY", _DEFAULT_ENCRYPTION_KEY)
    salt = b"aurora-storage-salt"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode()))


def assert_secure_encryption_key() -> None:
    """Refuse to boot in production if STORAGE_ENCRYPTION_KEY is unset or left
    at the public default.

    Only FRIDGE_ENV=production (or prod) enforces this; see
    backend.security.production(). Any other value, including an unset
    FRIDGE_ENV, only logs a warning, so a shared deployment must set
    FRIDGE_ENV=production explicitly.
    """
    key = os.getenv("STORAGE_ENCRYPTION_KEY", "").strip()
    # Default to dev-mode so a bare `uvicorn backend.main:app` boots. Prod
    # deploys must opt in explicitly with FRIDGE_ENV=production.
    is_prod = production()

    insecure = not key or key == _DEFAULT_ENCRYPTION_KEY

    if insecure and is_prod:
        raise RuntimeError(
            "STORAGE_ENCRYPTION_KEY is unset or left at the public default. "
            "Refusing to start: S3 secret access keys in storage_configs would "
            "be encrypted with a publicly known key and therefore readable by "
            "anyone with a DB dump. Set STORAGE_ENCRYPTION_KEY to a long random "
            "string (e.g. `python -c 'import secrets; print(secrets.token_urlsafe(48))'`) "
            "and rotate any secrets that were stored under the default. "
            "Set FRIDGE_ENV=development to bypass this check for local dev.",
        )

    if insecure:
        logger.warning(
            "STORAGE_ENCRYPTION_KEY not set; using the built-in dev default. "
            "This is OK for local development but MUST be set before running "
            "in any shared or production environment.",
        )


def warn_insecure_session_cookie() -> None:
    """Loudly WARN (never refuse) if a production deploy emits non-Secure session cookies.

    Gated on FRIDGE_ENV=production, so local/dev (the default) is silent and needs NO TLS/certs —
    the session cookie stays non-Secure and works fine over http://localhost. We warn rather than
    refuse because a fresh prod box legitimately runs plain HTTP until TLS is terminated, and a
    Secure cookie can't be sent over HTTP (so an operator may keep it false during that bootstrap
    window). The warning just makes a sniffable-session prod deploy impossible to ship SILENTLY.
    """
    is_prod = production()
    # Mirror auth.py's resolution (default true; only an explicit "false" disables Secure).
    secure = os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() != "false"
    if is_prod and not secure:
        logger.warning(
            "FRIDGE_ENV=production but SESSION_COOKIE_SECURE=false — session cookies are sent "
            "WITHOUT the Secure flag and are sniffable/replayable over plain HTTP. Terminate TLS "
            "(nginx :443 or an ALB/CloudFront + ACM cert) and set SESSION_COOKIE_SECURE=true. "
            "Leaving it false is only acceptable during a pre-TLS bootstrap.",
        )


def hash_token(raw: str) -> str:
    """One-way hash for API tokens stored in the DB.

    The token already carries 256 bits of entropy (``secrets.token_urlsafe(32)``),
    so a fast SHA-256 is sufficient — no per-token salt or bcrypt needed — and it
    keeps the unique-index lookup fast. We store this digest instead of the
    plaintext so a database dump can't be replayed as bearer credentials. The
    digest is 64 hex chars, which fits the existing ``token VARCHAR(64)`` column.
    """
    return hashlib.sha256(raw.encode()).hexdigest()


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    f = Fernet(get_encryption_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    f = Fernet(get_encryption_key())
    return f.decrypt(ciphertext.encode()).decode()

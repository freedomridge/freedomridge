# Trigger reload, embeddings, auth
import asyncio
import io
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from . import backups, system_status  # system_status records at import when this process started and its commit
from .api_errors import CodedHTTPException
from .colormaps import COLORMAPS
from .crypto import assert_secure_encryption_key, warn_insecure_session_cookie
from .database import lifespan
from .media_cache import CacheFill, read_cache, write_cache
from .request_limits import RequestLimitsMiddleware
from .routers import (
    auth,
    backup_settings,
    compute_launcher,
    concepts,
    datasets,
    embeddings,
    filter_query,
    followers,
    hashes,
    index_advisor,
    jobs,
    primaries,
    quality,
    sampling,
    schedules,
    search,
    storage,
    tokens,
    users,
    versions,
    views,
    workers,
)
from .security import production
from .version import VERSION

# Fail fast in prod if the storage-secret encryption key is missing/default.
# Local dev (FRIDGE_ENV=development) just logs a warning.
assert_secure_encryption_key()
# Loudly warn (never refuse) if a production deploy is emitting non-Secure session cookies.
# Silent in dev — non-prod needs no TLS/certs and keeps working over plain http://localhost.
warn_insecure_session_cookie()

app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestLimitsMiddleware)


class VersionHeaderMiddleware:
    """Every HTTP answer says which release made it (X-Fridge-Version), so the SDK can refuse a server of another
    major version instead of misreading its answers (python/fridge/core/client.py). Plain ASGI, so streamed and
    file answers get the header too; only a 500 from Starlette's outermost error handler goes without it, and the
    SDK lets an answer without the header through. A request from an SDK of another major version (its
    X-Fridge-SDK-Version, sent from 2.0 on) is refused with 426. SDKs before 2.0 send no version and aren't
    refused: the upgrade notes say to upgrade the SDK with the server."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        sdk = dict(scope.get('headers') or []).get(b'x-fridge-sdk-version', b'').decode('latin-1')
        if sdk and sdk.split('.')[0] != VERSION.split('.')[0]:
            detail = (
                f'This server runs Freedom Ridge {VERSION}, and this SDK is {sdk}. An SDK works only with a server '
                f'of its own major version: pip install "freedomridge=={VERSION}"'
            )
            response = JSONResponse(status_code=426, content={'detail': detail})
            response.headers['X-Fridge-Version'] = VERSION
            return await response(scope, receive, send)

        async def send_with_version(message):
            if message['type'] == 'http.response.start':
                message['headers'] = [*message.get('headers', []), (b'x-fridge-version', VERSION.encode())]
            await send(message)

        await self.app(scope, receive, send_with_version)


app.add_middleware(VersionHeaderMiddleware)


@app.exception_handler(CodedHTTPException)
async def coded_http_error(request: Request, exc: CodedHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={'detail': exc.detail, 'code': exc.code},
        headers=exc.headers,
    )


DATA_ROOT = os.getenv("FRIDGE_DATA_ROOT", os.path.join(os.getcwd(), "data"))
# NOTE: there is deliberately NO `app.mount("/media", StaticFiles(DATA_ROOT))`:
# it would serve the entire data tree with NO auth. Every image, local or S3, is
# served through the access-controlled /api/samples/{id}/media route, which
# enforces check_dataset_access and confines local reads to FRIDGE_DATA_ROOT.

# CORS
origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# --- CSRF defense for cookie-authenticated requests ---------------------------
# A session cookie is sent automatically by the browser on cross-site requests,
# so a malicious page could drive state-changing calls on a logged-in user's
# behalf (SameSite=Lax doesn't fully cover this). We require that unsafe-method
# requests that carry a session cookie originate from an allowed Origin.
#
# Deliberately permissive where it must be, to avoid regressions:
#   - Safe methods (GET/HEAD/OPTIONS) are never blocked (CORS preflight still works).
#   - Bearer-token requests are exempt (no ambient cookie credential = no CSRF).
#   - Requests with NO Origin header are allowed: that's non-browser clients
#     (the Python SDK, curl) and same-origin server-proxied calls. Browsers
#     always attach Origin on cross-site unsafe requests, which is exactly the
#     case we want to block.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Normalize (strip a trailing slash) so a stray "https://app/" in ALLOWED_ORIGINS
# vs the browser's "https://app" Origin doesn't silently 403 every write.
_ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in origins if o.strip()}
_csrf_block_logged = False


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    if request.method not in _CSRF_SAFE_METHODS:
        has_session_cookie = request.cookies.get("session_id") is not None
        is_bearer = request.headers.get("Authorization", "").startswith("Bearer ")
        if has_session_cookie and not is_bearer:
            origin = request.headers.get("origin")
            if origin is not None and origin.rstrip("/") not in _ALLOWED_ORIGINS:
                global _csrf_block_logged
                if not _csrf_block_logged:
                    # Log once so an ALLOWED_ORIGINS misconfig (scheme/host/slash
                    # mismatch) is diagnosable instead of a silent wall of 403s.
                    _csrf_block_logged = True
                    log.warning(
                        "CSRF guard blocked Origin %r (not in ALLOWED_ORIGINS=%r). "
                        "If legitimate, fix ALLOWED_ORIGINS to the exact scheme+host.",
                        origin,
                        sorted(_ALLOWED_ORIGINS),
                    )
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF check failed: origin not allowed"},
                )
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Report invalid values without echoing non-JSON inputs such as NaN or Infinity."""
    errors = [{key: value for key, value in error.items() if key not in ('input', 'ctx')} for error in exc.errors()]
    return JSONResponse(status_code=422, content={'detail': errors})


@app.exception_handler(asyncpg.DataError)
async def data_error_handler(request: Request, exc: asyncpg.DataError):
    """A Postgres data exception (SQLSTATE class 22) or an argument asyncpg could not encode is
    bad input — `GET /datasets/99999999999999` (beyond int32), a value its column cannot hold —
    not a server fault. Those were 500s with an error_id.
    400 with the first line of the reason; class-22 messages carry no row data (that is class 23,
    integrity, which stays a 500)."""
    from fastapi.responses import JSONResponse

    logging.getLogger("fridge.errors").warning("bad input on %s %s: %s", request.method, request.url.path, exc)
    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return JSONResponse(status_code=400, content={"detail": f"Invalid input: {reason}"})


@app.exception_handler(asyncpg.ObjectNotInPrerequisiteStateError)
async def dataset_lifecycle_error(request: Request, exc: asyncpg.ObjectNotInPrerequisiteStateError):
    # The same body a facet stream gives the fields it had not answered when the refusal came
    # (datasets._refusal): require_current_map names the stale lasso's field and the map's
    # current model, so the UI can drop that lasso instead of leaving every view of it failing.
    return JSONResponse(status_code=409, content=datasets._refusal(exc))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Turn unhandled errors into a diagnosable JSON 500 instead of the bare
    "Internal Server Error" string. Clients (SDK/UI) surface ``detail``; the
    error_id correlates the response with the server-side traceback. In
    production the exception text is withheld (it can leak paths/internals) —
    the error_id still links the report to the log line.
    """
    from fastapi.responses import JSONResponse

    error_id = uuid.uuid4().hex[:12]
    logging.getLogger("fridge.errors").error(
        "unhandled error %s on %s %s",
        error_id,
        request.method,
        request.url.path,
        exc_info=exc,
    )
    # Both spellings, matching crypto.py's production gates — an operator who booted with
    # FRIDGE_ENV=prod is in production and must not receive exception internals.
    detail = "Internal server error" if production() else f"{type(exc).__name__}: {exc}"
    # This handler runs in ServerErrorMiddleware, OUTSIDE CORSMiddleware — without the header
    # a cross-origin browser client can't read the body and the whole point of the JSON is lost.
    origin = request.headers.get("origin")
    headers = (
        {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
        if origin is not None and origin.rstrip("/") in _ALLOWED_ORIGINS
        else None
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"{detail} [error_id={error_id}]", "error_id": error_id},
        headers=headers,
    )


app.include_router(datasets.router, prefix="/api")
app.include_router(search.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(views.router, prefix="/api")
app.include_router(filter_query.router, prefix="/api")
app.include_router(embeddings.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(tokens.router, prefix="/api")
app.include_router(storage.router)  # Note: storage router has its own /api prefix
app.include_router(hashes.router, prefix="/api")
app.include_router(versions.router, prefix="/api")
app.include_router(sampling.router, prefix="/api")
app.include_router(quality.router, prefix="/api")
app.include_router(concepts.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(backup_settings.router, prefix="/api")
app.include_router(schedules.router, prefix="/api")
app.include_router(index_advisor.router, prefix="/api")
app.include_router(workers.router, prefix="/api")
app.include_router(followers.router, prefix="/api")
app.include_router(primaries.router, prefix="/api")
app.include_router(compute_launcher.router, prefix="/api")


@app.get("/api/health")
async def health():
    """Liveness + schema currency, with the running version. 200 {"status": "ok"} when every
    migration file is recorded as applied, the database records none this code lacks, AND every function body matches
    schema.sql; 503 {"status": "degraded", "pending_migrations": [...], "unknown_migrations": [...],
    "stale_functions": [...]} otherwise — so the deploy checklist and a load-balancer health check
    catch a backend serving a database that is behind (a missing migration, a downgraded function)
    or ahead of the code (an older version started after an upgrade, a newer backup restored).
    Unauthenticated; re-checks live on every call (running apply_migrations.sh needs no restart)."""
    from .schema_check import pending_migrations, stale_functions, unknown_migrations
    from .version import VERSION

    pending = await pending_migrations(db)
    unknown = await unknown_migrations(db)
    stale = await stale_functions(db)
    if pending or unknown or stale:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "version": VERSION,
                "pending_migrations": pending,
                "unknown_migrations": unknown,
                "stale_functions": stale,
            },
        )
    return {
        "status": "ok",
        "version": VERSION,
        "pending_migrations": [],
        "unknown_migrations": [],
        "stale_functions": [],
    }


@app.get("/api/admin/status")
async def admin_status(user: dict = Depends(auth.require_role("admin"))):
    """Settings → System status, and the admin low-disk banner (backend/system_status.py)."""
    return await system_status.collect(
        db,
        {"S3 image cache": S3_LOCAL_PATH, "Crop cache": CROP_CACHE_PATH, "Composite cache": COMPOSITE_CACHE_PATH},
        backup_folder=backups.BACKUP_DIR,
    )


@app.get("/")
async def root():
    return {"message": "Freedom Ridge API is running (Python/FastAPI)"}


@app.get("/api/config")
async def get_config():
    """Return public configuration for the frontend."""
    return {"demo_mode": os.getenv("DEMO_MODE", "false").lower() == "true"}


import mimetypes  # noqa: E402

from fastapi.responses import FileResponse, Response, StreamingResponse  # noqa: E402

# NOTE: the old unauthenticated `GET /files?path=` route was removed. It served
# any file under FRIDGE_DATA_ROOT with no dataset-access check, bypassing the
# media access-control hardening. Local images are served exclusively via the
# access-checked /api/samples/{id}/media route.

# Local path that mirrors S3 structure. If set:
#   - Serves files from here instantly (no S3 hit)
#   - Downloads from S3 are saved here, building up the mirror over time
# If not set, falls back to a temp dir cache.
S3_LOCAL_PATH = os.getenv("S3_LOCAL_CACHE_PATH", os.path.join(os.getenv("TMPDIR", "/tmp"), "freedomridge-s3-cache"))
os.makedirs(S3_LOCAL_PATH, exist_ok=True)

# Where composited heatmap-overlay PNGs (grid thumbnails) live. Separate from
# S3_LOCAL_PATH so it can be pointed at a smaller/faster disk (composites are
# cheap to regenerate — they're derived state, not source data).
COMPOSITE_CACHE_PATH = os.getenv(
    "FRIDGE_COMPOSITE_CACHE_PATH",
    os.path.join(os.getenv("TMPDIR", "/tmp"), "freedomridge-composite-cache"),
)
os.makedirs(COMPOSITE_CACHE_PATH, exist_ok=True)

# Where on-the-fly cropped/resized JPEGs (grid thumbnails, per-object crops) live.
# Separate cache dir for the same reason as composites: derived state, cheap to
# regenerate, may want its own disk.
CROP_CACHE_PATH = os.getenv(
    "FRIDGE_CROP_CACHE_PATH",
    os.path.join(os.getenv("TMPDIR", "/tmp"), "freedomridge-crop-cache"),
)
os.makedirs(CROP_CACHE_PATH, exist_ok=True)


_CACHE_MAX_BYTES = max(0, int(os.getenv("FRIDGE_CACHE_MAX_BYTES", str(10 * 1024**3))))


def _write_media_cache(path: str, body: bytes) -> None:
    write_cache(path, body, (CROP_CACHE_PATH, COMPOSITE_CACHE_PATH, S3_LOCAL_PATH), _CACHE_MAX_BYTES)


_CACHE_WRITERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-cache-write")
_CACHE_WRITE_SLOTS = threading.BoundedSemaphore(16)
_pending_cache_writes: set[asyncio.Task] = set()


async def _cache_media_later(path: str, body: bytes) -> None:
    # Called after sending a response. Slow cache storage must not fill the
    # serving executor or retain an unbounded queue of image bodies in memory.
    if not _CACHE_WRITE_SLOTS.acquire(blocking=False):
        return
    try:
        future = _CACHE_WRITERS.submit(_write_media_cache, path, body)
    except Exception:
        _CACHE_WRITE_SLOTS.release()
        raise
    # Cancellation of an HTTP background task cannot release a running writer's
    # slot early: only completion of the actual thread may admit another body.
    future.add_done_callback(lambda _: _CACHE_WRITE_SLOTS.release())
    try:
        await asyncio.wrap_future(future)
    except Exception:
        log.exception("Media cache-write failed")


def _queue_media_cache(path: str, body: bytes) -> None:
    # Internal S3 fetches feed renderers rather than returning a Response. Keep
    # these best-effort fills detached too, with a bounded set of live tasks.
    if len(_pending_cache_writes) >= 16:
        return
    task = asyncio.create_task(_cache_media_later(path, body))
    _pending_cache_writes.add(task)
    task.add_done_callback(_pending_cache_writes.discard)


# A raw S3 original goes to the client a chunk at a time and is never held whole in memory: many large requests at
# once could exhaust the API's memory (SEC-6). Nothing caps its size.
_MEDIA_CHUNK_BYTES = 1024 * 1024


class _S3MediaBody:
    """A raw S3 object's body, read a chunk at a time, and copied into the media cache as it passes when the copy fits
    the cache's budget and a cache-write slot is free. Reads hold a lock, so the copy's end, which runs on a cache
    writer and may come after the client has gone, never overlaps a read still running in a thread."""

    def __init__(self, body):
        self.body, self.copy, self.lock = body, None, threading.Lock()
        self.ended = self.finished = False

    def read(self) -> bytes:
        with self.lock:
            if self.ended or self.finished:
                return b""
            chunk = self.body.read(_MEDIA_CHUNK_BYTES)
            self.ended = not chunk
            if chunk and self.copy is not None:
                self.copy.write(chunk)
            return chunk

    def start_copy(self, path: str, size: int | None, first: bytes) -> None:
        if size is None or size > _CACHE_MAX_BYTES or _CACHE_MAX_BYTES <= 0:
            return
        with self.lock:
            # As _cache_media_later: slow cache storage never queues work without bound.
            if self.finished or not _CACHE_WRITE_SLOTS.acquire(blocking=False):
                return
            try:
                self.copy = CacheFill(path, (CROP_CACHE_PATH, COMPOSITE_CACHE_PATH, S3_LOCAL_PATH), _CACHE_MAX_BYTES)
                self.copy.write(first)
            except Exception:
                if self.copy is not None:
                    self.copy.discard()
                self.copy = None
                _CACHE_WRITE_SLOTS.release()
                log.exception("Media cache-write failed")

    def finish(self, keep: bool) -> None:
        """Close the body. With `keep` (the stream ended, or the client left), the bytes not read yet are read into
        the cache copy, as the whole object was always read before, and the copy is kept; else it is dropped."""
        with self.lock:
            copy, self.copy, self.finished = self.copy, None, True
            try:
                if copy is not None and keep:
                    while not self.ended and (chunk := self.body.read(_MEDIA_CHUNK_BYTES)):
                        copy.write(chunk)
                    copy.commit()
            except Exception:
                log.exception("Media cache-write failed")
            finally:
                self.body.close()
                if copy is not None:
                    copy.discard()  # nothing left to remove once committed
                    _CACHE_WRITE_SLOTS.release()


async def _s3_media_chunks(stream: _S3MediaBody, first: bytes, cached_path: str, size: int | None):
    """The response body of a raw S3 object: the chunk read with the response, then a chunk at a time."""
    keep = False
    try:
        await asyncio.to_thread(stream.start_copy, cached_path, size, first)
        chunk = first
        while chunk:
            yield chunk
            chunk = await asyncio.to_thread(stream.read)
        keep = True
    except (GeneratorExit, asyncio.CancelledError):
        keep = True  # the client left: the cache copy gets the rest
        raise
    finally:
        # On a cache writer, not the event loop: the lock may wait for a read still running.
        _CACHE_WRITERS.submit(stream.finish, keep)


# Thumbnails, crops and overlays decode the whole image, and an overlay holds several float copies of it, so how many
# render at once is bounded in each API process. A request past the bound waits for a slot; none is refused, and no
# image size is capped. Decoding is CPU-bound, so more renders than CPUs only add memory; renders run on asyncio's
# default thread pool (min(32, CPUs + 4) threads), and a slot is held from reading the source to the encoded result,
# so a fetched source no longer waits in memory for a thread (and slow storage bounds how fast renders go too).
# Cached derivatives and raw media take no slot.
# os.process_cpu_count is Python 3.13+ (requires-python is 3.12); either can answer None.
_MEDIA_RENDERS = max(1, int(os.getenv("FRIDGE_MEDIA_RENDERS") or getattr(os, "process_cpu_count", os.cpu_count)() or 1))
_render_limit: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _render_slot() -> asyncio.Semaphore:
    """The render bound for the running event loop (one per process; tests start a loop each)."""
    global _render_limit
    loop = asyncio.get_running_loop()
    if _render_limit is None or _render_limit[0] is not loop:
        _render_limit = (loop, asyncio.Semaphore(_MEDIA_RENDERS))
    return _render_limit[1]


def _thumbnail_width(width: int | None) -> int | None:
    if width is None:
        return None
    if not 1 <= width <= 4096:
        raise HTTPException(400, "w must be in [1, 4096]")
    # dataset-grid.tsx requests 256..1024 in 128px steps. Larger requests get
    # the full image, which remains available without a width parameter.
    return min(1024, max(256, math.ceil(width / 128) * 128)) if width <= 1024 else None


def _overlay_range(value: float) -> float:
    if not math.isfinite(value) or not -65535 <= value <= 65535:
        raise HTTPException(400, "Overlay range must be finite and in [-65535, 65535]")
    return round(value, 2) + 0.0


# --- Sample-based media route ------------------------------------------------
#
# The canonical URL for serving a sample's image. Frontend uses
#   <img src="/api/samples/{id}/media">
# regardless of whether the underlying file is on disk or in S3, and regardless
# of which storage config the dataset uses. The backend resolves all of that
# from the sample → dataset → storage_config chain, with access checks.

import logging  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402

from fastapi.responses import RedirectResponse  # noqa: E402

from .access import check_dataset_access, missing_unless_allowed  # noqa: E402
from .database import db  # noqa: E402
from .routers.auth import get_optional_user  # noqa: E402
from .storage import backend_for_route, route_s3_bucket  # noqa: E402

log = logging.getLogger("fridge.media")

# Field name validator — must match samples.* column or metadata key. Same
# pattern the rest of the codebase uses for safe identifiers.
_SAFE_FIELD_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{0,62}$')


def _parse_s3_path(s3_path: str) -> tuple[str, str]:
    """`s3://bucket/key/with/slashes` -> ('bucket', 'key/with/slashes')."""
    rest = s3_path[5:]  # strip "s3://"
    if '/' not in rest:
        return rest, ''
    bucket, key = rest.split('/', 1)
    return bucket, key


def _validate_s3_key(key: str) -> None:
    """Reject S3 keys that would escape the dataset-scoped cache directory.

    The cache path is `S3_LOCAL_PATH/{dataset_id}/{key}`. A malicious DB value
    like `s3://bucket/../../etc/passwd` would parse to key=`../../etc/passwd`
    and os.path.join would happily let it traverse out. Reject anything with
    a `..` segment, a leading `/`, or a NUL byte.

    Raises HTTPException(400) on invalid keys so the caller sees the failure
    clearly instead of silently serving the wrong file.
    """
    if not key:
        raise HTTPException(status_code=400, detail="Empty S3 key")
    if key.startswith('/') or '\x00' in key:
        raise HTTPException(status_code=400, detail=f"Unsafe S3 key: {key}")
    # Normalise path separators and check each segment
    for segment in key.replace('\\', '/').split('/'):
        if segment in ('..', '.'):
            raise HTTPException(status_code=400, detail=f"Unsafe S3 key: {key}")


def _validate_bucket(bucket: str) -> None:
    """Reject bucket names that would escape the cache dir once interpolated
    into `S3_LOCAL_PATH/{dataset_id}/{bucket}/{key}`. The bucket is now part of
    the on-disk cache path (so two buckets with the same key don't collide), so
    a malicious `s3://../key` must not be allowed to traverse out."""
    if not bucket or bucket in ('..', '.') or '/' in bucket or '\\' in bucket or '\x00' in bucket:
        raise HTTPException(status_code=400, detail=f"Unsafe bucket name: {bucket}")


def _resolve_local_path(path: str) -> str:
    """Resolve a local filepath for serving and confirm it stays inside the
    configured FRIDGE_DATA_ROOT (same containment contract the /files route
    enforces). Returns the resolved absolute path.

    Freedom Ridge's schema allows absolute filepaths anywhere on disk in
    principle, but the runtime policy is: local samples must live under
    FRIDGE_DATA_ROOT. Anything else is either a data-entry bug or a
    path-traversal attempt.

    Relative filepaths are anchored to FRIDGE_DATA_ROOT (NOT the process CWD).
    Ingest tools store relative paths for host/container portability (e.g. the
    BDD100K example writes `bdd100k/100k_images/...`); without this join,
    realpath() resolved them against the backend's CWD and every local image
    404/403'd. The containment check below still blocks `../` traversal because
    it runs after the join + realpath.
    """
    if not os.path.isabs(path):
        path = os.path.join(DATA_ROOT, path)
    resolved = os.path.realpath(path)
    data_root = os.path.realpath(DATA_ROOT)
    if resolved != data_root and not resolved.startswith(data_root + os.sep):
        raise HTTPException(
            status_code=403,
            detail="Local filepath is outside FRIDGE_DATA_ROOT",
        )
    return resolved


# --- Media content-type policy --------------------------------------------------
#
# Bytes served by the media routes are user-supplied (any power_user can ingest a
# filepath / S3 key) and land on the app's OWN origin. Served under a type derived from
# the filename or the S3 object's ContentType, an `.svg` or `.html` "sample" opened
# directly (a pasted link, "open image in new tab") executes script with the viewer's
# session — stored XSS, power_user -> admin.
#
# Policy: only known raster/video types are served inline. The type is the DECLARED one
# when it is on that list; otherwise the file's own magic bytes decide (S3 objects
# uploaded without a Content-Type come back as binary/octet-stream and must keep
# rendering in <img>); anything else is a download under an opaque type. Every raw media
# response also carries `nosniff` (the browser may not promote the type) and a sandbox
# CSP (a direct navigation to it can never run script).
_INLINE_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/avif",
        "image/heic",
        "image/heif",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-msvideo",
        "video/mpeg",
        "video/ogg",
    },
)
_MEDIA_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)
_MEDIA_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}
_MEDIA_HEAD_BYTES = 16
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sniff_media_type(head: bytes) -> str | None:
    """Image/video type from the leading bytes, or None if they are not a known raster/video."""
    for magic, media_type in _MEDIA_MAGIC:
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"  # ISO base media (MP4 / MOV / HEIC) — all inline-safe
    return None


def _inline_media_type(declared: str | None, head: bytes) -> str | None:
    """The type a media response may be served inline as, or None (-> opaque download)."""
    declared_type = (declared or "").split(";", 1)[0].strip().lower()
    if declared_type in _INLINE_MEDIA_TYPES:
        return declared_type
    return _sniff_media_type(head)


def _media_response_kwargs(declared: str | None, head: bytes, filename: str, **extra_headers: str) -> dict:
    """media_type + headers for a raw media response (FileResponse or Response)."""
    headers = dict(_MEDIA_HEADERS, **extra_headers)
    media_type = _inline_media_type(declared, head)
    if media_type is None:
        media_type = "application/octet-stream"
        safe_name = _SAFE_FILENAME_RE.sub("_", os.path.basename(filename)) or "file"
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return {"media_type": media_type, "headers": headers}


# Full-size media (the sample modal's image) went out with an ETag but no Cache-Control, and a
# revalidation with that ETag re-sent the whole file (thumbnails were
# already immutable-cached). `private`: media is behind login, shared caches must not keep it.
# An hour like the overlay composites, then a cheap 304 revalidation — the URL is per-sample, not
# per-content, so it can't be `immutable`.
_FULL_MEDIA_CACHE_CONTROL = "private, max-age=3600"


def _is_not_modified(request: Request, headers) -> bool:
    """True when the client's copy is current: If-None-Match names this ETag (weak compare), or —
    only without If-None-Match, per RFC 9110 — If-Modified-Since is at or after Last-Modified."""
    if_none_match = request.headers.get("if-none-match")
    etag = headers.get("etag")
    if if_none_match is not None:
        if not etag:
            return False
        wanted = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
        return "*" in wanted or etag.removeprefix("W/") in wanted
    if_modified_since = request.headers.get("if-modified-since")
    last_modified = headers.get("last-modified")
    if if_modified_since and last_modified:
        try:
            return parsedate_to_datetime(last_modified) <= parsedate_to_datetime(if_modified_since)
        except (TypeError, ValueError):
            return False
    return False


def _full_media_file_response(request: Request, path: str, **kwargs) -> Response:
    """FileResponse for full-size media with Cache-Control, answering a matching revalidation with
    304 and no body (Starlette's FileResponse sets ETag/Last-Modified but never answers 304)."""
    response = FileResponse(path, stat_result=os.stat(path), **kwargs)
    if _is_not_modified(request, response.headers):
        keep = ("etag", "last-modified", "cache-control")
        return Response(status_code=304, headers={k: v for k, v in response.headers.items() if k in keep})
    return response


def _media_head(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(_MEDIA_HEAD_BYTES)


class _CachedFileResponse(FileResponse):
    """Hold the cache inode across eviction, preserving streaming and HTTP ranges."""

    def __init__(self, cached_file, **kwargs):
        self.cached_file = cached_file
        super().__init__(f'/dev/fd/{cached_file.fileno()}', stat_result=os.fstat(cached_file.fileno()), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            # A server-side pathsend could open the fd path after we close it. Use
            # FileResponse's ordinary streaming/range handling while the inode is pinned.
            extensions = {k: v for k, v in scope.get('extensions', {}).items() if k != 'http.response.pathsend'}
            scope = {**scope, 'extensions': extensions}
            await super().__call__(scope, receive, send)
        finally:
            self.cached_file.close()


def _cached_media_response(request: Request, path: str, key: str) -> Response | None:
    """Open before eviction while retaining full-media revalidation and range support."""
    try:
        cached = open(path, 'rb')
    except FileNotFoundError:
        return None
    try:
        head = cached.read(_MEDIA_HEAD_BYTES)
        cached.seek(0)
        response = _CachedFileResponse(
            cached,
            **_media_response_kwargs(
                mimetypes.guess_type(key)[0],
                head,
                key,
                **{"X-Cache": "HIT", "Cache-Control": _FULL_MEDIA_CACHE_CONTROL},
            ),
        )
        if _is_not_modified(request, response.headers):
            cached.close()
            keep = ('etag', 'last-modified', 'cache-control')
            return Response(status_code=304, headers={k: v for k, v in response.headers.items() if k in keep})
        return response
    except BaseException:
        cached.close()
        raise


def _build_media_path_sql(field: str, versioned: bool = False) -> str:
    """Build SQL to select a media path for a sample.

    Accepts either a simple field name (e.g. `filepath`, `thumbnail_path`) or a
    dotted JSONB path (e.g. `<heatmap_name>.map_path` → `metadata->'<heatmap_name>'->>'map_path'`).
    Dotted paths let callers point at nested media inside structured metadata
    (heatmaps, segmentation masks, etc.) without exposing generic JSON-path queries.

    Every segment is validated against `_SAFE_FIELD_RE` (same identifier rules used
    elsewhere in the codebase), so the composed SQL string contains only vetted names.
    Raises HTTPException(400) on malformed field references.
    """
    segments = field.split(".")
    for seg in segments:
        if not _SAFE_FIELD_RE.match(seg):
            raise HTTPException(status_code=400, detail=f"Invalid field name: {field}")

    source = "samples"
    if versioned:
        source = """(SELECT v.sample_id AS id, v.filepath, v.metadata, d.dataset_id
                     FROM version_snapshots v JOIN dataset_versions d ON d.id = v.version_id
                     WHERE v.version_id = $2 AND d.include_metadata) samples"""
    if field == "filepath":
        return f"SELECT dataset_id, filepath AS path FROM {source} WHERE id = $1"

    # Nested JSONB: intermediate segments use `->` (returns jsonb), final uses `->>`
    # (returns text). e.g. metadata->'<key>'->>'map_path'
    if len(segments) == 1:
        expr = f"metadata->>'{segments[0]}'"
    else:
        path = "".join(f"->'{s}'" for s in segments[:-1])
        expr = f"metadata{path}->>'{segments[-1]}'"

    return f"SELECT dataset_id, {expr} AS path FROM {source} WHERE id = $1"


async def _readable_here(dataset_id: int, path: str) -> bool:
    """Whether this server can read a media path itself: a local file under the data root, an s3:// path in a
    bucket one of the dataset's storage configurations covers, or an external URL (redirected)."""
    if path.startswith(("http://", "https://")):
        return True
    if path.startswith("s3://"):
        bucket, _key = _parse_s3_path(path)
        return bool(bucket) and await route_s3_bucket(dataset_id, bucket) is not None
    try:
        return os.path.exists(_resolve_local_path(path))
    except HTTPException:
        return False  # outside the data root


async def _primary_media_source(dataset_id: int, sample_id: int):
    """The primary and the sample's ids there, when the dataset is a mirror whose primary serves its images."""
    return await db.fetchrow(
        """SELECT m.dataset_id, m.origin_dataset_id, ms.origin_id, l.id AS link_id, l.url, l.token_encrypted
           FROM mirrors m
           JOIN mirror_samples ms ON ms.dataset_id = m.dataset_id AND ms.sample_id = $2
           JOIN primary_links l ON l.id = m.link_id
           WHERE m.dataset_id = $1 AND m.images""",
        dataset_id,
        sample_id,
    )


# One client per primary (and event loop), so a grid's thumbnails reuse its connections.
_primary_clients: dict = {}


async def _primary_client(source):
    from . import follower_sync

    key = (source["link_id"], source["token_encrypted"], id(asyncio.get_running_loop()))
    client = _primary_clients.get(key)
    if client is None:
        client = _primary_clients[key] = follower_sync.connect(source, await follower_sync.installation_id(db))
    return client


async def _media_from_primary(source, path: str, params: dict) -> Response:
    """The primary's answer for this sample and request, cached here under the media cache. The key holds the
    sample's path, so a new file there is fetched again."""
    import glob
    import hashlib
    import json

    key = hashlib.md5(json.dumps([path, params], sort_keys=True).encode(), usedforsecurity=False).hexdigest()
    base = os.path.join(S3_LOCAL_PATH, "primary", str(source["dataset_id"]), str(source["origin_id"]), key)
    thumbnail = any(name in params for name in ("w", "crop", "overlay"))
    headers = {"Cache-Control": "private, max-age=31536000, immutable" if thumbnail else _FULL_MEDIA_CACHE_CONTROL}
    for cached_path in await asyncio.to_thread(glob.glob, glob.escape(base) + ".*"):
        body = await asyncio.to_thread(read_cache, cached_path)
        if body is not None:
            declared = mimetypes.guess_type(cached_path)[0]
            kwargs = _media_response_kwargs(declared, body[:_MEDIA_HEAD_BYTES], path, **headers, **{"X-Cache": "HIT"})
            return Response(content=body, **kwargs)
    url = f"/follower/datasets/{source['origin_dataset_id']}/samples/{source['origin_id']}/media"
    try:
        answer = await (await _primary_client(source)).get(url, params=params)
    except Exception as error:
        log.error("Primary %s unreachable for sample %s: %s", source["url"], source["origin_id"], error)
        raise HTTPException(status_code=502, detail=f"The primary didn't answer for this image: {error}")
    if answer.status_code != 200:
        try:
            detail = answer.json().get("detail")
        except ValueError:
            detail = None
        if answer.status_code in (403, 404):
            raise HTTPException(
                status_code=answer.status_code,
                detail=f"From the primary: {detail or answer.text[:200]}",
            )
        raise HTTPException(status_code=502, detail=f"The primary answered {answer.status_code}: {detail or ''}")
    # As for this instance's own media: only an image or a video is shown, sandboxed and never sniffed, and anything
    # else is a download. The primary's Content-Type doesn't decide what runs on this origin.
    kwargs = _media_response_kwargs(
        answer.headers.get("content-type"),
        answer.content[:_MEDIA_HEAD_BYTES],
        path,
        **headers,
    )
    _queue_media_cache(base + (mimetypes.guess_extension(kwargs["media_type"]) or ".bin"), answer.content)
    return Response(content=answer.content, **kwargs)


_HIGH_BIT_DEPTH_SUFFIXES = (".png", ".tif", ".tiff")


class _S3ObjectFile(io.RawIOBase):
    """An S3 object as a seekable file read by byte ranges, so Pillow reads an image's header without the whole object
    (a PNG's header is in its first bytes; a TIFF's directory can be anywhere in the file)."""

    def __init__(self, client, bucket: str, key: str):
        self.client, self.bucket, self.key = client, bucket, key
        self.position = 0
        self.size: int | None = None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_END and self.size is None:
            raise io.UnsupportedOperation("The object's size isn't known yet")
        self.position = max(0, offset + (0, self.position, self.size or 0)[whence])
        return self.position

    def readinto(self, buffer) -> int:
        if not len(buffer) or (self.size is not None and self.position >= self.size):
            return 0
        try:
            answer = self.client.get_object(
                Bucket=self.bucket,
                Key=self.key,
                Range=f"bytes={self.position}-{self.position + len(buffer) - 1}",
            )
            self.size = int(answer["ContentRange"].rsplit("/", 1)[1])
            body = answer["Body"]
            try:
                data = body.read(len(buffer))
            finally:
                body.close()
        except Exception as error:  # a storage error, or no byte range in the answer: the image isn't read
            raise OSError(f"Couldn't read s3://{self.bucket}/{self.key}: {error}") from error
        buffer[: len(data)] = data
        self.position += len(data)
        return len(data)


async def _is_high_bit_depth(dataset_id: int, path: str) -> bool:
    """Whether a PNG or TIFF holds one channel wider than 8 bits, from its header. An S3 one's header is read from
    its cached copy, or by byte ranges: the whole object isn't fetched for this. A storage error is the full view's
    to report."""
    if not path.lower().endswith(_HIGH_BIT_DEPTH_SUFFIXES):
        return False
    if path.startswith("s3://"):
        await _require_readable_source(dataset_id, path)
        bucket, key = _parse_s3_path(path)
        source: Any = os.path.join(S3_LOCAL_PATH, str(dataset_id), bucket, key)
        if not os.path.exists(source):
            route = await route_s3_bucket(dataset_id, bucket)
            backend = None if route is None else await backend_for_route(route)
            if backend is None or not backend.s3_client:
                return False  # the full view says what is wrong
            source = io.BufferedReader(_S3ObjectFile(backend.s3_client, bucket, key), 64 * 1024)
    else:
        source = _resolve_local_path(path)
        if not os.path.exists(source):
            return False  # the full view answers 404

    def mode() -> str:
        from PIL import Image

        with Image.open(source) as image:
            return image.mode

    from PIL import UnidentifiedImageError

    from .image_modes import is_high_bit_depth

    try:
        return is_high_bit_depth(await asyncio.to_thread(mode))
    except (UnidentifiedImageError, OSError):
        return False  # not an image Pillow reads: served as it is, as before


async def _require_readable_source(dataset_id: int, path: str) -> None:
    """The storage checks _fetch_media_bytes makes before it reads a source, without reading it: a local path must
    stay under the data root, and an s3:// path must be in a bucket this dataset may use now.

    A cached thumbnail, crop or overlay is served only after these, so restricting a storage configuration to other
    datasets stops its derived images too, not only its originals. External URLs are refused where they are fetched.
    """
    if path.startswith(("http://", "https://")):
        return
    if not path.startswith("s3://"):
        _resolve_local_path(path)
        return
    bucket, key = _parse_s3_path(path)
    if not bucket or not key:
        raise HTTPException(status_code=500, detail=f"Malformed s3 path: {path}")
    _validate_s3_key(key)
    _validate_bucket(bucket)
    if await route_s3_bucket(dataset_id, bucket) is None:
        raise HTTPException(status_code=403, detail="Bucket not authorized for this dataset")


async def _fetch_media_bytes(dataset_id: int, path: str) -> tuple[bytes, str]:
    """Fetch the raw bytes for a resolved media path.

    Handles the same three path shapes as the main serve_sample_media route
    (local, s3, http) but always returns bytes — used by the overlay branch,
    which needs to hand the image to PIL for compositing. The non-overlay
    branch still uses FileResponse for zero-copy sendfile() on local files.

    HTTP URLs are rejected here because composite math against a remote image
    would mean proxying arbitrary internet content just to blend it locally.
    External-URL samples don't have overlays in any realistic pipeline.
    """
    if path.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Cannot composite an overlay on an external-URL media source",
        )

    if not path.startswith("s3://"):
        resolved = _resolve_local_path(path)
        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="File not found on disk")
        with open(resolved, "rb") as f:
            body = f.read()
        return body, mimetypes.guess_type(resolved)[0] or "application/octet-stream"

    bucket, key = _parse_s3_path(path)
    if not bucket or not key:
        raise HTTPException(status_code=500, detail=f"Malformed s3 path: {path}")
    _validate_s3_key(key)
    _validate_bucket(bucket)

    # SSRF / confused-deputy guard: only fetch buckets this dataset is authorized
    # for. Without this, a user who can set a sample's media path could point it
    # at s3://arbitrary-bucket/key and the server would fetch it with our IAM creds.
    route = await route_s3_bucket(dataset_id, bucket)
    if route is None:
        raise HTTPException(status_code=403, detail="Bucket not authorized for this dataset")

    # Cache path includes the bucket so the same key in two different buckets
    # (a realistic multi-bucket dataset: images in one, masks/overlays in
    # another) can't collide and serve each other's bytes.
    cached_path = os.path.join(S3_LOCAL_PATH, str(dataset_id), bucket, key)
    cached_body = await asyncio.to_thread(read_cache, cached_path)
    if cached_body is not None:
        return cached_body, mimetypes.guess_type(key)[0] or "application/octet-stream"

    backend = await backend_for_route(route)
    if backend is None or not backend.s3_client:
        raise HTTPException(status_code=500, detail="S3 backend not configured for this dataset")

    def _fetch_blocking():
        resp = backend.s3_client.get_object(Bucket=bucket, Key=key)
        data = resp["Body"].read()
        ct = resp.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
        return data, ct

    try:
        body, content_type = await asyncio.to_thread(_fetch_blocking)
    except Exception as e:
        # Mirror the error mapping used by the main route so admins can tell
        # missing-key from denied-credentials from unreachable-backend.
        try:
            from botocore.exceptions import ClientError
        except ImportError:
            ClientError = ()  # type: ignore
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise HTTPException(status_code=404, detail=f"S3 key not found: {bucket}/{key}")
            if code in ("AccessDenied", "403"):
                log.error("S3 AccessDenied fetching %s/%s with config for dataset %s", bucket, key, dataset_id)
                raise HTTPException(status_code=500, detail="Storage backend denied access (check credentials)")
            log.error("S3 ClientError (%s) fetching %s/%s: %s", code, bucket, key, e)
            raise HTTPException(status_code=500, detail=f"S3 error: {code}")
        log.error("S3 fetch failed for %s/%s: %s", bucket, key, e)
        raise HTTPException(status_code=502, detail="Storage backend unreachable")

    _queue_media_cache(cached_path, body)

    return body, content_type


async def _serve_composite(
    sample_id: int,
    dataset_id: int,
    base_path: str,
    overlay_field: str,
    colormap_override: str | None,
    alpha: float,
    range_start_override: float | None = None,
    range_end_override: float | None = None,
    width: int | None = None,
    version_id: int | None = None,
) -> Response:
    """Fetch base + heatmap, paint the colormap, return the composited PNG.

    Cache layout: ``COMPOSITE_CACHE_PATH/{dataset_id}/{sample_id}/{overlay}-{colormap}-a{alpha}-r{range}.png``.
    Range, colormap, alpha and width are canonicalized before rendering/keying.
    Writes evict oldest cached files to stay within the shared byte budget;
    the hourly cleanup also prunes idle caches.

    Range and colormap are read from the heatmap's own metadata dict (the shape
    the SDK/migration writes: ``{type, map_path, range, colormap?}``). An
    explicit ``?colormap=`` query param overrides the metadata default.
    """
    # Read the heatmap metadata dict. The field must be a top-level metadata key.
    if not _SAFE_FIELD_RE.match(overlay_field):
        raise HTTPException(status_code=400, detail=f"Invalid overlay field: {overlay_field}")
    width = _thumbnail_width(width)

    if version_id is None:
        row = await db.fetchrow(
            f"SELECT metadata->'{overlay_field}' AS hm FROM samples WHERE id = $1",
            sample_id,
        )
    else:
        row = await db.fetchrow(
            f"SELECT metadata->'{overlay_field}' AS hm FROM version_snapshots WHERE sample_id = $1 AND version_id = $2",
            sample_id,
            version_id,
        )
    if not row or row["hm"] is None:
        raise HTTPException(
            status_code=404,
            detail=f"Sample {sample_id} has no heatmap in metadata.{overlay_field}",
        )
    hm = row["hm"]
    # asyncpg's jsonb codec already decodes to dict — but guard if something
    # slipped through as raw text.
    if isinstance(hm, str):
        import json as _json

        try:
            hm = _json.loads(hm)
        except Exception:
            raise HTTPException(status_code=500, detail=f"metadata.{overlay_field} is not valid JSON")
    if not isinstance(hm, dict) or hm.get("type") != "heatmap":
        raise HTTPException(
            status_code=400,
            detail=f"metadata.{overlay_field} is not a heatmap (missing type='heatmap')",
        )
    map_path = hm.get("map_path")
    rng = hm.get("range")
    if not map_path or not isinstance(rng, list) or len(rng) != 2:
        raise HTTPException(
            status_code=400,
            detail=f"metadata.{overlay_field} is missing map_path or range",
        )
    colormap = (colormap_override or hm.get("colormap") or "viridis").strip().lower()
    if colormap not in COLORMAPS:
        colormap = "viridis"
    if not math.isfinite(alpha) or not 0 <= alpha <= 1:
        raise HTTPException(400, "alpha must be in [0, 1]")
    alpha = round(alpha, 2) + 0.0
    # Preserve the order of range — `[start, end]` where start > end is a
    # legitimate way to invert the colormap without remapping colours. UI config
    # overrides (range_*_override) take precedence over the metadata range.
    range_start = range_start_override if range_start_override is not None else float(rng[0])
    range_end = range_end_override if range_end_override is not None else float(rng[1])

    range_start = _overlay_range(range_start)
    range_end = _overlay_range(range_end)

    # Cache key: include every input that affects the output. The path escape
    # checks (dataset_id is int, sample_id is int, overlay_field matches
    # _SAFE_FIELD_RE, colormap is alphanumeric) keep the filename safe.
    import hashlib as _hl

    safe_colormap = colormap
    # Fold a hash of map_path into the key so that re-pointing a heatmap (editing
    # metadata.{overlay}.map_path) busts the cache instead of serving the old
    # composite forever. (Overwriting the PNG *in place* at the same map_path is
    # not covered here — keep heatmap object keys content-addressed/immutable.)
    # usedforsecurity=False: this is a cache-key digest, not a security hash —
    # without it md5 raises on FIPS-mode hosts.
    mp_hash = _hl.md5(f"{base_path}|{map_path}".encode(), usedforsecurity=False).hexdigest()[:16]
    # Grid tiles pass ?w= and get a resized JPEG (a full-res PNG composite is 5-10MB/tile
    # — the problem plain tiles had before they were resized too); the modal omits w and gets the
    # full-res PNG. Width is baked into the cache name so the two don't collide.
    base_name = f"{overlay_field}-{safe_colormap}-a{alpha:.2f}-r{range_start:.2f}_{range_end:.2f}-m{mp_hash}"
    if width is not None:
        cache_name = f"{base_name}-w{width}.jpg"
        media_type = "image/jpeg"
    else:
        cache_name = f"{base_name}.png"
        media_type = "image/png"
    cache_dir = os.path.join(COMPOSITE_CACHE_PATH, str(dataset_id), str(sample_id))
    cache_path = os.path.join(cache_dir, cache_name)
    # Bounded browser caching only (no immutable): the URL doesn't encode
    # map_path, so an unbounded browser cache would keep serving a stale
    # composite after a heatmap re-point even though mp_hash busts the disk key.
    composite_headers = {"Cache-Control": "private, max-age=3600", "X-Overlay": overlay_field}
    await _require_readable_source(dataset_id, base_path)
    await _require_readable_source(dataset_id, map_path)
    cached = await asyncio.to_thread(read_cache, cache_path)
    if cached is not None:
        return Response(
            content=cached,
            media_type=media_type,
            headers={**composite_headers, "X-Cache": "HIT"},
        )

    # Cache miss — fetch base + overlay bytes and composite, in a render slot.
    async with _render_slot():
        base_bytes, _ = await _fetch_media_bytes(dataset_id, base_path)
        hm_bytes, _ = await _fetch_media_bytes(dataset_id, map_path)

        # PIL + numpy composite is synchronous; run it off the event loop.
        from .compositing import composite_heatmap

        def _composite():
            out = composite_heatmap(base_bytes, hm_bytes, range_start, range_end, colormap, alpha)
            if width is None:
                return out
            # Tile request: downscale the composited image and re-encode as JPEG (alpha is
            # already flattened by the composite, so JPEG is lossless-enough for a thumbnail).
            import io as _io

            from PIL import Image

            img = Image.open(_io.BytesIO(out)).convert("RGB")
            if img.width > width:
                new_height = max(1, round(img.height * width / img.width))
                img = img.resize((width, new_height), Image.Resampling.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        try:
            composed = await asyncio.to_thread(_composite)
        except Exception as e:
            log.error("Composite failed for sample %s overlay %s: %s", sample_id, overlay_field, e)
            raise HTTPException(status_code=500, detail=f"Composite failed: {e}")

    return Response(
        content=composed,
        background=BackgroundTask(_cache_media_later, cache_path, composed),
        media_type=media_type,
        headers={**composite_headers, "X-Cache": "MISS"},
    )


def _parse_crop_spec(spec: str) -> tuple[float, float, float, float]:
    """Parse a `crop=x,y,w,h` query value into floats."""
    parts = spec.split(",")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="crop must be 'x,y,w,h' or '1'")
    try:
        x, y, w, h = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=400, detail="crop coordinates must be numbers")
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise HTTPException(400, "crop coordinates must be finite")
    if w <= 0 or h <= 0:
        raise HTTPException(status_code=400, detail="crop width and height must be positive")
    return x, y, w, h


def _crop_pixels(body: bytes, bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(body)) as image:
        iw, ih = image.size
    x, y, w, h = bbox
    if max(bbox) <= 2.0:
        x, y, w, h = x * iw, y * ih, w * iw, h * ih
    left, top = int(max(0, min(iw, x))), int(max(0, min(ih, y)))
    right, bottom = int(max(0, min(iw, x + w))), int(max(0, min(ih, y + h)))
    if right <= left or bottom <= top:
        raise HTTPException(400, "crop region is empty after clamping to image bounds")
    return left, top, right, bottom


async def _serve_crop_resize(
    sample_id: int,
    dataset_id: int,
    path: str,
    field: str,
    crop: str | None,
    width: int | None,
    version_id: int | None = None,
) -> Response:
    """Serve a cropped and/or width-resized JPEG of a sample's image.

    Shares the storage + security path with the main route via
    `_fetch_media_bytes` (so the S3 authorization, SSRF and path-traversal guards
    all apply, and external http(s) URLs are rejected the same way overlay
    compositing rejects them — we can't crop a 302 redirect). The result is cached
    content-addressed under `CROP_CACHE_PATH` and returned with a long-lived,
    immutable `Cache-Control` so a grid of thumbnails hits the browser/CDN cache
    instead of re-deriving on every scroll.

    `crop='x,y,w,h'` crops an explicit box; `crop='1'` (or `'meta'`) reads
    `metadata.crop_bbox` — the per-object box stamped by the object-dataset
    producer. Box coordinates follow the same normalized-vs-pixel rule as the SDK
    `to_patches()` and the frontend renderer: all values <= 2.0 are treated as
    normalized [0, 1]. `w` resizes so the output width is `w` (height scales,
    never upscales past the source).
    """
    if path.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Cannot crop/resize an external-URL media source (use s3:// or a local path)",
        )
    width = _thumbnail_width(width)

    bbox: tuple[float, float, float, float] | None = None
    if crop is not None:
        if crop in ("1", "meta"):
            if version_id is None:
                mrow = await db.fetchrow("SELECT metadata FROM samples WHERE id = $1", sample_id)
            else:
                mrow = await db.fetchrow(
                    "SELECT metadata FROM version_snapshots WHERE sample_id = $1 AND version_id = $2",
                    sample_id,
                    version_id,
                )
            meta = (mrow["metadata"] if mrow else None) or {}
            if isinstance(meta, str):
                import json as _json

                meta = _json.loads(meta)
            cb = meta.get("crop_bbox")
            if not cb or len(cb) != 4:
                raise HTTPException(status_code=400, detail="Sample has no crop_bbox in metadata")
            bbox = _parse_crop_spec(",".join(str(v) for v in cb))
        else:
            bbox = _parse_crop_spec(crop)

    # Resolve/clamp pixels only on misses. Preserve the coordinate mode in the
    # key, including at the normalized/pixel boundary, without reading the source.
    cache_bbox = None if bbox is None else (max(bbox) <= 2.0, tuple(round(v, 4) + 0.0 for v in bbox))

    import hashlib as _hl

    # Fold a hash of the resolved media path into the key (mirroring mp_hash in
    # _serve_composite): re-pointing a sample's filepath/media field must bust
    # the thumbnail, not serve the old image's crop forever. Since the digest is
    # also the ETag and the on-disk name, server and browser caches bust together.
    p_hash = _hl.md5(path.encode(), usedforsecurity=False).hexdigest()[:8]
    cache_key = f"{field}|crop={cache_bbox}|w={width}|p={p_hash}"
    digest = _hl.md5(cache_key.encode(), usedforsecurity=False).hexdigest()
    cache_dir = os.path.join(CROP_CACHE_PATH, str(dataset_id), str(sample_id))
    cache_path = os.path.join(cache_dir, f"{digest}.jpg")
    headers = {
        # private, not public: these bytes are dataset-ACL'd — only the user's own
        # browser may treat them as immutable. nginx still caches (session-keyed,
        # short TTL) because its media locations ignore upstream Cache-Control.
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": f'"{digest}"',
    }

    await _require_readable_source(dataset_id, path)
    cached = await asyncio.to_thread(read_cache, cache_path)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={**headers, "X-Cache": "HIT"},
        )

    async with _render_slot():  # the source and its pixels are in memory until the JPEG is made
        body, _content_type = await _fetch_media_bytes(dataset_id, path)
        if bbox is not None:
            bbox = await asyncio.to_thread(_crop_pixels, body, bbox)

        def _render() -> bytes:
            import io as _io

            from PIL import Image

            from .image_modes import to_rgb

            img = to_rgb(Image.open(_io.BytesIO(body)))
            if bbox is not None:
                img = img.crop(bbox)
            if width is not None and img.width > width:
                new_height = max(1, round(img.height * width / img.width))
                img = img.resize((width, new_height), Image.Resampling.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        try:
            rendered = await asyncio.to_thread(_render)
        except Exception as e:
            log.error("Crop/resize failed for sample %s (field=%s): %s", sample_id, field, e)
            if type(e).__name__ == "UnidentifiedImageError":
                # PIL's message embeds "<_io.BytesIO object at 0x…>" — say what is wrong instead
                # (a .log registered as a sample's filepath).
                raise HTTPException(status_code=415, detail="This sample's file is not an image the server can read")
            raise HTTPException(status_code=400, detail=f"Crop/resize failed: {e}")

    return Response(
        content=rendered,
        background=BackgroundTask(_cache_media_later, cache_path, rendered),
        media_type="image/jpeg",
        headers={**headers, "X-Cache": "MISS"},
    )


@app.get("/api/samples/{sample_id}/media")
async def serve_sample_media(
    sample_id: int,
    request: Request,
    field: str = "filepath",
    overlay: str | None = None,
    colormap: str | None = None,
    alpha: float = 0.6,
    range_start: float | None = None,
    range_end: float | None = None,
    crop: str | None = None,
    w: int | None = None,
    version_id: int | None = None,
):
    """
    Serve image bytes for a sample's media field.

    The default `field=filepath` serves the primary image. Custom media fields
    can be looked up from `samples.metadata`, including nested values via a dotted
    path (e.g. `?field=<heatmap_name>.map_path` → `metadata->'<heatmap_name>'->>'map_path'`).
    All paths route through the per-dataset storage backend with access control.

    If `overlay` is set, the base image is composited with the named heatmap
    before being returned — used by the grid view to avoid running a canvas
    per cell. Composites are cached on disk under `COMPOSITE_CACHE_PATH`.
    `colormap` optionally overrides the colormap stored in the heatmap's metadata;
    `alpha` controls overlay opacity at nonzero pixels.

    Supported value formats in the resolved field:
      - s3://bucket/key       → fetched via dataset's storage backend, cached
      - /absolute/local/path  → served from disk
      - http(s)://...         → 302 redirect (external URL, frontend follows)
    """
    return await serve_media(
        sample_id,
        request,
        field=field,
        overlay=overlay,
        colormap=colormap,
        alpha=alpha,
        range_start=range_start,
        range_end=range_end,
        crop=crop,
        w=w,
        version_id=version_id,
    )


async def serve_media(
    sample_id: int,
    request: Request,
    *,
    field: str = "filepath",
    overlay: str | None = None,
    colormap: str | None = None,
    alpha: float = 0.6,
    range_start: float | None = None,
    range_end: float | None = None,
    crop: str | None = None,
    w: int | None = None,
    version_id: int | None = None,
    check_access: bool = True,
):
    """serve_sample_media's work. With check_access off, for a follower's request the followers router has
    authorized, the user's dataset access isn't checked and nothing is fetched from a primary."""
    # Who is asking comes before anything is looked up: answering 404 for a missing sample and 401 for an existing
    # one told an anonymous caller which sample ids exist, and which of a sample's fields hold a value.
    user = None
    if check_access:
        user = await get_optional_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
    sql = _build_media_path_sql(field, versioned=version_id is not None)
    media_args = [sample_id] if version_id is None else [sample_id, version_id]
    row = await db.fetchrow(sql, *media_args)
    if not row:
        raise HTTPException(status_code=404, detail="Sample not found")

    dataset_id = row['dataset_id']
    path = row['path']
    if check_access:
        # Before the field's own error, for the same reason: a user without access learns nothing of its fields, nor
        # that the sample exists (a sample in a dataset they can't open answers as a missing one does).
        with missing_unless_allowed("Sample not found"):
            await check_dataset_access(dataset_id, user)
    if not path:
        raise HTTPException(
            status_code=404,
            detail=f"Sample {sample_id} has no value for field '{field}'",
        )

    if check_access:
        # A mirror's image this instance can't read (on the primary's disk, or in a bucket it has no credentials
        # for) comes from the primary, when the primary serves that dataset's images (docs/FOLLOWERS.md).
        if version_id is None and not await _readable_here(dataset_id, path):
            source = await _primary_media_source(dataset_id, sample_id)
            if source is not None:
                params = {
                    "field": field,
                    "overlay": overlay,
                    "colormap": colormap,
                    "alpha": alpha if overlay is not None else None,
                    "range_start": range_start,
                    "range_end": range_end,
                    "crop": crop,
                    "w": w,
                }
                return await _media_from_primary(source, path, {k: v for k, v in params.items() if v is not None})

    if overlay is not None:
        # Overlay branch: composite and return (bypasses the FileResponse
        # fast-path because we're synthesizing a derived image).
        if not 0.0 <= alpha <= 1.0:
            raise HTTPException(status_code=400, detail="alpha must be in [0, 1]")
        return await _serve_composite(
            sample_id=sample_id,
            dataset_id=dataset_id,
            base_path=path,
            overlay_field=overlay,
            colormap_override=colormap,
            alpha=alpha,
            range_start_override=range_start,
            range_end_override=range_end,
            width=w,
            version_id=version_id,
        )

    # The grid always supplies a thumbnail width, including for external media.
    # Keep the existing redirect contract: let the browser scale the original,
    # without fetching arbitrary URLs on the backend. Crops still require a
    # local/S3 source and are rejected by _serve_crop_resize below.
    if path.startswith(('http://', 'https://')) and crop is None:
        return RedirectResponse(url=path)

    # A sample's own image in 16 bits or float (a multispectral band, a thermal frame): a browser shows a 16-bit
    # PNG by its top 8 bits, too dark for a 12-bit sensor, and most can't show a TIFF. Its full view is converted
    # as its thumbnails are (image_modes.to_rgb). Other media fields (masks, heatmaps) keep their raw values.
    if crop is None and w is None and field == "filepath" and await _is_high_bit_depth(dataset_id, path):
        return await _serve_crop_resize(
            sample_id=sample_id,
            dataset_id=dataset_id,
            path=path,
            field=field,
            crop=None,
            width=None,
            version_id=version_id,
        )

    if crop is not None or w is not None:
        # Crop/resize branch: derive a JPEG (per-object crop and/or a thumbnail)
        # instead of serving the source bytes. Routed through _fetch_media_bytes,
        # so it inherits the same storage backend + access guards as the S3 path.
        return await _serve_crop_resize(
            sample_id=sample_id,
            dataset_id=dataset_id,
            path=path,
            field=field,
            crop=crop,
            width=w,
            version_id=version_id,
        )

    # Local path → serve from disk, but only if the resolved realpath stays
    # inside FRIDGE_DATA_ROOT. Blocks `../`-style traversal out of the data dir
    # even if the DB has a malicious filepath (e.g. `/etc/passwd`).
    if not path.startswith('s3://'):
        resolved = _resolve_local_path(path)
        if not os.path.exists(resolved):
            raise HTTPException(status_code=404, detail="File not found on disk")
        return _full_media_file_response(
            request,
            resolved,
            **_media_response_kwargs(
                mimetypes.guess_type(resolved)[0],
                _media_head(resolved),
                resolved,
                **{"Cache-Control": _FULL_MEDIA_CACHE_CONTROL},
            ),
        )

    # S3 path: scope the local cache by dataset_id AND bucket so the same key in
    # two different buckets (multi-bucket dataset) can't collide and serve each
    # other's bytes. The dropped legacy unscoped-by-bucket fallback had exactly
    # that collision risk; cache is derived state, so re-fetching is harmless.
    bucket, key = _parse_s3_path(path)
    if not bucket or not key:
        raise HTTPException(status_code=500, detail=f"Malformed s3 path: {path}")
    # Reject keys/buckets that would escape S3_LOCAL_PATH/{dataset_id}/{bucket}/ via `..`.
    _validate_s3_key(key)
    _validate_bucket(bucket)

    # SSRF / confused-deputy guard: only fetch buckets this dataset is authorized
    # for. Without this, a user who can set a sample's media path could point it
    # at s3://arbitrary-bucket/key and the server would fetch it with our IAM creds.
    route = await route_s3_bucket(dataset_id, bucket)
    if route is None:
        raise HTTPException(status_code=403, detail="Bucket not authorized for this dataset")

    cached_path = os.path.join(S3_LOCAL_PATH, str(dataset_id), bucket, key)
    cached = await asyncio.to_thread(_cached_media_response, request, cached_path, key)
    if cached is not None:
        return cached

    # Bucket-aware routing: multi-bucket datasets (e.g. primary images in one
    # bucket, annotation overlays in another with different IAM) work because
    # route_s3_bucket picks credentials by the bucket in the s3:// path, among
    # the storage configs this dataset may use.
    backend = await backend_for_route(route)
    if backend is None or not backend.s3_client:
        raise HTTPException(status_code=500, detail="S3 backend not configured for this dataset")

    # IMPORTANT: boto3 is synchronous. Calling it directly from an async def
    # handler blocks the event loop, which serialises every concurrent image
    # request to one-at-a-time. With a grid firing 100+ thumbnails in parallel
    # that produces multi-second latencies on the tail requests. Run the
    # blocking work in the default thread pool so requests actually parallelise.
    # Only the first chunk is read here (its first bytes decide the type); the rest streams (_s3_media_chunks).
    def _fetch_blocking():
        resp = backend.s3_client.get_object(Bucket=bucket, Key=key)
        stream = _S3MediaBody(resp['Body'])
        try:
            first = stream.read()
        except BaseException:
            stream.finish(keep=False)
            raise
        ct = resp.get('ContentType') or mimetypes.guess_type(key)[0] or 'application/octet-stream'
        return stream, first, resp.get('ContentLength'), ct

    try:
        stream, first, size, content_type = await asyncio.to_thread(_fetch_blocking)
    except Exception as e:
        # Map S3 / boto3 errors to meaningful HTTP statuses instead of
        # collapsing everything to 404. Admins tailing logs need to
        # distinguish "key actually missing" (data bug) from "credentials
        # are wrong" (config bug) from "network is down" (infra problem).
        try:
            from botocore.exceptions import ClientError
        except ImportError:
            ClientError = ()  # type: ignore  # fallthrough if boto3 isn't installed
        if isinstance(e, ClientError):
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                raise HTTPException(status_code=404, detail=f"S3 key not found: {bucket}/{key}")
            if code in ("AccessDenied", "403"):
                # Surface as 500 — it's a server-side credential/bucket-policy
                # bug, not a client auth problem. The caller already passed
                # our own access-control check.
                log.error("S3 AccessDenied fetching %s/%s with config for dataset %s", bucket, key, dataset_id)
                raise HTTPException(status_code=500, detail="Storage backend denied access (check credentials)")
            log.error("S3 ClientError (%s) fetching %s/%s: %s", code, bucket, key, e)
            raise HTTPException(status_code=500, detail=f"S3 error: {code}")
        # Network, DNS, timeout, etc.
        log.error("S3 fetch failed for %s/%s: %s", bucket, key, e)
        raise HTTPException(status_code=502, detail="Storage backend unreachable")

    kwargs = _media_response_kwargs(
        content_type,
        first[:_MEDIA_HEAD_BYTES],
        key,
        **{"X-Cache": "MISS", "Cache-Control": _FULL_MEDIA_CACHE_CONTROL},
    )
    if size is not None:
        kwargs["headers"]["Content-Length"] = str(size)
    return StreamingResponse(_s3_media_chunks(stream, first, cached_path, size), **kwargs)

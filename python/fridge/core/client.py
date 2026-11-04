import json
import math
import os
import random
import struct
import time
import warnings
from typing import Any, Dict, Optional

import numpy as np
import requests
from urllib3.util import Timeout

from .job import Job

# Default (connect, read) timeout in seconds applied to every request.
# Generous read timeout because some endpoints (large queries, histograms)
# are slow on multi-million-row datasets; override per-Client if needed.
DEFAULT_TIMEOUT = (10, 300)


def _decode_vectors_binary(buf: bytes) -> Dict[str, Any]:
    """Decode the columnar binary page produced by GET /embeddings/vectors?format=binary.

    Layout (mirrors the scatter's binary payload): 4-byte LE header length, a JSON header padded
    to a 4-byte boundary, then int32[count] sample_id and float32[count*dim] vectors.

    Each item's "vector" is a float32 row VIEW into `buf`, not a list — every caller either
    assigns it into a preallocated array, passes it to np.asarray, or takes len(), so this is
    both compatible and allocation-free.
    """
    (header_len,) = struct.unpack_from("<I", buf, 0)
    header = json.loads(buf[4 : 4 + header_len].decode("utf-8").rstrip())
    count, dim = int(header["count"]), int(header["dim"])

    offset = 4 + header_len
    ids = np.frombuffer(buf, dtype="<i4", count=count, offset=offset)
    offset += count * 4
    if count:
        mat = np.frombuffer(buf, dtype="<f4", count=count * dim, offset=offset).reshape(count, dim)
    else:
        mat = np.empty((0, 0), dtype="<f4")

    items = [{"sample_id": int(sid), "vector": vec} for sid, vec in zip(ids, mat)]
    if "vector_versions" in header:
        for item, version in zip(items, header["vector_versions"]):
            item["vector_version"] = version
    return {
        "items": items,
        "total": header.get("total"),
    }


def _raise_for_status(resp):
    """``raise_for_status()``, but with the server's error detail in the message.

    FastAPI puts the actionable message in the JSON ``detail`` field, which
    requests' default HTTPError ("500 Server Error: Internal Server Error for
    url: ...") throws away — a real failure reads as noise. Raises the same
    ``requests.HTTPError`` type callers already handle, carrying status,
    method + URL, and the detail (or the first 500 chars of the body).
    """
    if resp.ok:
        return
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        detail = resp.text
    detail = str(detail)[:500] or resp.reason
    method = resp.request.method if resp.request is not None else ""
    msg = f"{resp.status_code} {resp.reason} for {method} {resp.url}: {detail}"
    if resp.status_code == 401:
        msg += " — call client.login(username, password) or set FRIDGE_API_TOKEN"
    raise requests.HTTPError(msg, response=resp)


class _Session(requests.Session):
    """A requests.Session that applies a default timeout to every request.

    Plain ``requests`` defaults to no timeout, so a stalled server or a dropped
    connection hangs the SDK forever. This injects a default timeout while still
    letting individual calls pass an explicit one (``setdefault`` never
    overrides an explicit value, so long-running calls like reduce_embeddings
    keep their own larger timeouts).
    """

    # How long to retry refusals with Retry-After: busy 409 (no write committed), or nginx 429
    # (rate/connection limited before proxying). Streamed uploads are never resent.
    busy_retry_seconds = 120

    def __init__(self, timeout=DEFAULT_TIMEOUT):
        super().__init__()
        self._default_timeout = timeout

    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        # requests consults netrc again on redirects, even with session.auth.
        # Reapply our token only where requests considers forwarding auth safe.
        if isinstance(self.auth, _BearerAuth) and not self.should_strip_auth(
            response.request.url,
            prepared_request.url,
        ):
            self.auth(prepared_request)

    def request(self, *args, **kwargs):
        retry_busy = kwargs.pop("retry_busy", True)
        kwargs.setdefault("timeout", self._default_timeout)
        resp = super().request(*args, **kwargs)
        # A streamed upload can't be sent twice; everything else is resent unchanged.
        resendable = "files" not in kwargs and not hasattr(kwargs.get("data"), "read")
        deadline = time.monotonic() + self.busy_retry_seconds
        delay = 1.0
        swap_seen = False
        while retry_busy and resp.status_code in (409, 429) and "Retry-After" in resp.headers and resendable:
            # A map re-fit swapping its new map in holds the dataset for at most this many more seconds
            # (the server ends the swap by then): keep retrying until it is over, not just the usual window.
            # Taken once, so a server that keeps answering never extends the wait.
            swap = resp.headers.get("X-Map-Swap-Seconds")
            if swap is not None and not swap_seen:
                swap_seen = True
                deadline = max(deadline, time.monotonic() + float(swap) + self.busy_retry_seconds)
            delay = max(delay, float(resp.headers["Retry-After"]))
            # Jitter: two clients refused together must not keep retrying in lockstep.
            pause = delay * random.uniform(1.0, 1.5)
            if time.monotonic() + pause > deadline:
                break
            time.sleep(pause)
            delay = min(delay * 2, 8.0)
            resp = super().request(*args, **kwargs)
        return resp


class _BearerAuth(requests.auth.AuthBase):
    """Explicit auth suppresses requests' netrc fallback while preserving proxies."""

    def __init__(self, token):
        self.token = token

    def __call__(self, request):
        request.headers['Authorization'] = f'Bearer {self.token}'
        return request


class Client:
    def __init__(self, base_url=None, api_token=None, timeout=DEFAULT_TIMEOUT):
        """Create a Freedom Ridge API client.

        Args:
            base_url: API base URL. Defaults to ``$FRIDGE_API_URL`` or
                ``http://localhost:8000/api``.
            api_token: Optional API token (Bearer). If omitted, falls back to
                ``$FRIDGE_API_TOKEN``. When set, every request is authenticated
                with ``Authorization: Bearer <token>`` and no ``login()`` is
                needed — this is the recommended auth for non-interactive
                scripts. See ``create_token()``.
            timeout: Default ``(connect, read)`` timeout in seconds.
        """
        self.base_url = base_url or os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")
        self.session = _Session(timeout=timeout)  # session maintains cookies + headers
        token = api_token or os.getenv("FRIDGE_API_TOKEN")
        if token:
            self.set_token(token)

    def set_token(self, token):
        """Authenticate all subsequent requests with an API token.

        Sets the ``Authorization: Bearer <token>`` header on the session.
        Unlike ``login()``, this needs no username/password and is the
        recommended mechanism for long-running / automated jobs. Create a
        token with ``create_token()`` or in the web UI (Settings → API Tokens).
        """
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.auth = _BearerAuth(token)

    def get_job(self, job_id) -> Job:
        """Reconnect to an existing job without submitting or restarting any work."""
        return Job(self, job_id)

    def get_job_status(self, job_id, *, timeout=None) -> dict:
        """Fetch status with an optional total HTTP timeout used by Job.wait()."""
        options: dict[str, Any] = {}
        if isinstance(self.session, _Session):
            options['retry_busy'] = False
        if timeout is not None:
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('timeout must be finite and positive')
            options['timeout'] = Timeout(total=timeout, connect=min(10, timeout), read=timeout)
        response = self.session.get(f'{self.base_url}/jobs/{Job(self, job_id).id}', **options)
        _raise_for_status(response)
        return response.json()

    def cancel_job(self, job_id) -> dict:
        """Request cancellation and return the server's current job status."""
        response = self.session.post(f'{self.base_url}/jobs/{Job(self, job_id).id}/cancel')
        _raise_for_status(response)
        return response.json()

    def list_jobs(self, dataset_id, *, limit=50, cursor=None, include_system_statistics=False) -> dict:
        """Page history; successful automatic incremental statistics are opt-in and retained for 24 hours."""
        params = {'limit': limit}
        if cursor is not None:
            params['cursor'] = cursor
        if include_system_statistics:
            params['include_system_statistics'] = 'true'
        response = self.session.get(f'{self.base_url}/datasets/{dataset_id}/jobs', params=params)
        _raise_for_status(response)
        return response.json()

    def login(self, username, password):
        """
        Authenticate with the API using session cookies.

        For non-interactive scripts prefer an API token (``set_token()`` /
        ``create_token()`` / ``$FRIDGE_API_TOKEN``), which doesn't expire on
        idle and never embeds a password.

        Args:
            username: Username for authentication
            password: Password for authentication

        Returns:
            dict: Response message
        """
        resp = self.session.post(f"{self.base_url}/auth/login", json={"username": username, "password": password})
        _raise_for_status(resp)
        # A Secure cookie over plain http is silently dropped by requests, so
        # login "succeeds" and then every later call 401s with no explanation
        # (SESSION_COOKIE_SECURE defaults to true server-side). Fail HERE with
        # the fix instead of letting the session limp into a wall of 401s.
        if self.base_url.lower().startswith("http://") and "Authorization" not in self.session.headers:
            # Bearer-token sessions authenticate every call via the header, so a dropped
            # session cookie is harmless there — only cookie-auth clients must fail here.
            dropped = [c.name for c in resp.cookies if c.secure]
            if dropped:
                raise RuntimeError(
                    f"login succeeded but the server set Secure cookie(s) {dropped} over plain "
                    f"http ({self.base_url}) — they will not be sent back, so every subsequent "
                    "call would fail with 401. Set SESSION_COOKIE_SECURE=false on the server "
                    "for http dev, or connect via https.",
                )
        return resp.json()

    def whoami(self):
        """Return the authenticated user (``GET /auth/me``) — a quick auth-state check."""
        resp = self.session.get(f"{self.base_url}/auth/me")
        _raise_for_status(resp)
        return resp.json()

    # --- API tokens ---

    def create_token(self, name, expires_days=90):
        """Create an API token (requires power_user or admin).

        Args:
            name: Human-readable label for the token.
            expires_days: Days until expiry (default 90). Pass ``None`` for the
                server default.

        Returns:
            dict with ``id``, ``name``, ``expires_at`` and ``token`` — the
            plaintext ``token`` is returned ONLY once here, so store it
            securely. Use it via ``Client(api_token=...)`` / ``set_token()`` /
            ``$FRIDGE_API_TOKEN``.
        """
        payload = {"name": name}
        if expires_days is not None:
            payload["expires_days"] = expires_days
        resp = self.session.post(f"{self.base_url}/tokens", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def list_tokens(self):
        """List your API tokens. Token values are never returned (only on creation)."""
        resp = self.session.get(f"{self.base_url}/tokens")
        _raise_for_status(resp)
        return resp.json()

    def revoke_token(self, token_id):
        """Revoke (deactivate) one of your API tokens."""
        resp = self.session.delete(f"{self.base_url}/tokens/{token_id}")
        _raise_for_status(resp)
        return resp.json()

    def get_datasets(self):
        # FastAPI requires trailing slash for this endpoint
        resp = self.session.get(f"{self.base_url}/datasets/")
        _raise_for_status(resp)
        return resp.json()

    def create_dataset(
        self,
        name,
        description=None,
        media_fields=None,
        storage_config_id=None,
        is_global=False,
    ):
        """Create a dataset.

        Args:
            name: Dataset name (must be unique).
            description: Optional description.
            media_fields: Optional list of media field names. Defaults to ["filepath"].
            storage_config_id: Optional storage config ID. NULL/None falls back to
                the is_default storage config, then to env-var credentials.
            is_global: If False (default), only the owner, admins, and explicitly-granted
                users see the dataset. If True, every signed-in user sees it.
        """
        payload = {
            "name": name,
            "description": description,
            "is_global": is_global,
        }
        if media_fields is not None:
            payload["media_fields"] = media_fields
        if storage_config_id is not None:
            payload["storage_config_id"] = storage_config_id
        resp = self.session.post(f"{self.base_url}/datasets", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def delete_dataset(self, dataset_id, *, wait=True, timeout=1800):
        """Delete through a durable job; a local wait timeout does not cancel deletion."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        resp = self.session.delete(f"{self.base_url}/datasets/{dataset_id}", timeout=(10, 30))
        _raise_for_status(resp)
        job = Job(self, resp.json()['job_id'])
        return job.wait(timeout=timeout) if wait else job

    def resume_dataset_deletion(self, dataset_id, *, wait=True, timeout=1800):
        """Continue the same cancelled/failed deletion and its checkpoint."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/deletion/resume", timeout=(10, 30))
        _raise_for_status(resp)
        job = Job(self, resp.json()['job_id'])
        return job.wait(timeout=timeout) if wait else job

    # --- Per-dataset access control ---

    def list_dataset_access(self, dataset_id):
        """List users who have explicit access to `dataset_id`. Admin only."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/access")
        _raise_for_status(resp)
        return resp.json()

    def grant_dataset_access(self, dataset_id, user_id):
        """Grant `user_id` access to `dataset_id`. Idempotent. Admin only."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/access",
            json={"user_id": user_id},
        )
        _raise_for_status(resp)
        return resp.json()

    def revoke_dataset_access(self, dataset_id, user_id):
        """Revoke `user_id`'s access to `dataset_id`. No-op if not granted. Admin only."""
        resp = self.session.delete(f"{self.base_url}/datasets/{dataset_id}/access/{user_id}")
        _raise_for_status(resp)
        return None

    def get_dataset(self, name):
        # Helper to find by name
        datasets = self.get_datasets()
        for d in datasets:
            if d['name'] == name and not d.get('deleting'):
                return d
        return None

    def get_samples(self, dataset_id, limit=None, offset=None):
        """
        Get samples from a dataset.

        Args:
            dataset_id: Dataset ID
            limit: Maximum number of samples to return
            offset: Offset for pagination

        Returns:
            dict: API response with items and total
        """
        params = {}
        # The backend GET /samples requires limit >= 1 (Query(50, ge=1)), so a
        # falsy/zero limit is dropped to let the server apply its default.
        # offset is forwarded even when 0 (offset is ge=0).
        if limit:
            params['limit'] = limit
        if offset is not None:
            params['offset'] = offset

        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/samples", params=params)
        _raise_for_status(resp)
        return resp.json()

    def query_samples(
        self,
        dataset_id,
        limit=1000,
        offset=0,
        filters=None,
        fields=None,
        slim=False,
        skip_count=False,
        sort_by=None,
        sort_order=None,
        after_id=None,
    ):
        """Query samples with optional slim mode for fast bulk iteration."""
        payload = {"limit": limit, "offset": offset}
        if filters:
            payload["filters"] = filters
        if fields:
            payload["fields"] = fields
        if slim:
            payload["slim"] = True
        if skip_count:
            payload["skip_count"] = True
        if sort_by:
            payload["sort_by"] = sort_by
        if sort_order:
            payload["sort_order"] = sort_order
        if after_id is not None:
            payload["after_id"] = after_id
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/samples/query",
            json=payload,
        )
        _raise_for_status(resp)
        return resp.json()

    def add_samples(self, dataset_id, samples, on_duplicate='error'):
        """Add samples to a dataset.

        Args:
            dataset_id: Dataset ID
            samples: List of sample dicts
            on_duplicate: 'error' (add new samples and return duplicate conflicts with ids + existing),
                         'skip' (silently skip duplicates),
                         'update' (overwrite existing samples)
        """
        payload = {"samples": samples, "on_duplicate": on_duplicate}
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/samples", json=payload)
        if resp.status_code == 409 and on_duplicate == 'error':
            try:
                conflict = resp.json()
            except ValueError:
                conflict = None
            if (
                isinstance(conflict, dict)
                and isinstance(conflict.get('ids'), list)
                and isinstance(conflict.get('existing'), dict)
            ):
                return conflict
        _raise_for_status(resp)
        return resp.json()

    def _upload_embeddings_batched(self, dataset_id, embeddings, batch_size, url, what):
        total = len(embeddings)
        results = []
        written = 0
        dropped = 0
        dropped_sample_ids: set[int] = set()

        for i in range(0, total, batch_size):
            batch = embeddings[i : i + batch_size]
            resp = self.session.post(url, json=batch)
            if resp.status_code == 422:
                try:
                    rejection = resp.json()
                except ValueError:
                    rejection = None
                detail = rejection.get("detail") if isinstance(rejection, dict) else None
                unknown = detail.get("unknown_sample_ids") if isinstance(detail, dict) else None
                if (
                    isinstance(unknown, list)
                    and unknown
                    and all(type(sample_id) is int for sample_id in unknown)
                    and set(unknown) <= {item["sample_id"] for item in batch}
                ):
                    rejected_ids = set(unknown)
                    retained = [item for item in batch if item["sample_id"] not in rejected_ids]
                    removed = len(batch) - len(retained)
                    dropped += removed
                    dropped_sample_ids.update(rejected_ids)
                    warnings.warn(
                        f"upload_{what}: dropped {removed} embeddings for sample IDs {sorted(rejected_ids)} "
                        f"absent from dataset {dataset_id}",
                        stacklevel=2,
                    )
                    batch = retained
                    if not batch:
                        results.append({"written": 0})
                        continue
                    # The 422 committed nothing. Retry only this remainder, at most once.
                    resp = self.session.post(url, json=batch)
            _raise_for_status(resp)
            body = resp.json()
            results.append(body)
            persisted = int(body.get("written", 0))
            # Older servers acknowledge partial uploads with 200; surface those failures too.
            if persisted != len(batch):
                raise requests.HTTPError(
                    f"upload_{what}: server persisted {persisted}/{len(batch)} {what} in this batch",
                    response=resp,
                )
            written += persisted
        return {
            "batches": len(results),
            "total": total,
            "written": written,
            "dropped": dropped,
            "dropped_sample_ids": sorted(dropped_sample_ids),
        }

    def upload_embeddings(self, dataset_id, embeddings, batch_size=1000):
        """
        Upload embeddings in batches to avoid timeout on large datasets.

        Args:
            dataset_id: Dataset ID
            embeddings: List of {sample_id, vector, field_name}
            batch_size: Number of embeddings per batch (default 1000)

        Returns:
            dict with 'batches', 'total' (submitted), 'written' (persisted),
            'dropped' (embedding rows), and 'dropped_sample_ids' (unique IDs).
            Warns and drops explicitly rejected sample IDs, retrying the rest once.
            Other failures raise. Each HTTP batch commits independently.
        """
        return self._upload_embeddings_batched(
            dataset_id,
            embeddings,
            batch_size,
            f"{self.base_url}/datasets/{dataset_id}/embeddings",
            "embeddings",
        )

    def upload_patch_embeddings(self, dataset_id, embeddings, batch_size=1000):
        """Upload patch embeddings. The field_name encoding is handled by the server.

        Args:
            dataset_id: Dataset ID
            embeddings: List of {sample_id, vector, field_name, patch_scale, patch_row, patch_col}
            batch_size: Number of embeddings per batch

        Returns:
            dict with 'batches', 'total' (submitted), 'written' (persisted),
            'dropped' (patch rows), and 'dropped_sample_ids' (unique IDs).
            Warns and drops explicitly rejected sample IDs, retrying the rest once.
            Other failures raise. Each HTTP batch commits independently.
        """
        return self._upload_embeddings_batched(
            dataset_id,
            embeddings,
            batch_size,
            f"{self.base_url}/datasets/{dataset_id}/embeddings/patches",
            "patch_embeddings",
        )

    def reduce_embeddings(self, dataset_id, field_name="clip", max_embeddings=100000):
        """
        Compute UMAP-reduced 2D embedding coordinates for visualization.
        This must be run before embeddings can be visualized in the UI.

        WARNING: This is a long-running operation. For large datasets (>100k),
        run this from a script rather than calling interactively.

        Args:
            dataset_id: Dataset ID
            field_name: Embedding field name (default: "clip")
            max_embeddings: Max embeddings to process. Set to -1 for all (default: 100000)

        Returns:
            dict: Result with total_embeddings and reduced count
        """
        print(f"Computing UMAP reduction for {field_name} embeddings...")
        if max_embeddings == -1:
            print("WARNING: Processing ALL embeddings. This may take 30+ minutes for 1M embeddings.")

        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/embeddings/reduce",
            json={"field_name": field_name, "max_embeddings": max_embeddings},
            timeout=7200,  # 2 hour timeout for large reductions
        )

        if not resp.ok:
            try:
                error_detail = resp.json().get("detail", resp.text)
            except Exception:
                error_detail = resp.text
            raise Exception(f"Reduction failed: {error_detail}")

        result = resp.json()

        print(f"✅ Reduction complete! {result.get('reduced', '?')} embeddings reduced.")
        return result

    def get_vectors(self, dataset_id, field_name="clip", limit=10000, offset=0, after_id=None, pending_only=False):
        """Fetch raw embedding vectors with sample IDs, paginated.

        Pass ``after_id`` (the last sample_id of the previous page) for keyset
        pagination — O(page) at any depth, unlike ``offset`` which re-scans. The
        server returns ``total`` only on the first page (after_id 0/None); carry
        it forward. pending_only=True returns only embeddings still missing UMAP
        coordinates (the projection queue) — used by the incremental projection worker.
        """
        params = {"field_name": field_name, "limit": limit}
        if after_id is not None:
            params["after_id"] = after_id
        else:
            params["offset"] = offset
        if pending_only:
            params["pending_only"] = "true"
        # Ask for the columnar binary page. The JSON form of a 10k x 1024-d page is a ~208MB
        # string that costs the SERVER seconds to encode and this client seconds to parse, and
        # every value round-trips through a Python float on both sides. The binary bytes are
        # already the float32 buffer we want. A server that predates format= ignores the unknown
        # query param and answers JSON, which the content-type check below handles.
        params["format"] = "binary"
        resp = self.session.get(
            f"{self.base_url}/datasets/{dataset_id}/embeddings/vectors",
            params=params,
        )
        _raise_for_status(resp)
        if resp.headers.get("content-type", "").startswith("application/octet-stream"):
            return _decode_vectors_binary(resp.content)
        return resp.json()

    def upload_coordinates(self, dataset_id, field_name, coordinates, batch_size=5000, model_id=None):
        """Upload 2D layout coordinates in batches, independently of vector storage.

        Projected points for a map with scheduled re-fits pass the current map's ``model_id``
        (``get_map_state``); the server refuses them (409) while a re-fit runs or after the map changed.
        Manual full layouts without that identity are refused on managed maps. Run a re-fit to
        change their layout. Managed/stale-model refusals are permanent and are not retried.
        """
        total = len(coordinates)
        params = {"field_name": field_name}
        if model_id is not None:
            params["model_id"] = str(model_id)
        for i in range(0, total, batch_size):
            batch = coordinates[i : i + batch_size]
            resp = self.session.post(
                f"{self.base_url}/datasets/{dataset_id}/embeddings/coordinates",
                params=params,
                json=batch,
            )
            _raise_for_status(resp)
        return {"total": total}

    def get_map_state(self, dataset_id, field_name) -> dict:
        """A field's map: its current model (``model_id``, ``artifact_location``/``artifact_path``,
        ``artifact_checksum``) when it has scheduled re-fits, ``refitted_at``, and ``active`` while a
        re-fit runs."""
        resp = self.session.get(
            f"{self.base_url}/datasets/{dataset_id}/embeddings/map-state",
            params={"field_name": field_name},
        )
        _raise_for_status(resp)
        return resp.json()

    def list_schedules(self, dataset_id) -> list:
        """The dataset's schedules (Jobs panel → Schedules), each with its last and next run."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/schedules")
        _raise_for_status(resp)
        return resp.json()["items"]

    def create_schedule(self, dataset_id, target, *, job_type="map_refit", **rule) -> dict:
        """Add a schedule. ``rule``: enabled, frequency ('daily'/'weekly'), weekday (0 = Sunday),
        time ('HH:MM'), timezone, min_new_samples, min_days_between (None drops a condition),
        window_minutes (a run starts only this long after its time). Defaults: weekly on Sunday
        (weekday 0) at 03:00 UTC, only when >= 20,000 new samples, no minimum gap
        (min_days_between None; with one, the next check is the first time the gap allows), within
        180 minutes, off."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/schedules",
            json={"job_type": job_type, "target": target, **rule},
        )
        _raise_for_status(resp)
        return resp.json()

    def update_schedule(self, dataset_id, schedule_id, **changes) -> dict:
        """Change a schedule's rule or switch it on/off; unspecified settings keep their values."""
        current = next((item for item in self.list_schedules(dataset_id) if item["id"] == schedule_id), None)
        if current is None:
            raise ValueError(f"Dataset {dataset_id} has no schedule {schedule_id}")
        keys = (
            "enabled",
            "frequency",
            "weekday",
            "time",
            "timezone",
            "min_new_samples",
            "min_days_between",
            "window_minutes",
        )
        body = {key: current[key] for key in keys}
        body.update(changes)
        resp = self.session.put(f"{self.base_url}/datasets/{dataset_id}/schedules/{schedule_id}", json=body)
        _raise_for_status(resp)
        return resp.json()

    def delete_schedule(self, dataset_id, schedule_id) -> None:
        resp = self.session.delete(f"{self.base_url}/datasets/{dataset_id}/schedules/{schedule_id}")
        _raise_for_status(resp)

    def run_schedule(self, dataset_id, schedule_id) -> Job:
        """Queue the schedule's job now, ignoring its conditions; returns the (new or already running) Job."""
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/schedules/{schedule_id}/run")
        _raise_for_status(resp)
        return Job(self, resp.json()["job_id"])

    def recompute_viz_rank(self, dataset_id, field_name, full=True) -> dict:
        """Recompute the even-coverage scatter sample-priority (viz_cell_rank) for a
        field, so the visualization query is a fast index range scan. Call after
        projecting coords: full=True after a full reduce (re-rank everything),
        full=False remains a compatibility call; coordinate writes already rank
        new points in ranked fields."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/embeddings/recompute-viz-rank",
            params={"field_name": field_name, "full": "true" if full else "false"},
            timeout=(10, 1800),
        )
        _raise_for_status(resp)
        return resp.json()

    def create_concept(
        self,
        dataset_id,
        name,
        seed_ids=None,
        embedding_field="clip",
        kind="seed",
        neg_ids=None,
        prompt=None,
        weight=None,
        bias=0.0,
        threshold=None,
        spec=None,
        *,
        wait=True,
        timeout=1800,
    ):
        """Create an embedding "concept" (a linear direction w → a calibrated [0,1] per-sample
        score). kind='seed' averages seed_ids into w; kind='probe' fits a logistic boundary on
        seed_ids (positives) vs neg_ids or a random background; kind='text' CLIP-encodes
        `prompt` (server-side; on a 501 the SDK encodes locally instead). Advanced: pass a raw
        `weight` vector. Returns the concept + a preview count at the threshold.

        Scoring runs as a server job; this waits for it (``timeout`` seconds, which never cancels
        the job). ``wait=False`` returns the :class:`Job` instead."""
        payload = {
            "name": name,
            "embedding_field": embedding_field,
            "kind": kind,
            "bias": bias,
        }
        if threshold is not None:
            # Omitted -> the server derives a sane default from the score range; a hardcoded
            # 0.5 matched essentially the whole dataset for text concepts (cosine ~0.55-0.65).
            payload["threshold"] = threshold
        if seed_ids is not None:
            payload["seed_ids"] = list(seed_ids)
        if neg_ids is not None:
            payload["neg_ids"] = list(neg_ids)
        if prompt is not None:
            payload["prompt"] = prompt
        if weight is not None:
            payload["weight"] = list(weight)
        if spec is not None:
            payload["spec"] = spec
        params = self._wait_params(wait, timeout)
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/concepts",
            json=payload,
            params=params,
            timeout=(10, 1800),
        )
        if resp.status_code == 501 and prompt is not None:
            # Server can't encode text — encode locally and resubmit as a raw weight
            # (kind stays 'text'; the prompt is kept in spec for provenance).
            import warnings as _w

            from .text import encode_text

            _w.warn(
                "Server has no text encoder; encoding the concept prompt locally with "
                "clip-ViT-B-32 — ensure the field was embedded with a compatible CLIP checkpoint.",
                stacklevel=2,
            )

            payload.pop("prompt", None)
            payload["weight"] = encode_text(prompt)
            payload.setdefault("spec", {})["prompt"] = prompt
            resp = self.session.post(
                f"{self.base_url}/datasets/{dataset_id}/concepts",
                json=payload,
                params=params,
                timeout=(10, 1800),
            )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def list_concepts(self, dataset_id) -> list:
        """List the concepts saved on a dataset."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/concepts")
        _raise_for_status(resp)
        return resp.json()

    def concept_preview(self, dataset_id, concept_id, op=">", threshold=0.5) -> dict:
        """Count of samples passing `score <op> threshold` (index-fast; for slider tuning)."""
        resp = self.session.get(
            f"{self.base_url}/datasets/{dataset_id}/concepts/{concept_id}/preview",
            params={"op": op, "threshold": threshold},
        )
        _raise_for_status(resp)
        return resp.json()

    def rescore_concept(self, dataset_id, concept_id, full=True, *, wait=True, timeout=1800):
        """Rescore a concept (full rebuild, or incremental for new/replaced/removed vectors).
        Returns {concept_id, scored} after waiting on the server job; ``wait=False`` returns the Job."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/concepts/{concept_id}/rescore",
            params={"full": "true" if full else "false", **self._wait_params(wait, timeout)},
            timeout=(10, 1800),
        )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def refine_concept(
        self,
        dataset_id,
        concept_id,
        keep_ids=None,
        exclude_ids=None,
        threshold=None,
        self_train=False,
        *,
        wait=True,
        timeout=1800,
    ):
        """Active-learning refine from boundary corrections: keep_ids → negatives ("don't
        exclude"), exclude_ids → positives ("exclude / more like this"). The concept becomes a
        weighted probe and is rescored. Pass the current `threshold` to preserve the cut level;
        `self_train=True` also weakly-labels the current hidden/kept split. Waits on the server
        job; ``wait=False`` returns the Job."""
        payload: Dict[str, Any] = {"self_train": bool(self_train)}
        if keep_ids is not None:
            payload["keep_ids"] = list(keep_ids)
        if exclude_ids is not None:
            payload["exclude_ids"] = list(exclude_ids)
        if threshold is not None:
            payload["threshold"] = threshold
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/concepts/{concept_id}/refine",
            json=payload,
            params=self._wait_params(wait, timeout),
            timeout=(10, 1800),
        )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def rebuild_concept(
        self,
        dataset_id,
        concept_id,
        seed_ids=None,
        weight=None,
        threshold=None,
        *,
        wait=True,
        timeout=1800,
    ):
        """Overwrite-in-place: regenerate w from new seeds/weight (then full rescore in a server
        job), or — if only threshold is given — retune the saved default cut with no rescore
        (always synchronous). ``wait=False`` returns the Job of a rescore."""
        payload = {}
        if seed_ids is not None:
            payload["seed_ids"] = list(seed_ids)
        if weight is not None:
            payload["weight"] = list(weight)
        if threshold is not None:
            payload["threshold"] = threshold
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/concepts/{concept_id}/rebuild",
            json=payload,
            params=self._wait_params(wait, timeout),
            timeout=(10, 1800),
        )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def delete_concept(self, dataset_id, concept_id) -> dict:
        """Delete a concept (its materialized scores cascade-delete)."""
        resp = self.session.delete(f"{self.base_url}/datasets/{dataset_id}/concepts/{concept_id}")
        _raise_for_status(resp)
        return resp.json()

    def update_sample(
        self,
        dataset_id,
        sample_id,
        tags=None,
        metadata=None,
        remove_metadata_keys=None,
        location=None,
        heading=None,
        array_fields=None,
    ):
        """
        Update a single sample's tags, metadata, and/or location.

        Args:
            dataset_id: Dataset ID
            sample_id: Sample ID to update
            tags: If provided, replaces all existing tags
            metadata: If provided, merges with existing metadata
            remove_metadata_keys: If provided, these top-level keys are removed
                from metadata after the merge. Combine with ``metadata`` to do
                an authoritative replace of a subset of keys (add/update some,
                drop others) without touching unrelated keys.
            location: If provided, replaces location (GeoJSON or tuple)
            heading: If provided, replaces heading

        Returns:
            dict: Update confirmation
        """
        payload = {}
        if tags is not None:
            payload["tags"] = tags
        if metadata is not None:
            payload["metadata"] = metadata
        if remove_metadata_keys is not None:
            payload["remove_metadata_keys"] = list(remove_metadata_keys)
        if location is not None:
            if isinstance(location, (tuple, list)) and len(location) == 2:
                payload["location"] = {"type": "Point", "coordinates": [location[0], location[1]]}
            else:
                payload["location"] = location
        if heading is not None:
            payload["heading"] = heading
        if array_fields is not None:
            payload["array_fields"] = array_fields

        resp = self.session.patch(f"{self.base_url}/datasets/{dataset_id}/samples/{sample_id}", json=payload)
        _raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _wait_params(wait, timeout):
        """Validate a job wait before submitting; ``wait=False`` asks the server for 202 at once."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        return {} if wait else {'wait': 'false'}

    def _job_result(self, response, *, wait=True, timeout=1800):
        """Endpoints that run a server job answer within about five seconds: the final JSON
        (unchanged shape), or 202 {job_id, state}. Waiting returns that same JSON from the job;
        a wait timeout raises JobTimeout and never cancels the job."""
        _raise_for_status(response)
        if response.status_code != 202:
            return response.json()
        job = Job(self, response.json()['job_id'])
        return job.wait(timeout=timeout) if wait else job

    def _bulk_result(self, response, *, wait=True, timeout=1800):
        _raise_for_status(response)
        result = response.json()
        if 'job_id' not in result:
            return result
        job = Job(self, result['job_id'])
        return job.wait(timeout=timeout) if wait else job

    def bulk_delete_samples(
        self,
        dataset_id,
        sample_ids=None,
        *,
        filters=None,
        selection_token=None,
        excluded_ids=None,
        wait=True,
        timeout=1800,
    ):
        """Delete a captured target; cancellation retains already deleted chunks (admin only)."""
        payload = {
            'sample_ids': list(sample_ids or []),
            'filters': filters,
            'selection_token': selection_token,
            'excluded_ids': excluded_ids or [],
        }
        response = self.session.post(
            f'{self.base_url}/datasets/{dataset_id}/samples/bulk-delete',
            json=payload,
            timeout=(10, 30),
        )
        return self._bulk_result(response, wait=wait, timeout=timeout)

    def resume_bulk_job(self, job_id, *, wait=True, timeout=1800):
        response = self.session.post(f'{self.base_url}/jobs/{job_id}/resume', timeout=(10, 30))
        return self._bulk_result(response, wait=wait, timeout=timeout)

    def discard_bulk_job(self, job_id):
        """Give up a cancelled/failed bulk job: completed chunks stay, the rest never runs."""
        response = self.session.post(f'{self.base_url}/jobs/{job_id}/discard', timeout=(10, 30))
        _raise_for_status(response)
        return response.json()

    def bulk_tag_samples(
        self,
        dataset_id,
        sample_ids=None,
        add_tags=None,
        remove_tags=None,
        *,
        filters=None,
        selection_token=None,
        excluded_ids=None,
        field_name='tags',
        wait=True,
        timeout=1800,
    ):
        """
        Add or remove tags from multiple samples at once.

        Args:
            dataset_id: Dataset ID
            sample_ids: List of sample IDs to update
            add_tags: Tags to add to all samples
            remove_tags: Tags to remove from all samples

        Returns:
            dict: Update confirmation with count
        """
        payload = {
            "sample_ids": list(sample_ids or []),
            "add_tags": add_tags or [],
            "remove_tags": remove_tags or [],
            'filters': filters,
            'selection_token': selection_token,
            'excluded_ids': excluded_ids or [],
            'field_name': field_name,
            'background': not wait,
        }

        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/samples/bulk-tag", json=payload)
        return self._bulk_result(resp, wait=wait, timeout=timeout)

    def bulk_set_metadata(
        self,
        dataset_id,
        sample_ids=None,
        metadata=None,
        remove_metadata_keys=None,
        updates=None,
        *,
        filters=None,
        selection_token=None,
        excluded_ids=None,
        wait=True,
        timeout=1800,
    ):
        """Merge metadata onto many samples in one request (bulk update_sample).

        Two modes:
          - same value: pass ``sample_ids`` + ``metadata`` (shallow-merged into
            each), with optional ``remove_metadata_keys`` dropped after merge.
          - per sample: pass ``updates=[{"sample_id": id, "metadata": {...}}]``
            to merge each sample with its own patch (e.g. per-sample scores).

        Returns {"updated_count": N, ...}.
        """
        payload: dict[str, Any]
        if updates is not None:
            payload = {"updates": list(updates)}
        else:
            payload = {"sample_ids": list(sample_ids or [])}
            if metadata is not None:
                payload["metadata"] = metadata
            if remove_metadata_keys is not None:
                payload["remove_metadata_keys"] = list(remove_metadata_keys)
        payload.update(
            filters=filters,
            selection_token=selection_token,
            excluded_ids=excluded_ids or [],
            background=not wait,
        )
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/samples/bulk-set",
            json=payload,
        )
        return self._bulk_result(resp, wait=wait, timeout=timeout)

    def refresh_stats(self, dataset_id, *, wait=True, timeout=1800, force=False):
        """Refresh captured committed work, or return a durable Job when wait=False.

        New arrivals do not extend this barrier. force=True rebuilds all blocks.
        A waiting timeout leaves the server job running and includes its ID.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/refresh-stats",
            params={'wait': 'false', 'force': str(force).lower()},
            timeout=(10, 30),
        )
        _raise_for_status(resp)
        job = Job(self, resp.json()['job_id'])
        return job.wait(timeout=timeout) if wait else job

    def aggregate(self, dataset_id, aggregations, filters=None):
        """Run one or more aggregations server-side in a single request.

        Args:
            dataset_id: Dataset ID
            aggregations: list of {"field": str, "op": str, ...}. Numeric ops:
                count, sum, mean, std, min, max, bounds, quantiles (+ optional
                "quantiles": [..]). Categorical ops: count_values, distinct,
                values (+ optional "limit"; uncapped by default).
            filters: optional filter dict to scope the aggregation.

        Returns:
            list of result dicts (one per aggregation, in order).
        """
        payload = {"aggregations": aggregations}
        if filters:
            payload["filters"] = filters
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/aggregate", json=payload)
        _raise_for_status(resp)
        return resp.json().get("results", [])

    def similarity_search(
        self,
        dataset_id: int,
        sample_id: Optional[int] = None,
        vector: Optional[list] = None,
        text: Optional[str] = None,
        field_name: str = "clip",
        limit: int = 50,
        filters: Optional[dict] = None,
    ):
        """
        Search for similar samples using vector similarity.

        Args:
            dataset_id: Dataset ID to search in
            sample_id: Find samples similar to this sample's embedding
            vector: Alternatively, provide a raw embedding vector
            text: Alternatively, a natural-language prompt. Encoded server-side
                (CLIP text tower); if the server lacks transformers (501), the
                SDK encodes locally via sentence-transformers instead.
            field_name: Embedding field to use (default: "clip")
            limit: Maximum results to return
            filters: Optional filters to scope the search

        Returns:
            List of similar samples with distance scores
        """
        payload: Dict[str, Any] = {"dataset_id": dataset_id, "field_name": field_name, "limit": limit}

        if sample_id is not None:
            payload["sample_id"] = sample_id
        elif vector is not None:
            payload["vector"] = vector
        elif text is not None:
            payload["text"] = text
        else:
            raise ValueError("Either sample_id, vector, or text must be provided")

        if filters:
            payload["filters"] = filters

        resp = self.session.post(f"{self.base_url}/search", json=payload)
        if resp.status_code == 501 and text is not None:
            # Server can't encode text (no transformers there) — encode locally with the
            # same default checkpoint and retry as a plain vector search.
            import warnings as _w

            from .text import encode_text

            _w.warn(
                "Server has no text encoder; encoding locally with clip-ViT-B-32. If this field "
                "was embedded with a DIFFERENT checkpoint, results will be wrong — install "
                "transformers server-side so the field's registered model is used.",
                stacklevel=2,
            )

            payload.pop("text", None)
            payload["vector"] = encode_text(text)
            resp = self.session.post(f"{self.base_url}/search", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def get_embedding_field_inventory(self, dataset_id) -> list:
        """Every embedding field actually stored, with per-field counts.

        Use this for data movement/auditing. ``get_embedding_fields`` answers the SCATTER's
        question — only fields with computed coords, with patch fields collapsed into pseudo
        scale names that match no row — so it silently omits embedded-but-unprojected fields.

        Returns:
            [{"field_name": str, "count": int}, ...]
        """
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/embeddings/field-inventory")
        _raise_for_status(resp)
        return resp.json()

    def register_field_model(self, dataset_id, field_name, model, force=False) -> dict:
        """Record which checkpoint produced ``field_name`` (field-global registry).

        Text search/concepts consult this to pick the matching CLIP text tower and
        to refuse text-less models (DINO etc.). ``compute_embeddings`` registers
        automatically; call this once per field when ingesting your own vectors.

        Args:
            force: overwrite an existing, DIFFERENT registration. Without it the server 409s,
                which is the right default — silently re-pointing a field's model would make
                text search use the wrong text tower for vectors already stored.
        """
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/embeddings/field-model",
            json={"field_name": field_name, "model": model, "force": force},
        )
        _raise_for_status(resp)
        return resp.json()

    def patch_similarity_search(
        self,
        dataset_id: int,
        sample_id: int,
        field_name: str = "clip",
        patch_scale: str = "4x4",
        patch_row: int = 0,
        patch_col: int = 0,
        limit: int = 50,
        filters: Optional[dict] = None,
    ):
        """Search for images with patches similar to a selected patch.

        Returns the best-matching patch per image with its grid position.
        """
        payload: Dict[str, Any] = {
            "dataset_id": dataset_id,
            "sample_id": sample_id,
            "field_name": field_name,
            "patch_scale": patch_scale,
            "patch_row": patch_row,
            "patch_col": patch_col,
            "limit": limit,
        }
        if filters:
            payload["filters"] = filters

        resp = self.session.post(f"{self.base_url}/search/patches", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def upload_hashes(self, dataset_id, hashes: list):
        """Upload pre-computed image hashes.

        Args:
            dataset_id: Dataset ID
            hashes: List of dicts with keys: sample_id, field_name, hash_hex
        """
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/hashes/upload",
            json=hashes,
        )
        _raise_for_status(resp)
        return resp.json()

    def export_hashes(self, dataset_id, field_name, after_id=0, limit=10000):
        """Read back a page of stored hashes for one field, keyset-paginated.

        The counterpart to ``upload_hashes``. Pass ``after_id`` = the last ``sample_id`` of the
        previous page; rows come back ordered by ``sample_id``, so a page shorter than ``limit``
        means you have reached the end. ``get_hash_fields()`` gives the per-field total, so a
        paging caller never needs a COUNT of its own.

        Returns:
            dict with ``items``: [{sample_id, field_name, hash_hex}, ...]
        """
        resp = self.session.get(
            f"{self.base_url}/datasets/{dataset_id}/hashes/export",
            params={"field_name": field_name, "after_id": after_id, "limit": limit},
        )
        _raise_for_status(resp)
        return resp.json()

    def lookup_samples(self, dataset_id, field: str, values: list) -> dict:
        """Fast bulk lookup: metadata field values -> sample IDs.

        Args:
            dataset_id: Dataset ID
            field: Metadata field name to match on (e.g. 'source_id')
            values: List of values to look up

        Returns:
            Dict mapping field values to sample IDs
        """
        result = {}
        batch_size = 10000
        total = len(values)
        for i in range(0, total, batch_size):
            batch = values[i : i + batch_size]
            resp = self.session.post(
                f"{self.base_url}/datasets/{dataset_id}/samples/lookup",
                json={"field": field, "values": batch},
                timeout=180,
            )
            _raise_for_status(resp)
            result.update(resp.json())
            print(f"  Lookup: {min(i + batch_size, total)}/{total} ({len(result)} matched)", end='\r')
        print()
        return result

    # --- Dataset metadata ---

    def get_auth_cookies(self) -> dict:
        """Get a copy of the current auth cookies for use in separate sessions."""
        return dict(self.session.cookies)

    def clone(self) -> "Client":
        """A new Client with its own session (e.g. for another thread) and the same credentials.

        Copies the cookies, headers and the explicit Bearer auth, so a token set with
        ``set_token()`` keeps winning over a ``~/.netrc`` entry in the copy too.
        """
        copy = Client(base_url=self.base_url, timeout=self.session._default_timeout)
        copy.session.cookies.update(self.get_auth_cookies())
        copy.session.headers.update(self.session.headers)
        copy.session.auth = self.session.auth
        return copy

    def get_dataset_by_id(self, dataset_id) -> dict:
        """Fetch a single dataset by ID."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}")
        _raise_for_status(resp)
        return resp.json()

    def update_dataset(self, dataset_id, **kwargs) -> dict:
        """Update dataset settings (name, description, media_fields, segmentation_config, etc.)."""
        resp = self.session.patch(f"{self.base_url}/datasets/{dataset_id}", json=kwargs)
        _raise_for_status(resp)
        return resp.json()

    # --- Sample deletion ---

    def delete_sample(self, dataset_id, sample_id) -> dict:
        """Delete a single sample from a dataset."""
        resp = self.session.delete(f"{self.base_url}/datasets/{dataset_id}/samples/{sample_id}")
        _raise_for_status(resp)
        return resp.json()

    # --- Schema & field management ---

    def get_schema(self, dataset_id, filters=None, lightning_mode=True) -> list:
        """Get dataset field schema with stats."""
        params = {"lightning_mode": lightning_mode}
        if filters:
            import json

            params["filters"] = json.dumps(filters)
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/schema", params=params)
        _raise_for_status(resp)
        return resp.json()

    def set_field_type(self, dataset_id, field_name, field_type) -> dict:
        """Correct a metadata field's declared type without converting its stored values.

        Types: unknown, boolean, number, string, datetime, list, object.
        Remove a field's index before changing its type; native/system fields cannot change type.
        """
        resp = self.session.patch(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/type",
            json={"field_type": field_type},
        )
        _raise_for_status(resp)
        return resp.json()

    def promote_field(self, dataset_id, field_name, *, wait=True, timeout=3600):
        """Promote a field (adds index for faster filtering). The index builds in a server job;
        this waits for it and returns {message}. ``wait=False`` returns the Job."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/promote",
            params=self._wait_params(wait, timeout),
        )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def demote_field(self, dataset_id, field_name, *, wait=True, timeout=3600):
        """Demote a field (removes its index online, in a server job); this waits for it and returns
        {message}. ``wait=False`` returns the Job."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/demote",
            params=self._wait_params(wait, timeout),
        )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def rename_field(self, dataset_id, field_name, new_name, *, wait=True, timeout=1800, overwrite=False):
        """Rename a metadata key across every sample."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/rename",
            json={"new_name": new_name, "overwrite": overwrite, "background": not wait},
        )
        return self._bulk_result(resp, wait=wait, timeout=timeout)

    def clone_field(self, dataset_id, field_name, new_name, *, wait=True, timeout=1800, overwrite=False):
        """Copy a metadata key into a new key on every sample."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/clone",
            json={"new_name": new_name, "overwrite": overwrite, "background": not wait},
        )
        return self._bulk_result(resp, wait=wait, timeout=timeout)

    def clear_field(self, dataset_id, field_name, *, wait=True, timeout=1800):
        """Remove a metadata key from every sample."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/clear",
            params={"background": str(not wait).lower()},
        )
        return self._bulk_result(resp, wait=wait, timeout=timeout)

    def set_field_display_type(self, dataset_id, field_name, display_type) -> dict:
        """Set the display type for a field (e.g., 'enum' for dropdowns on numeric fields)."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/fields/{field_name}/display-type",
            json={"display_type": display_type},
        )
        _raise_for_status(resp)
        return resp.json()

    # --- Array fields ---

    def create_array_field(self, dataset_id, name) -> dict:
        """Create a new TEXT[] array field on the samples table."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/array-fields",
            json={"field_name": name},
        )
        _raise_for_status(resp)
        return resp.json()

    def list_array_fields(self, dataset_id) -> list:
        """List all dynamic array fields on a dataset."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/array-fields")
        _raise_for_status(resp)
        return resp.json()

    # --- Views ---

    def create_view(self, dataset_id, name, content, description=None, is_public=False, username=None) -> dict:
        """Create a saved view on a dataset."""
        payload = {"name": name, "content": content, "is_public": is_public}
        if description:
            payload["description"] = description
        if username:
            payload["username"] = username
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/views", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def list_views(self, dataset_id) -> list:
        """List saved views for a dataset."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/views")
        _raise_for_status(resp)
        return resp.json()

    def get_view_by_slug(self, slug) -> dict:
        """Get a saved view by its slug."""
        resp = self.session.get(f"{self.base_url}/views/{slug}")
        _raise_for_status(resp)
        return resp.json()

    def delete_view(self, view_id) -> dict:
        """Delete a saved view."""
        resp = self.session.delete(f"{self.base_url}/views/{view_id}")
        _raise_for_status(resp)
        return resp.json()

    # --- Versions ---

    def get_version_status(self, dataset_id) -> dict:
        """Get current version status (latest version + has_changes flag)."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/versions/status")
        _raise_for_status(resp)
        return resp.json()

    def create_version(self, dataset_id, name, description=None, include_metadata=True) -> dict:
        """Create a new dataset version snapshot."""
        payload = {"name": name, "include_metadata": include_metadata}
        if description:
            payload["description"] = description
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/versions", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def list_versions(self, dataset_id) -> list:
        """List all versions of a dataset."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/versions")
        _raise_for_status(resp)
        return resp.json()

    def get_version(self, dataset_id, version_id) -> dict:
        """Get a specific version's details."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/versions/{version_id}")
        _raise_for_status(resp)
        return resp.json()

    def delete_version(self, dataset_id, version_id) -> dict:
        """Delete a dataset version."""
        resp = self.session.delete(f"{self.base_url}/datasets/{dataset_id}/versions/{version_id}")
        _raise_for_status(resp)
        return resp.json()

    def get_current_diff(self, dataset_id, version_id) -> dict:
        """Get diff between current state and a version."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/versions/current-diff/{version_id}")
        _raise_for_status(resp)
        return resp.json()

    def get_current_diff_samples(self, dataset_id, version_id, change_type, limit=100, offset=0) -> list:
        """Get sample IDs for a specific change type in current diff."""
        resp = self.session.get(
            f"{self.base_url}/datasets/{dataset_id}/versions/current-diff/{version_id}/samples",
            params={"change_type": change_type, "limit": limit, "offset": offset},
        )
        _raise_for_status(resp)
        return resp.json()

    def get_version_diff(self, dataset_id, version_a, version_b) -> dict:
        """Get diff between two versions."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/versions/diff/{version_a}/{version_b}")
        _raise_for_status(resp)
        return resp.json()

    def revert_version(self, dataset_id, version_id, restore_deleted=False, exact_membership=False) -> dict:
        """Revert dataset to a specific version.

        restore_deleted=True also re-creates samples deleted since the version
        (from snapshot; embeddings/hashes must be backfilled — see get_missing).
        """
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/versions/{version_id}/revert",
            params={"restore_deleted": restore_deleted, "exact_membership": exact_membership},
        )
        _raise_for_status(resp)
        return resp.json()

    def get_version_samples(self, dataset_id, version_id, limit=100, offset=0, full=False, after_id=None) -> dict:
        """Get samples from a specific version snapshot."""
        resp = self.session.get(
            f"{self.base_url}/datasets/{dataset_id}/versions/{version_id}/samples",
            params={"limit": limit, "offset": offset, "full": full, "after_id": after_id},
        )
        _raise_for_status(resp)
        return resp.json()

    def query_version_samples(
        self,
        dataset_id,
        version_id,
        filters=None,
        fields=None,
        limit=1000,
        after_id=None,
    ) -> list:
        """One page of a version's samples matching ``filters`` (id order, keyset-paged by ``after_id``).

        ``fields`` limits what the server reads; None returns whole rows.
        """
        payload = {"filters": filters, "fields": fields, "limit": limit, "after_id": after_id}
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/versions/{version_id}/samples/query",
            json=payload,
        )
        _raise_for_status(resp)
        return resp.json()["items"]

    def aggregate_version(self, dataset_id, version_id, aggregations, filters=None):
        """Like ``aggregate`` but over a version's stored samples. Returns one result dict per aggregation."""
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/versions/{version_id}/aggregate",
            json={"aggregations": aggregations, "filters": filters},
        )
        _raise_for_status(resp)
        return resp.json().get("results", [])

    # --- Embeddings ---

    def get_embedding_fields(self, dataset_id) -> list:
        """List available embedding field names."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/embeddings/fields")
        _raise_for_status(resp)
        return resp.json()

    def get_coverage(self, dataset_id) -> dict:
        """Per-field present/missing counts for embeddings, hashes, and location."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/coverage")
        _raise_for_status(resp)
        return resp.json()

    def get_missing(self, dataset_id, kind, field=None, limit=1000, offset=0, after_id=None) -> dict:
        """Samples lacking a derived artifact (kind='embedding'|'hash'|'location').
        Returns {items: [{id, filepath}], total}. after_id lists only samples with id > after_id."""
        params = {"kind": kind, "limit": limit, "offset": offset}
        if after_id is not None:
            params["after_id"] = after_id
        if field is not None:
            params["field"] = field
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/missing", params=params)
        _raise_for_status(resp)
        return resp.json()

    def query_embeddings(self, dataset_id, field="clip", filters=None, sample_pct=100, include_fields=None) -> dict:
        """Query embeddings with optional filters and sampling."""
        payload = {"field": field, "sample_pct": sample_pct}
        if filters:
            payload["filters"] = filters
        if include_fields:
            payload["include_fields"] = include_fields
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/embeddings/query", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def lookup_embedding_coordinates(self, dataset_id, sample_ids, field="clip") -> dict:
        """Get 2D UMAP visualization coordinates for specific sample IDs.

        Returns ``{"points": [{"sample_id", "x", "y"}, ...]}`` from the
        embeddings ``/lookup`` endpoint.

        NOTE: these are the reduced 2D coordinates produced by
        ``reduce_embeddings`` / ``compute_visualization``, **not** raw embedding
        vectors. For raw vectors use ``get_vectors()``.
        """
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/embeddings/lookup",
            json={"sample_ids": sample_ids, "field": field},
        )
        _raise_for_status(resp)
        return resp.json()

    # --- Hashes ---

    def get_hash_fields(self, dataset_id) -> list:
        """List available hash field names."""
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/hashes/fields")
        _raise_for_status(resp)
        data = resp.json()
        # Backend returns [{field_name, count}], normalize to just names
        if data and isinstance(data[0], dict):
            return [d['field_name'] for d in data]
        return data

    def get_hash_stats(self, dataset_id, filters=None) -> dict:
        """Get hash coverage and duplicate statistics."""
        if filters:
            resp = self.session.post(
                f"{self.base_url}/datasets/{dataset_id}/hashes/stats",
                json={"filters": filters},
            )
        else:
            resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/hashes/stats")
        _raise_for_status(resp)
        return resp.json()

    def find_hash_duplicates(self, dataset_id, field_name, limit=50, offset=0, filters=None) -> dict:
        """Find groups of exact-duplicate images by hash."""
        payload = {"field_name": field_name, "limit": limit, "offset": offset}
        if filters:
            payload["filters"] = filters
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/hashes/duplicates", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def sample_duplicates(self, dataset_id, sample_id, field_name="_any") -> dict:
        """Exact duplicates of one sample (samples sharing an identical hash).

        field_name: a specific hash field, "_any" (match on >=1 of the sample's
        hash fields), or "_all" (match on every hash field it has). The sample
        itself is included in the returned sample_ids.
        """
        payload = {"sample_id": sample_id, "field_name": field_name}
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/hashes/sample-duplicates", json=payload)
        _raise_for_status(resp)
        return resp.json()

    # --- Quality scoring (server-side ANN over the per-field HNSW index) ---

    def _quality_post(self, dataset_id, path, payload) -> dict:
        resp = self.session.post(f"{self.base_url}/datasets/{dataset_id}/quality/{path}", json=payload)
        _raise_for_status(resp)
        return resp.json()

    def build_ann_index(self, dataset_id, field, *, wait=True, timeout=3700):
        """Build the per-field HNSW index server-side (idempotent; CONCURRENTLY).

        One-time per embedding field; required by uniqueness/duplicates and what
        makes similarity search an index scan instead of an exact seq scan. Can
        take minutes on millions of vectors: the build is a server job (progress
        and cancel in the dataset's Jobs), and this waits for it — ``timeout``
        covers the server's 3600 s build cap. ``wait=False`` returns the Job.
        """
        resp = self.session.post(
            f"{self.base_url}/datasets/{dataset_id}/quality/ann-index",
            json={"field": field},
            params=self._wait_params(wait, timeout),
            timeout=(10, 300),
        )
        return self._job_result(resp, wait=wait, timeout=timeout)

    def quality_uniqueness_compute(
        self,
        dataset_id,
        field,
        k,
        ef_search,
        after_id,
        chunk,
        metadata_field,
        allow_seq_scan=False,
    ) -> dict:
        return self._quality_post(
            dataset_id,
            "uniqueness/compute",
            {
                "field": field,
                "k": k,
                "ef_search": ef_search,
                "after_id": after_id,
                "chunk": chunk,
                "metadata_field": metadata_field,
                "allow_seq_scan": allow_seq_scan,
            },
        )

    def quality_representativeness_centroid(self, dataset_id, field) -> dict:
        return self._quality_post(dataset_id, "representativeness/centroid", {"field": field})

    def quality_representativeness_compute(self, dataset_id, field, centroid, after_id, chunk, metadata_field) -> dict:
        return self._quality_post(
            dataset_id,
            "representativeness/compute",
            {
                "field": field,
                "centroid": centroid,
                "after_id": after_id,
                "chunk": chunk,
                "metadata_field": metadata_field,
            },
        )

    def quality_range(self, dataset_id, field) -> dict:
        resp = self.session.get(f"{self.base_url}/datasets/{dataset_id}/quality/range", params={"field": field})
        _raise_for_status(resp)
        return resp.json()

    def quality_normalize(self, dataset_id, metadata_field, lo, span, after_id, chunk) -> dict:
        return self._quality_post(
            dataset_id,
            "normalize",
            {"metadata_field": metadata_field, "lo": lo, "span": span, "after_id": after_id, "chunk": chunk},
        )

    def quality_duplicates_compute(
        self,
        dataset_id,
        field,
        thresh,
        k,
        ef_search,
        after_id,
        chunk,
        token,
        allow_seq_scan=False,
    ) -> dict:
        return self._quality_post(
            dataset_id,
            "duplicates/compute",
            {
                "field": field,
                "thresh": thresh,
                "k": k,
                "ef_search": ef_search,
                "after_id": after_id,
                "chunk": chunk,
                "token": token,
                "allow_seq_scan": allow_seq_scan,
            },
        )

    def quality_duplicates_finalize(self, dataset_id, token, group_field="emb_dup_group") -> dict:
        return self._quality_post(dataset_id, "duplicates/finalize", {"token": token, "group_field": group_field})

    def quality_duplicates_groups(self, dataset_id, group_field="emb_dup_group", limit=50, offset=0) -> dict:
        return self._quality_post(
            dataset_id,
            "duplicates/groups",
            {"group_field": group_field, "limit": limit, "offset": offset},
        )


# Global client instance
_client = None


def connect(url=None, api_token=None):
    """Configure the global Freedom Ridge client.

    Args:
        url: API base URL. Defaults to ``$FRIDGE_API_URL`` or
            ``http://localhost:8000/api``.
        api_token: Optional API token (Bearer). Falls back to
            ``$FRIDGE_API_TOKEN``. When set, no ``login()`` is needed.

    Example:
        import fridge
        fridge.connect("http://localhost:8000/api", api_token="...")
        # or, for interactive use:
        fridge.connect("http://localhost:8000/api")
        fridge.get_client().login("user", "pass")
    """
    global _client
    _client = Client(url, api_token=api_token)
    return _client


def get_client():
    global _client
    if _client is None:
        _client = Client()
    return _client

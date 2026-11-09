import ipaddress
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import public_settings
from ..api_errors import CodedHTTPException
from ..auth_cleanup import DEMO_ACCOUNT_LOCK, cleanup_accounts
from ..auth_policy import validate_new_password
from ..crypto import hash_token
from ..database import db
from ..passwords import DEMO_PASSWORD, DUMMY_HASH, hash_password, verify_password
from ..request_limits import set_request_user
from ..security import private_guest, public_mode

router = APIRouter()

# Configurable session lifetime. Active users get their session extended
# automatically (sliding window), so this is really the *idle* timeout.
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "24"))

# Cookie secure flag — True sends cookie only over HTTPS. Default to True
# unless explicitly set to "false" for local dev over plain HTTP.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() != "false"

# In-process login throttle: at most _LOGIN_MAX_FAILS failed attempts per
# (client IP, username) within _LOGIN_WINDOW_S. Bounds online password guessing
# against known usernames (e.g. the default admin). NOTE: state is per-worker, so
# with N uvicorn workers the effective cap is N×; for a hard global limit move
# this to Postgres/Redis. Good enough to blunt brute force without a new dep.
_LOGIN_FAILS: dict = defaultdict(list)
_LOGIN_WINDOW_S = 300
_LOGIN_MAX_FAILS = 10
_LOGIN_SWEEP_EVERY = 500  # opportunistically GC stale keys this often
_login_calls = 0


def _trusted_proxy_networks() -> list:
    raw = os.getenv("FRIDGE_TRUSTED_PROXIES", "")
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                logging.getLogger("fridge.auth").warning("ignoring bad FRIDGE_TRUSTED_PROXIES entry %r", part)
    return nets


_TRUSTED_PROXIES = _trusted_proxy_networks()


def _is_trusted_proxy(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_PROXIES)


def _client_ip(request) -> str:
    """Best-effort real client IP for throttling.

    Behind our nginx, request.client.host is the proxy's address (so every
    external client would share one key). nginx overwrites X-Real-IP with the
    real client ($remote_addr), so prefer it. These headers are only trusted
    because the deploy terminates at our own nginx; the throttle is an
    availability control, not an auth boundary.
    """
    peer = request.client.host if request.client else "?"
    # Forwarding headers only mean something when they come FROM our proxy. Served directly (the
    # development backend on :8000), a client could send a fresh X-Real-IP per attempt and never be
    # throttled. FRIDGE_TRUSTED_PROXIES lists the proxy CIDRs (the prod
    # compose sets it: nginx is the backend's only peer there). Default: none — and not loopback,
    # because the Next.js dev rewrite (:3000) passes the client's own headers through unchanged.
    candidate = peer
    if _is_trusted_proxy(peer):
        candidate = request.headers.get('x-real-ip') or request.headers.get('x-forwarded-for', peer).split(',')[-1]
    try:
        return str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        return peer


def _login_recent_fails(key: str, now: float) -> list:
    """Prune and return the recent failure timestamps for a throttle key."""
    fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW_S]
    if fails:
        _LOGIN_FAILS[key] = fails
    else:
        _LOGIN_FAILS.pop(key, None)
    return fails


def _maybe_sweep_login_fails(now: float) -> None:
    """Bound _LOGIN_FAILS memory: a username-spray creates a key per fake
    username, and entries are otherwise only reclaimed when that exact key is
    retried. Every _LOGIN_SWEEP_EVERY login calls, drop all stale keys."""
    global _login_calls
    _login_calls += 1
    if _login_calls % _LOGIN_SWEEP_EVERY == 0:
        for k in [k for k, ts in _LOGIN_FAILS.items() if not ts or now - ts[-1] >= _LOGIN_WINDOW_S]:
            _LOGIN_FAILS.pop(k, None)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    role: str
    must_change_password: bool
    is_active: bool
    is_demo_account: bool = False
    can_share_views: bool = True


# A user flagged must_change_password may reach ONLY the endpoints needed to change it / check
# status / log out — everything else 403s. Without this, a seeded/default credential (e.g. the
# admin/admin123 bootstrap) could use the API (mint tokens, read/write data) before rotating the
# password; the frontend change-password modal is UX, not a security boundary.
_PW_CHANGE_ALLOWED = ("/auth/change-password", "/auth/me", "/auth/logout")


def _enforce_password_change(user: dict, request: Request) -> None:
    if user.get("must_change_password") and not request.url.path.endswith(_PW_CHANGE_ALLOWED):
        raise HTTPException(
            status_code=403,
            detail="Password change required before using the API — call /api/auth/change-password.",
        )


async def _token_user(auth_header: str) -> tuple[dict, str]:
    """(user, token scope) for an Authorization "Bearer <token>" header; 401 for an unknown, revoked or expired
    token, 403 for an inactive account."""
    token = auth_header.replace("Bearer ", "").strip()
    # Tokens are stored hashed; look up by the hash of the presented token.
    query = """
        SELECT u.id, u.username, u.email, u.role, u.must_change_password, u.is_active,
               u.is_demo_account, t.id as token_id, t.scope
        FROM api_tokens t
        JOIN users u ON t.user_id = u.id
        WHERE t.token = $1
          AND t.is_active = TRUE
          AND (t.expires_at IS NULL OR t.expires_at > NOW())
    """
    user = await db.fetchrow(query, hash_token(token))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not user['is_active']:
        raise HTTPException(status_code=403, detail="Account is inactive")
    await db.execute("UPDATE api_tokens SET last_used_at = NOW() WHERE id = $1", user['token_id'])
    user_dict = dict(user)
    del user_dict['token_id'], user_dict['scope']
    return user_dict, user['scope']


async def get_worker_user(request: Request) -> dict:
    """The user behind a compute worker token. The worker endpoints accept these tokens and nothing else: no
    session, no full token. As for creating a token, the user must be a power user or an admin."""
    auth_header = request.headers.get("Authorization")
    if not (auth_header and auth_header.startswith("Bearer ")):
        raise HTTPException(status_code=401, detail="A compute worker authenticates with a worker token")
    user, scope = await _token_user(auth_header)
    if scope != 'worker':
        raise HTTPException(status_code=403, detail="Use a worker token (scope 'worker') for a compute worker")
    if user['role'] not in get_allowed_roles('power_user'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    await set_request_user(user)
    _enforce_password_change(user, request)
    return user


async def get_follower_user(request: Request) -> dict:
    """The account behind a follower instance's token. Only the follower endpoints (routers/followers.py) accept these
    tokens, and they accept nothing else: no session, no full or worker token."""
    auth_header = request.headers.get("Authorization")
    if not (auth_header and auth_header.startswith("Bearer ")):
        raise HTTPException(status_code=401, detail="A follower instance authenticates with its follower token")
    try:
        user, scope = await _token_user(auth_header)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        # Removing a follower deletes its token: the code tells the follower to stop its mirrors.
        raise CodedHTTPException(401, str(error.detail), 'follower_token_invalid') from None
    if scope != 'follower':
        raise HTTPException(status_code=403, detail="Use the follower's token (Settings → Followers) here")
    await set_request_user(user)
    return user


async def get_current_user(request: Request):
    """
    Authenticate via API token (Bearer) OR session cookie.

    Checks Authorization header first for "Bearer <token>",
    then falls back to session_id cookie for browser sessions.

    Tokens inherit user's role permissions.
    """
    # Check for Bearer token first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        user_dict, scope = await _token_user(auth_header)
        if scope == 'worker':
            raise HTTPException(
                status_code=403,
                detail="This is a compute worker token: only the worker endpoints (python -m fridge.worker) accept it",
            )
        if scope == 'follower':
            raise HTTPException(
                status_code=403,
                detail="This is a follower token: only the follower endpoints accept it",
            )
        await set_request_user(user_dict)
        _enforce_password_change(user_dict, request)
        return user_dict

    # Fall back to session cookie authentication
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    query = """
        SELECT u.id, u.username, u.email, u.role, u.must_change_password,
               u.is_active, u.is_demo_account, s.expires_at
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.id = $1 AND s.expires_at > NOW()
    """
    user = await db.fetchrow(query, session_id)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    if not user['is_active']:
        raise HTTPException(status_code=403, detail="Account is inactive")

    await set_request_user(dict(user))

    # Sliding window: extend the session while the user is active so they
    # don't get kicked mid-work after SESSION_TTL_HOURS. Only fires the
    # UPDATE when less than half the TTL remains — for a 24h TTL that means
    # at most one extension per ~12h of activity. Fire-and-forget so the
    # request isn't blocked on the write.
    # Sliding window: extend the session while the user is active so they don't
    # get kicked mid-work. Done entirely with the DB clock (NOW()) to avoid any
    # client/UTC skew — the WHERE only writes once less than half the TTL remains,
    # so a no-op UPDATE (index probe, 0 rows) is the common case.
    await db.execute(
        "UPDATE sessions SET expires_at = NOW() + $2::double precision * INTERVAL '1 hour' "
        "WHERE id = $1 AND expires_at < NOW() + $3::double precision * INTERVAL '1 hour'",
        session_id,
        float(SESSION_TTL_HOURS),
        SESSION_TTL_HOURS / 2,
    )

    user_dict = dict(user)
    del user_dict['expires_at']  # Internal field, not part of the user response
    _enforce_password_change(user_dict, request)
    return user_dict


async def get_optional_user(request: Request) -> Optional[dict]:
    """
    Like get_current_user, but returns None instead of raising 401 when no
    valid auth is present. Used by endpoints that allow unauthenticated access
    in some cases (e.g., the sample media proxy for global datasets).

    A 401 from no credentials, or from an expired/invalid session cookie, degrades
    to anonymous (returns None) so a stale cookie doesn't hard-fail public/global
    datasets and force a logout. An invalid or expired Bearer token is NOT
    degraded: the client explicitly presented a credential, and serving it as
    anonymous makes a script with a bad token see empty results instead of an
    error. A 403 (inactive account) is always propagated.
    """
    try:
        return await get_current_user(request)
    except HTTPException as e:
        # Propagate 403 (e.g. inactive account), which is an authorization decision,
        # not "who are you".
        if e.status_code == 401 and not _has_bearer_token(request):
            return None
        raise


def _has_bearer_token(request: Request) -> bool:
    """Whether the request presents an API token (the same check get_current_user makes)."""
    auth_header = request.headers.get("Authorization")
    return bool(auth_header and auth_header.startswith("Bearer "))


def get_allowed_roles(required_role: str) -> list[str]:
    hierarchy = {'admin': ['admin'], 'power_user': ['admin', 'power_user'], 'guest': ['admin', 'power_user', 'guest']}
    return hierarchy.get(required_role, [])


def require_role(role: str):
    def check_role(user: dict = Depends(get_current_user)):
        if user['role'] not in get_allowed_roles(role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return check_role


@router.post("/auth/login")
async def login(creds: LoginRequest, request: Request, response: Response):
    # 0. Throttle: refuse once too many recent failures for this (IP, username).
    ip = _client_ip(request)
    throttle_key = f"{ip}:{creds.username}"
    now = time.time()
    _maybe_sweep_login_fails(now)
    if len(_login_recent_fails(throttle_key, now)) >= _LOGIN_MAX_FAILS:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")

    def _record_fail():
        _LOGIN_FAILS[throttle_key].append(time.time())

    # 1. Get user
    user = await db.fetchrow("SELECT id, password_hash, is_active FROM users WHERE username = $1", creds.username)

    if not user:
        # Dummy check to prevent timing attacks
        await verify_password("dummy", DUMMY_HASH)
        _record_fail()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # 2. Verify password — BEFORE saying the account is inactive: answering that first told anyone
    # with a wrong password that the account exists and is deactivated, and never counted the
    # attempt toward the throttle.
    if not await verify_password(creds.password, user['password_hash']):
        _record_fail()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user['is_active']:
        raise HTTPException(status_code=403, detail="Account is inactive")

    # Success — clear the failure counter for this key.
    _LOGIN_FAILS.pop(throttle_key, None)

    # 3. Create session — expiry computed with the DB clock (NOW()) so it matches
    # the NOW()-based expiry check on every request. (Computing it from Python's
    # local time made sessions expire ~TZ-offset hours early.)
    session_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO sessions (id, user_id, expires_at) "
        "VALUES ($1, $2, NOW() + $3::double precision * INTERVAL '1 hour')",
        session_id,
        user['id'],
        float(SESSION_TTL_HOURS),
    )

    # 4. Update last login
    await db.execute("UPDATE users SET last_login = NOW() WHERE id = $1", user['id'])

    # 5. Set cookie. max_age is deliberately much longer than SESSION_TTL
    # because the sliding window in get_current_user extends the DB-side
    # expiry on activity. The cookie is just a lookup key — the DB expiry
    # is the real timeout. If we tied max_age to SESSION_TTL, the browser
    # would stop sending the cookie before the (extended) DB session expired,
    # defeating the sliding window entirely.
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=30 * 24 * 3600,  # 30 days; DB expiry is the real timeout
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )

    return {"message": "Logged in successfully"}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    session_id = request.cookies.get("session_id")
    if session_id:
        await db.execute("DELETE FROM sessions WHERE id = $1", session_id)

    response.delete_cookie("session_id")
    return {"message": "Logged out successfully"}


@router.get("/auth/me", response_model=UserResponse)
async def get_me(user: dict = Depends(get_current_user)):
    return {**user, 'can_share_views': not private_guest(user)}


@router.post("/auth/change-password")
async def change_password(data: ChangePasswordRequest, request: Request, user: dict = Depends(get_current_user)):
    # Get current user details
    user_row = await db.fetchrow(
        "SELECT password_hash, must_change_password, is_demo_account FROM users WHERE id = $1",
        user['id'],
    )

    if user_row.get('is_demo_account') and public_mode():
        raise HTTPException(status_code=403, detail="Demo accounts cannot change their password")
    # Forced changes still require the temporary password that the user was given.
    if not await verify_password(data.current_password, user_row['password_hash']):
        raise HTTPException(status_code=400, detail="Incorrect current password")

    # Server-side policy (the modal's minLength alone was bypassable via the API).
    validate_new_password(data.new_password, user.get('username'))

    # Hash new password
    new_hash = await hash_password(data.new_password)

    # Update password, clear must_change_password, and mark as no longer demo account
    await db.execute(
        """
        UPDATE users
        SET password_hash = $1, must_change_password = false, is_demo_account = false
        WHERE id = $2
        """,
        new_hash,
        user['id'],
    )

    # Invalidate the user's OTHER sessions so a stolen or stale session can't
    # outlive a password change. The current session is preserved so the user
    # isn't logged out mid-action. (For a Bearer-token caller there's no session
    # cookie, so this clears all their sessions, which is the safe default.)
    current_sid = request.cookies.get("session_id")
    await db.execute(
        "DELETE FROM sessions WHERE user_id = $1 AND id IS DISTINCT FROM $2",
        user['id'],
        current_sid,
    )

    return {"message": "Password changed successfully"}


@router.post("/auth/demo-login")
async def demo_login(request: Request, response: Response):
    """
    Create a demo guest account and log in automatically.
    Only works when DEMO_MODE environment variable is set to 'true'.
    """
    # Check if demo mode is enabled
    demo_mode = os.getenv("DEMO_MODE", "false").lower() == "true"
    if not demo_mode:
        raise HTTPException(status_code=404, detail="Demo mode is not enabled")

    username = f"guest_{secrets.token_hex(8)}"
    session_id = str(uuid.uuid4())
    limits = public_settings.settings
    client_ip = _client_ip(request)
    try:
        async with db.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('lock_timeout', $1, true)",
                str(max(1, int(limits.acquire_seconds * 1000))),
            )
            await connection.execute('SELECT pg_advisory_xact_lock($1)', DEMO_ACCOUNT_LOCK)
            if not limits.ip_allowlisted(client_ip):
                admission = await connection.fetchrow(
                    """SELECT count(*) FILTER (WHERE created_at > NOW()-INTERVAL '1 hour') AS recent,
                              count(*) FILTER (WHERE user_id IS NOT NULL) AS live
                       FROM demo_admissions WHERE client_ip=$1""",
                    client_ip,
                )
                if admission['recent'] >= limits.demo_ip_hourly:
                    raise HTTPException(
                        status_code=429,
                        detail='Too many guest accounts created from this IP in the last hour. Please try again later.',
                        headers={'Retry-After': '3600'},
                    )
                if admission['live'] >= limits.demo_ip_live:
                    raise HTTPException(
                        status_code=429,
                        detail='This IP has reached its live guest account limit. '
                        'Please reuse an existing guest session.',
                        headers={'Retry-After': '300'},
                    )
            count = await connection.fetchval(
                "SELECT count(*) FROM users WHERE is_demo_account",
            )
            if count >= limits.demo_account_cap:
                raise HTTPException(
                    status_code=503,
                    detail="The demo is busy: its guest account limit has been reached. Please try again later.",
                    headers={'Retry-After': '300'},
                )
            user_id = await connection.fetchval(
                """INSERT INTO users
                   (username,password_hash,role,is_active,must_change_password,is_demo_account,last_login)
                   VALUES ($1,$2,'guest',true,false,true,NOW()) RETURNING id""",
                username,
                DEMO_PASSWORD,
            )
            await connection.execute(
                "INSERT INTO demo_admissions(client_ip,user_id) VALUES ($1,$2)",
                client_ip,
                user_id,
            )
            await connection.execute(
                "INSERT INTO sessions (id,user_id,expires_at) VALUES ($1,$2,NOW()+INTERVAL '1 day')",
                session_id,
                user_id,
            )
    except asyncpg.LockNotAvailableError as error:
        raise HTTPException(
            status_code=503,
            detail='The demo is busy creating guest accounts. Please try again shortly.',
            headers={'Retry-After': '5'},
        ) from error

    # Set cookie. Honor SESSION_COOKIE_SECURE like the real login flow so demo
    # sessions aren't sent in clear over HTTPS deployments.
    response.set_cookie(
        key="session_id",
        value=session_id,
        httponly=True,
        max_age=86400,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )

    return {"message": "Demo account created", "username": username}


@router.delete("/auth/cleanup-demo-accounts")
async def cleanup_demo_accounts(user: dict = Depends(require_role("admin"))):
    """Delete demo accounts older than one day, their views, and expired sessions."""
    deleted_count = await cleanup_accounts(db.pool)
    return {"message": f"Cleaned up {deleted_count} stale demo accounts", "deleted_count": deleted_count}

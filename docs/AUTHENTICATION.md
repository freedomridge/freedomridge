# Authentication

Every API call needs a signed-in user; there are no anonymous reads. Browsers use a session
cookie. Scripts and the Python SDK use an API token.

## Signing in

The web UI signs in at `/login`. `POST /api/auth/login` checks the password and sets an HttpOnly
`session_id` cookie (SameSite=Lax). A session expires after `SESSION_TTL_HOURS` of inactivity
(default 24); activity extends it.

Passwords must be at least 8 characters, must not equal the username, and must not be one of a
short list of very common passwords. The same rule applies to every place a password is set.

## The first admin account

**Development** (`FRIDGE_ENV` is anything but `production` or `prod`; the default is
`development`): the database starts with `admin` /
`admin123`, flagged for a password change. Until the password is changed, the API answers 403 on
every route except `/api/auth/change-password`, `/api/auth/me` and `/api/auth/logout`, for
sessions and tokens alike. The UI asks for the new password at first login.

**Production** (`FRIDGE_ENV=production`): set `FRIDGE_ADMIN_PASSWORD` before the first start.
While the admin account still has the seeded `admin123` password, startup sets it to
`FRIDGE_ADMIN_PASSWORD`, clears the change flag and signs out existing admin sessions. If the
variable is missing, equals `admin123`, or fails the password rule, the backend refuses to start.
Once the admin has any other password, startup ignores the variable, so changing it later does
not change the password. `docker-compose.prod.yml` refuses to start without it.

To reset the admin password on a production deployment:

```bash
docker compose -f docker-compose.prod.yml exec backend python /app/reset_admin.py
```

With no argument this applies `FRIDGE_ADMIN_PASSWORD` from the container's environment; pass
`--password` to choose another one. It also reactivates the account and signs out its sessions.

## Roles

Each user has one role. Admins create users and set roles in **Settings → User Management**. New
users, and users whose password an admin resets, must choose a new password at their next login.

| Action | guest | power_user | admin |
|---|---|---|---|
| See a dataset | if it is global, owned by them, or shared with them | same as guest | all datasets |
| Browse, filter, search, export, save views | yes | yes | yes |
| Add and update samples, tags, fields, embeddings, concepts and versions | no | yes | yes |
| Create API tokens | no | yes | yes |
| Create, edit and delete datasets; delete samples; share datasets with users | no | no | yes |
| Manage users, storage, backups, the index advisor and system status | no | no | yes |

New datasets are private: admins, the dataset's owner and users granted access
(`Dataset.grant_access(user_id)`) see them. An admin opens a dataset to every signed-in user, demo
guests included, under Dataset Settings → General → Visibility, and can make it private again
there. From the SDK, `Dataset(name, is_global=True)` creates a dataset that is already open.

## API tokens

A token authenticates as the user who created it, with that user's current role. Send it as
`Authorization: Bearer <token>`. The token is a random 43-character URL-safe string with no
prefix. The server stores only a hash, so the value is shown once, when it is created.

Create one in the UI:

1. Open **Settings → API Tokens**.
2. Click **Create New Token**.
3. Enter a **Token Name** and choose an **Expiration** (30 days, 90 days, 180 days or 1 year).
4. Click **Create Token**, then copy the value from the banner.

Or from a script that is already signed in, for example with an existing token:

```python
import fridge

client = fridge.connect("http://localhost:8000/api", api_token="EXISTING_TOKEN")
new = client.create_token("ci-runner", expires_days=30)
print(new["token"])  # shown only now
```

Use a token:

```python
import fridge

fridge.connect("http://localhost:8000/api", api_token="...")  # or set FRIDGE_API_URL and FRIDGE_API_TOKEN
print(fridge.get_client().whoami())
```

```bash
curl -H "Authorization: Bearer $FRIDGE_API_TOKEN" http://localhost:8000/api/auth/me
```

Details:

- Guests cannot create tokens; `POST /api/tokens` needs `power_user` or `admin`.
- Through the API, `expires_days` accepts 1 to 36500 and defaults to 90. `"expires_days": null`
  creates a token that never expires.
- **Revoke** on the tokens page, `client.revoke_token(token_id)` or `DELETE /api/tokens/{id}`
  disables a token immediately. Users can list and revoke only their own tokens
  (`client.list_tokens()`, `GET /api/tokens`).
- A deactivated user's tokens stop working (403).
- A **worker token** (on the tokens page, **Used by: a compute worker**; `"scope": "worker"`;
  `client.create_token(name, scope="worker")`) is for a compute
  worker, a machine that runs jobs for the server (`python -m fridge.worker`). Only the worker endpoints
  accept it; everywhere else it gets a 403, so the token kept on a worker machine can do only what a
  worker does. A full token or a session doesn't work on the worker endpoints either.
- A **follower token** is created with a follower on the primary (`POST /api/followers`, admins only), for
  a [follower instance](FOLLOWERS.md). It belongs to the follower's own account, `follower:<name>`, and
  only the follower endpoints accept it. Removing the follower revokes it.

The SDK's `client.login(username, password)` also works, but it keeps the password in your script
and uses a cookie session that expires when idle. Prefer a token.

## Guests and demo mode

With `DEMO_MODE=true` the login page shows **Try as Guest**. `POST /api/auth/demo-login` creates
an account named `guest_<random>` with the `guest` role and a one-day session. Without demo mode
that route returns 404.

The backend is in *public mode* when `FRIDGE_ENV=production` or `DEMO_MODE=true`. In public mode:

- Demo accounts cannot change their password, and guests cannot share saved views; their views
  stay private.
- A guest can keep at most `FRIDGE_GUEST_VIEW_COUNT` saved views (default 200, then 429) totalling
  `FRIDGE_GUEST_VIEW_TOTAL_BYTES` (default 2 MiB, then 413).
- Heavy guest queries (sample queries, statistics, maps, embeddings, search, exports) run at most
  `FRIDGE_GUEST_CONCURRENCY` at a time per guest (default 2). Extra ones wait up to
  `FRIDGE_GUEST_QUEUE_WAIT_SECONDS` (default 10, then 503). Each SQL statement a guest runs is
  limited to `FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS` (default 30). These limits budget work; they
  never cut rows from a result.
- The job runner deletes demo accounts, with their saved views, one day after they were created,
  and removes expired sessions, every 5 minutes. Admins can run the same cleanup with
  `DELETE /api/auth/cleanup-demo-accounts`.

Guest account creation is limited:

| Setting | Default | When exceeded |
|---|---|---|
| `FRIDGE_DEMO_IP_HOURLY_LIMIT` | 5 new accounts per client IP per hour | 429 |
| `FRIDGE_DEMO_IP_LIVE_CAP` | 10 existing accounts per client IP | 429 |
| `FRIDGE_DEMO_ACCOUNT_CAP` | 2000 demo accounts in total | 503 |

`FRIDGE_DEMO_IP_ALLOWLIST` lists trusted IPv4/IPv6 addresses or CIDRs, separated by commas or
spaces, for example a company network that shares one public IP. Listed networks skip both per-IP
limits; the total cap still applies. Invalid entries, CIDRs with host bits set, and `/0` stop the
backend at startup. In the production stack nginx reads the same list and gives each signed-in
browser on those networks its own request budget. See "Organizations behind one IP" in
[Docker deployment](DOCKER_DEPLOYMENT.md). Anyone behind a listed address gets the exemption, so
list only networks you trust.

## Login throttle

After 10 failed logins for the same client IP and username within 5 minutes, `/api/auth/login`
answers 429 until the window has passed. A successful login clears the count. The count is kept
in each API worker process, so with several workers the effective limit is higher. In production,
nginx also rate-limits the login and guest-login routes.

The client IP is the address of the direct peer. The backend reads `X-Real-IP` or
`X-Forwarded-For` only when that peer is in `FRIDGE_TRUSTED_PROXIES`, a comma-separated list of
proxy CIDRs. It is empty by default; `docker-compose.prod.yml` sets the private ranges, because
nginx is the backend's only peer there.

## CSRF and allowed origins

`ALLOWED_ORIGINS` is a comma-separated list of exact browser origins (scheme, host and port), such
as `https://YOUR_DOMAIN`. It drives CORS and a CSRF check: a request that is not GET, HEAD,
OPTIONS or TRACE, carries the session cookie, and has an `Origin` header outside the list gets
403 `CSRF check failed: origin not allowed`. The backend logs the first blocked origin. Requests
with a Bearer token, and requests without an `Origin` header (the SDK, curl), are not checked.

The backend default is `http://localhost:5173,http://localhost:3000`. `scripts/run_backend.sh`
defaults to `http://localhost:${FRIDGE_FRONTEND_PORT:-3000}`. `docker-compose.prod.yml` requires
the variable.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `FRIDGE_ENV` | `development` (prod compose: `production`) | `production` or `prod` requires `FRIDGE_ADMIN_PASSWORD` and `STORAGE_ENCRYPTION_KEY` at startup and turns on public mode |
| `FRIDGE_ADMIN_PASSWORD` | unset | Replaces the seeded admin password on the first production start; see above |
| `STORAGE_ENCRYPTION_KEY` | built-in development key | Encrypts stored S3 credentials. Production refuses to start when it is unset or the default |
| `SESSION_TTL_HOURS` | `24` | Session idle timeout |
| `SESSION_COOKIE_SECURE` | `true` (`.env.example` and `run_backend.sh`: `false`) | Sends the cookie only over HTTPS. With `true` on plain HTTP, browsers on hosts other than `localhost`, and the SDK's `login()`, drop the cookie. Keep `true` in production |
| `ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000` | CORS and CSRF origins |
| `FRIDGE_TRUSTED_PROXIES` | empty | Proxies whose forwarding headers set the client IP |
| `DEMO_MODE` | `false` | Guest accounts and public mode |
| `FRIDGE_DEMO_*`, `FRIDGE_GUEST_*` | see above | Guest admission and query budgets; all numbers must be positive |

`.env.example` lists these with comments.

## Endpoints

| Endpoint | Method | Who | Purpose |
|---|---|---|---|
| `/api/auth/login` | POST | anyone | Password login; sets the session cookie |
| `/api/auth/logout` | POST | anyone | Ends the current session |
| `/api/auth/me` | GET | signed in | Current user and role |
| `/api/auth/change-password` | POST | signed in | Change your own password |
| `/api/auth/demo-login` | POST | anyone, demo mode only | Create a guest account and session |
| `/api/auth/cleanup-demo-accounts` | DELETE | admin | Delete day-old demo accounts and expired sessions |
| `/api/tokens` | POST | power_user, admin | Create a token |
| `/api/tokens` | GET | signed in | List your tokens (never their values) |
| `/api/tokens/{id}` | DELETE | owner | Revoke one of your tokens |
| `/api/workers/register` | POST | worker token of a power_user or admin | Register a compute worker |
| `/api/workers/{id}/heartbeat` | POST | its owner's worker token | Keep a compute worker online |
| `/api/workers/{id}/claim` | POST | its owner's worker token | Claim compute work (long poll; only datasets its owner can access) |
| `/api/workers/{id}/jobs/{job_id}/heartbeat`, `.../complete`, `.../fail`, `.../release` | POST | its owner's worker token, with the job's lease | Renew, finish, fail or give back a claimed job |
| `/api/workers/{id}/jobs/{job_id}/files/{name}` | GET, PUT | its owner's worker token, with the job's lease (`X-Fridge-Lease`) | Download a job's inputs, upload its outputs |
| `/api/workers` | GET | power_user (own workers), admin (all) | List compute workers and job runners |
| `/api/followers` | POST, GET | admin | Add a follower (its token is returned once); list followers and what they follow |
| `/api/followers/{id}` | DELETE | admin | Remove a follower and everything kept for it |
| `/api/followers/{id}/datasets/{dataset_id}` | PUT, DELETE | admin | Follow a dataset (`tags`, `views`: `primary` or `both`), or stop |
| `/api/follower/config` | GET | follower token | What this follower follows, and the primary's identity |
| `/api/follower/datasets/{id}/blocks`, `.../blocks/samples`, `.../vectors`, `.../state` | GET, POST | follower token | Copy a followed dataset |
| `/api/follower/datasets/{id}/changes`, `.../ack` | GET, POST | follower token | Read what changed since a cursor; acknowledge it |
| `/api/follower/datasets/{id}/coordinates` | POST | follower token | Copy a followed dataset's map coordinates |
| `/api/follower/datasets/{id}/tags`, `.../views`, `.../views/{view_id}` | POST, DELETE | follower token | Edits sent up, when the dataset's tags or views option is both ways |
| `/api/follower/datasets/{id}/fields` | POST | follower token | Report the patch scales the follower holds of a dataset, computed there |
| `/api/follower/searches`, `/api/follower/searches/{id}` | GET, POST | follower token | Claim a patch search the primary relays to the follower (long poll); answer it |
| `/api/primaries` | POST, GET | admin | On a follower: follow a primary (its follower token is checked first); list primaries and their mirrors |
| `/api/primaries/{id}` | DELETE | admin | Stop following a primary; its mirrors stay as read-only copies |
| `/api/primaries/mirrors/{id}/keep-separate` | POST | admin | Keep a stopped copy as it is: a new copy of its dataset makes a new dataset instead of resuming into it |
| `/api/compute-launcher` | GET, PUT | admin | On-demand compute workers: their settings (never the secret), and the machines started |
| `/api/compute-launcher/check` | POST | admin | Ask AWS whether it would start a machine with the saved settings (a dry run) |

## Troubleshooting

- **401 `Not authenticated`, `Invalid or expired session` or `Invalid or expired token`**: sign in
  again, or create a new token.
- **403 `Password change required before using the API`**: change the password in the UI or with
  `POST /api/auth/change-password`.
- **403 `Insufficient permissions`**: the action needs a higher role; see the table above.
- **403 `CSRF check failed: origin not allowed`**: add the exact origin the browser uses to
  `ALLOWED_ORIGINS` and restart the backend.
- **429 `Too many failed login attempts`**: wait 5 minutes.
- **SDK `RuntimeError: login succeeded but the server set Secure cookie(s)`**: use a token, or set
  `SESSION_COOKIE_SECURE=false` on a plain-HTTP development server.

See also: [Python SDK](SDK.md), [Docker deployment](DOCKER_DEPLOYMENT.md),
[Deploy to AWS](DEPLOY_TO_AWS.md).

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
`--password` to choose another one. It also reactivates the account, signs out its sessions and revokes its API
tokens, follower and worker tokens included: it prints how many, and followers that used one need a new token.
Add `--keep-tokens` to keep the tokens working.

## Roles

Each user has one role. Admins create users and set roles in **Settings → User Management**. New
users, and users whose password an admin resets, must choose a new password at their next login. A reset also
signs the user out and, with **Also revoke their API tokens** ticked (the default), revokes their API tokens, the
follower and worker tokens they issued included: a follower that connects with one stops syncing until it is given
a new token (Settings → Followers). Unticked, their tokens keep working. Deactivating an account (untick **Active
Account**) revokes its API tokens the same way, for good: reactivating it doesn't bring them back.

Changing your own password (**Change Password** in the user menu, or the change asked for at the first login)
signs out your other sessions. Tick **Also revoke my API tokens** (unticked by default) to revoke all of your
tokens too, follower and worker tokens included.

When one of these revokes tokens, it says how many, and how many of them were follower or worker tokens.

What a user may do in a dataset is their [access level](#access-levels) there. The role sets its bounds:

| Role | In a dataset | Also |
|---|---|---|
| guest | view at most, whatever its shares or the everyone level give | nothing more (demo guests are guests) |
| power_user | the level its shares, its groups' shares and the everyone level give | creates API tokens |
| admin | manage, in every dataset | creates API tokens, and does [what only an administrator does](#only-administrators) |

## Access levels

A dataset is shared with users and with groups, each share at one of four levels. Each level includes everything in
the levels before it:

| Level | Adds |
|---|---|
| **view** | Browse, filter, search, export, compare versions, save and share views |
| **tag** | Add and remove sample tags (the `tags` field only, and on a follower a mirror's own `@local.tags`), one sample at a time or across a selection, new tag names included |
| **edit** | Add, update and delete samples (one at a time, in bulk, and objects with no image); fields; embeddings, their maps and deleting them; hashes, concepts, quality scores and schedules; make versions and revert to them; the dataset's display settings (description, media fields, colours, the default view); change, share or delete anyone's public views in the dataset |
| **manage** | Who has access and at what level (users and groups, up to manage); whether the dataset is global, and its everyone level; its name; delete versions; delete the dataset |

Deleting a version needs manage, because a version is what brings deleted samples back. Deleting samples that no
version holds is edit, and can't be undone except from a backup. Nor can a revert's deletion of the samples added
since the version: its undo snapshot brings back their fields and tags, not their embeddings, map positions, hashes
or concept scores.

### A user's level in a dataset

- It is the highest of their own share, their groups' shares and, for a global dataset, its everyone level.
- A guest has view at most, whatever those give. An administrator has manage in every dataset.
- With none of them, the dataset isn't listed for the user, and its routes answer 403 `Access denied for this
  dataset`. An action above their level answers 403 `This needs <level> access to the dataset`.
- `GET /api/datasets` and `GET /api/datasets/{id}` answer the caller's level as `access_level`; the SDK reads it as
  `Dataset.access_level`.
- Creating a dataset gives its creator an edit share, which can be changed or removed like any other. Being a
  dataset's owner (`owner_id`, who created it) gives no access by itself.
- A [compute worker](SDK.md#compute-workers) claims a dataset's jobs, and keeps them, only while its owner has edit
  there: a job's result is a write to the dataset.

### Groups

Admins create groups and add and remove their members under **Settings → User Management** (`/api/groups`). A user
can be in several groups, and gets each group's level in the datasets shared with it. There are no group admins: a
team lead needs only manage in the team's datasets to share them. Deleting a group removes its shares, and deleting a
user removes them from their groups.

### Sharing a dataset

A new dataset is private: admins and the users and groups it is shared with see it. Someone with manage in the
dataset shares it in the Access section of **Dataset Settings**, from the SDK
([`Dataset.grant_access`](SDK.md#sharing-a-dataset)) or with `POST /api/datasets/{id}/access`, at any level up to
manage. A manager who isn't an admin chooses among the names of every active user and every group, and sees no
email addresses; followers' accounts and demo guests aren't offered.

A **global** dataset gives every signed-in user its **everyone level**: view, tag or edit (demo guests get view). A
manager makes a dataset global or private again, and sets its everyone level, in Dataset Settings, or with
`Dataset.set_everyone_level(level)` (`None` makes it private). A share can give more than the everyone level, and
keeps giving access when the dataset is made private again. A dataset made global starts at view, until a manager
raises it: `Dataset(name, is_global=True)` creates a dataset that is global at view, and
`Dataset(name, is_global=True, everyone_level="edit")` one that every power user can edit; for a dataset that
exists, `set_everyone_level("edit")` raises it. Datasets already global when the server was upgraded to access levels
kept edit.

### Only administrators

Whatever their levels, only an administrator can:

- create a dataset;
- choose a dataset's storage configuration (`storage_config_id`, a manager's `PATCH /api/datasets/{id}` included), and
  manage storage configurations;
- import patch vectors from S3 (`patches/import`);
- run the index advisor: its reviews (a dataset's too, and review schedules), and removing or restoring an index;
- manage users, and groups and their members;
- manage followers, primaries, the compute launcher, backups and system status;
- refresh every dataset's stale statistics at once (`POST /api/datasets/refresh-stats`);
- stop, resume or discard a patch-vector import, a mirror's sync, or a job that belongs to no dataset.

### Stopping jobs

A job of a dataset is stopped, resumed or discarded (Background jobs, or `POST /api/jobs/{id}/cancel`, `.../resume`,
`.../discard`) by:

- an administrator, or anyone with manage in the dataset;
- the user who started it, while they still have the level it needs: tag for adding or removing tags across samples,
  manage for deleting the dataset, edit for the others;
- for a map re-fit, anyone with edit in the dataset, as they can start one.

A patch-vector import and a mirror's sync are an administrator's alone: a manager of the dataset can't stop, resume or
discard them.

### Files are not separated between teams

Levels decide who sees and changes a dataset. They don't separate the files its samples point at: the server serves a
sample's file to anyone who can see the sample's dataset, from local disk or from S3, and checks only that the
dataset may read where the file is.

- **A bucket per team is separated.** A dataset reads a bucket through any storage configuration that covers the
  bucket, by its bucket name or one of its bucket patterns, and that the dataset may use: one serving all datasets,
  or one limited to chosen datasets that include it ([Media storage](S3_PERMISSIONS.md#s3)). To keep a team's bucket
  to that team, an administrator gives it a storage configuration limited to the team's datasets (**Only these
  datasets** in Settings → Storage Configuration). Every other dataset is then refused that bucket while no other
  configuration covers it, by name or pattern, unless that one is also limited to the same team's datasets; and
  while it isn't the environment configuration's `S3_BUCKET`, which serves every dataset that has no storage
  configuration of its own when no default configuration exists. Configurations are chosen by bucket name and
  pattern only, so give each one credentials that reach only its own team's buckets. A public bucket is readable by
  anyone, whatever the levels.
- **Local files and a shared bucket are not separated.** Files under `FRIDGE_DATA_ROOT`, and a bucket several teams
  share under different prefixes, can be reached from any dataset that can read that root or bucket: someone with
  edit in one dataset can add a sample that points at another team's file under the same root or in the same bucket,
  and see it. Isolation per prefix or per folder doesn't exist yet. Teams that must not see each other's files need
  a bucket each.

### Followers

Shares, groups and everyone levels belong to each instance, and none of them is synced to a
[follower](FOLLOWERS.md). A mirror on a follower is private there, and is shared on the follower like any of its
datasets.

## API tokens

A token authenticates as the user who created it, with that user's current role, groups and levels. Send it as
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
  (`client.list_tokens()`, `GET /api/tokens`). An admin can revoke all of another user's tokens with
  `DELETE /api/users/{id}/tokens`.
- `POST /api/users/{id}/reset-password` (`{"password": ..., "revoke_tokens": true}`) revokes the user's tokens
  unless `revoke_tokens` is `false`; without the field it revokes them. `POST /api/auth/change-password` revokes
  your own tokens only with `"revoke_tokens": true`. Both answer `revoked_tokens` with how many they revoked of
  each scope (`full`, `follower`, `worker`), all 0 when not asked to revoke.
- Deactivating a user revokes all of their tokens, follower and worker tokens included, in the same step;
  reactivating the account doesn't bring them back. `PATCH /api/users/{id}` with `"is_active": false` answers
  `revoked_tokens` with how many it revoked of each scope (`full`, `follower`, `worker`).
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
| `/api/users/{id}/tokens` | DELETE | admin | Revoke all of a user's tokens |
| `/api/groups` | GET, POST | admin | List groups with their members; create a group |
| `/api/groups/{id}` | PATCH, DELETE | admin | Rename a group; delete it with its shares |
| `/api/groups/{id}/members/{user_id}` | PUT, DELETE | admin | Add a member; remove one |
| `/api/datasets/{id}/access` | GET, POST | manage | The dataset's shares and everyone level; share it with a user (`user_id`) or a group (`group`, its name) at a `level` |
| `/api/datasets/{id}/access/{user_id}`, `/api/datasets/{id}/access/groups/{group_id}` | DELETE | manage | Remove a user's or a group's share |
| `/api/datasets/{id}/access/candidates` | GET | manage | The users and groups it can be shared with, by name |
| `/api/datasets/{id}` | PATCH | edit; manage for `name`, `is_global` and `everyone_level`; admin for `storage_config_id` | Change the dataset's settings |
| `/api/workers/register` | POST | worker token of a power_user or admin | Register a compute worker |
| `/api/workers/{id}/heartbeat` | POST | its owner's worker token | Keep a compute worker online |
| `/api/workers/{id}/claim` | POST | its owner's worker token | Claim compute work (long poll; only datasets its owner has edit in) |
| `/api/workers/{id}/jobs/{job_id}/heartbeat`, `.../complete`, `.../fail`, `.../release` | POST | its owner's worker token, with the job's lease (heartbeat and complete: while its owner has edit in the job's dataset) | Renew, finish, fail or give back a claimed job |
| `/api/workers/{id}/jobs/{job_id}/files/{name}` | GET, PUT | its owner's worker token, with the job's lease (`X-Fridge-Lease`), while its owner has edit in the job's dataset | Download a job's inputs, upload its outputs |
| `/api/workers` | GET | power_user (own workers), admin (all) | List compute workers and job runners |
| `/api/followers` | POST, GET | admin | Add a follower (its token is returned once); list followers and what they follow |
| `/api/followers/{id}` | DELETE | admin | Remove a follower and everything kept for it |
| `/api/followers/{id}/datasets/{dataset_id}` | PUT, DELETE | admin | Follow a dataset (`tags`, `views`: `primary` or `both`), or stop |
| `/api/follower/config` | GET | follower token | What this follower follows, and the primary's identity |
| `/api/follower/datasets/{id}/blocks`, `.../blocks/samples`, `.../vectors`, `.../state` | GET, POST | follower token | Copy a followed dataset |
| `/api/follower/datasets/{id}/match-keys` | GET | follower token | Match a held copy with a restored primary: its samples' ids, file paths and image hashes, a page at a time |
| `/api/follower/datasets/{id}/changes`, `.../ack` | GET, POST | follower token | Read what changed since a cursor; acknowledge it |
| `/api/follower/datasets/{id}/coordinates` | POST | follower token | Copy a followed dataset's map coordinates |
| `/api/follower/datasets/{id}/tags`, `.../views`, `.../views/{view_id}` | POST, DELETE | follower token | Edits sent up, when the dataset's tags or views option is both ways |
| `/api/follower/datasets/{id}/fields` | POST | follower token | Report the patch scales the follower holds of a dataset, computed there |
| `/api/follower/searches`, `/api/follower/searches/{id}` | GET, POST | follower token | Claim a patch search the primary relays to the follower (long poll); answer it |
| `/api/follower/leave`, `/api/follower/return` | POST | follower token | The follower stopped following: its subscriptions move aside and the primary stops logging for it; it added the primary again: they come back ([Followers](FOLLOWERS.md#when-it-stops)) |
| `/api/primaries` | POST, GET | admin | On a follower: follow a primary (its follower token is checked first); list primaries and their mirrors |
| `/api/primaries/{id}` | DELETE | admin | Stop following a primary; its mirrors stay as read-only copies |
| `/api/primaries/mirrors/{id}/keep-separate` | POST | admin | Keep a stopped copy as it is: a new copy of its dataset makes a new dataset instead of resuming into it |
| `/api/primaries/mirrors/{id}/restore-decision` | POST | admin | Answer a copy held after its primary was restored: `resume` into it, or make a `new` copy |
| `/api/compute-launcher` | GET, PUT | admin | On-demand compute workers: their settings (never the secret), and the machines started |
| `/api/compute-launcher/check` | POST | admin | Ask AWS whether it would start a machine with the saved settings (a dry run) |

## Troubleshooting

- **401 `Not authenticated`, `Invalid or expired session` or `Invalid or expired token`**: sign in
  again, or create a new token.
- **403 `Password change required before using the API`**: change the password in the UI or with
  `POST /api/auth/change-password`.
- **403 `This needs <level> access to the dataset`**: ask someone with manage in the dataset, or an admin, for that
  level; see [Access levels](#access-levels). A guest can't be given more than view.
- **403 `Access denied for this dataset`**: no share, group or everyone level gives you the dataset.
- **403 `Insufficient permissions`**: the action needs a higher role; see [Roles](#roles).
- **403 `CSRF check failed: origin not allowed`**: add the exact origin the browser uses to
  `ALLOWED_ORIGINS` and restart the backend.
- **429 `Too many failed login attempts`**: wait 5 minutes.
- **SDK `RuntimeError: login succeeded but the server set Secure cookie(s)`**: use a token, or set
  `SESSION_COOKIE_SECURE=false` on a plain-HTTP development server.

See also: [Python SDK](SDK.md), [Docker deployment](DOCKER_DEPLOYMENT.md),
[Deploy to AWS](DEPLOY_TO_AWS.md).

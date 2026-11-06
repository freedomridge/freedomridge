# Docker deployment

Local development uses `docker-compose.yml` and `scripts/run_backend.sh` with plain
HTTP; see the [quick start](QUICK_START.md). **All commands here select
`docker-compose.prod.yml` explicitly.** Replace `YOUR_DOMAIN` below with the hostname
you will serve.
The complete AWS workflow is [Deploy to AWS](DEPLOY_TO_AWS.md), including demo
imports, patch embeddings, guest checks, backup recovery and teardown.

## Production prerequisites and environment

Use Docker with Compose v2, a public DNS hostname, inbound 80/443 and outbound
access for images, ACME and model weights. Plan for at least 8 GiB of RAM: the
backend alone uses about 3.4 GiB with search warm, so 16 GiB gives headroom. Larger
datasets need more; see [Sizing](#sizing). The full
four-dataset CPU import takes a day or more on a small host; see the import plan in
the AWS guide. The four-dataset demo needs about 40 GiB of OS/build space, 300 GiB
of data and 30 GiB of cache; see the AWS guide's assumptions. CPU-only torch and
transformers are installed in the backend image.

Deploy a commit you have reviewed. On AWS, user-data generates `.env`; **do not
overwrite it**. For a new non-AWS installation, create a production-only file (not
the HTTP development template):

```bash
umask 077
test ! -e .env && test ! -L .env || { echo "Preserve the existing .env"; exit 1; }
cat > .env <<ENV
COMPOSE_PROJECT_NAME=freedomridge-prod
FRIDGE_ENV=production
POSTGRES_PASSWORD=$(openssl rand -hex 24)
STORAGE_ENCRYPTION_KEY=$(openssl rand -hex 32)
FRIDGE_ADMIN_PASSWORD=$(openssl rand -hex 24)
DOMAIN_NAME=YOUR_DOMAIN
LETSENCRYPT_EMAIL=you@example.com
ALLOWED_ORIGINS=https://YOUR_DOMAIN
SESSION_COOKIE_SECURE=true
DEMO_MODE=true
NEXT_PUBLIC_MAPBOX_TOKEN=YOUR_PUBLIC_MAPBOX_TOKEN
DATA_VOLUME_PATH=/srv/freedomridge/datasets
FRIDGE_DATA_ROOT=/srv/freedomridge/datasets
ENV
chmod 600 .env
# Owned by you, so the downloader and importers can write archives, crops and caches.
sudo install -d -o "$USER" -g "$USER" /srv/freedomridge/datasets
```

Replace `YOUR_DOMAIN`, the email and the Mapbox token, and adjust the data path if
needed. In your Mapbox account, restrict the token's allowed URLs to
`https://YOUR_DOMAIN`: it is compiled into browser JavaScript. Keep DB/backups outside the media
root. Host imports store absolute paths: set `DATA_VOLUME_PATH` and
`FRIDGE_DATA_ROOT` to the same absolute directory; backend and job-runner mount it
at that identical path. `HOST_DATA_ROOT` does not translate sample paths. Defaults use project-scoped Docker volumes for PostgreSQL, cache and dumps.
AWS instead sets `POSTGRES_VOLUME_PATH`, `CACHE_VOLUME_PATH`, `BACKUP_VOLUME_PATH`
and `TLS_STATE_PATH` to the provisioned EBS directories. Do not change paths or
`COMPOSE_PROJECT_NAME` on an existing installation without migrating data first.
The backend runs as uid 1000; custom cache bind mounts need writable `s3`, `crop`,
`composite` and `huggingface` subdirectories owned by that uid. Never put backups
or secrets inside `DATA_VOLUME_PATH`.

## Sizing

The 8 GiB minimum covers the application and the four-dataset demo. Beyond that, the database
grows with samples, embedding vectors and their search (HNSW) indexes. These figures were measured
on a development database with 4.5 million samples and 9.7 million vectors (PostgreSQL 18,
pgvector 0.8):

| Per item | Disk |
|---|---|
| Sample, with its metadata and indexes | about 4–6 KB (more with large metadata) |
| Stored vector, 512 dimensions | about 2.2 KB |
| Stored vector, 1024 dimensions | about 4.2 KB |
| Search index entry, 512 dimensions | about 2.7 KB |
| Search index entry, 1024 dimensions | about 8.1 KB (one entry per 8 KB page) |

For example, 3.8 million samples with a 512-dimension CLIP field and a 1024-dimension DINOv3
field, both indexed, take about 22 GB of samples, 24 GB of vectors and 41 GB of indexes: roughly
90 GB of database before map coordinates, backups and the image cache.

**RAM** is the 8 GiB minimum plus room for the search indexes you use most. Search reads only a
small part of an index, so a single search works from SSD. Jobs that search once per sample, such
as `compute_uniqueness` and duplicate detection, are much faster when the field's index fits in
memory: about 10 GB for the CLIP field above and 31 GB for the DINOv3 one.

**The job runner** is one container that runs up to `JOB_WORKER_SLOTS` jobs at once (default 3),
sharing the host's memory with PostgreSQL and the backend. Bulk edits and deletions work in
chunks of 1,000 samples; each vector search-index (HNSW) build uses 256 MB of PostgreSQL
maintenance memory.
Server-side map re-fits run one at a time in a memory-capped process that leaves 20% of the
host's RAM free. A re-fit holds every vector of the field plus its fit sample: about
`vectors × dimensions × 4` bytes + `dimensions × 3.5 MB` + 1 GiB, or about 20 GiB for the
DINOv3 field above. There is no systemd in the job-runner container, so the fit runs within the
container's own memory limit, `JOB_RUNNER_MEMORY_LIMIT` in `.env` (for example `24g`), which every
job in that container shares. The fit is sized to what the limit leaves; if it still runs out, the
kernel stops only the fit, and the runner retries it with fewer points. Exports and models go to the
`umap_models` volume. Unset (the default), there is no limit and the runner skips re-fits with
"Memory containment is not available"; build maps with the SDK instead (`compute_visualization`).

**Heavy computation runs where the SDK runs.** Embeddings, image metrics and maps are computed by
the SDK on the machine that calls it, which only uploads the results. Run those from a machine
with the RAM, CPU or GPU for them; the server does not need to be sized for that work. A separate
compute worker that claims jobs from the server is designed but not built yet (see
[Jobs](architecture/JOBS.md)).

## TLS, startup and admin login

Point the domain's A record at the host; remove incorrect AAAA records. The stack
serves only HTTPS, even for the first login.

```bash
docker compose -f docker-compose.prod.yml config -q
bash ops/tls/bootstrap.sh
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Bootstrap runs a challenge-only nginx config with no certificates or upstreams,
then certbot uses the shared HTTP webroot to obtain the first certificate. The
normal stack serves HTTPS, redirects HTTP except ACME, rejects unknown Hosts with
444, and sends HSTS/security headers. CSP is Report-Only; follow the AWS guide
before enforcing it because Next.js uses inline scripts. All API/media access
goes through nginx; DB/backend/frontend have no published ports.

Log in as `admin` at the HTTPS domain with the generated `FRIDGE_ADMIN_PASSWORD`
from `.env`, then follow any password-change prompt. Do not use a default password.
Read the AWS guide's public-demo checks: private guest views return 200; admin-only
writes and demo-account password changes return 403 before
sharing the URL (`bash ops/smoke/guest_gate.sh https://YOUR_DOMAIN DATASET_ID` checks
them). `DEMO_MODE=true` enables guest accounts for a public demo.

Install renewal on a systemd host (edit the service paths if the checkout is not
`/home/ubuntu/app`):

```bash
sudo install -m 644 ops/systemd/freedomridge-cert-renew.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now freedomridge-cert-renew.timer
docker compose -f docker-compose.prod.yml run --rm --no-deps certbot \
  renew --dry-run --no-random-sleep-on-renew --webroot -w /var/www/certbot
sudo systemctl start freedomridge-cert-renew.service
```

On a host without systemd, schedule `bash /ABSOLUTE/CHECKOUT/ops/tls/renew.sh` every
12 hours as an account allowed to use Docker, and monitor failures. The script
reloads nginx after a successful renewal check; never run two schedulers. The
whole Let's Encrypt state directory is mounted, so certificate symlinks survive.

## Organizations behind one IP

By default, guest admission allows 5 new accounts per IP per hour and 10 live
accounts per IP (`FRIDGE_DEMO_IP_HOURLY_LIMIT` / `FRIDGE_DEMO_IP_LIVE_CAP`). Set
`FRIDGE_DEMO_IP_ALLOWLIST` in `.env` to trusted IPv4/IPv6 addresses or CIDRs,
separated by commas or whitespace, for example `203.0.113.0/24,2001:db8::/32`
(documentation ranges; replace with actual trusted networks). Empty means no exemptions;
invalid entries, CIDRs with host bits set, and `/0` are rejected.

Listed networks skip both per-IP guest-account limits. nginx request and connection
budgets become per signed-in browser (IP plus `session_id` cookie); requests without
a session cookie still share the IP's budget. The global `FRIDGE_DEMO_ACCOUNT_CAP`
(default 2000) still applies to everyone. Anyone behind a listed IP gets the exemption,
so list only networks you trust.

Apply changes to both services:

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate backend nginx
```

## Services and persistent state

| Service | Responsibility |
|---|---|
| nginx | TLS, request/connection limits, body limits, proxy timeouts |
| frontend | Next.js production server; Mapbox token baked at build time |
| backend | API, media access checks, lazy CPU CLIP text/image inference |
| db | PostgreSQL 18, PostGIS and pgvector |
| worker | Hourly pruning of media caches only |
| job-runner | Background jobs (index builds, statistics, placing new points on maps, map re-fits where supported, schedules) and expired demo-account cleanup; see [Sizing](#sizing) |
| backup | Daily logical dumps, two local copies, optional S3 copy |
| certbot | On-demand certificate issuance/renewal; `tls` profile |

`HF_HOME=/cache/huggingface` keeps weights across backend recreation; the prune
worker excludes that directory. Media is authorized by the backend on each request
and uses its persistent render caches. nginx does not cache authorization responses.
The production PostgreSQL defaults are conservative for an 8 GiB host; monitor RAM
and adjust `POSTGRES_*` settings deliberately as load changes.

For a local **production TLS rehearsal**, use a separate project and data directory,
set `HTTP_BIND=127.0.0.1`, `HTTPS_BIND=127.0.0.1`, `HTTP_PORT=18080`,
`HTTPS_PORT=18443`, and put a self-signed certificate at
`$TLS_STATE_PATH/letsencrypt/live/$DOMAIN_NAME/fullchain.pem` and `privkey.pem`
in that same directory. Use curl with
`--resolve` and `-k` only for that rehearsal. Normal production always uses the CA
certificate. The HTTP redirect intentionally targets canonical HTTPS port 443.

## Demo data and indexes

To load the hosted-demo datasets, follow the hosted-demo path in
[examples/README.md](../examples/README.md#hosted-demo-path) on this host. Export
`FRIDGE_DATA_ROOT` with the same absolute path as in `.env` and
`FRIDGE_API_URL=https://YOUR_DOMAIN/api`. Run the KITTI GPS step before any KITTI
import. Afterwards build the search and map indexes as in
[Deploy to AWS, section 9](DEPLOY_TO_AWS.md#build-the-search-and-map-indexes).

## Backups, updates and troubleshooting

Set `BACKUP_S3_URI=s3://YOUR_PRIVATE_BUCKET/database` and provide an instance role
on AWS. The Terraform module creates it. The application Backups card is a separate
scheduler and is not wired to the sidecar; inspect sidecar logs and backup objects.
Dumps do not include media or `.env`; preserve both separately. Restore into an
empty DB with clients stopped, `psql -X -v ON_ERROR_STOP=1`, then ANALYZE and apply
migrations before starting clients. Use the exact commands in the
[AWS recovery procedure](DEPLOY_TO_AWS.md#12-backups-and-recovery) and the
[backup guide](BACKUP_AND_MIGRATION.md).

```bash
docker compose -f docker-compose.prod.yml logs --tail=100 backend nginx backup
docker compose -f docker-compose.prod.yml exec db pg_isready -U freedomridge
docker compose -f docker-compose.prod.yml exec nginx nginx -t
# After backing up and checking out a reviewed revision:
docker compose -f docker-compose.prod.yml build --pull
FRIDGE_DB_CONTAINER=$(docker compose -f docker-compose.prod.yml ps -q db) \
  bash scripts/apply_migrations.sh
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload
```

Rebuild the frontend when changing the Mapbox token. nginx uses Docker DNS with
a 10-second cache for recreated services; allow health checks and DNS expiry.
Editing `nginx.conf` requires `docker compose -f docker-compose.prod.yml up -d
--no-deps --force-recreate nginx` to render the template. Included `ops/nginx/*.conf`
files need `nginx -t` and a reload only. The renewal timer supplies jitter; certbot
random sleep is disabled so a manual renewal check does not appear hung. Check disk usage and cache
budgets before deleting anything; never use volume pruning to recover space.
For shutdown use `docker compose -f docker-compose.prod.yml down` without `-v`.
Existing volume/bind data survives container recreation. This single-host setup
does not provide automatic failover or a guaranteed user capacity; measure before
adding replicas or promising a traffic level.

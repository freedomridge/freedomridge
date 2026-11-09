# Deploy the hosted demo to AWS

This guide deploys one EC2 instance using `ops/terraform` and
`docker-compose.prod.yml`. Replace **`YOUR_DOMAIN`** everywhere below with the
hostname you will serve, for example `demo.example.com`. The public origin is then
`https://YOUR_DOMAIN`. (The project's own demo runs at `freedomridge.app`.)
The stack serves only HTTPS; there is no HTTP login step. Under an HSTS-preloaded
TLD such as `.dev`, browsers also require a valid certificate from the first visit.
Local development uses `docker-compose.yml` and `scripts/run_backend.sh` over HTTP;
see the [quick start](QUICK_START.md).

It runs on Arm (Graviton) or x86_64: the release images are built for both. **Arm is the default and the
recommendation**: `t4g.large` costs about 19% less than `t3.large` for the same 8 GiB, and in our test the
demo's searches ran as fast. An x86_64 deployment can move to Arm (section 13).

The backend enforces the public-demo protections: the `FRIDGE_ADMIN_PASSWORD` admin
bootstrap, guests limited to browsing and private saved views, and removal of expired
demo accounts. The smoke tests in section 11 are a release gate: run them before
inviting visitors.

## Checklist

1. Prepare the prerequisites, including a Mapbox token restricted to
   `https://YOUR_DOMAIN` (section 1).
2. Choose the data-volume size now. EBS volumes can grow but never shrink (section 1).
3. Create the Terraform state bucket, write `terraform.tfvars`, run `terraform apply`, and
   confirm the SNS email (section 2).
4. SSH in and wait for cloud-init (section 3).
5. Review `.env` and set `NEXT_PUBLIC_MAPBOX_TOKEN` (section 4).
6. Point the A record at the Elastic IP and run `ops/tls/bootstrap.sh` (section 5).
7. Start the stack and enable certificate renewal (section 6).
8. Log in as `admin` and create an API token (section 7).
9. Download and import the four datasets, on the instance or from a workstation into S3 (section 8).
10. Compute the 4×4 patches and build the search and map indexes (section 9).
11. Open each hosted dataset to guests, then check guest behaviour and data credits (section 10).
12. Run the smoke tests, including `ops/smoke/guest_gate.sh` (section 11).
13. Confirm an S3 backup, the SNS alarms and disk headroom (sections 12 and 13).
14. Revoke the import token, then invite visitors.

## 1. Prerequisites

- An AWS account, CLI credentials, Terraform >= 1.5, and permission to create EC2,
  EBS, IAM, S3, CloudWatch, SNS and Budgets resources. Run Terraform on your workstation.
  This module expects a default VPC with a public default subnet and Internet gateway.
- An EC2 SSH key pair in the selected region, its private key, and your current
  **public IPv4 address/32** for SSH. The module has no world-open SSH default.
- Control of DNS for `YOUR_DOMAIN`. Lower the old A record TTL a day ahead;
  keep it until ready to issue the certificate. Remove stale AAAA records unless
  IPv6 actually reaches this host. Only `YOUR_DOMAIN` itself is served: other names
  such as `www.YOUR_DOMAIN` are not in the certificate and are rejected.
- The repository to deploy and a reviewed commit SHA. The Terraform default is
  `https://github.com/freedomridge/freedomridge`; set your fork's HTTPS URL instead
  if you deploy a fork. Public cloning needs no credentials. For a private fork, use
  a fine-grained token restricted to that repository with **Contents: read-only**,
  and follow the token-file/retry procedure in section 3. Never embed a token in
  the URL, tfvars, user-data, or a command argument.
- A Mapbox public `pk.` token for `NEXT_PUBLIC_MAPBOX_TOKEN`. Browsers receive it,
  so it is not a secret, but anyone can copy it. In your Mapbox account, **restrict
  the token's allowed URLs to `https://YOUR_DOMAIN`** before you start the
  frontend. If the same token is used in CI, the Playwright traces of failed
  end-to-end runs also contain it.
- An email address for certificate notices, AWS Budget alerts and SNS notifications.
  Confirm the SNS subscription email after applying.
- A choice of where to import (section 8): on the instance, with the media on its data
  disk, or from a workstation, with the media in S3. A workstation needs a checkout of
  the same reviewed commit, disk space for the downloads, ideally an NVIDIA GPU, and an
  AWS login that may write to the media bucket Terraform creates.
- A size for the data disk. The default allocation is 40 GiB root, 300 GiB data and
  30 GiB cache. With the media on the instance the full demo needs at least 150 GiB;
  with the media in S3, 50–100 GiB (section 13). **EBS volumes can grow but cannot
  shrink**, so choose `data_volume_gib` before the first apply.
- A budget. In us-east-1 at on-demand prices, the default instance and disks cost
  about **$82/month**: t4g.large (Graviton) is 730 h × $0.0672 = $49.06, 370 GiB of gp3 is
  370 × $0.08 = $29.60, and one public IPv4 address is 730 h × $0.005 = $3.65.
  S3 dumps, CloudWatch metrics and alarms, Internet egress, temporary import
  instances and CPU-credit charges come on top, under the **$100/month** budget alert
  set below. The x86_64 t3.large costs $60.74/month instead, and a t4g.xlarge (16 GiB)
  about $98/month for the instance alone. Check your region's prices in the
  [AWS calculator](https://calculator.aws/).

Full CPU imports and patch generation are substantial jobs. Run them in `tmux`
or a persistent SSH session. The full selection is about **1.24 million CLIP
encodings** and is not a safe overnight job on a t4g.large. Either import COCO and
KITTI first (the smaller first launch in section 8), or use a temporary
compute-optimized instance for the import window.

## 2. Terraform

### Keep the state in S3 (recommended)

Terraform records what it created in a state file, by default
`ops/terraform/terraform.tfstate` on your workstation. If that file is lost, Terraform
no longer knows about the instance, disks or buckets, and each must be imported again
by hand. Keep the state in its own bucket instead. Create it once per account, outside
this module, so `terraform destroy` can never delete it. Versioning keeps every earlier
state, and the lock file (Terraform 1.10 or later) refuses a second plan or apply while
one is running. The instance role has no access to this bucket. From the checkout root,
with your own AWS login:

```bash
STATE_BUCKET="freedomridge-tfstate-$(aws sts get-caller-identity --query Account --output text)"
aws s3api create-bucket --bucket "$STATE_BUCKET" --region us-east-1
aws s3api put-public-access-block --bucket "$STATE_BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-policy --bucket "$STATE_BUCKET" --policy "$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny",
"Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::$STATE_BUCKET","arn:aws:s3:::$STATE_BUCKET/*"],
"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
)"
cat > ops/terraform/backend.tf <<HCL
terraform {
  backend "s3" {
    bucket       = "$STATE_BUCKET"
    key          = "demo/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
HCL
```

In another region, add `--create-bucket-configuration LocationConstraint=YOUR_REGION`
to `create-bucket` and use that region in `backend.tf`. Git ignores `backend.tf`, like
`terraform.tfvars`. The `terraform init` below then keeps the state only in the bucket.
If an interrupted apply (for example, an expired AWS login) leaves the lock behind,
make sure no other Terraform run is active, then run `terraform force-unlock LOCK_ID`
with the ID from the error. Earlier states are listed by
`aws s3api list-object-versions --bucket "$STATE_BUCKET" --prefix demo/`.

### Create the stack

From your workstation's reviewed checkout:

```bash
cd ops/terraform
cat > terraform.tfvars <<'VARS'
aws_region         = "us-east-1"
key_name           = "YOUR_EXISTING_EC2_KEY"
allowed_ssh_cidr   = "YOUR_PUBLIC_IPV4/32"
repository_url     = "https://github.com/freedomridge/freedomridge"
repository_ref     = "YOUR_REVIEWED_COMMIT_SHA"
public_origin      = "https://YOUR_DOMAIN"
budget_email       = "you@example.com"
monthly_budget_usd = 100
instance_type      = "t4g.large"
data_volume_gib    = 300
cache_volume_gib   = 30
# Private repo only: this is a path ON THE INSTANCE, not a token.
# repository_token_file = "/root/fridge-repository-token"
VARS
terraform init
terraform fmt -check
terraform validate
terraform plan -out=demo.tfplan
terraform apply demo.tfplan
terraform output
```

`public_origin` has no default: Terraform requires it. Keep Terraform state private
and backed up (the state bucket above does both); do not commit state, plans or tfvars.
The module creates an Elastic IP (stable across stop/start), encrypted gp3 disks,
private versioned backup and media buckets, an instance role, CPU/disk alarms, and an
account-wide monthly budget. It resolves Canonical's current Ubuntu 26.04 LTS AMI for the
instance type's architecture, Arm (Graviton, such as t4g or c7g) or x86_64 (a Haswell-class
or newer family, such as t3 or c7i), from Canonical's public SSM parameters; the release
images are built for both. It also picks a default subnet in an AZ offering `instance_type`;
`availability_zone` can select a specific supported AZ. After first deploy, pin that AZ from the
`availability_zone` output in tfvars. The instance ignores later `ami`, `user_data`,
`subnet_id` and `instance_type` drift; EBS AZs are also held stable. An instance
`prevent_destroy` guard refuses replacement. Routine applies therefore cannot
roll the AMI/bootstrap or resize the instance. OS/image upgrades and deliberate
replacement need a maintenance procedure and backups; never remove the guard
just to make an unexplained plan succeed. Resize using section 8, not Terraform.

Only TCP 80/443 and your SSH CIDR are admitted. PostgreSQL, FastAPI and Next.js have
**no host port mappings**. Port 80 serves ACME challenges and a fixed-host HTTPS
redirect. Unknown Host/bare-IP requests receive nginx 444 (an empty connection).
IMDSv2 is required; hop limit 2 lets the backup container use the instance role.
No long-lived AWS keys are written into `.env`. All containers on this single host
remain within the instance-role trust boundary: any container can upload and list
dumps, list the media bucket and read its `media/` prefix, and nothing else. The role
cannot download dumps, read archives or write media. Restores and imports to S3 use your own credentials. Container-specific IMDS isolation is
not provided by this Compose stack. SSM access is not provisioned.
Docker can bypass ordinary UFW forwarding rules; use the security group as the
network boundary and publish only nginx.

## 3. What first boot does

```bash
ssh -i YOUR_KEY.pem ubuntu@ELASTIC_IP
sudo cloud-init status --wait
sudo tail -100 /var/log/cloud-init-output.log
# If this SSH session predates cloud-init, log out and reconnect for Docker group access.
exit
ssh -i YOUR_KEY.pem ubuntu@ELASTIC_IP
cd /home/ubuntu/app
```

`user_data.sh` first clones the reviewed revision, then waits for EBS attachments, identifies them by volume ID, formats
only blank disks, mounts `/data` and `/cache` by UUID, and makes Docker depend on
those mounts. It installs Docker/Compose, Python venv support, unattended security
updates and the CloudWatch agent, checks out the selected revision, creates the
storage directories and generates `/data/secrets/.env` with mode **600** owned by
`ubuntu`, symlinked into `/home/ubuntu/app/.env`. Existing secrets are reused. The
CloudWatch package signature and published signing-key fingerprint are verified
before installation ([AWS verification procedure](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/verify-CloudWatch-Agent-Package-Signature.html)).
It installs, but does not enable, the certificate renewal timer.

**It does not start the app.** Complete the configuration and certificate steps
first. If cloning fails, bootstrap stops **before mounting/formatting data disks or
generating secrets**, with a clear message. Failed clone staging directories under
`/home/ubuntu/.app-bootstrap.*` are harmless and contain no token. A retry skips an
existing checkout (requiring the same reviewed ref), checks existing mounts, and
reuses secrets. It never reformats an existing filesystem or resets the checkout.
If PostgreSQL already exists without secrets, it refuses to invent new credentials.

For a private repo, first boot will stop until the token file exists. After fixing
repository access, SSH in and run this as `ubuntu` (the token is read silently;
it is not a command argument or shell history entry):

```bash
sudo install -m 600 /dev/null /root/fridge-repository-token
read -rsp 'Read-only GitHub token: ' REPO_TOKEN; echo
printf '%s' "$REPO_TOKEN" | sudo tee /root/fridge-repository-token >/dev/null
unset REPO_TOKEN
sudo env REPOSITORY_TOKEN_FILE=/root/fridge-repository-token \
  bash /var/lib/cloud/instance/scripts/part-001
# Remove the credential when bootstrap succeeds; git does not persist it.
sudo rm -f /root/fridge-repository-token
```

For public repositories, fix the URL/ref in the rendered script if needed, then
run `sudo bash /var/lib/cloud/instance/scripts/part-001`. For a private retry after
a wrong URL/ref, fix those base64 assignments in that script too (or render fresh
user-data from the reviewed module); changing tfvars alone does not re-run cloud-init.
After success, reconnect for Docker group membership and resume section 4.
Do not use `cloud-init clean` or reformat disks as a retry mechanism. After EBS expansion, grow the ext4 filesystem with
`resize2fs` on the verified device; increasing the Terraform size alone does not
grow an existing filesystem.

## 4. Review `.env`

```bash
cd /home/ubuntu/app
stat -Lc '%a %U %n' .env
nano .env
```

The generated values include:

| Setting | Production value/purpose |
|---|---|
| `FRIDGE_ENV` | `production`; do not weaken the startup gate |
| `STORAGE_ENCRYPTION_KEY` | Random 32-byte hex secret; preserve it with backups |
| `POSTGRES_PASSWORD` | Random 24-byte hex secret; changing `.env` alone does not change an initialized DB password |
| `FRIDGE_ADMIN_PASSWORD` | Random 24-byte hex secret used to bootstrap the seeded admin |
| `FRIDGE_DEMO_*`, `FRIDGE_GUEST_*`, `FRIDGE_DB_ACQUIRE_TIMEOUT_SECONDS` | Optional public-demo limits (demo-account caps per IP and in total, guest query timeout and concurrency, guest saved-view quota). The defaults in `.env.example` suit the demo; numeric limits must be positive; `FRIDGE_DEMO_IP_ALLOWLIST` accepts trusted IPs/CIDRs (see section 10) |
| `DEMO_MODE` | `true`; shows the **Try as Guest** button on the login page |
| `ALLOWED_ORIGINS` | `https://YOUR_DOMAIN`, exact browser origin |
| `SESSION_COOKIE_SECURE` | `true` (also the production Compose default) |
| `DOMAIN_NAME` | `YOUR_DOMAIN` |
| `LETSENCRYPT_EMAIL` | Your notification email |
| `NEXT_PUBLIC_MAPBOX_TOKEN` | After restricting the token to `https://YOUR_DOMAIN`. The frontend reads it when it starts: after a change, `docker compose -f docker-compose.prod.yml up -d frontend` |
| `DATA_VOLUME_PATH`, `FRIDGE_DATA_ROOT` | Both `/data/datasets`; mounted at the **same absolute path** in backend and job-runner |
| `POSTGRES_VOLUME_PATH` | `/data/postgres`, outside the media root |
| `CACHE_VOLUME_PATH` | `/cache`, including `/cache/huggingface` |
| `JOB_RUNNER_MEMORY_LIMIT` | Optional. The job-runner container's memory limit, within which map re-fits run. Unset, there is no limit and re-fits are skipped. Leave it unset on a t3.large, which has no room beside PostgreSQL, and build maps with the SDK (`compute_visualization`) |
| `BACKUP_VOLUME_PATH` | `/data/backups`, outside the media root |
| `TLS_STATE_PATH` | `/data/tls`; entire Let's Encrypt directory, including symlinks |
| `BACKUP_S3_URI` | Terraform-created `s3://…/database` |
| `STORAGE_TYPE`, `S3_BUCKET`, `S3_REGION` | `s3`, the Terraform media bucket and its region: the backend reads `s3://` media with the instance role (section 8). Media under `FRIDGE_DATA_ROOT` is served as before |

Do not copy the development `.env.example` over this file: it intentionally uses
HTTP settings for local development. Back up secrets securely off the instance;
never paste them into logs/issues. Compose requires the admin bootstrap value.
On a replacement instance reusing `/data`, bootstrap links the existing
`/data/secrets/.env`; it does not regenerate DB or encryption secrets. For recovery
onto a new data disk, restore the original environment before starting anything.

On first start, the backend replaces the seeded default admin password with
`FRIDGE_ADMIN_PASSWORD`. After that the bootstrap does nothing, even if you change
the value. To reset the admin password later, with the stack running:

```bash
docker compose -f docker-compose.prod.yml exec backend python /app/reset_admin.py
```

Without `--password`, this sets the admin password back to the
`FRIDGE_ADMIN_PASSWORD` that the backend container was started with, and signs the
admin out everywhere. To set a new password, either change `FRIDGE_ADMIN_PASSWORD`
in `.env`, recreate the backend with `docker compose -f docker-compose.prod.yml up -d backend`
and run the command above, or append `--password 'NEW_PASSWORD'` (which leaves the
password in shell history).

```bash
# This prints configuration including secrets if -q is omitted.
docker compose -f docker-compose.prod.yml config -q
```

## 5. DNS and the first certificate

Create an **A record** for `YOUR_DOMAIN` pointing to `terraform output -raw
instance_public_ip`. Verify it from outside AWS. Port 80 must be reachable by
Let's Encrypt for HTTP-01; a restrictive CAA record must permit Let's Encrypt.

```bash
cd /home/ubuntu/app
bash ops/tls/bootstrap.sh
```

The script starts only nginx with `ops/nginx/bootstrap.conf`: no certificate,
frontend, database or API is needed. ACME files are served from a shared webroot;
all other requests to the domain redirect to HTTPS. The checked-in certbot service
obtains the certificate and keeps its entire state below `/data/tls`. A failed
issuance leaves the challenge-only proxy running. Correct DNS/firewall/email and
rerun; avoid repeated issuance attempts that consume CA limits. Do not bootstrap
again on a working site just to renew it.

## 6. Start HTTPS and renewal

`pull` fetches the release's images from ghcr.io. For a revision that isn't a release, build
them from the checkout with `docker compose -f docker-compose.prod.yml build --pull` instead.

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 backend nginx backup
sudo systemctl enable --now freedomridge-cert-renew.timer
sudo systemctl list-timers freedomridge-cert-renew.timer
# Exercise the ACME renewal path without replacing the live certificate:
docker compose -f docker-compose.prod.yml run --rm --no-deps certbot \
  renew --dry-run --no-random-sleep-on-renew --webroot -w /var/www/certbot
sudo systemctl start freedomridge-cert-renew.service
sudo journalctl -u freedomridge-cert-renew.service --no-pager -n 50
```

The normal nginx template now serves 443 with the real certificate. The timer
runs twice daily with up to one hour of systemd jitter. Certbot random sleep is
disabled in `renew.sh` (and in the manual dry-run above), so a manual service start
does not silently wait up to eight minutes. `renew.sh` checks renewal, tests nginx configuration,
and reloads nginx after a successful check (also safe when renewal was not due).
It needs no Docker socket in certbot. Check timer failures and certificate expiry
as part of operations; there is no certificate-expiry email alarm in this module.

nginx sends HSTS with `includeSubDomains`, so every subdomain of `YOUR_DOMAIN` must
serve HTTPS too. Every location adding its own headers
includes the shared security headers, including media. CSP starts as
**Content-Security-Policy-Report-Only**: violations appear in the browser console;
no report collector is configured. Next.js inline scripts will report violations
but keep running. Before planning enforcement, add a CSP report collector and `report-uri`/`report-to`
with appropriate retention, then review login, maps, grids, modals and search in a
browser, implement an appropriate nonce/hash policy for inline scripts in the
frontend, adjust the policy in `ops/nginx/security-headers.conf`, then change its
header name to `Content-Security-Policy`. Test and reload nginx. Simply renaming
the current header before that work can break the app.

If an ALB is added later, keep HTTPS from the ALB to nginx, preserve the domain
Host header, use `/health` for checks with appropriate host routing, and configure
`ops/nginx/real-ip.conf` with **only the ALB subnet CIDRs** via `set_real_ip_from`,
`real_ip_header X-Forwarded-For`, `real_ip_recursive on`. Restrict instance ingress
to the ALB security group. Without this, limits count all visitors as the ALB;
trusting the whole Internet permits spoofing. The current default is direct EC2
with no trusted forwarding headers. ALB target checks normally use an IP Host:
provide a separate SG-restricted health listener if needed rather than weakening
the public default server. ACME HTTP-01 must still reach the webroot.

## 7. First admin login

Open **https://YOUR_DOMAIN**, log in as `admin`, and read the generated
password from `FRIDGE_ADMIN_PASSWORD` in `/home/ubuntu/app/.env` over SSH. Complete
any required password change, then create an admin API token in **Settings → API
Tokens** for import. Copy it when it is shown; it is shown only once.
Keep the admin token private and revoke it after import.

## 8. Download and import the datasets

The intended data is **COCO val2017, Open Images V7 validation, iNaturalist 2021
validation and KITTI object training with LiDAR**. KITTI does not provide a
separate official validation set through this importer. Do not use `--all`:
OSV-5M, EuroSAT, BDD100K and TuSimple are outside this hosted demo. COCO and
iNaturalist leave out photos under No-Derivatives licenses by default (section 10).

There are two ways to import:

- **On the instance (CPU)**, described first: the media stays on the data disk under
  `/data/datasets`.
- **[From a workstation into S3](#from-a-workstation-into-s3)**: a machine with more
  compute, ideally an NVIDIA GPU, downloads and imports the datasets and uploads their
  media to the media bucket. The instance only serves it. A GPU imports in hours
  rather than days, no temporary instance is needed, and the data disk holds only the
  database, dumps and caches.

Plan the full workload as 150,433 images, 369,188 object crops and **719,360
COCO/Open Images 4×4 patches** (section 9): about **1.24 million CLIP encodings**.
On CPU, expect roughly 20–28 encodings/s with 2–4 threads, before JPEG decoding,
API writes and index builds. At that rate, encoding alone takes about **12–17 hours**;
allow **1–2 days or more on t4g.large**, plus downloads and map building. These are
estimates, not a measured EC2 run.

For a full import, temporarily use **c7g.8xlarge (32 vCPU, 64 GiB, Graviton)**, or
c7i.8xlarge on an x86_64 instance: an instance can only change type within its
architecture, since its disk holds that architecture's Ubuntu. A provisional
80–160 encodings/s budget gives about 2–4.5 hours of encoding; reserve **4–10 hours**
including decode, writes, indexing and reductions, plus download time. This is an
unmeasured scaling estimate; benchmark the first few thousand inputs and revise it.
For cost planning, budget about **$1.20/hour** (c7i.8xlarge: $1.50), or **$5–12** for that window,
excluding disks/egress. Confirm the selected region's Linux On-Demand rate in the
[AWS calculator](https://calculator.aws/) before resizing. CPU scaling is not linear;
try 16 versus 32 OMP threads if throughput drops. Run one importer at a time.

Resize through the EC2 console (stop, change instance type, start), or from your
operator workstation with these explicit commands. Check the current AZ offers
the temporary type first:

```bash
export INSTANCE_ID=YOUR_INSTANCE_ID
aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
aws ec2 modify-instance-attribute --instance-id "$INSTANCE_ID" \
  --instance-type '{"Value":"c7g.8xlarge"}'
aws ec2 start-instances --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
```

The EIP and EBS disks remain. Wait for SSH/Docker, check `findmnt /data` and
`findmnt /cache`, then import. Never resize during an import. After patches and
indexes finish (section 9), repeat stop/wait/modify/start/wait with `t4g.large`.
Terraform deliberately ignores instance-type drift; it does not perform these
resizes. Keep the pinned AZ unchanged. Sustained imports on t4g or t3 can incur Unlimited
CPU-credit charges.

### Prepare the import environment

Run as `ubuntu`, from `/home/ubuntu/app`:

```bash
sudo apt-get install -y tmux jq
# Run the remaining import commands inside tmux.
tmux new -s demo-import
cd /home/ubuntu/app
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env
# The locked versions, CPU-only PyTorch (the instance has no GPU).
uv sync --frozen --extra cpu
source .venv/bin/activate
export FRIDGE_DATA_ROOT=/data/datasets
export FRIDGE_API_URL=https://YOUR_DOMAIN/api
export HF_HOME=/cache/huggingface
# One importer runs at a time, so it may use every core.
export OMP_NUM_THREADS=$(nproc) MKL_NUM_THREADS=$(nproc) CUDA_VISIBLE_DEVICES=''
read -rsp 'Admin API token: ' FRIDGE_API_TOKEN; echo
export FRIDGE_API_TOKEN
# Importers use local_files_only: explicitly fetch the model first.
python -c "from transformers import CLIPModel, CLIPProcessor; CLIPModel.from_pretrained('openai/clip-vit-base-patch32'); CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')"
```

### Full import

```bash
python examples/download_demo_data.py \
  --datasets coco,open_images,inaturalist,kitti \
  --data-root "$FRIDGE_DATA_ROOT"
# KITTI GPS cache: needs the KITTI ZIPs above; run it before ANY KITTI import.
python examples/kitti/ingest.py --data-root "$FRIDGE_DATA_ROOT/kitti" --limit 0 --fetch-oxts-only
python examples/import_demo_data.py \
  --datasets coco,open_images,inaturalist,kitti --limit 100 --device cpu
# Check the UI and status/log files, then resume the same datasets to full size:
python examples/import_demo_data.py \
  --datasets coco,open_images,inaturalist,kitti --full --device cpu
```

**Run the KITTI GPS step before the first KITTI import, including the `--limit 100`
smoke run.** It reads the downloaded KITTI ZIPs, makes no API calls, and fetches
about 12 MB of raw-drive GPS (OXTS) data for 141 drives over HTTPS in a few minutes.
It exits nonzero if any drive fails; rerun it to retry. Without it, KITTI imports
with no GPS and no map points, **and the runner still reports PASS**: the KITTI log
only says `N frames lack cached GPS`. If that already happened, fetch the cache, then
rerun KITTI directly with `--on-duplicate update` and the same `--limit` as the
import you are repairing (`0` after `--full`). This re-encodes those frames:

```bash
python examples/kitti/ingest.py --data-root "$FRIDGE_DATA_ROOT/kitti" \
  --limit 0 --device cpu --on-duplicate update
```

The downloader keeps archives and resumes ranges. The `kitti` download includes the LiDAR ZIP. KITTI archives stay zipped;
its importer selectively extracts training media and LiDAR if the LiDAR archive
is present. Check `.import-logs/*/status.txt` under the data root: all four runs
must pass. A KITTI PASS does not prove GPS: confirm that `kitti` shows points on
the map. Reruns resume committed samples and object work. `--full` has no dataset
viewing cap. Expected dataset names include `coco-val2017`, `open-images-v7-val`,
`inaturalist-2021-val` and `kitti`. Check the importer output for exact counts
and any license exclusions. `/data/datasets` is mounted **at `/data/datasets`**
inside backend and job-runner. Importers store resolved absolute host paths;
`FRIDGE_DATA_ROOT` and the container mount must match them. `HOST_DATA_ROOT` does
not translate paths for sample media. DB/TLS/backups/secrets stay outside this root.

### Smaller first launch

To open the demo sooner, import COCO and KITTI first, about 76,000 encodings. Allow
several hours, including the 41 GB KITTI download, and benchmark before promising a
time. Datasets you import later start private too: open each one to guests when it is ready.
Invite guests after those datasets pass the section 11 checks:

```bash
python examples/download_demo_data.py --datasets coco,kitti --data-root "$FRIDGE_DATA_ROOT"
python examples/kitti/ingest.py --data-root "$FRIDGE_DATA_ROOT/kitti" --limit 0 --fetch-oxts-only
python examples/import_demo_data.py --datasets coco,kitti --limit 100 --device cpu
python examples/import_demo_data.py --datasets coco,kitti --full --device cpu
```

Add Open Images and iNaturalist later, ideally in a separate compute window. They
need no GPS step:

```bash
python examples/download_demo_data.py --datasets open_images,inaturalist --data-root "$FRIDGE_DATA_ROOT"
python examples/import_demo_data.py --datasets open_images,inaturalist --full --device cpu
```

Then run section 9 (patches for both COCO and Open Images, then the indexes).
Alternatively import on a GPU workstation, then transfer a verified DB dump and
media using the **same absolute paths**, following section 12.

### Tuning

`import_demo_data.py` does not expose batch/thread flags. To tune batches, invoke
the individual importers (same resume behavior), for example:

```bash
python examples/coco/ingest.py --data-root "$FRIDGE_DATA_ROOT/coco" \
  --limit 0 --device cpu --batch-size 64 --api-url "$FRIDGE_API_URL"
```

Start at batch 32–64 on the 64 GiB import box and measure RSS/throughput; smaller
batches help memory. Image/object importers use OMP/MKL for compute threads and have
no `--threads` option. The patch script's `--threads` controls image loading, not
OMP inference threads. Do not pass those flags to the orchestrator.

### From a workstation into S3

Use a Linux workstation with a checkout of the same reviewed commit and enough disk for
the downloads (section 13). Its AWS login needs `s3:PutObject` and `s3:GetObject` on the
media bucket's `media/*` (imports write, the patch step in section 9 reads), and for
the archives below `s3:PutObject`, `s3:GetObject` and `s3:ListBucket`. An IAM Identity
Center profile renews itself while you stay signed in. Terraform gave the instance read
access to `media/`, and a new deployment's first boot set `STORAGE_TYPE`, `S3_BUCKET`
and `S3_REGION` in `.env` (section 4). On a deployment whose `.env` predates the media
bucket (first boot never rewrites it), add them on the instance, with the bucket name
from `terraform output -raw media_bucket` and your `aws_region`, then recreate the
backend:

```bash
cd /home/ubuntu/app
printf 'STORAGE_TYPE=s3\nS3_BUCKET=%s\nS3_REGION=%s\n' MEDIA_BUCKET_NAME us-east-1 >> .env
docker compose -f docker-compose.prod.yml up -d backend
```

From the repository root on the workstation:

```bash
export MEDIA_BUCKET=$(terraform -chdir=ops/terraform output -raw media_bucket)
uv sync --frozen --extra cuda   # --extra cpu without an NVIDIA GPU
source .venv/bin/activate
export AWS_PROFILE=YOUR_PROFILE   # after: aws sso login --profile YOUR_PROFILE
export FRIDGE_DATA_ROOT=$HOME/freedomridge-data FRIDGE_API_URL=https://YOUR_DOMAIN/api
mkdir -p "$FRIDGE_DATA_ROOT"
read -rsp 'Admin API token: ' FRIDGE_API_TOKEN; echo
export FRIDGE_API_TOKEN
python -c "from transformers import CLIPModel, CLIPProcessor; CLIPModel.from_pretrained('openai/clip-vit-base-patch32'); CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')"
python examples/download_demo_data.py \
  --datasets coco,open_images,inaturalist,kitti --data-root "$FRIDGE_DATA_ROOT"
python examples/kitti/ingest.py --data-root "$FRIDGE_DATA_ROOT/kitti" --limit 0 --fetch-oxts-only
python examples/import_demo_data.py \
  --datasets coco,open_images,inaturalist,kitti --limit 100 --media-uri "s3://$MEDIA_BUCKET/media"
python examples/import_demo_data.py \
  --datasets coco,open_images,inaturalist,kitti --full --media-uri "s3://$MEDIA_BUCKET/media"
```

The importers use the GPU when one works. Before saving each batch they upload its
images, object crops, masks and LiDAR files under `media/<dataset folder>/`, then
record `s3://` paths. The media check after the first batch proves the instance can
read them; a 403 there means `.env` lacks the S3 settings (above), or a default storage
configuration in Settings → Storage Configuration has replaced them; do not mark another
configuration as default on this stack. Your upload
bandwidth sets the pace: the extracted media is about 22 GB for COCO and KITTI and
about 50 GB for all four datasets. If the AWS login expires, the importer stops and
says so: sign in again and rerun the same command, which resumes. The KITTI GPS step,
the status files and the checks are as described above. Any importer you run directly,
such as the KITTI GPS repair or the tuning example, needs the same per-dataset prefix
the runner used, or it writes local paths and nothing resumes: add
`--media-uri "s3://$MEDIA_BUCKET/media/kitti"` (the dataset's folder: `coco`,
`open_images_v7`, `inaturalist` or `kitti`). Keep the downloads at least until the
imports pass; section 13 covers keeping the archives in S3 instead.

### Search on the server

The backend image installs CPU-only torch and transformers at the versions locked in
`uv.lock` (the `cpu` extra; see [Updating dependencies](DEPENDENCIES.md)). `backend/encoders.py` lazily loads the CLIP text
and vision towers with `from_pretrained`; `HF_HOME=/cache/huggingface` persists
weights across container recreation and shares the host import cache. The cache
worker prunes only `s3`, `crop` and `composite`, never model weights. Cold search
may still take time to load weights into each worker's RAM. Warm text and reverse
image search before inviting guests: repeat about eight mixed searches so all four
workers have a chance to load both towers; inspect memory and latency. One request
warms only one worker. The backend uses about **1.6 GiB** idle and about **3.4 GiB**
with search warm, before PostgreSQL, Next.js, jobs and page cache. **t4g.large
(8 GiB) is the default and fits the budget**, but memory is tight: watch `free -h`
and `docker stats` while warming search. t4g.xlarge (16 GiB) gives headroom at about
$98/month for the instance alone. No GPU is required.

See [examples/README.md](../examples/README.md) for resume, verification and
per-dataset source details. Dataset downloads and model fetches require outbound
Internet on the deployed box.

## 9. Compute 4×4 patches

In the same import environment, after full image/object imports. From a workstation,
the script reads the `s3://` images with your AWS login and uses the GPU when present.

```bash
python examples/sdk/compute_patch_embeddings.py \
  --dataset coco-val2017 --scales 4x4 --batch-size 32 --threads 8 \
  --api-url "$FRIDGE_API_URL"
python examples/sdk/compute_patch_embeddings.py \
  --dataset open-images-v7-val --scales 4x4 --batch-size 32 --threads 8 \
  --api-url "$FRIDGE_API_URL"
```

This script uses the cached model and also computes the patch visualization unless
`--no-reduce` is set. The CPU wheel and empty `CUDA_VISIBLE_DEVICES` keep this on
CPU; this script has no `--device` option. With the default COCO license selection,
44,960 images × 16 patches means about 719,360 patch embeddings. Allow extra
DB/index/WAL space and time. Check patch scale `4x4` in both datasets and try patch search.

### Build the search and map indexes

Run these once, after all imports and patches. Without them, search and the map
still work, but they scan whole tables.

```bash
python -c 'from fridge import Dataset, connect; connect(); ds = Dataset("coco-val2017"); print(ds.build_ann_index("clip")); print(ds.build_ann_index("clip_obj"))'
docker compose -f docker-compose.prod.yml exec worker python3 scripts/backfill_sample_lonlat.py
docker compose -f docker-compose.prod.yml exec db psql -U freedomridge -d freedomridge -Atc \
  "SELECT indexname FROM pg_indexes WHERE indexname IN ('hnsw_emb_clip', 'hnsw_emb_clip_obj', 'idx_samples_lonlat_cover')"
```

- `build_ann_index` builds one HNSW index per embedding field, covering every
  dataset, so it does not matter which dataset you call it on. `clip` serves image and
  text search, `clip_obj` object search: about 520,000 vectors between them. Each build
  is a server job; the call waits and prints the result, and progress shows under
  Background jobs. A build has a one-hour limit. If it fails on time, run the same
  build in the worker container, which has no limit:
  `docker compose -f docker-compose.prod.yml exec worker python3 scripts/build_ann_indexes.py --field clip`
  (then `--field clip_obj`).
- Patch search and find-unlabeled rank every tile of a scale unless each tile position
  has its own index; on a small server that takes 15-30 s for Open Images. Build the 16
  `4x4` position indexes the same way (about 18 s each for 55,000 vectors):
  `python -c 'from fridge import Dataset, connect; connect(); ds = Dataset("coco-val2017"); [print(ds.build_ann_index(f"clip_patch_4x4_r{r}c{c}")) for r in range(4) for c in range(4)]'`.
  Until all 16 exist, the backend log may warn at startup that the patch tile fields have
  no HNSW index.
- `backfill_sample_lonlat.py` fills any missing longitude/latitude columns, builds
  the covering index `idx_samples_lonlat_cover` for the map without blocking writes,
  then runs `VACUUM (ANALYZE) samples`. It is safe to rerun.
- The last command must print all three index names.

## 10. Public-demo behavior and credits

Keep `DEMO_MODE=true`. Guests must be able to browse global datasets, filter,
inspect every sample, view maps/embeddings and search. They must receive **403**
for admin-only writes: datasets, samples/tags/fields, tokens, concepts and
admin operations. Guests **may save private views** (200); they cannot publish or
share them. Demo-account password changes with a valid body must return **403**.
Section 11 checks these. New datasets are private, so guests see nothing until you open them:
for each hosted dataset, open Dataset Settings → General and set Visibility to **Everyone signed
in**. Leave any dataset guests should not see private.

The job runner removes expired demo accounts (with their sessions and saved views)
and expired sessions every five minutes: check its logs before inviting guests. Compose passes `FRIDGE_ENV=production` even when `DEMO_MODE=false`.
As a manual fallback, use the admin `DELETE /api/auth/cleanup-demo-accounts`
endpoint and inspect `deleted_count`. The cache
worker prunes rendered media hourly; request logs have size-based rotation
(5 × 50 MB per container), not a promised time-based deletion window.

nginx limits are per IP by default, shared across its workers:

| Requests | Sustained rate | Burst | Additional connection limit |
|---|---:|---:|---:|
| Password login | 6/minute | 5 | 10 |
| Demo login (separate zone) | 30/minute | 40 | 100 |
| Reverse image search | 2/second | 4 | 4 |
| Sparse-sample, exports, bulk analytics | 2/second | 10 | 8 |
| General API/frontend, text search, concepts | 50/second | 150 | 100 |
| Media/thumbnail grid | 200/second | 3000 (delay above 400) | 512, separate zone |
| Ingest samples/vectors/patches/coordinates/hashes | 10/second | 20 | 100 |

The heavy rows share a rate zone. Limits return 429; clients should pause and retry.
429/503 include JSON `detail` and `Retry-After: 10`. A shared NAT shares limits unless allowlisted below.
Media queues excess requests and uses separate request/connection zones, so media
streams do not consume API capacity. The first 400 excess media requests are
immediate; the rest drain at 200/s up to the bounded queue. There
is no row/view cap. Request bodies are 1 MiB generally, 30 MiB for by-image search,
and 100 MiB only on ingest routes. Proxy timeouts preserve the existing long
analytics/SDK budgets; nginx timeouts do not themselves cancel backend work.

Keep each dataset's **Data credits & license** link and the per-image attribution
visible. This selection is for a **non-commercial demo**;
that label alone does not satisfy every photo's terms. Source/license information
here follows the checked-in [dataset guide](../examples/README.md):

- COCO: annotations CC BY 4.0; photos have mixed Flickr licenses. The importer
  leaves out NoDerivs photos (license IDs 3 and 6) by default because the app derives
  thumbnails, crops and patches. Preserve attribution and review NC/SA restrictions.
- Open Images V7: annotations CC BY 4.0; photos normally CC BY 2.0. Preserve each
  photo's author, title, source URL and license.
- iNaturalist 2021: per-photo licenses; the default selection excludes ND and
  includes NC. Preserve rights-holder/source/license fields.
- KITTI: CC BY-NC-SA 3.0; retain KITTI attribution and share-alike terms, including
  derived crops/patches. LiDAR is part of the selected source data.
- TuSimple is **not hosted**: dataset hosting rights have not been established.

Before public launch, review the actual imported license metadata and frontend
credits. A successful download/import does not establish redistribution rights.

### Organizations behind one IP

Guest admission defaults to 5 new accounts per IP per hour
(`FRIDGE_DEMO_IP_HOURLY_LIMIT`) and 10 live accounts per IP
(`FRIDGE_DEMO_IP_LIVE_CAP`). For a trusted organization sharing one public IP,
set `FRIDGE_DEMO_IP_ALLOWLIST` in `.env` to comma- or whitespace-separated IPv4/IPv6
addresses or CIDRs, for example:

```dotenv
FRIDGE_DEMO_IP_ALLOWLIST=203.0.113.0/24,2001:db8::/32
```

Use the organization's actual networks; these are documentation ranges. An empty
value exempts nobody. Invalid entries, CIDRs with host bits set, and `/0` are rejected.
Listed networks skip both per-IP guest-account limits. nginx request and connection
budgets become per signed-in browser (IP plus `session_id` cookie); requests without
a session cookie still share the IP's budget. The global `FRIDGE_DEMO_ACCOUNT_CAP`
(default 2000) still applies to everyone.

After changing the value, recreate both services so they use the same allowlist:

```bash
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate backend nginx
```

Anyone behind a listed IP gets the exemption, so list only networks you trust.

## 11. Smoke test before invitations

From the box (`jq` was installed in section 8), use the real certificate (no `-k`):

```bash
curl -fsS https://YOUR_DOMAIN/api/health
curl -sI http://YOUR_DOMAIN/                 # 301 to the fixed HTTPS domain
curl -sI https://YOUR_DOMAIN/               # HSTS, nosniff, CSP Report-Only
curl --max-time 5 -v http://ELASTIC_IP/      # empty reply (nginx 444)
# List the dataset IDs with the admin token from section 8:
curl -fsS -H "Authorization: Bearer $FRIDGE_API_TOKEN" https://YOUR_DOMAIN/api/datasets \
  | jq -r '.[] | "\(.id) \(.name) global=\(.is_global)"'
```

Every dataset guests should see must print `global=true`; open any that does not (section 10).
Then run the guest gate with one of those dataset IDs:

```bash
bash ops/smoke/guest_gate.sh https://YOUR_DOMAIN DATASET_ID
```

The script signs in as a new demo guest and checks that the session cookie is
`Secure`, `HttpOnly` and `SameSite=Lax`, that guests get **403** for tokens, datasets,
samples, password changes and admin status, and that a guest's saved view is created
private (**200**, `is_public:false`, no shared users) and cannot be shared (**403**).
It sends valid bodies and the Origin header, so body validation or CSRF cannot pass
for role enforcement. It prints `GUEST GATE PASSED`, or marks each failing line and
exits 1. A **422** means the request body is invalid and proves nothing about
authorization; **401** means authentication failed. **200/201** on an admin-only
route is a failed gate. Run the companion authorization tests for complete
admin-write coverage; the script is a representative release check, not the whole
route matrix.

Check **one stored absolute-path sample per imported dataset** with the admin token:

```bash
for id in $(curl -fsS -H "Authorization: Bearer $FRIDGE_API_TOKEN" https://YOUR_DOMAIN/api/datasets | jq -r '.[].id'); do
  sample=$(curl -fsS -H "Authorization: Bearer $FRIDGE_API_TOKEN" \
    "https://YOUR_DOMAIN/api/datasets/$id/samples?limit=1" | jq -r '.items[0].id')
  curl -sS -o /dev/null -D /tmp/media-headers -w "dataset $id, sample $sample: %{http_code} (expected 200)\n" \
    -H "Authorization: Bearer $FRIDGE_API_TOKEN" "https://YOUR_DOMAIN/api/samples/$sample/media"
done
grep -i -E 'strict-transport-security|x-content-type-options|content-security-policy' /tmp/media-headers
```

Every dataset must return 200, and the media response must carry the HSTS, nosniff
and CSP headers. In a browser, check:

- admin and guest login;
- a long HTTP/2 thumbnail scroll with both side panels closed and the smallest tile
  size (zero 429s/unavailable tiles);
- a KITTI sample's modal: projected 3D boxes and 2D boxes on the image. LiDAR is
  stored only as a file reference (`lidar`) and a per-box `num_lidar_points`
  attribute; there is no point-cloud viewer;
- maps, including KITTI points;
- the concept slider, text and image search, and the patch sets already imported.

A room-sized burst of 20 demo logins should succeed; do not create dozens of guests
just to test a rate limit in production. Remove `/tmp/media-headers`, revoke the
importer token in **Settings → API Tokens**, confirm a successful S3 backup and SNS
delivery, and inspect CPU/disk metrics before invitations.

## 12. Backups and recovery

The `backup` sidecar runs `pg_dump` daily, validates gzip, retains two local dumps (`BACKUP_KEEP=2`)
and copies each successful dump to `BACKUP_S3_URI` using the instance role. The
bucket is private, encrypted, versioned, and expires current dumps after 30 days
and noncurrent versions after 7 days. Failures are logged and retried on the next
cycle; there is no backup-success alarm yet. Check logs and the actual S3 objects.
The application Backups card uses a separate scheduler and is not configured by
this stack; **the sidecar/S3 objects are authoritative**, not that card.

```bash
docker compose -f docker-compose.prod.yml logs --tail=100 backup
docker compose -f docker-compose.prod.yml run --rm --no-deps --entrypoint sh backup \
  -c 'aws s3 ls "$BACKUP_S3_URI/"'
# Restart begins a new immediate dump; verify its S3 upload in the log.
docker compose -f docker-compose.prod.yml restart backup
```

Media in S3 lives in the versioned media bucket: an overwritten or deleted object
stays recoverable for 7 days. The demo's media can always be imported again.

Dumps contain DB state, not source media, TLS certificates or `.env`. Preserve
`.env`/encryption key securely off-box. This demo's source data can be downloaded
and imported again at the same paths; for unique media arrange a separate backup
or EBS snapshot. The data disk survives instance replacement but is not itself an
off-instance backup. RPO is up to one day; recovery time depends on redownloading
media and restoring the DB. Test restore before relying on it.

For a restore **on the intended replacement instance**, stop all DB clients and
restore into an empty database. The instance role cannot download dumps. On your
**operator workstation** with authorized S3 read credentials, download a verified
dump and copy it over SSH (never put operator AWS keys into the app containers):

```bash
aws s3 cp s3://YOUR_BACKUP_BUCKET/database/freedomridge_TIMESTAMP.sql.gz ./restore.sql.gz
gunzip -t ./restore.sql.gz
scp -i YOUR_KEY.pem ./restore.sql.gz ubuntu@ELASTIC_IP:/home/ubuntu/restore.sql.gz
```

Then on the recovery instance:

```bash
cd /home/ubuntu/app
docker compose -f docker-compose.prod.yml stop nginx frontend backend worker job-runner backup
docker compose -f docker-compose.prod.yml up -d db
sudo install -m 600 /home/ubuntu/restore.sql.gz /data/backups/restore.sql.gz
sudo gzip -t /data/backups/restore.sql.gz
# Destructive to this target DB: verify you are on the recovery host first.
docker compose -f docker-compose.prod.yml exec -T db \
  dropdb -U freedomridge --force --if-exists freedomridge
docker compose -f docker-compose.prod.yml exec -T db \
  createdb -U freedomridge -O freedomridge freedomridge
sudo gzip -dc /data/backups/restore.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db \
    psql -X -v ON_ERROR_STOP=1 -U freedomridge -d freedomridge
docker compose -f docker-compose.prod.yml exec -T db \
  psql -X -v ON_ERROR_STOP=1 -U freedomridge -d freedomridge -c ANALYZE
FRIDGE_DB_CONTAINER=$(docker compose -f docker-compose.prod.yml ps -q db) \
  bash scripts/apply_migrations.sh
docker compose -f docker-compose.prod.yml up -d
```

Use the source `STORAGE_ENCRYPTION_KEY`. Preserve matching media paths. Verify
accounts, dataset/sample counts, media and search before reopening traffic. See
[backup and migration details](BACKUP_AND_MIGRATION.md). Never use `down -v` as
an upgrade or restore shortcut. After host loss, obtain a fresh TLS certificate
using bootstrap if its state was not recovered.

## 13. Capacity, costs and maintenance

Default allocation: **40 GiB root + 300 GiB data + 30 GiB cache**, all gp3. EBS
volumes can grow but not shrink, so the data size is an up-front choice.
Retained archives plus extracted source media total about **112–115 GB** (COCO
2.6 GB, Open Images ~30 GB, iNaturalist 18–21 GB, KITTI ~61.7 GB). Add an estimated
15–25 GB for PostgreSQL/indexes/WAL and 8–10 GB for **two local dumps**, about
135–150 GB before growth/temporary import files. These are estimates; seven local
dumps would add roughly 20–25 GB. S3 independently retains daily dumps for 30 days.
After a dataset's import passes, move its archives to
[Glacier Instant Retrieval](#archives-in-glacier-instant-retrieval) (below) rather than
keeping them on disk. On the instance you may instead delete them if re-downloading is
acceptable. Check extracted media and all status files first, and never remove source
paths still referenced by samples. The KITTI importer also reads labels and calibration
from its ZIPs, so keep them (or their S3 copies) if you may re-import.
The 300 GiB data default leaves room for WAL, import workspace and recovery.
Measure with `df -h / /data /cache` and `sudo du -xsh /data/* /cache/*` during
import. Expand before 80%; do not wait for the alarm during a large download.
The 30 GiB cache allows a 10 GiB pruned media budget plus model weights/headroom.

**With the media in S3** (section 8), the data disk holds PostgreSQL with its indexes
and WAL (15–25 GB for the full demo), two local dumps (8–10 GB) and room to restore a
dump: 50–100 GiB. The workstation holds the downloads instead, about 115 GB for all
four datasets including archives and 64 GB for COCO and KITTI. S3 Standard costs
$0.023 per GB-month: about $1.15/month for the full demo's ~50 GB of media. Uploading
it is roughly 550,000 PUT requests, under $3 once. The instance reads media in its
own region with no transfer charge, and `/cache/s3` keeps recently viewed files local.

### Moving an x86_64 instance to Arm

An x86_64 deployment moves to Arm without a dump and restore: PostgreSQL's data directory is the same on both
(64-bit little-endian, with the same PostgreSQL major version and the same Debian `db` image). We checked it on
this demo's 11 GB database, from a `t3.large` to a `t4g.large`: PostgreSQL 18 started on the copied files,
every count matched, and `amcheck` found every B-tree index sound. A dump and restore would work too, but it
rebuilds every vector index, which takes hours on two vCPUs.

EC2 can't change an instance's architecture (its disk holds that architecture's Ubuntu), so the move is to a new
instance:

1. **Back up first** (section 12): a verified dump in S3. Keep the old instance until the new one has run for a
   while.
2. **A new Arm instance** (`t4g.large`, Ubuntu 26.04 arm64) with the same security group and instance role, and
   **new, empty** data and cache disks at `/data` and `/cache`. Install Docker, check out the release you run,
   and pull its images. Any AZ with capacity will do: the Elastic IP moves between AZs in a region.
   - Don't run the database on a disk **restored from an EBS snapshot** without initializing it first. Such a disk
     loads each block from S3 the first time it's read, at a few MB/s, and queries wait on it: in our test, patch
     search timed out while PostgreSQL waited on disk reads with the CPU idle. A new empty disk has no such delay.
     Neither does attaching the old disks themselves to the new instance, which works only within their AZ.
3. **Copy `/data` while the old server runs**, as root, keeping owners (PostgreSQL's files belong to uid 999),
   for example from the new instance: `sudo rsync -aHAX --numeric-ids --rsync-path='sudo rsync'
   ubuntu@OLD_PRIVATE_IP:/data/ /data/`. This copy of a running database isn't consistent; it moves the bulk.
   A temporary security group rule lets the new instance reach the old one over SSH; remove it afterwards.
4. **Stop the old stack** (`docker compose -f docker-compose.prod.yml stop`), so PostgreSQL shuts down cleanly,
   and **run the same rsync again with `--delete`**: only what changed since moves, so this, the downtime, is short.
5. **Start the stack on the new instance**, apply migrations if its release is newer, check `/api/health`, then
   move the Elastic IP (`aws ec2 associate-address --allocation-id EIP_ALLOCATION --instance-id NEW_INSTANCE
   --allow-reassociation`) and run the smoke checks (section 11). TLS state and secrets come with `/data`.
6. **To roll back**, start the old stack again and move the Elastic IP back: its disks were only read.
7. **Afterwards**, terminate the old instance and its disks, and update the Terraform state: the instance, the
   disks and their attachments, and `availability_zone` if it changed.

To check the copy's indexes yourself, on the new instance:
`CREATE EXTENSION IF NOT EXISTS amcheck;` then
`SELECT count(bt_index_check(i.indexrelid, true)) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_am a ON a.oid = c.relam WHERE a.amname = 'btree' AND i.indisvalid;`
(an error names any index that isn't sound).

### Archives in Glacier Instant Retrieval

The downloader keeps every archive after extracting it. Samples use the extracted
files, so once a dataset's import has passed, its archives are needed only to run
that dataset's downloader or importer again. In the media bucket's `archives/`
prefix, stored as Glacier Instant Retrieval, they cost $0.004 per GB-month instead
of $0.08 on gp3: about $0.26/month for all four datasets. Reading them back costs
$0.03 per GB (about $1.25 for KITTI), and each object is billed for at least 90 days.
A lifecycle rule moves anything under `archives/` to that class; upload with it
directly so no Standard storage is billed in the meantime. The instance role cannot
read or write `archives/`, so use your own AWS login from wherever the downloads are.
On the instance, the 150 GiB minimum already allows for keeping them on the data disk.

| Dataset | Archives under the data root | Size |
|---|---|---|
| COCO | `coco/*.zip` | 1.07 GB |
| Open Images | `open_images_v7/validation.tar.gz open_images_v7/masks/*.zip` | ~14 GB |
| iNaturalist | `inaturalist/*.tar.gz` | 8.94 GB |
| KITTI | `kitti/*.zip` | 41.35 GB |

Copy a dataset's archives only after its `.import-logs/*/status.txt` line shows PASS
and the dataset looks right in the UI; for KITTI, also confirm the map shows its
points. Leave the annotation CSV/JSONL files and the `.extract.jsonl` receipts in
place. Set `ARCHIVES` to the dataset's row and upload:

```bash
export MEDIA_BUCKET=$(terraform -chdir=ops/terraform output -raw media_bucket)
ARCHIVES='kitti/*.zip'
cd "$FRIDGE_DATA_ROOT"
for f in $ARCHIVES; do
  aws s3 cp --only-show-errors --storage-class GLACIER_IR "$f" "s3://$MEDIA_BUCKET/archives/$f"
  set -- $(aws s3api head-object --bucket "$MEDIA_BUCKET" --key "archives/$f" \
    --query "[ContentLength,StorageClass]" --output text)
  if [ "$1" != "$(stat -c %s "$f")" ] || [ "$2" != GLACIER_IR ]; then
    echo "MISMATCH $f: $*" >&2
    break
  fi
  echo "OK $f ($1 bytes, $2)"
done
```

Delete the local copies only when every file printed `OK`, in the same shell (the
unquoted `$ARCHIVES` expands to the same files): `rm -- $ARCHIVES`.

Restore a dataset's archives before running its downloader or importer again.
Without them the downloader fetches the full archives from their source again, and
the KITTI importer cannot start; if only `data_object_velodyne.zip` is missing, it
runs but leaves out LiDAR. A restored archive is extracted again, which never
overwrites existing files. Restore with the dataset's directory (`coco`,
`open_images_v7`, `inaturalist` or `kitti`):

```bash
DATASET=kitti
aws s3 cp --only-show-errors --recursive "s3://$MEDIA_BUCKET/archives/$DATASET/" "$FRIDGE_DATA_ROOT/$DATASET/"
```

Section 1 gives the monthly estimate: about **$82** for t4g.large, 370 GiB of gp3 and
one public IPv4 address in us-east-1, before S3 dumps and requests, CloudWatch
metrics and alarms, Internet egress, temporary larger-instance hours and CPU
credits. With the media in S3 and a 100 GiB data disk, the disks cost $13.60 instead
of $29.60, plus about $1.50 for S3. CPU credits on burstable instances and repeated media downloads can
dominate the estimate. **The $100/month budget is an alert, not a spending cap.**
Notifications go out at 80% of actual spend and at 100% of forecast spend, and the
budget is account-wide. At about $94/month the 80% notification arrives late in
most months; treat the forecast notification as the overrun warning. Stopping EC2
does not stop EBS, backup storage or allocated IP charges.

CPU alarms trigger above 85% for three 5-minute periods. Disk alarms trigger above
80% for two 5-minute periods for `/`, `/data` and `/cache`; missing disk metrics
also alarm, including during first boot and when the agent stops. Confirm the SNS email subscription and look for agent errors if disk
metrics are absent. The module does not add a 5xx alarm or demo-login dashboard.

Review security updates monthly and after relevant advisories. OS unattended
upgrades do not update containers. Back up first, check out the release that has
the fixes, pull its images with `docker compose -f docker-compose.prod.yml pull`
(between releases, rebuild from the checkout with `build --pull`), apply migrations using the explicit production DB container, and recreate with
`up -d`. nginx re-resolves Docker service IPs every 10 seconds; allow startup
health checks and that DNS window after recreation. A single-instance restart still
has downtime; this is not a rolling deployment. After changing the Mapbox token, recreate
the frontend (`docker compose -f docker-compose.prod.yml up -d frontend`). Editing the top-level `nginx.conf` template needs
`docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate nginx`
because envsubst runs at container start. Edits to `ops/nginx/*.conf` need only
`exec nginx nginx -t` followed by `exec nginx nginx -s reload`. Check migrations
and the smoke tests after every update. Do not prune volumes to free disk space.

## 14. Teardown

Before removing the demo, take and test an off-box backup, preserve secrets, and
remove DNS records. Disable the certificate timer and stop the stack on this host:

```bash
sudo systemctl disable --now freedomridge-cert-renew.timer
cd /home/ubuntu/app
docker compose -f docker-compose.prod.yml down
```

On your workstation, `terraform plan -destroy` intentionally refuses while the
instance, data EBS and the backup and media S3 buckets have `prevent_destroy = true`. For
permanent deletion, first decide where recovery copies live, then explicitly remove those
four guards in `main.tf`. S3 also has `force_destroy = false`: empty **all object versions
and delete markers** yourself only after preserving needed backups, media and archives. Review the full
destroy plan, then run `terraform destroy`. This also removes the cache disk,
instance, Elastic IP, alarms, role and budget. If retaining resources, manage their
Terraform ownership deliberately; retaining a stopped instance/disk/bucket still
costs money. Do not leave untracked resources by deleting Terraform state. The state
bucket is not part of the module: once the destroy has finished and the state is no
longer needed, empty all its object versions and delete it yourself.

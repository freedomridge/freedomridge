#!/bin/bash
set +x
set -euo pipefail
# Never enable shell tracing: generated secrets must not reach cloud-init logs.
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update
apt-get -o DPkg::Lock::Timeout=600 install -y ca-certificates curl gnupg lsb-release git openssl python3-venv unzip jq nvme-cli unattended-upgrades

# Clone before touching disks or generating secrets. Never put credentials in URLs,
# process arguments, git config or cloud-init logs. This block is safe to re-run.
REPOSITORY_URL=$(printf '%s' '${repository_url_base64}' | base64 --decode)
REPOSITORY_REF=$(printf '%s' '${repository_ref_base64}' | base64 --decode)
TOKEN_FILE_DEFAULT=$(printf '%s' '${repository_token_file_base64}' | base64 --decode)
export REPOSITORY_TOKEN_FILE="$${REPOSITORY_TOKEN_FILE:-$TOKEN_FILE_DEFAULT}"
export GIT_TERMINAL_PROMPT=0
if [ ! -d /home/ubuntu/app/.git ]; then
  if [ -e /home/ubuntu/app ]; then
    rmdir /home/ubuntu/app || { echo 'Bootstrap stopped: /home/ubuntu/app is not an empty directory or a checkout.' >&2; exit 1; }
  fi
  clone_dir=$(mktemp -d /home/ubuntu/.app-bootstrap.XXXXXX)
  askpass=$(mktemp)
  trap 'rm -f "$askpass"' EXIT
  cat > "$askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) cat "$REPOSITORY_TOKEN_FILE" ;;
esac
ASKPASS
  chmod 700 "$askpass"
  export GIT_ASKPASS="$askpass"
  if [ -n "$REPOSITORY_TOKEN_FILE" ] && [ ! -s "$REPOSITORY_TOKEN_FILE" ]; then
    echo 'Bootstrap stopped before disk setup: repository token file missing or empty. Follow private-repo recovery in docs/DEPLOY_TO_AWS.md.' >&2
    exit 1
  fi
  if ! git -c credential.helper= clone --quiet "$REPOSITORY_URL" "$clone_dir" >/dev/null 2>&1 ||
     ! git -C "$clone_dir" checkout --quiet "$REPOSITORY_REF" >/dev/null 2>&1; then
    echo 'Bootstrap stopped before disk setup: clone/checkout failed. Check repository URL, reviewed ref and read-only token access; follow bootstrap recovery in docs/DEPLOY_TO_AWS.md. No secrets were generated.' >&2
    exit 1
  fi
  mv "$clone_dir" /home/ubuntu/app
  rm -f "$askpass"
  trap - EXIT
fi
cd /home/ubuntu/app
# A re-run preserves an existing checkout and never resets operator changes.
if [ "$(git -c safe.directory=/home/ubuntu/app rev-parse HEAD)" != "$(git -c safe.directory=/home/ubuntu/app rev-parse "$REPOSITORY_REF^{commit}")" ]; then
  echo 'Bootstrap stopped: existing checkout differs from repository_ref. Review and check out the intended revision before re-running.' >&2
  exit 1
fi
unset GIT_ASKPASS REPOSITORY_TOKEN_FILE

# Attachments may arrive after cloud-init starts. Identify Nitro disks by EBS ID,
# never by enumeration order. Refuse unknown filesystems; never format existing data.
mount_ebs() {
  local volume_id="$1" mount_path="$2" device="" serial attempt fs uuid
  serial=$(printf '%s' "$volume_id" | tr -d '-')
  for attempt in $(seq 1 120); do
    device=$(lsblk -dnpo NAME,SERIAL | awk -v id="$serial" '$2 == id {print $1}')
    [ -n "$device" ] && break
    sleep 5
  done
  [ -b "$device" ] || { echo "EBS $volume_id not attached" >&2; exit 1; }
  fs=$(lsblk -dnro FSTYPE "$device")
  if [ -z "$fs" ]; then
    [ "$(lsblk -nr "$device" | wc -l)" -eq 1 ] || { echo 'Refusing partitioned disk' >&2; exit 1; }
    [ -z "$(wipefs -n "$device")" ] || { echo 'Refusing disk with signatures' >&2; exit 1; }
    mkfs.ext4 -m 1 "$device"
  elif [ "$fs" != ext4 ]; then
    echo "Expected ext4 on $volume_id, got $fs" >&2; exit 1
  fi
  uuid=$(blkid -s UUID -o value "$device")
  mkdir -p "$mount_path"
  if grep -q "^UUID=$uuid " /etc/fstab; then
    sed -i "\|^UUID=$uuid |c\UUID=$uuid $mount_path ext4 defaults,nofail,x-systemd.device-timeout=60s 0 2" /etc/fstab
  else
    echo "UUID=$uuid $mount_path ext4 defaults,nofail,x-systemd.device-timeout=60s 0 2" >> /etc/fstab
  fi
  if mountpoint -q "$mount_path"; then
    [ "$(findmnt -n -o UUID --target "$mount_path")" = "$uuid" ] || { echo "Wrong volume mounted at $mount_path" >&2; exit 1; }
  else
    mount "$mount_path"
  fi
}
mount_ebs '${data_volume_id}' /data
mount_ebs '${cache_volume_id}' /cache

mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' \
  "$(dpkg --print-architecture)" "$(lsb_release -cs)" > /etc/apt/sources.list.d/docker.list
apt-get -o DPkg::Lock::Timeout=600 update
apt-get -o DPkg::Lock::Timeout=600 install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
usermod -aG docker ubuntu
# Fail closed if an EBS mount fails on reboot; never start containers on empty root dirs.
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/storage.conf <<'UNIT'
[Unit]
RequiresMountsFor=/data /cache
UNIT
systemctl daemon-reload

ORIGIN='${public_origin}'
DOMAIN=$(printf '%s' "$ORIGIN" | sed 's|^https://||')
EMAIL=$(printf '%s' '${email_base64}' | base64 --decode)
# Keep DB/backups outside the backend's read-only media containment root.
install -d -o ubuntu -g ubuntu /data/datasets /cache/s3 /cache/crop /cache/composite /cache/huggingface
install -d -m 700 /data/postgres /data/backups /data/tls/letsencrypt
install -d -m 755 /data/tls/webroot
# The authoritative environment survives root-volume/instance replacement.
install -d -m 700 -o ubuntu -g ubuntu /data/secrets
if [ ! -s /data/secrets/.env ]; then
  if [ -s .env ] && [ ! -L .env ]; then
    install -m 600 -o ubuntu -g ubuntu .env /data/secrets/.env
  elif [ -s /data/postgres/PG_VERSION ]; then
    echo 'Existing database without /data/secrets/.env: restore original secrets before continuing; refusing to generate a new password.' >&2
    exit 1
  else
  umask 077
  # Publish only a complete file; an interrupted write can be retried safely.
  cat > /data/secrets/.env.new <<ENV
COMPOSE_PROJECT_NAME=freedomridge-prod
FRIDGE_ENV=production
STORAGE_ENCRYPTION_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 24)
FRIDGE_ADMIN_PASSWORD=$(openssl rand -hex 24)
DEMO_MODE=true
FRIDGE_DEMO_IP_HOURLY_LIMIT=5
FRIDGE_DEMO_IP_LIVE_CAP=10
FRIDGE_DEMO_IP_ALLOWLIST=
DOMAIN_NAME=$DOMAIN
LETSENCRYPT_EMAIL=$EMAIL
ALLOWED_ORIGINS=$ORIGIN
SESSION_COOKIE_SECURE=true
NEXT_PUBLIC_MAPBOX_TOKEN=
DATA_VOLUME_PATH=/data/datasets
FRIDGE_DATA_ROOT=/data/datasets
POSTGRES_VOLUME_PATH=/data/postgres
CACHE_VOLUME_PATH=/cache
BACKUP_VOLUME_PATH=/data/backups
BACKUP_KEEP=2
TLS_STATE_PATH=/data/tls
BACKUP_S3_URI=${backup_s3_uri}
AWS_DEFAULT_REGION=${aws_region}
STORAGE_TYPE=s3
S3_BUCKET=${media_bucket}
S3_REGION=${aws_region}
POSTGRES_SHARED_BUFFERS=1GB
POSTGRES_EFFECTIVE_CACHE_SIZE=4GB
POSTGRES_WORK_MEM=16MB
FRIDGE_CACHE_MAX_BYTES=10737418240
ENV
  mv /data/secrets/.env.new /data/secrets/.env
  fi
fi
chmod 600 /data/secrets/.env
chown ubuntu:ubuntu /data/secrets/.env
ln -sfn /data/secrets/.env .env
chown -R ubuntu:ubuntu /home/ubuntu/app

# Disk metrics complement EC2's built-in CPU metrics. No application secrets in config.
# Verify the package against AWS's published signing-key fingerprint before install.
# https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/verify-CloudWatch-Agent-Package-Signature.html
cw_dir=$(mktemp -d /tmp/fridge-cloudwatch.XXXXXX)
install -d -m 700 "$cw_dir/gnupg"
cw_base=https://amazoncloudwatch-agent.s3.amazonaws.com
curl -fsSL "$cw_base/assets/amazon-cloudwatch-agent.gpg" -o "$cw_dir/key.gpg"
cw_fingerprint=$(gpg --homedir "$cw_dir/gnupg" --show-keys --with-colons "$cw_dir/key.gpg" | awk -F: '$1 == "fpr" {print $10; exit}')
[ "$cw_fingerprint" = 937616F3450B7D806CBD9725D58167303B789C72 ] || { echo 'CloudWatch signing key mismatch' >&2; exit 1; }
gpg --homedir "$cw_dir/gnupg" --batch --import "$cw_dir/key.gpg"
curl -fsSL "$cw_base/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" -o "$cw_dir/agent.deb"
curl -fsSL "$cw_base/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb.sig" -o "$cw_dir/agent.deb.sig"
gpg --homedir "$cw_dir/gnupg" --batch --verify "$cw_dir/agent.deb.sig" "$cw_dir/agent.deb"
dpkg -i "$cw_dir/agent.deb"
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'JSON'
{
  "metrics": {
    "namespace": "FreedomRidgeHost",
    "append_dimensions": {"InstanceId": "$${aws:InstanceId}"},
    "aggregation_dimensions": [["InstanceId", "path"]],
    "metrics_collected": {
      "disk": {
        "resources": ["/", "/data", "/cache"],
        "measurement": ["used_percent"],
        "metrics_collection_interval": 60,
        "drop_device": true
      }
    }
  }
}
JSON
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
install -m 644 ops/systemd/freedomridge-cert-renew.* /etc/systemd/system/
systemctl daemon-reload
# Operator enables the timer AFTER issuance; no app starts before TLS and token setup.
echo 'Bootstrap complete. Secrets (including FRIDGE_ADMIN_PASSWORD) are in /data/secrets/.env (mode 600), linked from /home/ubuntu/app/.env.'
echo 'Set the Mapbox token, configure DNS, then follow docs/DEPLOY_TO_AWS.md to issue TLS and start the app.'

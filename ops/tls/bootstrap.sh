#!/usr/bin/env bash
# Run from the repository root after DNS points to this machine.
set -euo pipefail
cd "$(dirname "$0")/../.."
compose=(docker compose -f docker-compose.prod.yml)
# Safe to rerun before app launch; briefly replaces the HTTPS proxy if already live.
NGINX_CONFIG_TEMPLATE=./ops/nginx/bootstrap.conf "${compose[@]}" up -d --no-deps nginx
"${compose[@]}" run --rm --no-deps --entrypoint /bin/sh certbot -c '
    exec certbot certonly --webroot -w /var/www/certbot \
        --cert-name "$DOMAIN_NAME" -d "$DOMAIN_NAME" \
        --email "$LETSENCRYPT_EMAIL" --agree-tos --non-interactive
'
echo 'Certificate ready. Start HTTPS with: docker compose -f docker-compose.prod.yml pull && docker compose -f docker-compose.prod.yml up -d'

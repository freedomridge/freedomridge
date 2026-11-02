#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
compose=(docker compose -f docker-compose.prod.yml)
"${compose[@]}" run --rm --no-deps certbot renew --no-random-sleep-on-renew --webroot -w /var/www/certbot --quiet
# Reload after each successful renewal check (also safe when nothing was due).
# No Docker socket or process namespace is shared with the certbot container.
"${compose[@]}" exec -T nginx nginx -t
"${compose[@]}" exec -T nginx nginx -s reload

#!/bin/sh
set -eu
set -f

template=${NGINX_TEMPLATE_PATH:-/etc/nginx/nginx.conf.template}
output=${NGINX_OUTPUT_PATH:-/etc/nginx/nginx.conf}
allowlist=${NGINX_ALLOWLIST_PATH:-/etc/nginx/allowlist.conf}

: > "$allowlist"
entries=$(printf '%s' "${FRIDGE_DEMO_IP_ALLOWLIST:-}" | tr ',[:space:]' ' ')
for entry in $entries; do
    if ! printf '%s\n' "$entry" | grep -Eq '^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$'; then
        printf 'FRIDGE_DEMO_IP_ALLOWLIST invalid entry: %s\n' "$entry" >&2
        exit 1
    fi
    case "$entry" in
        */0|*/00|*/000)
            printf 'FRIDGE_DEMO_IP_ALLOWLIST invalid entry: %s (/0 is not allowed)\n' "$entry" >&2
            exit 1
            ;;
    esac
    printf '%s 1;\n' "$entry" >> "$allowlist"
done

envsubst '$DOMAIN_NAME' < "$template" > "$output"
exec nginx -g 'daemon off;'

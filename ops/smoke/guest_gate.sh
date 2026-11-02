#!/usr/bin/env bash
# Guest-gate release check for a public demo (docs/DEPLOY_TO_AWS.md, section 11).
#
# Usage: bash ops/smoke/guest_gate.sh https://YOUR_DOMAIN DATASET_ID
#
# Signs in as a new demo guest, then checks that admin-only writes return 403 and that
# a guest's saved view stays private. Every request has a valid body and the site's
# Origin header, so a 422 (invalid body) or a CSRF refusal cannot pass for role
# enforcement. DATASET_ID must be open to guests (Visibility: Everyone signed in). Needs curl and python3.
# Exits 0 only when every check passes. Creates one demo account and one private view.
set -u

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 https://YOUR_DOMAIN DATASET_ID" >&2
  exit 2
fi
origin=${1%/}
dataset=$2
api="$origin/api"

umask 077
work=$(mktemp -d)
cleanup() {
  rm -f -- "$work/cookies" "$work/headers" "$work/response"
  rmdir -- "$work"
}
trap cleanup EXIT
failed=0

curl -fsS -D "$work/headers" -c "$work/cookies" -o /dev/null -X POST "$api/auth/demo-login" || {
  echo "Demo login failed: is DEMO_MODE=true?" >&2
  exit 1
}
cookie=$(grep -i '^set-cookie: session_id=' "$work/headers" | tr -d '\r')
required='HttpOnly SameSite=lax'
case "$origin" in https://*) required="$required Secure" ;; esac
for flag in $required; do
  if printf '%s' "$cookie" | grep -qi "; *$flag"; then
    echo "session cookie has $flag"
  else
    echo "FAIL: session cookie lacks $flag"
    failed=1
  fi
done

# check EXPECTED METHOD ROUTE [BODY]
check() {
  local expected="$1" method="$2" route="$3" body="${4:-}" code
  local args=(-sS -o "$work/response" -w '%{http_code}' -X "$method" -b "$work/cookies" -H "Origin: $origin")
  if [ -n "$body" ]; then
    args+=(-H 'Content-Type: application/json' -d "$body")
  fi
  code=$(curl "${args[@]}" "$api/$route")
  if [ "$code" = "$expected" ]; then
    echo "$method $route: $code"
  else
    echo "FAIL: $method $route: $code (expected $expected)"
    failed=1
  fi
}

check 200 GET datasets
# A private dataset would fail every check below for the wrong reason, so stop here instead.
check 200 GET "datasets/$dataset"
if ! python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("is_global") else 1)' "$work/response" 2>/dev/null; then
  echo "FAIL: guests cannot see dataset $dataset; set its Visibility to Everyone signed in (DEPLOY_TO_AWS.md section 10)" >&2
  exit 1
fi
# 403: signed in, but forbidden by role or account policy.
check 403 POST tokens '{"name":"guest-gate","expires_days":1}'
check 403 POST datasets '{"name":"guest-gate"}'
check 403 POST "datasets/$dataset/samples" '{"samples":[{"filepath":"/data/datasets/gate.jpg"}]}'
check 403 POST auth/change-password '{"current_password":"x","new_password":"Correct-Horse-9"}'
check 403 GET admin/status
# 200: guests may save a view, but a requested public share is stripped.
check 200 POST "datasets/$dataset/views" \
  "{\"name\":\"guest-gate-$(date +%s)\",\"content\":{},\"is_public\":true,\"shared_with_user_ids\":[]}"
view=$(python3 - "$work/response" <<'PY'
import json
import sys

try:
    with open(sys.argv[1]) as handle:
        view = json.load(handle)
except (OSError, ValueError):
    sys.exit(0)
if isinstance(view, dict) and view.get('is_public') is False and view.get('shared_with_user_ids') == []:
    print(view['id'])
PY
)
if [ -n "$view" ]; then
  echo "saved view $view is private"
  check 403 PATCH "datasets/$dataset/views/$view/share" '{"is_public":true,"shared_with_user_ids":[]}'
else
  echo "FAIL: the guest's view was not created private"
  failed=1
fi

if [ "$failed" = 0 ]; then
  echo "GUEST GATE PASSED"
else
  echo "GUEST GATE FAILED: fix the lines marked FAIL before inviting visitors"
  exit 1
fi

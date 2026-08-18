#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_BASE_URL="${APP_BASE_URL:-http://127.0.0.1:8001}"
SMOKE_ATTEMPTS="${SMOKE_ATTEMPTS:-30}"
SMOKE_INTERVAL_SECONDS="${SMOKE_INTERVAL_SECONDS:-2}"

fail() {
    printf 'smoke_failed category=%s\n' "$1" >&2
    exit 1
}

[[ "$APP_BASE_URL" == "http://127.0.0.1:8001" ]] \
    || fail "unexpected_target"
command -v curl >/dev/null 2>&1 || fail "curl_unavailable"

wait_for_endpoint() {
    local path=$1
    local attempt
    for ((attempt = 1; attempt <= SMOKE_ATTEMPTS; attempt++)); do
        if curl \
            --fail \
            --silent \
            --show-error \
            --max-time 10 \
            --output /dev/null \
            "${APP_BASE_URL}${path}"; then
            return 0
        fi
        sleep "$SMOKE_INTERVAL_SECONDS"
    done
    return 1
}

wait_for_endpoint "/live" || fail "live"
wait_for_endpoint "/ready" || fail "ready"
wait_for_endpoint "/internal/metrics" || fail "metrics"

# This smoke suite is intentionally read-only and never performs a
# consultation or calls DirectMail, CAPTCHA, OSS, or a real provider.
printf 'smoke_succeeded\n'

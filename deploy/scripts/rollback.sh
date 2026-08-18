#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/deploy/compose.production.yml"
STATE_DIR="${RELEASE_STATE_DIR:-/srv/weiquan/release-state}"
CURRENT_IMAGE_FILE="$STATE_DIR/current-image"
PREVIOUS_IMAGE_FILE="$STATE_DIR/previous-image"

fail() {
    printf 'rollback_failed category=%s\n' "$1" >&2
    exit 1
}

[[ -s "$PREVIOUS_IMAGE_FILE" ]] || fail "previous-image_missing"
previous_image="$(<"$PREVIOUS_IMAGE_FILE")"
[[ -n "$previous_image" ]] || fail "previous-image_empty"
case "$previous_image" in
    latest|*:latest|*@latest) fail "previous-image_mutable" ;;
esac

failed_image=""
if [[ -s "$CURRENT_IMAGE_FILE" ]]; then
    failed_image="$(<"$CURRENT_IMAGE_FILE")"
fi

export IMAGE_REF="$previous_image"
/usr/bin/bash "$SCRIPT_DIR/preflight.sh"

(
    cd -- "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" up -d --no-deps app
) || fail "application_start_failed"

/usr/bin/bash "$SCRIPT_DIR/smoke.sh" || fail "smoke_failed"

printf '%s\n' "$previous_image" >"${CURRENT_IMAGE_FILE}.tmp"
mv -f -- "${CURRENT_IMAGE_FILE}.tmp" "$CURRENT_IMAGE_FILE"
if [[ -n "$failed_image" && "$failed_image" != "$previous_image" ]]; then
    printf '%s\n' "$failed_image" >"${PREVIOUS_IMAGE_FILE}.tmp"
    mv -f -- "${PREVIOUS_IMAGE_FILE}.tmp" "$PREVIOUS_IMAGE_FILE"
fi

# Database schema is forward-only during normal rollback. Schema downgrade
# and automatic database restoration are separate, approved procedures.
printf 'rollback_succeeded image=%s\n' "$previous_image"

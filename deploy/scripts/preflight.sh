#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/deploy/compose.production.yml"
ENV_FILE="/etc/weiquan/weiquan.env"
APP_URL="http://127.0.0.1:8001"
IMAGE_SOURCE="${IMAGE_SOURCE:-pull}"

fail() {
    printf 'preflight_failed category=%s\n' "$1" >&2
    exit 1
}

for command_name in docker nginx curl; do
    command -v "$command_name" >/dev/null 2>&1 \
        || fail "missing_${command_name}"
done
docker compose version >/dev/null 2>&1 \
    || fail "docker_compose_unavailable"

[[ -n "${IMAGE_REF:-}" ]] || fail "missing_IMAGE_REF"
case "$IMAGE_SOURCE" in
    pull|build) ;;
    *) fail "invalid_IMAGE_SOURCE" ;;
esac
[[ -r "$ENV_FILE" ]] || fail "missing_environment_file"
[[ -r "$PROJECT_DIR/data/seed_statutes.yaml" ]] \
    || fail "missing_data/seed_statutes.yaml"

for path in \
    /srv/weiquan/attachments \
    /srv/weiquan/backup-staging \
    /srv/weiquan/logs; do
    [[ -d "$path" ]] || fail "runtime_directory_missing"
    [[ -w "$path" ]] || fail "runtime_directory_not_writable"
done

(
    cd -- "$PROJECT_DIR"
    docker compose -f "$COMPOSE_FILE" config --quiet
) || fail "compose_config_invalid"

nginx -t >/dev/null 2>&1 || fail "nginx_config_invalid"

case "$APP_URL" in
    http://127.0.0.1:8001) ;;
    *) fail "app_listener_not_loopback" ;;
esac

printf 'preflight_succeeded\n'

#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/deploy/compose.production.yml"
STATE_DIR="${RELEASE_STATE_DIR:-/srv/weiquan/release-state}"
CURRENT_IMAGE_FILE="$STATE_DIR/current-image"
PREVIOUS_IMAGE_FILE="$STATE_DIR/previous-image"
POSTGRES_PROBE_ATTEMPTS=60
POSTGRES_PROBE_INTERVAL_SECONDS=2
IMAGE_SOURCE="${IMAGE_SOURCE:-pull}"

fail() {
    printf 'deploy_failed category=%s\n' "$1" >&2
    exit 1
}

[[ -n "${IMAGE_REF:-}" ]] || fail "missing_IMAGE_REF"
case "$IMAGE_REF" in
    latest|*:latest|*@latest) fail "mutable_latest_image" ;;
esac
case "$IMAGE_SOURCE" in
    pull|build) ;;
    *) fail "invalid_IMAGE_SOURCE" ;;
esac
export IMAGE_REF IMAGE_SOURCE

mkdir -p -- "$STATE_DIR"
[[ -w "$STATE_DIR" ]] || fail "release_state_not_writable"

/usr/bin/bash "$SCRIPT_DIR/preflight.sh"

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

(
    cd -- "$PROJECT_DIR"
    case "$IMAGE_SOURCE" in
        pull)
            compose pull app postgres
            ;;
        build)
            compose pull postgres
            compose build --pull app
            ;;
    esac
) || fail "image_prepare_failed"

(
    cd -- "$PROJECT_DIR"
    compose run --rm --no-deps app python scripts/verify_refs.py
    compose run --rm --no-deps app python scripts/check_recall.py
) || fail "image_statutes_invalid"

(
    cd -- "$PROJECT_DIR"
    compose up -d --no-recreate postgres
) || fail "postgres_start_failed"

database_exists=""
for ((attempt = 1; attempt <= POSTGRES_PROBE_ATTEMPTS; attempt++)); do
    if database_exists="$(
        cd -- "$PROJECT_DIR"
        compose exec -T postgres sh -ceu \
            'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc \
            "SELECT CASE WHEN to_regclass('\''public.alembic_version'\'') IS NULL \
            THEN '\''no'\'' ELSE '\''yes'\'' END"'
    )"; then
        break
    fi
    database_exists=""
    if ((attempt < POSTGRES_PROBE_ATTEMPTS)); then
        sleep "$POSTGRES_PROBE_INTERVAL_SECONDS"
    fi
done
[[ "$database_exists" == "yes" || "$database_exists" == "no" ]] \
    || fail "database_probe_failed"

if [[ "$database_exists" == "yes" ]]; then
    (
        cd -- "$PROJECT_DIR"
        compose run --rm --no-deps app \
            /app/deploy/backup/backup-postgres.sh
    ) || fail "backup_failed"
elif [[ "${ALLOW_INITIAL_DEPLOY:-}" != "1" ]]; then
    fail "initial_deploy_not_confirmed"
fi

(
    cd -- "$PROJECT_DIR"
    compose run --rm --no-deps app python -m alembic upgrade head
) || fail "migration_failed"

old_image=""
if [[ -s "$CURRENT_IMAGE_FILE" ]]; then
    old_image="$(<"$CURRENT_IMAGE_FILE")"
elif container_id="$(
    cd -- "$PROJECT_DIR"
    compose ps -q app
)" && [[ -n "$container_id" ]]; then
    old_image="$(
        docker inspect --format '{{.Config.Image}}' "$container_id"
    )"
fi

if [[ -n "$old_image" && "$old_image" != "$IMAGE_REF" ]]; then
    printf '%s\n' "$old_image" >"${PREVIOUS_IMAGE_FILE}.tmp"
    mv -f -- "${PREVIOUS_IMAGE_FILE}.tmp" "$PREVIOUS_IMAGE_FILE"
fi

(
    cd -- "$PROJECT_DIR"
    compose up -d --no-deps app
) || fail "application_start_failed"

if ! /usr/bin/bash "$SCRIPT_DIR/smoke.sh"; then
    if [[ -s "$PREVIOUS_IMAGE_FILE" ]]; then
        /usr/bin/bash "$SCRIPT_DIR/rollback.sh" || true
    fi
    fail "smoke_failed"
fi

printf '%s\n' "$IMAGE_REF" >"${CURRENT_IMAGE_FILE}.tmp"
mv -f -- "${CURRENT_IMAGE_FILE}.tmp" "$CURRENT_IMAGE_FILE"
printf 'deploy_succeeded image=%s\n' "$IMAGE_REF"

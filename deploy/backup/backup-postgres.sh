#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_STEP="configuration"
PLAIN_DUMP=""
ENCRYPTED_TMP=""
CHECKSUM_TMP=""

cleanup() {
    rm -f -- "$PLAIN_DUMP" "$ENCRYPTED_TMP" "$CHECKSUM_TMP"
}

on_error() {
    local status=$?
    printf 'backup_failed category=%s\n' "$CURRENT_STEP" >&2
    exit "$status"
}

trap cleanup EXIT
trap on_error ERR

require_env() {
    local name=$1
    if [[ -z "${!name:-}" ]]; then
        printf 'backup_failed category=missing_%s\n' "$name" >&2
        return 1
    fi
}

for variable in \
    PGHOST PGPORT PGUSER PGDATABASE \
    AGE_BACKUP_RECIPIENT ALIYUN_ACCESS_KEY_ID \
    ALIYUN_ACCESS_KEY_SECRET OSS_ENDPOINT OSS_BUCKET \
    BACKUP_STAGING_DIR; do
    require_env "$variable"
done
require_env "PGPASSWORD"

CURRENT_STEP="postgres_compatibility"
"$SCRIPT_DIR/check-postgres-compatibility.sh" || exit $?

BACKUP_TMPFS_DIR="${BACKUP_TMPFS_DIR:-/dev/shm}"
BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"
mkdir -p -- "$BACKUP_STAGING_DIR"
if [[ ! -d "$BACKUP_TMPFS_DIR" ]]; then
    printf 'backup_failed category=tmpfs_unavailable\n' >&2
    exit 1
fi
if command -v findmnt >/dev/null 2>&1; then
    if [[ "$(findmnt -n -o FSTYPE --target "$BACKUP_TMPFS_DIR")" != "tmpfs" ]]; then
        printf 'backup_failed category=tmpfs_required\n' >&2
        exit 1
    fi
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="weiquan-${STAMP}.dump.age"
PLAIN_DUMP="$(mktemp "${BACKUP_TMPFS_DIR%/}/weiquan-pg.XXXXXX.dump")"
ENCRYPTED_TMP="$(mktemp "${BACKUP_STAGING_DIR%/}/.${NAME}.XXXXXX")"
CHECKSUM_TMP="$(mktemp "${BACKUP_STAGING_DIR%/}/.${NAME}.sha256.XXXXXX")"
ENCRYPTED="${BACKUP_STAGING_DIR%/}/${NAME}"
CHECKSUM="${ENCRYPTED}.sha256"

CURRENT_STEP="pg_dump"
pg_dump \
    --format=custom \
    --no-owner \
    --no-privileges \
    --file="$PLAIN_DUMP"
test -s "$PLAIN_DUMP"

CURRENT_STEP="encryption"
age --encrypt \
    --recipient "$AGE_BACKUP_RECIPIENT" \
    --output "$ENCRYPTED_TMP" \
    "$PLAIN_DUMP"
test -s "$ENCRYPTED_TMP"
rm -f -- "$PLAIN_DUMP"
PLAIN_DUMP=""
mv -f -- "$ENCRYPTED_TMP" "$ENCRYPTED"
ENCRYPTED_TMP=""

CURRENT_STEP="checksum"
(
    cd -- "$BACKUP_STAGING_DIR"
    sha256sum "$NAME" >"$CHECKSUM_TMP"
    sha256sum --check "$(basename -- "$CHECKSUM_TMP")"
)
mv -f -- "$CHECKSUM_TMP" "$CHECKSUM"
CHECKSUM_TMP=""
(
    cd -- "$BACKUP_STAGING_DIR"
    sha256sum --check "$(basename -- "$CHECKSUM")"
)

DAILY_KEY="${BACKUP_PREFIX%/}/daily/${NAME}"
CURRENT_STEP="upload"
# oss_objects.py put keeps credentials in the process environment.
python "$SCRIPT_DIR/oss_objects.py" put \
    --source "$ENCRYPTED" \
    --key "$DAILY_KEY"
python "$SCRIPT_DIR/oss_objects.py" put \
    --source "$CHECKSUM" \
    --key "${DAILY_KEY}.sha256"

if [[ "$(date -u +%u)" == "7" ]]; then
    WEEKLY_KEY="${BACKUP_PREFIX%/}/weekly/${NAME}"
    python "$SCRIPT_DIR/oss_objects.py" put \
        --source "$ENCRYPTED" \
        --key "$WEEKLY_KEY"
    python "$SCRIPT_DIR/oss_objects.py" put \
        --source "$CHECKSUM" \
        --key "${WEEKLY_KEY}.sha256"
fi

CURRENT_STEP="retention"
"$SCRIPT_DIR/prune-backups.sh"

MARKER_TMP="${BACKUP_STAGING_DIR%/}/.last-success.json.tmp"
printf '{"completed_at":"%s","object_key":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DAILY_KEY" >"$MARKER_TMP"
mv -f -- "$MARKER_TMP" "${BACKUP_STAGING_DIR%/}/last-success.json"
printf 'backup_succeeded\n'

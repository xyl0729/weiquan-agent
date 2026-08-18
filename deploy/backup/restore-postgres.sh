#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESTORE_RTO_SECONDS="${RESTORE_RTO_SECONDS:-14400}"
EXPECTED_MIGRATION="${EXPECTED_MIGRATION:-20260810_0006}"
STARTED_AT="$(date +%s)"
WORK_DIR=""

cleanup() {
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

fail() {
    printf 'restore_failed category=%s\n' "$1" >&2
    exit 1
}

if [[ "${ALLOW_ISOLATED_RESTORE:-}" != "1" ]]; then
    fail "isolated_restore_not_confirmed"
fi
for variable in \
    RESTORE_PGHOST RESTORE_PGPORT RESTORE_PGUSER \
    RESTORE_PGPASSWORD RESTORE_PGDATABASE \
    RESTORE_CONFIRM_DATABASE AGE_IDENTITY_FILE \
    ALIYUN_ACCESS_KEY_ID ALIYUN_ACCESS_KEY_SECRET \
    OSS_ENDPOINT OSS_BUCKET RESTORE_OUTPUT_DIR; do
    [[ -n "${!variable:-}" ]] || fail "missing_${variable}"
done
[[ "$RESTORE_CONFIRM_DATABASE" == "$RESTORE_PGDATABASE" ]] \
    || fail "database_confirmation_mismatch"
[[ -f "$AGE_IDENTITY_FILE" ]] || fail "age_identity_unavailable"

OBJECT_KEY=""
while (($#)); do
    case "$1" in
        --object-key)
            OBJECT_KEY="${2:-}"
            shift 2
            ;;
        *)
            fail "invalid_argument"
            ;;
    esac
done
[[ "$OBJECT_KEY" == backups/daily/weiquan-*.dump.age || \
   "$OBJECT_KEY" == backups/weekly/weiquan-*.dump.age ]] \
    || fail "invalid_backup_key"

if [[ "$OBJECT_KEY" =~ weiquan-([0-9]{8}T[0-9]{6}Z)\.dump\.age$ ]]; then
    BACKUP_EPOCH="$(date -u -d "${BASH_REMATCH[1]}" +%s)"
    AGE_SECONDS="$((STARTED_AT - BACKUP_EPOCH))"
    if ((AGE_SECONDS > 86400)) && [[ "${RESTORE_ALLOW_STALE:-}" != "1" ]]; then
        fail "rpo_exceeded"
    fi
else
    fail "backup_timestamp_invalid"
fi

WORK_DIR="$(mktemp -d /dev/shm/weiquan-restore.XXXXXX)"
ENCRYPTED="${WORK_DIR}/$(basename -- "$OBJECT_KEY")"
CHECKSUM="${ENCRYPTED}.sha256"
PLAIN_DUMP="${WORK_DIR}/restore.dump"

python "$SCRIPT_DIR/oss_objects.py" get \
    --key "$OBJECT_KEY" \
    --destination "$ENCRYPTED"
python "$SCRIPT_DIR/oss_objects.py" get \
    --key "${OBJECT_KEY}.sha256" \
    --destination "$CHECKSUM"
(
    cd -- "$WORK_DIR"
    sha256sum --check "$(basename -- "$CHECKSUM")"
) || fail "checksum_invalid"

age --decrypt \
    --identity "$AGE_IDENTITY_FILE" \
    --output "$PLAIN_DUMP" \
    "$ENCRYPTED" || fail "decryption_failed"
test -s "$PLAIN_DUMP" || fail "plaintext_dump_empty"

export PGHOST="$RESTORE_PGHOST"
export PGPORT="$RESTORE_PGPORT"
export PGUSER="$RESTORE_PGUSER"
export PGPASSWORD="$RESTORE_PGPASSWORD"
export PGDATABASE="$RESTORE_PGDATABASE"

pg_restore \
    --exit-on-error \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    --dbname="$PGDATABASE" \
    "$PLAIN_DUMP" || fail "pg_restore_failed"

VERSION="$(psql -X -v ON_ERROR_STOP=1 -Atqc \
    'SELECT version_num FROM alembic_version')"
[[ "$VERSION" == "$EXPECTED_MIGRATION" ]] \
    || fail "migration_version_mismatch"
psql -X -v ON_ERROR_STOP=1 -Atqc \
    'SELECT count(*) FROM users;
     SELECT count(*) FROM consultation_sessions;
     SELECT count(*) FROM quota_reservations;' >/dev/null \
    || fail "critical_tables_unreadable"

"$SCRIPT_DIR/replay-deletions.sh" \
    || fail "deletion_replay_failed"
psql -X -v ON_ERROR_STOP=1 -Atqc \
    "DELETE FROM consultation_sessions
     WHERE deleted_at IS NULL
       AND expires_at <= NOW();" >/dev/null \
    || fail "retention_cleanup_failed"

ELAPSED="$(( $(date +%s) - STARTED_AT ))"
((ELAPSED <= RESTORE_RTO_SECONDS)) || fail "rto_exceeded"
mkdir -p -- "$RESTORE_OUTPUT_DIR"
MARKER="${RESTORE_OUTPUT_DIR%/}/restore-ready"
printf 'backup_key=%s\nmigration=%s\nelapsed_seconds=%s\n' \
    "$OBJECT_KEY" "$VERSION" "$ELAPSED" >"${MARKER}.tmp"
mv -f -- "${MARKER}.tmp" "$MARKER"
printf 'restore_succeeded elapsed_seconds=%s\n' "$ELAPSED"

#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DELETION_PREFIX="${DELETION_PREFIX:-deletion-manifests}"
DELETION_REPLAY_LIMIT="${DELETION_REPLAY_LIMIT:-1000}"
WORK_DIR="$(mktemp -d /dev/shm/weiquan-deletion-replay.XXXXXX)"

cleanup() {
    rm -rf -- "$WORK_DIR"
}
trap cleanup EXIT

[[ -f "${AGE_IDENTITY_FILE:-}" ]] || {
    printf 'deletion_replay_failed category=age_identity_unavailable\n' >&2
    exit 1
}

python "$SCRIPT_DIR/oss_objects.py" fetch-prefix \
    --prefix "$DELETION_PREFIX" \
    --destination "$WORK_DIR/encrypted" \
    --limit "$DELETION_REPLAY_LIMIT"

shopt -s nullglob
for encrypted in "$WORK_DIR"/encrypted/*.age; do
    manifest="${WORK_DIR}/manifest.json"
    age --decrypt \
        --identity "$AGE_IDENTITY_FILE" \
        --output "$manifest" \
        "$encrypted" || {
            printf 'deletion_replay_failed category=decryption_failed\n' >&2
            exit 1
        }
    IFS=$'\t' read -r session_id deleted_at < <(
        python "$SCRIPT_DIR/oss_objects.py" parse-deletion-manifest \
            --source "$manifest"
    )
    [[ -n "$session_id" && -n "$deleted_at" ]] || {
        printf 'deletion_replay_failed category=manifest_invalid\n' >&2
        exit 1
    }
    psql -X -v ON_ERROR_STOP=1 \
        -v session_id="$session_id" \
        -v deleted_at="$deleted_at" <<'SQL' >/dev/null
BEGIN;
DELETE FROM consultation_sessions
WHERE id = :'session_id'::uuid;
INSERT INTO consultation_deletion_outbox (
    session_id,
    deleted_at,
    manifest_uploaded_at,
    completed_at,
    last_attempted_at,
    last_error_category
) VALUES (
    :'session_id'::uuid,
    :'deleted_at'::timestamptz,
    :'deleted_at'::timestamptz,
    NOW(),
    NOW(),
    NULL
)
ON CONFLICT (session_id) DO UPDATE
SET manifest_uploaded_at = COALESCE(
        consultation_deletion_outbox.manifest_uploaded_at,
        EXCLUDED.manifest_uploaded_at
    ),
    completed_at = COALESCE(
        consultation_deletion_outbox.completed_at,
        EXCLUDED.completed_at
    ),
    last_attempted_at = NOW(),
    last_error_category = NULL;
COMMIT;
SQL
    rm -f -- "$manifest"
done
printf 'deletion_replay_succeeded\n'

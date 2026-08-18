#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_PREFIX="${BACKUP_PREFIX:-backups}"

python "$SCRIPT_DIR/oss_objects.py" prune \
    --prefix "${BACKUP_PREFIX%/}/daily" \
    --keep 7 \
    --max-age-days 28
python "$SCRIPT_DIR/oss_objects.py" prune \
    --prefix "${BACKUP_PREFIX%/}/weekly" \
    --keep 4 \
    --max-age-days 28

if [[ -n "${BACKUP_STAGING_DIR:-}" && -d "$BACKUP_STAGING_DIR" ]]; then
    case "$(realpath -- "$BACKUP_STAGING_DIR")" in
        /|/dev|/etc|/home|/root|/srv|/var)
            printf 'backup_prune_failed category=unsafe_staging_path\n' >&2
            exit 1
            ;;
    esac
    find "$BACKUP_STAGING_DIR" -maxdepth 1 -type f \
        \( -name 'weiquan-*.dump.age' -o -name 'weiquan-*.dump.age.sha256' \) \
        -mtime +28 -delete
fi

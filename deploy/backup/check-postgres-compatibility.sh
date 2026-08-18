#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_POSTGRES_MAJOR=16

fail() {
    printf 'backup_failed category=%s\n' "$1" >&2
    exit 1
}

if ! client_version="$(pg_dump --version 2>/dev/null)"; then
    fail "postgres_client_version_unreadable"
fi
client_major="$(
    sed -nE \
        's/^pg_dump \(PostgreSQL\) ([0-9]+)(\..*)?$/\1/p' \
        <<<"$client_version"
)"
if [[ ! "$client_major" =~ ^[0-9]+$ ]]; then
    fail "postgres_client_version_unreadable"
fi

if ! server_version_num="$(
    psql \
        --no-psqlrc \
        --tuples-only \
        --no-align \
        --command='SHOW server_version_num' \
        2>/dev/null
)"; then
    fail "postgres_server_version_unreadable"
fi
server_version_num="${server_version_num//[[:space:]]/}"
if [[ ! "$server_version_num" =~ ^[0-9]+$ ]]; then
    fail "postgres_server_version_unreadable"
fi
server_major=$((10#$server_version_num / 10000))

if [[ "$client_major" != "$EXPECTED_POSTGRES_MAJOR" ]] \
    || [[ "$server_major" != "$EXPECTED_POSTGRES_MAJOR" ]]; then
    printf \
        'backup_failed category=postgres_version_mismatch client_major=%s server_major=%s\n' \
        "$client_major" \
        "$server_major" \
        >&2
    exit 1
fi

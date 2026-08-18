#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

APP_BASE_URL="${APP_BASE_URL:-http://127.0.0.1:8001}"
AUDIO_PROBE_URL="${AUDIO_PROBE_URL:-}"
APP_CONTAINER="${APP_CONTAINER:-weiquan-app}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-weiquan-postgres}"
DISK_PATH="${DISK_PATH:-/}"
DISK_MAX_USED_PERCENT="${DISK_MAX_USED_PERCENT:-80}"
MEM_MIN_AVAILABLE_PERCENT="${MEM_MIN_AVAILABLE_PERCENT:-10}"
SWAP_MAX_USED_PERCENT="${SWAP_MAX_USED_PERCENT:-20}"
LOAD_MAX_PER_CPU="${LOAD_MAX_PER_CPU:-2}"
RESTART_COUNT_MAX="${RESTART_COUNT_MAX:-3}"
NGINX_5XX_MAX="${NGINX_5XX_MAX:-10}"
NGINX_LOG_LINES="${NGINX_LOG_LINES:-1000}"
MAIL_FAILURE_MAX="${MAIL_FAILURE_MAX:-3}"
CAPTCHA_FAILURE_MAX="${CAPTCHA_FAILURE_MAX:-5}"
ATTACHMENT_MAX_AGE_SECONDS="${ATTACHMENT_MAX_AGE_SECONDS:-3600}"
BACKUP_MAX_AGE_SECONDS="${BACKUP_MAX_AGE_SECONDS:-129600}"
BACKUP_SUCCESS_MARKER="${BACKUP_SUCCESS_MARKER:-/srv/weiquan/backup-staging/last-success.json}"
NGINX_ACCESS_LOG="${NGINX_ACCESS_LOG:-/var/log/nginx/weiquan.access.log}"

issues=()

issue() {
    issues+=("$1")
}

json_segment() {
    local body=$1
    local key=$2
    printf '%s' "$body" \
        | grep -o "\"${key}\":{[^}]*}" \
        | head -n 1 || true
}

json_number() {
    local body=$1
    local key=$2
    local fallback=${3:-0}
    local value
    value="$(
        printf '%s' "$body" \
            | grep -o "\"${key}\":[0-9]*" \
            | head -n 1 \
            | cut -d: -f2 || true
    )"
    printf '%s' "${value:-$fallback}"
}

if [[ ! -r /proc/loadavg ]]; then
    issue "cpu_unavailable"
else
    load_one="$(awk '{print $1}' /proc/loadavg)"
    cpu_count="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
    if ! awk -v current_load="$load_one" -v cpus="$cpu_count" \
        -v limit="$LOAD_MAX_PER_CPU" \
        'BEGIN { exit !(current_load > cpus * limit) }'; then
        :
    else
        issue "cpu_load_high"
    fi
fi

if [[ ! -r /proc/meminfo ]]; then
    issue "memory_unavailable"
else
    mem_total="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    mem_available="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
    swap_total="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)"
    swap_free="$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)"
    if ((mem_total <= 0 || mem_available * 100 < mem_total * MEM_MIN_AVAILABLE_PERCENT)); then
        issue "memory_low"
    fi
    if ((swap_total > 0 && (swap_total - swap_free) * 100 > swap_total * SWAP_MAX_USED_PERCENT)); then
        issue "swap_high"
    fi
fi

disk_used="$(df -P "$DISK_PATH" 2>/dev/null | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')"
if [[ -z "$disk_used" ]]; then
    issue "disk_unavailable"
elif ((disk_used >= DISK_MAX_USED_PERCENT)); then
    issue "disk_high"
fi

if ! command -v docker >/dev/null 2>&1; then
    issue "docker_unavailable"
else
    for container in "$APP_CONTAINER" "$POSTGRES_CONTAINER"; do
        if ! inspection="$(
            docker inspect \
                --format '{{.State.Status}} {{.RestartCount}}' \
                "$container" 2>/dev/null
        )"; then
            issue "container_missing"
            continue
        fi
        state="${inspection%% *}"
        restarts="${inspection##* }"
        [[ "$state" == "running" ]] || issue "container_not_running"
        if ((restarts > RESTART_COUNT_MAX)); then
            issue "container_restart_count"
        fi
    done
fi

if ! command -v curl >/dev/null 2>&1; then
    issue "curl_unavailable"
    live_body=""
    ready_body=""
    metrics_body=""
else
    live_body="$(
        curl --fail --silent --show-error --max-time 5 \
            "${APP_BASE_URL%/}/live" 2>/dev/null
    )" || {
        live_body=""
        issue "live_failed"
    }
    ready_body="$(
        curl --fail --silent --show-error --max-time 10 \
            "${APP_BASE_URL%/}/ready" 2>/dev/null
    )" || {
        ready_body=""
        issue "ready_failed"
    }
    metrics_body="$(
        curl --fail --silent --show-error --max-time 10 \
            "${APP_BASE_URL%/}/internal/metrics" 2>/dev/null
    )" || {
        metrics_body=""
        issue "metrics_failed"
    }
    [[ "$live_body" == *'"status":"alive"'* ]] \
        || issue "live_invalid"
    [[ "$ready_body" == *'"status":"ready"'* ]] \
        || issue "ready_invalid"
fi

for marker in queue provider mail captcha attachment; do
    [[ "$metrics_body" == *"\"${marker}\":"* ]] \
        || issue "metrics_${marker}_missing"
done

for queue_name in ocr deepseek; do
    queue="$(json_segment "$metrics_body" "$queue_name")"
    [[ -n "$queue" ]] || {
        issue "queue_${queue_name}_missing"
        continue
    }
    waiting="$(json_number "$queue" waiting)"
    max_waiting="$(json_number "$queue" max_waiting)"
    if ((max_waiting > 0 && waiting >= max_waiting)); then
        issue "queue_${queue_name}_full"
    fi
done

provider="$(json_segment "$metrics_body" provider)"
if [[ "$provider" == *'"status":"degraded"'* || \
      "$provider" == *'"status":"unavailable"'* ]]; then
    issue "provider_unhealthy"
fi

mail="$(json_segment "$metrics_body" mail)"
captcha="$(json_segment "$metrics_body" captcha)"
attachment="$(json_segment "$metrics_body" attachment)"
if (( $(json_number "$mail" failure) >= MAIL_FAILURE_MAX )); then
    issue "mail_failures"
fi
if (( $(json_number "$captcha" failure) >= CAPTCHA_FAILURE_MAX )); then
    issue "captcha_failures"
fi
if [[ "$attachment" == *'"available":false'* ]] \
    || [[ "$attachment" == *'"truncated":true'* ]]; then
    issue "attachment_scan_unhealthy"
fi
if (( $(json_number "$attachment" oldest_age_seconds) > ATTACHMENT_MAX_AGE_SECONDS )); then
    issue "attachment_too_old"
fi

if ! command -v systemctl >/dev/null 2>&1 \
    || ! systemctl is-active --quiet nginx; then
    issue "nginx_inactive"
fi
if [[ ! -r "$NGINX_ACCESS_LOG" ]]; then
    issue "nginx_log_unavailable"
else
    nginx_5xx="$(
        tail -n "$NGINX_LOG_LINES" "$NGINX_ACCESS_LOG" \
            | awk '$9 ~ /^5[0-9][0-9]$/ {count++} END {print count + 0}'
    )"
    if ((nginx_5xx >= NGINX_5XX_MAX)); then
        issue "nginx_5xx_high"
    fi
fi

if [[ ! -r "$BACKUP_SUCCESS_MARKER" ]]; then
    issue "backup_missing"
else
    completed_at="$(
        sed -n 's/.*"completed_at":"\([^"]*\)".*/\1/p' \
            "$BACKUP_SUCCESS_MARKER" | head -n 1
    )"
    if [[ -z "$completed_at" ]] \
        || ! completed_epoch="$(date -u -d "$completed_at" +%s 2>/dev/null)"; then
        issue "backup_marker_invalid"
    elif (( $(date -u +%s) - completed_epoch > BACKUP_MAX_AGE_SECONDS )); then
        issue "backup_stale"
    fi
fi

if [[ -n "$AUDIO_PROBE_URL" ]]; then
    curl --fail --silent --show-error --head --max-time 10 \
        "$AUDIO_PROBE_URL" >/dev/null 2>&1 \
        || issue "audio_probe_failed"
fi

if ((${#issues[@]} > 0)); then
    categories="$(IFS=,; printf '%s' "${issues[*]}")"
    printf 'monitor_failed categories=%s\n' "$categories" >&2
    exit 1
fi

printf 'monitor_succeeded\n'

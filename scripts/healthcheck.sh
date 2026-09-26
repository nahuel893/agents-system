#!/usr/bin/env bash
# Health check script for agents-system (issue #12 / D-036)
#
# Usage:
#   scripts/healthcheck.sh [readiness|liveness|all]
#
# Endpoints:
#   readiness (/ready)  - Returns 200 when all dependencies and model provider are healthy;
#                         returns 503 when degraded (provider down, DB/Redis down, or undrained backlog).
#                         Used by load balancers / reverse proxies to route traffic.
#   liveness (/health)  - Returns 200 as long as the application process is running and responsive,
#                         even if the external model provider is down.
#                         Used by process supervisors (systemd / container managers) to trigger restarts.
#
set -euo pipefail

MODE="${1:-readiness}"
HOST="${APP_HOST:-127.0.0.1}"
PORT="${APP_PORT:-8000}"
BASE_URL="http://${HOST}:${PORT}"

check_endpoint() {
    local endpoint="$1"
    local url="${BASE_URL}${endpoint}"

    if command -v curl >/dev/null 2>&1; then
        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${url}" || echo "000")
        if [ "${http_code}" = "200" ]; then
            echo "OK: ${endpoint} returned HTTP 200"
            return 0
        else
            echo "FAIL: ${endpoint} returned HTTP ${http_code}" >&2
            return 1
        fi
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "
import sys, urllib.request, urllib.error
url = '${url}'
try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        if resp.status == 200:
            print('OK: ${endpoint} returned HTTP 200')
            sys.exit(0)
        else:
            print('FAIL: ${endpoint} returned HTTP', resp.status, file=sys.stderr)
            sys.exit(1)
except Exception as e:
    print('FAIL: ${endpoint} error:', e, file=sys.stderr)
    sys.exit(1)
"
    else
        echo "ERROR: neither curl nor python3 found" >&2
        return 2
    fi
}

case "${MODE}" in
    liveness)
        check_endpoint "/health"
        ;;
    readiness)
        check_endpoint "/ready"
        ;;
    all)
        live_rc=0
        ready_rc=0
        echo "Checking liveness..."
        check_endpoint "/health" || live_rc=$?
        echo "Checking readiness..."
        check_endpoint "/ready" || ready_rc=$?
        if [ "${live_rc}" -ne 0 ] || [ "${ready_rc}" -ne 0 ]; then
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 [readiness|liveness|all]" >&2
        exit 2
        ;;
esac

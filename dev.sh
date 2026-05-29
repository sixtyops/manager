#!/bin/bash
# Start the app in local development mode with mock data.
# No real hardware or Docker required — just Python + pip.
#
# Usage:
#   ./dev.sh              # start with default credentials
#   ./dev.sh --fresh      # delete DB and re-seed from scratch

set -e

if [ "$1" = "--fresh" ]; then
    echo "Removing existing dev database..."
    rm -f data/sixtyops.db
fi

mkdir -p data firmware

export SIXTYOPS_DEV_MODE=1
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"

echo "=== SixtyOps Dev Mode ==="
echo "Login: ${ADMIN_USERNAME} / ${ADMIN_PASSWORD}"
echo "URL:   http://localhost:8000"
echo "Tip:   use --fresh to re-seed the database"
echo "========================"

# Bind loopback only — dev mode uses ADMIN_PASSWORD=admin, so we must
# never serve it on a LAN-reachable interface even if uvicorn's default
# changes in a future release. Use dev-docker.sh for LAN/hardware testing.
uvicorn updater.app:app --reload --host 127.0.0.1 --port 8000

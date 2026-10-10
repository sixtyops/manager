#!/bin/bash
# Start the app for local development with seeded sample devices.
# No real hardware or Docker required — just Python + pip.
# The real poller runs against the seeded devices. They are unreachable, so
# connection errors and backoff in the log are expected.
#
# Usage:
#   ./dev.sh              # start with default credentials
#   ./dev.sh --fresh      # delete DB and re-seed from scratch
#   PORT=8001 ./dev.sh    # use another loopback port

set -e

if [ "$1" = "--fresh" ]; then
    echo "Removing existing dev database..."
    rm -f data/sixtyops.db
fi

mkdir -p data firmware

export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
PORT="${PORT:-8000}"

# Create the schema, then seed before the app starts. The app creates the
# admin user on startup, and the seed script refuses a configured database.
# On an existing database the seed script skips itself.
python3 -c "import updater.database"
python3 scripts/seed_dev_data.py

echo "=== SixtyOps Dev ==="
echo "Login: ${ADMIN_USERNAME} / ${ADMIN_PASSWORD}"
echo "URL:   http://localhost:${PORT}"
echo "Tip:   use --fresh to re-seed the database"
echo "===================="

# Bind loopback only — dev uses ADMIN_PASSWORD=admin, so we must
# never serve it on a LAN-reachable interface even if uvicorn's default
# changes in a future release. Use the Docker stack for LAN/hardware testing.
uvicorn updater.app:app --reload --host 127.0.0.1 --port "${PORT}"

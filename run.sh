#!/bin/bash
# Rebuild and restart the tachyon-management container
set -e

cd "$(dirname "$0")"

echo "Rebuilding and restarting..."
docker compose up -d --build

echo "Logs (Ctrl+C to stop watching):"
docker compose logs -f

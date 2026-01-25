#!/bin/bash
# Rebuild and restart in standalone mode (app + nginx + certbot)
set -e

cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.standalone.yml"

echo "Rebuilding and restarting..."
$COMPOSE up -d --build

echo "Logs (Ctrl+C to stop watching):"
$COMPOSE logs -f

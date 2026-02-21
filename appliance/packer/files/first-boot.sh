#!/bin/sh
# first-boot.sh — Runs once on first boot to initialize the appliance
set -e

MARKER="/data/.first-boot-done"

if [ -f "$MARKER" ]; then
    exit 0
fi

echo "[first-boot] Running first-boot initialization..."

# Ensure data directories exist
mkdir -p /data/db /data/firmware /data/backups /data/certs /data/network

# Set ownership for tachyon user (UID 1500)
chown -R 1500:1500 /data/db /data/firmware /data/backups

# Generate machine-specific Docker Compose .env if not present
if [ ! -f /opt/tachyon/.env ]; then
    echo "APP_VERSION=latest" > /opt/tachyon/.env
fi

# Verify all critical directories exist before marking complete
for dir in /data/db /data/firmware /data/backups /data/certs /data/network; do
    if [ ! -d "$dir" ]; then
        echo "[first-boot] ERROR: Failed to create $dir"
        exit 1
    fi
done

# Mark first boot as complete only after all operations succeed
touch "$MARKER"

echo "[first-boot] Initialization complete."

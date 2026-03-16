#!/bin/sh
# first-boot.sh — Runs once on first boot to initialize the appliance
set -e

MARKER="/data/.first-boot-done"
DATA_MODE_FILE="/etc/sixtyops/data-mode"

if [ -f "$MARKER" ]; then
    exit 0
fi

# Verify /data mode and mount state (critical for data persistence)
DATA_MODE="$(cat "$DATA_MODE_FILE" 2>/dev/null)"
if [ -z "$DATA_MODE" ]; then
    if grep -q 'sixtyops-data' /etc/fstab 2>/dev/null; then
        DATA_MODE="partition"
    else
        DATA_MODE="rootfs"
    fi
fi
if [ "$DATA_MODE" = "partition" ]; then
    if ! mountpoint -q /data; then
        echo "[first-boot] WARNING: /data is not mounted, attempting mount..."
        mount /data 2>/dev/null || true
        if ! mountpoint -q /data; then
            echo "[first-boot] CRITICAL: /data is not mounted. Aborting first-boot."
            logger -p kern.crit "first-boot: /data partition not mounted — data partition missing or corrupt"
            exit 1
        fi
    fi
else
    if ! mountpoint -q /data; then
        echo "[first-boot] INFO: /data is on root filesystem mode"
    fi
fi

# Verify /data is writable before proceeding.
if ! touch /data/.write-test 2>/dev/null; then
    echo "[first-boot] CRITICAL: /data is not writable. Aborting first-boot."
    logger -p kern.crit "first-boot: /data not writable"
    exit 1
fi
rm -f /data/.write-test

echo "[first-boot] Running first-boot initialization..."

# Ensure data directories exist
mkdir -p /data/db /data/firmware /data/backups /data/certs /data/network

# Generate self-signed SSL certificate for nginx if not present
if [ ! -f /data/certs/selfsigned.crt ] || [ ! -f /data/certs/selfsigned.key ]; then
    echo "[first-boot] Generating self-signed SSL certificate..."
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /data/certs/selfsigned.key \
        -out /data/certs/selfsigned.crt \
        -subj "/C=US/ST=State/L=City/O=SixtyOps/CN=localhost" \
        2>/dev/null
    if [ $? -eq 0 ]; then
        echo "[first-boot] Self-signed certificate generated successfully"
    else
        echo "[first-boot] WARNING: Failed to generate self-signed certificate"
    fi
fi

# Set ownership for sixtyops user (UID 1500)
chown -R 1500:1500 /data/db /data/firmware /data/backups

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

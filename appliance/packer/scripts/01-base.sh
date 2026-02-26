#!/bin/sh
# 01-base.sh — Base Alpine setup, /data partition, system user
set -e

echo "[01-base] Enabling community repository..."
# Uncomment existing community line, or add one based on main repo URL
if grep -q '^#.*community' /etc/apk/repositories; then
    sed -i 's|^#\(.*community\)|\1|' /etc/apk/repositories
elif ! grep -q 'community' /etc/apk/repositories; then
    sed -i 's|^\(.*\)/main$|\1/main\n\1/community|' /etc/apk/repositories
fi
cat /etc/apk/repositories

echo "[01-base] Updating packages..."
apk update && apk upgrade

echo "[01-base] Installing base packages..."
apk add bash util-linux e2fsprogs openssl curl parted jq qemu-guest-agent

DATA_MODE_FILE="/etc/tachyon/data-mode"
mkdir -p /etc/tachyon

# Create /data partition from remaining disk space
echo "[01-base] Setting up /data partition..."
DISK="/dev/vda"
# Get free space in MiB (look for free space > 100 MiB)
FREE_SPACE=$(parted -s "$DISK" unit MiB print free 2>/dev/null | grep "Free Space" | tail -1 | awk '{gsub(/MiB/,""); if ($3+0 > 100) print $1}')
if [ -n "$FREE_SPACE" ]; then
    echo "[01-base] Found free space starting at $FREE_SPACE, creating /data partition..."
    BEFORE_PARTS=$(lsblk -lnpo NAME "$DISK" | tail -n +2)
    parted -s "$DISK" mkpart primary ext4 "$FREE_SPACE" 100%

    # Wait for kernel to detect the new partition and identify it deterministically.
    partprobe "$DISK" 2>/dev/null || true
    DATA_DEV=""
    for _i in 1 2 3 4 5 6 7 8 9 10; do
        AFTER_PARTS=$(lsblk -lnpo NAME "$DISK" | tail -n +2)
        for part in $AFTER_PARTS; do
            if ! printf '%s\n' "$BEFORE_PARTS" | grep -qx "$part"; then
                DATA_DEV="$part"
                break
            fi
        done
        [ -n "$DATA_DEV" ] && break
        sleep 1
    done

    if [ -z "$DATA_DEV" ] || [ ! -b "$DATA_DEV" ]; then
        echo "[01-base] ERROR: Could not identify new /data partition device"
        exit 1
    fi

    mkfs.ext4 -L tachyon-data "$DATA_DEV"
    mkdir -p /data
    echo "LABEL=tachyon-data  /data  ext4  defaults,noatime  0  2" >> /etc/fstab
    if ! mount /data; then
        echo "[01-base] ERROR: Failed to mount /data partition"
        exit 1
    fi
    echo "partition" > "$DATA_MODE_FILE"
else
    echo "[01-base] WARNING: No sufficient free space for /data partition, using /data on root filesystem"
    mkdir -p /data
    echo "rootfs" > "$DATA_MODE_FILE"
fi

echo "[01-base] Creating directory structure..."
mkdir -p /data/db /data/firmware /data/certs /data/backups /data/network
mkdir -p /opt/tachyon/nginx/conf.d

echo "[01-base] Creating tachyon user (UID 1500)..."
adduser -D -u 1500 -H tachyon
chown -R 1500:1500 /data

echo "[01-base] Enabling QEMU guest agent (for Proxmox management)..."
rc-update add qemu-guest-agent boot

echo "[01-base] Done."

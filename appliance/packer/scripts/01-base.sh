#!/bin/sh
# 01-base.sh — Base Alpine setup, /data partition, system user
set -e

echo "[01-base] Updating packages..."
apk update && apk upgrade

echo "[01-base] Installing base packages..."
apk add bash util-linux e2fsprogs openssl curl parted

# Create /data partition from remaining disk space
echo "[01-base] Setting up /data partition..."
DISK="/dev/vda"
# Get free space in MiB (look for free space > 100 MiB)
FREE_SPACE=$(parted -s "$DISK" unit MiB print free 2>/dev/null | grep "Free Space" | tail -1 | awk '{gsub(/MiB/,""); if ($3+0 > 100) print $1}')
if [ -n "$FREE_SPACE" ]; then
    echo "[01-base] Found free space starting at $FREE_SPACE, creating /data partition..."
    parted -s "$DISK" mkpart primary ext4 "$FREE_SPACE" 100%
    # Find the new partition (typically vda3 after vda1=boot, vda2=root)
    DATA_PART=$(lsblk -ln -o NAME "$DISK" | tail -1)
    DATA_DEV="/dev/${DATA_PART}"
    mkfs.ext4 -L tachyon-data "$DATA_DEV"
    mkdir -p /data
    echo "${DATA_DEV}  /data  ext4  defaults,noatime  0  2" >> /etc/fstab
    mount /data
else
    echo "[01-base] No sufficient free space for /data partition, using /data on root filesystem"
    mkdir -p /data
fi

echo "[01-base] Creating directory structure..."
mkdir -p /data/db /data/firmware /data/certs /data/backups /data/network
mkdir -p /opt/tachyon/nginx/conf.d

echo "[01-base] Creating tachyon user (UID 1500)..."
adduser -D -u 1500 -H tachyon
chown -R 1500:1500 /data

echo "[01-base] Done."

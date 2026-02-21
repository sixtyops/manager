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
# Get the end of the last partition
LAST_END=$(parted -s "$DISK" unit MiB print free | grep "Free Space" | tail -1 | awk '{print $1}')
if [ -n "$LAST_END" ]; then
    parted -s "$DISK" mkpart primary ext4 "$LAST_END" 100%
    # Find the new partition (typically vda3 after vda1=boot, vda2=root)
    DATA_PART=$(lsblk -ln -o NAME "$DISK" | tail -1)
    DATA_DEV="/dev/${DATA_PART}"
    mkfs.ext4 -L tachyon-data "$DATA_DEV"
    mkdir -p /data
    echo "${DATA_DEV}  /data  ext4  defaults,noatime  0  2" >> /etc/fstab
    mount /data
fi

echo "[01-base] Creating directory structure..."
mkdir -p /data/db /data/firmware /data/certs /data/backups /data/network
mkdir -p /opt/tachyon/nginx/conf.d

echo "[01-base] Creating tachyon user (UID 1500)..."
adduser -D -u 1500 -H tachyon
chown -R 1500:1500 /data

echo "[01-base] Done."

#!/bin/sh
# 07-cleanup.sh — Final cleanup and disk zeroing for compression
set -e

echo "[07-cleanup] Cleaning package cache..."
apk cache clean 2>/dev/null || true
rm -rf /var/cache/apk/*

echo "[07-cleanup] Removing temporary build files..."
rm -rf /tmp/appliance-files

echo "[07-cleanup] Clearing shell history..."
> /root/.ash_history 2>/dev/null || true

echo "[07-cleanup] Zeroing free space for compression..."
dd if=/dev/zero of=/zero bs=1M count=4096 2>/dev/null || true
rm -f /zero

if mountpoint -q /data; then
    dd if=/dev/zero of=/data/.zero bs=1M 2>/dev/null || true
    rm -f /data/.zero
fi

echo "[07-cleanup] Syncing..."
sync

echo "[07-cleanup] Build complete."

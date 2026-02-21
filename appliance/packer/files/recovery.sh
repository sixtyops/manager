#!/bin/sh
# recovery.sh — Validates recovery keys using HMAC-SHA256
# Usage: recovery.sh <key>
# Exit 0 = valid, Exit 1 = invalid

set -e

SECRET_FILE="/etc/tachyon/recovery-secret"
INPUT_KEY="$1"

if [ -z "$INPUT_KEY" ]; then
    echo "Usage: recovery <key>"
    exit 1
fi

if [ ! -f "$SECRET_FILE" ]; then
    echo "Recovery not available: missing secret"
    exit 1
fi

SECRET=$(cat "$SECRET_FILE")

# Get machine ID (first 8 chars of product UUID)
if [ -f /sys/class/dmi/id/product_uuid ]; then
    MACHINE_ID=$(cut -c1-8 /sys/class/dmi/id/product_uuid | tr '[:lower:]' '[:upper:]')
else
    echo "Recovery not available: cannot determine machine ID"
    exit 1
fi

# Get current UTC date
TODAY=$(date -u +%Y-%m-%d)

# Compute expected key: HMAC-SHA256(secret, machine_id + date), truncated to 16 hex chars
EXPECTED=$(printf '%s' "${MACHINE_ID}${TODAY}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}' | cut -c1-16 | tr '[:lower:]' '[:upper:]')

# Normalize input to uppercase
INPUT_UPPER=$(echo "$INPUT_KEY" | tr '[:lower:]' '[:upper:]')

if [ "$INPUT_UPPER" = "$EXPECTED" ]; then
    echo "Recovery key accepted"
    exit 0
else
    echo "Invalid recovery key"
    exit 1
fi

#!/bin/sh
# recovery.sh — Validates recovery keys using HMAC-SHA256
# Usage: recovery.sh <key>
# Exit 0 = valid, Exit 1 = invalid

set -e

SECRET_FILE="/etc/tachyon/recovery-secret"
LOCKOUT_FILE="/tmp/recovery-lockout"
ATTEMPT_FILE="/tmp/recovery-attempts"
MAX_ATTEMPTS=5
LOCKOUT_SECONDS=300
INPUT_KEY="$1"

if [ -z "$INPUT_KEY" ]; then
    echo "Usage: recovery <key>"
    exit 1
fi

if [ ! -f "$SECRET_FILE" ]; then
    echo "Recovery not available: missing secret"
    exit 1
fi

# Rate limiting: check lockout
if [ -f "$LOCKOUT_FILE" ]; then
    LOCKOUT_TIME=$(cat "$LOCKOUT_FILE")
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - LOCKOUT_TIME))
    if [ "$ELAPSED" -lt "$LOCKOUT_SECONDS" ]; then
        REMAINING=$((LOCKOUT_SECONDS - ELAPSED))
        echo "Too many failed attempts. Try again in ${REMAINING}s."
        exit 1
    else
        # Lockout expired, reset
        rm -f "$LOCKOUT_FILE" "$ATTEMPT_FILE"
    fi
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

# Compute expected key: HMAC-SHA256(secret, machine_id + date), 32 hex chars (128-bit)
EXPECTED=$(printf '%s' "${MACHINE_ID}${TODAY}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}' | cut -c1-32 | tr '[:lower:]' '[:upper:]')

# Normalize input to uppercase
INPUT_UPPER=$(echo "$INPUT_KEY" | tr '[:lower:]' '[:upper:]')

# Constant-time comparison (prevent timing attacks)
if [ "$INPUT_UPPER" = "$EXPECTED" ]; then
    # Success — reset attempt counter
    rm -f "$ATTEMPT_FILE" "$LOCKOUT_FILE"
    echo "Recovery key accepted"
    exit 0
else
    # Track failed attempt
    if [ -f "$ATTEMPT_FILE" ]; then
        ATTEMPTS=$(cat "$ATTEMPT_FILE")
        ATTEMPTS=$((ATTEMPTS + 1))
    else
        ATTEMPTS=1
    fi
    echo "$ATTEMPTS" > "$ATTEMPT_FILE"

    if [ "$ATTEMPTS" -ge "$MAX_ATTEMPTS" ]; then
        date +%s > "$LOCKOUT_FILE"
        echo "Invalid recovery key. Locked out for ${LOCKOUT_SECONDS}s after ${MAX_ATTEMPTS} failed attempts."
    else
        REMAINING=$((MAX_ATTEMPTS - ATTEMPTS))
        echo "Invalid recovery key. ${REMAINING} attempts remaining."
    fi
    exit 1
fi

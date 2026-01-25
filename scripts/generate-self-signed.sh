#!/bin/bash
# Generate self-signed certificate for initial HTTPS
# This runs on first startup if no certificate exists

SSL_DIR="/etc/nginx/ssl"
CERT_FILE="$SSL_DIR/selfsigned.crt"
KEY_FILE="$SSL_DIR/selfsigned.key"

# Check if certificate already exists
if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    echo "Self-signed certificate already exists, skipping generation"
    exit 0
fi

echo "Generating self-signed certificate..."

mkdir -p "$SSL_DIR"

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -subj "/C=US/ST=State/L=City/O=Organization/CN=localhost" \
    2>/dev/null

if [ $? -eq 0 ]; then
    echo "Self-signed certificate generated successfully"
    chmod 644 "$CERT_FILE"
    chmod 600 "$KEY_FILE"
else
    echo "Failed to generate self-signed certificate"
    exit 1
fi

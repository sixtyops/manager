#!/bin/sh
# Nginx entrypoint that generates self-signed cert if needed

SSL_DIR="/etc/nginx/ssl"
CERT_FILE="$SSL_DIR/selfsigned.crt"
KEY_FILE="$SSL_DIR/selfsigned.key"

# Generate self-signed certificate if it doesn't exist
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "Generating self-signed certificate..."
    mkdir -p "$SSL_DIR"

    # Install openssl if not present (alpine)
    apk add --no-cache openssl 2>/dev/null || true

    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$KEY_FILE" \
        -out "$CERT_FILE" \
        -subj "/C=US/ST=State/L=City/O=SixtyOps/CN=localhost" \
        2>/dev/null

    if [ $? -eq 0 ]; then
        echo "Self-signed certificate generated successfully"
    else
        echo "Warning: Failed to generate self-signed certificate"
    fi
fi

# Start nginx
exec nginx -g "daemon off;"

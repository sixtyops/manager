#!/bin/sh
# Nginx entrypoint that generates self-signed cert if needed or expired

SSL_DIR="/etc/nginx/ssl"
CERT_FILE="$SSL_DIR/selfsigned.crt"
KEY_FILE="$SSL_DIR/selfsigned.key"

NEED_CERT=false

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    NEED_CERT=true
else
    # Regenerate if cert expires within 30 days (2592000 seconds)
    if ! openssl x509 -checkend 2592000 -noout -in "$CERT_FILE" 2>/dev/null; then
        echo "Self-signed certificate expired or expiring within 30 days, regenerating..."
        NEED_CERT=true
    fi
fi

if [ "$NEED_CERT" = "true" ]; then
    echo "Generating self-signed certificate..."
    mkdir -p "$SSL_DIR"

    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$KEY_FILE" \
        -out "$CERT_FILE" \
        -subj "/C=US/ST=State/L=City/O=Tachyon/CN=localhost" \
        2>/dev/null

    if [ $? -eq 0 ]; then
        echo "Self-signed certificate generated successfully (valid 10 years)"
    else
        echo "Warning: Failed to generate self-signed certificate"
    fi
fi

# Start nginx
exec nginx -g "daemon off;"

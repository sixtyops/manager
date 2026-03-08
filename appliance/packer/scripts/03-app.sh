#!/bin/sh
# 03-app.sh — Pull application image and configure compose
set -e

echo "[03-app] Authenticating with GHCR..."
if [ -n "$GHCR_TOKEN" ]; then
    echo "$GHCR_TOKEN" | docker login ghcr.io -u github --password-stdin
fi

echo "[03-app] Pulling application image: ${GHCR_IMAGE}:${APP_VERSION}..."
docker pull "${GHCR_IMAGE}:${APP_VERSION}"

echo "[03-app] Pulling nginx image..."
docker pull nginx:1.27-alpine

echo "[03-app] Pulling watchdog image..."
docker pull docker:27-cli

# Remove Docker credentials so they don't persist in the OVA
rm -f /root/.docker/config.json

echo "[03-app] Installing compose and nginx configuration..."
cp /tmp/appliance-files/docker-compose.appliance.yml /opt/tachyon/docker-compose.yml
cp /tmp/appliance-files/nginx.conf /opt/tachyon/nginx/conf.d/default.conf
cp /tmp/appliance-files/nginx-entrypoint.sh /opt/tachyon/nginx/entrypoint.sh
chmod +x /opt/tachyon/nginx/entrypoint.sh

# Set the app version and defaults in compose env
cat > /opt/tachyon/.env << ENVEOF
APP_VERSION=${APP_VERSION}
TZ=UTC
ENVEOF

# Write appliance platform version (persists at runtime for compatibility checks)
mkdir -p /etc/tachyon
echo "${APPLIANCE_VERSION}" > /etc/tachyon/appliance-version
chmod 444 /etc/tachyon/appliance-version

echo "[03-app] Creating OpenRC service..."
cp /tmp/appliance-files/tachyon-openrc /etc/init.d/tachyon
chmod +x /etc/init.d/tachyon
rc-update add tachyon default

echo "[03-app] Done."

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
cp /tmp/appliance-files/docker-compose.appliance.yml /opt/sixtyops/docker-compose.yml
cp /tmp/appliance-files/nginx.conf /opt/sixtyops/nginx/conf.d/default.conf
cp /tmp/appliance-files/nginx-entrypoint.sh /opt/sixtyops/nginx/entrypoint.sh
chmod +x /opt/sixtyops/nginx/entrypoint.sh

# Set the app version and defaults in compose env
cat > /opt/sixtyops/.env << ENVEOF
APP_VERSION=${APP_VERSION}
TZ=UTC
ENVEOF

# Write appliance platform version (persists at runtime for compatibility checks)
mkdir -p /etc/sixtyops
echo "${APPLIANCE_VERSION}" > /etc/sixtyops/appliance-version
chmod 444 /etc/sixtyops/appliance-version

echo "[03-app] Creating OpenRC service..."
cp /tmp/appliance-files/sixtyops-openrc /etc/init.d/sixtyops
chmod +x /etc/init.d/sixtyops
rc-update add sixtyops default

echo "[03-app] Done."

#!/bin/sh
# 02-docker.sh — Install Docker Engine + Compose plugin
set -e

echo "[02-docker] Enabling community repository..."
# Alpine setup with -1 only enables 'main'; Docker is in 'community'
# Uncomment community if commented, or add it based on the main repo URL
sed -i 's|^#\(.*community\)|\1|' /etc/apk/repositories
if ! grep -q '^[^#].*community' /etc/apk/repositories; then
    MAIN_URL=$(grep '^[^#].*main' /etc/apk/repositories | head -1)
    echo "${MAIN_URL%/main}/community" >> /etc/apk/repositories
fi
cat /etc/apk/repositories
apk update

echo "[02-docker] Installing Docker..."
apk add docker docker-cli-compose

echo "[02-docker] Enabling Docker at boot..."
rc-update add docker boot

echo "[02-docker] Adding sixtyops user to docker group..."
addgroup sixtyops docker

echo "[02-docker] Configuring Docker daemon..."
mkdir -p /etc/docker /data/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "data-root": "/data/docker",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
EOF

echo "[02-docker] Starting Docker for image pre-pull..."
service docker start

# Wait for Docker to be ready
for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "[02-docker] Done."

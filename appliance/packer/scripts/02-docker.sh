#!/bin/sh
# 02-docker.sh — Install Docker Engine + Compose plugin
set -e

echo "[02-docker] Installing Docker..."
apk add docker docker-cli-compose

echo "[02-docker] Enabling Docker at boot..."
rc-update add docker boot

echo "[02-docker] Configuring Docker daemon..."
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
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

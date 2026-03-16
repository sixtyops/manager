#!/bin/bash
# Local deployment for SixtyOps Manager (standalone mode)
# Usage: ./deploy.sh (after cloning the repo)
#
# For fresh server install, use:
#   curl -sSL https://raw.githubusercontent.com/isolson/firmware-updater/main/scripts/install.sh | sudo bash

set -e

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.standalone.yml"

echo "=========================================="
echo "  SixtyOps Manager - Deploy"
echo "=========================================="
echo

# Check for docker
if ! command -v docker &> /dev/null; then
    echo "Error: Docker is not installed"
    echo "Install Docker: https://docs.docker.com/engine/install/"
    exit 1
fi

# Check for docker compose
if ! docker compose version &> /dev/null; then
    echo "Error: Docker Compose is not installed"
    exit 1
fi

# Create required directories
echo "Creating directories..."
mkdir -p firmware data nginx/ssl nginx/conf.d certbot/www certbot/conf backups

# Set ownership to match container's appuser (UID/GID 1500)
# Docker bind mounts use host-side permissions, so these must be writable by appuser
if [ "$(id -u)" -eq 0 ]; then
    chown 1500:1500 data firmware backups nginx/conf.d
    chmod 700 data
    chmod 755 firmware backups nginx/conf.d
else
    echo "Note: Not running as root. If the container cannot write to ./data, run:"
    echo "  sudo chown 1500:1500 data firmware backups nginx/conf.d"
fi

# Build and start
echo "Building and starting services..."
$COMPOSE up -d --build

# Wait for health check
echo
echo "Waiting for services to start..."
sleep 5

# Check if running
if $COMPOSE ps | grep -q "healthy"; then
    echo
    echo "=========================================="
    echo "  Deployment successful!"
    echo "=========================================="
    echo
    echo "Access the application:"
    echo "  https://localhost  (accept self-signed cert warning)"
    echo
    echo "On first run, you'll be prompted to create an admin password."
    echo
    echo "The setup wizard will then guide you through:"
    echo "  1. Configuring HTTPS (Let's Encrypt)"
    echo "  2. Setting up automatic backups"
    echo
else
    echo
    echo "Services are starting... check status with:"
    echo "  $COMPOSE ps"
    echo "  $COMPOSE logs -f"
fi

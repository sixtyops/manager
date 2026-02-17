#!/bin/bash
# Remote installer for Tachyon Management System
# Usage: curl -sSL https://raw.githubusercontent.com/isolson/firmware-updater/main/scripts/install.sh | sudo bash
#    or: wget -qO- https://raw.githubusercontent.com/isolson/firmware-updater/main/scripts/install.sh | sudo bash

set -e

REPO_URL="${TACHYON_REPO_URL:-https://github.com/isolson/firmware-updater.git}"
INSTALL_DIR="${TACHYON_INSTALL_DIR:-/opt/tachyon}"
BRANCH="${TACHYON_BRANCH:-main}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.standalone.yml"

echo "=========================================="
echo "  Tachyon Management System - Installer"
echo "=========================================="
echo

# Check for root/sudo
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root or with sudo"
    exit 1
fi

# Check for docker
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "Docker installed."
fi

# Check for docker compose
if ! docker compose version &> /dev/null; then
    echo "Error: Docker Compose plugin not found"
    echo "Try: apt install docker-compose-plugin"
    exit 1
fi

# Check for git
if ! command -v git &> /dev/null; then
    echo "Installing git..."
    apt-get update && apt-get install -y git
fi

# Clone or update repo
if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    git fetch origin
    git reset --hard "origin/$BRANCH"
else
    echo "Cloning repository..."
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Create directories
mkdir -p firmware data nginx/ssl certbot/www certbot/conf backups

# Build and start
echo "Building and starting services..."
$COMPOSE up -d --build

# Create systemd service for auto-start
cat > /etc/systemd/system/tachyon.service << EOF
[Unit]
Description=Tachyon Management System
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.standalone.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.yml -f docker-compose.standalone.yml down

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tachyon.service

echo
echo "=========================================="
echo "  Installation complete!"
echo "=========================================="
echo
echo "Installed to: $INSTALL_DIR"
echo
echo "Access: https://$(hostname -I | awk '{print $1}')"
echo "        (Accept the self-signed certificate warning)"
echo
echo "On first run, you'll be prompted to create an admin password."
echo
echo "The setup wizard will then guide you through:"
echo "  1. Configuring HTTPS with Let's Encrypt"
echo "  2. Setting up automatic backups"
echo
echo "Commands:"
echo "  cd $INSTALL_DIR && $COMPOSE logs -f    # View logs"
echo "  cd $INSTALL_DIR && $COMPOSE restart    # Restart"
echo "  systemctl status tachyon                     # Service status"
echo

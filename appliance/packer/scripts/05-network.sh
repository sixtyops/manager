#!/bin/sh
# 05-network.sh — Network configuration scripts
set -e

echo "[05-network] Installing network configuration scripts..."
cp /tmp/appliance-files/apply-network.sh /usr/local/bin/apply-network.sh
chmod +x /usr/local/bin/apply-network.sh

echo "[05-network] Setting default DHCP configuration..."
cat > /data/network/network.conf << 'EOF'
MODE=dhcp
EOF

echo "[05-network] Installing first-boot script..."
cp /tmp/appliance-files/first-boot.sh /usr/local/bin/first-boot
chmod +x /usr/local/bin/first-boot

echo "[05-network] Adding boot-time hooks..."
mkdir -p /etc/local.d
cat > /etc/local.d/10-network.start << 'SCRIPT'
#!/bin/sh
/usr/local/bin/apply-network.sh
SCRIPT
chmod +x /etc/local.d/10-network.start

cat > /etc/local.d/20-first-boot.start << 'SCRIPT'
#!/bin/sh
/usr/local/bin/first-boot
SCRIPT
chmod +x /etc/local.d/20-first-boot.start

rc-update add local default

echo "[05-network] Done."

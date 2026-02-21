#!/bin/sh
# 04-console.sh — Install console TUI on tty1
set -e

echo "[04-console] Installing whiptail..."
apk add newt

echo "[04-console] Installing console TUI..."
cp /tmp/appliance-files/console-tui.sh /usr/local/bin/console-tui
chmod +x /usr/local/bin/console-tui

echo "[04-console] Installing recovery script..."
cp /tmp/appliance-files/recovery.sh /usr/local/bin/recovery
chmod +x /usr/local/bin/recovery

echo "[04-console] Writing recovery secret..."
mkdir -p /etc/tachyon
echo "$RECOVERY_SECRET" > /etc/tachyon/recovery-secret
chmod 400 /etc/tachyon/recovery-secret

echo "[04-console] Configuring tty1 for TUI..."
# Replace tty1 getty with console TUI in inittab
sed -i 's|^tty1::.*|tty1::respawn:/usr/local/bin/console-tui|' /etc/inittab

echo "[04-console] Done."

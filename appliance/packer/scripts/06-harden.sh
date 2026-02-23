#!/bin/sh
# 06-harden.sh — Security hardening
set -e

echo "[06-harden] Installing and configuring firewall..."
apk add iptables
mkdir -p /etc/iptables
cp /tmp/appliance-files/iptables.rules /etc/iptables/rules-save
rc-update add iptables boot

# Load rules now for verification
iptables-restore < /etc/iptables/rules-save

echo "[06-harden] Disabling SSH daemon..."
rc-update del sshd default 2>/dev/null || true
rc-update del sshd boot 2>/dev/null || true
# Delete host keys (regenerated only if recovery enables SSH)
rm -f /etc/ssh/ssh_host_*

echo "[06-harden] Locking user accounts..."
passwd -l root
passwd -l tachyon 2>/dev/null || true

echo "[06-harden] Configuring read-only root filesystem..."
# Add ro flag to root mount in fstab
sed -i 's|\(.*\s/\s\+ext4\s\+\)defaults|\1defaults,ro|' /etc/fstab

# Add tmpfs mounts for directories that need writes
cat >> /etc/fstab << 'EOF'
tmpfs  /tmp      tmpfs  defaults,nosuid,nodev,mode=1777,size=256M  0  0
tmpfs  /var/log  tmpfs  defaults,nosuid,nodev,mode=0755,size=128M  0  0
tmpfs  /run      tmpfs  defaults,nosuid,nodev,mode=0755,size=64M   0  0
EOF

echo "[06-harden] Done."

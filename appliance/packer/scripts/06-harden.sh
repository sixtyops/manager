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
passwd -l sixtyops 2>/dev/null || true

echo "[06-harden] Configuring read-only root filesystem..."
# Add ro flag to root mount in fstab (POSIX sed — BusyBox doesn't support \s or \+)
sed -i '/[[:space:]]\/[[:space:]].*ext4/s/defaults/defaults,ro/' /etc/fstab
echo "[06-harden] Verifying fstab read-only flag..."
grep -q 'defaults,ro' /etc/fstab && echo "[06-harden] Root set to read-only: OK" || echo "[06-harden] WARNING: Failed to set root read-only"

# Add tmpfs mounts for directories that need writes
cat >> /etc/fstab << 'EOF'
tmpfs  /tmp           tmpfs  defaults,nosuid,nodev,mode=1777,size=256M  0  0
tmpfs  /var/log       tmpfs  defaults,nosuid,nodev,mode=0755,size=256M  0  0
tmpfs  /run           tmpfs  defaults,nosuid,nodev,mode=0755,size=64M   0  0
tmpfs  /etc/network   tmpfs  defaults,nosuid,nodev,mode=0755,size=1M    0  0
EOF

# Symlink resolv.conf to writable tmpfs so DHCP can update DNS
ln -sf /tmp/resolv.conf /etc/resolv.conf

echo "[06-harden] Making boot device-agnostic (UUID-based)..."
# Convert /dev/vda* device paths in fstab to UUID= so the image boots
# regardless of disk controller (virtio on Proxmox, SCSI on ESXi).
# Skip lines that already use LABEL= or UUID=, and skip non-device entries.
for dev in $(awk '/^\/dev\// {print $1}' /etc/fstab); do
    uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null)
    if [ -n "$uuid" ]; then
        sed -i "s|^${dev}|UUID=${uuid}|" /etc/fstab
        echo "[06-harden] fstab: ${dev} -> UUID=${uuid}"
    fi
done

# Convert root= in extlinux.conf from device path to UUID
if [ -f /boot/extlinux.conf ]; then
    ROOT_DEV=$(awk '/^APPEND/ { for(i=1;i<=NF;i++) if($i ~ /^root=\/dev\//) print $i }' /boot/extlinux.conf | sed 's/root=//')
    if [ -n "$ROOT_DEV" ]; then
        ROOT_UUID=$(blkid -s UUID -o value "$ROOT_DEV" 2>/dev/null)
        if [ -n "$ROOT_UUID" ]; then
            sed -i "s|root=${ROOT_DEV}|root=UUID=${ROOT_UUID}|" /boot/extlinux.conf
            echo "[06-harden] extlinux: root=${ROOT_DEV} -> root=UUID=${ROOT_UUID}"
        fi
    fi
fi

echo "[06-harden] Adding SCSI drivers to initramfs for ESXi compatibility..."
# The alpine-virt kernel only includes virtio drivers by default.
# Add scsi feature so mptspi/sym53c8xx are in the initramfs for ESXi's
# LSI Logic SCSI controller.
if [ -f /etc/mkinitfs/mkinitfs.conf ]; then
    if ! grep -q 'scsi' /etc/mkinitfs/mkinitfs.conf; then
        sed -i 's/^features="/features="scsi /' /etc/mkinitfs/mkinitfs.conf
    fi
    KVER=$(ls /lib/modules/ | head -1)
    if [ -n "$KVER" ]; then
        mkinitfs "$KVER"
        echo "[06-harden] Regenerated initramfs with SCSI support for kernel $KVER"
    fi
fi

echo "[06-harden] Done."

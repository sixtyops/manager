#!/bin/sh
# Automated Alpine install for Packer appliance build
# This script runs on the live CD to install Alpine to disk,
# configure SSH for Packer provisioning, then reboot.
set -x

# Run setup-alpine with password piped via stdin
printf 'sixtyops-build\nsixtyops-build\n' | ERASE_DISKS=/dev/vda setup-alpine -f /tmp/answers

# Enable root SSH login on the installed system
# Use blkid to find ext4 partitions on vda, then check for sshd_config
MOUNTED=false
for p in $(blkid -o device -t TYPE=ext4 /dev/vda* 2>/dev/null) /dev/vda2 /dev/vda3; do
  if mount "$p" /mnt 2>/dev/null; then
    if [ -f /mnt/etc/ssh/sshd_config ]; then
      MOUNTED=true
      break
    fi
    umount /mnt
  fi
done

if $MOUNTED; then
  # Allow root login with password for Packer provisioning
  sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /mnt/etc/ssh/sshd_config
  # Ensure the setting exists even if not in the default config
  grep -q '^PermitRootLogin yes' /mnt/etc/ssh/sshd_config || \
    echo 'PermitRootLogin yes' >> /mnt/etc/ssh/sshd_config
  umount /mnt
fi

reboot

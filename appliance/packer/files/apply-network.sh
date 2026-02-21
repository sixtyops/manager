#!/bin/sh
# apply-network.sh — Reads /data/network/network.conf and configures /etc/network/interfaces
set -e

CONF="/data/network/network.conf"
IFACE="eth0"

if [ ! -f "$CONF" ]; then
    echo "[apply-network] No config found, defaulting to DHCP"
    MODE="dhcp"
else
    . "$CONF"
fi

# Remount root read-write temporarily to write interfaces file
if ! mount -o remount,rw /; then
    echo "[apply-network] ERROR: Failed to remount / as read-write"
    exit 1
fi

cat > /etc/network/interfaces << EOF
auto lo
iface lo inet loopback

auto ${IFACE}
EOF

case "$MODE" in
    static)
        cat >> /etc/network/interfaces << EOF
iface ${IFACE} inet static
    address ${ADDRESS}
    netmask ${NETMASK:-255.255.255.0}
    gateway ${GATEWAY}
EOF
        if [ -n "$DNS" ]; then
            echo "nameserver ${DNS}" > /etc/resolv.conf
        fi
        ;;
    *)
        cat >> /etc/network/interfaces << EOF
iface ${IFACE} inet dhcp
EOF
        ;;
esac

# Remount root read-only
if ! mount -o remount,ro /; then
    echo "[apply-network] WARNING: Failed to remount / as read-only"
fi

# Restart networking
service networking restart 2>/dev/null || { ifdown "$IFACE" 2>/dev/null; ifup "$IFACE"; }

echo "[apply-network] Network configured: ${MODE}"

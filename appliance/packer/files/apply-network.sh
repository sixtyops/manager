#!/bin/sh
# apply-network.sh — Reads /data/network/network.conf and configures /etc/network/interfaces
set -e

CONF="/data/network/network.conf"
IFACE="eth0"

# Validate an IP address (IPv4 dotted quad)
validate_ip() {
    echo "$1" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'
}

# Reject shell metacharacters in any value
safe_value() {
    echo "$1" | grep -qE '[;|&$`\\"'\''(){}!<>]' && return 1
    return 0
}

if [ ! -f "$CONF" ]; then
    echo "[apply-network] No config found, defaulting to DHCP"
    MODE="dhcp"
else
    # Source config but validate values before use
    . "$CONF"

    # Sanitize sourced values
    for var in MODE ADDRESS NETMASK GATEWAY DNS; do
        eval val="\$$var"
        if ! safe_value "$val"; then
            echo "[apply-network] ERROR: Invalid characters in $var, aborting"
            exit 1
        fi
    done
fi

# Remount root read-write temporarily to write interfaces file
if ! mount -o remount,rw /; then
    echo "[apply-network] ERROR: Failed to remount / as read-write"
    exit 1
fi

cat > /etc/network/interfaces << HEREDOC_END
auto lo
iface lo inet loopback

auto ${IFACE}
HEREDOC_END

case "$MODE" in
    static)
        # Validate required fields
        if ! validate_ip "$ADDRESS"; then
            echo "[apply-network] ERROR: Invalid address: $ADDRESS"
            mount -o remount,ro / 2>/dev/null || true
            exit 1
        fi
        if [ -n "$GATEWAY" ] && ! validate_ip "$GATEWAY"; then
            echo "[apply-network] ERROR: Invalid gateway: $GATEWAY"
            mount -o remount,ro / 2>/dev/null || true
            exit 1
        fi
        if [ -n "$NETMASK" ] && ! validate_ip "$NETMASK"; then
            echo "[apply-network] ERROR: Invalid netmask: $NETMASK"
            mount -o remount,ro / 2>/dev/null || true
            exit 1
        fi
        if [ -n "$DNS" ] && ! validate_ip "$DNS"; then
            echo "[apply-network] ERROR: Invalid DNS: $DNS"
            mount -o remount,ro / 2>/dev/null || true
            exit 1
        fi

        cat >> /etc/network/interfaces << HEREDOC_END
iface ${IFACE} inet static
    address ${ADDRESS}
    netmask ${NETMASK:-255.255.255.0}
    gateway ${GATEWAY}
HEREDOC_END
        if [ -n "$DNS" ]; then
            echo "nameserver ${DNS}" > /etc/resolv.conf
        fi
        ;;
    *)
        cat >> /etc/network/interfaces << HEREDOC_END
iface ${IFACE} inet dhcp
HEREDOC_END
        ;;
esac

# Remount root read-only
if ! mount -o remount,ro /; then
    echo "[apply-network] WARNING: Failed to remount / as read-only"
fi

# Restart networking
if ! service networking restart 2>/dev/null; then
    ifdown "$IFACE" 2>/dev/null || true
    ifup "$IFACE"
fi

echo "[apply-network] Network configured: ${MODE}"

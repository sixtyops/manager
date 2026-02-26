#!/bin/sh
# apply-network.sh — Reads /data/network/network.conf and configures /etc/network/interfaces
set -e

CONF="/data/network/network.conf"

# Auto-detect primary network interface
detect_iface() {
    # Prefer interface with default route if available
    _iface=$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
    if [ -n "$_iface" ]; then
        echo "$_iface"
        return
    fi

    # Boot-safe fallback: first non-virtual interface, even if not "up" yet.
    for _iface in $(ls /sys/class/net 2>/dev/null); do
        case "$_iface" in
            lo|docker*|br-*|veth*|virbr*|tap*|tun*)
                continue
                ;;
        esac
        echo "$_iface"
        return
    done

    echo "eth0"
}
IFACE="$(detect_iface)"

# Validate an IP address (IPv4 dotted quad with 0-255 octet ranges)
validate_ip() {
    echo "$1" | awk -F. '
        NF != 4 { exit 1 }
        {
            for (i = 1; i <= 4; i++) {
                if ($i !~ /^[0-9]+$/ || $i < 0 || $i > 255) exit 1
            }
        }
        { exit 0 }
    ' >/dev/null 2>&1
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
    # Parse config safely without shell sourcing (prevents command injection)
    MODE=$(grep '^MODE=' "$CONF" | cut -d= -f2- | head -1)
    ADDRESS=$(grep '^ADDRESS=' "$CONF" | cut -d= -f2- | head -1)
    NETMASK=$(grep '^NETMASK=' "$CONF" | cut -d= -f2- | head -1)
    GATEWAY=$(grep '^GATEWAY=' "$CONF" | cut -d= -f2- | head -1)
    DNS=$(grep '^DNS=' "$CONF" | cut -d= -f2- | head -1)

    # Sanitize parsed values
    for var in MODE ADDRESS NETMASK GATEWAY DNS; do
        eval val="\$$var"
        if ! safe_value "$val"; then
            echo "[apply-network] ERROR: Invalid characters in $var, aborting"
            exit 1
        fi
    done
    case "$MODE" in
        dhcp|static|"")
            ;;
        *)
            echo "[apply-network] ERROR: Invalid MODE: $MODE"
            exit 1
            ;;
    esac
fi

# Back up current interfaces config for rollback on failure
cp /etc/network/interfaces /tmp/interfaces.bak 2>/dev/null || true

# /etc/network is a tmpfs — always writable, regenerated each boot
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
            exit 1
        fi
        if [ -n "$GATEWAY" ] && ! validate_ip "$GATEWAY"; then
            echo "[apply-network] ERROR: Invalid gateway: $GATEWAY"
            exit 1
        fi
        if [ -n "$NETMASK" ] && ! validate_ip "$NETMASK"; then
            echo "[apply-network] ERROR: Invalid netmask: $NETMASK"
            exit 1
        fi
        if [ -n "$DNS" ] && ! validate_ip "$DNS"; then
            echo "[apply-network] ERROR: Invalid DNS: $DNS"
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

# Restart networking (skip during early boot — networking service handles it)
if [ "$1" != "--no-restart" ]; then
    if ! service networking restart 2>/dev/null; then
        if ! ifdown "$IFACE" 2>/dev/null || ! ifup "$IFACE" 2>/dev/null; then
            echo "[apply-network] ERROR: Network restart failed, restoring previous config"
            cp /tmp/interfaces.bak /etc/network/interfaces 2>/dev/null || true
            ifup "$IFACE" 2>/dev/null || true
            exit 1
        fi
    fi
fi

echo "[apply-network] Network configured: ${MODE} (interface: ${IFACE})"

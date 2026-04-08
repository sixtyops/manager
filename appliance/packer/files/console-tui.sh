#!/bin/sh
# console-tui.sh — Whiptail-based console TUI for SixtyOps appliance (tty1)

# Prevent kernel/init messages from corrupting the TUI display
dmesg -n 1 2>/dev/null || true

# Colors and sizing
TERM=linux
export TERM
ROWS=24
COLS=78

# Auto-detect primary network interface
# Prefer interface with default route; fall back to first physical NIC
detect_iface() {
    _IFACE=$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
    if [ -n "$_IFACE" ]; then
        echo "$_IFACE"
        return
    fi

    # Boot-safe fallback: first non-virtual interface, even if not yet up.
    for _IFACE in $(ls /sys/class/net 2>/dev/null); do
        case "$_IFACE" in
            lo|docker*|br-*|veth*|virbr*|tap*|tun*)
                continue
                ;;
        esac
        echo "$_IFACE"
        return
    done

    if [ -z "$_IFACE" ]; then
        _IFACE="eth0"
    fi
    echo "$_IFACE"
}

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

get_ip() {
    IFACE=$(detect_iface)
    IP=$(ip -4 addr show "$IFACE" 2>/dev/null | awk '/inet /{split($2,a,"/"); print a[1]; exit}')
    echo "${IP:-No IP}"
}

get_version() {
    if timeout 5 docker inspect sixtyops-management >/dev/null 2>&1; then
        timeout 5 docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' sixtyops-management 2>/dev/null \
            | grep '^APP_VERSION=' | cut -d= -f2 || echo "unknown"
    else
        grep '^APP_VERSION=' /opt/sixtyops/.env 2>/dev/null | cut -d= -f2 || echo "unknown"
    fi
}

get_machine_id() {
    if [ -f /sys/class/dmi/id/product_uuid ]; then
        cut -c1-8 /sys/class/dmi/id/product_uuid 2>/dev/null | tr '[:lower:]' '[:upper:]'
    else
        echo "UNKNOWN"
    fi
}

get_uptime() {
    uptime | sed 's/.*up *//' | sed 's/, *[0-9]* user.*//'
}

get_disk_usage() {
    df -h /data 2>/dev/null | awk 'NR==2 {printf "%s / %s (%s)", $3, $2, $5}'
}

get_status() {
    if timeout 5 docker ps --format '{{.Names}}' 2>/dev/null | grep -q sixtyops-management; then
        echo "Running"
    else
        echo "Stopped"
    fi
}

show_main_menu() {
    IP=$(get_ip)
    VERSION=$(get_version)
    MACHINE_ID=$(get_machine_id)
    UPTIME=$(get_uptime)
    DISK=$(get_disk_usage)
    STATUS=$(get_status)

    CHOICE=$(whiptail --title "SixtyOps Manager" \
        --menu "\n  IP Address:  ${IP}\n  Version:     ${VERSION}\n  Machine ID:  ${MACHINE_ID}\n  Status:      ${STATUS}\n  Uptime:      ${UPTIME}\n  Disk:        ${DISK}\n" \
        $ROWS $COLS 6 \
        "1" "Network Configuration" \
        "2" "View Logs" \
        "3" "Check for Updates" \
        "4" "Factory Reset" \
        "5" "Reboot" \
        "6" "Recovery Console" \
        3>&1 1>&2 2>&3) || return

    case "$CHOICE" in
        1) show_network_menu ;;
        2) show_logs ;;
        3) check_updates ;;
        4) factory_reset ;;
        5) do_reboot ;;
        6) recovery_console ;;
    esac
}

show_network_menu() {
    clear
    CURRENT_MODE="DHCP"
    if [ -f /data/network/network.conf ]; then
        # Parse config safely without shell sourcing
        CURRENT_MODE=$(grep '^MODE=' /data/network/network.conf | cut -d= -f2- | head -1)
        CURRENT_MODE=$(echo "$CURRENT_MODE" | tr '[:lower:]' '[:upper:]')
        CURRENT_MODE="${CURRENT_MODE:-DHCP}"
    fi

    CHOICE=$(whiptail --title "Network Configuration" \
        --menu "Current mode: ${CURRENT_MODE}\nCurrent IP: $(get_ip)" \
        $ROWS $COLS 2 \
        "1" "DHCP (automatic)" \
        "2" "Static IP" \
        3>&1 1>&2 2>&3) || return

    case "$CHOICE" in
        1) configure_dhcp ;;
        2) configure_static ;;
    esac
}

configure_dhcp() {
    cat > /data/network/network.conf << 'EOF'
MODE=dhcp
EOF
    whiptail --title "Network" --infobox "Applying network configuration..." 5 $COLS
    if ! /usr/local/bin/apply-network.sh > /dev/null 2>&1; then
        whiptail --title "Error" --msgbox "Network configuration failed.\nPrevious settings restored." 8 $COLS
        return
    fi
    whiptail --title "Network" --msgbox "DHCP configured. New IP: $(get_ip)" 8 $COLS
}

configure_static() {
    ADDRESS=$(whiptail --title "Static IP" --inputbox "IP Address:" 8 $COLS 3>&1 1>&2 2>&3) || return
    NETMASK=$(whiptail --title "Static IP" --inputbox "Netmask:" 8 $COLS "255.255.255.0" 3>&1 1>&2 2>&3) || return
    GATEWAY=$(whiptail --title "Static IP" --inputbox "Gateway:" 8 $COLS 3>&1 1>&2 2>&3) || return
    DNS=$(whiptail --title "Static IP" --inputbox "DNS Server:" 8 $COLS "8.8.8.8" 3>&1 1>&2 2>&3) || return

    if [ -z "$ADDRESS" ] || [ -z "$GATEWAY" ]; then
        whiptail --title "Error" --msgbox "IP address and gateway are required." 8 $COLS
        return
    fi

    # Validate IP format before writing to config
    if ! validate_ip "$ADDRESS"; then
        whiptail --title "Error" --msgbox "Invalid IP address format: ${ADDRESS}" 8 $COLS
        return
    fi
    if ! validate_ip "$NETMASK"; then
        whiptail --title "Error" --msgbox "Invalid netmask format: ${NETMASK}" 8 $COLS
        return
    fi
    if ! validate_ip "$GATEWAY"; then
        whiptail --title "Error" --msgbox "Invalid gateway format: ${GATEWAY}" 8 $COLS
        return
    fi
    if [ -n "$DNS" ] && ! validate_ip "$DNS"; then
        whiptail --title "Error" --msgbox "Invalid DNS server format: ${DNS}" 8 $COLS
        return
    fi

    cat > /data/network/network.conf << EOF
MODE=static
ADDRESS=${ADDRESS}
NETMASK=${NETMASK}
GATEWAY=${GATEWAY}
DNS=${DNS}
EOF
    whiptail --title "Network" --infobox "Applying network configuration..." 5 $COLS
    if ! /usr/local/bin/apply-network.sh > /dev/null 2>&1; then
        whiptail --title "Error" --msgbox "Network configuration failed.\nPrevious settings restored." 8 $COLS
        return
    fi
    whiptail --title "Network" --msgbox "Static IP configured: ${ADDRESS}" 8 $COLS
}

show_logs() {
    clear
    LOGFILE=$(mktemp /tmp/sixtyops-logs.XXXXXX)
    timeout 10 docker logs --tail 100 sixtyops-management > "$LOGFILE" 2>&1 || echo "(timed out fetching logs)" > "$LOGFILE"
    whiptail --title "Application Logs (last 100 lines)" --scrolltext --textbox "$LOGFILE" $ROWS $COLS
    rm -f "$LOGFILE"
}

check_updates() {
    whiptail --title "Check for Updates" --infobox "Checking for updates..." 5 $COLS
    # Query the app API for update status
    RESULT=$(timeout 15 curl -sf http://localhost:8000/api/update-status 2>/dev/null)
    if [ $? -eq 0 ]; then
        AVAILABLE=$(echo "$RESULT" | jq -r '.update_available // false')
        LATEST=$(echo "$RESULT" | jq -r '.latest_version // "unknown"')
        APPLIANCE_UPGRADE=$(echo "$RESULT" | jq -r '.appliance_upgrade_required // false')
        if [ "$AVAILABLE" = "true" ]; then
            if [ "$APPLIANCE_UPGRADE" = "true" ]; then
                MIN_VER=$(echo "$RESULT" | jq -r '.min_appliance_version // "unknown"')
                whiptail --title "Appliance Upgrade Required" --msgbox \
                    "Update ${LATEST} requires appliance platform v${MIN_VER}.\n\nDownload the latest appliance OVA from:\nhttps://github.com/sixtyops/manager/releases/tag/appliance-latest\n\nDeploy the new OVA and migrate your data." \
                    12 $COLS
            else
                whiptail --title "Update Available" --yesno "Update available: ${LATEST}\n\nApply update now?" 10 $COLS
                if [ $? -eq 0 ]; then
                    whiptail --title "Updating" --infobox "Applying update to ${LATEST}...\nThis may take several minutes." 6 $COLS
                    timeout 120 curl -sf -X POST http://localhost:8000/api/apply-update > /dev/null 2>&1
                    # Poll for update completion (watchdog takes ~90s)
                    for poll_i in $(seq 1 18); do
                        sleep 10
                        STATUS=$(timeout 5 curl -sf http://localhost:8000/api/update-status 2>/dev/null)
                        if [ $? -ne 0 ]; then
                            # App may be restarting
                            whiptail --title "Updating" --infobox "Update in progress... (app restarting)" 5 $COLS
                            continue
                        fi
                        PENDING=$(echo "$STATUS" | jq -r '.update_pending // false')
                        if [ "$PENDING" = "false" ]; then
                            break
                        fi
                        whiptail --title "Updating" --infobox "Update in progress... ($poll_i/18)" 5 $COLS
                    done
                    whiptail --title "Update" --msgbox "Update complete. Check version on the main screen." 8 $COLS
                fi
            fi
        else
            whiptail --title "Up to Date" --msgbox "Current version is up to date.\nVersion: $(get_version)" 8 $COLS
        fi
    else
        whiptail --title "Error" --msgbox "Could not check for updates.\nThe application may not be running." 8 $COLS
    fi
}

factory_reset() {
    # Require recovery key authentication before factory reset
    MACHINE_ID=$(get_machine_id)
    whiptail --title "Factory Reset — Authentication Required" --msgbox \
        "Factory reset requires a recovery key.\n\nMachine ID: ${MACHINE_ID}\n\nContact support with this Machine ID to receive a recovery key." 12 $COLS

    KEY=$(whiptail --title "Factory Reset — Recovery Key" --inputbox "Enter recovery key:" 8 $COLS 3>&1 1>&2 2>&3) || return
    if [ -z "$KEY" ]; then
        return
    fi

    if ! /usr/local/bin/recovery "$KEY"; then
        whiptail --title "Factory Reset" --msgbox "Invalid or expired recovery key.\nFactory reset denied." 8 $COLS
        return
    fi

    whiptail --title "Factory Reset" --yesno "WARNING: This will erase ALL data:\n\n  - Database\n  - Firmware files\n  - Backups\n  - SSL certificates\n  - Network configuration\n\nThis action CANNOT be undone.\n\nAre you sure?" 16 $COLS
    if [ $? -ne 0 ]; then
        return
    fi

    # Double confirm
    CONFIRM=$(whiptail --title "Confirm Factory Reset" --inputbox "Type RESET to confirm:" 8 $COLS 3>&1 1>&2 2>&3) || return
    if [ "$CONFIRM" != "RESET" ]; then
        whiptail --title "Cancelled" --msgbox "Factory reset cancelled." 8 $COLS
        return
    fi

    whiptail --title "Factory Reset" --infobox "Performing factory reset..." 5 $COLS

    # Stop the app (timeout must exceed stop_grace_period of 60s)
    timeout 90 docker compose -f /opt/sixtyops/docker-compose.yml down 2>/dev/null || {
        echo "[factory-reset] Graceful shutdown timed out, force-killing containers..."
        docker compose -f /opt/sixtyops/docker-compose.yml kill 2>/dev/null || true
        docker compose -f /opt/sixtyops/docker-compose.yml down --timeout 5 2>/dev/null || true
    }

    # Brief pause to let Docker release volume mounts
    sleep 2

    # Wipe data
    rm -rf /data/db/* /data/firmware/* /data/backups/* /data/certs/*
    cat > /data/network/network.conf << 'EOF'
MODE=dhcp
EOF
    rm -f /data/.first-boot-done

    # Restart
    whiptail --title "Factory Reset" --msgbox "Factory reset complete. The appliance will now reboot." 8 $COLS
    reboot
}

do_reboot() {
    whiptail --title "Reboot" --yesno "Are you sure you want to reboot?" 8 $COLS
    if [ $? -eq 0 ]; then
        reboot
    fi
}

recovery_console() {
    MACHINE_ID=$(get_machine_id)
    whiptail --title "Recovery Console" --msgbox "Machine ID: ${MACHINE_ID}\n\nContact support with this Machine ID to receive a recovery key.\nThe recovery key is valid for 24 hours." 10 $COLS

    KEY=$(whiptail --title "Recovery Console" --inputbox "Enter recovery key:" 8 $COLS 3>&1 1>&2 2>&3) || return

    if [ -z "$KEY" ]; then
        return
    fi

    if /usr/local/bin/recovery "$KEY"; then
        whiptail --title "Recovery" --msgbox "Recovery key accepted.\nYou will be dropped into a shell.\nType 'exit' to return to this menu." 10 $COLS
        /bin/sh
    else
        whiptail --title "Recovery" --msgbox "Invalid or expired recovery key." 8 $COLS
    fi
}

# Main loop
clear
while true; do
    clear
    show_main_menu
done

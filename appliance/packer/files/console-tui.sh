#!/bin/sh
# console-tui.sh — Whiptail-based console TUI for Tachyon appliance (tty1)

# Colors and sizing
TERM=linux
export TERM
ROWS=24
COLS=78

get_ip() {
    ip -4 addr show eth0 2>/dev/null | grep -oP 'inet \K[\d.]+' || echo "No IP"
}

get_version() {
    if timeout 5 docker inspect tachyon-management >/dev/null 2>&1; then
        timeout 5 docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' tachyon-management 2>/dev/null \
            | grep '^APP_VERSION=' | cut -d= -f2 || echo "unknown"
    else
        grep '^APP_VERSION=' /opt/tachyon/.env 2>/dev/null | cut -d= -f2 || echo "unknown"
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
    uptime | sed 's/.*up\s*//' | sed 's/,\s*[0-9]* user.*//'
}

get_disk_usage() {
    df -h /data 2>/dev/null | awk 'NR==2 {printf "%s / %s (%s)", $3, $2, $5}'
}

get_status() {
    if timeout 5 docker ps --format '{{.Names}}' 2>/dev/null | grep -q tachyon-management; then
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

    CHOICE=$(whiptail --title "Tachyon Firmware Updater" \
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
    CURRENT_MODE="DHCP"
    if [ -f /data/network/network.conf ]; then
        . /data/network/network.conf
        CURRENT_MODE=$(echo "$MODE" | tr '[:lower:]' '[:upper:]')
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
    mount -o remount,rw / 2>/dev/null || true
    cat > /data/network/network.conf << 'EOF'
MODE=dhcp
EOF
    /usr/local/bin/apply-network.sh
    mount -o remount,ro / 2>/dev/null || true
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

    mount -o remount,rw / 2>/dev/null || true
    cat > /data/network/network.conf << EOF
MODE=static
ADDRESS=${ADDRESS}
NETMASK=${NETMASK}
GATEWAY=${GATEWAY}
DNS=${DNS}
EOF
    /usr/local/bin/apply-network.sh
    mount -o remount,ro / 2>/dev/null || true
    whiptail --title "Network" --msgbox "Static IP configured: ${ADDRESS}" 8 $COLS
}

show_logs() {
    LOGFILE=$(mktemp /tmp/tachyon-logs.XXXXXX)
    timeout 10 docker logs --tail 100 tachyon-management > "$LOGFILE" 2>&1 || echo "(timed out fetching logs)" > "$LOGFILE"
    whiptail --title "Application Logs (last 100 lines)" --scrolltext --textbox "$LOGFILE" $ROWS $COLS
    rm -f "$LOGFILE"
}

check_updates() {
    whiptail --title "Check for Updates" --infobox "Checking for updates..." 5 $COLS
    # Query the app API for update status
    RESULT=$(timeout 15 curl -sf http://localhost:8000/api/update-status 2>/dev/null)
    if [ $? -eq 0 ]; then
        AVAILABLE=$(echo "$RESULT" | grep -o '"update_available":[a-z]*' | cut -d: -f2)
        LATEST=$(echo "$RESULT" | grep -o '"latest_version":"[^"]*"' | cut -d'"' -f4)
        APPLIANCE_UPGRADE=$(echo "$RESULT" | grep -o '"appliance_upgrade_required":[a-z]*' | cut -d: -f2)
        if [ "$AVAILABLE" = "true" ]; then
            if [ "$APPLIANCE_UPGRADE" = "true" ]; then
                MIN_VER=$(echo "$RESULT" | grep -o '"min_appliance_version":"[^"]*"' | cut -d'"' -f4)
                whiptail --title "Appliance Upgrade Required" --msgbox \
                    "Update ${LATEST} requires appliance platform v${MIN_VER}.\n\nDownload the latest appliance OVA from:\nhttps://github.com/isolson/firmware-updater/releases/tag/appliance-latest\n\nDeploy the new OVA and migrate your data." \
                    12 $COLS
            else
                whiptail --title "Update Available" --yesno "Update available: ${LATEST}\n\nApply update now?" 10 $COLS
                if [ $? -eq 0 ]; then
                    whiptail --title "Updating" --infobox "Applying update to ${LATEST}...\nThis may take several minutes." 6 $COLS
                    timeout 30 curl -sf -X POST http://localhost:8000/api/apply-update > /dev/null 2>&1
                    sleep 5
                    whiptail --title "Update" --msgbox "Update initiated. The appliance will restart automatically." 8 $COLS
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

    # Stop the app
    timeout 30 docker compose -f /opt/tachyon/docker-compose.yml down 2>/dev/null || true

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
while true; do
    show_main_menu
done

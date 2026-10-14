#!/usr/bin/env bash
# Installed as iot-nexus; iot-gateway remains a compatible command.
set -uo pipefail

BROKER_SERVICE="iot-gateway-broker.service"
WEB_SERVICE="iot-gateway-web.service"
VPN_SERVICE="iot-gateway-websocket.service"
VPN_CONFIG="/etc/iot-gateway/websocket.env"
UNINSTALL_SCRIPT="/usr/local/lib/iot-gateway/uninstall.sh"
PANEL_CONFIG="/etc/iot-gateway/panel.env"
PANEL_DROPIN="/etc/systemd/system/$WEB_SERVICE.d/10-panel.conf"
APP_DIR="/opt/mqtt-gui"
VPN_PYTHON="/usr/local/lib/iot-gateway/venv/bin/python"
VPN_CRYPTO="/usr/local/lib/iot-gateway/vpn_crypto.py"
MOSQUITTO_ASSETS="/usr/local/lib/iot-gateway/mosquitto_assets.py"

interactive_screen() {
    [[ -t 0 && -t 1 && "${TERM:-dumb}" != dumb ]]
}

screen_open() {
    if interactive_screen; then
        printf '\033[?1049h'
        trap 'printf "\033[0m\033[?1049l"' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
    fi
}

draw_menu() {
    local title=$1 row
    shift
    if interactive_screen; then
        printf '\033[H\033[2J'
        if [[ -z "${NO_COLOR:-}" ]]; then printf '\033[1;36m'; fi
    fi
    printf '  ╭──────────────────────────────────────────────────────────╮\n'
    while IFS= read -r row; do
        printf '  │ %-56s │\n' "$row"
    done <<'BANNER'
  ___ ___ _____   _   _ _______  ___   _ ____
 |_ _/ _ \_   _| | \ | | ____\ \/ / | | / ___|
  | | | | || |   |  \| |  _|  \  /| | | \___ \
  | | |_| || |   | |\  | |___ /  \| |_| |___) |
 |___\___/ |_|   |_| \_|_____/_/\_\___/|____/

                   iot-nexus
            CONNECT / CONTROL / MONITOR
BANNER
    printf '  ╰──────────────────────────────────────────────────────────╯\n'
    if interactive_screen && [[ -z "${NO_COLOR:-}" ]]; then printf '\033[0m'; fi
    printf '\n  ◈ %s\n\n' "$title"
    printf '  %s\n' "$@"
    printf '\n  ──────────────────────────────────────────────────────────\n'
}

finish_action() {
    local ignored
    if interactive_screen; then
        printf '\n'
        read -r -p '  Press Enter to return to the menu… ' ignored || true
    fi
}

as_root() {
    if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

read_port() {
    local default_port=8765 saved_port
    if [[ -r "$VPN_CONFIG" ]]; then
        saved_port=$(sed -n 's/^VPN_PORT=//p' "$VPN_CONFIG")
        if [[ "$saved_port" =~ ^[0-9]{1,5}$ ]] && (( 10#$saved_port >= 1 && 10#$saved_port <= 65535 )); then
            default_port=$((10#$saved_port))
        fi
    fi
    while true; do
        read -r -p "WebSocket port [$default_port]: " port || return 1
        port=${port:-$default_port}
        if [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )); then
            port=$((10#$port))
            return 0
        fi
        echo "Enter a port between 1 and 65535."
    done
}

vpn_listens() {
    local pid sockets
    systemctl is-active --quiet "$VPN_SERVICE" || return 1
    pid=$(systemctl show --property=MainPID --value "$VPN_SERVICE") || return 1
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    sockets=$(as_root ss -H -ltnp "sport = :$port") || return 1
    [[ "$sockets" == *"pid=$pid,"* ]]
}

vpn_status() {
    if vpn_listens; then
        echo "RUNNING: runner_vpn.py is listening on TCP port $port."
    else
        echo "NOT LISTENING: runner_vpn.py is not listening on TCP port $port."
        systemctl --no-pager --full status "$VPN_SERVICE" || true
    fi
}

vpn_start() {
    local sockets attempt force_restart=${1:-0} already_ours=0
    if vpn_listens; then
        already_ours=1
        if [[ "$force_restart" == 0 ]]; then
            echo "Already running on port $port."
            return
        fi
    fi
    sockets=$(as_root ss -H -ltn "sport = :$port") || return 1
    if [[ -n "$sockets" && "$already_ours" == 0 ]]; then
        echo "Port $port is already in use. Choose another port."
        return 1
    fi
    as_root install -d -o root -g root -m 755 /etc/iot-gateway || return 1
    printf 'VPN_PORT=%s\n' "$port" | as_root tee "$VPN_CONFIG" >/dev/null || return 1
    as_root systemctl restart "$VPN_SERVICE" || return 1
    # An active process alone doesn't prove that the Python socket is listening.
    for attempt in {1..20}; do
        if vpn_listens; then
            echo "Started WebSocket VPN on port $port (runs as root)."
            return 0
        fi
        sleep 0.25
    done
    echo "WebSocket did not start listening. Recent service logs:"
    as_root journalctl -u "$VPN_SERVICE" -n 15 --no-pager
    return 1
}

vpn_key_action() {
    local action=$1 was_active=0
    if [[ "$action" == set || "$action" == generate ]]; then
        systemctl is-active --quiet "$VPN_SERVICE" && was_active=1
        echo 'Changing the key disconnects active clients. Update the key on every client.'
    fi
    as_root "$VPN_PYTHON" "$VPN_CRYPTO" "$action" || return 1
    if [[ "$action" == set || "$action" == generate ]]; then
        if [[ "$was_active" == 1 ]]; then
            if ! as_root systemctl restart "$VPN_SERVICE"; then
                echo 'Key saved, but WebSocket restart failed. Check service logs before reconnecting.'
                return 1
            fi
        fi
        echo 'Key saved. Use show key to copy it securely to your client.'
    fi
}

vpn_key_menu() {
    local choice
    draw_menu 'WebSocket / Encryption Key' '1 - key status' '2 - show key' '3 - set key' '4 - generate random key' '0 - back'
    while true; do
        read -r -p 'Select: ' choice || return
        case "$choice" in
            1) vpn_key_action status ;;
            2) vpn_key_action show ;;
            3) vpn_key_action set ;;
            4) vpn_key_action generate ;;
            0) return ;;
            *) echo 'Invalid option.' ;;
        esac
        finish_action
        if interactive_screen; then
            draw_menu 'WebSocket / Encryption Key' '1 - key status' '2 - show key' '3 - set key' '4 - generate random key' '0 - back'
        fi
    done
}

websocket_menu() {
    local choice port
    draw_menu 'WebSocket VPN' '1 - start' '2 - status' '3 - restart' '4 - stop' '5 - encryption key' '0 - back'
    while true; do
        read -r -p "Select: " choice || return
        case "$choice" in
            1) read_port && vpn_start ;;
            2) read_port && vpn_status ;;
            3) read_port && vpn_start 1 ;;
            4) if as_root systemctl stop "$VPN_SERVICE"; then
                   echo "WebSocket VPN stopped."
               else
                   echo "Could not stop WebSocket VPN."
               fi ;;
            5) vpn_key_menu ;;
            0) return ;;
            *) echo "Invalid option." ;;
        esac
        finish_action
        if interactive_screen; then
            draw_menu 'WebSocket VPN' '1 - start' '2 - status' '3 - restart' '4 - stop' '5 - encryption key' '0 - back'
        fi
    done
}

print_panel_urls() {
    local bind_address=$1 panel_port=$2 address family
    case "$bind_address" in
        0.0.0.0|'*')
            printf 'Local URL: http://127.0.0.1:%s\n' "$panel_port"
            family=4 ;;
        ::)
            printf 'Local URL: http://[::1]:%s\n' "$panel_port"
            family=6 ;;
        *)
            if [[ "$bind_address" == *:* ]]; then
                printf 'Panel URL: http://[%s]:%s\n' "$bind_address" "$panel_port"
            else
                printf 'Panel URL: http://%s:%s\n' "$bind_address" "$panel_port"
            fi
            return ;;
    esac
    while read -r address; do
        [[ -n "$address" ]] || continue
        if [[ "$family" == 6 ]]; then
            printf 'Network URL: http://[%s]:%s\n' "$address" "$panel_port"
        else
            printf 'Network URL: http://%s:%s\n' "$address" "$panel_port"
        fi
    done < <(ip -o "-$family" addr show scope global | awk '{split($4, address, "/"); print address[1]}' | sort -u)
}

panel_listener() {
    local expected_port=${1:-} pid sockets state receive_queue send_queue endpoint peer process
    PANEL_BOUND_ADDRESS=''
    PANEL_BOUND_PORT=''
    systemctl is-active --quiet "$WEB_SERVICE" || return 1
    pid=$(systemctl show --property=MainPID --value "$WEB_SERVICE") || return 1
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    sockets=$(as_root ss -H -ltnp) || return 1
    while read -r state receive_queue send_queue endpoint peer process; do
        [[ "$process" == *"pid=$pid,"* ]] || continue
        PANEL_BOUND_PORT=${endpoint##*:}
        [[ "$PANEL_BOUND_PORT" =~ ^[0-9]+$ ]] || continue
        [[ -z "$expected_port" || "$PANEL_BOUND_PORT" == "$expected_port" ]] || continue
        PANEL_BOUND_ADDRESS=${endpoint%:*}
        PANEL_BOUND_ADDRESS=${PANEL_BOUND_ADDRESS#\[}
        PANEL_BOUND_ADDRESS=${PANEL_BOUND_ADDRESS%\]}
        return 0
    done <<< "$sockets"
    return 1
}

panel_http_ready() {
    local host=$PANEL_BOUND_ADDRESS
    case "$host" in
        0.0.0.0|'*') host=127.0.0.1 ;;
        ::) host='[::1]' ;;
        *:*) host="[$host]" ;;
    esac
    python3 - "$host" "$PANEL_BOUND_PORT" <<'PY'
import sys
import urllib.request
try:
    # A configured proxy must never intercept this local readiness check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{sys.argv[1]}:{sys.argv[2]}/login", timeout=1) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

panel_diagnostics() {
    echo "Panel startup failed. Service details and recent errors:"
    systemctl --no-pager --full status "$WEB_SERVICE" || true
    as_root journalctl -u "$WEB_SERVICE" -n 25 --no-pager || true
}

panel_status() {
    if panel_listener && panel_http_ready; then
        echo "Panel is RUNNING and responding to HTTP."
        print_panel_urls "$PANEL_BOUND_ADDRESS" "$PANEL_BOUND_PORT"
    elif systemctl is-active --quiet "$WEB_SERVICE"; then
        echo "Panel is not ready: no working HTTP listener."
        panel_diagnostics
        return 1
    else
        echo "Panel is not running."
        panel_diagnostics
        return 1
    fi
}

random_panel_port() {
    python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("0.0.0.0", 0))
    print(listener.getsockname()[1])
PY
}

read_panel_port() {
    local default_port=9000 saved_port sockets
    if [[ -r "$PANEL_CONFIG" ]]; then
        saved_port=$(sed -n 's/^PORT=//p' "$PANEL_CONFIG")
        if [[ "$saved_port" =~ ^[0-9]{1,5}$ ]] && (( 10#$saved_port >= 1 && 10#$saved_port <= 65535 )); then
            default_port=$((10#$saved_port))
        fi
    elif panel_listener; then
        default_port=$PANEL_BOUND_PORT
    fi
    while true; do
        read -r -p "Panel port [$default_port] (r = random free port): " panel_port || return 1
        panel_port=${panel_port:-$default_port}
        if [[ "$panel_port" == r || "$panel_port" == R || "$panel_port" == random ]]; then
            panel_port=$(random_panel_port) || return 1
            echo "Selected free port: $panel_port"
        fi
        if [[ ! "$panel_port" =~ ^[0-9]{1,5}$ ]] || (( 10#$panel_port < 1 || 10#$panel_port > 65535 )); then
            echo "Enter a port between 1 and 65535, or r for random."
            continue
        fi
        panel_port=$((10#$panel_port))
        if panel_listener "$panel_port"; then return 0; fi
        sockets=$(as_root ss -H -ltn "sport = :$panel_port") || return 1
        if [[ -n "$sockets" ]]; then
            echo "Port $panel_port is already in use. Choose another port or r."
            continue
        fi
        return 0
    done
}

repair_panel_permissions() {
    local data_file
    if [[ ! -d "$APP_DIR" || ! -f "$APP_DIR/main.py" ]]; then
        echo "Panel installation not found: $APP_DIR"
        return 1
    fi
    # cp preserves source modes; a source file with mode 600 cannot be imported
    # by the mqtt-gui service user after it is copied and owned by root.
    as_root chown mqtt-gui:mqtt-gui "$APP_DIR" || return 1
    as_root chmod 751 "$APP_DIR" || return 1
    as_root find "$APP_DIR" -maxdepth 1 -type f -name '*.py' -exec chmod 644 '{}' + || return 1
    as_root find "$APP_DIR/templates" -type d -exec chmod 755 '{}' + || return 1
    as_root find "$APP_DIR/templates" -type f -name '*.html' -exec chmod 644 '{}' + || return 1
    for data_file in "$APP_DIR"/login_signup.db* "$APP_DIR"/device_status.db*; do
        if [[ -f "$data_file" && ! -L "$data_file" ]]; then
            as_root chown mqtt-gui:mqtt-gui "$data_file" || return 1
            as_root chmod 600 "$data_file" || return 1
        fi
    done
}

configure_panel_port() {
    as_root install -d -o root -g root -m 755 /etc/iot-gateway "${PANEL_DROPIN%/*}" || return 1
    printf 'PORT=%s\n' "$panel_port" | as_root tee "$PANEL_CONFIG" >/dev/null || return 1
    printf '[Service]\nEnvironmentFile=%s\n' "$PANEL_CONFIG" | as_root tee "$PANEL_DROPIN" >/dev/null || return 1
    as_root chmod 644 "$PANEL_CONFIG" "$PANEL_DROPIN" || return 1
    as_root systemctl daemon-reload || return 1
}

wait_panel_ready() {
    local attempt ready_count=0 last_pid='' pid
    for attempt in {1..40}; do
        if panel_listener "$panel_port" && panel_http_ready; then
            pid=$(systemctl show --property=MainPID --value "$WEB_SERVICE")
            if [[ "$pid" == "$last_pid" ]]; then
                ready_count=$((ready_count + 1))
            else
                ready_count=1
                last_pid=$pid
            fi
            if (( ready_count >= 2 )); then
                echo "Panel is ready on port $panel_port."
                print_panel_urls "$PANEL_BOUND_ADDRESS" "$PANEL_BOUND_PORT"
                return 0
            fi
        else
            ready_count=0
        fi
        if systemctl is-failed --quiet "$WEB_SERVICE"; then break; fi
        sleep 0.25
    done
    panel_diagnostics
    return 1
}

manage_panel() {
    local action=$1 panel_port restart_needed=0
    if [[ "$action" == stop ]]; then
        if as_root systemctl stop "$WEB_SERVICE"; then
            echo "Panel stopped."
            return 0
        fi
        echo "Could not stop panel."
        return 1
    fi
    if ! systemctl cat "$WEB_SERVICE" >/dev/null 2>&1; then
        echo "Panel service is not installed: $WEB_SERVICE"
        return 1
    fi
    read_panel_port || return 1
    if systemctl is-active --quiet "$WEB_SERVICE" && ! panel_listener "$panel_port"; then
        restart_needed=1
    fi
    repair_panel_permissions || return 1
    configure_panel_port || return 1
    as_root systemctl reset-failed "$WEB_SERVICE" || return 1
    if [[ "$action" == restart || "$restart_needed" == 1 ]]; then action=restart; fi
    echo "Starting panel on port $panel_port; checking HTTP readiness…"
    if ! as_root systemctl "$action" "$WEB_SERVICE"; then
        panel_diagnostics
        return 1
    fi
    wait_panel_ready
}

panel_menu() {
    local choice
    draw_menu 'Panel Management' '1 - start' '2 - restart' '3 - stop' '4 - status / URL' '0 - back'
    while true; do
        read -r -p "Select: " choice || return
        case "$choice" in
            1) manage_panel start ;;
            2) manage_panel restart ;;
            3) manage_panel stop ;;
            4) panel_status ;;
            0) return ;;
            *) echo "Invalid option." ;;
        esac
        finish_action
        if interactive_screen; then
            draw_menu 'Panel Management' '1 - start' '2 - restart' '3 - stop' '4 - status / URL' '0 - back'
        fi
    done
}

uninstall_gateway() {
    local confirmation
    if [[ ! -f "$UNINSTALL_SCRIPT" ]]; then
        echo "Uninstall script not found: $UNINSTALL_SCRIPT"
        return 1
    fi
    echo "This removes iot-nexus, its services, databases, configuration and Mosquitto packages."
    read -r -p "Type UNINSTALL to confirm (anything else cancels): " confirmation || return 1
    if [[ "$confirmation" != UNINSTALL ]]; then
        echo "Uninstall cancelled."
        return 1
    fi
    if as_root bash "$UNINSTALL_SCRIPT"; then
        echo "iot-nexus has been uninstalled."
    else
        echo "Uninstall failed; some files or services may already have been removed."
        return 1
    fi
}

manage_gateway() {
    local action=$1 vpn_was_active=0
    if [[ "$action" == stop ]]; then
        if as_root systemctl stop "$WEB_SERVICE" "$BROKER_SERVICE" "$VPN_SERVICE"; then
            echo "All services stopped."
            return 0
        fi
        echo "Could not stop all services."
        return 1
    fi
    if systemctl is-active --quiet "$VPN_SERVICE"; then vpn_was_active=1; fi
    prepare_broker_credentials || return 1
    if ! as_root systemctl "$action" "$BROKER_SERVICE"; then
        echo "Broker $action failed."
        return 1
    fi
    manage_panel "$action" || return 1
    if [[ "$action" == restart && "$vpn_was_active" == 1 ]]; then
        as_root systemctl restart "$VPN_SERVICE" || return 1
    fi
    echo "iot-nexus $action completed."
}

mosquitto_asset_action() {
    if [[ ! -f "$MOSQUITTO_ASSETS" ]]; then
        echo "Mosquitto manager not installed: $MOSQUITTO_ASSETS"
        return 1
    fi
    as_root python3 "$MOSQUITTO_ASSETS" "$@"
}

prepare_broker_credentials() {
    mosquitto_asset_action internal repair
}

broker_summary() {
    if systemctl is-active --quiet "$BROKER_SERVICE"; then
        echo '  Broker: RUN'
    elif systemctl is-failed --quiet "$BROKER_SERVICE"; then
        echo '  Broker: ERROR'
    else
        echo '  Broker: STOPPED'
    fi
}

broker_ready() {
    local pid sockets
    systemctl is-active --quiet "$BROKER_SERVICE" || return 1
    pid=$(systemctl show --property=MainPID --value "$BROKER_SERVICE") || return 1
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    sockets=$(as_root ss -H -ltnp 'sport = :7556') || return 1
    [[ "$sockets" == *"pid=$pid,"* ]]
}

restart_broker() {
    local attempt
    prepare_broker_credentials || return 1
    if as_root systemctl restart "$BROKER_SERVICE"; then
        for attempt in {1..40}; do
            if broker_ready; then
                echo 'Broker: RUN — restarted and listening on port 7556.'
                return 0
            fi
            sleep 0.25
        done
    fi
    echo 'Broker: ERROR — restart failed or listener 7556 is unavailable.'
    as_root systemctl --no-pager --full status "$BROKER_SERVICE" || true
    as_root journalctl -u "$BROKER_SERVICE" -n 25 --no-pager || true
    return 1
}

mosquitto_password_menu() {
    local choice
    while true; do
        draw_menu 'Mosquitto / Password' '1 - create password file and first user' '2 - add user / update password' '3 - remove user' '4 - list files / paths' '5 - list usernames' '6 - delete password file' '0 - back'
        read -r -p 'Select: ' choice || return
        case "$choice" in
            1) mosquitto_asset_action password create ;;
            2) mosquitto_asset_action password add-user ;;
            3) mosquitto_asset_action password remove-user ;;
            4) mosquitto_asset_action password list ;;
            5) mosquitto_asset_action password users ;;
            6) mosquitto_asset_action password delete ;;
            0) return ;;
            *) echo 'Invalid option.' ;;
        esac
        finish_action
    done
}

mosquitto_certificate_menu() {
    local choice
    while true; do
        draw_menu 'Mosquitto / Certificate Self-signed & Certbot' '1 - create self-signed certificate (hostname / IP)' '2 - obtain Certbot certificate (public domain)' '3 - list certificates / paths' '4 - delete certificate and private key' '5 - renew Certbot certificate / refresh copies' '0 - back'
        read -r -p 'Select: ' choice || return
        case "$choice" in
            1) mosquitto_asset_action certificate self-signed ;;
            2) mosquitto_asset_action certificate certbot ;;
            3) mosquitto_asset_action certificate list ;;
            4) mosquitto_asset_action certificate delete ;;
            5) mosquitto_asset_action certificate renew ;;
            0) return ;;
            *) echo 'Invalid option.' ;;
        esac
        finish_action
    done
}

mosquitto_menu() {
    local choice
    while true; do
        draw_menu 'Mosquitto' '1 - password' '2 - certificate self-signed / Certbot' '3 - restart broker' '0 - back'
        broker_summary
        read -r -p 'Select: ' choice || return
        case "$choice" in
            1) mosquitto_password_menu ;;
            2) mosquitto_certificate_menu ;;
            3) restart_broker; finish_action ;;
            0) return ;;
            *) echo 'Invalid option.' ;;
        esac
    done
}

main_menu() {
    draw_menu 'Control Center' '1 - websocket' '2 - panel management' '3 - restart' '4 - start' '5 - stop' '6 - uninstall' '7 - mosquitto' '8 - restart broker' '0 - exit'
    broker_summary
}

main() {
    local choice command_name
    for command_name in systemctl ss ip python3; do
        command -v "$command_name" >/dev/null || { echo "iot-nexus requires Linux, systemd, iproute2 and Python 3."; return 1; }
    done
    screen_open
    main_menu
    while true; do
        read -r -p "Select: " choice || return 0
        case "$choice" in
            1) websocket_menu; main_menu; continue ;;
            2) panel_menu; main_menu; continue ;;
            3) manage_gateway restart ;;
            4) manage_gateway start ;;
            5) manage_gateway stop ;;
            6) if uninstall_gateway; then return 0; fi ;;
            7) mosquitto_menu; main_menu; continue ;;
            8) restart_broker; finish_action; main_menu; continue ;;
            0) return 0 ;;
            *) echo "Invalid option." ;;
        esac
        finish_action
        if interactive_screen; then main_menu; fi
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi

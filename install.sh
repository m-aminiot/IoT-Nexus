#!/usr/bin/env bash

set -e
umask 022

#######################################
# Project Configuration
#######################################

PROJECT_NAME="mqtt-gui"
INSTALL_DIR="/opt/$PROJECT_NAME"
INTERNAL_PASSWORD_FILE="$INSTALL_DIR/passwords/.mqtt_passwords"
APP_USER="mqtt-gui"
APP_GROUP="mqtt-gui"

PYTHON_MIN="3.10"

#######################################
# Colors
#######################################

GREEN="\033[0;32m"
RED="\033[0;31m"
YELLOW="\033[1;33m"
BLUE="\033[0;34m"
NC="\033[0m"


SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[ OK ]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[FAIL]${NC} $1"
}

#######################################
# Root Check
#######################################

if [[ $EUID -ne 0 ]]; then
    error "Please run with sudo."
    exit 1
fi

#######################################
# Check Required Files
#######################################

if [[ ! -f "main.py" ]]; then
    error "main.py not found."
    exit 1
fi

if [[ ! -f "requirements.txt" ]]; then
    error "requirements.txt not found."
    exit 1
fi

if [[ ! -f "mosquitto.conf" ]]; then
    error "mosquitto.conf not found."
    exit 1
fi

for required_file in iot-gateway.sh runner_vpn.py vpn_crypto.py Tun.py uninstall.sh mosquitto_assets.py set_mosquitto.py json.json; do
    if [[ ! -f "$required_file" ]]; then
        error "$required_file not found."
        exit 1
    fi
done

# Reject invalid listener ports before changing an existing installation.
python3 -c 'import json, set_mosquitto; set_mosquitto.validate_ports(json.load(open("json.json", encoding="utf-8")))'

#######################################
# Update Packages
#######################################

info "Updating package list..."

apt update

#######################################
# Install Packages
#######################################

info "Installing packages..."

apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    iproute2 \
    sudo \
    mosquitto \
    mosquitto-clients \
    openssl \
    certbot

#######################################
# Check Python Version
#######################################

VERSION=$(python3 -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')")

if [ "$(printf '%s\n' "$PYTHON_MIN" "$VERSION" | sort -V | head -n1)" != "$PYTHON_MIN" ]; then

    error "Python >= $PYTHON_MIN required."

    exit 1

fi

success "Python Version: $VERSION"

#######################################
# Copy Project
#######################################

info "Installing project..."

# Stop previous and current units before replacing application files.
for service in mosquitto-project.service mqtt-gui.service iot-gateway-broker.service iot-gateway-web.service iot-gateway-websocket.service; do
    systemctl stop "$service" 2>/dev/null || true
done
for service in mosquitto-project.service mqtt-gui.service; do
    systemctl disable "$service" 2>/dev/null || true
    rm -f "/etc/systemd/system/$service"
done

rm -rf "$INSTALL_DIR"

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -o root -g root -m 755 "$INSTALL_DIR"

cp -r ./* "$INSTALL_DIR"

success "Project copied."

#######################################
# Create Virtual Environment
#######################################

cd "$INSTALL_DIR"

info "Creating Python virtual environment..."

python3 -m venv venv

source venv/bin/activate

pip install --upgrade pip

pip install -r requirements.txt

deactivate

success "Python dependencies installed."

#######################################
# Create Per-install MQTT Credentials
#######################################

info "Generating private MQTT credentials..."

MQTT_USERNAME="svc_$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
MQTT_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

install -o "$APP_USER" -g "$APP_GROUP" -m 600 /dev/null "$INSTALL_DIR/.mqtt_credentials"
printf 'username=%s\npassword=%s\n' "$MQTT_USERNAME" "$MQTT_PASSWORD" > "$INSTALL_DIR/.mqtt_credentials"

install -d -o root -g mosquitto -m 750 "$INSTALL_DIR/passwords"
install -o mosquitto -g mosquitto -m 600 /dev/null "$INTERNAL_PASSWORD_FILE"
printf '%s:%s\n' "$MQTT_USERNAME" "$MQTT_PASSWORD" > "$INTERNAL_PASSWORD_FILE"
mosquitto_passwd -U "$INTERNAL_PASSWORD_FILE"
chown mosquitto:mosquitto "$INTERNAL_PASSWORD_FILE"
chmod 600 "$INTERNAL_PASSWORD_FILE"

unset MQTT_USERNAME MQTT_PASSWORD

success "Unique MQTT credentials generated."

#######################################
# Permissions
#######################################

info "Setting permissions..."

chown -R root:root "$INSTALL_DIR"

chown "$APP_USER:$APP_GROUP" "$INSTALL_DIR"
chown "$APP_USER:$APP_GROUP" "$INSTALL_DIR/.mqtt_credentials"
chown "$APP_USER:$APP_GROUP" "$INSTALL_DIR/json.json"
chown "$APP_USER:$APP_GROUP" "$INSTALL_DIR/login_signup.db" 2>/dev/null || true
chown "$APP_USER:$APP_GROUP" "$INSTALL_DIR/mosquitto.conf"
chown root:mosquitto "$INSTALL_DIR/passwords"
chown mosquitto:mosquitto "$INTERNAL_PASSWORD_FILE"
chmod 750 "$INSTALL_DIR/passwords"

chmod 751 "$INSTALL_DIR"

# Source file modes may be 600 on the development machine. The web service
# must be able to import root-owned Python modules and read its templates.
find "$INSTALL_DIR" -maxdepth 1 -type f -name '*.py' -exec chmod 644 '{}' +
find "$INSTALL_DIR/templates" -type d -exec chmod 755 '{}' +
find "$INSTALL_DIR/templates" -type f -name '*.html' -exec chmod 644 '{}' +
for database in "$INSTALL_DIR"/login_signup.db* "$INSTALL_DIR"/device_status.db*; do
    if [[ -f "$database" && ! -L "$database" ]]; then
        chown "$APP_USER:$APP_GROUP" "$database"
        chmod 600 "$database"
    fi
done

chmod 644 "$INSTALL_DIR/mosquitto.conf"
chmod 640 "$INSTALL_DIR/json.json"
chmod 600 "$INSTALL_DIR/login_signup.db" 2>/dev/null || true
chmod 600 "$INSTALL_DIR/.mqtt_credentials"
chmod 600 "$INTERNAL_PASSWORD_FILE"

python3 "$INSTALL_DIR/set_mosquitto.py"

cat > /etc/sudoers.d/mqtt-gui << EOF
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart iot-gateway-broker.service
EOF
chmod 440 /etc/sudoers.d/mqtt-gui
visudo -cf /etc/sudoers.d/mqtt-gui >/dev/null

success "Permissions configured."

#######################################
# Stop Default Mosquitto
#######################################

info "Stopping default Mosquitto service..."

systemctl stop mosquitto 2>/dev/null || true
systemctl disable mosquitto 2>/dev/null || true

success "Default Mosquitto disabled."

#######################################
# Create Mosquitto Service
#######################################

info "Creating Mosquitto service..."

cat > /etc/systemd/system/iot-gateway-broker.service << EOF
[Unit]
Description=IoT Gateway MQTT Broker
After=network.target

[Service]
Type=simple

User=mosquitto
Group=mosquitto

ExecStart=/usr/sbin/mosquitto -c $INSTALL_DIR/mosquitto.conf

Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

success "Mosquitto service created."

#######################################
# Create Flask Service
#######################################

info "Creating Flask service..."

cat > /etc/systemd/system/iot-gateway-web.service << EOF
[Unit]
Description=IoT Gateway Web Panel
After=network.target iot-gateway-broker.service

[Service]
Type=simple

User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$INSTALL_DIR
Environment="SECRET_KEY=$SECRET_KEY"
EnvironmentFile=-/etc/iot-gateway/panel.env

ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py

Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

success "Flask service created."

#######################################
# Install Terminal Menu and Root WebSocket Service
#######################################

info "Installing IoT Gateway terminal menu and WebSocket VPN..."

install -o root -g root -m 755 "$INSTALL_DIR/iot-gateway.sh" /usr/local/bin/iot-gateway
install -o root -g root -m 755 "$INSTALL_DIR/iot-gateway.sh" /usr/local/bin/iot-nexus
install -d -o root -g root -m 755 /etc/iot-gateway /usr/local/lib/iot-gateway
install -o root -g root -m 644 "$INSTALL_DIR/uninstall.sh" /usr/local/lib/iot-gateway/uninstall.sh
install -o root -g root -m 644 "$INSTALL_DIR/mosquitto_assets.py" /usr/local/lib/iot-gateway/mosquitto_assets.py
if [[ ! -f /etc/iot-gateway/websocket.env ]]; then
    printf 'VPN_PORT=8765\n' > /etc/iot-gateway/websocket.env
fi
chown root:root /etc/iot-gateway/websocket.env
chmod 644 /etc/iot-gateway/websocket.env

# Keep root-executed code outside the web user's writable directory.
install -o root -g root -m 644 "$INSTALL_DIR/runner_vpn.py" "$INSTALL_DIR/vpn_crypto.py" "$INSTALL_DIR/Tun.py" /usr/local/lib/iot-gateway/
python3 -m venv /usr/local/lib/iot-gateway/venv
/usr/local/lib/iot-gateway/venv/bin/pip install 'websockets>=14.0,<16.0' 'cryptography>=46.0.3,<47.0'
# A reinstall preserves the existing VPN key; invalid permissions fail closed.
/usr/local/lib/iot-gateway/venv/bin/python /usr/local/lib/iot-gateway/vpn_crypto.py init

cat > /etc/systemd/system/iot-gateway-websocket.service << EOF
[Unit]
Description=IoT Gateway WebSocket VPN
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/usr/local/lib/iot-gateway
EnvironmentFile=/etc/iot-gateway/websocket.env
ExecStart=/usr/local/lib/iot-gateway/venv/bin/python /usr/local/lib/iot-gateway/runner_vpn.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

success "Run iot-nexus to open the menu. Start WebSocket from option 1."

#######################################
# Reload systemd
#######################################

info "Reloading systemd..."

systemctl daemon-reload

#######################################
# Enable Services
#######################################

info "Enabling services..."

systemctl enable iot-gateway-broker.service
systemctl enable iot-gateway-web.service

#######################################
# Start Services
#######################################

info "Starting services..."

systemctl restart iot-gateway-broker.service
systemctl restart iot-gateway-web.service

#######################################
# Check Status
#######################################

if systemctl is-active --quiet iot-gateway-broker.service; then
    success "Mosquitto started successfully."
else
    error "Mosquitto failed to start."
fi

if (
    source /usr/local/bin/iot-nexus
    panel_port=9000
    if [[ -r "$PANEL_CONFIG" ]]; then
        panel_port=$(sed -n 's/^PORT=//p' "$PANEL_CONFIG")
    fi
    wait_panel_ready
); then
    success "Panel is responding to HTTP."
else
    error "Panel failed to become ready."
    exit 1
fi

#######################################
# Finish
#######################################

echo
echo "========================================"
echo "      iot-nexus Installed Successfully"
echo "========================================"
echo
echo "Project Path:"
echo "  $INSTALL_DIR"
echo
echo "Useful Commands:"
echo "  iot-nexus"
echo
echo "Status:"
echo "  systemctl status iot-gateway-broker"
echo "  systemctl status iot-gateway-web"
echo
echo "Restart:"
echo "  systemctl restart iot-gateway-broker"
echo "  systemctl restart iot-gateway-web"
echo
echo "Logs:"
echo "  journalctl -u iot-gateway-broker -f"
echo "  journalctl -u iot-gateway-web -f"
echo
echo "========================================"

#!/usr/bin/env bash

set -e

#######################################
# Project Configuration
#######################################

PROJECT_NAME="mqtt-gui"
INSTALL_DIR="/opt/$PROJECT_NAME"
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
    sudo \
    mosquitto \
    mosquitto-clients

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

install -o mosquitto -g mosquitto -m 600 /dev/null "$INSTALL_DIR/.mqtt_passwords"
printf '%s:%s\n' "$MQTT_USERNAME" "$MQTT_PASSWORD" > "$INSTALL_DIR/.mqtt_passwords"
mosquitto_passwd -U "$INSTALL_DIR/.mqtt_passwords"
chown mosquitto:mosquitto "$INSTALL_DIR/.mqtt_passwords"
chmod 600 "$INSTALL_DIR/.mqtt_passwords"

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
chown mosquitto:mosquitto "$INSTALL_DIR/.mqtt_passwords"

chmod 751 "$INSTALL_DIR"

chmod 644 "$INSTALL_DIR/mosquitto.conf"
chmod 640 "$INSTALL_DIR/json.json"
chmod 600 "$INSTALL_DIR/login_signup.db" 2>/dev/null || true
chmod 600 "$INSTALL_DIR/.mqtt_credentials"
chmod 600 "$INSTALL_DIR/.mqtt_passwords"

sed -i "s|password_file \./.mqtt_passwords|password_file $INSTALL_DIR/.mqtt_passwords|" "$INSTALL_DIR/mosquitto.conf"

cat > /etc/sudoers.d/mqtt-gui << EOF
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart mosquitto-project.service
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

cat > /etc/systemd/system/mosquitto-project.service << EOF
[Unit]
Description=MQTT GUI Mosquitto Broker
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

cat > /etc/systemd/system/mqtt-gui.service << EOF
[Unit]
Description=MQTT GUI Web Panel
After=network.target mosquitto-project.service

[Service]
Type=simple

User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$INSTALL_DIR
Environment="SECRET_KEY=$SECRET_KEY"

ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py

Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

success "Flask service created."

#######################################
# Reload systemd
#######################################

info "Reloading systemd..."

systemctl daemon-reload

#######################################
# Enable Services
#######################################

info "Enabling services..."

systemctl enable mosquitto-project.service
systemctl enable mqtt-gui.service

#######################################
# Start Services
#######################################

info "Starting services..."

systemctl restart mosquitto-project.service
systemctl restart mqtt-gui.service

#######################################
# Check Status
#######################################

if systemctl is-active --quiet mosquitto-project.service; then
    success "Mosquitto started successfully."
else
    error "Mosquitto failed to start."
fi

if systemctl is-active --quiet mqtt-gui.service; then
    success "Flask started successfully."
else
    error "Flask failed to start."
fi

#######################################
# Finish
#######################################

echo
echo "========================================"
echo "      MQTT GUI Installed Successfully"
echo "========================================"
echo
echo "Project Path:"
echo "  $INSTALL_DIR"
echo
echo "Useful Commands:"
echo
echo "Status:"
echo "  systemctl status mosquitto-project"
echo "  systemctl status mqtt-gui"
echo
echo "Restart:"
echo "  systemctl restart mosquitto-project"
echo "  systemctl restart mqtt-gui"
echo
echo "Logs:"
echo "  journalctl -u mosquitto-project -f"
echo "  journalctl -u mqtt-gui -f"
echo
echo "========================================"

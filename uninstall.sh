#!/usr/bin/env bash

set -e

PROJECT_NAME="mqtt-gui"
INSTALL_DIR="/opt/$PROJECT_NAME"

GREEN="\033[0;32m"
RED="\033[0;31m"
BLUE="\033[0;34m"
NC="\033[0m"

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[ OK ]${NC} $1"
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
# Stop Services
#######################################

info "Stopping services..."

systemctl stop mqtt-gui.service 2>/dev/null || true
systemctl stop mosquitto-project.service 2>/dev/null || true

#######################################
# Disable Services
#######################################

info "Disabling services..."

systemctl disable mqtt-gui.service 2>/dev/null || true
systemctl disable mosquitto-project.service 2>/dev/null || true

#######################################
# Remove Service Files
#######################################

info "Removing service files..."

rm -f /etc/systemd/system/mqtt-gui.service
rm -f /etc/systemd/system/mosquitto-project.service
rm -f /etc/sudoers.d/mqtt-gui

systemctl daemon-reload

#######################################
# Remove Project
#######################################

info "Removing project..."

rm -rf "$INSTALL_DIR"

if id -u mqtt-gui >/dev/null 2>&1; then
    userdel mqtt-gui
fi

#######################################
# Remove Packages
#######################################

info "Removing Mosquitto..."

apt remove -y mosquitto mosquitto-clients

apt autoremove -y

#######################################
# Done
#######################################

success "Uninstall completed."

echo
echo "Removed:"
echo "  - /opt/$PROJECT_NAME"
echo "  - mqtt-gui.service"
echo "  - mosquitto-project.service"
echo "  - mqtt-gui system user"
echo "  - mosquitto"
echo "  - mosquitto-clients"
echo




#!/bin/bash
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo: sudo bash install_services.sh"
  exit 1
fi

# The user who actually owns the checkout, even when run via sudo.
REAL_USER="${SUDO_USER:-$(whoami)}"

AP_INTERFACE="${REANIMARLO_AP_INTERFACE:-wlan1}"
AP_GATEWAY_IP="${REANIMARLO_AP_GATEWAY_IP:-172.14.0.1}"

echo "This will install services for user: $REAL_USER"
echo "Reanimarlo checkout: $DIR"
read -rp "Path to your arlo-cam-api checkout (the separate dependency you cloned): " ARLO_CAM_API_DIR
if [ ! -f "$ARLO_CAM_API_DIR/server.py" ]; then
  echo "server.py not found in $ARLO_CAM_API_DIR - is that the right path?"
  exit 1
fi

TMP="$(mktemp -d)"
for name in reanimarlo-hostapd reanimarlo-dnsmasq reanimarlo-cam-api reanimarlo-viewer; do
  sed \
    -e "s#__INSTALL_DIR__#$DIR#g" \
    -e "s#__ARLO_CAM_API_DIR__#$ARLO_CAM_API_DIR#g" \
    -e "s#__USERNAME__#$REAL_USER#g" \
    -e "s#__AP_INTERFACE__#$AP_INTERFACE#g" \
    -e "s#__AP_GATEWAY_IP__#$AP_GATEWAY_IP#g" \
    "$DIR/systemd/$name.service.template" > "$TMP/$name.service"
done

echo "Installing systemd units..."
cp "$TMP"/*.service /etc/systemd/system/
rm -rf "$TMP"
systemctl daemon-reload

echo "Stopping any manually-run processes so systemd can take over cleanly..."
pkill -x hostapd 2>/dev/null || true
pkill -x dnsmasq 2>/dev/null || true
pkill -f "$ARLO_CAM_API_DIR/venv/bin/python3 server.py" 2>/dev/null || true
pkill -f "$DIR/viewer/venv/bin/python3 app.py" 2>/dev/null || true
sleep 2

echo "Enabling + starting services (in dependency order)..."
systemctl enable --now reanimarlo-hostapd.service
sleep 2
systemctl enable --now reanimarlo-dnsmasq.service
sleep 1
systemctl enable --now reanimarlo-cam-api.service
sleep 1
systemctl enable --now reanimarlo-viewer.service

echo
echo "Status:"
systemctl --no-pager status reanimarlo-hostapd reanimarlo-dnsmasq reanimarlo-cam-api reanimarlo-viewer | grep -E "●|Active:"
echo
echo "All four services are now enabled - they'll start automatically on every"
echo "boot and restart themselves if they ever crash."

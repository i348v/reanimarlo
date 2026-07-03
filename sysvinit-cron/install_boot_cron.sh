#!/bin/bash
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo: sudo bash install_boot_cron.sh"
  exit 1
fi

if command -v systemctl >/dev/null 2>&1 && systemctl status >/dev/null 2>&1; then
  echo "This system is running systemd as PID 1 - use systemd/install_services.sh"
  echo "instead, it's the more standard approach when systemd is actually available."
  exit 1
fi

# The user who actually owns the checkout, even when run via sudo.
REAL_USER="${SUDO_USER:-$(whoami)}"

echo "This will install a @reboot cron entry for user: $REAL_USER"
echo "Reanimarlo checkout: $DIR"
read -rp "Path to your arlo-cam-api checkout (the separate dependency you cloned): " ARLO_CAM_API_DIR
if [ ! -f "$ARLO_CAM_API_DIR/server.py" ]; then
  echo "server.py not found in $ARLO_CAM_API_DIR - is that the right path?"
  exit 1
fi

BOOT_SCRIPT="$DIR/sysvinit-cron/boot_all.sh"
sed \
  -e "s#__INSTALL_DIR__#$DIR#g" \
  -e "s#__ARLO_CAM_API_DIR__#$ARLO_CAM_API_DIR#g" \
  -e "s#__USERNAME__#$REAL_USER#g" \
  "$DIR/sysvinit-cron/boot_all.sh.template" > "$BOOT_SCRIPT"
chmod +x "$BOOT_SCRIPT"

echo "@reboot root $BOOT_SCRIPT" > /etc/cron.d/reanimarlo
chmod 644 /etc/cron.d/reanimarlo

echo
echo "Installed: $BOOT_SCRIPT"
echo "Registered: /etc/cron.d/reanimarlo"
echo
echo "This runs automatically on your next reboot. To test it right now"
echo "without rebooting (stop anything you have running manually first):"
echo "  sudo bash $BOOT_SCRIPT"
echo "Logs land in $DIR/boot-logs/ once it's run at least once."

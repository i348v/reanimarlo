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
WATCHDOG_SCRIPT="$DIR/sysvinit-cron/watchdog.sh"
for template in boot_all watchdog; do
  sed \
    -e "s#__INSTALL_DIR__#$DIR#g" \
    -e "s#__ARLO_CAM_API_DIR__#$ARLO_CAM_API_DIR#g" \
    -e "s#__USERNAME__#$REAL_USER#g" \
    "$DIR/sysvinit-cron/$template.sh.template" > "$DIR/sysvinit-cron/$template.sh"
  chmod +x "$DIR/sysvinit-cron/$template.sh"
done

cat > /etc/cron.d/reanimarlo <<EOF
@reboot root $BOOT_SCRIPT
*/2 * * * * root $WATCHDOG_SCRIPT
EOF
chmod 644 /etc/cron.d/reanimarlo

echo
echo "Installed: $BOOT_SCRIPT"
echo "Installed: $WATCHDOG_SCRIPT (runs every 2 minutes, restarts anything"
echo "that crashed mid-session - @reboot alone only covers actual reboots)"
echo "Registered: /etc/cron.d/reanimarlo"
echo
echo "The boot script runs automatically on your next reboot. To test it"
echo "right now without rebooting (stop anything you have running manually"
echo "first): sudo bash $BOOT_SCRIPT"
echo "Logs land in $DIR/boot-logs/ once each has run at least once."

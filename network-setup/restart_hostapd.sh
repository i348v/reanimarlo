#!/bin/bash
# Restart hostapd after editing hostapd.conf (e.g. changing the WiFi
# channel to work around interference).
IFACE=wlan1
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo: sudo bash restart_hostapd.sh"
  exit 1
fi

pkill -x hostapd 2>/dev/null
sleep 1
rm -f "$DIR/hostapd.log"
hostapd -B -f "$DIR/hostapd.log" "$DIR/hostapd.conf"
sleep 1
chmod 666 "$DIR/hostapd.log" 2>/dev/null
chmod 666 /var/run/hostapd/$IFACE 2>/dev/null
echo "hostapd restarted on the channel in hostapd.conf."

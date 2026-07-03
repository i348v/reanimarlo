#!/bin/bash
set -e

# Edit these two if your setup differs from the defaults documented in
# hostapd.conf.example / dnsmasq-wlan1.conf.example.
IFACE=wlan1
GATEWAY_IP=172.14.0.1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Custom-built hostapd with the WPS post-failure disconnect delay patch
# (see hostapd-patches/) - needed for at least one camera model that
# never completes pairing against stock hostapd. Falls back to the
# system hostapd if you haven't built the patched version.
HOSTAPD_BIN="$DIR/hostapd-src/hostapd-2.10/hostapd/hostapd"
[ -x "$HOSTAPD_BIN" ] || HOSTAPD_BIN="hostapd"

if [ "$EUID" -ne 0 ]; then
  echo "Run with sudo: sudo bash start_ap.sh"
  exit 1
fi

if [ ! -f "$DIR/hostapd.conf" ]; then
  echo "hostapd.conf not found - copy hostapd.conf.example to hostapd.conf and fill it in first."
  exit 1
fi
if [ ! -f "$DIR/dnsmasq-wlan1.conf" ]; then
  echo "dnsmasq-wlan1.conf not found - copy dnsmasq-wlan1.conf.example to dnsmasq-wlan1.conf first."
  exit 1
fi

echo "Taking $IFACE away from NetworkManager..."
nmcli device set $IFACE managed no || true
sleep 1

echo "Stopping any conflicting services..."
systemctl stop dnsmasq 2>/dev/null || true
pkill -x hostapd 2>/dev/null || true
pkill -x dnsmasq 2>/dev/null || true
sleep 1

echo "Assigning static IP $GATEWAY_IP/24 to $IFACE..."
ip link set $IFACE down
ip addr flush dev $IFACE
ip link set $IFACE up
ip addr add $GATEWAY_IP/24 dev $IFACE

echo "Starting hostapd ($HOSTAPD_BIN)..."
rm -f "$DIR/hostapd.log"
"$HOSTAPD_BIN" -B -f "$DIR/hostapd.log" "$DIR/hostapd.conf"
sleep 1
chmod 666 "$DIR/hostapd.log" 2>/dev/null || true

echo "Starting dnsmasq (scoped to $IFACE only)..."
rm -f "$DIR/dnsmasq.log"
dnsmasq -C "$DIR/dnsmasq-wlan1.conf" --log-facility="$DIR/dnsmasq.log" --log-dhcp --pid-file="$DIR/dnsmasq.pid"
sleep 1
chmod 666 "$DIR/dnsmasq.log" 2>/dev/null || true
chmod 666 /var/run/hostapd/$IFACE 2>/dev/null || true

echo "AP is up on $IFACE. WPS push-button pairing is enabled."
echo "Logs: $DIR/hostapd.log  $DIR/dnsmasq.log"
echo
echo "To pair a camera: hostapd_cli -i $IFACE wps_pbc, then press Sync on the camera"
echo "within about 2 minutes. See README.md for the full pairing walkthrough."

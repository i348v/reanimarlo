# hostapd patch: WPS post-failure disconnect delay

## What this fixes

Stock hostapd force-disconnects a station 10ms after sending the
WPS-mandated EAP-Failure (this EAP-Failure is normal - see
[`../../docs/PROTOCOL_NOTES.md`](../../docs/PROTOCOL_NOTES.md) for why).
That 10ms window is fine for most clients, but at least one camera model
we tested needs longer to gracefully tear down its own WPS session and
begin reconnecting - confirmed via packet capture against a genuine
Arlo/Netgear base station, which allows roughly a full second for this
before the camera voluntarily disconnects and reconnects on its own.
Getting force-deauthenticated by hostapd mid-teardown appears to corrupt
that camera's internal state, so it never attempts the follow-up
reconnection and pairing just permanently fails.

This patch stretches that delay from 10ms to 1.5s. It's a one-line
behavioral change with no protocol/security implications - it only
affects how long hostapd waits before kicking a station whose EAP
authentication already ended, WPS success or failure alike.

You probably only need this if a specific camera reliably completes WPS
(`WPS-SUCCESS` in the log) but never reconnects afterward. If your setup
is already working, you don't need this patch.

## Building

```bash
sudo apt install -y build-essential pkg-config libnl-3-dev libnl-genl-3-dev libssl-dev
curl -sL https://w1.fi/releases/hostapd-2.10.tar.gz -o hostapd-2.10.tar.gz
tar xzf hostapd-2.10.tar.gz
cd hostapd-2.10
patch -p1 < /path/to/hostapd-2.10-wps-deauth-delay.patch
cd hostapd
cp defconfig .config
sed -i 's/^#CONFIG_WPS=y/CONFIG_WPS=y/' .config
make -j"$(nproc)"
```

This produces a `hostapd` binary in that directory - it doesn't touch
your system's package-managed `/sbin/hostapd`. Point `start_ap.sh` (or
your systemd unit) at this binary's full path instead of the system one.

## Verifying it applied

```bash
grep "1500 ms" src/ap/sta_info.c
```

## Known quirk: `-f logfile` produces no file when combined with `-B`

The system-packaged hostapd handles `-B -f hostapd.log` (background +
log to file) fine; this from-source build doesn't - the log file never
gets created, though the daemon itself runs completely normally (`iw dev
<iface> station dump` and `hostapd_cli` both work as expected). This
looks like a build-configuration difference from the Debian package
rather than anything related to the patch itself. If you need live logs
for troubleshooting, run it in the foreground with shell redirection
instead: `sudo ./hostapd -f hostapd.log path/to/hostapd.conf` (no `-B`),
or `sudo ./hostapd path/to/hostapd.conf 2>&1 | tee hostapd.log`.

Should print the patched log line. If you're troubleshooting and want to
confirm which binary is actually running: `ps aux | grep hostapd` shows
the full path it was launched from.

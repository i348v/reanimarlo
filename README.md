# Reanimarlo

Bring your own-supported-hardware Arlo Pro / Pro 2 cameras back to life after
Arlo drops cloud support for them — no cloud, no subscription, no phoning
home. Your PC becomes the camera's "base station," and you get a local web
dashboard for live view and motion-triggered recording.

**Not affiliated with Arlo or Netgear in any way.** This is a hobbyist
reverse-engineering project for reclaiming hardware you already own after
the vendor discontinued support for it.

## Is this for you?

This targets the Arlo Pro / Pro 2 generation (camera models like
`VMC4030P`) — cameras that connect to a base station over **plain
802.11 WiFi with WPA2-PSK**, not a proprietary radio. If you're not sure
which generation you have: if the model number starts with `VMC30` or
`VMC40`, this is the right project. Later generations (Pro 3+, Ultra) use a
different protocol and aren't covered here.

## What you get

- **Live view** in a browser dashboard, multiple cameras at once
- **Motion-triggered recording**, automatically stitched across the
  camera's streaming limitations (see [Known limitations](#known-limitations))
  so a long motion event doesn't get cut short
- A permanent timestamp + camera name burned into every saved clip
- Battery and signal strength shown per camera, useful when picking mounting
  spots
- Optional local object detection (person/cat/dog/vehicle) and face
  clustering - fully offline, no cloud AI - though **be aware this is
  genuinely CPU-heavy**, see [requirements-ai.txt](viewer/requirements-ai.txt)
- Runs as systemd services that survive a reboot and restart themselves if
  they crash

## What this depends on

This project is the *local dashboard + fake-base-station networking layer*
sitting on top of [`Meatballs1/arlo-cam-api`](https://github.com/Meatballs1/arlo-cam-api),
which did the hard work of reverse-engineering the camera-to-base-station
control protocol. That project has no license file, so **this repo does not
copy any of its code** — you clone it yourself as a separate dependency, and
`docs/ARLO_CAM_API_PATCH.md` documents the additions this project needs on
top of it (as new code to add, not a diff of their file).

Also worth knowing about: [`brianschrameck/arlo-cam-api`](https://github.com/brianschrameck/arlo-cam-api)
is a fork of the same project with broader device support (including
video doorbells) and more active maintenance - may be a better starting
point depending on your hardware. Either should work with the patch
instructions here, since both expose the same core protocol.

## Hardware you'll need

- A camera from a generation that's actually been discontinued/unsupported
  by Arlo (check the Arlo app/community forums for your specific model)
- **A WiFi adapter with mainline Linux driver support** - this matters a
  lot. We used the **Alfa AWUS036AM** (MediaTek MT7612U chipset): it just
  works with the in-kernel `mt76x2u` driver on a recent Linux kernel, no
  DKMS/out-of-tree driver compilation needed, and it supports AP mode +
  WPS. Adapters based on Realtek chipsets (e.g. the older AWUS036ACH) often
  need a separately-compiled driver that breaks on kernel updates - avoid
  those if you can.
- A Linux machine to run this on - built-in WiFi for your normal internet,
  the USB adapter dedicated to the fake camera network
- **Physically separate the USB adapter from the machine** with an
  extension cable if possible. Two 2.4GHz radios that close together cause
  real RF interference with each other, even on non-overlapping channels -
  we saw it firsthand as a collapsed RX bitrate on the built-in WiFi while
  the adapter was active inches away.

## Quick start

### 1. Set up the fake base station

```bash
git clone <this-repo-url> reanimarlo
cd reanimarlo/network-setup
cp hostapd.conf.example hostapd.conf
cp dnsmasq-wlan1.conf.example dnsmasq-wlan1.conf
# Edit hostapd.conf: set ctrl_interface_group to your username, pick a
# network name, and generate a real passphrase:
openssl rand -base64 12 | tr -dc 'a-zA-Z0-9' | head -c 16; echo
sudo apt install -y hostapd dnsmasq
sudo bash start_ap.sh
```

Check `iw dev wlan1 info` afterward to confirm the AP came up.

**If your camera won't connect and you're on Ubuntu/Debian with `ufw`
enabled**, this is very likely why: ufw blocks a machine from acting as a
DHCP server by default (sane security default, wrong for this use case).
Fix it narrowly, only on your fake-AP interface:

```bash
sudo ufw allow in on wlan1 to any port 67 proto udp
sudo ufw allow in on wlan1 to any port 68 proto udp
sudo ufw allow in on wlan1 to any port 4000 proto tcp
sudo ufw allow in on wlan1 to any port 5000 proto tcp
```

This cost us a couple of hours the first time - the AP looked perfectly
healthy (WPS handshake succeeded, WPA2 completed) but the camera's DHCP
broadcast just silently vanished before it ever reached dnsmasq.

**If a specific camera reliably reaches `WPS-SUCCESS` in `hostapd.log` but
never reconnects afterward**, no matter how many times you retry: this is
a real, fixed, known issue with a one-line patch -
[`network-setup/hostapd-patches/`](network-setup/hostapd-patches/) has
the full writeup and build instructions. `start_ap.sh` automatically uses
the patched build if you've built it. Most cameras don't need this.

### 2. Set up arlo-cam-api

```bash
git clone https://github.com/Meatballs1/arlo-cam-api
cd arlo-cam-api
python3 -m venv venv
./venv/bin/pip install flask pyyaml requests python-vlc webhooks wrapt cached-property standardjson
```

Now apply the additions documented in
[`docs/ARLO_CAM_API_PATCH.md`](docs/ARLO_CAM_API_PATCH.md) - a heartbeat
thread and a motion-recording rewrite that this project needs. Read
[`docs/PROTOCOL_NOTES.md`](docs/PROTOCOL_NOTES.md) first if you want to
understand *why* before copying code in.

Start it: `./venv/bin/python3 server.py`

### 3. Pair your camera

Battery Arlo cameras only accept **one WPS pairing session at a time** -
if you have multiple cameras, pair them one at a time, or the AP will hand
the session to whichever one grabs it first and you'll be confused why the
other one never connects (ask us how we know).

```bash
hostapd_cli -i wlan1 wps_pbc
```

Then immediately press and hold the **Sync** button on the camera. Watch
`sudo tail -f network-setup/hostapd.log` - you're looking for
`WPS-SUCCESS` followed by `EAPOL-4WAY-HS-COMPLETED`. If the camera was
previously paired to a real Arlo base station, Sync alone won't
re-trigger pairing mode - you'll need to factory-reset it first (hold
Sync for ~15s until the LED flashes amber, on most Pro/Pro2 units).

### 4. Set up the dashboard

```bash
cd reanimarlo/viewer
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python3 app.py
```

Open `http://localhost:8080`.

### 5. Make it survive a reboot

```bash
cd reanimarlo/systemd
sudo bash install_services.sh
```

It'll ask where you cloned `arlo-cam-api` and take care of the rest.

## Known limitations

**Live view isn't perfectly smooth, and that's not a bug in this project.**
Packet-capture analysis (see `docs/PROTOCOL_NOTES.md`) confirms these
cameras cut off RTP video after roughly 10-30 seconds of continuous
streaming, consistently, regardless of client behavior - both video and
RTCP halt at the exact same instant with no error, which looks like an
intentional firmware/hardware duty-cycle limit on a camera that was never
designed for sustained live view (it's built for short motion clips, and
even genuine Arlo base station hardware only supports local USB
recording in motion-clip form, not continuous streaming). The dashboard
auto-reconnects when this happens rather than leaving you with a frozen
frame, but expect brief "reconnecting" moments during long live-view
sessions.

**Motion recording is the reliable use case.** Since clips only need to
cover a motion event (usually well under the streaming ceiling), and the
recording pipeline stitches multiple camera sessions together
automatically when an event does outlast one session, this holds up much
better than live view.

**Keeping a camera reachable costs it battery.** The heartbeat that keeps
a camera responsive (see `PROTOCOL_NOTES.md`) is meaningfully more active
than these cameras' stock duty cycle. Expect shorter battery life than you
got from the real Arlo cloud service.

## License

Everything in this repository is MIT licensed (see `LICENSE`) - this is
100% original code written for this project, no code from
`arlo-cam-api` is included (see [What this depends on](#what-this-depends-on)).

`viewer/static/hls.min.js` is [hls.js](https://github.com/video-dev/hls.js),
Apache-2.0 licensed, included unmodified.

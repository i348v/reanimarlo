# Protocol notes

Findings from reverse-engineering the camera↔base-station link on Arlo
Pro/Pro2-generation cameras (tested on `VMC4030P`), gathered via extensive
packet capture (`tcpdump`) and empirical testing. These go beyond what
`arlo-cam-api` documents and explain several non-obvious behaviors you'll
hit if you build on top of it.

## The camera link is plain WiFi, not a proprietary radio

Worth stating explicitly since it's easy to assume otherwise (older Arlo
literature and some forum posts describe a proprietary long-range 2.4GHz
link for the wire-free camera generation). For Pro/Pro2, it's standard
802.11 with WPA2-PSK. A commodity WiFi adapter in AP mode is all you need
- confirmed by `arlo-cam-api`'s own hostapd-based setup and reproduced
independently here.

## The camera needs to hear from its base station roughly every 10s

The camera's initial `registerSet` payload includes
`"MaxMissedBeaconTime": 10`. Nothing in `arlo-cam-api`'s stock `server.py`
proactively contacts the camera - it's purely reactive (one-shot
request/response per TCP connection). Left alone, the camera decides
nobody's listening and drops into a much less responsive state: its
status LED goes dark, and it stops promptly ACKing or responding to
commands.

**Fix**: a background thread that sends a `statusRequest` to every
registered camera every ~8 seconds (under the 10s threshold), for as long
as the server runs. See `ARLO_CAM_API_PATCH.md`. This measurably keeps the
camera in a responsive state - confirmed by `inactive time` in
`iw dev wlan1 station dump` staying in the single-digit seconds instead of
climbing into minutes.

Trade-off: this is a much higher duty cycle than the camera's stock
behavior, so expect reduced battery life compared to what you got from the
real Arlo cloud service.

## hostapd's default inactivity timeout fights the camera's sleep cycle

`ap_max_inactivity` defaults to 300s in hostapd. A camera that's
legitimately still there but quiet (even with the heartbeat above, briefly)
gets forcibly deauthenticated, requiring a full WPA2 reassociation - and if
that's flaky, sometimes a full WPS re-pairing dance. Set
`ap_max_inactivity=3600` in `hostapd.conf`.

## The RTSP session dies over neglected tracks - or maybe just because

Pulling video via `ffmpeg` (or a from-scratch RTSP client), the `DESCRIBE`
response advertises **three tracks**: one video (H.264) and two audio
(AAC and Opus). A naive client `SETUP`s all three tracks but typically
only sends RTCP receiver reports on the track(s) it actually consumes
(the video track, if you don't care about audio) - the two neglected
audio tracks get zero RTCP ever.

Packet capture across several runs showed:

- With all 3 tracks set up (2 neglected): the session died consistently
  around **10 seconds** after `PLAY`.
- With only the video track set up (`-allowed_media_types video` in
  ffmpeg, so the audio tracks are never `SETUP` at all): sessions
  regularly ran **20-30+ seconds** before dying.

That's a real, reproducible improvement, so the fix is included. But full
transparency: even with only the video track set up and healthy periodic
RTCP being sent throughout, **the session still eventually dies** - video
and RTCP halt at the exact same instant, cleanly, with no `TEARDOWN` or
error. Duration varies run to run (not a fixed timer), which points to a
genuine hardware/firmware duty-cycle limit on a camera that was never
designed for sustained streaming, not a protocol negotiation you can fully
out-clever. Corroborating evidence: even the **real** Arlo base station
hardware (VMB4500-class) only supports local recording to USB in
motion-clip form - not continuous streaming - suggesting this is simply
how these cameras are built to operate.

**Bonus oddity**: with all 3 tracks set up, roughly 40% of the camera's
outbound packets (including real RTCP Sender Reports, identifiable by an
SDES chunk literally containing the string `"omnivision"` - the sensor
chip vendor) were addressed to **destination port 0** - a firmware bug,
unrelated to the audio tracks having no `SETUP`'d destination. Wasted
bandwidth on the camera's side, though it didn't appear to be the primary
driver of the session ending.

**Practical takeaway**: don't try to prevent the stall, detect and recover
from it. `viewer/app.py`'s `stream_watchdog()` kills a stream once its
output hasn't grown in 12 seconds, and the frontend automatically
reconnects, freezing the last frame with a small "Reconnecting…" indicator
rather than going blank. This is the correct design for this hardware, not
a workaround for an unsolved bug.

## ffmpeg can hang indefinitely on a dead RTSP input

A stalled RTSP session doesn't make `ffmpeg` exit - it can sit blocked in
a read forever, ignoring both the `-t` duration flag (which limits output
duration, not wall-clock time), *and* `SIGTERM`. Confirmed: a test process
sat alive for 6+ minutes after the camera's stream died, immune to
`terminate()`. Whatever monitors your `ffmpeg` process must poll the
output file for staleness itself and send `SIGKILL`, not rely on the
process exiting on its own or responding to a polite signal.

## RTSP `SETUP`/`PLAY` timing after waking the camera is tight

The camera's RTSP listener only accepts connections in a short window
right after being "woken" (a `userstreamactive` + `statusrequest` call
pair). Miss it and you get `Connection refused`. Retry the whole
wake-then-connect sequence rather than just retrying the connect - a
handful of attempts, each preceded by a fresh wake call, is reliable in
practice.

## Recording always wins over live view

Since the camera can only serve one RTSP client at a time (see above), live
view and motion recording are in direct competition for the same resource.
Given the two, **recording is the one that actually matters** - there's
rarely urgency around watching live, but a missed motion recording is gone
for good. So the two sides of the lock aren't symmetric:

- If a motion alert can't get the RTSP lock because live view is holding
  it, it doesn't just wait - it leaves a preempt request. The viewer's
  `stream_watchdog` checks for this every ~5s and voluntarily stops the
  live stream, releasing the lock.
- While a recording is actually in progress, the viewer also holds off on
  auto-reconnecting live view for that camera (surfaced in the dashboard as
  "Recording in progress…") rather than immediately reconnecting and
  taking the lock right back, which would just re-trigger the same
  contention in a loop.

Both signals are plain marker files in the shared lock directory
(`<ip>.preempt` and `<ip>.recording`) rather than anything requiring the
two processes to talk to each other directly - consistent with how the
RTSP lock itself works.

## Firewall note if you're testing DHCP with `ufw`

Not camera-specific, but cost real time: `ufw`'s default rules explicitly
catch outbound-server-role DHCP traffic (`udp dport 67`) and route it to a
chain that drops anything addressed to a broadcast destination - a sane
default to stop your machine acting as a rogue DHCP server, wrong for this
project where that's exactly the intent. See the README's quick-start for
the narrow fix.

# Additions to arlo-cam-api's server.py

This project needs three things added on top of a stock
[`arlo-cam-api`](https://github.com/Meatballs1/arlo-cam-api) checkout. This
doc describes each as new code plus where it goes, rather than a diff of
their file (see the main README for why). Read
[`PROTOCOL_NOTES.md`](PROTOCOL_NOTES.md) for the reasoning behind each one.

## 1. Imports

Add near the top of `server.py`, alongside the existing imports:

```python
import subprocess
```

(`os` is very likely already imported; add it too if not.)

## 2. RTSP mutual-exclusion lock

This camera's RTSP server only serves one client at a time. If your setup
also runs a live-view puller (like this project's `viewer/app.py`)
alongside motion recording, they'll stomp on each other without this.
Add near the top of the file, after the existing `recorder_lock`/`recorders`
globals:

```python
recording_in_progress = set()
motion_active = {}

RTSP_LOCK_DIR = os.environ.get("REANIMARLO_RTSP_LOCK_DIR", "/tmp/reanimarlo_rtsp_locks")
os.makedirs(RTSP_LOCK_DIR, exist_ok=True)


def _rtsp_lock_path(camera_ip):
    return os.path.join(RTSP_LOCK_DIR, camera_ip.replace(".", "_") + ".lock")


def acquire_rtsp_lock(camera_ip):
    path = _rtsp_lock_path(camera_ip)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            if attempt == 1:
                return None
            try:
                with open(path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                return None  # genuinely held by a live process
            except (ProcessLookupError, ValueError, FileNotFoundError):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
    return None


def release_rtsp_lock(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
```

Use the *same* `REANIMARLO_RTSP_LOCK_DIR` value (or matching default) as
`viewer/app.py` so both processes agree on where the lock files live -
that's what actually enforces the "one RTSP consumer at a time" rule
across both programs.

## 3. Heartbeat thread

Keeps registered cameras responsive (see `PROTOCOL_NOTES.md` for why this
is necessary). Add this class, and start it at the bottom of the file
alongside wherever the existing server threads get started:

```python
class HeartbeatThread(threading.Thread):
    """The camera's initial registerSet includes MaxMissedBeaconTime=10,
    implying it expects to hear from the base station roughly that often
    or it assumes nobody's listening. Nothing else in this server
    proactively contacts the camera, so ping it periodically."""
    def run(self):
        while True:
            try:
                with sqlite3.connect('arlo.db') as conn:
                    c = conn.cursor()
                    c.execute("SELECT serialnumber FROM camera")
                    serials = [row[0] for row in c.fetchall()]
                for serial in serials:
                    camera = Camera.from_db_serial(serial)
                    if camera is not None:
                        camera.status_request()
            except Exception as e:
                print(f"HeartbeatThread error: {e}")
            time.sleep(8)
```

Start it the same way the existing `ServerThread` gets started, e.g.:

```python
heartbeat_thread = HeartbeatThread(daemon=True)
heartbeat_thread.start()
```

## 4. Motion-recording rewrite with session stitching

The stock recorder captures a single continuous clip via VLC. Given the
camera's streaming ceiling (`PROTOCOL_NOTES.md`), a motion event that
outlasts one session gets truncated. This replaces the motion-alert
recording logic with a version that:

- Uses `ffmpeg` instead of VLC (more reliable against this camera in
  practice, confirmed via extensive testing)
- Detects a stalled/dead session by polling the output file for growth
  and force-kills it (`ffmpeg` does **not** reliably respect `-t` or
  `SIGTERM` against a dead RTSP input - see `PROTOCOL_NOTES.md`)
- On a stall, immediately reconnects and starts a new segment,
  continuing until the configured `MotionRecordingTimeout` budget is
  used up or a `motionTimeoutAlert` says the motion has actually ended
- Concatenates all segments into one continuous final recording

In the code handling incoming `alert` messages, find where
`AlertType == "pirMotionAlert"` triggers a recording, and replace that
recording-triggering logic with:

```python
if alert_type == "pirMotionAlert" and RECORD_ON_MOTION_ALERT:
   filename = f"{RECORDING_BASE_PATH}{camera.serial_number}_{timestr}_motion.mpg"
   motion_active[camera.ip] = True

   def _record_one_segment(cam, part_fname, already_warm=False):
       wake_settle = 0.2 if already_warm else 1.0
       connect_check = 1.0 if already_warm else 2.5
       stall_tick = 0.5 if already_warm else 1.0
       stall_ticks_needed = 4 if already_warm else 5
       for _ in range(5):
           lock_path = acquire_rtsp_lock(cam.ip)
           if lock_path is None:
               time.sleep(2.0)
               continue
           try:
               cam.set_user_stream_active(0)
               cam.status_request()
               time.sleep(wake_settle)
               proc = subprocess.Popen(
                   ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-allowed_media_types", "video",
                    "-rtsp_transport", "udp", "-i", f"rtsp://{cam.ip}/live",
                    "-an", "-c:v", "copy", "-f", "mpegts", part_fname],
               )
               time.sleep(connect_check)
               if proc.poll() is not None:
                   continue

               segment_start = time.time()
               last_size = -1
               stable_ticks = 0
               while True:
                   time.sleep(stall_tick)
                   if proc.poll() is not None:
                       break
                   try:
                       size = os.path.getsize(part_fname)
                   except FileNotFoundError:
                       size = 0
                   stable_ticks = stable_ticks + 1 if size == last_size else 0
                   last_size = size
                   elapsed_total = time.time() - segment_start
                   if stable_ticks >= stall_ticks_needed or elapsed_total >= MOTION_RECORDING_TIMEOUT:
                       proc.kill()
                       proc.wait()
                       break
               return os.path.exists(part_fname) and os.path.getsize(part_fname) > 0
           finally:
               release_rtsp_lock(lock_path)
       return False

   def _wake_and_record(cam=camera, base_fname=filename):
       with recorder_lock:
           if cam.ip in recording_in_progress:
               s_print(f"Motion recording: already recording {cam.ip}, skipping overlapping alert")
               return
           recording_in_progress.add(cam.ip)
       parts = []
       start_time = time.time()
       try:
           part_num = 0
           while time.time() - start_time < MOTION_RECORDING_TIMEOUT:
               if not motion_active.get(cam.ip):
                   break
               part_num += 1
               part_fname = f"{base_fname}.part{part_num}.ts"
               if _record_one_segment(cam, part_fname, already_warm=(part_num > 1)):
                   parts.append(part_fname)
               else:
                   s_print(f"Motion recording: gave up reconnecting to {cam.ip} after part {part_num}")
                   break

           if len(parts) == 1:
               os.rename(parts[0], base_fname)
           elif len(parts) > 1:
               concat_list = f"{base_fname}.concat.txt"
               with open(concat_list, "w") as f:
                   for p in parts:
                       f.write(f"file '{os.path.abspath(p)}'\n")
               subprocess.run(
                   ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat",
                    "-safe", "0", "-i", concat_list, "-c", "copy", base_fname],
                   check=False,
               )
               for p in parts:
                   try:
                       os.remove(p)
                   except FileNotFoundError:
                       pass
               os.remove(concat_list)
               s_print(f"Motion recording: stitched {len(parts)} segments into {base_fname}")
       finally:
           motion_active.pop(cam.ip, None)
           with recorder_lock:
               recording_in_progress.discard(cam.ip)
   threading.Thread(target=_wake_and_record, daemon=True).start()
```

Wrap any existing webhook notification call for this alert (e.g. a
`webhook_manager.motion_detected(...)` call) in a `try/except` if it isn't
already - a webhook failure shouldn't be able to crash the thread that's
supposed to send the protocol-level Ack back to the camera.

And wherever `alert_type == "motionTimeoutAlert"` is handled, replace the
body with:

```python
elif alert_type == "motionTimeoutAlert":
   motion_active[self.ip] = False
```

## 5. Config

In `config.yaml`, set:

```yaml
RecordOnMotionAlert: true
RecordingBasePath: "/path/to/your/recordings/"  # match REANIMARLO_RECORDINGS_DIR
```

If you're not using the webhook features, point the `*WebHookUrl` keys at
something harmless (or make sure your webhook call is wrapped in
`try/except` per step 4) rather than leaving them at defaults that might
hit a real external service.

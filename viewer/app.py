import atexit
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request, send_from_directory, abort

# --- Configuration --------------------------------------------------------
# Everything here is overridable via environment variable so this doesn't
# need editing for your own paths/network setup. See README.md for the
# full list and what each one means.
ARLO_API = os.environ.get("REANIMARLO_API_URL", "http://127.0.0.1:5000")
AP_INTERFACE = os.environ.get("REANIMARLO_AP_INTERFACE", "wlan1")
AP_GATEWAY_IP = os.environ.get("REANIMARLO_AP_GATEWAY_IP", "172.14.0.1")

# iw/ip usually live in /sbin or /usr/sbin, which isn't always on a normal
# (non-root) user's PATH - resolved once at startup, searching those
# directories explicitly rather than trusting this process's own inherited
# PATH, since that's exactly what's missing them when this runs via
# su/cron/a service manager instead of an interactive terminal (where it
# would otherwise silently fail, caught by a broad except and misread as
# "no data available" rather than "command not found").
_bin_search_path = "/sbin:/usr/sbin:/bin:/usr/bin:" + os.environ.get("PATH", "")
IW_BIN = shutil.which("iw", path=_bin_search_path) or "iw"
IP_BIN = shutil.which("ip", path=_bin_search_path) or "ip"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HLS_DIR = os.path.join(BASE_DIR, "hls")
RECORDINGS_DIR = os.environ.get(
    "REANIMARLO_RECORDINGS_DIR", os.path.join(BASE_DIR, "recordings")
)
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "snapshots")
RTSP_LOCK_DIR = os.environ.get(
    "REANIMARLO_RTSP_LOCK_DIR", os.path.join(BASE_DIR, "rtsp_locks")
)

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(RTSP_LOCK_DIR, exist_ok=True)

# Object/face detection (YOLOv8n + insightface) is CPU-heavy enough to
# visibly bog down a modest machine when it runs synchronously on every
# single recording. Off by default - flip to True (or wire up an on-demand
# trigger) once you know your hardware has headroom to spare, e.g. running
# "nice"d or only outside active-use hours.
AI_DETECTION_ENABLED = os.environ.get("REANIMARLO_AI_DETECTION", "0") == "1"

# Subset of the 80 COCO classes worth surfacing as filterable labels for a
# home security context - the full class list includes a lot of irrelevant
# objects (toaster, frisbee, etc.) that would just clutter the filter.
RELEVANT_CLASSES = {
    "person", "cat", "dog", "bird", "horse", "sheep", "cow",
    "car", "truck", "bus", "motorcycle", "bicycle",
}
_yolo_model = None
_yolo_lock = threading.Lock()


def _get_yolo_model():
    global _yolo_model
    with _yolo_lock:
        if _yolo_model is None:
            from ultralytics import YOLO
            _yolo_model = YOLO("yolov8n.pt")
        return _yolo_model


def _detect_labels(mp4_path, duration):
    """Sample a handful of frames spread across the clip and tag whatever
    relevant object classes YOLOv8n finds - cheap enough to run inline in
    remux_watcher rather than needing a separate queue/worker."""
    try:
        model = _get_yolo_model()
    except Exception as e:
        print(f"YOLO model unavailable, skipping labeling: {e}")
        return []

    dur = duration or 10.0
    sample_count = min(6, max(2, int(dur // 5) + 1))
    frame_dir = mp4_path[:-4] + "_frames"
    os.makedirs(frame_dir, exist_ok=True)
    try:
        frame_paths = []
        for i in range(sample_count):
            t = max(0.2, (dur / sample_count) * i + 0.5)
            fpath = os.path.join(frame_dir, f"f{i}.jpg")
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", mp4_path,
                 "-vframes", "1", fpath],
                check=False,
            )
            if r.returncode == 0 and os.path.exists(fpath):
                frame_paths.append(fpath)

        labels = set()
        for fpath in frame_paths:
            try:
                results = model(fpath, verbose=False)
                for res in results:
                    for c in res.boxes.cls:
                        name = model.names[int(c)]
                        if name in RELEVANT_CLASSES:
                            labels.add(name)
            except Exception as e:
                print(f"YOLO inference failed on {fpath}: {e}")
        return sorted(labels)
    finally:
        import shutil
        shutil.rmtree(frame_dir, ignore_errors=True)


# --- Face detection / unsupervised clustering ---------------------------
# No names are assigned automatically. Every detected face is matched
# against previously-seen faces by embedding similarity; a match reuses
# that cluster's ID, a non-match starts a new one and fires a desktop
# notification so a person can be named later once someone's around to
# do it. This is intentionally the whole "residents vs visitors" system
# minus the naming step - naming just attaches a label to a cluster.
FACES_DB_PATH = os.path.join(BASE_DIR, "faces.json")
FACES_DIR = os.path.join(BASE_DIR, "faces")
os.makedirs(FACES_DIR, exist_ok=True)
FACE_MATCH_THRESHOLD = 0.5  # cosine similarity on insightface's normed embeddings
_face_app = None
_face_model_lock = threading.Lock()
_faces_db_lock = threading.Lock()


def _get_face_app():
    global _face_app
    with _face_model_lock:
        if _face_app is None:
            from insightface.app import FaceAnalysis
            fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
            fa.prepare(ctx_id=-1, det_size=(640, 640))
            _face_app = fa
        return _face_app


def _load_faces_db():
    if os.path.exists(FACES_DB_PATH):
        try:
            with open(FACES_DB_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"clusters": [], "next_id": 1}


def _save_faces_db(db):
    tmp = FACES_DB_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f)
    os.replace(tmp, FACES_DB_PATH)


def _cosine_sim(a, b):
    import numpy as np
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def _notify_new_person(camera_name):
    try:
        subprocess.run(
            ["notify-send", "Reanimarlo — New Person",
             f"Unrecognized face seen on {camera_name}. Open the dashboard to name them."],
            check=False, env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0.0")},
        )
    except Exception as e:
        print(f"notify-send failed: {e}")


def _detect_faces(mp4_path, duration, camera_name):
    try:
        import cv2
        face_app = _get_face_app()
    except Exception as e:
        print(f"Face model unavailable, skipping face detection: {e}")
        return []

    dur = duration or 10.0
    sample_count = min(6, max(2, int(dur // 5) + 1))
    frame_dir = mp4_path[:-4] + "_faceframes"
    os.makedirs(frame_dir, exist_ok=True)
    cluster_ids_seen = set()
    try:
        for i in range(sample_count):
            t = max(0.2, (dur / sample_count) * i + 0.5)
            fpath = os.path.join(frame_dir, f"f{i}.jpg")
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", mp4_path,
                 "-vframes", "1", fpath],
                check=False,
            )
            if r.returncode != 0 or not os.path.exists(fpath):
                continue
            img = cv2.imread(fpath)
            if img is None:
                continue
            try:
                faces = face_app.get(img)
            except Exception as e:
                print(f"Face detection failed on {fpath}: {e}")
                continue

            for face in faces:
                emb = face.normed_embedding.tolist()
                with _faces_db_lock:
                    db = _load_faces_db()
                    best_id, best_sim = None, -1.0
                    for cluster in db["clusters"]:
                        sim = _cosine_sim(emb, cluster["embedding"])
                        if sim > best_sim:
                            best_sim, best_id = sim, cluster["id"]

                    if best_id is not None and best_sim >= FACE_MATCH_THRESHOLD:
                        cluster = next(c for c in db["clusters"] if c["id"] == best_id)
                        n = cluster["count"]
                        cluster["embedding"] = [
                            (e * n + v) / (n + 1) for e, v in zip(cluster["embedding"], emb)
                        ]
                        cluster["count"] = n + 1
                        cluster["last_seen"] = time.time()
                        cluster_ids_seen.add(best_id)
                    else:
                        new_id = db["next_id"]
                        db["next_id"] += 1
                        x1, y1, x2, y2 = [int(v) for v in face.bbox]
                        crop = img[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
                        thumb_name = f"cluster_{new_id}.jpg"
                        if crop.size > 0:
                            cv2.imwrite(os.path.join(FACES_DIR, thumb_name), crop)
                        db["clusters"].append({
                            "id": new_id,
                            "name": None,
                            "embedding": emb,
                            "thumbnail": thumb_name,
                            "first_seen": time.time(),
                            "last_seen": time.time(),
                            "count": 1,
                        })
                        cluster_ids_seen.add(new_id)
                        _notify_new_person(camera_name)

                    _save_faces_db(db)
        return [f"face:cluster_{cid}" for cid in sorted(cluster_ids_seen)]
    finally:
        import shutil
        shutil.rmtree(frame_dir, ignore_errors=True)


app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/api/faces")
def api_faces():
    db = _load_faces_db()
    return jsonify([
        {
            "id": c["id"],
            "name": c["name"],
            "thumbnail_url": f"/faces/{c['thumbnail']}",
            "first_seen": c["first_seen"],
            "last_seen": c["last_seen"],
            "count": c["count"],
        }
        for c in sorted(db["clusters"], key=lambda c: -c["last_seen"])
    ])


@app.route("/api/faces/<int:cluster_id>/name", methods=["POST"])
def api_name_face(cluster_id):
    name = (request.get_json(silent=True, force=True) or {}).get("name", "").strip()
    with _faces_db_lock:
        db = _load_faces_db()
        cluster = next((c for c in db["clusters"] if c["id"] == cluster_id), None)
        if cluster is None:
            abort(404)
        cluster["name"] = name or None
        _save_faces_db(db)
    return jsonify({"result": True})


@app.route("/faces/<path:filename>")
def faces_static(filename):
    return send_from_directory(FACES_DIR, filename)


streams = {}
stream_lock_paths = {}
streams_lock = threading.Lock()


def _kill_all_streams():
    # A plain `kill <pid>` (SIGTERM) on this process previously left any
    # active ffmpeg child running as an orphan (reparented to PID 1) -
    # confirmed in testing: one survived for 27+ minutes after an app
    # restart, completely untracked, still serving "live" video that the
    # Stop button had no way to reach. Belt-and-suspenders: run this on
    # both a clean exit and on SIGTERM.
    with streams_lock:
        for proc in streams.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in streams.values():
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def _handle_sigterm(signum, frame):
    _kill_all_streams()
    sys.exit(0)


atexit.register(_kill_all_streams)
signal.signal(signal.SIGTERM, _handle_sigterm)

# This camera's RTSP server can only serve one client at a time; concurrent
# pulls (live view vs. motion recording vs. snapshot) stall/corrupt each
# other instead of erroring cleanly. This lock, shared by filename convention
# with the arlo-cam-api patch (see docs/ARLO_CAM_API_PATCH.md), enforces one
# RTSP consumer per camera across both processes.


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


def _consume_preempt_flag(camera_ip):
    """True (and clears the flag) if a motion recording is waiting on this
    camera's RTSP lock. Recording matters more than live view - the camera
    can only serve one RTSP client at a time, and there's never a rush to
    watch live - so live view yields rather than making the recorder retry
    indefinitely against a lock we're just sitting on."""
    path = os.path.join(RTSP_LOCK_DIR, camera_ip.replace(".", "_") + ".preempt")
    if os.path.exists(path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return True
    return False


def release_rtsp_lock(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def get_cameras():
    r = requests.get(f"{ARLO_API}/camera", timeout=5)
    r.raise_for_status()
    return r.json()


def get_camera(serial):
    for cam in get_cameras():
        if cam["serial_number"] == serial:
            return cam
    return None


def _stream_dir(serial):
    d = os.path.join(HLS_DIR, serial)
    os.makedirs(d, exist_ok=True)
    return d


def _ap_signal_by_mac():
    """dBm signal for each currently-associated station, keyed by MAC."""
    try:
        out = subprocess.run(
            [IW_BIN, "dev", AP_INTERFACE, "station", "dump"],
            capture_output=True, text=True, check=False, timeout=5,
        ).stdout
    except Exception:
        return {}
    result = {}
    current_mac = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Station "):
            current_mac = line.split()[1]
        elif line.startswith("signal:") and current_mac:
            try:
                result[current_mac] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return result


def _ap_ip_to_mac():
    try:
        out = subprocess.run(
            [IP_BIN, "neigh", "show", "dev", AP_INTERFACE],
            capture_output=True, text=True, check=False, timeout=5,
        ).stdout
    except Exception:
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "lladdr" in parts:
            result[parts[0]] = parts[parts.index("lladdr") + 1]
    return result


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/cameras")
def api_cameras():
    cams = get_cameras()
    signal_by_mac = _ap_signal_by_mac()
    ip_to_mac = _ap_ip_to_mac()
    with streams_lock:
        for cam in cams:
            proc = streams.get(cam["serial_number"])
            cam["streaming"] = proc is not None and proc.poll() is None

            mac = ip_to_mac.get(cam.get("ip"))
            cam["signal_dbm"] = signal_by_mac.get(mac) if mac else None

            cam["recording"] = bool(cam.get("ip")) and os.path.exists(
                os.path.join(RTSP_LOCK_DIR, cam["ip"].replace(".", "_") + ".recording")
            )

            cam["battery_pct"] = None
            cam["signal_indicator"] = None
            try:
                r = requests.get(f"{ARLO_API}/camera/{cam['serial_number']}", timeout=3)
                if r.ok:
                    status = r.json()
                    cam["battery_pct"] = status.get("BatPercent")
                    cam["signal_indicator"] = status.get("SignalStrengthIndicator")
            except requests.RequestException:
                pass
    return jsonify(cams)


@app.route("/api/cameras/<serial>/status")
def api_status(serial):
    r = requests.get(f"{ARLO_API}/camera/{serial}", timeout=5)
    return jsonify(r.json()), r.status_code


@app.route("/api/cameras/<serial>/arm", methods=["POST"])
def api_arm(serial):
    armed = bool(request.get_json(silent=True, force=True).get("armed", False))
    body = {
        "PIRTargetState": "Armed" if armed else "Disarmed",
        "VideoMotionEstimationEnable": armed,
        "AudioTargetState": "Disarmed",
    }
    r = requests.post(f"{ARLO_API}/camera/{serial}/arm", json=body, timeout=10)
    return jsonify(r.json()), r.status_code


@app.route("/api/cameras/<serial>/quality", methods=["POST"])
def api_quality(serial):
    quality = request.get_json(silent=True, force=True).get("quality", "medium")
    r = requests.post(f"{ARLO_API}/camera/{serial}/quality", json={"quality": quality}, timeout=10)
    return jsonify(r.json()), r.status_code


@app.route("/api/cameras/<serial>/friendlyname", methods=["POST"])
def api_friendlyname(serial):
    name = request.get_json(silent=True, force=True).get("name", "")
    r = requests.post(f"{ARLO_API}/camera/{serial}/friendlyname", json={"name": name}, timeout=10)
    return jsonify(r.json()), r.status_code


@app.route("/api/cameras/<serial>/snapshot", methods=["POST"])
def api_snapshot(serial):
    identifier = f"{serial}_{int(time.time())}"
    dest_url = f"http://{AP_GATEWAY_IP}:5000/snapshot/{identifier}/"
    r = requests.post(f"{ARLO_API}/camera/{serial}/snapshot", json={"url": dest_url}, timeout=10)
    if not r.ok or not r.json().get("result"):
        return jsonify({"error": "camera did not accept the snapshot request"}), 502

    tmp_path = f"/tmp/{identifier}.jpg"
    for _ in range(50):
        if os.path.exists(tmp_path):
            dest_path = os.path.join(SNAPSHOTS_DIR, f"{identifier}.jpg")
            os.replace(tmp_path, dest_path)
            return jsonify({"snapshot_url": f"/snapshots/{identifier}.jpg"})
        time.sleep(0.2)
    return jsonify({"error": "timed out waiting for camera to upload snapshot"}), 504


@app.route("/snapshots/<path:filename>")
def snapshots(filename):
    return send_from_directory(SNAPSHOTS_DIR, filename)


@app.route("/api/cameras/<serial>/stream/start", methods=["POST"])
def stream_start(serial):
    with streams_lock:
        proc = streams.get(serial)
        if proc is not None and proc.poll() is None:
            return jsonify({"hls_url": f"/hls/{serial}/stream.m3u8"})

    cam = get_camera(serial)
    if cam is None:
        abort(404)

    lock_path = acquire_rtsp_lock(cam['ip'])
    if lock_path is None:
        return jsonify({"error": "camera is busy (motion recording in progress) — try again shortly"}), 409

    d = _stream_dir(serial)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))

    # Arlo Pro/Pro2-generation cameras kill the RTSP session after roughly
    # 10-30s of continuous streaming regardless of client behavior - this
    # looks like a real firmware/hardware duty-cycle limit on these battery
    # cameras, not a protocol bug you can negotiate around (confirmed via
    # packet capture: RTP video and RTCP both halt at the exact same instant,
    # cleanly, with no TEARDOWN). See docs/PROTOCOL_NOTES.md for the full
    # writeup. _stream_keepalive() below and the frontend's auto-reconnect
    # are the practical answer: detect the stall, reconnect seamlessly,
    # rather than trying to prevent something that appears to be by design.
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "warning",
        "-use_wallclock_as_timestamps", "1",
        # DESCRIBE advertises 3 tracks (video + 2 audio). ffmpeg SETUPs all
        # of them by default but only ever sends RTCP receiver reports on
        # the video track's RTCP port - the two audio tracks get none. This
        # measurably shortens how long the session survives (packet capture
        # confirms it), so we skip SETUP on the audio tracks entirely.
        "-allowed_media_types", "video",
        "-rtsp_transport", "udp", "-rtbufsize", "32M",
        "-reorder_queue_size", "500", "-max_delay", "500000",
        "-i", f"rtsp://{cam['ip']}/live",
        # The camera already outputs H.264 - remux into HLS segments rather
        # than transcoding. Re-encoding is a real option if you need it
        # (e.g. lower bitrate for bandwidth reasons) but costs real CPU.
        "-c:v", "copy", "-an",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments",
        os.path.join(d, "stream.m3u8"),
    ]

    # The camera's RTSP listener only stays open briefly after being woken,
    # and the window is sometimes missed by the time ffmpeg actually opens
    # its socket. A process that's merely still running isn't proof it
    # connected (a failed RTSP connect can take >1s to surface), so treat
    # an actual manifest file as the only real success signal.
    manifest_path = os.path.join(d, "stream.m3u8")
    proc = None
    for attempt in range(4):
        requests.post(f"{ARLO_API}/camera/{serial}/userstreamactive", json={"active": 0}, timeout=10)
        requests.post(f"{ARLO_API}/camera/{serial}/statusrequest", timeout=10)

        log_file = open(os.path.join(d, "ffmpeg.log"), "wb")
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)

        connected = False
        for _ in range(30):
            time.sleep(0.2)
            if proc.poll() is not None:
                break
            if os.path.exists(manifest_path):
                connected = True
                break
        if connected:
            break
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc = None

    if proc is None:
        release_rtsp_lock(lock_path)
        return jsonify({"error": "camera refused the RTSP connection after several attempts"}), 502

    with streams_lock:
        streams[serial] = proc
        stream_lock_paths[serial] = lock_path

    threading.Thread(target=_stream_keepalive, args=(serial, proc), daemon=True).start()

    return jsonify({"hls_url": f"/hls/{serial}/stream.m3u8"})


def _stream_keepalive(serial, proc):
    # Repeats the wake sequence (userstreamactive + statusrequest)
    # periodically for as long as this stream is the active one for this
    # camera - part of the mitigation for the streaming-ceiling behavior
    # described above.
    while True:
        time.sleep(2)
        with streams_lock:
            if streams.get(serial) is not proc or proc.poll() is not None:
                return
        try:
            requests.post(f"{ARLO_API}/camera/{serial}/userstreamactive", json={"active": 0}, timeout=10)
            requests.post(f"{ARLO_API}/camera/{serial}/statusrequest", timeout=10)
        except requests.RequestException:
            pass


def _stop_stream_locked(serial):
    """Caller must already hold streams_lock."""
    proc = streams.pop(serial, None)
    lock_path = stream_lock_paths.pop(serial, None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if lock_path:
        release_rtsp_lock(lock_path)


@app.route("/api/cameras/<serial>/stream/stop", methods=["POST"])
def stream_stop(serial):
    with streams_lock:
        _stop_stream_locked(serial)
    try:
        requests.post(f"{ARLO_API}/camera/{serial}/userstreamactive", json={"active": 1}, timeout=10)
    except requests.RequestException:
        pass
    return jsonify({"result": True})


def stream_watchdog():
    """A stalled RTSP input (camera drops the stream without a proper
    TEARDOWN) can leave ffmpeg alive but stuck producing no new segments
    forever. Detect and kill any stream whose manifest hasn't updated
    recently so the lock is freed and the user can retry instead of it
    silently hanging.

    Also checks for motion-recording preempt requests: recording is the
    priority (see docs/PROTOCOL_NOTES.md), so a live view that's holding a
    camera's RTSP lock while a motion event needs it gets stopped here
    within one poll cycle rather than starving the recording indefinitely.
    """
    while True:
        time.sleep(5)
        with streams_lock:
            stale = []
            ip_by_serial = {}
            for serial, proc in list(streams.items()):
                if proc.poll() is not None:
                    stale.append(serial)
                    continue
                manifest = os.path.join(_stream_dir(serial), "stream.m3u8")
                try:
                    if time.time() - os.stat(manifest).st_mtime > 12:
                        stale.append(serial)
                        continue
                except FileNotFoundError:
                    pass
                ip_by_serial[serial] = None
            if ip_by_serial:
                try:
                    for cam in get_cameras():
                        if cam["serial_number"] in ip_by_serial:
                            ip_by_serial[cam["serial_number"]] = cam.get("ip")
                except requests.RequestException:
                    pass
                for serial, ip in ip_by_serial.items():
                    if ip and _consume_preempt_flag(ip):
                        stale.append(serial)
            for serial in stale:
                _stop_stream_locked(serial)


@app.route("/hls/<serial>/<path:filename>")
def hls_files(serial, filename):
    resp = send_from_directory(_stream_dir(serial), filename)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


RECORDING_NAME_RE = re.compile(r"^([A-Za-z0-9]+)_(\d{8}-\d{6})_motion\.")


def _parse_recording_name(fname):
    m = RECORDING_NAME_RE.match(fname)
    if not m:
        return None, None
    serial, ts_str = m.groups()
    try:
        dt = datetime.strptime(ts_str, "%Y%m%d-%H%M%S")
    except ValueError:
        return serial, None
    return serial, dt


def _camera_name_for(serial, camera_name_cache):
    if serial in camera_name_cache:
        return camera_name_cache[serial]
    try:
        for cam in get_cameras():
            camera_name_cache[cam["serial_number"]] = cam["friendly_name"]
    except requests.RequestException:
        pass
    return camera_name_cache.get(serial, serial or "unknown")


def _ffmpeg_text_escape(s):
    # Escape for both the outer drawtext filter-option syntax and the
    # text value itself - ':' and single quotes both need protecting.
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")


def _resolve_face_labels(labels):
    """Swap face:cluster_N placeholders for the assigned name, once one
    exists, so the gallery doesn't keep showing raw cluster IDs forever."""
    if not any(l.startswith("face:cluster_") for l in labels):
        return labels
    db = _load_faces_db()
    names_by_id = {c["id"]: c["name"] for c in db["clusters"] if c["name"]}
    resolved = []
    for l in labels:
        if l.startswith("face:cluster_"):
            cid = int(l.split("_", 1)[1])
            resolved.append(names_by_id.get(cid, l))
        else:
            resolved.append(l)
    return resolved


@app.route("/api/recordings")
def api_recordings():
    camera_name_cache = {}
    items = []
    for fname in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
        if not fname.endswith(".mp4"):
            continue
        path = os.path.join(RECORDINGS_DIR, fname)
        stat = os.stat(path)
        serial, dt = _parse_recording_name(fname)
        meta_path = path[:-4] + ".meta.json"
        duration = None
        labels = []
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                    duration = meta.get("duration")
                    labels = meta.get("labels", [])
            except (json.JSONDecodeError, OSError):
                pass
        thumb_path = path[:-4] + ".jpg"
        items.append({
            "filename": fname,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "url": f"/recordings/{fname}",
            "thumbnail_url": f"/recordings/{os.path.basename(thumb_path)}" if os.path.exists(thumb_path) else None,
            "camera_serial": serial,
            "camera_name": _camera_name_for(serial, camera_name_cache),
            "date": dt.strftime("%Y-%m-%d") if dt else None,
            "time": dt.strftime("%H:%M:%S") if dt else None,
            "duration": duration,
            "labels": _resolve_face_labels(labels),
        })
    return jsonify(items)


@app.route("/recordings/<path:filename>")
def recordings(filename):
    return send_from_directory(RECORDINGS_DIR, filename)


def remux_watcher():
    """Recordings are captured as MPEG-TS (.mpg) by the motion-recording
    pipeline, which browsers can't play natively. Once a file stops growing:
      1. remux to .mp4 with a burned-in camera name + running timestamp
         overlay (permanent, survives if the file is copied elsewhere)
      2. generate a thumbnail for the gallery
      3. cache duration in a sidecar .meta.json so the API doesn't need to
         re-probe every file on every request
    """
    camera_name_cache = {}
    seen_stable = {}
    failed = set()
    while True:
        try:
            for fname in os.listdir(RECORDINGS_DIR):
                if not fname.endswith(".mpg") or fname in failed:
                    continue
                path = os.path.join(RECORDINGS_DIR, fname)
                mp4_path = path[:-4] + ".mp4"
                if os.path.exists(mp4_path):
                    continue
                mtime = os.stat(path).st_mtime
                if seen_stable.get(fname) != mtime or time.time() - mtime <= 3:
                    seen_stable[fname] = mtime
                    continue
                if os.path.getsize(path) == 0:
                    # Camera never actually connected for this recording attempt.
                    failed.add(fname)
                    continue

                serial, dt = _parse_recording_name(fname)
                start_epoch = int(dt.timestamp()) if dt else int(mtime)
                cam_name = _camera_name_for(serial, camera_name_cache)

                name_text = _ffmpeg_text_escape(cam_name)
                # drawtext's pts:localtime format string is parsed by a
                # *second*, nested arg-splitter inside the expansion syntax -
                # colons in the time portion (%H:%M:%S) get miscounted as
                # extra function arguments even when backslash-escaped for
                # the outer filter-option parser. Sidestep it with periods.
                clock_text = f"%{{pts\\:localtime\\:{start_epoch}\\:%Y-%m-%d %H.%M.%S}}"
                vf = (
                    f"drawtext=fontcolor=white:fontsize=20:box=1:boxcolor=black@0.5:"
                    f"boxborderw=6:x=10:y=h-th-10:text='{name_text}',"
                    f"drawtext=fontcolor=white:fontsize=20:box=1:boxcolor=black@0.5:"
                    f"boxborderw=6:x=10:y=10:text='{clock_text}'"
                )

                result = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "warning", "-i", path,
                     "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "23", "-an", mp4_path],
                    check=False, capture_output=True,
                )
                if result.returncode != 0 or not os.path.exists(mp4_path):
                    failed.add(fname)
                    continue

                thumb_path = mp4_path[:-4] + ".jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", mp4_path,
                     "-vframes", "1", "-vf", "scale=320:-1", thumb_path],
                    check=False,
                )

                duration = None
                dur_result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", mp4_path],
                    capture_output=True, text=True, check=False,
                )
                try:
                    duration = float(dur_result.stdout.strip())
                except ValueError:
                    pass

                if AI_DETECTION_ENABLED:
                    labels = _detect_labels(mp4_path, duration)
                    face_labels = _detect_faces(mp4_path, duration, cam_name) if "person" in labels else []
                else:
                    labels, face_labels = [], []

                with open(mp4_path[:-4] + ".meta.json", "w") as f:
                    json.dump({
                        "camera_serial": serial,
                        "camera_name": cam_name,
                        "start_time": start_epoch,
                        "duration": duration,
                        "labels": labels + face_labels,
                    }, f)
        except Exception as e:
            print(f"remux_watcher error: {e}")
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=remux_watcher, daemon=True).start()
    threading.Thread(target=stream_watchdog, daemon=True).start()
    port = int(os.environ.get("REANIMARLO_VIEWER_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)

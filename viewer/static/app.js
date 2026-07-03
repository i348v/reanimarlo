const grid = document.getElementById("camera-grid");
const recordingsList = document.getElementById("recordings-list");
const modal = document.getElementById("player-modal");
const modalVideo = document.getElementById("player-video");
const modalClose = document.getElementById("player-close");

const hlsInstances = {};
let camerasCache = [];
// This camera's RTSP stream is prone to stalling after a short window
// (a real firmware characteristic, not a client bug — confirmed by the
// server-side watchdog cleanly detecting and killing stalled sessions).
// Track which cameras the user wants to keep watching so a stall can be
// silently reconnected instead of leaving a frozen frame.
const activeWatches = new Set();
const reconnecting = new Set();

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 ** 2) return (n / 1024).toFixed(0) + " KB";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(2) + " GB";
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString();
}

function fmtBattery(pct) {
  if (pct == null) return `<span class="cam-stat">🔋 —</span>`;
  const cls = pct < 20 ? "stat-low" : pct < 50 ? "stat-mid" : "stat-ok";
  return `<span class="cam-stat ${cls}">🔋 ${pct}%</span>`;
}

function fmtSignal(dbm) {
  if (dbm == null) return `<span class="cam-stat">📶 —</span>`;
  // Typical WiFi dBm ranges: -50 excellent ... -80 unusable
  let quality, cls;
  if (dbm >= -55) { quality = "Excellent"; cls = "stat-ok"; }
  else if (dbm >= -67) { quality = "Good"; cls = "stat-ok"; }
  else if (dbm >= -75) { quality = "Fair"; cls = "stat-mid"; }
  else { quality = "Weak"; cls = "stat-low"; }
  return `<span class="cam-stat ${cls}" title="${quality}">📶 ${dbm} dBm</span>`;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

function attachHls(videoEl, url, serial) {
  if (hlsInstances[serial]) {
    hlsInstances[serial].destroy();
    delete hlsInstances[serial];
  }
  // Attaching a source doesn't start playback on its own - without an
  // explicit play() (or the `autoplay` attribute) the element just sits
  // showing the first decoded frame forever, which looks identical to a
  // frozen/broken player.
  const tryPlay = () => videoEl.play().catch(() => {});
  if (videoEl.canPlayType("application/vnd.apple.mpegurl")) {
    videoEl.src = url;
    videoEl.addEventListener("loadedmetadata", tryPlay, { once: true });
    return;
  }
  if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({
      liveSyncDurationCount: 3,
      liveMaxLatencyDurationCount: 6,
      maxBufferLength: 6,
    });
    hls.loadSource(url);
    hls.attachMedia(videoEl);
    hls.on(Hls.Events.MANIFEST_PARSED, tryPlay);
    hlsInstances[serial] = hls;
  }
}

function detachHls(serial) {
  if (hlsInstances[serial]) {
    hlsInstances[serial].destroy();
    delete hlsInstances[serial];
  }
}

function setReconnectBadge(wrapEl, show) {
  let badge = wrapEl.querySelector(".reconnect-badge");
  if (show) {
    if (!badge) {
      badge = document.createElement("div");
      badge.className = "reconnect-badge";
      badge.textContent = "Reconnecting…";
      wrapEl.appendChild(badge);
    }
  } else if (badge) {
    badge.remove();
  }
}

async function startStream(cam, wrapEl, { isReconnect = false } = {}) {
  const existingVideo = wrapEl.querySelector("video");
  if (isReconnect && existingVideo) {
    // Leave the last frame on screen instead of blanking to a placeholder -
    // reads as a brief freeze rather than the player being broken.
    setReconnectBadge(wrapEl, true);
  } else {
    wrapEl.innerHTML = `<div class="cam-placeholder">Connecting… (the camera may be asleep — this can take up to ~30s)</div>`;
  }

  let data;
  try {
    data = await api(`/api/cameras/${cam.serial_number}/stream/start`, { method: "POST" });
  } catch (e) {
    if (!(isReconnect && existingVideo)) {
      wrapEl.innerHTML = `<div class="cam-placeholder">Couldn't reach the camera. It may be in deep sleep — retrying automatically…</div>`;
    }
    return false;
  }

  // Poll briefly for the manifest to actually appear before attaching player
  let ready = false;
  for (let i = 0; i < 20; i++) {
    const r = await fetch(data.hls_url, { method: "HEAD" }).catch(() => null);
    if (r && r.ok) { ready = true; break; }
    await new Promise((r) => setTimeout(r, 500));
  }
  if (!ready) {
    if (!(isReconnect && existingVideo)) {
      wrapEl.innerHTML = `<div class="cam-placeholder">Stream didn't start in time. Retrying…</div>`;
    }
    return false;
  }

  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  wrapEl.innerHTML = "";
  wrapEl.appendChild(video);
  attachHls(video, data.hls_url, cam.serial_number);
  return true;
}

async function toggleStream(cam, wrapEl) {
  if (cam.streaming || activeWatches.has(cam.serial_number)) {
    activeWatches.delete(cam.serial_number);
    detachHls(cam.serial_number);
    await api(`/api/cameras/${cam.serial_number}/stream/stop`, { method: "POST" });
    loadCameras();
    return;
  }

  activeWatches.add(cam.serial_number);
  await startStream(cam, wrapEl);
  loadCameras();
}

async function maybeReconnect(cam, wrapEl) {
  if (!activeWatches.has(cam.serial_number)) return;
  if (cam.streaming) return;
  if (reconnecting.has(cam.serial_number)) return;
  reconnecting.add(cam.serial_number);
  try {
    await startStream(cam, wrapEl, { isReconnect: true });
  } finally {
    reconnecting.delete(cam.serial_number);
    loadCameras();
  }
}

function syncWatchButton(btn, cam) {
  const watching = cam.streaming || activeWatches.has(cam.serial_number);
  btn.textContent = watching ? "Stop" : "Watch Live";
}

function renderCameraCard(cam) {
  const card = document.createElement("div");
  card.className = "cam-card";

  const wrap = document.createElement("div");
  wrap.className = "cam-video-wrap";
  wrap.innerHTML = cam.streaming
    ? `<div class="cam-placeholder">Loading stream…</div>`
    : `<div class="cam-placeholder">Not watching</div>`;

  const badge = document.createElement("div");
  badge.className = "cam-badge";
  badge.innerHTML = `<span class="dot ${cam.streaming ? "live" : ""}"></span>${cam.streaming ? "LIVE" : "IDLE"}`;
  wrap.appendChild(badge);

  const body = document.createElement("div");
  body.className = "cam-body";

  const nameRow = document.createElement("div");
  nameRow.className = "cam-name-row";
  nameRow.innerHTML = `<span class="cam-name">${cam.friendly_name}</span>`;

  const statusRow = document.createElement("div");
  statusRow.className = "cam-status-row";
  statusRow.innerHTML = `${fmtBattery(cam.battery_pct)}${fmtSignal(cam.signal_dbm)}`;

  const controls = document.createElement("div");
  controls.className = "cam-controls";

  const watchBtn = document.createElement("button");
  watchBtn.className = "action primary watch-btn";
  syncWatchButton(watchBtn, cam);
  watchBtn.onclick = async () => {
    // The button's label reflects live state via syncWatchButton(), but the
    // `cam` object closed over here is a snapshot from whenever this card
    // was first created - re-fetch the current one so a stream that was
    // stopped elsewhere (the watchdog, another tab) doesn't leave this
    // click acting on stale streaming/serial data.
    const current = camerasCache.find((c) => c.serial_number === cam.serial_number) || cam;
    watchBtn.disabled = true;
    try {
      await toggleStream(current, wrap);
    } catch (e) {
      console.error(e);
    }
    watchBtn.disabled = false;
  };

  const armBtn = document.createElement("button");
  armBtn.className = "action";
  armBtn.textContent = "Arm/Disarm";
  armBtn.onclick = async () => {
    const armed = !(cam._armed || false);
    await api(`/api/cameras/${cam.serial_number}/arm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ armed }),
    });
    cam._armed = armed;
    armBtn.textContent = armed ? "Armed (tap to disarm)" : "Arm/Disarm";
  };

  const snapBtn = document.createElement("button");
  snapBtn.className = "action";
  snapBtn.textContent = "Snapshot";
  snapBtn.onclick = async () => {
    snapBtn.disabled = true;
    snapBtn.textContent = "…";
    try {
      const data = await api(`/api/cameras/${cam.serial_number}/snapshot`, { method: "POST" });
      openSnapshot(data.snapshot_url);
    } catch (e) {
      alert("Snapshot failed: " + e.message);
    }
    snapBtn.disabled = false;
    snapBtn.textContent = "Snapshot";
  };

  const renameBtn = document.createElement("button");
  renameBtn.className = "action";
  renameBtn.textContent = "Rename";
  renameBtn.onclick = async () => {
    const name = prompt("Camera name", cam.friendly_name);
    if (!name) return;
    await api(`/api/cameras/${cam.serial_number}/friendlyname`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    loadCameras();
  };

  const qualitySel = document.createElement("select");
  qualitySel.className = "quality";
  ["low", "medium", "high"].forEach((q) => {
    const opt = document.createElement("option");
    opt.value = q;
    opt.textContent = q[0].toUpperCase() + q.slice(1);
    qualitySel.appendChild(opt);
  });
  qualitySel.value = "medium";
  qualitySel.onchange = () => {
    api(`/api/cameras/${cam.serial_number}/quality`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quality: qualitySel.value }),
    });
  };

  controls.append(watchBtn, armBtn, snapBtn, renameBtn, qualitySel);
  body.append(nameRow, statusRow, controls);
  card.append(wrap, body);
  return card;
}

function openSnapshot(url) {
  modalVideo.pause();
  modalVideo.removeAttribute("src");
  const img = document.createElement("img");
  img.src = url;
  img.style.width = "100%";
  const content = modalVideo.parentElement;
  content.querySelectorAll("img").forEach((el) => el.remove());
  modalVideo.classList.add("hidden");
  content.insertBefore(img, modalClose.nextSibling);
  modal.classList.remove("hidden");
}

function openRecording(url) {
  const content = modalVideo.parentElement;
  content.querySelectorAll("img").forEach((el) => el.remove());
  modalVideo.classList.remove("hidden");
  modalVideo.src = url;
  modal.classList.remove("hidden");
}

modalClose.onclick = () => {
  modal.classList.add("hidden");
  modalVideo.pause();
  modalVideo.removeAttribute("src");
};

async function loadCameras() {
  let cams;
  try {
    cams = await api("/api/cameras");
  } catch (e) {
    grid.innerHTML = `<div class="empty-state">Can't reach the camera server. Is arlo-cam-api running?</div>`;
    return;
  }
  camerasCache = cams;
  if (cams.length === 0) {
    grid.innerHTML = `<div class="empty-state">No cameras registered yet. Press Sync on a camera to pair it.</div>`;
    return;
  }

  // Clear any leftover empty/error-state message before adding real cards
  const stale = grid.querySelector(".empty-state");
  if (stale) grid.innerHTML = "";

  // Preserve any live video elements already playing instead of nuking them
  cams.forEach((cam) => {
    let card = grid.querySelector(`[data-serial="${cam.serial_number}"]`);
    if (!card) {
      card = renderCameraCard(cam);
      card.dataset.serial = cam.serial_number;
      grid.appendChild(card);
    } else {
      const statusRow = card.querySelector(".cam-status-row");
      if (statusRow) statusRow.innerHTML = `${fmtBattery(cam.battery_pct)}${fmtSignal(cam.signal_dbm)}`;
      const watchBtn = card.querySelector(".watch-btn");
      if (watchBtn) syncWatchButton(watchBtn, cam);
      if (!cam.streaming && activeWatches.has(cam.serial_number)) {
        const wrap = card.querySelector(".cam-video-wrap");
        if (wrap) maybeReconnect(cam, wrap);
      }
    }
  });
}

let recordingsCache = [];
let selectedCameraSerial = "all";
let selectedDate = "all";
let selectedLabel = "all";

function fmtDuration(seconds) {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function renderCameraTabs() {
  const tabsEl = document.getElementById("camera-tabs");
  // Union of currently-registered cameras and any camera seen in past
  // recordings (covers cameras that have since been removed/renamed).
  const known = new Map();
  camerasCache.forEach((c) => known.set(c.serial_number, c.friendly_name));
  recordingsCache.forEach((r) => {
    if (r.camera_serial && !known.has(r.camera_serial)) known.set(r.camera_serial, r.camera_name);
  });

  const tabs = [["all", "All Cameras"], ...known.entries()];
  tabsEl.innerHTML = "";
  tabs.forEach(([serial, name]) => {
    const btn = document.createElement("button");
    btn.className = "cam-tab-btn" + (serial === selectedCameraSerial ? " active" : "");
    btn.textContent = name;
    btn.onclick = () => {
      selectedCameraSerial = serial;
      selectedDate = "all";
      selectedLabel = "all";
      renderRecordings();
    };
    tabsEl.appendChild(btn);
  });
}

function renderDateFilter() {
  const sel = document.getElementById("date-filter");
  const inScope = recordingsCache.filter(
    (r) => selectedCameraSerial === "all" || r.camera_serial === selectedCameraSerial
  );
  const dates = [...new Set(inScope.map((r) => r.date).filter(Boolean))].sort().reverse();
  sel.innerHTML = `<option value="all">All Dates</option>` +
    dates.map((d) => `<option value="${d}">${d}</option>`).join("");
  sel.value = selectedDate;
  sel.onchange = () => {
    selectedDate = sel.value;
    renderGallery();
  };
}

function renderLabelFilter() {
  const sel = document.getElementById("label-filter");
  const inScope = recordingsCache.filter(
    (r) => selectedCameraSerial === "all" || r.camera_serial === selectedCameraSerial
  );
  const labels = [...new Set(inScope.flatMap((r) => r.labels || []))].sort();
  if (labels.length === 0) {
    sel.innerHTML = `<option value="all">All Labels</option>`;
    sel.value = "all";
    selectedLabel = "all";
    return;
  }
  sel.innerHTML = `<option value="all">All Labels</option>` +
    labels.map((l) => `<option value="${l}">${l[0].toUpperCase() + l.slice(1)}</option>`).join("");
  sel.value = selectedLabel;
  sel.onchange = () => {
    selectedLabel = sel.value;
    renderGallery();
  };
}

function renderGallery() {
  const filtered = recordingsCache.filter((r) => {
    if (selectedCameraSerial !== "all" && r.camera_serial !== selectedCameraSerial) return false;
    if (selectedDate !== "all" && r.date !== selectedDate) return false;
    if (selectedLabel !== "all" && !(r.labels || []).includes(selectedLabel)) return false;
    return true;
  });
  if (filtered.length === 0) {
    recordingsList.innerHTML = `<div class="empty-state">No recordings match this filter yet.</div>`;
    return;
  }
  recordingsList.innerHTML = "";
  filtered.forEach((rec) => {
    const card = document.createElement("div");
    card.className = "recording-card";
    const labelTags = (rec.labels || []).map((l) => `<span class="label-tag">${l}</span>`).join("");
    card.innerHTML = `
      <div class="recording-thumb">
        ${rec.thumbnail_url ? `<img src="${rec.thumbnail_url}" loading="lazy">` : `<div class="cam-placeholder">Processing…</div>`}
        ${rec.duration ? `<span class="recording-duration">${fmtDuration(rec.duration)}</span>` : ""}
      </div>
      <div class="recording-info">
        <span class="recording-camera">${rec.camera_name || "Unknown"}</span>
        <span class="recording-meta">${rec.date || ""} ${rec.time || ""} · ${fmtBytes(rec.size)}</span>
        ${labelTags ? `<div class="label-tags">${labelTags}</div>` : ""}
      </div>
    `;
    card.onclick = () => openRecording(rec.url);
    recordingsList.appendChild(card);
  });
}

function renderRecordings() {
  renderCameraTabs();
  renderDateFilter();
  renderLabelFilter();
  renderGallery();
}

async function loadRecordings() {
  try {
    recordingsCache = await api("/api/recordings");
  } catch (e) {
    recordingsList.innerHTML = `<div class="empty-state">Can't load recordings.</div>`;
    return;
  }
  if (recordingsCache.length === 0 && camerasCache.length === 0) {
    recordingsList.innerHTML = `<div class="empty-state">No recordings yet. They'll appear here after motion events.</div>`;
    document.getElementById("camera-tabs").innerHTML = "";
    document.getElementById("date-filter").innerHTML = "";
    return;
  }
  renderRecordings();
}

let activeTab = "live";
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    activeTab = btn.dataset.tab;
    if (activeTab === "recordings") loadRecordings();
  };
});

loadCameras();
setInterval(loadCameras, 8000);
setInterval(() => { if (activeTab === "recordings") loadRecordings(); }, 8000);

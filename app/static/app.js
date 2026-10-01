// Audio Recorder – plain JavaScript, no build step.
"use strict";

const $ = (id) => document.getElementById(id);

const ui = {
  disk: $("disk"), banner: $("banner"), device: $("device"), monitor: $("monitor"),
  monitorInfo: $("monitor-info"), warnings: $("warnings"), meters: $("meters"), clip: $("clip"),
  tag: $("tag"), rec: $("rec"), recLabel: $("rec-label"), timer: $("timer"), recFile: $("rec-file"),
  marker: $("marker"), markers: $("markers"), list: $("list"), empty: $("empty"), refresh: $("refresh"),
  player: $("player"), playerName: $("player-name"), playerClose: $("player-close"),
  audio: $("audio"), waveform: $("waveform"), confirm: $("confirm"), confirmText: $("confirm-text"),
};

let status = null;       // last /api/status
let busy = false;        // a start/stop request is running
let playing = null;      // file name in the player
let lastFileShown = null;
const WAVEFORM_MAX_MB = 60; // the browser decodes the whole file; keep it small on phones

// ---------- helpers ----------

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Error ${res.status}`);
  return data;
}

function showBanner(text, kind = "error") {
  if (!text) { ui.banner.classList.add("hidden"); return; }
  ui.banner.textContent = text;
  ui.banner.className = "banner" + (kind === "info" ? " info" : "");
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtSize(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) +
    " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

const enc = encodeURIComponent;

// ---------- status / recording ----------

async function loadStatus() {
  try {
    status = await api("GET", "/api/status");
  } catch (e) {
    showBanner("Server not reachable: " + e.message);
    return;
  }
  renderStatus();
}

function renderStatus() {
  const s = status;
  const rec = s.state !== "idle";
  ui.disk.textContent = `${(s.disk_free_mb / 1024).toFixed(1)} GB free`;
  ui.rec.classList.toggle("recording", rec);
  ui.timer.classList.toggle("recording", rec);
  ui.recLabel.textContent = rec ? "STOP" : "REC";
  ui.rec.setAttribute("aria-label", rec ? "Stop" : "Record");
  ui.tag.disabled = rec;
  ui.device.disabled = rec;
  ui.marker.classList.toggle("hidden", !rec);

  if (rec) {
    ui.timer.textContent = fmtTime(s.elapsed);
    ui.recFile.textContent = `${s.file} · ${fmtSize(s.size)}`;
    ui.markers.innerHTML = s.markers.map((m) => `📍 ${fmtTime(m.t)}${m.note ? " " + esc(m.note) : ""}`).join(" · ");
  } else {
    ui.timer.textContent = "00:00";
    const c = s.config;
    ui.recFile.textContent = `${c.format.toUpperCase()} · ${c.sample_rate / 1000} kHz · ${c.bit_depth}-bit · ${c.out_channels} ch · max ${c.max_duration_min} min`;
    ui.markers.textContent = "";
  }

  // A recording that stopped by itself (limit, disk full, ffmpeg error).
  if (!rec && s.last_file && s.last_file !== lastFileShown) {
    lastFileShown = s.last_file;
    loadRecordings();
  }
  if (!rec && s.last_error) showBanner("Recording stopped: " + s.last_error);

  // Monitor switch
  const m = s.monitor;
  ui.monitor.checked = m.enabled;
  ui.monitor.disabled = !m.available;
  ui.monitorInfo.textContent = !m.available ? `not available: ${m.reason}`
    : m.enabled ? (m.running ? `on · ~${m.latency_ms} ms` : `starting… ${m.last_error || ""}`) : "";

  ui.warnings.innerHTML = (s.warnings || []).map((w) => "⚠ " + esc(w)).join("<br>");
}

function esc(t) {
  return String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

ui.rec.addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  ui.rec.disabled = true;
  try {
    if (status && status.state !== "idle") {
      const r = await api("POST", "/api/record/stop");
      lastFileShown = r.last_file;
      showBanner(r.last_file ? `Saved: ${r.last_file}` : "", "info");
      ui.tag.value = "";
      loadRecordings();
    } else {
      showBanner("");
      await api("POST", "/api/record/start", { name: ui.tag.value.trim() });
    }
  } catch (e) {
    showBanner(e.message);
  } finally {
    busy = false;
    ui.rec.disabled = false;
    loadStatus();
  }
});

ui.marker.addEventListener("click", async () => {
  try {
    await api("POST", "/api/record/marker", { note: "" });
    loadStatus();
  } catch (e) { showBanner(e.message); }
});

// Poll faster while recording (timer, size), slower when idle.
async function pollLoop() {
  await loadStatus();
  const fast = status && status.state !== "idle";
  setTimeout(pollLoop, fast ? 1000 : 4000);
}

// ---------- devices / monitor ----------

async function loadDevices() {
  try {
    const d = await api("GET", "/api/devices");
    ui.device.innerHTML = d.devices.map((x) =>
      `<option value="${esc(x.id)}" ${x.id === d.selected ? "selected" : ""}>${esc(x.label)}</option>`).join("");
    if (!d.devices.some((x) => x.id === d.selected) && d.selected) {
      ui.device.insertAdjacentHTML("afterbegin", `<option value="${esc(d.selected)}" selected>${esc(d.selected)}</option>`);
    }
  } catch (e) { showBanner(e.message); }
}

ui.device.addEventListener("change", async () => {
  try {
    const r = await api("PUT", "/api/devices/selected", { device: ui.device.value });
    showBanner(r.warnings.length ? r.warnings.join(" ") : "Input changed.", r.warnings.length ? "error" : "info");
  } catch (e) {
    showBanner(e.message);
    loadDevices();
  }
  loadStatus();
});

ui.monitor.addEventListener("change", async () => {
  try {
    await api("PUT", "/api/monitor", { enabled: ui.monitor.checked });
  } catch (e) { showBanner(e.message); }
  setTimeout(loadStatus, 500);
});

// ---------- level meter (WebSocket) ----------

const meterEls = [];
let clipUntil = 0;

function dbToPct(db) {
  return Math.min(100, Math.max(0, ((db + 60) / 60) * 100)); // -60 dB .. 0 dB
}

function ensureMeters(n) {
  while (meterEls.length < n) {
    const el = document.createElement("div");
    el.className = "meter";
    el.innerHTML = '<div class="bar"></div><div class="cover"></div><div class="hold"></div><span class="db"></span>';
    ui.meters.appendChild(el);
    meterEls.push({ el, cover: el.children[1], hold: el.children[2], db: el.children[3], peak: -90, peakAt: 0 });
  }
}

function renderLevel(msg) {
  ensureMeters(msg.peak_db.length);
  const now = Date.now();
  msg.peak_db.forEach((db, i) => {
    const m = meterEls[i];
    m.cover.style.width = `${100 - dbToPct(db)}%`;
    // Peak hold: keep the highest value for 1.5 s.
    if (db >= m.peak || now - m.peakAt > 1500) { m.peak = db; m.peakAt = now; }
    m.hold.style.left = `calc(${dbToPct(m.peak)}% - 2px)`;
    m.db.textContent = msg.active ? `${db.toFixed(1)} dB` : "no signal";
  });
  if (msg.clip) clipUntil = now + 2000; // keep "CLIP" on for 2 s
  ui.clip.classList.toggle("on", now < clipUntil);
}

let levelWs = null;
function connectLevel() {
  if (levelWs || document.visibilityState !== "visible") return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/level`);
  levelWs = ws;
  ws.onmessage = (ev) => renderLevel(JSON.parse(ev.data));
  ws.onclose = () => {
    if (levelWs === ws) levelWs = null;
    setTimeout(connectLevel, 2000); // reconnect (only if the page is visible)
  };
}

// Close the meter when the page is hidden: the server then stops reading
// the input for the meter, and the phone saves battery.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    connectLevel(); loadStatus(); loadRecordings();
  } else if (levelWs) {
    const ws = levelWs;
    levelWs = null;
    ws.close();
  }
});

// ---------- recordings ----------

async function loadRecordings() {
  let items;
  try { items = await api("GET", "/api/recordings"); } catch (e) { showBanner(e.message); return; }
  ui.empty.classList.toggle("hidden", items.length > 0);
  ui.list.innerHTML = items.map((r) => `
    <div class="card item ${r.name === playing ? "playing" : ""}" data-name="${esc(r.name)}">
      <div class="item-name">${esc(r.name)}</div>
      <div class="item-meta">${fmtDate(r.date)} · ${r.duration != null ? fmtTime(r.duration) : "?"} · ${fmtSize(r.size)}</div>
      <div class="item-actions">
        <button class="btn" data-act="play" aria-label="Play">▶</button>
        <a class="btn" href="/api/recordings/${enc(r.name)}/download" aria-label="Download" style="text-align:center;text-decoration:none;line-height:28px">⬇</a>
        <button class="btn" data-act="rename" aria-label="Rename">✎</button>
        <button class="btn" data-act="delete" aria-label="Delete">🗑</button>
      </div>
      <div class="item-extra">
        <button class="btn" data-act="share">📤 Share</button>
        ${r.name.endsWith(".mp3") ? "" : '<button class="btn" data-act="mp3">Make MP3</button>'}
        ${r.size <= WAVEFORM_MAX_MB * 1024 * 1024 ? '<button class="btn" data-act="wave">Waveform</button>' : ""}
        <span class="markers-slot"></span>
      </div>
    </div>`).join("");
}

ui.list.addEventListener("click", async (ev) => {
  const chip = ev.target.closest(".marker-chip");
  if (chip) { ui.audio.currentTime = Number(chip.dataset.t); ui.audio.play(); return; }
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const item = btn.closest(".item");
  const name = item.dataset.name;
  const act = btn.dataset.act;
  try {
    if (act === "play") {
      play(name, item);
    } else if (act === "wave") {
      play(name, item);
      await showWaveform(name);
    } else if (act === "rename") {
      const current = name.replace(/^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_?/, "").replace(/\.[^.]+$/, "");
      const tag = prompt("New name (the date stays):", current);
      if (tag === null) return;
      const r = await api("PATCH", `/api/recordings/${enc(name)}`, { name: tag });
      if (playing === name) { playing = r.name; ui.playerName.textContent = r.name; }
      loadRecordings();
    } else if (act === "delete") {
      if (!(await confirmDialog(`Delete "${name}"? This cannot be undone.`))) return;
      if (playing === name) closePlayer();
      await api("DELETE", `/api/recordings/${enc(name)}`);
      loadRecordings();
    } else if (act === "share") {
      openShare(name);
    } else if (act === "mp3") {
      btn.disabled = true;
      btn.textContent = "Converting…";
      const r = await api("POST", `/api/recordings/${enc(name)}/mp3`);
      showBanner(`MP3 ready: ${r.name}`, "info");
      loadRecordings();
    }
  } catch (e) {
    showBanner(e.message);
    if (act === "mp3") loadRecordings();
  }
});

async function play(name, item) {
  playing = name;
  ui.player.classList.remove("hidden");
  ui.playerName.textContent = name;
  ui.waveform.innerHTML = "";
  ui.audio.src = `/api/recordings/${enc(name)}`;
  ui.audio.play().catch(() => {});
  document.querySelectorAll(".item.playing").forEach((x) => x.classList.remove("playing"));
  item.classList.add("playing");
  // Markers saved during recording: tap to jump.
  try {
    const marks = await api("GET", `/api/recordings/${enc(name)}/markers`);
    item.querySelector(".markers-slot").innerHTML = marks.map((m) =>
      `<span class="marker-chip" data-t="${m.t}">📍 ${fmtTime(m.t)}${m.note ? " " + esc(m.note) : ""}</span>`).join("");
  } catch { /* no markers */ }
}

function closePlayer() {
  ui.audio.pause();
  ui.audio.removeAttribute("src");
  ui.audio.load();
  ui.waveform.innerHTML = "";
  ui.player.classList.add("hidden");
  playing = null;
  document.querySelectorAll(".item.playing").forEach((x) => x.classList.remove("playing"));
}
ui.playerClose.addEventListener("click", closePlayer);

// Waveform (wavesurfer.js, loaded only when needed).
let wavesurferLoaded = null;
function loadWavesurfer() {
  wavesurferLoaded ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdnjs.cloudflare.com/ajax/libs/wavesurfer.js/7.8.6/wavesurfer.min.js";
    s.onload = resolve;
    s.onerror = () => { wavesurferLoaded = null; reject(new Error("Could not load wavesurfer.js")); };
    document.head.appendChild(s);
  });
  return wavesurferLoaded;
}

let wave = null;
async function showWaveform(name) {
  await loadWavesurfer();
  if (wave) { wave.destroy(); wave = null; }
  ui.waveform.innerHTML = "";
  wave = WaveSurfer.create({
    container: ui.waveform, media: ui.audio, height: 64,
    waveColor: "#4a4f59", progressColor: "#5b9cff", cursorColor: "#e8eaed",
    url: `/api/recordings/${enc(name)}`,
  });
}

function confirmDialog(text) {
  ui.confirmText.textContent = text;
  return new Promise((resolve) => {
    ui.confirm.onclose = () => resolve(ui.confirm.returnValue === "ok");
    ui.confirm.returnValue = "";
    ui.confirm.showModal();
  });
}

// ---------- share (Messenger, Instagram, WhatsApp, ...) ----------
//
// Browsers open the share menu only right after a tap. Making the file can
// take a while, so it is two taps: 1) choose MP3 / MP4 (the file is made and
// downloaded), 2) "Share" opens the phone's share menu with the ready file.

const shareUi = {
  dlg: $("share"), name: $("share-name"), choose: $("share-choose"),
  status: $("share-status"), go: $("share-go"),
};
let shareFile = null;
let shareFor = null;

function canShareFiles() {
  try {
    return !!navigator.canShare && navigator.canShare({ files: [new File(["x"], "x.mp3", { type: "audio/mpeg" })] });
  } catch { return false; }
}

function openShare(name) {
  shareFor = name;
  shareFile = null;
  shareUi.name.textContent = name;
  shareUi.status.textContent = canShareFiles() ? "" :
    "This browser cannot open the share menu here (it works only on HTTPS pages). The file will be downloaded; share it from your Files app.";
  shareUi.go.classList.add("hidden");
  shareUi.choose.querySelectorAll("button").forEach((b) => (b.disabled = false));
  shareUi.dlg.showModal();
}

shareUi.choose.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-fmt]");
  if (!btn) return;
  const fmt = btn.dataset.fmt;
  const name = shareFor;
  shareUi.choose.querySelectorAll("button").forEach((b) => (b.disabled = true));
  shareUi.go.classList.add("hidden");
  shareUi.status.textContent = fmt === "mp4" ? "Making the video… (long recordings take a while)" : "Making the MP3…";
  try {
    const res = await fetch(`/api/recordings/${enc(name)}/share/${fmt}`);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `Error ${res.status}`);
    }
    const blob = await res.blob();
    if (shareFor !== name) return; // dialog was closed / reopened meanwhile
    const fileName = name.replace(/\.[^.]+$/, "") + "." + fmt;
    const file = new File([blob], fileName, { type: fmt === "mp4" ? "video/mp4" : "audio/mpeg" });
    const mb = (blob.size / 1024 / 1024).toFixed(1);
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      shareFile = file;
      shareUi.status.textContent = `Ready: ${fileName} (${mb} MB). Tap "Share" and choose the app.`;
      shareUi.go.classList.remove("hidden");
    } else {
      downloadBlob(blob, fileName);
      shareUi.status.textContent = `Downloaded: ${fileName} (${mb} MB). Share it from your Files / Downloads app.`;
    }
  } catch (e) {
    shareUi.status.textContent = "Error: " + e.message;
  } finally {
    shareUi.choose.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
});

shareUi.go.addEventListener("click", async () => {
  if (!shareFile) return;
  try {
    await navigator.share({ files: [shareFile], title: shareFile.name });
    shareUi.dlg.close();
  } catch (e) {
    if (e.name !== "AbortError") shareUi.status.textContent = "Share failed: " + e.message;
  }
});

shareUi.dlg.addEventListener("close", () => { shareFile = null; shareFor = null; });

function downloadBlob(blob, fileName) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

ui.refresh.addEventListener("click", () => { loadRecordings(); loadStatus(); });
ui.tag.addEventListener("keydown", (e) => { if (e.key === "Enter") ui.tag.blur(); });

// ---------- start ----------
loadDevices();
loadRecordings();
pollLoop();
connectLevel();

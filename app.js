let appConfig = { local: false, local_default_host: "127.0.0.1:5000" };

async function loadConfig() {
  try {
    const res = await fetch("config.json", { cache: "no-store" });
    if (res.ok) appConfig = { ...appConfig, ...(await res.json()) };
  } catch (e) {

  }
  applyConnectMode();
}

function applyConnectMode() {
  const input = document.getElementById("connectInput");
  const title = document.getElementById("gateTitle");
  const sub = document.getElementById("gateSub");
  const hint = document.getElementById("gateHint");

  if (appConfig.local) {
    title.textContent = "Connect to your local sandbox";
    sub.textContent = "Detection happens on this machine. Start the app, then enter the address it's running on below — usually the default already works.";
    input.placeholder = appConfig.local_default_host || "127.0.0.1:5000";
    if (!input.value) input.value = appConfig.local_default_host || "127.0.0.1:5000";
    hint.innerHTML = 'App not running yet? <a href="#/integration">See setup →</a>';
  } else {
    title.textContent = "Pair this tab";
    sub.textContent = "Nothing gets analyzed on a server we run. Detection happens on your own machine — download the app, click Start, and paste the connection code it shows below.";
    input.placeholder = "paste connection code or full URL — e.g. 8f2a-91cd or https://…";
    hint.textContent = "Don't have the app yet? Grab it in step 1.";
  }
}

const ROUTES = ["/", "/docs", "/integration"];

function restorePath() {
  const params = new URLSearchParams(location.search);
  const redirect = params.get("redirect");
  if (redirect) {
    history.replaceState(null, "", redirect);
  }
}
restorePath();

function currentPath() {
  if (location.hash.startsWith("#/")) return location.hash.slice(1);
  const p = location.pathname.replace(/^\/censor/, "");
  if (p.startsWith("/u/")) return p;
  return "/";
}

function pairCodeFromPath(path) {
  const m = path.match(/^\/u\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function render() {
  const path = currentPath();
  const base = path.startsWith("/u/") ? "/" : path;

  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.dataset.view === base));
  document.querySelectorAll("#navLinks a").forEach(a => a.classList.toggle("active", a.dataset.route === base));

  const code = pairCodeFromPath(path);
  if (code && !connection.base) {
    document.getElementById("connectInput").value = code;
    attemptConnect(code, { silent: false });
  }
}

window.addEventListener("hashchange", render);
window.addEventListener("popstate", render);

function setupDocsNav() {
  const links = document.querySelectorAll("#docsNav a[data-target]");
  links.forEach(a => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const target = document.getElementById(a.dataset.target);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      links.forEach(x => x.classList.remove("active"));
      a.classList.add("active");
    });
  });
}
setupDocsNav();

const connection = { base: null };
const NGROK_HEADER = { "ngrok-skip-browser-warning": "true" };

function decodeConnectionInput(raw) {
  raw = raw.trim();
  if (!raw) return null;

  if (appConfig.local) {
    if (/^https?:\/\//i.test(raw)) return raw.replace(/\/+$/, "");
    if (/^[\w.-]+(:\d+)?$/.test(raw)) return "http://" + raw.replace(/\/+$/, "");
    return null;
  }

  if (/^https?:\/\//i.test(raw)) return raw.replace(/\/+$/, "");
  try {
    const padded = raw.replace(/-/g, "+").replace(/_/g, "/");
    const decoded = atob(padded + "===".slice((padded.length + 3) % 4));
    if (/^https?:\/\//i.test(decoded)) return decoded.replace(/\/+$/, "");
  } catch (e) { /* not base64 */ }
  return null;
}

const gateEl = document.getElementById("gate");
const appEl = document.getElementById("app");
const nodeSandbox = document.getElementById("nodeSandbox");
const pairLink = document.getElementById("pairLink");
const gateError = document.getElementById("gateError");
const connectBtn = document.getElementById("connectBtn");
const navStatus = document.getElementById("navStatus");
const navStatusText = document.getElementById("navStatusText");

document.getElementById("connectForm").addEventListener("submit", (e) => {
  e.preventDefault();
  attemptConnect(document.getElementById("connectInput").value);
});

async function attemptConnect(raw, opts = {}) {
  const target = decodeConnectionInput(raw);
  gateError.textContent = "";
  if (!target) {
    gateError.textContent = "That doesn't look like a valid code or URL — copy it fresh from the app.";
    return;
  }

  connectBtn.disabled = true;
  connectBtn.textContent = "Pairing…";
  pairLink.classList.add("connecting");
  nodeSandbox.classList.add("pulsing");

  try {
    const res = await fetch(target + "/health", { headers: NGROK_HEADER });
    if (!res.ok) throw new Error("sandbox responded with " + res.status);
    const data = await res.json();
    if (data.status !== "ok") throw new Error("unexpected response from sandbox");

    connection.base = target;
    localStorage.setItem("censor_last_connection", raw.trim());
    onConnected(target);
  } catch (err) {
    pairLink.classList.remove("connecting");
    nodeSandbox.classList.remove("pulsing");
    gateError.textContent = "Couldn't reach that sandbox (" + err.message + "). Is the app still running?";
  } finally {
    connectBtn.disabled = false;
    connectBtn.textContent = "Pair";
  }
}

function onConnected(target) {
  pairLink.classList.remove("connecting");
  pairLink.classList.add("linked");
  nodeSandbox.classList.remove("pulsing");
  nodeSandbox.classList.add("linked");

  navStatus.classList.add("live");
  navStatusText.textContent = "sandbox connected";

  document.getElementById("connectedTo").textContent = shortenUrl(target);

  setTimeout(() => {
    gateEl.style.display = "none";
    appEl.style.display = "block";
  }, 350);
}

function shortenUrl(url) {
  try {
    const u = new URL(url);
    return u.hostname;
  } catch (e) { return url; }
}

(function offerLastConnection() {
  const last = localStorage.getItem("censor_last_connection");
  if (last) document.getElementById("connectInput").value = last;
})();


const DOWNLOAD_URL = "downloads/CensorSandbox-Setup.exe";
const downloadAppBtn = document.getElementById("downloadAppBtn");
const downloadIdle = document.getElementById("downloadIdle");
const downloadPreparing = document.getElementById("downloadPreparing");
const downloadDone = document.getElementById("downloadDone");
const walkthroughPanel = document.getElementById("walkthroughPanel");

function trackEvent(name, data) {
  try { console.debug("[censor:track]", name, data || {}); } catch (e) { /* noop */ }
}

if (downloadAppBtn) {
  downloadAppBtn.addEventListener("click", () => {
    trackEvent("download_click", { url: downloadAppBtn.getAttribute("href") });
    downloadIdle.style.display = "none";
    downloadPreparing.style.display = "block";
    setTimeout(() => {
      downloadPreparing.style.display = "none";
      downloadDone.style.display = "block";
      walkthroughPanel.style.display = "block";
      walkthroughPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 900);
  });
}

const GENITAL_ANUS_CLASSES = ["FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"];
const BREAST_CLASSES = ["FEMALE_BREAST_EXPOSED", "MALE_BREAST_EXPOSED"];
const BUTTOCKS_CLASSES = ["BUTTOCKS_EXPOSED"];
const FEET_CLASSES = ["FEET_EXPOSED"];
const FACE_CLASSES = ["FACE_FEMALE", "FACE_MALE"];
const BELLY_CLASSES = ["BELLY_EXPOSED"];
const ARMPITS_CLASSES = ["ARMPITS_EXPOSED"];

function apiUrl(path) { return connection.base + path; }

const fileInput   = document.getElementById("fileInput");
const dropZone    = document.getElementById("dropZone");
const dropLabel   = document.getElementById("dropLabel");
const analyzeBtn  = document.getElementById("analyzeBtn");
const analyzeLabel = document.getElementById("analyzeLabel");
const resetBtn    = document.getElementById("resetBtn");
const statusMsg   = document.getElementById("statusMsg");
const canvasWrap  = document.getElementById("canvasWrap");
const canvasPlaceholder = document.getElementById("canvasPlaceholder");
const canvas      = document.getElementById("canvas");
const ctx         = canvas.getContext("2d");
const videoQueuePanel = document.getElementById("videoQueuePanel");
const videoQueueList = document.getElementById("videoQueueList");
const logEmpty    = document.getElementById("logEmpty");
const logList     = document.getElementById("logList");
const logCount    = document.getElementById("logCount");
const showAllToggle  = document.getElementById("showAllToggle");
const showAllWrap = document.getElementById("showAllWrap");
const preBlurToggle = document.getElementById("preBlurToggle");
const censorGenitals = document.getElementById("censorGenitals");
const censorBreasts  = document.getElementById("censorBreasts");
const censorButtocks = document.getElementById("censorButtocks");
const censorFeet = document.getElementById("censorFeet");
const censorFace = document.getElementById("censorFace");
const censorBelly = document.getElementById("censorBelly");
const censorArmpits = document.getElementById("censorArmpits");
const styleRadios = document.querySelectorAll('input[name="style"]');
const solidColorWrap = document.getElementById("solidColorWrap");
const solidColorInput = document.getElementById("solidColorInput");
const solidSwatchPreview = document.getElementById("solidSwatchPreview");
const samplingWrap = document.getElementById("samplingWrap");
const sampleEvery  = document.getElementById("sampleEvery");
const sampleValueLabel = document.getElementById("sampleValueLabel");
const videoWrap = document.getElementById("videoWrap");
const previewVideo = document.getElementById("previewVideo");
const previewVideoLabel = document.getElementById("previewVideoLabel");
const downloadResultBtn = document.getElementById("downloadResultBtn");
const copyResultBtn = document.getElementById("copyResultBtn");

let currentImage = null;
let currentFile = null;
let lastResult = null;
let mode = null;

sampleEvery.addEventListener("input", () => {
  sampleValueLabel.textContent = sampleEvery.value === "1" ? "every frame" : `every ${sampleEvery.value} frames`;
});

["dragover", "drop"].forEach(evt => window.addEventListener(evt, (e) => e.preventDefault()));
["dragenter", "dragover"].forEach(evt => dropZone.addEventListener(evt, (e) => { e.preventDefault(); e.stopPropagation(); dropZone.classList.add("drag"); }));
["dragleave", "dragend"].forEach(evt => dropZone.addEventListener(evt, (e) => { e.preventDefault(); dropZone.classList.remove("drag"); }));

dropZone.addEventListener("drop", (e) => {
  e.preventDefault(); e.stopPropagation();
  dropZone.classList.remove("drag");
  const dt = e.dataTransfer;
  let files = [];
  if (dt.files && dt.files.length) files = Array.from(dt.files);
  else if (dt.items && dt.items.length) for (const item of dt.items) { if (item.kind === "file") { const f = item.getAsFile(); if (f) files.push(f); } }
  if (!files.length) { setStatus("That drop didn't contain an actual file. Try dragging from your file explorer, or click to choose instead.", "error"); return; }
  loadFiles(files);
});

fileInput.addEventListener("change", (e) => { if (e.target.files.length) loadFiles(Array.from(e.target.files)); fileInput.value = ""; });

function showImageUI() {
  mode = "image";
  hideVideoPreview();
  canvasWrap.style.display = "block";
  canvasPlaceholder.style.display = "none";
  showAllWrap.style.display = "flex";
  logCard.style.display = "block";
}
const logCard = document.getElementById("logCard");

const frameModalOverlay = document.getElementById("frameModalOverlay");
const frameModalFileName = document.getElementById("frameModalFileName");
const frameModalInput = document.getElementById("frameModalInput");
const frameStepDown = document.getElementById("frameStepDown");
const frameStepUp = document.getElementById("frameStepUp");
const frameModalPresets = document.getElementById("frameModalPresets");
const frameModalCancel = document.getElementById("frameModalCancel");
const frameModalConfirm = document.getElementById("frameModalConfirm");

function markActivePreset(val) {
  frameModalPresets.querySelectorAll(".preset-chip").forEach(chip => {
    chip.classList.toggle("active", Number(chip.dataset.val) === Number(val));
  });
}

function askFrameRate(fileName, defaultVal) {
  return new Promise((resolve) => {
    frameModalFileName.textContent = `Run detection every how many frames for "${fileName}"?`;
    frameModalInput.value = defaultVal;
    markActivePreset(defaultVal);
    frameModalOverlay.style.display = "flex";
    frameModalInput.focus();
    frameModalInput.select();

    function cleanup() {
      frameModalOverlay.style.display = "none";
      frameModalOverlay.removeEventListener("click", onOverlayClick);
      frameModalConfirm.removeEventListener("click", onConfirm);
      frameModalCancel.removeEventListener("click", onCancel);
      frameStepUp.removeEventListener("click", onStepUp);
      frameStepDown.removeEventListener("click", onStepDown);
      frameModalPresets.removeEventListener("click", onPresetClick);
      frameModalInput.removeEventListener("keydown", onKeydown);
      document.removeEventListener("keydown", onEscape);
    }
    function currentValue() {
      const parsed = parseInt(frameModalInput.value, 10);
      return Number.isFinite(parsed) && parsed > 0 ? parsed : defaultVal;
    }
    function onConfirm() { const v = currentValue(); cleanup(); resolve(v); }
    function onCancel() { cleanup(); resolve(null); }
    function onOverlayClick(e) { if (e.target === frameModalOverlay) onCancel(); }
    function onStepUp() { frameModalInput.value = currentValue() + 1; markActivePreset(-1); }
    function onStepDown() { frameModalInput.value = Math.max(1, currentValue() - 1); markActivePreset(-1); }
    function onPresetClick(e) {
      const chip = e.target.closest(".preset-chip");
      if (!chip) return;
      frameModalInput.value = chip.dataset.val;
      markActivePreset(chip.dataset.val);
    }
    function onKeydown(e) { if (e.key === "Enter") onConfirm(); }
    function onEscape(e) { if (e.key === "Escape") onCancel(); }

    frameModalOverlay.addEventListener("click", onOverlayClick);
    frameModalConfirm.addEventListener("click", onConfirm);
    frameModalCancel.addEventListener("click", onCancel);
    frameStepUp.addEventListener("click", onStepUp);
    frameStepDown.addEventListener("click", onStepDown);
    frameModalPresets.addEventListener("click", onPresetClick);
    frameModalInput.addEventListener("keydown", onKeydown);
    document.addEventListener("keydown", onEscape);
    frameModalInput.addEventListener("input", () => markActivePreset(-1));
  });
}

function loadFiles(files) {
  const videoFiles = files.filter(f => f.type && f.type.startsWith("video/"));
  const imageFiles = files.filter(f => !f.type || f.type.startsWith("image/"));
  const otherFiles = files.filter(f => f.type && !f.type.startsWith("video/") && !f.type.startsWith("image/"));

  if (otherFiles.length) setStatus(`Skipped ${otherFiles.length} file(s) — only images and mp4 video are supported.`, "error");

  const badVideos = videoFiles.filter(f => f.type !== "video/mp4");
  const goodVideos = videoFiles.filter(f => f.type === "video/mp4");
  if (badVideos.length) setStatus("Only mp4 video is supported for now (skipped non-mp4 video file(s)).", "error");

  goodVideos.forEach(f => addVideoJob(f));

  if (imageFiles.length) {
    if (imageFiles.length > 1) setStatus(`Loaded the first of ${imageFiles.length} images — images are handled one at a time; drop videos to batch-process multiple at once.`, "info");
    loadImageFile(imageFiles[0]);
  }
}

function loadImageFile(file) {
  currentFile = file;
  const img = new Image();
  img.onload = () => {
    currentImage = img;
    lastResult = null;
    showImageUI();
    applyPreBlurState();
    setAnalyzeEnabled(true);
    setResultActionsEnabled(false);
    setStatus(
      preBlurToggle.checked
        ? `Loaded ${file.name} (${img.width}×${img.height}) — blurred until scanned. Click "Analyze & censor" when ready.`
        : `Loaded ${file.name} (${img.width}×${img.height}).`,
      "info"
    );
    clearLog();
  };
  img.onerror = () => setStatus("Could not read that file as an image.", "error");
  img.src = URL.createObjectURL(file);
}

function drawImageOnly(img) {
  canvas.width = img.width;
  canvas.height = img.height;
  ctx.drawImage(img, 0, 0);
  canvasPlaceholder.style.display = "none";
  canvasWrap.style.display = "block";
}

function applyPreBlurState() {
  drawImageOnly(currentImage);
  if (preBlurToggle.checked) {
    ctx.save();
    ctx.filter = "blur(26px)";
    ctx.drawImage(currentImage, 0, 0, canvas.width, canvas.height);
    ctx.restore();
  }
}

preBlurToggle.addEventListener("change", () => {
  if (currentImage && mode === "image" && !lastResult) applyPreBlurState();
});

function setStatus(msg, cls) {
  statusMsg.textContent = msg;
  statusMsg.className = "status-line " + (cls || "");
}

function setAnalyzeEnabled(enabled) {
  analyzeBtn.disabled = !enabled;
}

function setResultActionsEnabled(enabled) {
  downloadResultBtn.disabled = !enabled;
  copyResultBtn.disabled = !enabled;
}

function setAnalyzing(isAnalyzing, label) {
  if (isAnalyzing) {
    analyzeBtn.disabled = true;
    analyzeLabel.textContent = label || "Scanning…";
  } else {
    analyzeLabel.textContent = "Analyze & censor";
    setAnalyzeEnabled(!!currentFile);
  }
}

analyzeBtn.addEventListener("click", async () => { if (currentFile) await analyzeImage(); });

const MAX_DETECT_DIM = 1600;

function prepareDetectUpload(img, file) {
  return new Promise((resolve) => {
    const maxSide = Math.max(img.width, img.height);
    if (maxSide <= MAX_DETECT_DIM) {
      resolve({ blob: file, scale: 1 });
      return;
    }
    const factor = MAX_DETECT_DIM / maxSide;
    const w = Math.max(1, Math.round(img.width * factor));
    const h = Math.max(1, Math.round(img.height * factor));
    const off = document.createElement("canvas");
    off.width = w; off.height = h;
    off.getContext("2d").drawImage(img, 0, 0, w, h);
    off.toBlob((blob) => {
      if (!blob) { resolve({ blob: file, scale: 1 }); return; }
      resolve({ blob, scale: maxSide / Math.max(w, h) });
    }, "image/jpeg", 0.87);
  });
}

async function analyzeImage() {
  setStatus("Analyzing…", "info");
  setAnalyzing(true, "Scanning image…");
  try {
    const { blob, scale } = await prepareDetectUpload(currentImage, currentFile);
    const formData = new FormData();
    formData.append("image", blob, currentFile.name || "photo.jpg");
    const res = await fetch(apiUrl("/detect"), { method: "POST", body: formData, headers: NGROK_HEADER });
    if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.error || `Server returned ${res.status}`); }
    const data = await res.json();
    if (scale !== 1) data.all_detections.forEach(d => { d.box = d.box.map(v => v * scale); });
    lastResult = data;
    renderResult(data);
    setResultActionsEnabled(true);
  } catch (err) {
    setStatus("Error: " + err.message + " — is your sandbox still paired?", "error");
  } finally {
    setAnalyzing(false);
  }
}

const STATUS_LABELS = {
  queued: "Queued…",
  decoding: "Decoding video frames",
  queued_for_gpu: "Waiting for a GPU slot",
  detecting: "Running detection",
  censoring: "Applying censor to frames",
  writing: "Writing frames",
  encoding: "Encoding final mp4",
};

let jobCounter = 0;

async function addVideoJob(file) {
  const localId = "job" + (++jobCounter);
  const style = document.querySelector('input[name="style"]:checked').value;
  const solidColor = solidColorInput.value;
  const defaultSample = sampleEvery.value;

  const answer = await askFrameRate(file.name, defaultSample);
  if (answer === null) { setStatus(`Skipped "${file.name}" — cancelled.`, "info"); return; }
  const sampleEveryVal = answer;
  const categories = collectCategories().join(",");

  videoQueuePanel.style.display = "block";

  const card = document.createElement("div");
  card.className = "queue-card";
  card.dataset.localId = localId;
  card.innerHTML = `
    <div style="display:flex; justify-content:space-between; gap:8px;">
      <span class="queue-name" title="${file.name}">${file.name}</span>
      <span class="muted-tag" data-role="count"></span>
    </div>
    <div data-role="progressWrap">
      <div class="status-line info" data-role="label" style="margin-top:8px">Queued… (every ${sampleEveryVal} frames)</div>
      <div class="progress-bar"><div class="progress-fill" style="width:0%" data-role="bar"></div></div>
    </div>
    <button data-role="changeBtn" class="btn btn-accent btn-block" style="display:none; margin-top:10px;">Change</button>
    <div class="status-line error" data-role="errorMsg" style="display:none"></div>
  `;
  videoQueueList.prepend(card);

  const els = {
    label: card.querySelector('[data-role="label"]'),
    count: card.querySelector('[data-role="count"]'),
    bar: card.querySelector('[data-role="bar"]'),
    progressWrap: card.querySelector('[data-role="progressWrap"]'),
    changeBtn: card.querySelector('[data-role="changeBtn"]'),
    errorMsg: card.querySelector('[data-role="errorMsg"]'),
  };

  runVideoJob(file, style, sampleEveryVal, categories, els, localId, card, solidColor);
}

function collectCategories() {
  const cats = [];
  if (censorGenitals.checked) cats.push("genitals");
  if (censorBreasts.checked) cats.push("breasts");
  if (censorButtocks.checked) cats.push("buttocks");
  if (censorFeet.checked) cats.push("feet");
  if (censorFace.checked) cats.push("face");
  if (censorBelly.checked) cats.push("belly");
  if (censorArmpits.checked) cats.push("armpits");
  return cats;
}

function updateJobProgressUI(els, status, current, total) {
  els.label.textContent = STATUS_LABELS[status] || status;
  if (total > 1) { els.count.textContent = `${current} / ${total} frames`; els.bar.style.width = Math.min(100, Math.round((current / total) * 100)) + "%"; }
  else if (current > 0) { els.count.textContent = `${current} frames so far`; els.bar.style.width = "100%"; }
  else { els.count.textContent = ""; els.bar.style.width = "0%"; }
}

async function runVideoJob(file, style, sampleEveryVal, categories, els, localId, cardEl, solidColor) {
  const formData = new FormData();
  formData.append("video", file);
  formData.append("sample_every", sampleEveryVal);
  formData.append("style", style);
  formData.append("categories", categories);
  formData.append("solid_color", solidColor || "#000000");

  try {
    const startRes = await fetch(apiUrl("/start_video_job"), { method: "POST", body: formData, headers: NGROK_HEADER });
    if (!startRes.ok) { const err = await startRes.json().catch(() => ({})); throw new Error(err.error || `Server returned ${startRes.status}`); }
    const { job_id } = await startRes.json();

    let done = false;
    while (!done) {
      await new Promise(r => setTimeout(r, 500));
      const progRes = await fetch(apiUrl(`/video_progress/${job_id}`), { headers: NGROK_HEADER });
      if (!progRes.ok) throw new Error("lost track of the processing job");
      const prog = await progRes.json();
      if (prog.status === "error") throw new Error(prog.error || "processing failed");
      updateJobProgressUI(els, prog.status, prog.current, prog.total);
      if (prog.status === "done") done = true;
    }

    const resultRes = await fetch(apiUrl(`/video_result/${job_id}`), { headers: NGROK_HEADER });
    if (!resultRes.ok) { const err = await resultRes.json().catch(() => ({})); throw new Error(err.error || `Server returned ${resultRes.status}`); }
    const blob = await resultRes.blob();
    const url = URL.createObjectURL(blob);

    els.progressWrap.style.display = "none";
    els.changeBtn.style.display = "block";
    cardEl.classList.add("done");

    const job = { localId, name: file.name, url, cardEl };
    els.changeBtn.addEventListener("click", () => selectVideoJob(job));
  } catch (err) {
    els.label.textContent = "Failed";
    els.errorMsg.textContent = err.message + " — is your sandbox still paired?";
    els.errorMsg.style.display = "block";
  }
}

function hideVideoPreview() { videoWrap.style.display = "none"; previewVideo.pause(); }

function selectVideoJob(job) {
  mode = "video";
  canvasWrap.style.display = "none";
  canvasPlaceholder.style.display = "none";
  showAllWrap.style.display = "none";
  logCard.style.display = "none";
  setResultActionsEnabled(false);
  videoWrap.style.display = "flex";
  previewVideoLabel.textContent = job.name;
  if (previewVideo.dataset.currentUrl !== job.url) {
    previewVideo.pause(); previewVideo.autoplay = false;
    previewVideo.src = job.url; previewVideo.dataset.currentUrl = job.url; previewVideo.load();
  }
  document.querySelectorAll('#videoQueueList [data-local-id]').forEach(el => el.style.outline = el.dataset.localId === job.localId ? `2px solid var(--accent)` : "none");
  setStatus(`Showing "${job.name}" in the preview.`, "ok");
}

function activeClasses() {
  const classes = [];
  if (censorGenitals.checked) classes.push(...GENITAL_ANUS_CLASSES);
  if (censorBreasts.checked) classes.push(...BREAST_CLASSES);
  if (censorButtocks.checked) classes.push(...BUTTOCKS_CLASSES);
  if (censorFeet.checked) classes.push(...FEET_CLASSES);
  if (censorFace.checked) classes.push(...FACE_CLASSES);
  if (censorBelly.checked) classes.push(...BELLY_CLASSES);
  if (censorArmpits.checked) classes.push(...ARMPITS_CLASSES);
  return classes;
}

function currentStyle() { return document.querySelector('input[name="style"]:checked').value; }

function applyStyleToRegion(style, box) {
  switch (style) {
    case "pixelate": pixelateRegion(box); break;
    case "frosted": frostedRegion(box); break;
    case "box": blackBoxRegion(box); break;
    case "solid": solidFillRegion(box, solidColorInput.value); break;
    default: blurRegion(box);
  }
}

function renderResult(data) {
  drawImageOnly(currentImage);
  const wanted = activeClasses();
  const boxesToCensor = data.all_detections.filter(d => wanted.includes(d.class));
  const style = currentStyle();

  boxesToCensor.forEach(det => applyStyleToRegion(style, det.box));
  if (showAllToggle.checked) data.all_detections.forEach(det => drawBoxOutline(det.box, det.class, boxesToCensor.includes(det)));

  setStatus(boxesToCensor.length === 0 ? "No selected regions detected — nothing to censor." : `Detected & censored ${boxesToCensor.length} region(s).`, "ok");
  populateLog(data, boxesToCensor);
}

function clampBox([x, y, w, h]) {
  x = Math.max(0, Math.floor(x)); y = Math.max(0, Math.floor(y));
  w = Math.min(Math.ceil(w), canvas.width - x); h = Math.min(Math.ceil(h), canvas.height - y);
  return [x, y, w, h];
}

function blackBoxRegion(box) { const [x, y, w, h] = clampBox(box); if (w <= 0 || h <= 0) return; ctx.fillStyle = "#000"; ctx.fillRect(x, y, w, h); }

function solidFillRegion(box, color) { const [x, y, w, h] = clampBox(box); if (w <= 0 || h <= 0) return; ctx.fillStyle = color || "#000000"; ctx.fillRect(x, y, w, h); }

function blurRegion(box, radius = 18) {
  const [x, y, w, h] = clampBox(box); if (w <= 0 || h <= 0) return;
  const pad = radius * 2;
  const sx = Math.max(0, x - pad), sy = Math.max(0, y - pad);
  const ex = Math.min(canvas.width, x + w + pad), ey = Math.min(canvas.height, y + h + pad);
  const sw = ex - sx, sh = ey - sy;
  const off = document.createElement("canvas"); off.width = sw; off.height = sh;
  const offCtx = off.getContext("2d");
  offCtx.filter = `blur(${radius}px)`;
  offCtx.drawImage(currentImage, sx, sy, sw, sh, 0, 0, sw, sh);
  ctx.drawImage(off, x - sx, y - sy, w, h, x, y, w, h);
}

function pixelateRegion(box) {
  const [x, y, w, h] = clampBox(box); if (w <= 0 || h <= 0) return;
  const blockSize = Math.max(6, Math.round(Math.min(w, h) / 9));
  const smallW = Math.max(1, Math.round(w / blockSize));
  const smallH = Math.max(1, Math.round(h / blockSize));
  const off = document.createElement("canvas");
  off.width = smallW; off.height = smallH;
  off.getContext("2d").drawImage(currentImage, x, y, w, h, 0, 0, smallW, smallH);
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0, smallW, smallH, x, y, w, h);
  ctx.restore();
}

function frostedRegion(box) {
  const [x, y, w, h] = clampBox(box); if (w <= 0 || h <= 0) return;
  blurRegion(box, 32);
  ctx.save();
  ctx.fillStyle = "rgba(233, 236, 242, 0.32)";
  ctx.fillRect(x, y, w, h);
  ctx.restore();
}

function drawBoxOutline([x, y, w, h], label, censored) {
  ctx.save();
  ctx.strokeStyle = censored ? "#ef5d6f" : "#4bd8c9";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, w, h);
  ctx.font = "12px sans-serif";
  const padding = 4;
  const textWidth = ctx.measureText(label).width;
  ctx.fillStyle = censored ? "#ef5d6f" : "#4bd8c9";
  ctx.fillRect(x, y, textWidth + padding * 2, 20);
  ctx.fillStyle = "#0f172a";
  ctx.fillText(label, x + padding, y + 14);
  ctx.restore();
}

function clearLog() { logList.innerHTML = ""; logList.style.display = "none"; logEmpty.style.display = "block"; logCount.textContent = ""; }

function populateLog(data, boxesToCensor) {
  logList.innerHTML = "";
  if (data.all_detections.length === 0) { clearLog(); return; }
  logEmpty.style.display = "none"; logList.style.display = "block";
  logCount.textContent = `${data.all_detections.length} found`;

  data.all_detections.forEach(det => {
    const isCensored = boxesToCensor.includes(det);
    const [x, y, w, h] = det.box;
    const row = document.createElement("div");
    row.className = "log-row";
    row.innerHTML = `
      <div class="lr-top">
        <span class="lr-class">${det.class.replaceAll('_', ' ').toLowerCase()}</span>
        <span class="lr-score">${(det.score * 100).toFixed(1)}%</span>
      </div>
      <div class="log-coords">
        <div>x ${Math.round(x)}px</div><div>y ${Math.round(y)}px</div>
        <div>w ${Math.round(w)}px</div><div>h ${Math.round(h)}px</div>
      </div>
      <span class="tag ${isCensored ? 'censored' : 'ignored'}">${isCensored ? 'censored' : 'ignored'}</span>
    `;
    logList.appendChild(row);
  });
}

function updateSolidColorVisibility() {
  solidColorWrap.style.display = currentStyle() === "solid" ? "flex" : "none";
}

showAllToggle.addEventListener("change", () => { if (lastResult && mode === "image") renderResult(lastResult); });
[censorGenitals, censorBreasts, censorButtocks, censorFeet, censorFace, censorBelly, censorArmpits].forEach(cb => cb.addEventListener("change", () => { if (lastResult && mode === "image") renderResult(lastResult); }));
styleRadios.forEach(r => r.addEventListener("change", () => {
  updateSolidColorVisibility();
  if (lastResult && mode === "image") renderResult(lastResult);
}));
solidColorInput.addEventListener("input", () => {
  solidSwatchPreview.style.background = solidColorInput.value;
  if (lastResult && mode === "image" && currentStyle() === "solid") renderResult(lastResult);
});
updateSolidColorVisibility();
solidSwatchPreview.style.background = solidColorInput.value;

downloadResultBtn.addEventListener("click", () => {
  if (!lastResult) return;
  canvas.toBlob((blob) => {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const base = (currentFile && currentFile.name ? currentFile.name.replace(/\.[^.]+$/, "") : "censored");
    const a = document.createElement("a");
    a.href = url; a.download = `${base}-censored.png`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }, "image/png");
});

copyResultBtn.addEventListener("click", () => {
  if (!lastResult) return;
  canvas.toBlob(async (blob) => {
    if (!blob) return;
    try {
      await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })]);
      setStatus("Copied the censored image to your clipboard.", "ok");
    } catch (e) {
      setStatus("Couldn't copy automatically in this browser — use the download button instead.", "error");
    }
  }, "image/png");
});

const previewPanel = document.getElementById("previewPanel");
const fullscreenBtn = document.getElementById("fullscreenBtn");
const focusBtn = document.getElementById("focusBtn");
const focusBackBtn = document.getElementById("focusBackBtn");
let focusActive = false;

function applyExpandedStyle(active) {
  previewPanel.style.position = active ? "fixed" : "";
  previewPanel.style.inset = active ? "0" : "";
  previewPanel.style.zIndex = active ? "50" : "";
  previewPanel.style.borderRadius = active ? "0" : "";
  document.body.style.overflow = active ? "hidden" : "";
}

fullscreenBtn.addEventListener("click", () => {
  if (!document.fullscreenElement) previewPanel.requestFullscreen?.().catch(err => setStatus("Fullscreen failed: " + err.message, "error"));
  else document.exitFullscreen?.();
});
document.addEventListener("fullscreenchange", () => {
  const isFs = document.fullscreenElement === previewPanel;
  applyExpandedStyle(isFs);
  if (isFs) { focusActive = false; focusBackBtn.style.display = "none"; }
});
function enterFocus() { focusActive = true; applyExpandedStyle(true); focusBackBtn.style.display = "flex"; }
function exitFocus() { focusActive = false; applyExpandedStyle(false); focusBackBtn.style.display = "none"; }
focusBtn.addEventListener("click", () => { if (document.fullscreenElement) { document.exitFullscreen?.(); return; } focusActive ? exitFocus() : enterFocus(); });
focusBackBtn.addEventListener("click", exitFocus);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && focusActive) exitFocus(); });

resetBtn.addEventListener("click", () => {
  currentImage = null; currentFile = null; lastResult = null; mode = null;
  fileInput.value = "";
  hideVideoPreview();
  canvasWrap.style.display = "none";
  canvasPlaceholder.style.display = "flex";
  showAllWrap.style.display = "flex";
  logCard.style.display = "block";
  setAnalyzeEnabled(false);
  setResultActionsEnabled(false);
  setStatus("", "");
  clearLog();
});

loadConfig().then(render);
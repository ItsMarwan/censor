"""
Genital/breast NSFW region detector — backend
---------------------------------------------
Image path: NudeNet object detector -> bounding boxes -> frontend blurs them.

Video path (mp4): decode frames with OpenCV, run NudeNet in GPU batches on a
sampled subset of frames (e.g. every 5th frame), linearly interpolate boxes
for the frames in between so blur doesn't "flicker" on/off, apply the censor
server-side (blur or black box, matching what the user picked in the UI),
then re-encode to H.264 mp4 with ffmpeg — piping raw frames in and muxing the
original audio track back in.

This process is meant to be started by connect_launcher.py, which also opens
a tunnel so the static site can reach it without a hardcoded IP. You can also
run it directly for local-only use:

    pip install -r requirements.txt
    python app.py

Requires ffmpeg on PATH. Requires an NVIDIA GPU + onnxruntime-gpu for fast
video processing (NudeNet uses onnxruntime under the hood).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from nudenet import NudeDetector

app = Flask(__name__)
# allow_headers includes ngrok-skip-browser-warning: when app.py is exposed
# through a free ngrok tunnel (see connect_launcher.py), ngrok shows an HTML
# interstitial to browser-ish requests unless this header is present. The
# frontend sends it on every fetch(); CORS just needs to allow it through.
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    supports_credentials=False,
    allow_headers=["Content-Type", "ngrok-skip-browser-warning"],
)


@app.after_request
def add_cors_headers(response):
    # Belt-and-suspenders: guarantee the header is present even on error
    # responses / responses raised from inside send_file's generator, which
    # some flask-cors versions don't reliably patch.
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, ngrok-skip-browser-warning")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response


@app.errorhandler(Exception)
def handle_any_error(e):
    import traceback
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

# onnxruntime-gpu will pick up CUDAExecutionProvider automatically if it's
# installed and a CUDA-capable GPU + drivers are present. NudeDetector doesn't
# expose a provider kwarg in older versions, so we just rely on the onnxruntime
# wheel installed (see requirements.txt: onnxruntime-gpu, not onnxruntime).
#
# NudeDetector(), left to its own defaults, looks for its .onnx weights next
# to nudenet's own nudenet.py (os.path.dirname(__file__)/320n.onnx). That
# works fine with a normal `pip install`, but breaks for someone who only
# downloaded the packaged .exe and not the Python library: a PyInstaller
# build only auto-bundles Python modules, not sibling data files, so unless
# --add-data explicitly included it (see README > Packaging), the model
# simply isn't there. That's also just as true if a build was ever shipped
# without it by mistake — this needs to be a runtime-recoverable situation,
# not just a packaging instruction people have to get right.
#
# So: model lookup is separate from detector construction. connect_launcher.py
# checks find_local_model_path() before starting the server; if it's missing,
# it asks the user whether to download it, calls download_model() with a
# progress callback if they say yes, then calls init_detector(). Running
# app.py directly (python app.py) does the same thing non-interactively at
# the bottom of this file.

MODEL_FILENAME = "320n.onnx"
# Same file NudeNet's own PyPI wheel ships (notAI-tech/NudeNet, v3 branch) —
# pinned to a specific ref (not a moving branch pointer at request time) plus
# a checksum, so a downloaded file is verified byte-for-byte before use.
MODEL_URL = "https://raw.githubusercontent.com/notAI-tech/NudeNet/v3/nudenet/320n.onnx"
MODEL_SHA256 = "c15d8273adad2d0a92f014cc69ab2d6c311a06777a55545f2c4eb46f51911f0f"


def user_model_cache_dir():
    """A persistent (non-temp) per-user folder to download the model into,
    so a onefile .exe — which re-extracts to a fresh, wiped _MEI#### temp
    folder on *every* launch — only needs to download it once, not once per
    run."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "CensorSandbox" / "models"


def find_local_model_path():
    """Look everywhere the model plausibly is. Returns a path string, or
    None if it isn't anywhere — never raises, so callers can decide what to
    do about a missing model (prompt to download, fail loudly, etc.)."""
    import nudenet as _nudenet_pkg

    candidates = []
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: files added via `--add-data src;nudenet`
        # land at sys._MEIPASS/nudenet/<file> after extraction.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "nudenet" / MODEL_FILENAME)
        # Onedir builds (or a manually copied folder) may instead sit right
        # next to the executable.
        candidates.append(Path(sys.executable).parent / "nudenet" / MODEL_FILENAME)
        candidates.append(Path(sys.executable).parent / "_internal" / "nudenet" / MODEL_FILENAME)
    # Running from source, or a frozen build where --add-data happened to
    # bundle it back at the package's normal relative spot.
    candidates.append(Path(os.path.dirname(_nudenet_pkg.__file__)) / MODEL_FILENAME)
    # Previously auto-downloaded by this same app.
    candidates.append(user_model_cache_dir() / MODEL_FILENAME)

    for path in candidates:
        if path.is_file():
            return str(path)
    return None


class ModelDownloadError(Exception):
    pass


def download_model(progress_cb=None, cancel_event=None):
    """Downloads MODEL_URL to user_model_cache_dir(), verifying its sha256
    against MODEL_SHA256 before it's considered valid. progress_cb, if given,
    is called with a float 0..1 as bytes arrive. Returns the final path.
    Raises ModelDownloadError on any network failure or checksum mismatch —
    never leaves a corrupt/partial file at the final destination."""
    import hashlib

    cache_dir = user_model_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    final_path = cache_dir / MODEL_FILENAME
    tmp_path = cache_dir / (MODEL_FILENAME + ".part")

    try:
        req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "CensorSandbox/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            hasher = hashlib.sha256()
            written = 0
            with open(tmp_path, "wb") as f:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ModelDownloadError("Download cancelled.")
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    written += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(1.0, written / total))
    except ModelDownloadError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise ModelDownloadError(f"Couldn't download the model: {e}") from e

    digest = hasher.hexdigest()
    if digest != MODEL_SHA256:
        tmp_path.unlink(missing_ok=True)
        raise ModelDownloadError(
            "Downloaded file didn't match the expected checksum — it may have been "
            "corrupted or tampered with in transit. Nothing was installed; try again."
        )

    tmp_path.replace(final_path)
    return str(final_path)


detector = None


def init_detector(model_path=None):
    """Constructs the global `detector`. Call this once, after confirming a
    model is available, before starting the Flask server."""
    global detector
    if model_path is None:
        model_path = find_local_model_path()
    if not model_path or not Path(model_path).is_file():
        raise FileNotFoundError("The detection model wasn't found on this PC.")
    detector = NudeDetector(model_path=model_path)
    return detector


# Multiple videos can be uploaded/processed at once (each gets its own
# background thread + job_id), but running unlimited concurrent GPU
# inference passes risks CUDA OOM. Cap how many jobs can be actively running
# the model at the same time — decoding/writing/encoding for other jobs can
# still happen in parallel, only the detect_batch() call queues behind this.
MAX_CONCURRENT_GPU_JOBS = 2
gpu_semaphore = threading.Semaphore(MAX_CONCURRENT_GPU_JOBS)

MIN_SCORE = 0.25

GENITAL_ANUS_CLASSES = ["FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"]
BREAST_CLASSES = ["FEMALE_BREAST_EXPOSED", "MALE_BREAST_EXPOSED"]
BUTTOCKS_CLASSES = ["BUTTOCKS_EXPOSED"]
FEET_CLASSES = ["FEET_EXPOSED"]
FACE_CLASSES = ["FACE_FEMALE", "FACE_MALE"]
BELLY_CLASSES = ["BELLY_EXPOSED"]
ARMPITS_CLASSES = ["ARMPITS_EXPOSED"]

# NOTE: NudeNet is a body-part detector, not a general content classifier.
# It has no classes for scat, urine/piss, or bestiality/animal genitalia — so
# there's nothing real to wire a tickbox up to for those categories with this
# model. The categories below are the full set of things NudeNet actually
# detects with a bounding box.
CATEGORY_MAP = {
    "genitals": GENITAL_ANUS_CLASSES,
    "breasts": BREAST_CLASSES,
    "buttocks": BUTTOCKS_CLASSES,
    "feet": FEET_CLASSES,
    "face": FACE_CLASSES,
    "belly": BELLY_CLASSES,
    "armpits": ARMPITS_CLASSES,
}

WORK_DIR = Path(tempfile.gettempdir()) / "nsfw_blur_jobs"
WORK_DIR.mkdir(exist_ok=True)

# In-memory job registry for async video processing + progress polling.
JOBS = {}
JOBS_LOCK = threading.Lock()


def set_progress(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)


# ---------------------------------------------------------------- utilities

def clamp_box(x, y, w, h, width, height):
    x = max(0, int(x))
    y = max(0, int(y))
    w = min(int(round(w)), width - x)
    h = min(int(round(h)), height - y)
    return x, y, w, h


def blur_region(frame, box, radius=25):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w]
    k = max(15, (min(w, h) // 3) | 1)
    if k % 2 == 0:
        k += 1
    blurred = cv2.GaussianBlur(roi, (k, k), radius)
    frame[y:y + h, x:x + w] = blurred


def black_box_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    frame[y:y + h, x:x + w] = 0


def pixelate_region(frame, box):
    """Mirrors the client's pixelateRegion: shrink to blocks, scale back up
    with nearest-neighbor so each source block becomes one flat square."""
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w]
    block_size = max(6, round(min(w, h) / 9))
    small_w = max(1, round(w / block_size))
    small_h = max(1, round(h / block_size))
    small = cv2.resize(roi, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
    pixelated = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
    frame[y:y + h, x:x + w] = pixelated


def frosted_region(frame, box):
    """Mirrors the client's frostedRegion: heavier blur plus a translucent
    light wash so shape still reads but no detail does."""
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    blur_region(frame, (x, y, w, h), radius=32)
    roi = frame[y:y + h, x:x + w]
    wash = np.full_like(roi, 233)  # approximates rgba(233,236,242,0.32)
    frame[y:y + h, x:x + w] = cv2.addWeighted(roi, 0.68, wash, 0.32, 0)


def solid_fill_region(frame, box, color_bgr):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    frame[y:y + h, x:x + w] = color_bgr


def hex_to_bgr(hex_color):
    hex_color = (hex_color or "#000000").lstrip("#")
    if len(hex_color) != 6:
        return (0, 0, 0)
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return (b, g, r)
    except ValueError:
        return (0, 0, 0)


def lerp_box(box_a, box_b, t):
    return [a + (b - a) * t for a, b in zip(box_a, box_b)]


def match_detections(prev_dets, next_dets):
    pairs = []
    used_next = set()
    for d in prev_dets:
        best_j, best_dist = None, None
        for j, nd in enumerate(next_dets):
            if j in used_next or nd["class"] != d["class"]:
                continue
            dist = sum((a - b) ** 2 for a, b in zip(d["box"], nd["box"]))
            if best_dist is None or dist < best_dist:
                best_dist, best_j = dist, j
        if best_j is not None:
            used_next.add(best_j)
            pairs.append((d["class"], d["box"], next_dets[best_j]["box"]))
        else:
            pairs.append((d["class"], d["box"], None))
    for j, nd in enumerate(next_dets):
        if j not in used_next:
            pairs.append((nd["class"], None, nd["box"]))
    return pairs


def run_ffmpeg_mux(raw_frames_pattern, fps, width, height, src_video_path, out_path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(src_video_path)],
        capture_output=True, text=True,
    )
    has_audio = bool(probe.stdout.strip())

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(raw_frames_pattern),
    ]
    if has_audio:
        cmd += ["-i", str(src_video_path), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-shortest"]
    cmd += [
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "medium",
        "-crf", "20",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------- routes

@app.route("/detect", methods=["POST"])
def detect():
    if "image" not in request.files:
        return jsonify({"error": "no image field in form-data"}), 400

    raw = request.files["image"].read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "could not decode image"}), 400

    detections = detector.detect(img)
    all_detections = [
        {"class": d["class"], "score": round(d["score"], 3), "box": d["box"]}
        for d in detections if d["score"] >= MIN_SCORE
    ]
    return jsonify({"width": int(img.shape[1]), "height": int(img.shape[0]), "all_detections": all_detections})


def process_video_job(job_id, src_path, job_dir, frames_dir, sample_every, style, censor_classes, solid_color_bgr=(0, 0, 0)):
    try:
        set_progress(job_id, status="decoding", current=0, total=1)

        cap = cv2.VideoCapture(str(src_path))
        if not cap.isOpened():
            raise RuntimeError("could not open video")

        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total_frames_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

        sample_indices = []
        sample_frames = []
        frame_index = 0
        ok, frame = cap.read()
        while ok:
            if frame_index % sample_every == 0:
                sample_indices.append(frame_index)
                sample_frames.append(frame.copy())
            frame_index += 1
            set_progress(job_id, status="decoding", current=frame_index, total=total_frames_hint or 0)
            ok, frame = cap.read()
        cap.release()

        total_frames = frame_index
        if total_frames == 0:
            raise RuntimeError("no frames decoded from video")

        if sample_indices[-1] != total_frames - 1:
            sample_indices.append(total_frames - 1)
            cap = cv2.VideoCapture(str(src_path))
            if not cap.isOpened():
                raise RuntimeError("could not reopen video for sampling")
            cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                raise RuntimeError("could not read last frame for sampling")
            sample_frames.append(frame.copy())

        detections_by_sample = []
        detect_batch_size = 8
        set_progress(job_id, status="queued_for_gpu", current=0, total=len(sample_indices))
        with gpu_semaphore:
            set_progress(job_id, status="detecting", current=0, total=len(sample_indices))
            for start in range(0, len(sample_indices), detect_batch_size):
                chunk_frames = sample_frames[start:start + detect_batch_size]
                try:
                    batch_results = detector.detect_batch(chunk_frames)
                except AttributeError:
                    batch_results = [detector.detect(f) for f in chunk_frames]

                for dets in batch_results:
                    filtered = [
                        {"class": d["class"], "score": round(d["score"], 3), "box": d["box"]}
                        for d in dets if d["score"] >= MIN_SCORE and d["class"] in censor_classes
                    ]
                    detections_by_sample.append(filtered)

                set_progress(job_id, status="detecting", current=len(detections_by_sample), total=len(sample_indices))

        total_regions_censored = 0
        set_progress(job_id, status="censoring", current=0, total=total_frames)
        censored_count = 0

        cap = cv2.VideoCapture(str(src_path))
        if not cap.isOpened():
            raise RuntimeError("could not reopen video for censoring")

        segment = 0
        if len(sample_indices) > 1:
            start_idx = sample_indices[0]
            end_idx = sample_indices[1]
            pairs = match_detections(detections_by_sample[0], detections_by_sample[1])
            span = max(1, end_idx - start_idx)
        else:
            start_idx = 0
            end_idx = 0
            pairs = match_detections(detections_by_sample[0], [])
            span = 1

        for frame_index in range(total_frames):
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                raise RuntimeError(f"failed reading frame {frame_index}")

            while segment < len(sample_indices) - 2 and frame_index > sample_indices[segment + 1]:
                segment += 1
                start_idx = sample_indices[segment]
                end_idx = sample_indices[segment + 1]
                pairs = match_detections(detections_by_sample[segment], detections_by_sample[segment + 1])
                span = max(1, end_idx - start_idx)

            t = (frame_index - start_idx) / span if frame_index >= start_idx else 0
            for cls, box_a, box_b in pairs:
                if box_a is not None and box_b is not None:
                    box = lerp_box(box_a, box_b, t)
                elif box_a is not None and t < 0.5:
                    box = box_a
                elif box_b is not None and t >= 0.5:
                    box = box_b
                else:
                    continue
                total_regions_censored += 1
                if style == "pixelate":
                    pixelate_region(frame, box)
                elif style == "frosted":
                    frosted_region(frame, box)
                elif style == "box":
                    black_box_region(frame, box)
                elif style == "solid":
                    solid_fill_region(frame, box, solid_color_bgr)
                else:
                    blur_region(frame, box)

            cv2.imwrite(str(frames_dir / f"frame_{frame_index:06d}.png"), frame)
            censored_count += 1
            set_progress(job_id, status="censoring", current=censored_count, total=total_frames)

        cap.release()

        set_progress(job_id, status="encoding", current=0, total=1)
        out_path = job_dir / "output.mp4"
        run_ffmpeg_mux(frames_dir / "frame_%06d.png", fps, 0, 0, src_path, out_path)

        set_progress(
            job_id, status="done", current=total_frames, total=total_frames,
            result_path=str(out_path),
            meta={
                "total_frames": total_frames,
                "sampled_frames": len(sample_indices),
                "regions_censored": total_regions_censored,
            },
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        set_progress(job_id, status="error", error=str(e))


@app.route("/start_video_job", methods=["POST"])
def start_video_job():
    if "video" not in request.files:
        return jsonify({"error": "no video field in form-data"}), 400

    sample_every = max(1, int(request.form.get("sample_every", 5)))
    style = request.form.get("style", "blur")
    solid_color_bgr = hex_to_bgr(request.form.get("solid_color", "#000000"))

    requested_categories = [c.strip() for c in request.form.get("categories", "genitals,breasts").split(",") if c.strip()]
    censor_classes = []
    for cat in requested_categories:
        censor_classes += CATEGORY_MAP.get(cat, [])

    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True)

    src_path = job_dir / "input.mp4"
    request.files["video"].save(src_path)

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "current": 0, "total": 1, "result_path": None, "error": None, "meta": {}}

    thread = threading.Thread(
        target=process_video_job,
        args=(job_id, src_path, job_dir, frames_dir, sample_every, style, censor_classes, solid_color_bgr),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/video_progress/<job_id>")
def video_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify({
        "status": job["status"],
        "current": job["current"],
        "total": job["total"],
        "error": job.get("error"),
        "meta": job.get("meta", {}),
    })


@app.route("/video_result/<job_id>")
def video_result(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job_id"}), 404
    if job["status"] != "done" or not job["result_path"]:
        return jsonify({"error": "job not finished", "status": job["status"]}), 409

    response = send_file(job["result_path"], mimetype="video/mp4", as_attachment=False, conditional=True)

    @response.call_on_close
    def _cleanup():
        job_dir = WORK_DIR / job_id
        shutil.rmtree(job_dir, ignore_errors=True)
        with JOBS_LOCK:
            JOBS.pop(job_id, None)

    return response


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Bound to localhost only — connect_launcher.py is what makes this
    # reachable from the browser, via a tunnel it controls and tears down.
    # (connect_launcher.py calls init_detector() itself, with a GUI prompt to
    # auto-download the model if it's missing, before ever importing/running
    # this module. Running app.py directly skips that UI, so just try the
    # normal search and fail with a clear message rather than a raw
    # ONNXRuntimeError if nothing's there.)
    try:
        init_detector()
    except FileNotFoundError as e:
        print(f"\n✗ {e}")
        print(f"  Expected it at: {find_local_model_path() or user_model_cache_dir() / MODEL_FILENAME}")
        print(f"  Download it manually from: {MODEL_URL}")
        raise SystemExit(1)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)

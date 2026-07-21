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

try:
    from style_utils import normalize_effect_style
except ImportError:  # pragma: no cover - fallback for direct script execution
    from backend.style_utils import normalize_effect_style

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
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, ngrok-skip-browser-warning")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response


@app.errorhandler(Exception)
def handle_any_error(e):
    import traceback
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

MODEL_FILENAME = "320n.onnx"
MODEL_URL = "https://raw.githubusercontent.com/notAI-tech/NudeNet/v3/nudenet/320n.onnx"
MODEL_SHA256 = "c15d8273adad2d0a92f014cc69ab2d6c311a06777a55545f2c4eb46f51911f0f"

def user_model_cache_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "CensorSandbox" / "models"


def find_local_model_path():
    import nudenet as _nudenet_pkg

    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "nudenet" / MODEL_FILENAME)
        candidates.append(Path(sys.executable).parent / "nudenet" / MODEL_FILENAME)
        candidates.append(Path(sys.executable).parent / "_internal" / "nudenet" / MODEL_FILENAME)
    candidates.append(Path(os.path.dirname(_nudenet_pkg.__file__)) / MODEL_FILENAME)
    candidates.append(user_model_cache_dir() / MODEL_FILENAME)

    for path in candidates:
        if path.is_file():
            return str(path)
    return None


class ModelDownloadError(Exception):
    pass


def download_model(progress_cb=None, cancel_event=None):
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
    global detector
    if model_path is None:
        model_path = find_local_model_path()
    if not model_path or not Path(model_path).is_file():
        raise FileNotFoundError("The detection model wasn't found on this PC.")
    detector = NudeDetector(model_path=model_path)
    return detector

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

JOBS = {}
JOBS_LOCK = threading.Lock()

def set_progress(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)

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
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    blur_region(frame, (x, y, w, h), radius=32)
    roi = frame[y:y + h, x:x + w]
    wash = np.full_like(roi, 233)
    frame[y:y + h, x:x + w] = cv2.addWeighted(roi, 0.68, wash, 0.32, 0)


def smudge_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w]
    blurred = cv2.medianBlur(roi, 5)
    frame[y:y + h, x:x + w] = blurred


def cover_region(frame, box, color_bgr=(24, 24, 24)):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w]
    tint = np.full_like(roi, color_bgr, dtype=np.uint8)
    frame[y:y + h, x:x + w] = cv2.addWeighted(roi, 0.7, tint, 0.3, 0)


def solid_fill_region(frame, box, color_bgr):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    frame[y:y + h, x:x + w] = color_bgr

def _dashed_line(frame, pt1, pt2, color_bgr, thickness, dash, gap):
    x1, y1 = pt1
    x2, y2 = pt2
    length = max(1, int(round(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)))
    step = dash + gap
    pos = 0
    while pos < length:
        end = min(length, pos + dash)
        sx = x1 + (x2 - x1) * pos / length
        sy = y1 + (y2 - y1) * pos / length
        ex = x1 + (x2 - x1) * end / length
        ey = y1 + (y2 - y1) * end / length
        cv2.line(frame, (int(round(sx)), int(round(sy))), (int(round(ex)), int(round(ey))), color_bgr, thickness, cv2.LINE_AA)
        pos += step

def outline_region(frame, box, color_bgr=(201, 216, 75)):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    thickness, dash, gap = 3, 7, 6
    _dashed_line(frame, (x, y), (x + w, y), color_bgr, thickness, dash, gap)
    _dashed_line(frame, (x + w, y), (x + w, y + h), color_bgr, thickness, dash, gap)
    _dashed_line(frame, (x + w, y + h), (x, y + h), color_bgr, thickness, dash, gap)
    _dashed_line(frame, (x, y + h), (x, y), color_bgr, thickness, dash, gap)

def glitch_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w].copy()
    slices = 5
    slice_h = max(1, round(h / slices))
    tint_danger = np.array([111, 93, 239], dtype=np.float32)
    tint_accent = np.array([201, 216, 75], dtype=np.float32)
    i = 0
    band_idx = 0
    while i < h:
        sh = min(slice_h, h - i)
        band = roi[i:i + sh]
        shift = 4 if band_idx % 2 == 0 else -4
        shifted = np.roll(band, shift, axis=1)
        tint = tint_danger if band_idx % 2 == 0 else tint_accent
        tinted = cv2.addWeighted(shifted, 0.82, np.full_like(shifted, tint), 0.18, 0)
        roi[i:i + sh] = tinted
        i += sh
        band_idx += 1
    frame[y:y + h, x:x + w] = roi
    cv2.rectangle(frame, (x + 1, y + 1), (x + w - 2, y + h - 2), (255, 255, 255), 1, cv2.LINE_AA)

def rainbow_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w].astype(np.float32)

    stops_rgb = [
        (255, 77, 109), (247, 208, 63), (74, 217, 255),
        (124, 107, 255), (132, 255, 124), (255, 77, 109),
    ]
    stops_bgr = np.array([[b, g, r] for (r, g, b) in stops_rgb], dtype=np.float32)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    t = np.clip((yy / max(1, h - 1) + xx / max(1, w - 1)) / 2.0, 0, 1)

    n_segments = len(stops_bgr) - 1
    seg = np.clip((t * n_segments).astype(np.int32), 0, n_segments - 1)
    local_t = (t * n_segments) - seg
    c0 = stops_bgr[seg]
    c1 = stops_bgr[np.clip(seg + 1, 0, n_segments)]
    gradient = c0 + (c1 - c0) * local_t[..., None]

    g = gradient / 255.0
    o = roi / 255.0
    overlaid = np.where(g <= 0.5, 2 * g * o, 1 - 2 * (1 - g) * (1 - o))
    frame[y:y + h, x:x + w] = np.clip(overlaid * 255.0, 0, 255).astype(np.uint8)

def dots_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    frame[y:y + h, x:x + w] = (26, 27, 18)
    step = max(7, round(min(w, h) / 10))
    radius = max(1, int(round(step * 0.22)))
    color = (201, 216, 75)
    py = 0
    while py < h:
        px = 0
        while px < w:
            cx = x + px + int(round(step * 0.45))
            cy = y + py + int(round(step * 0.45))
            if cx < x + w and cy < y + h:
                cv2.circle(frame, (cx, cy), radius, color, -1, cv2.LINE_AA)
            px += step
        py += step

def scanline_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    base = np.full_like(roi, (39, 40, 17))
    line = 0
    while line < h:
        lh = min(3, h - line)
        base[line:line + lh] = np.clip(base[line:line + lh] + np.array([41, 55, 19], dtype=np.float32), 0, 255)
        line += 6
    screened = 255.0 - (255.0 - base) * (255.0 - roi) / 255.0
    frame[y:y + h, x:x + w] = np.clip(screened, 0, 255).astype(np.uint8)

def negative_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    frame[y:y + h, x:x + w] = 255 - frame[y:y + h, x:x + w]

def emboss_region(frame, box):
    x, y, w, h = box
    height, width = frame.shape[:2]
    x, y, w, h = clamp_box(x, y, w, h, width, height)
    if w <= 0 or h <= 0:
        return
    frame[y:y + h, x:x + w] = (38, 28, 23)
    if w > 4 and h > 4:
        cv2.rectangle(frame, (x + 1, y + 1), (x + w - 2, y + h - 2), (235, 235, 235), 2, cv2.LINE_AA)
    if w > 8 and h > 8:
        cv2.rectangle(frame, (x + 3, y + 3), (x + w - 6, y + h - 6), (10, 10, 10), 2, cv2.LINE_AA)

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

        # Normalize the style strings universally to guard against misnamed HTML inputs
        s = normalize_effect_style(style)

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
                
                # We use the thoroughly robust 's' comparison check right here!
                if "pixel" in s:
                    pixelate_region(frame, box)
                elif "frost" in s:
                    frosted_region(frame, box)
                elif s == "smudge":
                    smudge_region(frame, box)
                elif s == "cover":
                    cover_region(frame, box, solid_color_bgr)
                elif s in ("box", "black-box", "blackbox"):
                    black_box_region(frame, box)
                elif "solid" in s:
                    solid_fill_region(frame, box, solid_color_bgr)
                elif s in ("soft-blur", "soft"):
                    blur_region(frame, box, radius=12)
                elif s == "outline":
                    outline_region(frame, box)
                elif s == "glitch":
                    glitch_region(frame, box)
                elif s == "rainbow":
                    rainbow_region(frame, box)
                elif s == "dots":
                    dots_region(frame, box)
                elif s == "scanline":
                    scanline_region(frame, box)
                elif s == "negative":
                    negative_region(frame, box)
                elif s == "emboss":
                    emboss_region(frame, box)
                else:
                    blur_region(frame, box) # Only defaults if utterly unrecognizable

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
    style = normalize_effect_style(
        request.form.get("style")
        or request.form.get("censor_style")
        or request.form.get("style_name")
    )
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
    try:
        init_detector()
    except FileNotFoundError as e:
        print(f"\n✗ {e}")
        print(f"  Expected it at: {find_local_model_path() or user_model_cache_dir() / MODEL_FILENAME}")
        print(f"  Download it manually from: {MODEL_URL}")
        raise SystemExit(1)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
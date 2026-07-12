# Censor toolkit

Two pieces:
- **`site/`** — the static GitHub Pages front end (`index.html`, `app.js`, `styles.css`).
- **`backend/`** — `app.py` (the NudeNet detector) plus `connect_launcher.py`,
  the desktop app people download and run. It's a normal windowed GUI
  (Tkinter — ships with Python, nothing extra to install), styled to match
  the site. It boots the detector, optionally opens a tunnel with `pyngrok`,
  and shows a pairing code — this is also the exact thing packaged into
  `CensorSandbox-Setup.exe` that the site's home page links to.

## What the app looks like

`connect_launcher.py` has two screens:

1. **Start screen** — choose Tunnel (pair with another device) or Local only
   (same machine), then click **Start sandbox**. A progress bar shows it
   booting the detector and, if you picked tunnel mode, opening the tunnel.
2. **Ready screen** — a "Sandbox ready" card, your pairing code in a
   monospace teal box, **Copy code** / **Open pairing page** buttons, and a
   note that closing the window disconnects the sandbox. This is deliberately
   the same layout as the "What you'll see when you open it" mockup on the
   site's home page, so what you download matches what you were shown before
   downloading it. The raw tunnel URL is hidden behind a "Show raw address"
   toggle — the app leads with the short code, not a URL.

## How the "masked URL" actually works

The pairing code is the tunnel address (e.g. `https://8f2a91cd.ngrok-free.app`)
base64url-encoded. The site's `404.html` catches deep links like
`/censor/u/<code>` (GitHub Pages has no server-side router), bounces to
`index.html`, and `app.js` decodes the code back into the real tunnel URL
entirely in the visitor's own browser, then uses it for every `fetch()` call.

That means:
- It's a **cosmetic/obfuscation layer**, not a hidden relay — there's no
  server under anyone's control proxying uploads. Nothing is registered
  anywhere when the code is generated.
- The code is only as private as who you give it to. Treat it like a
  screen-share link: anyone with it can reach your sandbox for as long as
  the launcher is running.
- Closing the app kills the tunnel and the code stops resolving to anything,
  immediately.

## Local mode (fastest — use this when you can)

Pairing through a free ngrok tunnel adds real latency: every request makes a
round trip through ngrok's edge network before it reaches your machine, on
top of free-tier bandwidth throttling. If the browser and the sandbox are on
the *same* machine, skip the tunnel entirely:

- In the app, choose **Local only**.
- `site/config.json` needs `"local": true` so the site's pairing box asks
  for an address (`127.0.0.1:5000`) instead of a masked code:
  ```json
  { "local": true, "local_default_host": "127.0.0.1:5000" }
  ```
- For a genuinely faster *tunneled* setup instead, set `NGROK_AUTHTOKEN`
  (free ngrok account) before launching — authenticated tunnels get a less
  congested edge than anonymous ones.

## Running from source

```bash
cd backend
pip install -r requirements.txt
python connect_launcher.py
```

Requires Python 3.9+, ffmpeg on PATH, and (for fast video) an NVIDIA GPU +
`onnxruntime-gpu`. See `site`'s "For developers" page for the same steps.

## Integrating NudeNet in your own project

`app.py`'s detection logic is just the open-source `nudenet` Python package
— there's no proprietary model here:

```python
# pip install nudenet opencv-python
from nudenet import NudeDetector
import cv2

detector = NudeDetector()
image = cv2.imread("photo.jpg")
results = detector.detect(image)
for r in results:
    # r == {"class": "FEMALE_BREAST_EXPOSED", "score": 0.87, "box": [x, y, w, h]}
    print(r["class"], r["score"], r["box"])
```

- **Classes**: `FEMALE_GENITALIA_EXPOSED`, `MALE_GENITALIA_EXPOSED`,
  `ANUS_EXPOSED`, `FEMALE_BREAST_EXPOSED`, `MALE_BREAST_EXPOSED`,
  `BUTTOCKS_EXPOSED`, `FEET_EXPOSED`, `BELLY_EXPOSED`, `ARMPITS_EXPOSED`,
  `FACE_FEMALE`, `FACE_MALE`. Some versions also return matching
  `_COVERED` classes for clothed regions.
- **Thresholding**: drop anything under ~`0.25` score to cut false positives.
- **Batching**: `detector.detect_batch(list_of_frames)` is far faster than
  calling `detect()` per-frame for video.
- **GPU**: install `onnxruntime-gpu` instead of the CPU package; it picks up
  CUDA automatically if a compatible GPU/driver is present.
- `app.py` in this repo is a complete Flask wrapper around the same calls,
  including video sampling/interpolation — copy from it freely.

---

## Packaging `connect_launcher.py` into `CensorSandbox-Setup.exe`

This is what produces the file the site's **Download for Windows** button
links to (`site/downloads/CensorSandbox-Setup.exe`).

### 1. Generate the brand icon (once)

```bash
pip install pillow
python make_icon.py
```

Writes `assets/icon.ico` (block-with-an-eye mark, matches the site) and
`assets/icon.png`.

### 2. Build the exe

```bash
pip install -r requirements.txt
pip install pyinstaller

pyinstaller connect_launcher.py --name CensorSandbox-Setup --onefile --windowed --icon assets/icon.ico --add-data "app.py;."
```

(On macOS/Linux use `--add-data "app.py:."` — colon instead of semicolon.)

- `--windowed` stops a console window from flashing behind the Tkinter GUI.
- `--onefile` produces a single exe under `dist/`. This is a self-contained
  launcher, not a wizard-style installer — good enough for "download and
  run." If you want an actual install wizard (Start Menu entry, uninstaller,
  Program Files placement), wrap the onefile build with
  [Inno Setup](https://jrsoftware.org/isinfo.php): point its `[Files]`
  section at `dist/CensorSandbox-Setup.exe` and it'll produce a proper
  installer with the same name.
- **GPU builds**: `onnxruntime-gpu` and CUDA are large; PyInstaller will
  bundle whatever's in the environment you build in. Build on a machine with
  the CPU-only `onnxruntime` if you want a smaller download and are fine
  with CPU-speed detection, or ship the GPU build as a separate, larger
  download.

### 3. Test it standalone

Run `dist/CensorSandbox-Setup.exe` on a clean machine (or VM) before
publishing — PyInstaller onefile builds occasionally miss a dynamic import.
If NudeNet's ONNX model file isn't found, add it explicitly:

```bash
  --add-data "path\to\nudenet\model.onnx;nudenet"
```

(check where `pip show nudenet` installed it to get the right source path.)

### 4. Publish

Upload the built exe as a GitHub Release asset, then update
`DOWNLOAD_URL` in `site/app.js` and the `href` on the download button in
`site/index.html` to point at the release asset URL (or keep it at
`site/downloads/CensorSandbox-Setup.exe` if you're committing the binary
into the Pages repo directly — fine for a small single exe, but a GitHub
Release is cleaner for anything that'll be updated often).

### Unsigned-binary warning

Windows will show a SmartScreen "unknown publisher" warning for an unsigned
exe — this is expected and unrelated to how it was built. The site already
tells users to click "More info → Run anyway." Real fix is a code-signing
certificate, which is a paid, separate step (EV certs from a CA like
DigiCert/Sectigo) — not something PyInstaller or Inno Setup can add for you.

"""
connect_launcher.py — sandbox HUD
----------------------------------
Boots app.py locally, opens a public tunnel to it (via pyngrok), and prints a
pairing code/URL for the static site to connect with. Nothing here uploads
your files anywhere — the tunnel just lets your own browser reach your own
machine; the images/videos you censor still never leave it.

Run:
    pip install -r requirements.txt
    python connect_launcher.py

Notes:
  - Requires ffmpeg on PATH (used by app.py for video encoding).
  - pyngrok downloads the ngrok binary itself on first run.
  - Free ngrok tunnels are ephemeral and rate-limited. For anything longer
    than a quick test, set NGROK_AUTHTOKEN in your environment (a free ngrok
    account is enough) for a more stable tunnel.
  - "Masked" connection URL: the itsmarwan.github.io/censor/u/<code> link the
    site shows is a cosmetic address that decodes back to this tunnel's real
    URL entirely inside your own browser (see the site's app.js). This
    script doesn't register anything with a third-party relay — closing this
    process kills the tunnel and the code stops working immediately.
"""

import base64
import os
import sys
import time
import threading
import urllib.request
import urllib.error

APP_PORT = 5000
SITE_BASE = "https://itsmarwan.github.io/censor/u/"


def hud_line(msg, width=60):
    print(msg)


def loading_bar(label, seconds, steps=24):
    for i in range(steps + 1):
        pct = int(i / steps * 100)
        filled = int(i / steps * 28)
        bar = "█" * filled + "░" * (28 - filled)
        sys.stdout.write(f"\r  {label}  [{bar}] {pct:3d}%")
        sys.stdout.flush()
        time.sleep(seconds / steps)
    print()


def wait_for_health(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/health", timeout=1.5) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.4)
    return False


def start_backend():
    """Runs app.py's Flask server in this process on a background thread,
    so this script keeps control of the terminal for the HUD/prompts."""
    import app as backend  # local import: app.py lives alongside this script
    thread = threading.Thread(
        target=lambda: backend.app.run(host="127.0.0.1", port=APP_PORT, debug=False, threaded=True, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread


def open_tunnel():
    from pyngrok import ngrok, conf
    authtoken = os.environ.get("NGROK_AUTHTOKEN")
    if authtoken:
        conf.get_default().auth_token = authtoken
    tunnel = ngrok.connect(APP_PORT, "http")
    return tunnel.public_url.replace("http://", "https://"), tunnel


def make_pairing_code(tunnel_url):
    return base64.urlsafe_b64encode(tunnel_url.encode()).decode().rstrip("=")


def main():
    print()
    print("┌" + "─" * 58 + "┐")
    print("│  CENSOR SANDBOX".ljust(59) + "│")
    print("│  local detector · nothing leaves this machine".ljust(59) + "│")
    print("└" + "─" * 58 + "┘")
    print()

    answer = input("Start local sandbox? [Y/n] ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("Cancelled.")
        return

    print()
    loading_bar("booting detector", 2.0)
    start_backend()

    if not wait_for_health():
        print("\n  ✗ Detector didn't come up. Check that dependencies (nudenet, opencv, ffmpeg) are installed.")
        return

    mode = input("Expose for another device via tunnel, or local-only (same machine)? [t/l] ").strip().lower()

    if mode in ("l", "local", "local-only"):
        local_url = f"http://127.0.0.1:{APP_PORT}"
        print()
        print("  ✓ sandbox ready (local-only — no tunnel opened).")
        print()
        print(f"    In the site, make sure site/config.json has \"local\": true, then enter:")
        print(f"    127.0.0.1:{APP_PORT}")
        print()
        print("  Leave this running while you use the site. Ctrl+C to stop.")
        print()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  Shutting down…")
        return

    loading_bar("opening tunnel", 1.4)
    try:
        tunnel_url, tunnel = open_tunnel()
    except Exception as e:
        print(f"\n  ✗ Couldn't open a tunnel: {e}")
        print("    Install pyngrok (`pip install pyngrok`) or run behind your own reverse proxy instead.")
        return

    code = make_pairing_code(tunnel_url)

    print()
    print("  ✓ sandbox ready — pair the site with it:")
    print()
    print(f"    {SITE_BASE}{code}")
    print()
    print(f"    or paste just the code: {code}")
    print(f"    (raw URL, if you'd rather paste that instead: {tunnel_url})")
    print()
    print("  Leave this running while you use the site. Ctrl+C to stop the sandbox and tunnel.")
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down…")
        try:
            from pyngrok import ngrok
            ngrok.disconnect(tunnel.public_url)
            ngrok.kill()
        except Exception:
            pass


if __name__ == "__main__":
    main()

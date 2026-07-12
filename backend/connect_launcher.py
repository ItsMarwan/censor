"""
connect_launcher.py — Censor Sandbox (desktop app)
----------------------------------------------------
This is the app the website links to as "Download for Windows (.exe)". It's
a normal windowed GUI (Tkinter, ships with Python — no extra install), styled
to match itsmarwan.github.io/censor/ so the app you open looks like the
mockup the site showed you before you downloaded it.

What it does, in order:
  1. Boots app.py's Flask detector in a background thread on 127.0.0.1:5000.
  2. Either opens a tunnel to it (pyngrok) or stays local-only, per your choice.
  3. Shows a short pairing code (or, in local-only mode, the local address)
     for you to paste into the website's "Pair this tab" box.

Nothing here uploads your files anywhere. The tunnel — when you choose it —
only lets your own browser reach your own machine; images/videos you censor
still never leave it. Closing this window kills the detector and, if one was
open, the tunnel — the pairing code stops working immediately.

Run:
    pip install -r requirements.txt
    python connect_launcher.py

Packaging this into the CensorSandbox-Setup.exe on the website: see README.md.
"""

import base64
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

APP_PORT = 5000
SITE_BASE = "https://itsmarwan.github.io/censor/"
SITE_PAIR_BASE = SITE_BASE + "u/"

# ---------------------------------------------------------------- theme
# Pulled straight from site/styles.css :root so the app and the site match.
BG = "#10131a"
PANEL = "#191d26"
PANEL_2 = "#1f2430"
BORDER = "#262c3a"
TEXT = "#e9ecf2"
MUTED = "#8b93a5"
MUTED_2 = "#5c6478"
ACCENT = "#4bd8c9"
ACCENT_DIM = "#1c3733"
DANGER = "#ef5d6f"
WARN = "#f0a857"
INK_ON_ACCENT = "#0a1512"

FONT_MONO_CANDIDATES = ["JetBrains Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"]
FONT_BODY_CANDIDATES = ["Inter", "Segoe UI", "Helvetica Neue", "Arial"]
FONT_DISPLAY_CANDIDATES = ["Space Grotesk", "Segoe UI Semibold", "Helvetica Neue", "Arial"]


def pick_font(root, candidates, fallback="TkDefaultFont"):
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return fallback


# ---------------------------------------------------------------- rounded panel helper

def round_rect(canvas, x1, y1, x2, y2, r=12, **kwargs):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Panel(tk.Canvas):
    """A rounded-corner card, like .panel in styles.css."""

    def __init__(self, parent, bg_color=PANEL, border_color=BORDER, radius=12, **kw):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0, **kw)
        self._bg_color = bg_color
        self._border_color = border_color
        self._radius = radius
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event=None):
        self.delete("bgshape")
        w = self.winfo_width()
        h = self.winfo_height()
        if w > 2 and h > 2:
            round_rect(
                self, 1, 1, w - 1, h - 1, self._radius,
                fill=self._bg_color, outline=self._border_color, width=1, tags="bgshape",
            )
            self.tag_lower("bgshape")


class PillButton(tk.Canvas):
    """A flat, rounded button matching .btn / .btn-accent / .btn-ghost."""

    def __init__(self, parent, text, command=None, style="ghost", font=None, width=220, height=38, radius=9):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.text = text
        self.style = style
        self.font = font
        self.radius = radius
        self._enabled = True
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw(hover=False))
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()

    def _colors(self, hover):
        if self.style == "accent":
            fill = ACCENT if not hover else "#63e0d3"
            outline = ACCENT
            fg = INK_ON_ACCENT
        else:
            fill = PANEL_2 if not hover else "#2a3140"
            outline = BORDER
            fg = TEXT
        if not self._enabled:
            fill = PANEL_2
            outline = BORDER
            fg = MUTED_2
        return fill, outline, fg

    def _draw(self, hover=False):
        self.delete("all")
        w = int(self["width"])
        h = int(self["height"])
        fill, outline, fg = self._colors(hover and self._enabled)
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill, outline=outline, width=1)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)

    def _on_click(self, event):
        if self._enabled and self.command:
            self.command()


class ConfirmDialog(tk.Toplevel):
    """A themed modal Yes/No dialog. Runs entirely on the main thread; call
    from a background thread via root.after(0, ...) and wait on an Event."""

    def __init__(self, parent, title, body_lines, yes_text, no_text, fonts, on_choice):
        super().__init__(parent, bg=BG)
        self.on_choice = on_choice
        self._answered = False
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(False))

        f_h2, f_body, f_body_sm = fonts

        pad = tk.Frame(self, bg=BG)
        pad.pack(fill="both", expand=True, padx=20, pady=18)

        tk.Label(pad, text=title, bg=BG, fg=TEXT, font=f_h2, wraplength=340, justify="left").pack(anchor="w")
        for line in body_lines:
            tk.Label(pad, text=line, bg=BG, fg=MUTED, font=f_body_sm, wraplength=340,
                     justify="left").pack(anchor="w", pady=(8, 0))

        btn_row = tk.Frame(pad, bg=BG)
        btn_row.pack(fill="x", pady=(18, 0))
        PillButton(btn_row, no_text, command=lambda: self._choose(False), style="ghost",
                   font=f_body, width=150, height=38).pack(side="left")
        PillButton(btn_row, yes_text, command=lambda: self._choose(True), style="accent",
                   font=f_body, width=150, height=38).pack(side="right")

        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

        self.grab_set()
        self.focus_set()

    def _choose(self, value):
        if self._answered:
            return
        self._answered = True
        self.grab_release()
        self.destroy()
        self.on_choice(value)


# ---------------------------------------------------------------- app

class SandboxApp:
    def __init__(self, root):
        self.root = root
        self.tunnel = None
        self.backend_thread = None
        self.mode = tk.StringVar(value="tunnel")
        self.status_text = tk.StringVar(value="not started")

        root.title("Censor Sandbox")
        root.configure(bg=BG)
        root.geometry("460x560")
        root.minsize(420, 520)
        try:
            root.iconphoto(True, self._brand_icon())
        except Exception:
            pass

        self.mono = pick_font(root, FONT_MONO_CANDIDATES)
        self.body = pick_font(root, FONT_BODY_CANDIDATES)
        self.display = pick_font(root, FONT_DISPLAY_CANDIDATES)

        self.f_mono_sm = (self.mono, 10)
        self.f_mono_tag = (self.mono, 9)
        self.f_mono_code = (self.mono, 14, "bold")
        self.f_body = (self.body, 10)
        self.f_body_sm = (self.body, 9)
        self.f_title = (self.display, 15, "bold")
        self.f_h2 = (self.display, 17, "bold")

        self._build_header()

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(fill="both", expand=True, padx=18, pady=(14, 18))

        self._build_idle_screen()
        self._build_ready_screen()
        self.show_idle()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -------------------------------------------------- window icon (block + eye)
    def _brand_icon(self):
        img = tk.PhotoImage(width=32, height=32)
        img.put(BG, to=(0, 0, 32, 32))
        # rounded-ish block
        for y in range(3, 29):
            img.put(PANEL_2, to=(3, y, 29, y + 1))
        for x in range(3, 29):
            img.put(BORDER, to=(x, 3, x + 1, 4))
            img.put(BORDER, to=(x, 28, x + 1, 29))
        # eye: almond outline + pupil, in accent teal
        eye_rows = {
            10: (9, 23), 11: (7, 25), 12: (6, 26), 13: (5, 27),
            14: (5, 27), 15: (5, 27), 16: (5, 27), 17: (6, 26),
            18: (7, 25), 19: (9, 23),
        }
        for y, (x0, x1) in eye_rows.items():
            img.put(ACCENT, to=(x0, y, x1, y + 1))
        for y in range(12, 19):
            img.put(PANEL, to=(x0 + 6, y, x1 - 6, y + 1))
        for y in range(13, 18):
            img.put(ACCENT, to=(14, y, 18, y + 1))
        return img

    # -------------------------------------------------- header
    def _build_header(self):
        header = tk.Frame(self.root, bg=PANEL_2, height=42)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        dot = tk.Canvas(header, width=12, height=12, bg=PANEL_2, highlightthickness=0)
        dot.create_oval(1, 1, 11, 11, fill=ACCENT, outline="")
        dot.pack(side="left", padx=(16, 8))

        tk.Label(header, text="Censor Sandbox", bg=PANEL_2, fg=MUTED, font=self.f_mono_sm).pack(side="left")

        self.status_dot = tk.Canvas(header, width=10, height=10, bg=PANEL_2, highlightthickness=0)
        self.status_dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=MUTED_2, outline="")
        self.status_dot.pack(side="right", padx=(0, 8))
        self.status_label = tk.Label(header, textvariable=self.status_text, bg=PANEL_2, fg=MUTED, font=self.f_mono_tag)
        self.status_label.pack(side="right", padx=(0, 4))

    def _set_status(self, text, live=False):
        self.status_text.set(text)
        self.status_dot.itemconfig(self.status_dot_id, fill=ACCENT if live else MUTED_2)

    # -------------------------------------------------- idle / setup screen
    def _build_idle_screen(self):
        self.idle_frame = tk.Frame(self.container, bg=BG)

        tk.Label(self.idle_frame, text="local-only processing", bg=BG, fg=ACCENT,
                 font=self.f_mono_tag).pack(anchor="w")
        tk.Label(self.idle_frame, text="Start your sandbox", bg=BG, fg=TEXT,
                 font=self.f_h2).pack(anchor="w", pady=(4, 2))
        tk.Label(self.idle_frame, text="Nothing runs or is exposed until you click Start.",
                 bg=BG, fg=MUTED, font=self.f_body_sm, wraplength=400, justify="left").pack(anchor="w", pady=(0, 16))

        mode_panel = Panel(self.idle_frame, height=118)
        mode_panel.pack(fill="x", pady=(0, 14))
        mode_inner = tk.Frame(mode_panel, bg=PANEL)
        mode_panel.create_window(10, 10, anchor="nw", window=mode_inner)

        tk.Label(mode_inner, text="CONNECTION", bg=PANEL, fg=MUTED_2, font=self.f_mono_tag).pack(anchor="w", pady=(2, 8))

        for value, label, sub in [
            ("tunnel", "Tunnel — pair with another device",
             "For a phone, or a laptop that isn't running the site."),
            ("local", "Local only — same machine",
             "Fastest option. Site and sandbox run on this computer."),
        ]:
            row = tk.Frame(mode_inner, bg=PANEL)
            row.pack(anchor="w", fill="x", pady=3)
            rb = tk.Radiobutton(
                row, text=label, variable=self.mode, value=value, bg=PANEL, fg=TEXT,
                selectcolor=PANEL_2, activebackground=PANEL, activeforeground=TEXT,
                font=self.f_body, highlightthickness=0, bd=0, anchor="w",
            )
            rb.pack(anchor="w")
            tk.Label(row, text=sub, bg=PANEL, fg=MUTED_2, font=self.f_body_sm).pack(anchor="w", padx=(24, 0))

        self.start_btn = PillButton(self.idle_frame, "Start sandbox", command=self.on_start, style="accent",
                                     font=self.f_body, width=420, height=42)
        self.start_btn.pack(fill="x")

        self.idle_status = tk.Label(self.idle_frame, text="", bg=BG, fg=MUTED, font=self.f_body_sm,
                                     wraplength=400, justify="left")
        self.idle_status.pack(anchor="w", pady=(12, 0))

        self.progress = Panel(self.idle_frame, height=8, radius=4)
        self.progress_fill_id = None

    def _set_idle_progress(self, frac, text=""):
        self.idle_status.config(text=text)
        if not self.progress.winfo_ismapped():
            self.progress.pack(fill="x", pady=(10, 0))
        self.progress.delete("fill")
        w = self.progress.winfo_width() or 400
        h = self.progress.winfo_height() or 8
        round_rect(self.progress, 1, 1, w - 1, h - 1, 4, fill=PANEL_2, outline=BORDER, tags="fill")
        if frac > 0:
            round_rect(self.progress, 1, 1, max(9, int(w * frac)), h - 1, 4, fill=ACCENT, outline="", tags="fill")

    def show_idle(self):
        self.ready_frame.pack_forget()
        self.idle_frame.pack(fill="both", expand=True)
        self._set_status("not connected")

    # -------------------------------------------------- ready screen (matches the site mockup)
    def _build_ready_screen(self):
        self.ready_frame = tk.Frame(self.container, bg=BG)

        status_panel = Panel(self.ready_frame, height=64)
        status_panel.pack(fill="x", pady=(0, 16))
        status_inner = tk.Frame(status_panel, bg=PANEL_2)
        status_panel.create_window(16, 32, anchor="w", window=status_inner)

        dotrow = tk.Frame(status_inner, bg=PANEL_2)
        dotrow.pack(anchor="w")
        d = tk.Canvas(dotrow, width=12, height=12, bg=PANEL_2, highlightthickness=0)
        d.create_oval(1, 1, 11, 11, fill=ACCENT, outline="")
        d.pack(side="left", padx=(0, 8))
        tk.Label(dotrow, text="Sandbox ready", bg=PANEL_2, fg=TEXT, font=(self.body, 12, "bold")).pack(side="left")
        self.ready_sub = tk.Label(status_inner, text="Detector running on this machine",
                                   bg=PANEL_2, fg=MUTED, font=self.f_body_sm)
        self.ready_sub.pack(anchor="w", pady=(2, 0))

        tk.Label(self.ready_frame, text="YOUR PAIRING CODE", bg=BG, fg=MUTED_2,
                 font=self.f_mono_tag).pack(anchor="w", pady=(0, 6))

        code_panel = Panel(self.ready_frame, height=54)
        code_panel.pack(fill="x", pady=(0, 4))
        self.code_label = tk.Label(code_panel, text="", bg=PANEL, fg=ACCENT, font=self.f_mono_code)
        code_panel.create_window(16, 27, anchor="w", window=self.code_label)

        self.copy_feedback = tk.Label(self.ready_frame, text=" ", bg=BG, fg=ACCENT, font=self.f_body_sm)
        self.copy_feedback.pack(anchor="w", pady=(4, 12))

        btn_row = tk.Frame(self.ready_frame, bg=BG)
        btn_row.pack(fill="x")
        self.copy_btn = PillButton(btn_row, "Copy code", command=self.on_copy_code, style="accent",
                                    font=self.f_body, width=200, height=38)
        self.copy_btn.pack(side="left", padx=(0, 8))
        self.open_btn = PillButton(btn_row, "Open pairing page", command=self.on_open_site, style="ghost",
                                    font=self.f_body, width=200, height=38)
        self.open_btn.pack(side="left")

        # collapsible "advanced" — raw tunnel URL is not shown by default so the
        # UI never leads with a URL people might paste somewhere public.
        self.adv_shown = tk.BooleanVar(value=False)
        self.adv_toggle = tk.Label(self.ready_frame, text="Show raw address ▾", bg=BG, fg=MUTED_2,
                                    font=self.f_body_sm, cursor="hand2")
        self.adv_toggle.pack(anchor="w", pady=(14, 0))
        self.adv_toggle.bind("<Button-1>", self._toggle_advanced)
        self.adv_label = tk.Label(self.ready_frame, text="", bg=BG, fg=MUTED, font=self.f_mono_tag,
                                   wraplength=400, justify="left")

        tk.Frame(self.ready_frame, bg=BG, height=20).pack()
        tk.Label(self.ready_frame, text="Leave this window open while you use the site.",
                 bg=BG, fg=MUTED_2, font=self.f_body_sm).pack(anchor="w")
        tk.Label(self.ready_frame, text="Closing it disconnects your sandbox immediately.",
                 bg=BG, fg=MUTED_2, font=self.f_body_sm).pack(anchor="w")

        self.stop_btn = PillButton(self.ready_frame, "Stop sandbox", command=self._on_close, style="ghost",
                                    font=self.f_body, width=420, height=38)
        self.stop_btn.pack(fill="x", pady=(18, 0))

    def _toggle_advanced(self, event=None):
        shown = not self.adv_shown.get()
        self.adv_shown.set(shown)
        if shown:
            self.adv_toggle.config(text="Hide raw address ▴")
            self.adv_label.pack(anchor="w", pady=(6, 0))
        else:
            self.adv_toggle.config(text="Show raw address ▾")
            self.adv_label.pack_forget()

    def show_ready(self, code_or_addr, raw_url, sub_text):
        self.idle_frame.pack_forget()
        self.ready_frame.pack(fill="both", expand=True)
        self.code_label.config(text=code_or_addr)
        self.ready_sub.config(text=sub_text)
        self.adv_label.config(text=raw_url)
        self._set_status("ready", live=True)

    # -------------------------------------------------- actions
    def on_start(self):
        self.start_btn.set_enabled(False)
        self._set_idle_progress(0.05, "Booting detector…")
        self._set_status("starting…")
        threading.Thread(target=self._start_flow, daemon=True).start()

    def _start_flow(self):
        try:
            import app as backend  # local import: app.py must sit next to this script.
            # This only imports the module — it does NOT construct the
            # NudeDetector yet, so it's safe even if the model is missing.

            self.root.after(0, self._set_idle_progress, 0.03, "Checking for the detection model…")
            model_path = backend.find_local_model_path()

            if not model_path:
                if not self._ask_install_model():
                    self.root.after(0, self._fail, "The detection model wasn't found on this PC.")
                    return
                try:
                    model_path = self._download_model_with_progress(backend)
                except backend.ModelDownloadError as e:
                    self.root.after(0, self._fail, f"The detection model wasn't found on this PC. {e}")
                    return

            try:
                backend.init_detector(model_path)
            except FileNotFoundError:
                self.root.after(0, self._fail, "The detection model wasn't found on this PC.")
                return

            self.root.after(0, self._set_idle_progress, 0.35, "Booting detector…")
            self._start_backend()
            for i in range(1, 4):
                time.sleep(0.2)
                self.root.after(0, self._set_idle_progress, 0.35 + i * 0.08, "Booting detector…")

            ok = self._wait_for_health()
            if not ok:
                self.root.after(0, self._fail, "Detector didn't come up. Check that nudenet, opencv, and ffmpeg are installed (see requirements.txt).")
                return

            if self.mode.get() == "local":
                self.root.after(0, self._set_idle_progress, 1.0, "Ready — local only, no tunnel opened.")
                self.root.after(0, self.show_ready, f"127.0.0.1:{APP_PORT}",
                                 f"http://127.0.0.1:{APP_PORT}",
                                 "Detector running on this machine (local only)")
                return

            self.root.after(0, self._set_idle_progress, 0.75, "Opening tunnel…")
            tunnel_url = self._open_tunnel()
            code = self._make_pairing_code(tunnel_url)
            self.root.after(0, self._set_idle_progress, 1.0, "Ready — paired via tunnel.")
            self.root.after(0, self.show_ready, code, tunnel_url, "Detector running on this machine")
        except Exception as e:
            self.root.after(0, self._fail, f"Couldn't start the sandbox: {e}")

    def _ask_install_model(self):
        """Blocks the calling (background) thread until the user answers the
        Yes/No dialog, shown on the main thread. Returns True for Yes."""
        event = threading.Event()
        result = {"install": False}

        def show():
            self._set_idle_progress(0, "")
            self.idle_status.config(text="Detection model not found on this PC — waiting for you to choose…", fg=MUTED)

            def choice(value):
                result["install"] = value
                event.set()

            ConfirmDialog(
                self.root,
                title="Install detection model?",
                body_lines=[
                    "The sandbox couldn't find the NudeNet detection model "
                    "(320n.onnx, ~12 MB) on this PC. This happens if you "
                    "downloaded just the app without the full install.",
                    "Download it now from the official NudeNet repository? "
                    "It's saved locally and only needs to happen once.",
                ],
                yes_text="Yes, install",
                no_text="No, don't",
                fonts=(self.f_h2, self.f_body, self.f_body_sm),
                on_choice=choice,
            )

        self.root.after(0, show)
        event.wait()
        return result["install"]

    def _download_model_with_progress(self, backend):
        def cb(frac):
            self.root.after(0, self._set_idle_progress, 0.05 + frac * 0.25,
                             f"Downloading detection model… {int(frac * 100)}%")

        self.root.after(0, self._set_idle_progress, 0.05, "Downloading detection model…")
        return backend.download_model(progress_cb=cb)

    def _fail(self, message):
        self.start_btn.set_enabled(True)
        self._set_status("error")
        self._set_idle_progress(0, "")
        self.idle_status.config(text=message, fg=DANGER)

    def _start_backend(self):
        import app as backend  # local import: app.py must sit next to this script
        t = threading.Thread(
            target=lambda: backend.app.run(host="127.0.0.1", port=APP_PORT, debug=False,
                                            threaded=True, use_reloader=False),
            daemon=True,
        )
        t.start()
        self.backend_thread = t

    def _wait_for_health(self, timeout=25):
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

    def _open_tunnel(self):
        from pyngrok import ngrok, conf
        authtoken = os.environ.get("NGROK_AUTHTOKEN")
        if authtoken:
            conf.get_default().auth_token = authtoken
        self.tunnel = ngrok.connect(APP_PORT, "http")
        return self.tunnel.public_url.replace("http://", "https://")

    @staticmethod
    def _make_pairing_code(tunnel_url):
        return base64.urlsafe_b64encode(tunnel_url.encode()).decode().rstrip("=")

    def on_copy_code(self):
        code = self.code_label.cget("text")
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.copy_feedback.config(text="Copied to clipboard.")
        self.root.after(1800, lambda: self.copy_feedback.config(text=" "))

    def on_open_site(self):
        code = self.code_label.cget("text")
        if self.mode.get() == "local":
            webbrowser.open(SITE_BASE)
        else:
            webbrowser.open(SITE_PAIR_BASE + code)

    def _on_close(self):
        try:
            if self.tunnel is not None:
                from pyngrok import ngrok
                ngrok.disconnect(self.tunnel.public_url)
                ngrok.kill()
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)


def main():
    root = tk.Tk()
    SandboxApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

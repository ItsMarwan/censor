# Censor toolkit

Two pieces:

- **`site/`** — static frontend (deploy as-is to GitHub Pages, e.g. as the
  `itsmarwan.github.io/censor` repo). Has a navbar (Home / Docs /
  Integration) and never contains a hardcoded server address — it's locked
  behind a "pair with your sandbox" gate until you give it a connection
  code from the launcher.
- **`backend/`** — `app.py` (the NudeNet detector, same detection logic as
  before) plus `connect_launcher.py`, the script people run locally. It
  shows a small terminal HUD, asks for confirmation, boots the detector,
  opens a tunnel with `pyngrok`, and prints a pairing code.

## How the "masked URL" actually works

`connect_launcher.py` base64url-encodes its real tunnel address (e.g.
`https://8f2a91cd.ngrok-free.app`) into a short code, and prints it as
`https://itsmarwan.github.io/censor/u/<code>`. That path only *looks* like
it points at a route your GitHub Pages site serves — there's no backend
behind `itsmarwan.github.io` doing a lookup. `site/404.html` catches the
deep link (GitHub Pages has no server-side router), bounces to
`index.html`, and `app.js` decodes the code back into the real tunnel URL
entirely in the visitor's own browser, then uses it for every `fetch()`
call instead of a hardcoded IP.

That means:
- It's a **cosmetic/obfuscation layer**, not a hidden relay — I can't stand
  up a real always-on relay service for you as part of this, since that
  would mean a server under my control proxying your uploads.
- The code is only as private as who you give it to. Treat it like a
  screen-share link: anyone with it can reach your sandbox for as long as
  the launcher is running.
- Closing `connect_launcher.py` kills the tunnel and the code stops
  resolving to anything, immediately.

If you'd rather have a real persistent relay (so the code doesn't die with
your laptop), that's a different project — a small always-on proxy you
deploy yourself (e.g. a $5 VPS forwarding to whichever sandbox is currently
registered) — happy to help scope that separately if useful.

## Local mode

`site/config.json` controls what the connect gate asks for:

```json
{ "local": false, "local_default_host": "127.0.0.1:5000" }
```

- `"local": false` (default) — the masked-code flow above: paste the code
  `connect_launcher.py` prints, meant for a sandbox reached through a tunnel
  (e.g. someone else's machine, or a phone connecting to a laptop).
- `"local": true` — skips masking entirely. The gate just asks for an
  address like `127.0.0.1:5000`, for when the browser and the sandbox are
  on the same machine (or same LAN) and there's nothing to hide behind a
  code. Set this if you're deploying the site for people who'll run
  everything locally.

`connect_launcher.py` matches this: when you start it, it asks whether to
open a tunnel or run local-only. Local-only skips `pyngrok` entirely and
just prints `127.0.0.1:5000` for you to enter — make sure `config.json` has
`"local": true` in that case, or the gate will try to treat it as a masked
code and fail to parse it.

## Running it

```bash
cd backend
pip install -r requirements.txt
python connect_launcher.py
```

Then open the site (locally: just open `site/index.html`; deployed: your
GitHub Pages URL) and paste the code the launcher prints.

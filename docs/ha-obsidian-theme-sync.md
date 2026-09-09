# Obsidian Add-on Theme Synchronization with Home Assistant

This guide describes how the add-on automatically matches the Obsidian theme
(`obsidian` for dark, `moonstone` for light) to the active Home Assistant
theme whenever the web UI is opened via **Open Web UI** or the side panel.

This document supersedes the first draft of the plan. Section 2 records the
defects found in that draft; the rest is the corrected, implemented design.

---

## 1. Architecture

```text
Browser (Selkies dashboard page)
  │  theme-sync.js (injected into the dashboard source HTML)
  │  1. reads HA theme vars from window.parent (same-origin via ingress)
  │  2. falls back to prefers-color-scheme (standalone tab)
  │  3. classifies by WCAG relative luminance (any HA theme, not just stock)
  ▼
POST <page-origin><page-path>/api/set-theme?theme=obsidian|moonstone
  ▼
nginx (port 3000/3001, location /api/set-theme)
  ▼
theme_server.py  (s6 service, loopback 127.0.0.1:8090, runs as abc)
  ▼
<vault>/.obsidian/appearance.json   (atomic write, keys preserved, abc-owned)
  ▼
obsidian-cli reload  (CLI client, best-effort, only when the value changed)
  ▼
Obsidian applies the theme live, without restarting the add-on
```

Key properties:

- **Detection is luminance-based.** The client parses the resolved value of
  `--clear-background-color` (fallback `--primary-background-color`) and
  compares WCAG relative luminance against 0.5. This works for the stock HA
  themes *and* custom community themes, where string-matching colors fails.
- **The endpoint is derived from the page location.** Under HA ingress the
  page lives at `https://<ha>/api/ingress/<token>`, so a hardcoded absolute
  path would hit HA core, not the add-on. Deriving the endpoint from
  `window.location` is correct for both ingress and standalone access.
- **All base-image edits are made at build time against the regeneration
  sources** (see Section 2.3), so they survive the boot-time regeneration.
- **The vault is resolved per request**: `THEME_SYNC_VAULT` override → most
  recent vault in Obsidian's global config → `/config`.
- **Newly created vaults inherit the last requested theme.** First-run
  installs start with no vault, so the initial sync writes the default
  location; a vault the user creates later starts with Obsidian's default
  appearance and would otherwise stay out of sync. The daemon records the
  last requested theme and runs a background watcher over Obsidian's vault
  registry: a vault registered after the last request gets the remembered
  theme applied and the app reloaded — no browser request needed. Vault
  paths that existed before the watcher started are never overwritten, so a
  user-chosen custom theme is left untouched.
- **The running app is reloaded through the Obsidian CLI.** At boot, the
  `init-obsidian-cli` s6-rc oneshot merges `"cli": true` into the global
  config (the key the app gates every CLI command on). After an actual
  theme change the daemon runs `/opt/obsidian/obsidian-cli reload` best-effort; a failed
  reload never fails the API — the file is correct and the theme applies on
  the next app start.

---

## 2. Defects found in the first draft

Verified against the actual base image
(`lscr.io/linuxserver/obsidian:v1.12.7-ls132`):

1. **`apk add python3` — wrong and unnecessary.** The image is Debian 13
   (trixie), not Alpine; `apk` does not exist, so the build fails. Python
   3.13 is already installed at `/usr/bin/python3`.
2. **Wrong web stack.** The draft targeted `/usr/share/kasmvnc/www/index.html`;
   that path does not exist. The UI is **Selkies**, served by nginx from
   `/usr/share/selkies/web/`.
3. **Transient nginx edits.** `init-nginx` re-copies the template
   `/defaults/default.conf` over the live config and re-copies the dashboard
   source at *every boot*. The draft's runtime `sed` of
   `/etc/nginx/sites-enabled/default` therefore only survives until the next
   restart. The durable targets are the template and the dashboard *source*.
4. **Broken theme detection.** `clearBg === "#111"` never matches HA's
   `#111111`; `rgb(17, 17, 17)` is not the form `getComputedStyle` returns
   for a custom property; `primaryBg.includes("surface-container")` is dead
   code — a color value never contains a token name. Detection failed even
   for the default theme and for every custom theme.
5. **Absolute fetch path breaks the ingress case** (see Section 1).
6. **"Applies without restart" was assumed, not verified.** The daemon now
   makes the write robust (atomic + owned); live application is an explicit
   verification step with a documented fallback.
7. **Ownership/atomicity gaps.** A root-owned, in-place-rewritten
   `appearance.json` risks both permission errors for the `abc` user and a
   torn read by Obsidian. Fixed by running as `abc`, atomic `os.replace`,
   and best-effort `PUID`/`PGID` chown.
8. **Always-200 API.** Invalid themes now return 400; unknown paths 404.
9. **No tests, no ADR** (required by project policy). Both added:
   `tests/test_theme_server.py` and
   `obsidian/adr/0001-obsidian-ha-theme-sync.md`.
10. **`FROM ...:latest`** would break the repo's digest pinning, Renovate and
    the auto-locker CI. The existing pinned base is kept and extended.

---

## 3. Verified facts about the base image

| Fact | Evidence |
|---|---|
| Debian 13 (trixie), not Alpine | `/etc/os-release` in the image |
| Python 3.13.5 at `/usr/bin/python3` | `command -v python3` in the image |
| Web UI is Selkies, served by nginx on 3000 (HTTP) / 3001 (HTTPS) | `/defaults/default.conf`, `svc-selkies/run` |
| Runtime web dir `/usr/share/selkies/web/` is re-copied from `/usr/share/selkies/selkies-dashboard/` every boot | `init-nginx/run` |
| Runtime nginx config is re-copied from `/defaults/default.conf` every boot | `init-nginx/run` |
| s6-overlay entrypoint (`/init`); `/etc/services.d` is the standard services layer; LSIO services use `s6-setuidgid abc` | image entrypoint, `svc-selkies/run` |
| Obsidian autostarted bare (`obsidian`); vault is user-provided; global config at `/config/.config/obsidian/obsidian.json` (HOME=/config) | `/defaults/autostart`, `id abc` |
| HA ingress strips `/api/ingress/<token>` and forwards the rest unchanged (no `/api` re-added) | Supervisor `api/ingress.py` |

---

## 4. File layout

```text
obsidian/
├── Dockerfile                              # merged: pinned base + theme-sync layer
├── config.yaml                             # unchanged (ingress, ports, maps)
├── adr/
│   └── 0001-obsidian-ha-theme-sync.md      # architecture decision record
├── ha-theme-sync/
│   ├── theme_server.py                     # daemon (stdlib only)
│   ├── theme-sync.js                       # client detection
│   └── test_theme_server.py                # pytest suite (Given-When-Then)
└── root/
    ├── etc/services.d/theme-api/run        # s6 longrun service
    └── etc/s6-overlay/s6-rc.d/
        ├── init-obsidian-cli/              # boot-time oneshot: CLI enablement
        │   ├── type                        # oneshot
        │   ├── run                         # jq merge script
        │   ├── up
        │   └── dependencies.d/init-obsidian-config
        └── user/contents.d/init-obsidian-cli
```

---

## 5. Step-by-step breakdown

### Step 1 — Theme daemon (`ha-theme-sync/theme_server.py`)

**Goal.** A loopback HTTP service that writes the requested theme into the
active vault, safely.

- `POST /api/set-theme?theme=obsidian|moonstone` → 200
  `{"status":"ok","changed":bool,"reloaded":bool}`; 400 for missing/unknown
  theme; 404 for other paths.
- Pure, unit-testable functions:
  - `parse_color()` — `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb()`, `rgba()`;
    rejects out-of-range and malformed values (false-positive guard).
  - `relative_luminance()` / `is_dark_color()` — WCAG luminance, threshold
    0.5 (boundary behavior tested at the 0.5 luminance edge).
  - `resolve_vault()` — `THEME_SYNC_VAULT` → newest `ts` in
    `<home>/.config/obsidian/obsidian.json` → `/config`; corrupt, missing or
    malformed configs all fall through to the default.
  - `resolve_owner()` — `PUID`/`PGID`, then the `abc` account.
  - `apply_theme()` — validates against the allowlist, preserves unrelated
    keys, writes via temp file + `os.replace` (atomic), chowns best-effort,
    no temp files left behind, corrupt/foreign existing files are replaced.
  - `reload_obsidian()` — runs `/opt/obsidian/obsidian-cli reload` via
    `subprocess.run(check=True, timeout=15)` with explicit
    `HOME`/`XDG_RUNTIME_DIR`; never raises, returns `False` when the CLI or
    the app is unavailable.
  - `handle_set_theme()` — orchestrates vault/owner resolution,
    `apply_theme`, persists the requested theme for the watcher (even for a
    no-op request, so the remembered theme is always the last one asked
    for), then `reload_obsidian` only when the value actually changed;
    returns `{"changed": bool, "reloaded": bool}`.
  - `store_last_theme()` / `load_last_theme()` — persist the last requested
    theme at `<home>/.config/ha-theme-sync.json`; storing is best-effort
    (a failure only leaves the watcher with nothing to apply), loading
    returns `None` for a missing, corrupt or disallowed value.
  - `registered_vaults()` — the existing vault paths recorded in Obsidian's
    global config; non-dict configs, non-dict entries and non-existent
    paths are skipped.
  - `InotifyWaiter` / `create_config_waiter()` — an inotify(7) waiter on
    the config directory, built with `ctypes` (no dependencies). `wait()`
    blocks in the kernel until a create/delete/modify/move event arrives,
    drains it, and returns a boolean; a dead watch (`IN_DELETE_SELF` /
    `IN_IGNORED`) is reported through `is_alive`. Watching the directory —
    not the file — survives Electron's replace-the-file config rewrites
    (new inode → `IN_CREATE`/`IN_MOVED_TO`; in-place write → `IN_MODIFY`).
  - `VaultWatcher` / `run_vault_watcher()` — a daemon thread that keeps the
    vault registry in sync. On Linux it is event-driven: it blocks in
    inotify until the config directory changes, so an idle watcher costs
    no CPU and reacts within one syscall of the write; a safety rescan
    every 60 s guards against lost events. Without inotify, or before the
    config directory exists (first boot), it polls every 2 s and retries
    the inotify watch each cycle. The first step only records a baseline,
    so vaults that existed before the watcher started are never
    overwritten; a newly registered vault gets the remembered theme written
    (allowlist enforced by `apply_theme`) and, only when something was
    written, a best-effort `obsidian-cli reload`. A step that raises never
    stops the loop.
- Runs as a stdlib-only script (`http.server.ThreadingHTTPServer`), with
  the watcher thread started from `main()`.

**Verify.** `python3 -m pytest tests/ -v` — all green.

### Step 2 — Client detection (`ha-theme-sync/theme-sync.js`)

**Goal.** Determine the HA theme in the browser and request it.

- Same luminance logic as the daemon (mirrored in JS), reading
  `--clear-background-color` / `--primary-background-color` from
  `window.parent`; cross-origin failures degrade to `prefers-color-scheme`.
- Endpoint built from `window.location` (ingress-safe, see Section 1).
- Runs on load, then event-driven (no periodic polling): the
  `prefers-color-scheme` change event covers the fallback path, and a
  `MutationObserver` on the parent document covers HA runtime theme
  switches — every mechanism that changes those CSS variables (`theme`/
  class attributes on `<html>`, inline custom properties, stylesheet
  swaps) surfaces as a DOM mutation. The `lastSent` guard suppresses
  duplicate sends when the resolved theme is unchanged.
- A failed send is retried a bounded number of times (3 attempts, 2 s
  apart, re-reading the current theme), so a transient error at the moment
  of a switch cannot lose the sync; 404 responses are not retried.
- `tests/theme-sync.test.js` — 18 Given-When-Then tests under
  `node --test` (zero dependencies) that load the real script into a fake
  browser: spec-faithful `MutationObserver` delivery, `matchMedia` change
  events, recorded `fetch`/timers.

**Verify.** `node --test "tests/*.test.js"` all green;
served file check (Step 5) + end-to-end smoke test (Section 6).

### Step 3 — s6 service (`root/etc/services.d/theme-api/run`)

**Goal.** Run the daemon for the container lifetime.

```sh
#!/usr/bin/with-contenv bash
exec s6-setuidgid abc python3 /opt/ha-theme-sync/theme_server.py
```

Running as `abc` means the files the daemon creates are owned by the add-on
user from the start (matching LSIO's own service pattern).

**Verify.** After boot, the process is visible in the s6 service list and
`ss -ltn` shows `127.0.0.1:8090`.

### Step 4 — Build-time web hooks (Dockerfile)

**Goal.** Make the web UI reach the daemon, durably.

- Copy `theme_server.py` + `theme-sync.js` to `/opt/ha-theme-sync/`, the
  longrun service to `/etc/services.d/theme-api/`, and `chmod +x` the run
  script.
- Install the `init-obsidian-cli` s6-rc oneshot under
  `/etc/s6-overlay/s6-rc.d/` (type `oneshot`, depending on the base image's
  `init-obsidian-config`) and add it to the `user` bundle, so `"cli": true`
  is merged into the global config at every boot before the app first reads
  it (idempotent: preserves user keys, respects an explicit
  `"cli": false`, replaces corrupt or non-object files).
- Insert into the nginx **template** `/defaults/default.conf` (both server
  blocks, anchored on `location SUBFOLDER {`):
  ```nginx
  location /api/set-theme { proxy_pass http://127.0.0.1:8090; }
  location /theme-sync.js { alias /opt/ha-theme-sync/theme-sync.js; }
  ```
- Insert `<script src="/theme-sync.js"></script>` before `</head>` in the
  dashboard **source** `/usr/share/selkies/selkies-dashboard/index.html`
  (idempotency-guarded).
- Grep assertions fail the build if the expected anchors are missing, so a
  future base-image bump cannot silently drop the feature.
- The existing pinned base and `mkdir /share` steps are kept untouched, and
  the base image's `init-obsidian-config` service is not modified — the CLI
  enablement lives in our own oneshot service, so a failure there can no
  longer mark the base image's service as failed.

**Verify.** `docker build` succeeds; after boot, the live config contains
both locations (Section 6).

### Step 5 — ADR + tests

- `obsidian/adr/0001-obsidian-ha-theme-sync.md` records the decision, the
  rejected alternatives (runtime sed, `apk`, KasmVNC injection, string-based
  detection) and the consequences.
- `tests/test_theme_server.py` — 136 Given-When-Then tests covering
  parsing edge cases, threshold boundaries, vault resolution fall-throughs,
  atomicity, ownership, CLI reload success/failure paths, CLI enablement
  merges, API status codes and idempotency, the remembered-theme state
  (round-trip, allowlist, corruption, swallowed failures) and the vault
  watcher (baseline, new-vault sync, re-creation after removal, corrupt
  config, no-op registries, multi-vault reload coalescing, loop survival).
- `tests/theme-sync.test.js` — the client's event-driven behavior
  (scheme change, parent mutations, guard, bounded retries, no polling)
  under `node --test` with a fake browser.

**Verify.** `python3 -m pytest tests/ -v` all green;
`node --test "tests/*.test.js"` all green; `shellcheck`
on the run script.

---

## 6. Verification & troubleshooting (end-to-end)

1. **Build** the image (CI or `docker build -f obsidian/Dockerfile obsidian`).
2. **Boot** the container with a `/config` volume; confirm:
   - s6 shows `theme-api` up; daemon log line
     `[ha-theme-sync] listening on 127.0.0.1:8090`;
   - `nginx -t` passes at runtime (certs exist by then);
   - `GET /theme-sync.js` → 200, JS body;
   - dashboard `GET /` HTML contains `<script src="/theme-sync.js">`.
3. **API:**
   - `POST /api/set-theme?theme=moonstone` → 200, `changed:true`;
   - again → 200, `changed:false`;
   - `?theme=purple` → 400; no theme → 400; other path → 404.
4. **Vault file:** `cat <vault>/.obsidian/appearance.json` shows
   `"theme": "moonstone"`, owned by the add-on user, other keys intact.
5. **Live apply:** switch the HA theme and confirm the running Obsidian
   follows *immediately* (event-driven, no poll wait) *without* restarting
   the add-on — the API response shows `"reloaded": true` when
   `obsidian-cli reload` succeeded. If
   `"reloaded"` is `false` (CLI unavailable), restart the app once — the
   file is already correct, so the theme applies on next start.
6. **Failure modes:**
   - *404 from HA core on the POST* → the page is not under ingress and the
     endpoint derivation regressed; check `theme-sync.js` endpoint logic.
   - *Permission errors on appearance.json* → the daemon is not running as
     `abc`; check the s6 run script.
   - *Feature disappears after a base-image bump* → the build-time grep
     assertions should have failed the build; inspect the Dockerfile layer.
   - *`"reloaded": false` in the API response* → the CLI gate is closed or
     the app is not running: check `"cli": true` in
     `/config/.config/obsidian/obsidian.json` and run
     `s6-setuidgid abc /opt/obsidian/obsidian-cli help` to see the CLI error text.
   - *A newly created vault stays at the default theme* → the daemon
     watcher applies the remembered theme to vaults registered after the
     last request — immediately on Linux (inotify event on the config
     directory), or within a couple of seconds on the polling fallback.
     Check that
     `/config/.config/ha-theme-sync.json` exists (it is only written after
     the first `/api/set-theme` request — until then there is nothing to
     apply), that the vault is listed in
     `/config/.config/obsidian/obsidian.json`, and that `theme-api` is up.
     Vault paths that already existed when the daemon started — including
     ones created before a daemon restart — are left as-is by design,
     including user-chosen custom themes.

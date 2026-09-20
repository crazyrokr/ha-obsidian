# ADR 0001: Obsidian theme synchronization with Home Assistant

- Status: accepted
- Date: 2026-09-06
- Last updated: 2026-09-20
- Deciders: maintainers

## Context

Users expect the Obsidian add-on to follow the Home Assistant theme: dark HA
theme means the Obsidian `obsidian` (dark) theme, light means `moonstone`.
The HA theme is a purely client-side concern (CSS variables in the HA
frontend); the HA backend has no API for it, so the container cannot discover
it server-side. The add-on is served to HA through ingress
(`config.yaml: ingress: true`), which means the add-on web UI loads in a
same-origin iframe of the HA window, making the HA CSS variables readable.

The base image (`lscr.io/linuxserver/obsidian`, verified at
`v1.12.7-ls132`) is Debian trixie, runs s6-overlay (`/init`), serves its
Selkies web UI from nginx on ports 3000/3001, and regenerates its web stack
at every boot:

- `init-nginx` copies `/defaults/default.conf` over
  `/etc/nginx/sites-available/default` and re-copies
  `/usr/share/selkies/selkies-dashboard/` to `/usr/share/selkies/web/`.
- Services in `/etc/services.d` are started by s6-overlay as longrun
  services; LSIO's own services live in `/etc/s6-overlay/s6-rc.d/`.
- Python 3.13 is already present at `/usr/bin/python3`; the web client is
  Selkies, not KasmVNC.

## Decision

A small loopback HTTP daemon writes the theme into the active vault's
`.obsidian/appearance.json`; a client script injected into the web UI
detects the HA theme and posts it to the daemon through nginx.

1. **Detection is luminance-based, never string-matching.** The client reads
   `--clear-background-color` (fallback `--primary-background-color`) from
   the parent window, parses the color, and compares WCAG relative
   luminance against 0.5. This works for the stock HA themes and any custom
   theme. When the parent window is not accessible (standalone tab), the
   browser `prefers-color-scheme` is used. Detection runs on load and is
   then event-driven: the `prefers-color-scheme` change event covers the
   fallback, and a `MutationObserver` on the parent document covers HA
   theme switches — every way those CSS variables change (an attribute on
   `<html>`, an inline custom property, a class toggle, a stylesheet
   swap) surfaces as a DOM mutation, so no polling is needed. A failed
   send is retried a bounded number of times (3 attempts, 2 s apart)
   instead of being waited out on the next poll.
2. **The endpoint is derived from the page location, not hardcoded.**
   Under ingress the page URL is `https://<ha>/api/ingress/<token>...`, so a
   hardcoded `/api/set-theme` would hit HA core instead of the add-on. The
   client posts to `<origin><pathname>/api/set-theme`, which is correct for
   both ingress and standalone access. Same-origin in both cases, so no CORS
   is needed.
3. **All base-image edits happen at build time, against the regeneration
   sources.** The nginx `location` blocks are inserted into the template
   `/defaults/default.conf` and the `<script>` tag into the *source*
   dashboard `/usr/share/selkies/selkies-dashboard/index.html`, so both
   survive the boot-time regeneration. No runtime `sed` of regenerated files,
   no `/custom-cont-init.d` ordering dependency.
4. **The daemon resolves the vault per request.** Priority: `THEME_SYNC_VAULT`
   environment override, then the most recent vault recorded in Obsidian's
   global config (`<home>/.config/obsidian/obsidian.json`), then `/config`.
   The theme file is per-vault (`<vault>/.obsidian/appearance.json`) and the
   vault is user-provided at runtime, so it must not be assumed.
5. **Writes are atomic and ownership-safe.** The new value is written to a
   temp file in the same directory and renamed over
   `appearance.json` (`os.replace`), preserving unrelated keys. The daemon
   runs as `abc` (s6 `s6-setuidgid`) and additionally chowns best-effort to
   `PUID`/`PGID`, so the app user always owns the file.
6. **Strict API semantics.** `POST /api/set-theme?theme=obsidian|moonstone`
   returns 200 with `{"status":"ok","changed":bool,"reloaded":bool}`, 400
   for a missing or unknown theme, 404 for other paths.
7. **Live application uses the Obsidian system CLI, not a restart.** A
   dedicated s6-rc oneshot service (`init-obsidian-cli`, declared under
   `/etc/s6-overlay/s6-rc.d/` and activated by the `user` bundle)
   idempotently merges `"cli": true` into the global config
   `<home>/.config/obsidian/obsidian.json` — the key the app gates every
   CLI command on — preserving user keys and an explicit `false`, and
   replacing corrupt or non-object files with the defaults (atomic
   replace, re-owned for the `abc` user). It depends on the base image's
   `init-obsidian-config`, so it runs after `/config` is chowned. After a
   successful theme *change* (not a no-op), the daemon invokes the CLI
   client binary `/opt/obsidian/obsidian-cli reload` best-effort (15 s
   timeout) with `HOME=/config` and `XDG_RUNTIME_DIR=/config/.XDG` so the
   client finds the app socket — never the `obsidian` on PATH, which in
   this image is the app launcher and would start a second instance.
   A failed reload is reported as `reloaded: false` but never fails the
   request: the file is already correct and the theme applies on the next
   app start.
8. **Newly created vaults inherit the last requested theme.** On a
   first-run install there is no vault, so the initial sync writes the
   default location (`/config`); a vault the user later creates at another
   path starts with Obsidian's default appearance and, if the HA theme never
   changes again, stays out of sync forever. The daemon therefore persists
   the last requested theme (allowlist-enforced, best-effort) at
   `<home>/.config/ha-theme-sync.json` and runs a background watcher thread
   for Obsidian's vault registry
   (`<home>/.config/obsidian/obsidian.json`). The watcher is event-driven
   on Linux: it watches the config *directory* with inotify (through
   `ctypes`, so the zero-dependency constraint holds) and blocks in the
   kernel until a create/delete/modify/move event arrives, so an idle
   watcher costs no CPU and reacts within one syscall of Obsidian's write.
   Watching the directory — rather than the file — is what makes inotify
   viable here: Electron rewrites the config either by replacing it with a
   new inode (temp file plus rename, surfacing as `IN_CREATE`/`IN_MOVED_TO`)
   or in place (`IN_MODIFY`), and a watch on the file would die on the
   former. A safety rescan every 60 s covers events that were never
   delivered, and when inotify is unavailable — or the directory does not
   exist yet (first boot, before Obsidian has run) — the watcher degrades to
   polling every 2 s and retries the inotify watch each cycle, so a config
   directory that appears later is picked up without a restart. A vault path
   that appears in the registry after the watcher's first step gets the
   remembered theme written (reusing `apply_theme`, so the same atomic
   write, allowlist and ownership rules apply) and the app is reloaded once
   if anything was written. The watcher's first step only records a
   baseline — vault paths that already exist when it starts (including ones
   created before a daemon restart) are never overwritten, so a
   user-chosen custom theme is preserved. Rejected alternative: applying
   the theme to *all* registered vaults on every request — that would
   clobber a custom theme in an existing vault and still not fix the
   reported scenario.
9. **The desktop background follows the theme.** The black background
   behind the streamed desktop is painted in the theme's color. The client
   already reads the HA background variables, so it also posts the exact
   color (`&color=<#rrggbb>`, the same first parseable candidate the theme
   decision uses) — the desktop mirrors the real HA background rather than
   a guess. The daemon validates the color's darkness against the theme
   (a light color for the dark theme is a contract violation, rejected
   with 400 — the desktop must never go light under a dark theme), falls
   back to a per-theme default (`#000000` dark, `#f2f4f9` light) when the
   color is missing, remembers the last color in the existing state file,
   and restores it at boot. The base image ships two desktop stacks,
   selected by `PIXELFLUX_WAYLAND`, and both are supported: in X mode
   (the shipped configuration) `xsetroot -solid` paints Xvfb's root window
   and is re-applied on a 30 s cadence; in Wayland mode a `swaybg` process
   runs as a client of the *nested* labwc compositor and is respawned
   whenever it dies. Nested-display discovery is done through the
   `wayland-<n>.lock` files: every Wayland server keeps its lock open, so
   the lock's inode names its owner — and only a labwc-owned display
   renders a background client (Selkies owns the streamed root display,
   where a background would be hidden behind the nested window). A
   supervisor thread re-establishes the background until the desktop is up
   and afterwards whenever it is missing, so the color survives desktop
   restarts. All background failures are swallowed: the background is a
   cosmetic concern and must never break a theme request. `swaybg` is
   installed into the image; `xsetroot` already ships with the base image
   via `x11-xserver-utils`. Rejected alternative: a fixed per-theme
   background without the exact color — the color is already read
   client-side, so posting it costs nothing and matches the requested
   behavior.

## Consequences

- The feature depends on the Selkies dashboard source and the nginx template
  of the pinned base image; the build fails loudly if their expected anchors
  disappear (grep assertions in the Dockerfile), so base-image bumps are
  validated in CI before release.
- A theme change is applied to a running app through `obsidian-cli reload`.
  If the CLI is unavailable (app not started yet, or a base image whose
  client does not accept the command), the daemon degrades to write-only and the
  theme applies on the next app start; the API reports which path was taken
  via `reloaded`.
- Enabling the CLI requires the app to read `"cli": true` from its global
  config *before* it starts, so the merge runs as a boot-time s6-rc oneshot
  in the `user` bundle, not at first request. The oneshot is a separate
  service, so a failure there cannot mark the base image's
  `init-obsidian-config` service as failed, and boot continues regardless
  (the previous config stays intact thanks to the atomic replace).
- The daemon spawns one short-lived subprocess (`obsidian-cli reload`) per
  actual theme change — never per request with an unchanged value.
- The daemon also runs one long-lived background thread (the vault
  watcher). In event mode it blocks in a kernel inotify wait, so an idle
  watcher costs no CPU; it wakes to rescan a small JSON file on each config
  change, plus a safety rescan every 60 s. Without inotify it polls the
  file every 2 s instead. A step that raises is swallowed, so the watcher
  can never crash the daemon, and it only writes to vault paths registered
  after the daemon's first observation, so it cannot clobber user settings
  in existing vaults.
- The daemon runs a second long-lived background thread that keeps the
  desktop background in the requested color. It wakes every 2 s; in X mode
  the work is a `xsetroot -solid` re-apply every 30 s, in Wayland mode a
  cheap `poll()` of the `swaybg` client plus a `/proc` comm scan when the
  desktop comes up. An idle supervisor is near-free, and a failing step is
  swallowed, so it can never crash the daemon or fail a theme request.
- The state file may now carry a `background` key; older state files
  without it are read as theme-only, and the daemon falls back to the
  per-theme default until a request carries a color again.
- The image carries one extra package (`swaybg`); the X-mode painting uses
  `xsetroot`, already present via `x11-xserver-utils`.
- The remembered-theme state (`<home>/.config/ha-theme-sync.json`) is
  best-effort: a write failure leaves the watcher with nothing to apply
  until the next request, and it never fails the request itself, whose
  theme write already succeeded.
- `/api/set-theme` is reachable by anyone who can open the add-on web UI.
  It only accepts two whitelisted values and writes one key to the vault
  config, so the impact surface is negligible.
- The `selkies-dashboard-wish` dashboard variant is not instrumented; the
  add-on uses the default `selkies-dashboard`.

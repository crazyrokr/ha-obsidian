# Obsidian Add-on: Links Open in Your Browser

This guide describes how the add-on forwards external `http(s)` links
clicked in the streamed Obsidian app to the browser that is viewing the
add-on, instead of opening them inside the container's desktop.

## 1. Behavior

- Clicking an `http(s)` link in Obsidian opens it in the viewing browser:
  a new tab when the browser allows the script-initiated open, or a compact
  **Open link in your browser?** toast whose **Open** button always works.
- A single `obsidian://` deep link (for example the Importer plugin's
  `obsidian://show-plugin?id=obsidian-importer`) opens in the streamed
  Obsidian app itself.
- Local attachments and every other `xdg-open` argument keep the previous
  behavior and open in the in-container desktop.
- Works through Home Assistant ingress (the side panel / Web UI iframe) and
  through the exposed HTTP (3000) / HTTPS (3001) ports.
- The toast shows the destination host (the full URL is in its tooltip),
  follows the active Home Assistant theme, and offers **Open**, **Copy** and
  **Dismiss**.

### Link types

| Link in Obsidian | Leaves the app as | Lands in |
|---|---|---|
| `http://`, `https://` | `shell.openExternal` → `xdg-open` → forwarder | the viewing browser, unless the Web viewer intercepts |
| `obsidian://…` | `xdg-open` → forwarder → `/usr/bin/obsidian` | the streamed Obsidian app |
| `file://…`, plain local paths | `xdg-open` → real binary | the in-container desktop |
| `mailto:…` | Electron `xdg-email` (not `xdg-open`) | the container's mail handler; none is installed, so normally nothing |
| `tel:`, `sms:`, `ftp:`, `zotero:` and other schemes | `shell.openExternal` → `xdg-open` → real binary | an in-container handler if one exists; otherwise the browser fallback may show a blank page |
| `javascript:`, `data:`, malformed or over-long values | `xdg-open` | rejected by the forwarder; never queued for the viewing browser |
| internal links, `[[wikilinks]]`, anchors, `app://` | Obsidian internal | never reach the OS |

Obsidian itself asks for confirmation before handing any scheme other than
`http(s)`, `obsidian` and existing local files to the OS, with an "always
open" checkbox; the add-on does not override that step.

The rule is narrow by design: only `http(s)` is queued for the viewing
browser, only `obsidian://` is routed to the app, and every other argument
keeps the in-container behavior. `mailto:` is not forwarded — forwarding it
would need an `xdg-email` overlay plus a daemon/client scheme allowlist (a
separate follow-up), and the image installs no mail client anyway.

## 2. Architecture

```text
Obsidian (Electron)
  click link
    -> shell.openExternal
       -> /usr/local/bin/xdg-open            PATH overlay
          -> valid single http(s) URL?
             -> POST 127.0.0.1:8091/api/open-url  {"url": "..."}
                -> bounded queue (8 items, 30 s TTL, 2 s dedupe)
          -> single obsidian:// deep link?
             -> exec /usr/bin/obsidian "$uri"  (the running app receives it)
          -> otherwise, or on any failure: exec /usr/bin/xdg-open "$@"   (in-container)

Viewing browser (Selkies dashboard page)
  open-url.js (classic <script> in the dashboard source HTML)
    -> GET <page-origin><ingress path>/api/open-url?wait=60   (hanging GET)
       -> nginx (GET-only, 70 s read timeout)
          -> open_url_server.py (S6 service, loopback 127.0.0.1:8091, as abc)
    -> 200 {"url": "...", "id": N}
       -> try window.open(url, "_blank")
          -> if blocked: show the anchor toast
    -> 204 / 429 / error: re-poll or back off
```

The hanging GET blocks on a condition variable and wakes in 0.5 s slices to
notice a disconnected client; the idle cost is a non-blocking select twice a
second, and the default recycles one request per minute. A real push transport
(SSE or WebSocket) would remove request churn but not idle CPU and would add
streaming-protocol and proxy-buffering complexity.

## 3. Verified base-image facts

Verified against the pinned base image
`lscr.io/linuxserver/obsidian:v1.14.5-ls150` (LinuxServer build commit
`c7d12aafacffeaf7786af9890164f9895c02e062`):

- Debian trixie with `xdg-utils 1.2.1-2`, `curl 8.14.1`, `python3.13`,
  `chromium` and `x11-utils`.
- Obsidian is launched as `/opt/obsidian/obsidian --no-sandbox` from
  `/usr/bin/obsidian`, as user `abc`.
- The default `PATH` is
  `/lsiopy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`,
  so `/usr/local/bin/xdg-open` shadows `/usr/bin/xdg-open`.
- The Selkies base image's `init-selkies-config` resolves `xdg-open` with
  `which` at every boot and `chmod 0755`s it (or `chmod 0000` when
  `HARDEN_DESKTOP=true`). The overlay is found and stays executable;
  hardening disables the feature by design.
- The nginx template has `location SUBFOLDERapi`, which becomes
  `location /api` (the Selkies WebSocket proxy). The explicit
  `/api/open-url` prefix location wins by longest-prefix matching, the same
  way the existing `/api/set-theme` location does.
- Home Assistant core and the Supervisor proxy ingress requests with
  `ClientTimeout(total=None)` and stream chunked responses, so the bounded
  hanging GET survives both hops.
- The add-on ingress panel iframe in the Home Assistant frontend has no
  `sandbox` attribute, so a new tab from the dashboard is permitted.
- Electron's `shell.openExternal` calls `xdg-open` for `http(s)` URLs and
  `xdg-email` for `mailto:`. Obsidian's Web viewer core plugin is disabled
  by default; once enabled it can route external links to the in-app viewer
  instead of `xdg-open`.
- The LinuxServer build extracts the Obsidian AppImage to `/opt/obsidian`
  but installs no `obsidian.desktop` scheme handler, so the real `xdg-open`
  cannot resolve `obsidian://` and falls through to Chromium, which renders
  a blank page. `/usr/bin/obsidian` forwards every argument to
  `/opt/obsidian/obsidian --no-sandbox`; Obsidian's documented Linux
  protocol handling takes the URI as an argument (`Exec=... %u`). Verified
  against the pinned 1.14.5 bundle: the second launch filters `--` flags
  and sends the remaining arguments over the app's CLI socket
  (`$XDG_RUNTIME_DIR/.obsidian-cli.sock`), and the running instance handles
  a trailing `obsidian://` argument. The forwarder passes the URI as the
  only non-flag argument, so it is last.

## 4. Protocol

```text
POST /api/open-url          (loopback, from the forwarder)
  {"url":"https://example.com/a?b=1"}
  -> 202 {"status":"queued","id":7}
  -> 400 {"status":"error","error":"..."}
  -> 404 (other paths)

GET /api/open-url?wait=60   (browser, through nginx; GET only)
  -> 200 {"url":"https://example.com/a?b=1","id":7}
  -> 204 (no URL within the wait window)
  -> 429 Retry-After: 2 (more than four simultaneous waiters)
  -> 404 (other paths)
```

- Only `http` and `https` are accepted. Empty, over-length (>8192),
  whitespace, control-character, missing-host and other-scheme values are
  rejected with 400 and never reach the browser.
- A single well-formed `obsidian://` argument is not queued: the forwarder
  execs `/usr/bin/obsidian` with it, so the running app handles the deep
  link. The viewing browser never sees it.
- The queue holds at most 8 items, drops items older than 30 s, and
  collapses repeats of the same URL within 2 s. A URL is delivered to the
  first live waiting client; a client that disconnects is detected within
  0.5 s, releases its slot and does not consume the URL. The state is
  in-memory and is lost on a daemon restart.
- `wait` defaults to 60 s and is clamped to 1..60 s. nginx uses a 70 s read
  timeout, deliberately above the maximum wait.
- The external nginx surface is GET-only (`limit_except GET { deny all; }`);
  only the in-container forwarder can enqueue a URL.
- Full URLs are not logged by default. Diagnostics carry the scheme, host
  and outcome; set `OPEN_URL_DEBUG=1` to include the full URL while
  debugging.

## 5. Why not the host-listener recipe

A common Docker recipe runs a small listener on the host and has the
container's `xdg-open` post to `host.docker.internal`. It does not fit this
add-on:

- The add-on manifest has no `extra_hosts` and there is no compose file, so
  `host.docker.internal` is not defined on the add-on network.
- The "host" is the Home Assistant OS machine, which is normally headless;
  the browser that matters is the client viewing the add-on.
- The sample listener binds `0.0.0.0` without authentication and does not
  validate schemes, which turns it into an open popup/`file://` surface.

The client-browser design needs none of that and works when the add-on is
viewed from any device.

## 6. Troubleshooting

- **Links open inside Obsidian.** Obsidian's Web viewer core plugin is
  enabled. Disable it (Settings → Core plugins) or use the link's
  right-click **Open in default browser**, which still goes through
  `xdg-open`.
- **Links open in the container's Chromium.** The forwarder could not reach
  the daemon and fell back to the real `xdg-open`. Check that the S6 service
  is running and inspect the add-on log for `[ha-open-url] outcome=...`
  lines.
- **Nothing happens and no toast appears.** The dashboard script may be
  stale (restart the add-on and hard-refresh the tab), or the tab was not
  visible when the link was clicked and the queued URL expired after 30 s.
- **The popup is blocked.** Click **Open** in the toast; a real anchor click
  is a user gesture and is allowed even when script-initiated popups are
  blocked.
- **`obsidian://` links.** They are handed to the in-container app, so the
  plugin or note screen opens in streamed Obsidian, not the browser. If the
  launcher cannot be executed the forwarder falls back to the real
  `xdg-open`, which may show a blank in-container page; the log shows
  `scheme=obsidian outcome=app` on the working path and `outcome=fallback`
  otherwise.
- **`mailto:` and other schemes.** They are intentionally not forwarded.
  Electron hands `mailto:` to `xdg-email`; the image installs no mail
  client, so normally nothing opens in the viewing browser. Other schemes
  (`tel:`, `zotero:`, …) fall back to the in-container `xdg-open`; without
  a handler, Chromium's fallback may show a blank in-container page. If the
  session's `xdg-email` delegates to `xdg-open`, the add-on log shows
  `scheme=mailto outcome=fallback`.
- **`HARDEN_DESKTOP=true`.** The base image makes `xdg-open` non-executable,
  so the feature is off by design.
- **Multiple viewers.** A queued link is consumed by the first waiting
  client. The add-on normally has one viewer.
- **A user-provided reverse proxy with a short idle timeout.** The 60 s
  hanging GET may be cut; the client retries and the 30 s TTL bounds
  unconsumed URLs. The wait can be lowered later without a protocol change.

## 7. Verification checklist

1. From inside the container:
   `docker exec -it addon_obsidian bash -c 'xdg-open https://example.com'` —
   the log shows `outcome=forwarded`, and the viewing browser opens a tab or
   shows the toast.
2. Click an external link in streamed Obsidian through the HA side panel.
3. Open the add-on on the exposed port 3000 and repeat.
4. Stop the daemon (for example `s6-svc -d /run/service/open-url-api`) and
   click a link — it opens in the in-container desktop and the add-on keeps
   running.
5. `xdg-open /config/some-file.pdf` — in-container behavior, unchanged.
6. `xdg-open 'javascript:alert(1)'` and `xdg-open 'file:///etc/passwd'` —
   not forwarded.
7. Click an `obsidian://` link (for example
   `obsidian://show-plugin?id=obsidian-importer`) — the plugin screen opens
   in streamed Obsidian, and the log shows `scheme=obsidian outcome=app`.
8. Enable the Web viewer core plugin and click a link — it opens in the
   in-app viewer; disabling the plugin restores forwarding.
9. Click a `mailto:` link — Obsidian's scheme warning appears (once);
   after allowing, no tab opens in the viewing browser and the link does
   not reach the toast.
10. Click a `tel:` or `zotero://` link — the same warning; the link stays in
    the container, where a blank page is expected when no handler exists.
11. Repeat 2–3 in Chrome and Firefox (and one mobile client if available).

## 8. Files

- `obsidian/root/usr/local/bin/xdg-open` — PATH overlay launcher.
- `obsidian/root/opt/ha-open-url/forwarder.py` — validation, JSON POST,
  Obsidian deep-link routing, fallback exec.
- `obsidian/root/opt/ha-open-url/open_url_server.py` — queue, HTTP API,
  daemon entry point.
- `obsidian/root/opt/ha-open-url/open-url.js` — dashboard client.
- `obsidian/root/etc/services.d/open-url-api/run` — S6 longrun service.
- `obsidian/Dockerfile` — nginx locations, script injection, build guards.
- `tests/test_open_url_server.py`, `tests/test_open_url_forwarder.py`,
  `tests/open-url.test.js`, `tests/test_packaging.py` — test coverage.

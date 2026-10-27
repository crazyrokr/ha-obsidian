# Open external links in the viewing browser

- Status: proposed
- Date: 2026-10-27
- Scope: add-on runtime, web UI, tests, documentation
- Related: [`docs/adr/0001-obsidian-ha-theme-sync.md`](../../adr/0001-obsidian-ha-theme-sync.md)

## 1. Context

Obsidian is the full desktop app streamed through Selkies. When a note
contains an `http(s)` link, Obsidian hands it to Electron's
`shell.openExternal`, which on Linux invokes `xdg-open`. In the add-on that
runs inside the container: whatever `xdg-open` resolves to is the container's
desktop, not the browser the user is actually looking at. Users expect the
link to open in their own browser.

The add-on is distributed as a Home Assistant add-on, not as a standalone
`docker-compose` service:

- There is no compose file and no `extra_hosts` in the add-on manifest, so
  `host.docker.internal` does not exist on the add-on network.
- The "host" is the Home Assistant OS machine, which is normally headless.
  Forwarding a URL to `xdg-open` there does not help.
- The browser that matters is the client that opened the add-on Web UI
  (ingress side panel, Web UI link, or the exposed port), possibly on a
  different machine or in the Home Assistant companion app.

The correct delivery target is therefore the **viewing browser**, and the
existing web UI already runs client-side code inside that browser.

## 2. Goals and non-goals

### Goals

1. Clicking an `http(s)` link in streamed Obsidian opens it in the browser
   that is viewing the add-on, without a copy/paste step.
2. Works through Home Assistant ingress and through the standalone exposed
   ports.
3. Does not change how non-URL `xdg-open` calls behave: opening a local
   attachment still uses the in-container desktop.
4. Degrades safely: if the new daemon is unavailable, links still open
   (in-container), and no add-on service fails.
5. No new runtime dependencies: Python standard library and plain browser
   JavaScript only, matching the existing theme sync.
6. Full unit coverage for the daemon, the forwarder and the client script;
   the existing suites stay green.
7. No idle CPU cost: the transport is a hanging GET whose handler blocks on
   a condition variable; the default recycles at most one request per
   minute per client and never busy-waits.

### Non-goals

- Forwarding `mailto:` (Electron uses `xdg-email`, a different binary).
  Documented as a possible follow-up.
- Forwarding or opening URLs on the Home Assistant host machine. That is a
  different deployment model and is not what add-on users need.
- Opening local files or arbitrary schemes outside the container.
- Enabling Obsidian's Web viewer plugin or changing Obsidian settings.
- Supporting the `selkies-dashboard-wish` variant (the add-on uses the
  default `selkies-dashboard`, same as the theme sync).

## 3. Verified environment facts

Verified against the pinned base image
`lscr.io/linuxserver/obsidian:v1.14.5-ls150@sha256:da0e7e08…` (LinuxServer
build commit `c7d12aafacffeaf7786af9890164f9895c02e062`):

- Debian trixie; `xdg-utils 1.2.1-2`, `curl 8.14.1`, `python3.13`,
  `chromium`, `x11-utils` are installed.
- Obsidian is launched as `/opt/obsidian/obsidian --no-sandbox` from
  `/usr/bin/obsidian`, as user `abc`.
- The default `PATH` is
  `/lsiopy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`,
  so a `/usr/local/bin/xdg-open` shadows `/usr/bin/xdg-open`.
- The Selkies base image's `init-selkies-config` resolves `xdg-open` with
  `which` at every boot and `chmod 0755`s it (or `chmod 0000` when
  `HARDEN_DESKTOP=true`). A wrapper on `PATH` is found and stays executable,
  and hardening correctly disables it.
- The nginx template has `location SUBFOLDERapi`, which becomes `location
  /api` (the Selkies WebSocket proxy). A new `/api/open-url` prefix location
  wins by longest-prefix matching, the same way the existing
  `/api/set-theme` location does.
- Home Assistant core and the Supervisor proxy ingress requests with
  `ClientTimeout(total=None)`, so a bounded long-poll survives both hops.
- The add-on ingress panel iframe in the Home Assistant frontend has no
  `sandbox` attribute; regular iframe panels allow `allow-popups`. Opening a
  new tab from the dashboard is permitted.
- Electron's `shell.openExternal` calls `xdg-open` for `http(s)` URLs and
  `xdg-email` for `mailto:`. Obsidian's Web viewer core plugin is disabled
  by default; once enabled it can route external links to the in-app viewer
  instead of `xdg-open`.

The one runtime assumption to confirm on the first built image is that
Obsidian's link click reaches `xdg-open` in this configuration. The
forwarder writes a diagnostic line for every invocation (scheme and host
only, plus `forwarded` or `fallback`) so the integration test can confirm or
refute it immediately.

## 4. Architecture

```text
Obsidian (Electron)
  click http(s) link
    -> shell.openExternal
       -> /usr/local/bin/xdg-open            wrapper (PATH ahead of /usr/bin)
          -> valid single http(s) URL?
             -> POST 127.0.0.1:8091/api/open-url  {"url": "..."}
                -> bounded queue (8 items, 30 s TTL)
             -> on any failure: exec /usr/bin/xdg-open "$@"   (in-container)

Viewing browser (Selkies dashboard page)
  open-url.js (injected into the dashboard source HTML, classic <script>)
    -> GET <page-origin><ingress path>/api/open-url?wait=60   (hanging GET)
       -> nginx (new location, GET only)
          -> open_url_server.py (s6 service, loopback 127.0.0.1:8091, as abc)
    -> 200 {"url": "...", "id": N}
       -> try window.open(url, "_blank")
          -> if blocked: show a compact toast
             with <a href=url target="_blank" rel="noopener noreferrer">
    -> 204 / 429 / error: re-poll immediately or with capped backoff
```

The feature is always on. `HARDEN_DESKTOP=true` disables it as a side effect
of the base image disabling `xdg-open`; no add-on option is introduced.

## 5. Components

### 5.1 Forwarder (`/usr/local/bin/xdg-open`)

A small executable launcher that execs the Python module in
`/opt/ha-open-url/forwarder.py` (keeps the testable logic in an importable
module and the on-PATH entry point trivial).

Decision rules:

- Forward only when exactly one argument is present and it is an `http(s)`
  URL: case-insensitive `http://` or `https://` prefix, `urllib.parse.urlsplit`
  parses it, `netloc` is non-empty, length is at most 8192 characters, and
  the value contains no ASCII control characters or whitespace.
- Otherwise `os.execv(/usr/bin/xdg-open, [real, *args])` unchanged.
- On a valid URL, send JSON `{"url": "..."}` to
  `OPEN_URL_ENDPOINT` (default `http://127.0.0.1:8091/api/open-url`) with a
  2 s total timeout. Any exception (connection refused, timeout, non-2xx)
  falls back to `os.execv` of the real `xdg-open`, so the link is not lost.
- `XDG_OPEN_REAL` overrides the real binary path (default `/usr/bin/xdg-open`)
  for tests.
- Arguments are never interpolated into a shell. The URL is JSON-encoded and
  passed as a value; `os.execv` receives a list.
- Diagnostics: write one line for every invocation with the scheme, host
  and outcome (`forwarded` or `fallback`); log the full URL only when
  `OPEN_URL_DEBUG=1`. Electron starts `xdg-open` without waiting for it, so
  the 2 s worst case does not block Obsidian.

### 5.2 Link daemon (`open_url_server.py`)

A new, dedicated service on loopback, separate from the theme daemon:

- `ThreadingHTTPServer`, `daemon_threads = True`, bound to `127.0.0.1:8091`
  (`OPEN_URL_HOST`/`OPEN_URL_PORT` overrides for tests), running as `abc`
  through a new s6 service. Standard library only.
- `PendingUrls`: a `deque(maxlen=8)` of `(id, url, enqueued_at)` guarded by
  a `threading.Condition`; monotonically increasing ids; injectable clock
  for deterministic tests.
  - `enqueue(url)`: drop stale items (older than 30 s); if the same URL was
    enqueued within the last 2 s, return the existing id; otherwise append,
    dropping the oldest item when full; notify waiters.
  - `take(wait_seconds)`: wait on the condition until an item or the
    deadline; pop and return the oldest non-stale item, or `None`.
  - The 30 s TTL is independent of the 60 s wait: a client already waiting
    receives an enqueued URL immediately; the TTL only bounds how long an
    unconsumed URL is kept while no client is waiting.
- Endpoints:
  - `POST /api/open-url` (loopback only; the wrapper calls it directly):
    JSON body `{"url": "..."}`. `202 {"status": "queued", "id": N}` on
    success; `400 {"status": "error", "error": "..."}` for a missing,
    malformed, disallowed-scheme, control-character, whitespace or
    over-length URL. Wrong path → 404.
  - `GET /api/open-url?wait=<seconds>` (browser via nginx): `wait` is
    optional, default 60, clamped to 1..60. `200 {"url": "...", "id": N}`
    with `Cache-Control: no-store` when an item is taken; `204` with no body
    on timeout; `429` with `Retry-After: 2` when the number of concurrently
    waiting clients exceeds 4. Wrong path → 404.
  - Only `GET` and `POST` are implemented on this path; other paths return
    404, consistent with the theme daemon's API style.
- No filesystem access, no state persisted across a daemon restart. A
  restart only loses queued links, which is acceptable.

Transport rationale: the long-poll is a hanging GET, not short polling. The
handler blocks in the condition variable, so an idle client consumes no CPU;
at the 60 s default it recycles one request per minute. Server-Sent Events
and WebSockets remove request churn but not idle CPU — both still need a
heartbeat — and add streaming-protocol and proxy-buffering complexity. The
60 s maximum wait stays under the 70 s nginx read timeout.

Validation is duplicated in the forwarder and the daemon: the forwarder
avoids pointless posts and the daemon is the authoritative gate for any
loopback caller.

### 5.3 Client script (`open-url.js`)

A second classic script injected into the **source** dashboard
`index.html` at build time (the same rule as `theme-sync.js`: edit the
regeneration source, not the regenerated copy).

- Derive the endpoint once at load:
  `<origin><pathname without trailing slashes>/api/open-url`. This is the
  same derivation the theme client uses, correct under ingress and
  standalone access.
- Long-poll loop:
  - One in-flight `GET ...?wait=60` at a time.
  - `200` → handle the URL, then poll again immediately.
  - `204` → poll again after a short yield.
  - `429` → honor `Retry-After`, then poll again.
  - Network error or unexpected status → retry after 1 s, 2 s, 5 s, then
    stay at 5 s; a success resets the backoff.
  - Pause while `document.hidden`; resume on `visibilitychange` when the
    document becomes visible.
- Opening strategy:
  1. Defensively validate with `new URL(url)` and require `http:`/`https:`.
     Ignore anything else (the daemon already filters; this is defense in
     depth).
  2. Try `window.open(url, "_blank")`. If it returns a window, set
     `opener = null` best-effort and stop.
  3. If it returns `null` (blocked), show the toast. The toast's **Open**
     control is a real `<a target="_blank" rel="noopener noreferrer">`, so
     the user's click is a browser gesture and the browser allows it even
     when script-initiated popups are blocked. The streamed-canvas click
     usually leaves transient activation for ~5 s, so the optimistic open
     often succeeds on Chrome; the toast is the guaranteed path on browsers
     with stricter policies.
- Toast:
  - One singleton fixed-position element appended to `document.body`,
    created lazily when a URL arrives. Maximum z-index, responsive width,
    safe-area aware.
  - Shows a short prompt, the destination host (full URL in the `title`),
    and **Open**, **Copy**, **Dismiss** controls. A new URL replaces the
    content of an existing toast.
  - Styled from `localStorage["theme"]` (seeded by `theme-sync.js` as
    `dark`/`light`) with a `prefers-color-scheme` fallback.
  - `role="status"` and `aria-live="polite"`; no focus stealing.
  - The URL is inserted with `textContent`/`setAttribute`, never
    `innerHTML`.
- If the dashboard removes the element during an SPA route change, the next
  URL recreates it; no observer is needed.

### 5.4 nginx and build integration

In the Dockerfile, extend the existing build-time edit of
`/defaults/default.conf` (which is copied to the live config at every boot)
with:

```nginx
location /api/open-url {
  limit_except GET { deny all; }
  proxy_http_version 1.1;
  proxy_read_timeout 70s;
  proxy_buffering off;
  proxy_pass http://127.0.0.1:8091;
}
location /open-url.js {
  alias /opt/ha-open-url/open-url.js;
}
```

- `limit_except GET` keeps the external surface read-only: only the
  in-container wrapper (loopback, bypassing nginx) can enqueue a URL.
- `proxy_read_timeout 70s` is deliberately above the 60 s maximum wait so
  nginx does not cut a legitimate long-poll short.
- Add the `<script src="./open-url.js"></script>` tag to the source
  `/usr/share/selkies/selkies-dashboard/index.html`, next to the existing
  `theme-sync.js` tag.
- Add build-time grep guards for `api/open-url`, the `open-url.js` location
  and the script tag, so a base-image bump that moves the anchors fails the
  build loudly, as the theme sync already does.
- Ensure the on-PATH launcher is executable (`chmod 0755`), and add the new
  s6 service under `/etc/services.d/open-url-api/run` (mode 0755, same
  shape as `theme-api/run`).

## 6. Protocol summary

```text
POST /api/open-url          (loopback, from the forwarder)
  {"url":"https://example.com/a?b=1"}
  -> 202 {"status":"queued","id":7}
  -> 400 {"status":"error","error":"unsupported scheme: 'javascript'"}
  -> 404 (other paths)

GET /api/open-url?wait=60   (browser, through nginx; GET only)
  -> 200 {"url":"https://example.com/a?b=1","id":7}
  -> 204 (no URL within the wait window)
  -> 429 Retry-After: 2 (more than 4 simultaneous waiters)
  -> 404 (other paths)
```

## 7. Security and failure handling

- **Scheme allowlist.** Only `http` and `https` are ever queued or opened.
  `javascript:`, `data:`, `file:`, `mailto:`, `ftp:` and unknown schemes are
  rejected by the daemon (400) and ignored by the client.
- **No shell.** The forwarder JSON-encodes the URL and uses `os.execv` with
  an argument list; the daemon never executes anything.
- **External surface is read-only.** nginx permits only `GET` on
  `/api/open-url`; enqueueing is loopback-only. The endpoint is additionally
  behind Home Assistant authentication when reached through ingress.
- **Destination visibility.** The toast shows the host and the full URL on
  hover, so the user sees where the link goes before clicking. The toast's
  Open control uses `rel="noopener noreferrer"`.
- **Bounded resources.** The queue holds at most 8 items and drops items
  older than 30 s; at most 4 clients can wait concurrently (429 beyond);
  `wait` is clamped to 60 s.
- **Privacy.** Full URLs are not logged by default; diagnostics log only the
  scheme and host. URLs may contain tokens, and the state is in-memory only.
- **Failure modes:**
  - Daemon unavailable → forwarder execs the real `xdg-open`, so the link
    opens in the container as before.
  - No browser client connected → the URL waits up to 30 s, then is dropped.
    The common case has a long-poll already in flight because the dashboard
    is visible.
  - Daemon restarted → queued links are lost; no persisted state.
  - `HARDEN_DESKTOP=true` → the base image makes `xdg-open` non-executable;
    the feature is off, by design.

## 8. Compatibility and edge cases

- **Obsidian Web viewer.** If the user enables the Web viewer core plugin
  and leaves "Open external links" on, links open in Obsidian's in-app
  viewer and never reach `xdg-open`. Documented; this is the user's explicit
  choice. Disabling the plugin restores the feature.
- **Right-click "Open in default browser".** Obsidian's explicit system
  open path goes through the same `shell.openExternal` → `xdg-open`, so it
  works even when the Web viewer is enabled.
- **Multiple viewers.** A queued link is consumed by the first waiting
  client. The add-on normally has one viewer; documented.
- **Standalone access.** The exposed port (3000/3001) serves the same page
  and the same script; the endpoint derivation is identical.
- **Mobile companion app.** The toast's anchor opens the system browser (or
  the app's external-browser view), subject to the host app's policy.
- **Popup blockers.** The optimistic `window.open` may be blocked,
  especially outside Chrome; the anchor toast is the guaranteed fallback.
- **Local attachments and other paths.** `xdg-open /config/file.pdf` is not
  an `http(s)` URL, so the wrapper execs the real binary and behavior is
  unchanged.
- **Theme.** The toast follows the already-seeded dashboard theme so it is
  legible in both HA luminance classes.

## 9. Testing

All tests use Given-When-Then.

### 9.1 `tests/test_open_url_server.py`

- Validation table: accepts `http://`/`https://` (including uppercase
  scheme), rejects empty, `javascript:`, `data:`, `file:`, `mailto:`, `ftp:`,
  missing netloc, control characters, internal whitespace, >8192 chars.
- Queue: FIFO order; consume-once; timeout returns `None`; stale items are
  dropped; `maxlen` drops the oldest; dedupe within 2 s returns the existing
  id; ids increase monotonically.
- HTTP: POST → 202; POST invalid → 400; wrong path → 404; GET takes a queued
  item → 200 JSON; GET timeout → 204; more than four waiters → 429 with
  `Retry-After`; `wait` defaults to 60 and clamps values outside 1..60;
  `Cache-Control: no-store`.
- Concurrency: enqueue from another thread wakes a blocked GET; a second
  waiter does not receive the same item.

### 9.2 `tests/test_open_url_forwarder.py`

- Decision table: `http(s)` true; uppercase scheme true; `file:`,
  `javascript:`, `mailto:`, plain paths, empty, multiple arguments false.
- Sending: a stub sender captures the JSON body and timeout; an exception
  triggers the fallback path.
- Integration: run the real launcher with `XDG_OPEN_REAL` pointing at a stub
  script and a local stub HTTP server; assert the JSON arrives; stop the
  server and assert the stub real binary receives the original argument.
- Injection: a URL containing shell metacharacters is sent as one JSON value
  and never executed.

### 9.3 `tests/open-url.test.js`

- Endpoint derivation: ingress path and standalone path.
- `200` + http(s) URL → `window.open` with `_blank`; `opener` is cleared.
- `window.open` returns `null` → toast appears with an anchor whose `href`,
  `target` and `rel` are correct; the anchor's text is set with
  `textContent`.
- `javascript:`/`file:` payload → ignored (defense in depth).
- `204` → immediate re-poll; `429` → honors `Retry-After`; network error →
  capped backoff; success resets backoff.
- Hidden document → no poll; visible again → poll resumes.
- Duplicate id → no second toast/open; a second URL replaces the existing
  toast content.
- **Copy** writes the URL through `navigator.clipboard` and degrades
  silently when clipboard access is unavailable.
- Dark/light theme from `localStorage` and the `prefers-color-scheme`
  fallback.
- No `document.body` yet → waits for `DOMContentLoaded`.

### 9.4 Suite and verification

- `python3 -m pytest -q` (existing 210 + new tests).
- `node --test "tests/*.test.js"` (existing 27 + new tests).
- Shellcheck the new s6 `run` script (the repository uses
  `# shellcheck shell=bash`).
- No SpotBugs/Checkstyle is configured in this repository.
- Live verification in a built add-on:
  1. `xdg-open https://example.com` inside the container → daemon log line,
     new tab or toast in the viewing browser.
  2. A real external link clicked in streamed Obsidian, through ingress and
     through the standalone port.
  3. Stop the daemon → the link opens in the in-container desktop.
  4. A local attachment path → in-container behavior unchanged.
  5. `javascript:` and `file://` probes → rejected/not forwarded.
  6. With the Web viewer plugin enabled → documented behavior observed.
  7. Chrome and Firefox (and one mobile client if available).

## 10. Documentation

- `docs/ha-obsidian-open-links.md`: architecture, verified base-image facts,
  behavior, troubleshooting (Web viewer, HARDEN_DESKTOP, popup blockers,
  daemon missing), and the live verification steps.
- Append a decision to the single shared ADR
  `docs/adr/0001-obsidian-ha-theme-sync.md` (per the repository rule: one
  shared ADR, append/update rather than a new record per change), covering
  the interceptor choice, the client-browser delivery, the separate daemon,
  the GET-only nginx surface and the Web viewer caveat.
- README: one feature bullet and a sentence in "How it works".
- No `config.yaml` option is added; the feature is on by default and is
  governed by the base image's `HARDEN_DESKTOP`.

## 11. Rejected alternatives

- **Host listener inside `docker-compose` (the pasted recipe).** Not
  applicable: no compose file or `extra_hosts` in an add-on, the HA host is
  usually headless, the client browser is the right target, the sample binds
  `0.0.0.0` without authentication, has no scheme allowlist, and its
  `xdg-open` call omits the URL. Kept only as background context.
- **Short polling instead of long-poll.** Simpler, but wakes the daemon
  continuously. Both ingress proxies allow requests with no total timeout,
  so a bounded hanging GET is verified viable and costs no CPU while idle.
- **Server-Sent Events.** Viable (both proxies stream chunked responses and
  Home Assistant excludes `text/event-stream` from compression), but it
  removes request churn, not idle CPU: a heartbeat is still required, and it
  adds a streaming handler, long-lived threads and proxy-buffering
  configuration. Plain HTTP long-poll is easier to bound and test; at the
  60 s default it recycles only one request per minute.
- **WebSocket.** Proven through ingress by the Selkies stream, but
  bidirectional and unnecessary here; a standard-library implementation
  needs RFC 6455 handshake and framing, or a new dependency, for no CPU
  benefit over a hanging GET.
- **Extend the theme daemon (port 8090).** Couples two unrelated concerns
  and puts long-poll waiters in the theme process. A separate single-purpose
  service is safer and independently testable.
- **Replace `/usr/bin/xdg-open` in place.** Mutating a distribution file is
  fragile; a `/usr/local/bin` overlay is PATH-safe and easy to remove.
- **An Obsidian community plugin.** A plugin runs inside the container and
  cannot reach the viewing browser; the OS-level `xdg-open` seam covers
  every caller, including Obsidian's own right-click "Open in default
  browser".
- **Obsidian's Web viewer.** It opens links inside Obsidian, the opposite of
  the goal.
- **Wrap `xdg-email` for `mailto:`.** Out of scope; a small follow-up if
  requested.

## 12. Open questions and risks

1. **Obsidian → `xdg-open` confirmation.** High confidence from Electron's
   source; the first integration run must confirm it. If a future Obsidian
   or Electron switches to another launcher, the forwarder's diagnostic
   disappears and the integration test fails.
2. **Popup auto-open policy.** Chrome should allow the optimistic open
   within the activation window; Safari may not. The toast is the fallback
   in all cases.
3. **Long-poll through exotic reverse proxies.** A user-provided reverse
   proxy may have a shorter idle timeout than 60 s; the client retries, and
   the queue TTL prevents stale delivery. A smaller `wait` can be chosen
   later without a protocol change.
4. **Multiple simultaneous viewers.** Consume-once means one viewer gets the
   link. Acceptable for the add-on's single-user model; document it.

## 13. Acceptance criteria

- An `http(s)` link clicked in streamed Obsidian opens in the viewing
  browser, via a new tab or a one-click toast.
- Works through ingress and the exposed ports.
- Non-URL opens behave exactly as before.
- All invalid schemes are rejected by the daemon and ignored by the client.
- Daemon absence falls back to in-container opening; no service crashes.
- The queue and its clients are bounded; no full URLs are logged by default.
- Existing and new pytest/node tests pass; documentation and the shared ADR
  are updated.
- No new runtime dependencies are added.

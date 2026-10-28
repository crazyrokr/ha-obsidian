# Open External Links in the Viewing Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `http(s)` links clicked in streamed Obsidian open in the browser that is viewing the add-on, while every other `xdg-open` call keeps its current in-container behavior.

**Architecture:** A `/usr/local/bin/xdg-open` overlay validates a single `http(s)` argument and POSTs it to a new loopback-only daemon (`127.0.0.1:8091`); on any failure it execs the real `/usr/bin/xdg-open`. The Selkies dashboard serves an injected client script that holds a 60 s hanging GET through nginx and opens the delivered URL with `window.open`, falling back to a user-clickable anchor toast when the browser blocks the popup.

**Tech Stack:** Python 3.13 standard library (`http.server`, `threading`, `urllib`), Debian shell (`sh`/`bash`), plain browser JavaScript (classic `<script>`, same style as `theme-sync.js`), pytest, Node's built-in `node:test`, Docker/S6.

**Spec:** `docs/superpowers/specs/2026-10-27-ha-obsidian-open-links-design.md`

## Global Constraints

- Only `http` and `https` URLs are ever forwarded or opened; every other scheme and non-URL argument is rejected or passed to the real `xdg-open`.
- No new runtime dependencies: Python standard library and plain browser JavaScript only.
- The daemon binds `127.0.0.1:8091`, runs as `abc`, and has no filesystem access.
- Queue limits: 8 items, 30 s TTL, 2 s dedupe window, wait default/max 60 s, at most 4 concurrent waiters.
- The nginx `/api/open-url` location is `GET`-only externally (`limit_except GET { deny all; }`) with `proxy_read_timeout 70s` and `proxy_buffering off`.
- The forwarder uses a 2 s timeout and falls back to `/usr/bin/xdg-open` on any error.
- Full URLs are never logged by default; diagnostics carry only scheme, host, and outcome.
- All source, identifiers, comments, tests, and docs are in English. Tests use Given-When-Then.
- The existing suites (210 pytest, 27 node) must stay green; no add-on options are added.
- Commits are signed (`commit.gpgsign=true`); write the message to a temp file and use `git commit -F <file>`.

## Review Focus

1. **A browser blocks the optimistic `window.open`.** The user still needs a one-click path; the toast's anchor must carry the URL, `target="_blank"`, and `rel="noopener noreferrer"`. Tested in Task 5.
2. **The daemon is down when a link is clicked.** The link must still open (in-container) and nothing may crash; the forwarder must exec the real `xdg-open`. Tested in Task 4.
3. **An unsafe or malformed URL is injected through the loopback POST** (`javascript:`, `file:`, control characters, whitespace, >8192 chars). It must be rejected before reaching the client. Tested in Tasks 1 and 3.
4. **The base image moves the nginx template or dashboard `</head>` anchor.** The build must fail loudly rather than ship a route that does not work. Tested in Task 6 (packaging guards plus the image build check).
5. **Two clients wait, or two links arrive quickly.** One URL must be consumed once, the second waiter must not hang forever, and a burst must not grow the queue without bound. Tested in Tasks 2 and 3.

---

## File Structure

- `obsidian/root/opt/ha-open-url/open_url_server.py` — URL validation, bounded queue, waiter limiter, HTTP handler, `main()`. One module because the forwarder reuses `validate_url` and the pieces share the same invariants.
- `obsidian/root/opt/ha-open-url/forwarder.py` — decision logic, JSON POST, fallback exec.
- `obsidian/root/usr/local/bin/xdg-open` — executable launcher that resolves the module relative to itself and execs Python.
- `obsidian/root/etc/services.d/open-url-api/run` — S6 longrun service, mode 0755.
- `obsidian/root/opt/ha-open-url/open-url.js` — client script injected into the dashboard source.
- `obsidian/Dockerfile` — nginx locations, script injection, build-time guards, executable bits.
- `tests/test_open_url_server.py` — validation, queue, limiter, HTTP behavior.
- `tests/test_open_url_forwarder.py` — decision table, JSON POST, fallback, launcher integration.
- `tests/open-url.test.js` — fake-browser tests for the client script.
- `tests/test_packaging.py` — repository-level guards for the launcher, service, Dockerfile, and the 60 s/70 s relationship.
- `docs/ha-obsidian-open-links.md`, `docs/adr/0001-obsidian-ha-theme-sync.md`, `README.md` — documentation.

The validation tests stay in `tests/test_open_url_server.py` (as the spec names it); the forwarder imports `validate_url` from the server module so both callers apply identical rules.

---

### Task 1: URL validation

**Files:**
- Create: `obsidian/root/opt/ha-open-url/open_url_server.py`
- Create: `tests/test_open_url_server.py`

**Interfaces:**
- Produces: `ALLOWED_SCHEMES: frozenset[str]`, `MAX_URL_LENGTH: int = 8192`, `validate_url(value: object) -> str`.
- `validate_url` returns the original string unchanged and raises `ValueError` on any rejection. It is used by the daemon (Task 3) and the forwarder (Task 4).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_open_url_server.py` with the module import pattern used by the
existing tests:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "obsidian" / "root" / "opt" / "ha-open-url"))

import open_url_server as ts  # noqa: E402
```

Add a parametrized accepted table and a parametrized rejected table. Each test
carries `# Given`, `# When`, `# Then` comments.

```python
@pytest.mark.parametrize("value", [
    "https://example.com/a?b=1",
    "http://example.com",
    "HTTPS://EXAMPLE.COM/path",
    "https://example.com/" + "a" * (ts.MAX_URL_LENGTH - len("https://example.com/")),
])
def test_valid_urls_are_returned_unchanged(value: str) -> None:
    ...
    assert ts.validate_url(value) == value


@pytest.mark.parametrize("value", [
    None, 123, "", "https://", "javascript:alert(1)", "data:text/html,x",
    "file:///etc/passwd", "mailto:a@b.c", "ftp://example.com",
    " https://example.com", "https://exam ple.com", "https://example.com/\x00",
    "https://example.com/" + "a" * (ts.MAX_URL_LENGTH - len("https://example.com/") + 1),
    "http://[::1",
])
def test_invalid_values_are_rejected(value: object) -> None:
    ...
    with pytest.raises(ValueError):
        ts.validate_url(value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_open_url_server.py -q`
Expected: FAIL with `AttributeError: module 'open_url_server' has no attribute 'MAX_URL_LENGTH'`.

- [ ] **Step 3: Implement `validate_url`**

In `open_url_server.py`:

```python
ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 8192

def validate_url(value: object) -> str:
    ...
```

Rules, in order: require `str`; require `0 < len(value) <= MAX_URL_LENGTH`;
reject any character where `character.isspace()` or `ord(character) < 0x20` or
`ord(character) == 0x7F`; parse with `urllib.parse.urlsplit`, catching
`ValueError`; require `scheme.lower() in ALLOWED_SCHEMES` and a non-empty
`netloc`. Return the original `value`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_open_url_server.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'feat: validate outbound link URLs' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add obsidian/root/opt/ha-open-url/open_url_server.py tests/test_open_url_server.py
git commit -F "$msg"
```

---

### Task 2: Bounded pending-URL queue

**Files:**
- Modify: `obsidian/root/opt/ha-open-url/open_url_server.py`
- Modify: `tests/test_open_url_server.py`

**Interfaces:**
- Produces: `PendingUrls(max_items: int = 8, ttl: float = 30.0, dedupe_window: float = 2.0, clock: Callable[[], float] = time.monotonic)`; `enqueue(url: str) -> int`; `take(wait_seconds: float) -> tuple[int, str] | None`; `__len__() -> int`; constants `DEFAULT_MAX_ITEMS`, `DEFAULT_TTL_SECONDS`, `DEFAULT_DEDUPE_WINDOW_SECONDS`.

- [ ] **Step 1: Write the failing tests**

Add a `FakeClock` helper and these tests:

| Test | Assertion |
|---|---|
| `test_enqueue_returns_increasing_ids` | ids `1`, `2` |
| `test_take_returns_fifo` | first enqueued URL is taken first |
| `test_take_consumes_an_item` | a second `take(0)` is `None` |
| `test_take_with_zero_wait_returns_none_when_empty` | `None`, no blocking |
| `test_stale_item_is_not_returned` | enqueue at `t=0`, advance `30.1`, `take(0)` is `None` |
| `test_item_at_ttl_boundary_is_returned` | advance exactly `30.0`, item returned |
| `test_dedupe_within_window_returns_existing_id` | same URL at `t=1`; same id, `len == 1` |
| `test_dedupe_outside_window_creates_new_id` | advance `3.0`; new id, `len == 2` |
| `test_max_items_drops_oldest` | enqueue 9 distinct URLs; `len == 8`; first is gone |
| `test_take_wakes_on_enqueue` | thread blocks in `take(1.0)`; `enqueue` wakes it with the item |
| `test_two_waiters_do_not_receive_the_same_item` | two threads call `take(0.5)`; one gets the item, the other `None` |

The wake tests use the real clock; the stale/dedupe tests use `FakeClock` with
`take(0)`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_open_url_server.py -q`
Expected: FAIL with `AttributeError: ... 'PendingUrls'`.

- [ ] **Step 3: Implement `PendingUrls`**

Use `collections.deque(maxlen=max_items)` of small records (id, url,
enqueued_at), `itertools.count(1)` for ids, and one `threading.Condition` for
all access. `enqueue` drops stale items from the left, searches from the right
for the same URL within `dedupe_window` and returns that id if found, otherwise
appends and notifies. `take` drops stale items, returns the left item if any,
otherwise waits until the deadline (`remaining <= 0` returns `None`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_open_url_server.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'feat: add the bounded pending-link queue' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add obsidian/root/opt/ha-open-url/open_url_server.py tests/test_open_url_server.py
git commit -F "$msg"
```

---

### Task 3: HTTP daemon and endpoints

**Files:**
- Modify: `obsidian/root/opt/ha-open-url/open_url_server.py`
- Modify: `tests/test_open_url_server.py`

**Interfaces:**
- Produces: `DEFAULT_HOST = "127.0.0.1"`, `DEFAULT_PORT = 8091`, `DEFAULT_WAIT_SECONDS = 60.0`, `MAX_WAIT_SECONDS = 60.0`, `MAX_WAITERS = 4`, `MAX_BODY_BYTES = 16384`; `clamp_wait(value: str | None) -> float`; `WaiterLimiter(limit: int = MAX_WAITERS)` with `acquire() -> bool`, `release() -> None`, `active` property; `OpenUrlServer(ThreadingHTTPServer)` with `.pending` and `.limiter`; `make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, pending: PendingUrls | None = None) -> OpenUrlServer`; `main() -> None`.
- Consumes: `validate_url`, `PendingUrls`.

- [ ] **Step 1: Write the failing tests**

Use a pytest fixture that calls `make_server("127.0.0.1", 0)`, starts
`serve_forever` in a daemon thread, and shuts it down in `finally`. Use
`http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)`.

| Test | Assertion |
|---|---|
| `test_clamp_wait` (parametrized) | `None→60`, `"30"→30`, `"0"→1`, `"-5"→1`, `"999"→60`, `"abc"→60`, `"nan"→60`, `"inf"→60` |
| `test_limiter_caps_concurrent_waiters` | 4 `acquire()` true, 5th false; `release()` allows the next |
| `test_post_queues_url` | 202, `status=queued`, id; `pending.take(0)` returns the URL |
| `test_post_rejects_invalid_scheme` | 400 and no queue entry |
| `test_post_rejects_malformed_json` | 400 |
| `test_post_rejects_missing_url` | 400 |
| `test_post_rejects_oversized_body` | 400, connection closed |
| `test_post_unknown_path_is_404` | 404 |
| `test_get_returns_a_queued_url` | 200 JSON with the URL and id, `Cache-Control: no-store` |
| `test_get_times_out_with_204` | `?wait=1` on an empty queue → 204, no body |
| `test_get_returns_429_when_all_waiter_slots_are_taken` | acquire 4 slots; GET → 429 with `Retry-After: 2` |
| `test_get_unknown_path_is_404` | 404 |
| `test_enqueue_wakes_a_blocked_get` | GET with `?wait=2` in a thread; POST after 0.1 s; GET returns 200 |
| `test_aborted_get_does_not_consume_a_later_url` | close a waiting GET; POST; the slot is released and a live GET still receives the URL |

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_open_url_server.py -q`
Expected: FAIL with `AttributeError: ... 'clamp_wait'` / connection refused.

- [ ] **Step 3: Implement the server**

`clamp_wait` handles `None`, non-numeric, non-finite, and out-of-range values,
returning `[1.0, 60.0]`. `WaiterLimiter` is a lock plus a counter.
`OpenUrlRequestHandler` implements `do_POST` and `do_GET` for the single path
`/api/open-url` (trailing slash tolerated); every other path is 404. `POST`
reads at most `MAX_BODY_BYTES`, parses JSON, requires an object with a string
`url`, calls `validate_url`, enqueues, and returns 202. `GET` clamps `wait`,
takes a limiter slot (non-blocking) or returns 429, calls `pending.take` with
a disconnect predicate, and returns 200 JSON or 204. An aborted request
returns without consuming a URL and releases its slot. JSON responses set
`Content-Type: application/json`,
`Content-Length`, and `Cache-Control: no-store`; 204 sends no body and no
`Content-Type`. `main()` reads `OPEN_URL_HOST`/`OPEN_URL_PORT`, prints
`[ha-open-url] listening on <host>:<port>`, and serves forever.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_open_url_server.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'feat: serve queued links over a loopback HTTP API' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add obsidian/root/opt/ha-open-url/open_url_server.py tests/test_open_url_server.py
git commit -F "$msg"
```

---

### Task 4: xdg-open forwarder and launcher

**Files:**
- Create: `obsidian/root/opt/ha-open-url/forwarder.py`
- Create: `obsidian/root/usr/local/bin/xdg-open`
- Create: `tests/test_open_url_forwarder.py`

**Interfaces:**
- Produces: `DEFAULT_ENDPOINT = "http://127.0.0.1:8091/api/open-url"`, `DEFAULT_REAL_XDG_OPEN = "/usr/bin/xdg-open"`, `DEFAULT_TIMEOUT = 2.0`; `should_forward(argv: list[str]) -> bool`; `send(url: str, endpoint: str = DEFAULT_ENDPOINT, timeout: float = DEFAULT_TIMEOUT) -> None`; `exec_real(argv: list[str], real: str = DEFAULT_REAL_XDG_OPEN) -> NoReturn`; `main(argv: list[str] | None = None) -> int`.
- Consumes: `validate_url` from `open_url_server`.

- [ ] **Step 1: Write the failing tests**

`tests/test_open_url_forwarder.py` inserts both module directories and imports
`forwarder`. Add:

| Test | Assertion |
|---|---|
| `test_should_forward` (parametrized) | `["https://example.com"]`, `["http://example.com"]`, `["HTTPS://EXAMPLE.COM"]` → true; `["file:///etc/passwd"]`, `["javascript:alert(1)"]`, `["mailto:a@b.c"]`, `["/config/a.pdf"]`, `[]`, `["https://a", "https://b"]` → false |
| `test_send_posts_json` | local stub server receives `POST /api/open-url`, `Content-Type: application/json`, body `{"url": "https://example.com/a?b=1"}` |
| `test_send_raises_on_non_2xx` | stub 500 → `OSError` |
| `test_main_forwards_without_exec` | stub endpoint; `exec_real` monkeypatched to fail the test if called; `main([url]) == 0` |
| `test_main_falls_back_on_connection_error` | closed endpoint; `exec_real` monkeypatched to record and raise `SystemExit(0)`; recorded args equal `[url]` |
| `test_main_falls_back_for_non_url` | `main(["/config/a.pdf"])`; `exec_real` receives the path |
| `test_diagnostics_do_not_include_the_full_url` | `capsys` stderr contains `host=example.com` and not `/secret-path` |
| `test_launcher_passes_the_argument_to_the_forwarder` | run the launcher from the repo with `XDG_OPEN_REAL` pointing at a stub that writes `$*` to a temp file and a dead endpoint; the file contains the URL |

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_open_url_forwarder.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'forwarder'`.

- [ ] **Step 3: Implement the forwarder and launcher**

`should_forward` requires exactly one argument and a successful
`validate_url`. `send` JSON-encodes `{"url": url}` and uses
`urllib.request.urlopen(..., timeout=timeout)`, raising `OSError` for a
non-2xx status. `main`:

1. Reads `XDG_OPEN_REAL` and `OPEN_URL_ENDPOINT` from the environment.
2. If `should_forward` is false, logs `outcome=fallback scheme=<s> host=<h>`
   and calls `exec_real`.
3. Otherwise calls `send`; on any exception logs the same diagnostic and calls
   `exec_real`; on success logs `outcome=forwarded scheme=<s> host=<h>` and
   returns `0`.

The diagnostic uses `urlsplit` for scheme/host, never the full URL unless
`OPEN_URL_DEBUG=1`. `exec_real` uses `os.execv(real, [real, *argv])`.

The launcher `obsidian/root/usr/local/bin/xdg-open` is mode 0755:

```sh
#!/bin/sh
# Runs the forwarder from this script's directory so the same path works in
# the image (/usr/local/bin -> /opt) and in the repository.
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 "$SELF_DIR/../../../opt/ha-open-url/forwarder.py" "$@"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_open_url_forwarder.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'feat: forward xdg-open links to the link daemon' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add obsidian/root/opt/ha-open-url/forwarder.py obsidian/root/usr/local/bin/xdg-open tests/test_open_url_forwarder.py
git commit -F "$msg"
```

---

### Task 5: Client long-poll and toast

**Files:**
- Create: `obsidian/root/opt/ha-open-url/open-url.js`
- Create: `tests/open-url.test.js`

**Interfaces:**
- Produces: an IIFE with no exports. Uses `window.location`, `window.open`,
  `window.document`, `window.navigator`, `window.localStorage`,
  `window.matchMedia`, `fetch`, `setTimeout`, `clearTimeout`, and the global
  `URL`.
- Constants: `POLL_WAIT_SECONDS = 60`, `RETRY_DELAYS_MS = [1000, 2000, 5000]`,
  `TOAST_ID = "ha-open-url-toast"`, `STYLE_ID = "ha-open-url-style"`.
- Functions: `endpoint()`, `poll()`, `schedule(delayMs)`, `handleResponse(response)`,
  `handleUrl(raw)`, `showToast(url)`, `removeToast()`, `toastTheme()`.

- [ ] **Step 1: Write the failing tests**

Create a fake browser modeled on `tests/theme-sync.test.js`. Load the script
with:

```js
const source = fs.readFileSync(SCRIPT_PATH, "utf8");
const factory = new Function("window", "fetch", "setTimeout", "clearTimeout", source);
factory(window, fakeFetch, setTimeoutStub, clearTimeoutStub);
```

The fake `window` provides `location.href`, `open`, `document`, `navigator`,
`localStorage`, and `matchMedia`. The fake `document` provides `head`, `body`,
`hidden`, `createElement`, `getElementById`, `addEventListener`, and an
`emit(event)` helper. Fake elements support `id`, `tagName`, `attributes`,
`textContent`, `children`, `parentNode`, `setAttribute`, `appendChild`,
`removeChild`, and `addEventListener`.

Tests (all with `// Given`, `// When`, `// Then`):

| Test | Assertion |
|---|---|
| `test_endpoint_uses_the_ingress_path` | first fetch is `https://ha.example.com/api/ingress/t0k3n/api/open-url?wait=60` |
| `test_endpoint_uses_the_standalone_path` | first fetch is `https://ha.example.com/api/open-url?wait=60` |
| `test_initial_fetch_is_no_store` | options include `cache: "no-store"` |
| `test_200_opens_the_url` | `window.open(url, "_blank")` called once; no toast |
| `test_200_ignores_non_http_schemes` | `javascript:` and `file:` cause no open and no toast; polling continues |
| `test_blocked_popup_shows_the_toast` | `window.open` returns `null`; toast exists; anchor `href`, `target`, `rel` correct; text is the host; `title` is the URL |
| `test_toast_uses_text_content` | no `innerHTML` path; URL with `<img>` is rendered as text |
| `test_204_repolls` | next fetch scheduled |
| `test_429_honors_retry_after` | next timeout delay is 2000 ms |
| `test_error_backoff_and_reset` | 500 → 1000, 2000, 5000, 5000 ms; a later 200 resets to 1000 |
| `test_hidden_document_pauses` | no new fetch while `hidden`; visible event starts one |
| `test_repeated_id_after_restart_still_opens` | the same `id` twice opens twice (ids reset with the daemon) |
| `test_copy_uses_clipboard_and_degrades` | `writeText(url)` called; absent clipboard does not throw |
| `test_dismiss_removes_the_toast` | `getElementById(TOAST_ID)` is null after dismiss |
| `test_theme_dark_and_light` | `data-theme` follows `localStorage["theme"]`, then `prefers-color-scheme` |
| `test_missing_body_waits_for_dom_content_loaded` | no throw; toast appears after the event |
| `test_no_overlapping_polls` | a second `poll()` while one is in flight issues no fetch |

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test tests/open-url.test.js`
Expected: FAIL because `open-url.js` does not exist.

- [ ] **Step 3: Implement the client script**

Derive the endpoint once, then run a single-flight loop: one `fetch` with
`?wait=60`; 200 → validate and handle, then re-poll; 204 → re-poll after a
yield; 429 → honor `Retry-After`; error/other → capped backoff
(1 s, 2 s, 5 s, then 5 s). Pause on `document.hidden`; resume on
`visibilitychange`.

`handleUrl` validates with `new URL`, requires `http:`/`https:`, tries
`window.open(url, "_blank")`, nulls `opener` when it returns a window, and
otherwise calls `showToast`. `showToast` creates a singleton fixed-position
element with a host label, an **Open** anchor (`href`, `target="_blank"`,
`rel="noopener noreferrer"`), **Copy**, and **Dismiss**; styles come from a
single `<style id="ha-open-url-style">` block with
`position: fixed`, a maximum `z-index`, and `[data-theme="dark"]`/light
variants. All values are set with `textContent`/`setAttribute`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test tests/open-url.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'feat: open delivered links in the dashboard browser' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add obsidian/root/opt/ha-open-url/open-url.js tests/open-url.test.js
git commit -F "$msg"
```

---

### Task 6: Image integration (nginx, S6, packaging guards)

**Files:**
- Create: `obsidian/root/etc/services.d/open-url-api/run`
- Modify: `obsidian/Dockerfile`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Produces: an S6 longrun service that starts `open_url_server.py` as `abc`;
  nginx locations `/api/open-url` and `/open-url.js`; the dashboard script
  tag; executable bits.
- Consumes: the modules from Tasks 1–5.

- [ ] **Step 1: Write the failing packaging tests**

`tests/test_packaging.py` reads the repository files directly:

| Test | Assertion |
|---|---|
| `test_xdg_open_launcher_is_executable` | `os.stat(ROOT / "obsidian/root/usr/local/bin/xdg-open").st_mode & 0o111` |
| `test_open_url_service_is_executable` | the S6 `run` file is executable |
| `test_service_runs_the_daemon_as_abc` | the run file contains `s6-setuidgid abc` and `open_url_server.py` |
| `test_launcher_resolves_the_forwarder` | the launcher contains `../../../opt/ha-open-url/forwarder.py` |
| `test_dockerfile_adds_the_open_url_location` | contains `location /api/open-url`, `limit_except GET`, `proxy_read_timeout 70s`, `proxy_buffering off`, `proxy_pass http://127.0.0.1:8091` |
| `test_dockerfile_adds_the_script_location` | contains `location /open-url.js` and the alias |
| `test_dockerfile_injects_the_script_with_a_guard` | contains both `grep -q open-url.js` checks and the `sed` into the dashboard source |
| `test_dockerfile_guards_the_nginx_anchor` | contains `grep -q api/open-url /defaults/default.conf` |
| `test_read_timeout_exceeds_max_wait` | import `open_url_server` with the same path insert as the other tests, parse `70` from the Dockerfile, assert `ts.MAX_WAIT_SECONDS == 60` and `ts.MAX_WAIT_SECONDS < 70` |

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_packaging.py -q`
Expected: FAIL (missing files and Dockerfile strings).

- [ ] **Step 3: Add the S6 service and Dockerfile changes**

`obsidian/root/etc/services.d/open-url-api/run` (mode 0755):

```bash
#!/usr/bin/with-contenv bash
# shellcheck shell=bash
# Runs the external-link daemon as the abc account; it only queues URLs and
# has no filesystem access.
exec s6-setuidgid abc python3 /opt/ha-open-url/open_url_server.py
```

In `obsidian/Dockerfile`:

- After `COPY root /`, add
  `RUN chmod 0755 /usr/local/bin/xdg-open /etc/services.d/open-url-api/run`.
- Extend the nginx `sed` with the `/api/open-url` and `/open-url.js` blocks
  (inserted before `location SUBFOLDER {`, so both the HTTP and HTTPS server
  blocks get them):

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

- Add a second, independent script injection:
  `( grep -q open-url.js /usr/share/selkies/selkies-dashboard/index.html || sed -i 's|</head>|<script src="./open-url.js"></script></head>|' /usr/share/selkies/selkies-dashboard/index.html )`
- Add `grep -q api/open-url /defaults/default.conf` and
  `grep -q open-url.js /usr/share/selkies/selkies-dashboard/index.html` to the
  build guards.

- [ ] **Step 4: Run the packaging tests and build the image**

Run: `python3 -m pytest tests/test_packaging.py -q`
Expected: PASS.

Run (if Docker is available): `docker build -t ha-obsidian-open-links ./obsidian`
Expected: the build succeeds. Then:

```bash
docker run --rm --entrypoint /bin/bash ha-obsidian-open-links -c \
  "grep -q 'api/open-url' /defaults/default.conf && grep -q 'open-url.js' /usr/share/selkies/selkies-dashboard/index.html && test -x /usr/local/bin/xdg-open && test -x /etc/services.d/open-url-api/run"
```

Expected: exit 0. If Docker is unavailable in the execution environment, note
it and rely on the packaging tests plus CI.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'feat: wire the link daemon into the add-on image' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add obsidian/Dockerfile obsidian/root/etc/services.d/open-url-api/run tests/test_packaging.py
git commit -F "$msg"
```

---

### Task 7: Documentation, ADR, README

**Files:**
- Create: `docs/ha-obsidian-open-links.md`
- Modify: `docs/adr/0001-obsidian-ha-theme-sync.md`
- Modify: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Write the feature guide**

Create `docs/ha-obsidian-open-links.md` with: the architecture diagram from
the spec, the verified base-image facts (`xdg-utils`, Electron → `xdg-open`,
the nginx `location /api` longest-prefix rule, the ingress sandbox and
streaming paths), the protocol (`POST` loopback, `GET` 60 s hanging GET), the
toast behavior, and a troubleshooting section covering the Web viewer plugin,
`HARDEN_DESKTOP=true`, popup blockers, and a missing daemon. Include the live
verification checklist.

- [ ] **Step 2: Append the shared ADR decision**

In `docs/adr/0001-obsidian-ha-theme-sync.md`, update `Last updated` to
`2026-10-27` and append item 11 to the Decision list: intercept `xdg-open`
through a PATH overlay, deliver via a separate loopback daemon and a 60 s
hanging GET, keep the external nginx surface GET-only, and accept the Web
viewer caveat. Record the rejected alternatives and why: the host listener
with `host.docker.internal`/`extra_hosts`, short polling, SSE, WebSocket, and
extending the theme daemon on 8090. Add the corresponding Consequences
bullets (one extra long-lived s6 service and one new nginx location; no new
dependencies; the feature is off under `HARDEN_DESKTOP`). Do not create a
second ADR.

- [ ] **Step 3: Update the README**

Add a feature bullet ("Links open in your browser") and one sentence in "How
it works"; add `docs/ha-obsidian-open-links.md` to the Documentation list.

- [ ] **Step 4: Verify the docs render and links resolve**

Run: `python3 -m pytest -q` (docs changes must not break anything).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
msg="$TMPDIR/$(date +%s)-commit-msg.txt"
printf '%s\n' 'docs: document external link forwarding' '' 'Co-Authored-By: Claude Code <noreply@anthropic.com>' > "$msg"
git add docs/ha-obsidian-open-links.md docs/adr/0001-obsidian-ha-theme-sync.md README.md
git commit -F "$msg"
```

---

### Task 8: Full verification and live acceptance

**Files:** none (verification only).

- [ ] **Step 1: Run the full Python suite**

Run: `python3 -m pytest -q`
Expected: all tests pass (the existing 210 plus the new server, forwarder, and
packaging tests).

- [ ] **Step 2: Run the full Node suite**

Run: `node --test "tests/*.test.js"`
Expected: all tests pass (the existing 27 plus the new client tests).

- [ ] **Step 3: Run shellcheck on the new shell files (if installed)**

Run: `shellcheck obsidian/root/etc/services.d/open-url-api/run obsidian/root/usr/local/bin/xdg-open`
Expected: no findings. If shellcheck is not installed, skip and note it.

- [ ] **Step 4: Build and inspect the image (if Docker is available)**

Run the build and container checks from Task 6. Expected: exit 0.

- [ ] **Step 5: Live acceptance on a running add-on**

On a real Home Assistant instance with the add-on built and running:

1. `docker exec -it addon_<slug> bash -c 'xdg-open https://example.com'` →
   the daemon logs `outcome=forwarded`, and the viewing browser opens a tab or
   shows the toast.
2. Click an external link in streamed Obsidian through the HA side panel →
   same result.
3. Open the add-on on the exposed port (3000) and repeat.
4. Stop the daemon (`s6-svc -d /run/service/open-url-api` or the equivalent)
   and click a link → it opens in the in-container desktop, and the add-on
   keeps running.
5. `xdg-open /config/some-file.pdf` → in-container behavior, unchanged.
6. `xdg-open 'javascript:alert(1)'` and `xdg-open 'file:///etc/passwd'` → not
   forwarded by the daemon (the wrapper passes non-http(s) straight through).
7. Enable Obsidian's Web viewer core plugin and click a link → it opens in the
   in-app viewer; disabling the plugin restores forwarding.

- [ ] **Step 6: Final commit (if verification changed anything)**

If any fix was needed, commit it with the signed `git commit -F` pattern.
Otherwise no commit is required.

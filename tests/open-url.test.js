"use strict";

/* Node-based tests for open-url.js. Loads the real script into a minimal
 * fake browser (document, storage, popups, clipboard, fetch and timers).
 * Run with:
 *
 *   node --test "tests/*.test.js"
 */

const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const SCRIPT_PATH = path.join(
  __dirname,
  "..",
  "obsidian",
  "root",
  "opt",
  "ha-open-url",
  "open-url.js"
);
const INGRESS_HREF = "https://ha.example.com/api/ingress/t0k3n/";
const STANDALONE_HREF = "https://ha.example.com/";
const TOAST_ID = "ha-open-url-toast";
const STYLE_ID = "ha-open-url-style";

function makeElement(tagName) {
  return {
    tagName,
    id: "",
    className: "",
    attributes: {},
    children: [],
    parentNode: null,
    listeners: {},
    _textContent: "",
    get textContent() {
      return this._textContent;
    },
    set textContent(value) {
      this._textContent = String(value);
      this.children = [];
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    getAttribute(name) {
      return this.attributes[name];
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      child.parentNode = null;
      this.children = this.children.filter((node) => node !== child);
      return child;
    },
    addEventListener(type, listener) {
      (this.listeners[type] = this.listeners[type] || []).push(listener);
    },
    emit(type, event) {
      (this.listeners[type] || []).slice().forEach((listener) => listener(event || {}));
    },
  };
}

function findByClass(root, className) {
  if (!root) return null;
  if (root.className === className) return root;
  for (const child of root.children) {
    const found = findByClass(child, className);
    if (found) return found;
  }
  return null;
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get() { return null; } },
    json() { return Promise.resolve(payload); },
  };
}

function statusResponse(status) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get(name) { return name === "Retry-After" ? "2" : null; } },
  };
}

function createBrowser(options = {}) {
  const fetchCalls = [];
  const timeouts = [];
  const openCalls = [];
  const openedWindows = [];
  const clipboardCalls = [];
  const createdTags = [];
  const behaviors = (options.behaviors || []).slice();
  const documentListeners = {};
  let body = options.body === false ? null : makeElement("body");
  const head = makeElement("head");
  const documentElement = makeElement("html");
  head.parentNode = documentElement;
  documentElement.children = [head];
  if (body) {
    body.parentNode = documentElement;
    documentElement.children.push(body);
  }

  function createElement(tagName) {
    createdTags.push(tagName);
    return makeElement(tagName);
  }

  const fakeDocument = {
    documentElement,
    head,
    body,
    hidden: false,
    createElement,
    getElementById(id) {
      let found = null;
      (function scan(node) {
        if (found) return;
        if (node.id === id) {
          found = node;
          return;
        }
        node.children.forEach(scan);
      })(documentElement);
      return found;
    },
    addEventListener(type, listener) {
      (documentListeners[type] = documentListeners[type] || []).push(listener);
    },
    emit(type, event) {
      (documentListeners[type] || []).slice().forEach((listener) => listener(event || {}));
    },
    attachBody() {
      body = makeElement("body");
      body.parentNode = documentElement;
      documentElement.children.push(body);
      fakeDocument.body = body;
      return body;
    },
  };

  const localStorage = {
    data: {},
    getItem(key) {
      return key in this.data ? this.data[key] : null;
    },
    setItem(key, value) {
      this.data[key] = String(value);
    },
  };
  if (options.theme) localStorage.setItem("theme", options.theme);

  const window = {
    location: { href: options.href || INGRESS_HREF },
    document: fakeDocument,
    localStorage,
    navigator: {},
    matchMedia(query) {
      assert.equal(query, "(prefers-color-scheme: dark)");
      return { matches: options.localScheme === "dark" };
    },
    open(url, target) {
      openCalls.push({ url, target });
      if (options.blockPopups) return null;
      const opened = { opener: {} };
      openedWindows.push(opened);
      return opened;
    },
  };
  if (options.clipboard !== false) {
    window.navigator.clipboard = {
      writeText(value) {
        clipboardCalls.push(value);
        return Promise.resolve();
      },
    };
  }

  function fakeFetch(url, init) {
    fetchCalls.push({ url: String(url), init });
    if (behaviors.length) return behaviors.shift()();
    return Promise.resolve(statusResponse(204));
  }

  function setTimeoutStub(callback, delay) {
    const entry = { callback, delay, cancelled: false };
    timeouts.push(entry);
    return entry;
  }

  function clearTimeoutStub(entry) {
    if (entry) entry.cancelled = true;
  }

  function runTimeouts() {
    const due = timeouts.splice(0, timeouts.length).filter((entry) => !entry.cancelled);
    due.forEach((entry) => entry.callback());
  }

  function pendingDelays() {
    return timeouts.filter((entry) => !entry.cancelled).map((entry) => entry.delay);
  }

  async function tick() {
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
  }

  function load() {
    const source = fs.readFileSync(SCRIPT_PATH, "utf8");
    const factory = new Function(
      "window",
      "fetch",
      "setTimeout",
      "clearTimeout",
      source
    );
    factory(window, fakeFetch, setTimeoutStub, clearTimeoutStub);
  }

  return {
    window,
    document: fakeDocument,
    fetchCalls,
    openCalls,
    openedWindows,
    clipboardCalls,
    createdTags,
    behaviors,
    push(behavior) {
      behaviors.push(behavior);
    },
    load,
    tick,
    runTimeouts,
    pendingDelays,
    toast() {
      return fakeDocument.getElementById(TOAST_ID);
    },
    style() {
      return fakeDocument.getElementById(STYLE_ID);
    },
    localStorage,
  };
}

test("endpoint: ingress path", async () => {
  // Given the dashboard is embedded under ingress
  const browser = createBrowser({ href: INGRESS_HREF });
  // When the script loads
  browser.load();
  await browser.tick();
  // Then the first request uses the ingress-prefixed endpoint
  assert.equal(
    browser.fetchCalls[0].url,
    "https://ha.example.com/api/ingress/t0k3n/api/open-url?wait=60"
  );
});

test("endpoint: standalone path", async () => {
  // Given the dashboard is opened directly
  const browser = createBrowser({ href: STANDALONE_HREF });
  // When the script loads
  browser.load();
  await browser.tick();
  // Then the first request uses the origin-relative endpoint
  assert.equal(
    browser.fetchCalls[0].url,
    "https://ha.example.com/api/open-url?wait=60"
  );
});

test("initial fetch is no-store", async () => {
  // Given the script on load
  const browser = createBrowser();
  // When it polls
  browser.load();
  await browser.tick();
  // Then the request bypasses the cache
  assert.equal(browser.fetchCalls[0].init.cache, "no-store");
});

test("200 opens the URL in a new tab", async () => {
  // Given a response carrying an https URL
  const browser = createBrowser({
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  // When the script handles it
  browser.load();
  await browser.tick();
  // Then a new tab is opened with the URL and no toast appears
  assert.deepEqual(browser.openCalls, [{ url: "https://example.com/a", target: "_blank" }]);
  assert.equal(browser.openedWindows[0].opener, null);
  assert.equal(browser.toast(), null);
});

test("mailto shows the toast without opening a tab", async () => {
  // Given a delivered mail link
  const link = "mailto:a@example.com?subject=Hi";
  const browser = createBrowser({
    behaviors: [() => Promise.resolve(jsonResponse({ url: link, id: 1 }))],
  });
  // When the script handles it
  browser.load();
  await browser.tick();
  // Then no tab is opened and the toast asks for a real user click
  assert.deepEqual(browser.openCalls, []);
  const toast = browser.toast();
  assert.ok(toast);
  assert.equal(
    findByClass(toast, "ha-open-url-title").textContent,
    "Open this email?"
  );
  const host = findByClass(toast, "ha-open-url-host");
  assert.equal(host.textContent, "a@example.com");
  assert.equal(host.attributes.title, link);
  const open = findByClass(toast, "ha-open-url-open");
  assert.equal(open.attributes.href, link);
  assert.equal(open.attributes.target, undefined);
});

test("tel shows the call toast", async () => {
  // Given a delivered phone link
  const link = "tel:+15551234567";
  const browser = createBrowser({
    behaviors: [() => Promise.resolve(jsonResponse({ url: link, id: 1 }))],
  });
  // When the script handles it
  browser.load();
  await browser.tick();
  // Then no tab is opened and the toast offers the call
  assert.deepEqual(browser.openCalls, []);
  const toast = browser.toast();
  assert.equal(
    findByClass(toast, "ha-open-url-title").textContent,
    "Call this number?"
  );
  assert.equal(findByClass(toast, "ha-open-url-host").textContent, "+15551234567");
  const open = findByClass(toast, "ha-open-url-open");
  assert.equal(open.attributes.href, link);
  assert.equal(open.attributes.target, undefined);
});

test("sms shows the message toast", async () => {
  // Given a delivered message link
  const link = "sms:+15551234567?body=Hi";
  const browser = createBrowser({
    behaviors: [() => Promise.resolve(jsonResponse({ url: link, id: 1 }))],
  });
  // When the script handles it
  browser.load();
  await browser.tick();
  // Then no tab is opened and the toast offers the message
  assert.deepEqual(browser.openCalls, []);
  const toast = browser.toast();
  assert.equal(
    findByClass(toast, "ha-open-url-title").textContent,
    "Send this message?"
  );
  assert.equal(findByClass(toast, "ha-open-url-host").textContent, "+15551234567");
  const open = findByClass(toast, "ha-open-url-open");
  assert.equal(open.attributes.href, link);
  assert.equal(open.attributes.target, undefined);
});

test("200 ignores unsupported schemes", async () => {
  // Given responses carrying disallowed schemes
  const browser = createBrowser({
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "javascript:alert(1)", id: 1 })),
      () => Promise.resolve(jsonResponse({ url: "file:///etc/passwd", id: 2 })),
      () => Promise.resolve(jsonResponse({ url: "ftp://example.com/x", id: 3 })),
      () => Promise.resolve(jsonResponse({ url: "obsidian://open?vault=main", id: 4 })),
    ],
  });
  // When the script handles each one
  browser.load();
  await browser.tick();
  // Then nothing opens, no toast appears, and polling continues
  assert.deepEqual(browser.openCalls, []);
  assert.equal(browser.toast(), null);
  assert.deepEqual(browser.pendingDelays(), [0]);
  browser.runTimeouts();
  await browser.tick();
  browser.runTimeouts();
  await browser.tick();
  browser.runTimeouts();
  await browser.tick();
  // Then every unsupported scheme is ignored and polling continues
  assert.deepEqual(browser.openCalls, []);
  assert.equal(browser.toast(), null);
  assert.deepEqual(browser.pendingDelays(), [0]);
});

test("blocked popup shows the toast", async () => {
  // Given a blocked popup and a delivered URL
  const browser = createBrowser({
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  // When the script handles it
  browser.load();
  await browser.tick();
  // Then the toast carries a user-clickable anchor and the destination
  const toast = browser.toast();
  assert.ok(toast);
  assert.equal(toast.attributes["data-theme"], "light");
  const host = findByClass(toast, "ha-open-url-host");
  assert.equal(host.textContent, "example.com");
  assert.equal(host.attributes.title, "https://example.com/a");
  const open = findByClass(toast, "ha-open-url-open");
  assert.equal(open.textContent, "Open");
  assert.equal(open.attributes.href, "https://example.com/a");
  assert.equal(open.attributes.target, "_blank");
  assert.equal(open.attributes.rel, "noopener noreferrer");
  // And the styles pin the toast above the stream
  const style = browser.style();
  assert.ok(style);
  assert.match(style.textContent, /position:fixed/);
  assert.match(style.textContent, /2147483647/);
});

test("toast renders the URL as text", async () => {
  // Given a URL containing HTML-like text
  const raw = "https://example.com/notes/<img>";
  const expected = new URL(raw).href;
  const browser = createBrowser({
    blockPopups: true,
    behaviors: [() => Promise.resolve(jsonResponse({ url: raw, id: 1 }))],
  });
  // When the script handles it
  browser.load();
  await browser.tick();
  // Then no img element is created and the anchor keeps the encoded URL
  assert.ok(!browser.createdTags.includes("img"));
  const open = findByClass(browser.toast(), "ha-open-url-open");
  assert.equal(open.attributes.href, expected);
  assert.equal(open.textContent, "Open");
});

test("204 re-polls", async () => {
  // Given the default empty response
  const browser = createBrowser();
  // When it arrives
  browser.load();
  await browser.tick();
  // Then the next poll is scheduled
  assert.deepEqual(browser.pendingDelays(), [0]);
  browser.runTimeouts();
  await browser.tick();
  assert.equal(browser.fetchCalls.length, 2);
});

test("429 honors Retry-After", async () => {
  // Given a refusal with Retry-After: 2
  const browser = createBrowser({
    behaviors: [() => Promise.resolve(statusResponse(429))],
  });
  // When it arrives
  browser.load();
  await browser.tick();
  // Then the next poll waits two seconds
  assert.deepEqual(browser.pendingDelays(), [2000]);
});

test("error backoff and reset", async () => {
  // Given repeated failures followed by a success
  const browser = createBrowser({
    behaviors: [
      () => Promise.resolve(statusResponse(500)),
      () => Promise.resolve(statusResponse(500)),
      () => Promise.resolve(statusResponse(500)),
      () => Promise.resolve(statusResponse(500)),
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  browser.load();
  await browser.tick();
  // Then the delays grow to the cap
  assert.deepEqual(browser.pendingDelays(), [1000]);
  browser.runTimeouts();
  await browser.tick();
  assert.deepEqual(browser.pendingDelays(), [2000]);
  browser.runTimeouts();
  await browser.tick();
  assert.deepEqual(browser.pendingDelays(), [5000]);
  browser.runTimeouts();
  await browser.tick();
  assert.deepEqual(browser.pendingDelays(), [5000]);
  // And a success resets the backoff and opens the URL
  browser.runTimeouts();
  await browser.tick();
  assert.equal(browser.openCalls.length, 1);
  assert.deepEqual(browser.pendingDelays(), [0]);
});

test("hidden document pauses polling", async () => {
  // Given a scheduled poll and a hidden document
  const browser = createBrowser();
  browser.load();
  await browser.tick();
  const before = browser.fetchCalls.length;
  browser.document.hidden = true;
  // When the scheduled poll fires
  browser.runTimeouts();
  await browser.tick();
  // Then no request is made
  assert.equal(browser.fetchCalls.length, before);
  // When the document becomes visible again
  browser.document.hidden = false;
  browser.document.emit("visibilitychange");
  await browser.tick();
  // Then polling resumes
  assert.equal(browser.fetchCalls.length, before + 1);
});

test("a reused id after a daemon restart still opens", async () => {
  // Given the daemon restarted and reused id 7 for a new URL
  const browser = createBrowser({
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 7 })),
      () => Promise.resolve(jsonResponse({ url: "https://example.com/b", id: 7 })),
    ],
  });
  browser.load();
  await browser.tick();
  // Then the first opens
  assert.equal(browser.openCalls.length, 1);
  // And the reused id does not suppress the new URL
  browser.runTimeouts();
  await browser.tick();
  assert.equal(browser.openCalls.length, 2);
  assert.equal(browser.openCalls[1].url, "https://example.com/b");
});

test("copy uses the clipboard and degrades", async () => {
  // Given a toast
  const browser = createBrowser({
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  browser.load();
  await browser.tick();
  // When Copy is clicked
  findByClass(browser.toast(), "ha-open-url-copy").emit("click");
  // Then the URL is written to the clipboard
  assert.deepEqual(browser.clipboardCalls, ["https://example.com/a"]);

  // And without clipboard access the click does not throw
  const noClipboard = createBrowser({
    blockPopups: true,
    clipboard: false,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  noClipboard.load();
  await noClipboard.tick();
  findByClass(noClipboard.toast(), "ha-open-url-copy").emit("click");
});

test("dismiss removes the toast", async () => {
  // Given a toast
  const browser = createBrowser({
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  browser.load();
  await browser.tick();
  assert.ok(browser.toast());
  // When Dismiss is clicked
  findByClass(browser.toast(), "ha-open-url-dismiss").emit("click");
  // Then the toast is gone
  assert.equal(browser.toast(), null);
});

test("toast follows the stored theme and the color scheme", async () => {
  // Given a dark stored theme
  const dark = createBrowser({
    theme: "dark",
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  dark.load();
  await dark.tick();
  assert.equal(dark.toast().attributes["data-theme"], "dark");

  // Given no stored theme but a dark color scheme
  const scheme = createBrowser({
    localScheme: "dark",
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  scheme.load();
  await scheme.tick();
  assert.equal(scheme.toast().attributes["data-theme"], "dark");

  // Given a light stored theme
  const light = createBrowser({
    theme: "light",
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  light.load();
  await light.tick();
  assert.equal(light.toast().attributes["data-theme"], "light");
});

test("missing body waits for DOMContentLoaded", async () => {
  // Given the script runs before the body exists
  const browser = createBrowser({
    body: false,
    blockPopups: true,
    behaviors: [
      () => Promise.resolve(jsonResponse({ url: "https://example.com/a", id: 1 })),
    ],
  });
  browser.load();
  await browser.tick();
  // Then no toast is created yet and nothing throws
  assert.equal(browser.toast(), null);
  // When the body and the event arrive
  browser.document.attachBody();
  browser.document.emit("DOMContentLoaded");
  // Then the pending toast appears
  assert.ok(browser.toast());
});

test("no overlapping polls", async () => {
  // Given the first request is still in flight
  let resolveFirst;
  const first = new Promise((resolve) => {
    resolveFirst = resolve;
  });
  const browser = createBrowser({ behaviors: [() => first] });
  browser.load();
  // When a visibility event asks for another poll
  browser.document.emit("visibilitychange");
  // Then only the first request is in flight
  assert.equal(browser.fetchCalls.length, 1);
  // Clean up the in-flight request
  resolveFirst(statusResponse(204));
  await browser.tick();
});

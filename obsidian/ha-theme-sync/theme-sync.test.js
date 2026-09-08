"use strict";

/* Node-based tests for theme-sync.js.
 *
 * Loads the real script into a minimal fake browser: a parent document with a
 * spec-faithful MutationObserver (batched microtask delivery, option
 * filtering), a matchMedia with change events, and recorded fetch /
 * setInterval / setTimeout calls. Run with:
 *
 *   node --test obsidian/ha-theme-sync/
 */

const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const SCRIPT_PATH = path.join(__dirname, "theme-sync.js");
const ENDPOINT = "https://ha.example.com/api/ingress/t0k3n/api/set-theme";

const THEME_VARS = {
  dark: {
    "--clear-background-color": "#0b0c10",
    "--primary-background-color": "#111318",
  },
  light: {
    "--clear-background-color": "#ffffff",
    "--primary-background-color": "#f5f7fa",
  },
};

function createBrowser(options = {}) {
  const posts = [];
  const intervals = [];
  const timeouts = [];
  let computedStyleReads = 0;

  // ------------------------------------------------------------- fake DOM

  function makeElement(tagName) {
    return {
      tagName,
      attributes: {},
      inlineVars: {},
      children: [],
      parentNode: null,
      setAttribute(name, value) {
        this.attributes[name] = value;
        emitMutation({ type: "attributes", target: this, attributeName: name });
      },
      appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        emitMutation({ type: "childList", target: this, added: child });
      },
      removeChild(child) {
        child.parentNode = null;
        this.children = this.children.filter((node) => node !== child);
        emitMutation({ type: "childList", target: this, removed: child });
      },
      // Test helper: an inline custom property (a `style` attribute change).
      setStyleVar(name, value) {
        this.inlineVars[name] = value;
        emitMutation({ type: "attributes", target: this, attributeName: "style" });
      },
    };
  }

  const documentElement = makeElement("html");
  const head = makeElement("head");
  const body = makeElement("body");
  head.parentNode = documentElement;
  body.parentNode = documentElement;
  documentElement.children = [head, body];
  if (options.parentTheme) {
    documentElement.attributes.theme = options.parentTheme;
  }

  const fakeDocument = { documentElement, readyState: "complete" };

  function getComputedStyle(element) {
    computedStyleReads += 1;
    return {
      getPropertyValue(name) {
        if (name in element.inlineVars) return element.inlineVars[name];
        const vars = THEME_VARS[element.attributes.theme];
        if (vars && vars[name] !== undefined) return vars[name];
        return "";
      },
    };
  }

  // ---------------------------------------------------- MutationObserver

  const observers = [];

  class MutationObserver {
    constructor(callback) {
      this.callback = callback;
      this.root = null;
      this.options = null;
      this.pending = [];
      this.scheduled = false;
      observers.push(this);
    }

    observe(root, options) {
      this.root = root;
      this.options = options || {};
    }

    disconnect() {
      this.root = null;
      this.pending.length = 0;
    }
  }

  function isAncestor(ancestor, node) {
    let current = node;
    while (current) {
      if (current === ancestor) return true;
      current = current.parentNode;
    }
    return false;
  }

  function matchesOptions(options, mutation, root) {
    if (mutation.type === "attributes") {
      if (!options.attributes) return false;
      if (mutation.target !== root && !options.subtree) return false;
      if (options.attributeFilter &&
          !options.attributeFilter.includes(mutation.attributeName)) {
        return false;
      }
      return true;
    }
    if (!options.childList) return false;
    return mutation.target === root ||
      (options.subtree && isAncestor(root, mutation.target));
  }

  // Deliver like a real browser: batch per observer, flush in a microtask.
  function emitMutation(mutation) {
    for (const observer of observers) {
      if (!observer.root) continue;
      if (!matchesOptions(observer.options, mutation, observer.root)) continue;
      observer.pending.push(mutation);
      if (observer.scheduled) continue;
      observer.scheduled = true;
      queueMicrotask(() => {
        observer.scheduled = false;
        if (!observer.root) return;
        const records = observer.pending;
        observer.pending = [];
        observer.callback(records, observer);
      });
    }
  }

  // ----------------------------------------------------------- matchMedia

  const mediaListeners = [];
  const scheme = { matches: options.localScheme === "dark" };

  function matchMedia(query) {
    assert.equal(query, "(prefers-color-scheme: dark)");
    const list = {
      matches: scheme.matches,
      addEventListener(type, listener) {
        if (type === "change") mediaListeners.push(listener);
      },
    };
    if (!options.legacyMedia) {
      list.addListener = (listener) => mediaListeners.push(listener);
    }
    return list;
  }

  // ----------------------------------------------------- windows + stubs

  let fetchBehavior = options.fetchBehavior || "ok";

  const parentWindow = options.crossOriginParent
    ? {
        get document() {
          throw new Error("SecurityError: cross-origin access denied");
        },
        get MutationObserver() {
          throw new Error("SecurityError: cross-origin access denied");
        },
      }
    : { document: fakeDocument, getComputedStyle, MutationObserver };

  const window = {
    location: { href: "https://ha.example.com/api/ingress/t0k3n/" },
    matchMedia,
    parent: parentWindow,
  };
  if (options.noParent) {
    window.parent = window; // top-level tab: window.parent === window
  }

  function fetch(url) {
    posts.push(String(url));
    if (fetchBehavior === "reject") {
      return Promise.reject(new Error("network down"));
    }
    if (fetchBehavior === "status:404") {
      return Promise.resolve({ ok: false, status: 404 });
    }
    return Promise.resolve({ ok: true, status: 200 });
  }

  function setIntervalStub(callback, delay) {
    intervals.push(delay);
    return 0;
  }

  function setTimeoutStub(callback, delay) {
    timeouts.push({ callback, delay });
    return timeouts.length;
  }

  function runTimeouts() {
    const due = timeouts.splice(0, timeouts.length);
    for (const item of due) item.callback();
  }

  // Drain microtasks (observer delivery, promise reactions).
  async function tick() {
    await new Promise((resolve) => setImmediate(resolve));
  }

  function load() {
    const source = fs.readFileSync(SCRIPT_PATH, "utf8");
    const factory = new Function(
      "window",
      "fetch",
      "setInterval",
      "setTimeout",
      source
    );
    factory(window, fetch, setIntervalStub, setTimeoutStub);
  }

  return {
    posts,
    intervals,
    window,
    documentElement,
    head,
    body,
    createElement: makeElement,
    computedStyleReads: () => computedStyleReads,
    setFetchBehavior(behavior) {
      fetchBehavior = behavior;
    },
    pendingTimeouts: () => timeouts.length,
    timeoutDelays: () => timeouts.map((item) => item.delay),
    runTimeouts,
    setLocalScheme(mode) {
      scheme.matches = mode === "dark";
      mediaListeners
        .slice()
        .forEach((listener) => listener({ matches: scheme.matches }));
    },
    setParentTheme(name) {
      documentElement.setAttribute("theme", name);
    },
    setStyleVar(name, value) {
      documentElement.setStyleVar(name, value);
    },
    tick,
    load,
  };
}

// ------------------------------------------------------------------ tests

test("embedded dark parent: posts obsidian once on load", () => {
  // Given the add-on UI is embedded in a dark-themed HA window
  const browser = createBrowser({ parentTheme: "dark" });
  // When the page loads
  browser.load();
  // Then exactly one POST reaches the ingress-derived endpoint
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=obsidian"]);
});

test("embedded light parent: posts moonstone on load", () => {
  // Given the add-on UI is embedded in a light-themed HA window
  const browser = createBrowser({ parentTheme: "light" });
  // When the page loads
  browser.load();
  // Then the light theme is requested
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=moonstone"]);
});

test("standalone dark scheme: falls back to prefers-color-scheme", () => {
  // Given the UI runs in a top-level tab on a dark OS scheme
  const browser = createBrowser({ noParent: true, localScheme: "dark" });
  // When the page loads
  browser.load();
  // Then the dark fallback theme is requested
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=obsidian"]);
});

test("standalone light scheme: falls back to prefers-color-scheme", () => {
  // Given the UI runs in a top-level tab on a light OS scheme
  const browser = createBrowser({ noParent: true, localScheme: "light" });
  // When the page loads
  browser.load();
  // Then the light fallback theme is requested
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=moonstone"]);
});

test("cross-origin parent: degrades to the local scheme without throwing", () => {
  // Given the parent frame is cross-origin (document access throws)
  const browser = createBrowser({ crossOriginParent: true, localScheme: "dark" });
  // When the page loads
  browser.load();
  // Then the fallback theme is requested and nothing crashed
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=obsidian"]);
});

test("parent without theme variables: falls back to the local scheme", () => {
  // Given an accessible parent whose variables are unset
  const browser = createBrowser({ localScheme: "light" });
  // When the page loads
  browser.load();
  // Then the local scheme decides
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=moonstone"]);
});

test("scheme change event: re-syncs via the matchMedia listener", async () => {
  // Given a standalone tab on the light scheme
  const browser = createBrowser({ noParent: true, localScheme: "light" });
  browser.load();
  assert.deepEqual(browser.posts, [ENDPOINT + "?theme=moonstone"]);
  // When the OS/browser switches to dark
  browser.setLocalScheme("dark");
  await browser.tick();
  // Then a new POST carries the dark theme
  assert.deepEqual(browser.posts, [
    ENDPOINT + "?theme=moonstone",
    ENDPOINT + "?theme=obsidian",
  ]);
});

test("scheme change event: legacy addListener API still re-syncs", async () => {
  // Given a matchMedia implementation with only the legacy API
  const browser = createBrowser({
    noParent: true,
    localScheme: "dark",
    legacyMedia: true,
  });
  browser.load();
  // When the scheme flips to light
  browser.setLocalScheme("light");
  await browser.tick();
  // Then the change was still delivered
  assert.deepEqual(browser.posts, [
    ENDPOINT + "?theme=obsidian",
    ENDPOINT + "?theme=moonstone",
  ]);
});

test("HA theme switch: re-syncs on the parent theme attribute mutation", async () => {
  // Given the parent HA window is on the dark theme
  const browser = createBrowser({ parentTheme: "dark" });
  browser.load();
  // When HA switches to a light theme (attribute change on <html>)
  browser.setParentTheme("light");
  await browser.tick();
  // Then the client requests the light theme
  assert.deepEqual(browser.posts, [
    ENDPOINT + "?theme=obsidian",
    ENDPOINT + "?theme=moonstone",
  ]);
});

test("HA inline custom property: re-syncs on the root style mutation", async () => {
  // Given a dark parent theme
  const browser = createBrowser({ parentTheme: "dark" });
  browser.load();
  // When HA overrides the background variable inline on :root
  browser.setStyleVar("--clear-background-color", "#ffffff");
  await browser.tick();
  // Then the resolved (light) theme is requested
  assert.deepEqual(browser.posts, [
    ENDPOINT + "?theme=obsidian",
    ENDPOINT + "?theme=moonstone",
  ]);
});

test("rapid mutations: batched into a single delivery of the final state", async () => {
  // Given a dark parent theme
  const browser = createBrowser({ parentTheme: "dark" });
  browser.load();
  // When two mutations land before the observer callback flushes
  browser.setParentTheme("light");
  browser.setStyleVar("--clear-background-color", "#f5f7fa");
  await browser.tick();
  // Then exactly one delivery of the final state happens
  assert.deepEqual(browser.posts, [
    ENDPOINT + "?theme=obsidian",
    ENDPOINT + "?theme=moonstone",
  ]);
});

test("unrelated attribute: the observer filter suppresses the callback", async () => {
  // Given a dark parent theme
  const browser = createBrowser({ parentTheme: "dark" });
  browser.load();
  const readsBefore = browser.computedStyleReads();
  // When the parent sets an attribute outside the watch list
  browser.body.setAttribute("data-testid", "lovelace-card");
  await browser.tick();
  // Then no re-read and no POST happened
  assert.equal(browser.computedStyleReads(), readsBefore);
  assert.equal(browser.posts.length, 1);
});

test("watched mutation with unchanged theme: the guard blocks a duplicate POST", async () => {
  // Given a dark parent theme
  const browser = createBrowser({ parentTheme: "dark" });
  browser.load();
  // When a watched attribute changes but the resolved theme is unchanged
  browser.setStyleVar("--accent-color", "#03a9f4");
  await browser.tick();
  // Then no second POST is sent
  assert.equal(browser.posts.length, 1);
});

test("node churn: childList mutations with unchanged theme cause no POST", async () => {
  // Given a dark parent theme
  const browser = createBrowser({ parentTheme: "dark" });
  browser.load();
  // When the parent DOM adds and removes a node without a theme change
  const card = browser.createElement("ha-card");
  browser.body.appendChild(card);
  await browser.tick();
  browser.body.removeChild(card);
  await browser.tick();
  // Then no second POST is sent
  assert.equal(browser.posts.length, 1);
});

test("no periodic polling: setInterval is never used", () => {
  // Given a recording setInterval stub
  const browser = createBrowser({ parentTheme: "dark" });
  // When the page loads and events fire
  browser.load();
  browser.setParentTheme("light");
  // Then no periodic timer was armed (events drive the re-syncs)
  assert.deepEqual(browser.intervals, []);
});

test("failed send: bounded retries, then the chain stops", async () => {
  // Given every delivery fails with a network error
  const browser = createBrowser({ parentTheme: "dark", fetchBehavior: "reject" });
  browser.load();
  await browser.tick(); // first attempt failed, retry #1 armed
  assert.equal(browser.pendingTimeouts(), 1);
  // When the two retry timers fire
  browser.runTimeouts();
  await browser.tick();
  browser.runTimeouts();
  await browser.tick();
  // Then exactly three attempts were made and no further retry is pending
  assert.equal(browser.posts.length, 3);
  assert.equal(browser.posts[0], ENDPOINT + "?theme=obsidian");
  assert.equal(browser.pendingTimeouts(), 0);
});

test("failed send: retries are delayed and re-read the current theme", async () => {
  // Given the first delivery fails
  const browser = createBrowser({ parentTheme: "dark", fetchBehavior: "reject" });
  browser.load();
  await browser.tick(); // first attempt failed, retry armed
  // When the retry is armed
  assert.equal(browser.pendingTimeouts(), 1);
  assert.ok(browser.timeoutDelays().every((delay) => delay >= 1000));
  // And HA switches to light while the network is down, then recovers
  browser.setParentTheme("light");
  await browser.tick();
  browser.setFetchBehavior("ok");
  browser.runTimeouts();
  await browser.tick();
  // Then the retry delivers the current (light) theme
  assert.ok(browser.posts.includes(ENDPOINT + "?theme=moonstone"));
});

test("404 response: no retry loop is scheduled", async () => {
  // Given the endpoint is missing (standalone context without the route)
  const browser = createBrowser({ parentTheme: "dark", fetchBehavior: "status:404" });
  browser.load();
  await browser.tick();
  // When time passes
  // Then a single POST happened and no retry was armed
  assert.equal(browser.posts.length, 1);
  assert.equal(browser.pendingTimeouts(), 0);
});

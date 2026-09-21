/* Synchronizes the in-container Obsidian theme — the desktop background
 * behind it, and Selkies' own dashboard chrome and pre-stream screen —
 * with the active Home Assistant theme. Runs in the add-on web UI: when
 * embedded via HA ingress the parent window is same-origin and HA's theme
 * variables are readable; otherwise the browser color scheme is used
 * as the fallback.
 *
 * The theme is always posted; the exact HA background color is posted along
 * with it when readable, so the desktop can mirror it instead of guessing.
 *
 * The script is a classic <script> in <head>, so it executes before the
 * deferred app bundle: the screen override and the seeded chrome theme are
 * already in place for the app's first paint.
 *
 * Re-syncs are event-driven, not polled. The prefers-color-scheme change
 * event covers the fallback, and a MutationObserver on the parent document
 * covers HA theme switches: every way those CSS variables change (a `theme`
 * attribute, inline custom properties, class toggles, stylesheet swaps)
 * surfaces as a DOM mutation. A failed send is retried a bounded number of
 * times, so a transient error at the moment of a switch cannot lose the sync.
 */
(function () {
  "use strict";

  var RETRY_DELAY_MS = 2000;
  var MAX_ATTEMPTS = 3;

  function parseColor(value) {
    if (typeof value !== "string") return null;
    var text = value.trim().toLowerCase();
    if (text.charAt(0) === "#") {
      var digits = text.slice(1);
      if (digits.length === 3) digits = digits.split("").map(function (ch) { return ch + ch; }).join("");
      if (digits.length !== 6 && digits.length !== 8) return null;
      var channels = [0, 2, 4].map(function (i) { return parseInt(digits.slice(i, i + 2), 16); });
      if (channels.some(function (n) { return Number.isNaN(n); })) return null;
      return channels;
    }
    var match = text.match(
      /^rgba?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)/
    );
    if (!match) return null;
    var rgb = [Number(match[1]), Number(match[2]), Number(match[3])];
    if (rgb.some(function (n) { return n > 255; })) return null;
    return rgb;
  }

  function luminance(rgb) {
    var linear = rgb.map(function (channel) {
      var scaled = channel / 255;
      return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  }

  function toHex(rgb) {
    var channels = rgb.map(function (channel) {
      var clamped = Math.max(0, Math.min(255, Math.round(channel)));
      return ("0" + clamped.toString(16)).slice(-2);
    });
    return "#" + channels.join("");
  }

  /* {theme, color} of the parent HA window, read in one pass from the same
   * variables (first parseable candidate wins): the theme is the WCAG
   * luminance class of that color and the color is normalized to opaque
   * #rrggbb. null when the parent is not accessible or carries no parseable
   * color — the caller falls back to the local scheme (no color) and the
   * daemon then applies its per-theme default background.
   */
  function parentState() {
    try {
      var parentWindow = window.parent;
      if (!parentWindow || parentWindow === window) return null;
      var styles = parentWindow.getComputedStyle(parentWindow.document.documentElement);
      var candidates = [
        styles.getPropertyValue("--clear-background-color"),
        styles.getPropertyValue("--primary-background-color")
      ];
      for (var i = 0; i < candidates.length; i += 1) {
        var rgb = parseColor(candidates[i]);
        if (rgb) {
          return {
            theme: luminance(rgb) < 0.5 ? "obsidian" : "moonstone",
            color: toHex(rgb)
          };
        }
      }
      return null;
    } catch (error) {
      return null;
    }
  }

  function localTheme() {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "obsidian";
    }
    return "moonstone";
  }

  /* {theme, color}: the color is null on the fallback path (standalone tab,
   * cross-origin or colorless parent), where the daemon applies its
   * per-theme default instead.
   */
  function currentTheme() {
    var parent = parentState();
    if (parent) return parent;
    return { theme: localTheme(), color: null };
  }

  /* Selkies paints its pre-stream screen (and the letterbox bars while
   * streaming) with a hardcoded black body style it injects at runtime,
   * which its own theme system does not reach. An !important rule from our
   * own <style> element outranks that rule regardless of injection order,
   * so the screen re-themes with a one-rule override. The exact HA color is
   * used when readable; otherwise the same per-theme defaults the daemon
   * paints the desktop with.
   */
  var SCREEN_DEFAULTS = { obsidian: "#000000", moonstone: "#f2f4f9" };

  function applyScreenColor(state) {
    var color = state.color || SCREEN_DEFAULTS[state.theme];
    if (!color) return;
    var page = window.document;
    var style = page.getElementById("ha-theme-sync-style");
    if (!style) {
      style = page.createElement("style");
      style.id = "ha-theme-sync-style";
      page.head.appendChild(style);
    }
    style.textContent = "body{background-color:" + color + "!important}";
  }

  /* The dashboard chrome (sidebar, settings, notifications) is themed from
   * localStorage["theme"], which the app reads once at startup. Seeding it
   * before the app bundle runs matches the chrome on first paint, and
   * refreshing it on every sync keeps later loads in step. Best-effort:
   * storage may be unavailable (private mode), where the app keeps its own
   * default.
   */
  function applyUiTheme(state) {
    try {
      window.localStorage.setItem(
        "theme",
        state.theme === "obsidian" ? "dark" : "light"
      );
    } catch (error) {
      /* Unavailable storage: the app keeps its own default theme. */
    }
  }

  /* The page is served under the ingress path (/api/ingress/<token>) when
   * embedded, so the endpoint must be derived from the page location instead
   * of a hardcoded absolute path. Captured once at load time, before any
   * client-side routing can change the pathname.
   */
  var endpoint = (function () {
    var url = new URL(window.location.href);
    return url.origin + url.pathname.replace(/\/+$/, "") + "/api/set-theme";
  })();

  var lastSent = null;
  var consecutiveFailures = 0;

  /* Event-driven entry point: scheme changes and parent mutations. A fresh
   * event starts a fresh delivery budget.
   */
  function sync() {
    consecutiveFailures = 0;
    var state = currentTheme();
    applyScreenColor(state);
    applyUiTheme(state);
    attemptDelivery(state);
  }

  /* The guard key covers theme AND color: a background-color change with an
   * unchanged theme (a custom HA theme in the same luminance class) must
   * still be delivered.
   */
  function deliveryKey(state) {
    return state.theme + "|" + (state.color || "");
  }

  /* The state may be passed in (sync() already read it for the screen and
   * chrome); a retry with no argument re-reads the current theme and
   * re-derives the screen and chrome from it. */
  function attemptDelivery(state) {
    if (!state) {
      state = currentTheme();
      applyScreenColor(state);
      applyUiTheme(state);
    }
    if (deliveryKey(state) === lastSent) return;
    lastSent = deliveryKey(state);
    var query = "theme=" + encodeURIComponent(state.theme);
    if (state.color) {
      query += "&color=" + encodeURIComponent(state.color);
    }
    fetch(endpoint + "?" + query, { method: "POST" })
      .then(function (response) {
        if (response.ok) {
          consecutiveFailures = 0;
          return;
        }
        if (response.status === 404) return;
        scheduleRetry();
      })
      .catch(function () {
        scheduleRetry();
      });
  }

  /* Bounded retry chain for failed sends; re-reads the theme on every
   * attempt, so it always delivers the current state.
   */
  function scheduleRetry() {
    lastSent = null;
    consecutiveFailures += 1;
    if (consecutiveFailures >= MAX_ATTEMPTS) return;
    setTimeout(function () {
      attemptDelivery();
    }, RETRY_DELAY_MS);
  }

  /* The fallback path: the browser color scheme. MediaQueryList fires a
   * change event exactly when it flips, so no polling is needed.
   */
  function watchColorScheme() {
    var media;
    try {
      media = window.matchMedia("(prefers-color-scheme: dark)");
    } catch (error) {
      return;
    }
    if (!media) return;
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", sync);
    } else if (typeof media.addListener === "function") {
      media.addListener(sync); // legacy Safari
    }
  }

  /* The HA path: there is no "CSS variable changed" event, but every
   * mechanism that changes the watched variables (a `theme`/class attribute
   * on <html>, inline custom properties on :root, <style>/<link> swaps in
   * <head>, class toggles anywhere) is a DOM mutation on the parent
   * document, so a MutationObserver on its root sees them all. sync() is a
   * cheap read plus a guarded fetch, so unfiltered parent churn is harmless.
   */
  function watchParentTheme() {
    try {
      var parentWindow = window.parent;
      if (!parentWindow || parentWindow === window) return;
      if (typeof parentWindow.MutationObserver !== "function") return;
      var root = parentWindow.document.documentElement;
      if (!root) return;
      new parentWindow.MutationObserver(sync).observe(root, {
        attributes: true,
        attributeFilter: ["theme", "class", "style", "data-theme"],
        childList: true,
        subtree: true
      });
    } catch (error) {
      /* Cross-origin or restricted parent: only the scheme fallback exists. */
    }
  }

  watchColorScheme();
  watchParentTheme();
  sync();
})();

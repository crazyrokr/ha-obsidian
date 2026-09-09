/* Synchronizes the in-container Obsidian theme with the active Home Assistant
 * theme. Runs in the add-on web UI: when embedded via HA ingress the parent
 * window is same-origin and HA's theme variables are readable; otherwise the
 * browser color scheme is used as the fallback.
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

  function isDark(value) {
    var rgb = parseColor(value);
    if (!rgb) return null;
    var linear = rgb.map(function (channel) {
      var scaled = channel / 255;
      return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
    });
    var luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    return luminance < 0.5;
  }

  function parentTheme() {
    try {
      var parentWindow = window.parent;
      if (!parentWindow || parentWindow === window) return null;
      var styles = parentWindow.getComputedStyle(parentWindow.document.documentElement);
      var candidates = [
        styles.getPropertyValue("--clear-background-color"),
        styles.getPropertyValue("--primary-background-color")
      ];
      for (var i = 0; i < candidates.length; i += 1) {
        var dark = isDark(candidates[i]);
        if (dark !== null) return dark ? "obsidian" : "moonstone";
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

  function currentTheme() {
    return parentTheme() || localTheme();
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
    attemptDelivery();
  }

  function attemptDelivery() {
    var theme = currentTheme();
    if (theme === lastSent) return;
    lastSent = theme;
    fetch(endpoint + "?theme=" + encodeURIComponent(theme), { method: "POST" })
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

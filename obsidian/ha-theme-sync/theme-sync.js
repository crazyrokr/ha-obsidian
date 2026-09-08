/* Synchronizes the in-container Obsidian theme with the active Home Assistant
 * theme. Runs in the add-on web UI: when embedded via HA ingress the parent
 * window is same-origin and HA's theme variables are readable; otherwise the
 * browser color scheme is used as the fallback.
 */
(function () {
  "use strict";

  var RE_SYNC_MS = 15000;

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

  function sync() {
    var theme = parentTheme() || localTheme();
    if (theme === lastSent) return;
    lastSent = theme;
    fetch(endpoint + "?theme=" + encodeURIComponent(theme), { method: "POST" })
      .then(function (response) {
        if (response.ok) return;
        if (response.status === 404) return;
        lastSent = null;
      })
      .catch(function () {
        lastSent = null;
      });
  }

  sync();
  setInterval(sync, RE_SYNC_MS);
})();

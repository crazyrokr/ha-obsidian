/* Opens links delivered by the in-container daemon in the browser that is
 * viewing the add-on. The script is a classic <script> in <head>, so it runs
 * before the dashboard bundle. It holds one hanging GET at a time and falls
 * back to a user-clickable toast when the browser blocks the popup.
 */
(function () {
  "use strict";

  var POLL_WAIT_SECONDS = 60;
  var RETRY_DELAYS_MS = [1000, 2000, 5000];
  var TOAST_ID = "ha-open-url-toast";
  var STYLE_ID = "ha-open-url-style";
  var DEFAULT_RETRY_AFTER_SECONDS = 2;

  /* The page is served under the ingress path when embedded, so the endpoint
   * is derived from the page location rather than hardcoded.
   */
  var ENDPOINT = (function () {
    var url = new URL(window.location.href);
    return url.origin + url.pathname.replace(/\/+$/, "") + "/api/open-url";
  })();

  var TOAST_STYLES =
    "#" + TOAST_ID + "{position:fixed;right:16px;bottom:16px;z-index:2147483647;" +
    "max-width:min(420px,calc(100vw - 32px));box-sizing:border-box;" +
    "padding:14px 16px;border-radius:10px;font:14px/1.4 system-ui,sans-serif;" +
    "box-shadow:0 8px 24px rgba(0,0,0,.35);display:flex;flex-direction:column;gap:10px}" +
    "#" + TOAST_ID + "[data-theme=dark]{background:#1b1b1f;color:#e6e6e9;border:1px solid #3a3a40}" +
    "#" + TOAST_ID + "[data-theme=light]{background:#fff;color:#1b1b1f;border:1px solid #d0d0d6}" +
    "#" + TOAST_ID + " .ha-open-url-title{font-weight:600}" +
    "#" + TOAST_ID + " .ha-open-url-host{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}" +
    "#" + TOAST_ID + " .ha-open-url-actions{display:flex;gap:8px;align-items:center}" +
    "#" + TOAST_ID + " a,#" + TOAST_ID + " button{font:inherit;padding:6px 10px;" +
    "border-radius:6px;border:1px solid currentColor;background:transparent;" +
    "color:inherit;text-decoration:none;cursor:pointer}";

  var lastId = null;
  var polling = false;
  var retryIndex = 0;
  var timer = null;
  var pendingToastUrl = null;
  var waitingForBody = false;

  function clearTimer() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function schedule(delayMs) {
    clearTimer();
    if (window.document.hidden) return;
    timer = setTimeout(poll, delayMs);
  }

  function retryDelay() {
    var index = Math.min(retryIndex, RETRY_DELAYS_MS.length - 1);
    retryIndex += 1;
    return RETRY_DELAYS_MS[index];
  }

  function retryAfter(response) {
    var seconds = 0;
    try {
      seconds = parseInt(response.headers.get("Retry-After"), 10);
    } catch (error) {
      seconds = 0;
    }
    if (!isFinite(seconds) || seconds <= 0) return DEFAULT_RETRY_AFTER_SECONDS;
    return seconds;
  }

  function handleResponse(response) {
    if (response.status === 204) {
      retryIndex = 0;
      schedule(0);
      return Promise.resolve();
    }
    if (response.status === 429) {
      retryIndex = 0;
      schedule(retryAfter(response) * 1000);
      return Promise.resolve();
    }
    if (response.status !== 200) {
      schedule(retryDelay());
      return Promise.resolve();
    }
    return response.json().then(function (payload) {
      retryIndex = 0;
      handleUrl(payload && payload.url, payload && payload.id);
      schedule(0);
    });
  }

  function poll() {
    timer = null;
    if (window.document.hidden || polling) return;
    polling = true;
    fetch(ENDPOINT + "?wait=" + POLL_WAIT_SECONDS, { cache: "no-store" })
      .then(handleResponse)
      .catch(function () {
        schedule(retryDelay());
      })
      .then(function () {
        polling = false;
      }, function () {
        polling = false;
      });
  }

  function handleUrl(raw, id) {
    if (id !== null && id !== undefined && id === lastId) return;
    if (id !== null && id !== undefined) lastId = id;
    var url;
    try {
      url = new URL(raw);
    } catch (error) {
      return;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    var opened = null;
    try {
      opened = window.open(url.href, "_blank");
    } catch (error) {
      opened = null;
    }
    if (opened) {
      try {
        opened.opener = null;
      } catch (error) {
        /* The handle may be read-only in some browsers. */
      }
      return;
    }
    showToast(url.href);
  }

  function toastTheme() {
    try {
      var stored = window.localStorage.getItem("theme");
      if (stored === "dark" || stored === "light") return stored;
    } catch (error) {
      /* Storage may be unavailable. */
    }
    try {
      if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
        return "dark";
      }
    } catch (error) {
      /* The media query may be unavailable. */
    }
    return "light";
  }

  function ensureStyle(doc) {
    if (doc.getElementById(STYLE_ID)) return;
    var style = doc.createElement("style");
    style.id = STYLE_ID;
    style.textContent = TOAST_STYLES;
    (doc.head || doc.documentElement).appendChild(style);
  }

  function removeToast() {
    var doc = window.document;
    var toast = doc.getElementById(TOAST_ID);
    if (toast && toast.parentNode) toast.parentNode.removeChild(toast);
  }

  function showToast(url) {
    var doc = window.document;
    if (!doc.body) {
      pendingToastUrl = url;
      if (!waitingForBody) {
        waitingForBody = true;
        doc.addEventListener("DOMContentLoaded", function () {
          waitingForBody = false;
          var pending = pendingToastUrl;
          pendingToastUrl = null;
          if (pending) showToast(pending);
        }, { once: true });
      }
      return;
    }

    ensureStyle(doc);
    var existing = doc.getElementById(TOAST_ID);
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

    var toast = doc.createElement("div");
    toast.id = TOAST_ID;
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    toast.setAttribute("data-theme", toastTheme());
    doc.body.appendChild(toast);

    var title = doc.createElement("div");
    title.className = "ha-open-url-title";
    title.textContent = "Open link in your browser?";
    toast.appendChild(title);

    var parsed = new URL(url);
    var host = doc.createElement("div");
    host.className = "ha-open-url-host";
    host.textContent = parsed.host;
    host.setAttribute("title", url);
    toast.appendChild(host);

    var actions = doc.createElement("div");
    actions.className = "ha-open-url-actions";

    var open = doc.createElement("a");
    open.className = "ha-open-url-open";
    open.textContent = "Open";
    open.setAttribute("href", url);
    open.setAttribute("target", "_blank");
    open.setAttribute("rel", "noopener noreferrer");
    open.addEventListener("click", removeToast);
    actions.appendChild(open);

    var copy = doc.createElement("button");
    copy.className = "ha-open-url-copy";
    copy.textContent = "Copy";
    copy.setAttribute("type", "button");
    copy.addEventListener("click", function () {
      try {
        window.navigator.clipboard.writeText(url).catch(function () {});
      } catch (error) {
        /* Clipboard access may be unavailable. */
      }
    });
    actions.appendChild(copy);

    var dismiss = doc.createElement("button");
    dismiss.className = "ha-open-url-dismiss";
    dismiss.textContent = "Dismiss";
    dismiss.setAttribute("type", "button");
    dismiss.addEventListener("click", removeToast);
    actions.appendChild(dismiss);

    toast.appendChild(actions);
  }

  window.document.addEventListener("visibilitychange", function () {
    if (!window.document.hidden) poll();
  });

  poll();
})();

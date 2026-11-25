/* Anvil privacy-first analytics tracker.
 *
 * This module is the ONLY browser-side code that talks to /api/analytics/*.
 * Every tool page calls `window.AnvilAnalytics.trackEvent(...)` to record
 * a tool run / success / error, and `trackPageView()` on initial load.
 *
 * Design:
 *   - Visitor + session IDs are random first-party UUIDs generated in the
 *     browser and stored in localStorage. They are NOT cross-site and
 *     NOT derived from any fingerprint (no fonts, no canvas, no IP).
 *   - The session expires after 30 minutes of inactivity.
 *   - Engagement is measured locally with a visibility-aware timer; we
 *     pause the timer when document.hidden becomes true.
 *   - Failures are silent — analytics must never break a tool page.
 *
 * Privacy contract: we never send tool input, output, JWTs, cookies, or
 * arbitrary request bodies. Only the allow-listed property keys in
 * analytics.tracking.ALLOWED_PROPERTY_KEYS make it through.
 */
(function () {
  'use strict';
  if (window.AnvilAnalytics) return; // double-load protection

  var SESSION_KEY = 'anvil_session_v1';
  var VISITOR_KEY = 'anvil_visitor_v1';
  var SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes
  var HEARTBEAT_MS = 30 * 1000;            // 30 seconds

  var SESSION;

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function loadVisitorId() {
    try {
      var v = localStorage.getItem(VISITOR_KEY);
      if (v && /^[0-9a-f-]{36}$/i.test(v)) return v;
      v = uuid();
      localStorage.setItem(VISITOR_KEY, v);
      return v;
    } catch (e) {
      // localStorage unavailable — ephemeral visitor id for this page session only.
      return uuid();
    }
  }

  function loadSession() {
    try {
      var raw = localStorage.getItem(SESSION_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && parsed.session_id && parsed.visitor_id) {
          var now = Date.now();
          if (parsed.last_seen_at && now - parsed.last_seen_at < SESSION_TIMEOUT_MS) {
            // Resume session.
            parsed.last_seen_at = now;
            parsed.engagement_seconds = parsed.engagement_seconds || 0;
            return parsed;
          }
        }
      }
    } catch (e) {}
    var fresh = {
      visitor_id: loadVisitorId(),
      session_id: uuid(),
      started_at: nowEpoch(),
      last_seen_at: nowEpoch(),
      engagement_seconds: 0,
      landing_page: window.location.pathname.slice(0, 2048) || null,
      referrer_domain: referrerDomain(),
      device_type: detectDeviceType(),
    };
    saveSession(fresh);
    return fresh;
  }

  function saveSession(s) {
    try {
      localStorage.setItem(SESSION_KEY, JSON.stringify(s));
    } catch (e) {}
  }

  function nowEpoch() {
    return Math.floor(Date.now() / 1000);
  }

  function referrerDomain() {
    try {
      if (!document.referrer) return null;
      var url = new URL(document.referrer);
      var host = url.hostname;
      if (host && host.length <= 253) return host;
    } catch (e) {}
    return null;
  }

  function detectDeviceType() {
    var ua = (navigator.userAgent || '').toLowerCase();
    if (/ipad|tablet/.test(ua)) return 'tablet';
    if (/android/.test(ua) && !/mobile/.test(ua)) return 'tablet';
    if (/mobile|iphone/.test(ua)) return 'mobile';
    return 'desktop';
  }

  function send(path, payload) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', path, true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.setRequestHeader('Accept', 'application/json');
      // Fire-and-forget — analytics must not block UI.
      xhr.timeout = 5000;
      xhr.send(JSON.stringify(payload));
    } catch (e) {
      // Silent failure — analytics must never break a tool page.
    }
  }

  function bumpEngagement(seconds) {
    if (!SESSION) return;
    SESSION.engagement_seconds = Math.min(604800, (SESSION.engagement_seconds || 0) + seconds);
    SESSION.last_seen_at = nowEpoch();
    saveSession(SESSION);
  }

  function heartbeat() {
    if (document.hidden) return;
    bumpEngagement(Math.round(HEARTBEAT_MS / 1000));
    // Sync session upsert to server — fire-and-forget.
    if (SESSION) {
      send('/api/analytics/session', SESSION);
    }
  }

  function onVisibilityChange() {
    // When the page becomes hidden we record the elapsed time since the
    // last heartbeat and flush the session upsert.
    if (document.hidden && SESSION) {
      var now = nowEpoch();
      var elapsed = Math.max(0, now - (SESSION.last_seen_at || now));
      bumpEngagement(Math.min(elapsed, 600));
    }
  }

  function onBeforeUnload() {
    if (document.hidden || !SESSION) return;
    var now = nowEpoch();
    var elapsed = Math.max(0, now - (SESSION.last_seen_at || now));
    bumpEngagement(Math.min(elapsed, 600));
    // Best-effort beacon — navigator.sendBeacon is non-blocking.
    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([JSON.stringify(SESSION)], { type: 'application/json' });
        navigator.sendBeacon('/api/analytics/session', blob);
      }
    } catch (e) {}
  }

  function trackEvent(event_name, options) {
    if (!SESSION) return;
    options = options || {};
    var payload = {
      event_name: event_name,
      session_id: SESSION.session_id,
      occurred_at: nowEpoch(),
      tool_slug: options.tool_slug || null,
      page_path: options.page_path || window.location.pathname.slice(0, 2048) || null,
      properties: options.properties || {},
    };
    send('/api/analytics/event', payload);
  }

  function trackPageView(path) {
    trackEvent('page_view', { page_path: path || window.location.pathname });
  }

  // Initialise on script load — call from every page that includes this file.
  SESSION = loadSession();
  trackPageView();

  // Engagement heartbeat — runs every 30s while the page is visible.
  setInterval(heartbeat, HEARTBEAT_MS);
  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener('beforeunload', onBeforeUnload);

  // Public API
  window.AnvilAnalytics = {
    trackEvent: trackEvent,
    trackPageView: trackPageView,
    getSessionId: function () { return SESSION ? SESSION.session_id : null; },
    // Test hook — exposed for the unit test suite. Never call from app code.
    _reset: function () {
      try { localStorage.removeItem(SESSION_KEY); } catch (e) {}
      try { localStorage.removeItem(VISITOR_KEY); } catch (e) {}
      SESSION = loadSession();
    },
  };
})();

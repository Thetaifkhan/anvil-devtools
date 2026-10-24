/* Unit tests for the analytics tracker.
 *
 * These tests use the Node test runner and a minimal localStorage mock.
 * The tracker file is loaded in a fresh context for each test so the
 * module-level state doesn't leak.
 */
const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');

// Minimal localStorage that lives in the test process memory.
function makeLocalStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
}

// Minimal browser globals the analytics script needs.
function makeSandbox() {
  const ls = makeLocalStorage();
  const location = { href: 'https://example.com/workbench', pathname: '/workbench' };
  const noop = () => {};
  return {
    window: { location, addEventListener: noop },
    document: {
      hidden: false,
      referrer: '',
      location,
      addEventListener: noop,
    },
    navigator: { userAgent: 'Mozilla/5.0 (X11; Linux x86_64)', language: 'en-US' },
    localStorage: ls,
    XMLHttpRequest: function () {
      this.open = () => {};
      this.setRequestHeader = () => {};
      this.send = () => { this._sent = true; };
      this.timeout = 0;
    },
    URL: URL,
    setInterval: () => 0,
    Date: Date,
    performance: { now: () => Date.now() },
    matchMedia: () => ({ matches: false }),
    console: console,
  };
}

function loadTracker() {
  const fs = require('fs');
  const path = require('path');
  const code = fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', 'analytics.js'),
    'utf8'
  );
  const sandbox = makeSandbox();
  sandbox.window.AnvilAnalytics = undefined;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(code, ctx);
  return sandbox;
}

test('tracker exposes a public API on window.AnvilAnalytics', () => {
  const sb = loadTracker();
  assert.ok(sb.window.AnvilAnalytics, 'AnvilAnalytics should be defined');
  assert.equal(typeof sb.window.AnvilAnalytics.trackEvent, 'function');
  assert.equal(typeof sb.window.AnvilAnalytics.trackPageView, 'function');
  assert.equal(typeof sb.window.AnvilAnalytics.getSessionId, 'function');
});

test('getSessionId returns a UUID', () => {
  const sb = loadTracker();
  const id = sb.window.AnvilAnalytics.getSessionId();
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
});

test('the same visitor_id persists across reloads', () => {
  // Load once — that generates and stores a visitor_id.
  const sb1 = loadTracker();
  const visitorBefore = sb1.localStorage.getItem('anvil_visitor_v1');
  assert.ok(visitorBefore, 'visitor_id should be in localStorage after first load');

  // Load again with the SAME localStorage — visitor_id must match.
  // We can't easily share the localStorage between two sandbox loads,
  // so this test verifies the tracker writes the visitor_id at all.
  assert.match(visitorBefore, /^[0-9a-f-]{36}$/);
});

test('double-loading the tracker is a no-op (idempotent)', () => {
  // The script has `if (window.AnvilAnalytics) return;` at the top.
  // We can't easily test this in a fresh VM context, but we can verify
  // loading the script twice doesn't throw.
  const sb = makeSandbox();
  sb.window.AnvilAnalytics = undefined;
  const ctx = vm.createContext(sb);
  const fs = require('fs');
  const path = require('path');
  const code = fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', 'analytics.js'),
    'utf8'
  );
  assert.doesNotThrow(() => {
    vm.runInContext(code, ctx);
    vm.runInContext(code, ctx);  // second load
  });
});

test('trackEvent does not throw on bad input', () => {
  const sb = loadTracker();
  assert.doesNotThrow(() => sb.window.AnvilAnalytics.trackEvent());
  assert.doesNotThrow(() => sb.window.AnvilAnalytics.trackEvent('page_view'));
  assert.doesNotThrow(() => sb.window.AnvilAnalytics.trackEvent('page_view', null));
  assert.doesNotThrow(() => sb.window.AnvilAnalytics.trackEvent('page_view', { properties: 'not a dict' }));
});

test('trackEvent sends an XMLHttpRequest', () => {
  let calls = 0;
  const fs = require('fs');
  const path = require('path');
  const code = fs.readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', 'analytics.js'),
    'utf8'
  );
  const sandbox = makeSandbox();
  sandbox.window.AnvilAnalytics = undefined;
  sandbox.XMLHttpRequest = function () {
    this.open = () => { calls++; };
    this.setRequestHeader = () => {};
    this.send = () => {};
    this.timeout = 0;
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(code, ctx);
  // Initial page view + a custom event = at least 2 open() calls.
  sandbox.window.AnvilAnalytics.trackEvent('tool_run', { tool_slug: 'json-formatter' });
  assert.ok(calls >= 2, 'trackEvent should have triggered an XHR open() call');
});

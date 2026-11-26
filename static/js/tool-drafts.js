/**
 * Tool input persistence.
 *
 * Every tool page keeps what the user typed into its input boxes across a
 * refresh in the same tab, the same way the Workbench does. This is input
 * text only; results and options are not stored.
 *
 * POLICY:
 *   - Storage is sessionStorage: survives a refresh, cleared when the tab closes.
 *   - Nothing is sent to the server.
 *   - Fields marked data-no-draft (for example the JWT box) are never stored.
 *   - Password fields, read-only fields, the site search and the Workbench
 *     (which has its own draft system) are skipped.
 *   - Every storage call is guarded: blocked or full storage never throws.
 */
(function () {
  "use strict";

  const path = window.location.pathname;
  if (path === "/workbench" || path.indexOf("/admin") === 0) return;

  const KEY = "anvil.tool.drafts.v1";
  const MAX_CHARS = 2000000;           // matches the server's MAX_TEXT_CHARS default
  const EXCLUDED_AREAS = ".site-header, .hero-search, .header-search, .workbench-shell, .admin-shell, .command-palette";
  const TEXT_TYPES = ["text", "search", "url", ""];

  function storage() {
    try { return window.sessionStorage || null; } catch (_) { return null; }
  }

  function readAll() {
    const s = storage();
    if (!s) return {};
    try {
      const data = JSON.parse(s.getItem(KEY) || "{}");
      return data && typeof data === "object" && !Array.isArray(data) ? data : {};
    } catch (_) {
      return {};
    }
  }

  function writeAll(data) {
    const s = storage();
    if (!s) return;
    try {
      if (Object.keys(data).length === 0) s.removeItem(KEY);
      else s.setItem(KEY, JSON.stringify(data));
    } catch (_) { /* storage full or blocked: drafts simply are not kept */ }
  }

  function keyFor(el) {
    return path + "|" + el.id;
  }

  function eligible(el) {
    if (!el || !el.id || el.id.indexOf("wb-") === 0) return false;
    if (el.hasAttribute("data-no-draft") || el.readOnly || el.disabled) return false;
    if (el.closest(EXCLUDED_AREAS)) return false;
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName === "INPUT") return TEXT_TYPES.indexOf((el.type || "").toLowerCase()) !== -1;
    return false;
  }

  function fields() {
    return Array.prototype.filter.call(
      document.querySelectorAll("textarea[id], input[id]"), eligible);
  }

  function saveField(el) {
    const data = readAll();
    const value = el.value || "";
    if (value.length === 0) delete data[keyFor(el)];
    else if (value.length <= MAX_CHARS) data[keyFor(el)] = value;
    writeAll(data);
  }

  function saveAll() {
    const data = readAll();
    fields().forEach((el) => {
      const value = el.value || "";
      if (value.length === 0) delete data[keyFor(el)];
      else if (value.length <= MAX_CHARS) data[keyFor(el)] = value;
    });
    writeAll(data);
  }

  function restore() {
    const data = readAll();
    fields().forEach((el) => {
      const saved = data[keyFor(el)];
      if (typeof saved === "string" && el.value === "") {
        el.value = saved;
        // Let live tools (decoders, regex tester) recompute from the restored text.
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
  }

  // Debounced save while typing.
  let timer = null;
  document.addEventListener("input", (e) => {
    const el = e.target;
    if (!eligible(el)) return;
    clearTimeout(timer);
    timer = setTimeout(() => saveField(el), 250);
  }, true);

  // Pastes, drops and file loads.
  ["change", "paste", "drop"].forEach((type) => {
    document.addEventListener(type, () => setTimeout(saveAll, 0), true);
  });

  // Clear buttons empty the boxes programmatically, so save after they run.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest && e.target.closest("button");
    if (btn && /clear/i.test(btn.id || "")) setTimeout(saveAll, 0);
  }, true);

  window.addEventListener("pagehide", saveAll);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restore);
  } else {
    restore();
  }
})();

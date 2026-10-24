/**
 * Workbench draft persistence (input text only).
 *
 * POLICY:
 *   - Only the text a user typed into the Workbench's input boxes is stored.
 *     Results, options and selections are never persisted.
 *   - Storage is sessionStorage: drafts survive a page refresh in the same tab
 *     and are discarded when the tab closes. Nothing is sent to the server.
 *   - Slots listed in NEVER_PERSIST (the JWT box, which may hold a bearer
 *     credential) are kept in memory for the current page only.
 *   - Every read and write is guarded. Missing, blocked, full or malformed
 *     storage degrades to in-memory drafts and never throws.
 *
 * SCHEMA (version 1):
 *   { "v": 1, "drafts": { "<slot>": { "a": "<text>", "b": "<text>" } } }
 */
(function (root) {
  "use strict";

  const VERSION = 1;
  const STORAGE_KEY = "anvil.workbench.drafts";
  const MAX_CHARS_PER_FIELD = 2000000; // mirrors the server's MAX_TEXT_CHARS default
  const SLOT_PATTERN = /^[a-z][a-z-]{0,31}$/;
  const NEVER_PERSIST = ["jwt"];

  function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }

  // Returns the sessionStorage object, or null when the browser blocks it
  // (some privacy modes throw on property access).
  function defaultStorage() {
    try {
      return root.sessionStorage || null;
    } catch (_) {
      return null;
    }
  }

  /**
   * @param {Storage|null} storage  Anything with getItem/setItem/removeItem.
   * @param {string} [storageKey]
   */
  function createDraftStore(storage, storageKey) {
    const key = storageKey || STORAGE_KEY;
    const store = storage === undefined ? defaultStorage() : storage;

    // True when a write round-trips. Probes once so the UI can explain the
    // limitation instead of silently losing drafts on refresh.
    function available() {
      if (!store) return false;
      try {
        const probe = key + ".probe";
        store.setItem(probe, "1");
        const ok = store.getItem(probe) === "1";
        store.removeItem(probe);
        return ok;
      } catch (_) {
        return false;
      }
    }

    // Returns { slot: { a, b } }. Anything malformed is dropped, so a
    // corrupted or outdated entry cannot break the page.
    function load() {
      if (!store) return {};
      let raw;
      try {
        raw = store.getItem(key);
      } catch (_) {
        return {};
      }
      if (raw == null) return {};
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (_) {
        discard();
        return {};
      }
      if (!isPlainObject(parsed) || parsed.v !== VERSION || !isPlainObject(parsed.drafts)) {
        discard();
        return {};
      }
      const out = {};
      Object.keys(parsed.drafts).forEach((slot) => {
        const entry = parsed.drafts[slot];
        if (!SLOT_PATTERN.test(slot) || NEVER_PERSIST.includes(slot) || !isPlainObject(entry)) return;
        const a = typeof entry.a === "string" && entry.a.length <= MAX_CHARS_PER_FIELD ? entry.a : "";
        const b = typeof entry.b === "string" && entry.b.length <= MAX_CHARS_PER_FIELD ? entry.b : "";
        if (a || b) out[slot] = { a, b };
      });
      return out;
    }

    // Persists the drafts map. Empty drafts and never-persist slots are
    // skipped. Returns true when the write succeeded.
    function save(drafts) {
      if (!store) return false;
      const kept = {};
      Object.keys(drafts || {}).forEach((slot) => {
        if (!SLOT_PATTERN.test(slot) || NEVER_PERSIST.includes(slot)) return;
        const d = drafts[slot] || {};
        const a = typeof d.a === "string" ? d.a : "";
        const b = typeof d.b === "string" ? d.b : "";
        if (a || b) kept[slot] = { a, b };
      });
      try {
        if (Object.keys(kept).length === 0) {
          store.removeItem(key);
        } else {
          store.setItem(key, JSON.stringify({ v: VERSION, drafts: kept }));
        }
        return true;
      } catch (_) {
        return false;
      }
    }

    // Removes every stored draft. Returns true when storage was cleared (or
    // there was nothing to clear).
    function discard() {
      if (!store) return true;
      try {
        store.removeItem(key);
        return true;
      } catch (_) {
        return false;
      }
    }

    return { available, load, save, discard, key };
  }

  const api = { VERSION, STORAGE_KEY, NEVER_PERSIST, MAX_CHARS_PER_FIELD, createDraftStore };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.WorkbenchDrafts = api;
  }
})(typeof window !== "undefined" ? window : globalThis);

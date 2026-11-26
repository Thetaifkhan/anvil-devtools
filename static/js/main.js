// Shared site behaviour: theme switching, keyboard shortcuts, tool search,
// and small helpers (copy, download, byte formatting) used by every tool page.

(function () {
  "use strict";

  /* ------------------------------ Theme ------------------------------ */
  const root = document.documentElement;
  const THEME_KEY = "anvil-theme";
  const MOTION_KEY = "anvil-reduced-motion";
  const THEME_COLORS = {
    light: "#F6F4EE", dracula: "#1B1C26", "night-owl": "#07080F",
    "high-contrast": "#000000",
  };

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    document.querySelectorAll(".theme-toggle button[data-theme]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.theme === theme);
    });
    const meta = document.getElementById("meta-theme-color");
    if (meta && THEME_COLORS[theme]) meta.setAttribute("content", THEME_COLORS[theme]);
  }

  function applyMotion(reduced) {
    root.setAttribute("data-motion", reduced ? "reduced" : "full");
    document.querySelectorAll("#motion-toggle").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(reduced));
      btn.classList.toggle("active", reduced);
      btn.title = reduced ? "Animations off - turn on" : "Reduce motion";
      btn.setAttribute("aria-label", btn.title);
    });
  }

  function motionPrefersReduce() {
    try {
      return localStorage.getItem(MOTION_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function initTheme() {
    let saved = "dracula";
    try {
      saved = localStorage.getItem(THEME_KEY) || "dracula";
    } catch (e) {
      // localStorage is unavailable; use default theme
    }
    applyTheme(saved);
    document.querySelectorAll(".theme-toggle button[data-theme]").forEach((btn) => {
      btn.addEventListener("click", () => {
        try {
          localStorage.setItem(THEME_KEY, btn.dataset.theme);
        } catch (e) {
          // localStorage write failed, but theme still applies to the page
        }
        applyTheme(btn.dataset.theme);
      });
    });

    // Motion toggle: stored choice wins (set pre-paint in base.html);
    // the button flips it for this site only.
    applyMotion(motionPrefersReduce());
    document.querySelectorAll("#motion-toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = !motionPrefersReduce();
        try {
          localStorage.setItem(MOTION_KEY, next ? "1" : "0");
        } catch (e) {
          // localStorage write failed; motion still applies to this page
        }
        applyMotion(next);
      });
    });
  }

  /* ---------------------------- Mobile nav ---------------------------- */
  function initMobileNav() {
    const toggle = document.getElementById("nav-toggle");
    const nav = document.getElementById("mobile-nav");
    if (!toggle || !nav) return;

    function setOpen(open) {
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      nav.setAttribute("aria-hidden", String(!open));
      // Inert keeps descendants out of keyboard focus even during the short
      // closing animation, while visibility handles the final visual state.
      nav.inert = !open;
      nav.classList.toggle("open", open);
      document.body.classList.toggle("nav-open", open);
    }

    toggle.addEventListener("click", () => {
      setOpen(toggle.getAttribute("aria-expanded") !== "true");
    });

    // Close when a link inside the menu is used, when tapping outside,
    // or when the viewport grows back to laptop width.
    nav.addEventListener("click", (e) => {
      if (e.target.closest("a")) setOpen(false);
    });
    document.addEventListener("click", (e) => {
      if (!nav.contains(e.target) && !toggle.contains(e.target)) setOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        toggle.focus();
      }
    });
    setOpen(false);
    window.addEventListener("resize", () => {
      if (window.innerWidth >= 1024) setOpen(false);
    });
  }

  /* --------------------------- Tool search ---------------------------- */
  // TOOL_INDEX is injected by base.html as a small JSON blob so this file
  // stays static and cacheable.
  function fuzzyMatch(tool, query) {
    const q = query.toLowerCase();
    if (tool.name.toLowerCase().includes(q)) return true;
    if (tool.slug.includes(q)) return true;
    if (tool.short && tool.short.toLowerCase().includes(q)) return true;
    if (tool.category && tool.category.toLowerCase().includes(q)) return true;
    return (tool.keywords || []).some((k) => k.includes(q));
  }

  function initSearch(input, resultsEl) {
    if (!input || !resultsEl || typeof TOOL_INDEX === "undefined") return;

    function render(query) {
      resultsEl.innerHTML = "";
      if (!query) {
        resultsEl.hidden = true;
        return;
      }
      const matches = TOOL_INDEX.filter((t) => fuzzyMatch(t, query)).slice(0, 8);
      if (matches.length === 0) {
        resultsEl.innerHTML = '<div class="empty-state">No tools match that search.</div>';
        resultsEl.hidden = false;
        return;
      }
      matches.forEach((t) => {
        const a = document.createElement("a");
        a.href = "/" + t.slug;
        a.className = "tool-card";

        const nameSpan = document.createElement("span");
        nameSpan.className = "name";
        nameSpan.textContent = t.name;

        const descP = document.createElement("p");
        descP.className = "desc";
        descP.textContent = t.short;

        a.appendChild(nameSpan);
        a.appendChild(descP);
        resultsEl.appendChild(a);
      });
      resultsEl.hidden = false;
    }

    input.addEventListener("input", () => render(input.value.trim()));
    document.addEventListener("click", (e) => {
      if (!resultsEl.contains(e.target) && e.target !== input) resultsEl.hidden = true;
    });
  }

  /* ------------------------- Keyboard shortcuts ------------------------ */
  function initShortcuts() {
    document.addEventListener("keydown", (e) => {
      const mod = e.metaKey || e.ctrlKey;

      // Cmd/Ctrl+K -> open the command palette
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (window.AnvilPalette) window.AnvilPalette.open();
      }

      // Cmd/Ctrl+Enter: fire the button marked [data-shortcut-enter] on
      // standalone tool pages.  Pages with their own handler (formatter.js,
      // workbench.js) must NOT add [data-shortcut-enter] to avoid double-fire.
      if (mod && e.key === "Enter") {
        const btn = document.querySelector("[data-shortcut-enter]");
        if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
      }

      // Esc -> blur active input / close search results / close palette / close nav
      if (e.key === "Escape") {
        document.activeElement && document.activeElement.blur();
        document.querySelectorAll("[data-search-results]").forEach((el) => (el.hidden = true));
        if (window.AnvilPalette) window.AnvilPalette.close();
        const toggle = document.getElementById("nav-toggle");
        if (toggle && toggle.getAttribute("aria-expanded") === "true") toggle.click();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initMobileNav();
    initSearch(document.getElementById("header-search-input"), document.getElementById("header-search-results"));
    initSearch(document.getElementById("hero-search-input"), document.getElementById("hero-search-results"));
    initSearch(document.getElementById("mobile-search-input"), document.getElementById("mobile-search-results"));
    initShortcuts();
  });

  /* ------------------------------ Helpers ------------------------------ */
  window.Anvil = {
    copyText(text, btn) {
      const done = (label) => {
        if (!btn) return;
        const original = btn.textContent;
        btn.textContent = label;
        setTimeout(() => (btn.textContent = original), 1200);
      };
      const fallback = () => {
        // Clipboard API needs a secure context; fall back to execCommand
        // so Copy keeps working on plain-http and older mobile browsers.
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try {
          document.execCommand("copy") ? done("Copied") : done("Copy failed");
        } catch (_) {
          done("Copy failed");
        }
        ta.remove();
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => done("Copied")).catch(fallback);
      } else {
        fallback();
      }
    },

    // Render a tool result. Very large output (over 1 MB) is previewed,
    // not dumped into the DOM: the page stays responsive and a Download
    // button offers the complete result.
    renderOutput(el, text, opts) {
      opts = opts || {};
      var PREVIEW_LIMIT = 1024 * 1024; // 1 MB
      var str = String(text == null ? "" : text);
      // Remove a previous truncation notice (same tool run twice).
      if (el.dataset.largeNote) {
        var oldNote = document.getElementById(el.dataset.largeNote);
        if (oldNote && oldNote.parentNode) oldNote.parentNode.removeChild(oldNote);
        delete el.dataset.largeNote;
      }
      if (str.length <= PREVIEW_LIMIT) {
        el.textContent = str;
        return;
      }
      el.textContent = str.slice(0, PREVIEW_LIMIT)
        + "\n\n[... output truncated for display - use Download for all "
        + str.length.toLocaleString() + " characters ...]";
      var note = document.createElement("div");
      note.className = "output-truncated-note";
      note.id = "large-note-" + Date.now();
      var msg = document.createElement("span");
      msg.textContent = "This output is "
        + Anvil.formatBytes(new Blob([str]).size)
        + " - showing the first 1 MB so the page stays fast. ";
      note.appendChild(msg);
      var dl = document.createElement("button");
      dl.type = "button";
      dl.className = "btn small";
      dl.textContent = "Download full output";
      dl.addEventListener("click", function () {
        Anvil.download(opts.filename || "output.txt", str,
                       opts.mime || "text/plain");
      });
      note.appendChild(dl);
      el.parentNode.insertBefore(note, el.nextSibling);
      el.dataset.largeNote = note.id;
    },

    download(filename, content, mime) {
      mime = mime || "text/plain";
      const blob = new Blob([content], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },

    formatBytes(bytes) {
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / (1024 * 1024)).toFixed(2) + " MB";
    },

    setStatus(el, message, type) {
      // type: "ok" | "error" | "neutral"
      el.textContent = message;
      el.className = "status-line " + (type || "neutral");
      // Loading shimmer while a tool runs (api_tool.js announces
      // "Processing..."; the shimmer class is removed by the next call).
      const running = type !== "error" && type !== "ok"
        && /processing|running|\u2026$/i.test(message);
      el.classList.toggle("is-running", running);
    },
  };

  // ── Output boxes: wrap each output in a scrollable frame with a fixed copy icon ──
  const COPY_ICON_SVG =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="13" height="13" rx="2"/>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

  function watchOutputGrowth(out) {
    // Smooth height change: when the box goes from empty to having
    // content, animate the expansion once (CSS .just-filled keyframes).
    if (out.dataset.growthWatched) return;
    out.dataset.growthWatched = "1";
    let hadContent = out.textContent.trim() !== "";
    const obs = new MutationObserver(() => {
      const has = out.textContent.trim() !== "";
      if (has && !hadContent) {
        out.classList.remove("just-filled");
        // Restart the animation on the next frame.
        requestAnimationFrame(() => out.classList.add("just-filled"));
      }
      hadContent = has;
    });
    obs.observe(out, { childList: true, characterData: true, subtree: true });
  }

  function enhanceOutputBoxes(root) {
    root.querySelectorAll(".output-area[id]").forEach((out) => {
      watchOutputGrowth(out);
      if (out.parentElement && out.parentElement.classList.contains("output-frame")) return;
      const frame = document.createElement("div");
      frame.className = "output-frame";
      out.parentNode.insertBefore(frame, out);
      frame.appendChild(out);
      const icon = document.createElement("button");
      icon.type = "button";
      icon.className = "output-copy-icon";
      icon.title = "Copy output";
      icon.setAttribute("aria-label", "Copy output");
      icon.dataset.copyFrom = out.id;
      icon.innerHTML = COPY_ICON_SVG;
      frame.appendChild(icon);
    });
  }

  // Any button with data-copy-from="<element id>" copies that element's text.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-copy-from]");
    if (!btn) return;
    const source = document.getElementById(btn.dataset.copyFrom);
    if (!source) return;
    window.Anvil.copyText(source.textContent, null);
    if (btn.classList.contains("output-copy-icon")) {
      btn.classList.add("copied");
      setTimeout(() => btn.classList.remove("copied"), 1200);
    } else {
      const original = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = original), 1200);
    }
  });

  const startOutputBoxes = () => {
    enhanceOutputBoxes(document);
    // Workbench builds some output boxes after load; wrap those too.
    new MutationObserver(() => enhanceOutputBoxes(document))
      .observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startOutputBoxes);
  } else {
    startOutputBoxes();
  }

  // ── Header: gains a shadow once the page is scrolled ─────────────────
  const siteHeader = document.querySelector(".site-header");
  if (siteHeader) {
    const onScroll = () => siteHeader.classList.toggle("scrolled", window.scrollY > 8);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  // ── Buttons: ripple from the click point (off when motion is reduced) ─
  document.addEventListener("pointerdown", (e) => {
    const btn = e.target.closest(".btn");
    if (!btn || btn.disabled) return;
    if (document.documentElement.getAttribute("data-motion") === "reduced") return;
    const rect = btn.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 2;
    const dot = document.createElement("span");
    dot.className = "ripple";
    dot.style.width = dot.style.height = size + "px";
    dot.style.left = (e.clientX - rect.left - size / 2) + "px";
    dot.style.top = (e.clientY - rect.top - size / 2) + "px";
    btn.appendChild(dot);
    dot.addEventListener("animationend", () => dot.remove(), { once: true });
  });
})();

// Shared site behaviour: theme switching, keyboard shortcuts, tool search,
// and small helpers (copy, download, byte formatting) used by every tool page.

(function () {
  "use strict";

  /* ------------------------------ Theme ------------------------------ */
  const root = document.documentElement;
  const THEME_KEY = "anvil-theme";
  const THEME_COLORS = { light: "#F6F4EE", dracula: "#1B1C26", "night-owl": "#011221" };

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    document.querySelectorAll(".theme-toggle button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.theme === theme);
    });
    const meta = document.getElementById("meta-theme-color");
    if (meta && THEME_COLORS[theme]) meta.setAttribute("content", THEME_COLORS[theme]);
  }

  function initTheme() {
    let saved = "dracula";
    try {
      saved = localStorage.getItem(THEME_KEY) || "dracula";
    } catch (e) {
      // localStorage is unavailable; use default theme
    }
    applyTheme(saved);
    document.querySelectorAll(".theme-toggle button").forEach((btn) => {
      btn.addEventListener("click", () => {
        try {
          localStorage.setItem(THEME_KEY, btn.dataset.theme);
        } catch (e) {
          // localStorage write failed, but theme still applies to the page
        }
        applyTheme(btn.dataset.theme);
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
    },
  };
})();

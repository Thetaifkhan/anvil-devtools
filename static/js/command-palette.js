// Command palette: Cmd/Ctrl+K opens a searchable list of tools and quick
// actions, built from window.TOOL_INDEX (the same registry data that
// powers header/hero search) plus a few static commands like theme switches.
(function () {
  "use strict";

  const root = document.getElementById("command-palette");
  if (!root) return;
  const input = document.getElementById("command-palette-input");
  const resultsEl = document.getElementById("command-palette-results");

  function staticCommands() {
    return [
      { type: "action", label: "Open Workbench", hint: "Formatter · Validator · Converter · more", run: () => (window.location.href = "/workbench") },
      { type: "action", label: "Open Tools directory", hint: "Browse all tools", run: () => (window.location.href = "/tools") },
      { type: "action", label: "Switch theme: Light", hint: "Theme", run: () => setTheme("light") },
      { type: "action", label: "Switch theme: Dracula", hint: "Theme", run: () => setTheme("dracula") },
      { type: "action", label: "Switch theme: Night Owl", hint: "Theme", run: () => setTheme("night-owl") },
    ];
  }

  function setTheme(theme) {
    try { localStorage.setItem("anvil-theme", theme); } catch (_) { /* theme remains active for this page */ }
    document.documentElement.setAttribute("data-theme", theme);
    document.querySelectorAll(".theme-toggle button").forEach((btn) => btn.classList.toggle("active", btn.dataset.theme === theme));
    window.dispatchEvent(new CustomEvent("anvil-theme-change", { detail: { theme } }));
  }

  function toolCommands(query) {
    const idx = window.TOOL_INDEX || [];
    const q = query.toLowerCase();
    return idx
      .filter((t) => !q || t.name.toLowerCase().includes(q) || t.slug.includes(q) || (t.operation && t.operation.includes(q)) || (t.keywords || []).some((k) => k.includes(q)))
      .slice(0, 12)
      .map((t) => ({
        type: "tool",
        label: t.name,
        hint: [t.operation, (t.formats || [])[0]].filter(Boolean).join(" · "),
        run: () => (window.location.href = "/" + t.slug),
      }));
  }

  function render(query) {
    const q = query.trim();
    const items = q ? toolCommands(q) : [...staticCommands(), ...toolCommands("")];
    resultsEl.innerHTML = "";
    if (items.length === 0) {
      resultsEl.innerHTML = '<div class="empty-state">No matches. Try "json", "convert", "base64"…</div>';
      return;
    }
    items.forEach((item, i) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "command-palette-item" + (i === 0 ? " active" : "");
      const label = document.createElement("span");
      label.textContent = item.label;
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = item.hint || "";
      row.append(label, hint);
      row.addEventListener("click", () => { item.run(); close(); });
      resultsEl.appendChild(row);
    });
  }

  function open() {
    root.hidden = false;
    input.value = "";
    render("");
    setTimeout(() => input.focus(), 0);
  }

  function close() {
    root.hidden = true;
  }

  function moveSelection(dir) {
    const items = Array.from(resultsEl.querySelectorAll(".command-palette-item"));
    if (!items.length) return;
    const currentIndex = items.findIndex((el) => el.classList.contains("active"));
    let next = currentIndex + dir;
    if (next < 0) next = items.length - 1;
    if (next >= items.length) next = 0;
    items.forEach((el) => el.classList.remove("active"));
    items[next].classList.add("active");
    items[next].scrollIntoView({ block: "nearest" });
  }

  input.addEventListener("input", () => render(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); moveSelection(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveSelection(-1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const active = resultsEl.querySelector(".command-palette-item.active");
      if (active) active.click();
    }
  });

  root.querySelectorAll("[data-palette-close]").forEach((el) => el.addEventListener("click", close));

  window.AnvilPalette = { open, close };
})();

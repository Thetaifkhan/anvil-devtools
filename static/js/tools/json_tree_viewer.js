(function () {
  "use strict";
  const input = document.getElementById("input");
  const output = document.getElementById("output");
  const status = document.getElementById("status");
  const searchBox = document.getElementById("search");

  function typeOf(v) {
    if (v === null) return "null";
    if (Array.isArray(v)) return "array";
    return typeof v;
  }

  function renderValue(v) {
    const t = typeOf(v);
    if (t === "string") return `<span class="val-string">"${escapeHtml(v)}"</span>`;
    if (t === "number") return `<span class="val-number">${v}</span>`;
    if (t === "boolean") return `<span class="val-bool">${v}</span>`;
    if (t === "null") return `<span class="val-null">null</span>`;
    return "";
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Build a safe JSONPath segment for a key.
  // Simple identifiers use dot notation; anything else uses bracket notation.
  function safePath(parentPath, key, isArray) {
    if (isArray) return `${parentPath}[${key}]`;
    const simple = /^[a-zA-Z_$][a-zA-Z0-9_$]*$/.test(String(key));
    if (simple) return parentPath ? `${parentPath}.${key}` : String(key);
    const escaped = String(key).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    return parentPath ? `${parentPath}["${escaped}"]` : `["${escaped}"]`;
  }

  function buildNode(key, value, path) {
    const t = typeOf(value);
    const li = document.createElement("li");

    if (t === "object" || t === "array") {
      const keys = t === "array" ? value.map((_, i) => i) : Object.keys(value);
      const toggle = document.createElement("span");
      toggle.className = "toggle";
      toggle.textContent = "▾";
      const label = document.createElement("span");
      // Use DOM to build label — not innerHTML with user key
      const keySpan = document.createElement("span"); keySpan.className = "key"; keySpan.textContent = String(key);
      const typeSpan = document.createElement("span"); typeSpan.className = "type"; typeSpan.textContent = `${t}(${keys.length})`;
      label.appendChild(keySpan); label.appendChild(typeSpan);
      label.title = "Click to copy path: " + path;
      label.style.cursor = "pointer";
      label.addEventListener("click", () => Anvil.copyText(path));

      const ul = document.createElement("ul");
      keys.forEach((k) => {
        const childPath = safePath(path, k, t === "array");
        ul.appendChild(buildNode(k, value[k], childPath));
      });

      toggle.addEventListener("click", () => {
        const collapsed = ul.style.display === "none";
        ul.style.display = collapsed ? "" : "none";
        toggle.textContent = collapsed ? "▾" : "▸";
      });

      li.appendChild(toggle);
      li.appendChild(label);
      li.appendChild(ul);
    } else {
      const label = document.createElement("span");
      // Build label with DOM to prevent XSS
      const keySpan2 = document.createElement("span"); keySpan2.className = "key"; keySpan2.textContent = String(key);
      label.appendChild(keySpan2);
      label.appendChild(document.createTextNode(": "));
      // renderValue returns trusted HTML (escapeHtml-protected) — insert as HTML
      const valSpan = document.createElement("span"); valSpan.innerHTML = renderValue(value);
      label.appendChild(valSpan);
      label.style.cursor = "pointer";
      label.title = "Click to copy value";
      label.addEventListener("click", () => Anvil.copyText(String(value)));
      li.style.marginLeft = "1.1rem";
      li.appendChild(label);
    }
    return li;
  }

  function render() {
    const text = input.value;
    if (!text.trim()) { Anvil.setStatus(status, "Enter some JSON first.", "neutral"); output.innerHTML = ""; return; }
    let data;
    try { data = JSON.parse(text); }
    catch (err) { Anvil.setStatus(status, "Invalid JSON — " + err.message, "error"); output.innerHTML = ""; return; }

    output.innerHTML = "";
    const ul = document.createElement("ul");
    ul.style.borderLeft = "none";
    ul.style.paddingLeft = "0";
    ul.appendChild(buildNode("$", data, "$"));
    output.appendChild(ul);
    Anvil.setStatus(status, "Rendered.", "ok");
  }

  document.getElementById("render-btn").addEventListener("click", render);
  document.getElementById("expand-all").addEventListener("click", () => {
    output.querySelectorAll("ul").forEach((u) => (u.style.display = ""));
    // Update every toggle glyph to the "expanded" state so the visual
    // indicator matches what the user just did.
    output.querySelectorAll(".toggle").forEach((t) => { t.textContent = "▾"; t.setAttribute("aria-expanded", "true"); });
  });
  document.getElementById("collapse-all").addEventListener("click", () => {
    output.querySelectorAll("li > ul").forEach((u) => (u.style.display = "none"));
    output.querySelectorAll(".toggle").forEach((t) => { t.textContent = "▸"; t.setAttribute("aria-expanded", "false"); });
  });

  searchBox.addEventListener("input", () => {
    const q = searchBox.value.toLowerCase();
    output.querySelectorAll(".key, .val-string, .val-number, .val-bool, .val-null").forEach((el) => {
      el.style.background = q && el.textContent.toLowerCase().includes(q) ? "color-mix(in srgb, var(--ember) 30%, transparent)" : "";
    });
  });
})();

/**
 * Standalone Formatter — JSON / XML / YAML / SQL / Markdown
 *
 * STATE INVARIANT: Switching between format tabs NEVER destroys user data.
 * Each format has its own independent state (input, output, options, scroll).
 * Only an explicit "Clear" action may destroy content.
 */
(function () {
  "use strict";

  const tabs         = document.querySelectorAll(".format-tabs button");
  const workspace    = document.getElementById("format-workspace");
  const unsupported  = document.getElementById("format-unsupported");
  const unsupportedText = document.getElementById("unsupported-text");
  const optionsEl    = document.getElementById("format-options");
  const actionRow    = document.getElementById("action-row");
  const input        = document.getElementById("input");
  const output       = document.getElementById("output");
  const status       = document.getElementById("status");
  const meta         = document.getElementById("meta-row");
  const inputCount   = document.getElementById("input-count");
  const inputLabel   = document.getElementById("input-label");
  const outputLabel  = document.getElementById("output-label");
  const dropZone     = document.getElementById("drop-zone");

  const SUPPORTED = new Set(["json", "xml", "yaml", "sql", "markdown"]);
  const PLACEHOLDERS = {
    json:     'Paste JSON here, e.g. {"name":"John","age":30} — or drag a .json file in',
    xml:      "Paste XML here, e.g. <root><item>1</item></root>",
    yaml:     "Paste YAML here, e.g.\nname: John\nage: 30",
    sql:      "select id,name from users where active=1 order by name",
    markdown: "# Hello\n\nSome **bold** text with a [link](https://example.com).",
  };
  const UNSUPPORTED_TEXT = {
    javascript: "JavaScript formatting isn't wired up yet — we'd rather say so than produce incorrect output.",
    html:       "HTML formatting isn't available yet.",
    css:        "CSS formatting isn't available yet.",
  };

  // ── Per-format state: input, output, options, scroll ────────────────────
  const STATE = {};
  function stateFor(fmt) {
    if (!STATE[fmt]) STATE[fmt] = { input: "", output: "", outputHtml: "", hasError: false, options: {}, scrollTop: 0, cursor: 0 };
    return STATE[fmt];
  }

  let current = "json";
  // Track current request to cancel stale responses
  let _reqId = 0;

  // ── Save / restore current state ────────────────────────────────────────
  function saveState(fmt) {
    const s = stateFor(fmt);
    s.input      = input.value;
    s.output     = output.textContent;
    s.outputHtml = output.innerHTML;
    s.hasError   = output.classList.contains("has-error");
    s.scrollTop  = input.scrollTop;
    s.cursor     = input.selectionStart;
    // Snapshot option values by element id
    s.options = {};
    optionsEl.querySelectorAll("select, input[type=checkbox], input[type=number]").forEach((el) => {
      s.options[el.id] = el.type === "checkbox" ? el.checked : el.value;
    });
  }

  function restoreState(fmt) {
    const s = stateFor(fmt);
    input.value = s.input;
    if (s.hasError) {
      output.classList.add("has-error");
      output.innerHTML = s.outputHtml;
    } else {
      output.classList.remove("has-error");
      // For markdown the output is HTML; for everything else it's text
      if (fmt === "markdown") {
        output.innerHTML = s.outputHtml;
      } else {
        output.textContent = s.output;
      }
    }
    // Restore option values after the options UI has been rebuilt
    requestAnimationFrame(() => {
      Object.entries(s.options).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.type === "checkbox") el.checked = val;
        else el.value = val;
      });
    });
    meta.textContent = s.output ? `${Anvil.formatBytes(new Blob([s.output]).size)} · ${s.output.split("\n").length} lines` : "";
    inputCount.textContent = s.input ? `${s.input.length} chars` : "";
    // Restore scroll and cursor asynchronously (after layout)
    requestAnimationFrame(() => {
      input.scrollTop = s.scrollTop;
      try { input.setSelectionRange(s.cursor, s.cursor); } catch (_) {}
    });
  }

  function updateMeta(text) {
    meta.textContent = `${Anvil.formatBytes(new Blob([text]).size)} · ${text.split("\n").length} lines`;
  }

  // ── JSON ─────────────────────────────────────────────────────────────────
  function sortObjectKeys(value) {
    if (Array.isArray(value)) return value.map(sortObjectKeys);
    if (value && typeof value === "object") {
      const sorted = {};
      Object.keys(value).sort().forEach((k) => (sorted[k] = sortObjectKeys(value[k])));
      return sorted;
    }
    return value;
  }
  function locateJsonError(text, err) {
    const match = /position (\d+)/.exec(err.message);
    if (!match) return err.message;
    const pos = Number(match[1]);
    const up = text.slice(0, pos);
    const line = up.split("\n").length;
    const col  = pos - up.lastIndexOf("\n");
    const lineText = text.split("\n")[line - 1] || "";
    const arrow = " ".repeat(Math.max(0, col - 1)) + "^";
    return `${err.message.replace(/position \d+/, `line ${line}, column ${col}`)}\n\n${lineText}\n${arrow}`;
  }
  function jsonIndent() {
    const v = document.getElementById("opt-indent");
    if (!v) return 2;
    return v.value === "tab" ? "\t" : Number(v.value);
  }
  function jsonAction(kind) {
    const text = input.value;
    if (!text.trim()) { Anvil.setStatus(status, "Enter some JSON first.", "neutral"); output.textContent = ""; meta.textContent = ""; return; }
    let data;
    try { data = JSON.parse(text); }
    catch (err) {
      output.classList.add("has-error");
      output.textContent = `Invalid JSON\n${locateJsonError(text, err)}`;
      meta.textContent = "";
      Anvil.setStatus(status, "Invalid JSON — see details in output.", "error");
      return;
    }
    output.classList.remove("has-error");
    if (kind === "validate") { output.textContent = "Valid JSON."; Anvil.setStatus(status, "Valid JSON.", "ok"); updateMeta(text); return; }
    if (document.getElementById("opt-sort") && document.getElementById("opt-sort").checked) data = sortObjectKeys(data);
    const result = kind === "minify" ? JSON.stringify(data) : JSON.stringify(data, null, jsonIndent());
    output.textContent = result;
    updateMeta(result);
    Anvil.setStatus(status, kind === "minify" ? "Valid JSON — minified." : "Valid JSON — formatted.", "ok");
  }

  // ── XML ──────────────────────────────────────────────────────────────────
  function xmlAction(kind) {
    if (!input.value.trim()) { Anvil.setStatus(status, "Enter some XML first.", "neutral"); return; }
    if (kind === "validate") {
      safeApiFetch("/api/xml-validate", { text: input.value }).then((data) => {
        if (!data) return;
        if (data.valid) {
          output.classList.remove("has-error");
          output.textContent = "Valid XML.";
          Anvil.setStatus(status, "Valid XML.", "ok");
        } else {
          const lineInfo = data.line ? ` at line ${data.line}, column ${data.column}` : "";
          output.classList.add("has-error");
          output.textContent = `Invalid XML${lineInfo}\n${data.message || ""}`;
          Anvil.setStatus(status, "Invalid XML.", "error");
        }
      });
      return;
    }
    const indentSel = document.getElementById("opt-indent");
    const indent = indentSel && indentSel.value === "tab" ? "\t" : " ".repeat(Number(indentSel ? indentSel.value : 2));
    safeApiFetch("/api/xml-format", { text: input.value, minify: kind === "minify", indent }).then((data) => {
      if (!data) return;
      if (!data.ok) {
        output.classList.add("has-error");
        output.textContent = data.error || "Error formatting XML.";
        Anvil.setStatus(status, data.error || "Error.", "error");
        return;
      }
      output.classList.remove("has-error");
      output.textContent = data.result;
      updateMeta(data.result);
      Anvil.setStatus(status, kind === "minify" ? "Minified." : "Formatted.", "ok");
    });
  }

  // ── YAML ─────────────────────────────────────────────────────────────────
  async function yamlAction(kind) {
    if (!input.value.trim()) { Anvil.setStatus(status, "Enter some YAML first.", "neutral"); return; }
    Anvil.setStatus(status, "Processing…", "neutral");
    const reqId = ++_reqId;
    try {
      const toJson = await fetch("/api/yaml-to-json", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: input.value }) }).then((r) => r.json());
      if (reqId !== _reqId) return; // stale
      if (!toJson.ok) { output.textContent = ""; meta.textContent = ""; Anvil.setStatus(status, toJson.error || "Invalid YAML.", "error"); return; }
      if (kind === "validate") { output.textContent = "Valid YAML."; Anvil.setStatus(status, "Valid YAML.", "ok"); updateMeta(input.value); return; }
      const toYaml = await fetch("/api/json-to-yaml", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: toJson.result }) }).then((r) => r.json());
      if (reqId !== _reqId) return;
      if (!toYaml.ok) { Anvil.setStatus(status, toYaml.error || "Could not normalize YAML.", "error"); return; }
      output.textContent = toYaml.result;
      updateMeta(toYaml.result);
      Anvil.setStatus(status, "Formatted. (Note: YAML comments and anchors are not preserved in this round-trip.)", "ok");
    } catch (e) {
      if (reqId !== _reqId) return;
      Anvil.setStatus(status, "Network error — could not reach the server.", "error");
    }
  }

  // ── SQL ──────────────────────────────────────────────────────────────────
  function sqlAction(kind) {
    if (!input.value.trim()) { Anvil.setStatus(status, "Enter a SQL query first.", "neutral"); return; }
    const caseSel   = document.getElementById("opt-case");
    const indentSel = document.getElementById("opt-indent");
    const stripComments = document.getElementById("opt-strip-comments") && document.getElementById("opt-strip-comments").checked;
    safeApiFetch("/api/sql-format", {
      text: input.value,
      action: kind === "minify" ? "minify" : "format",
      keyword_case: caseSel ? caseSel.value : "upper",
      indent_width: indentSel ? Number(indentSel.value) : 2,
      strip_comments: kind === "minify" ? stripComments : false,
    }).then((data) => {
      if (!data) return;
      if (!data.ok) {
        output.classList.add("has-error");
        output.textContent = data.error || "Error formatting SQL.";
        Anvil.setStatus(status, data.error || "Error.", "error");
        return;
      }
      output.classList.remove("has-error");
      output.textContent = data.result;
      updateMeta(data.result);
      Anvil.setStatus(status, kind === "minify" ? "Minified." : "Formatted.", "ok");
    });
  }

  // ── Markdown ─────────────────────────────────────────────────────────────
  const SAFE_URL = /^(https?:|mailto:|#|\/)/i;
  function sanitizeHref(href) {
    const t = String(href || "").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").trim();
    const safe = /^(https?:|mailto:|#)/i.test(t) || (t.startsWith("/") && !t.startsWith("//") && !t.startsWith("/\\"));
    return safe ? t : "#";
  }
  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function mdInline(escaped) {
    // Input is already HTML-escaped; we only pattern-match on the escaped form
    let t = escaped;
    t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // Italic with underscore only at word boundaries (so my_var_name stays intact)
    t = t.replace(/(?<![\w])_([^_]+)_(?![\w])/g, "<em>$1</em>");
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, linkText, href) =>
      `<a href="${escHtml(sanitizeHref(href))}" target="_blank" rel="noopener noreferrer">${linkText}</a>`
    );
    return t;
  }
  function mdRender(md) {
    const lines = md.split("\n");
    let html = "", inCode = false, listOpen = null;
    const closeList = () => { if (listOpen) { html += `</${listOpen}>`; listOpen = null; } };
    lines.forEach((line) => {
      if (line.trim().startsWith("```")) { inCode = !inCode; html += inCode ? "<pre><code>" : "</code></pre>"; return; }
      if (inCode) { html += escHtml(line) + "\n"; return; }
      const h = /^(#{1,6})\s+(.*)/.exec(line);
      if (h)             { closeList(); html += `<h${h[1].length}>${mdInline(escHtml(h[2]))}</h${h[1].length}>`; return; }
      if (/^>\s?/.test(line)) { closeList(); html += `<blockquote>${mdInline(escHtml(line.replace(/^>\s?/, "")))}</blockquote>`; return; }
      if (/^\s*[-*]\s+/.test(line)) { if (listOpen !== "ul") { closeList(); html += "<ul>"; listOpen = "ul"; } html += `<li>${mdInline(escHtml(line.replace(/^\s*[-*]\s+/, "")))}</li>`; return; }
      if (/^\s*\d+\.\s+/.test(line)) { if (listOpen !== "ol") { closeList(); html += "<ol>"; listOpen = "ol"; } html += `<li>${mdInline(escHtml(line.replace(/^\s*\d+\.\s+/, "")))}</li>`; return; }
      closeList();
      if (line.trim() === "") return;
      html += `<p>${mdInline(escHtml(line))}</p>`;
    });
    closeList();
    return html;
  }
  function markdownAction() {
    const html = mdRender(input.value);
    output.innerHTML = html || `<span style="color:var(--text-faint)">Preview appears here as you type.</span>`;
    stateFor(current).outputHtml = html;
    updateMeta(input.value);
    Anvil.setStatus(status, "Preview updated.", "ok");
  }

  // ── Safe API fetch with stale-response guard ──────────────────────────
  async function safeApiFetch(endpoint, body) {
    const reqId = ++_reqId;
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (reqId !== _reqId) return null;
      const data = await res.json();
      if (reqId !== _reqId) return null;
      return data;
    } catch (_) {
      if (reqId !== _reqId) return null;
      Anvil.setStatus(status, "Network error — could not reach the server.", "error");
      output.textContent = "";
      return null;
    }
  }

  // ── Options UI ───────────────────────────────────────────────────────────
  function makeEl(tag, attrs, html) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "textContent") e.textContent = v;
      else e.setAttribute(k, v);
    });
    if (html !== undefined) e.innerHTML = html; // only for trusted static markup
    return e;
  }

  function renderOptions(fmt) {
    optionsEl.innerHTML = "";
    if (fmt === "json") {
      optionsEl.appendChild(makeEl("label", {}, 'Indent <select id="opt-indent"><option value="2">2 spaces</option><option value="4">4 spaces</option><option value="tab">Tab</option></select>'));
      optionsEl.appendChild(makeEl("label", {}, '<input type="checkbox" id="opt-sort"> Sort keys'));
    } else if (fmt === "xml") {
      optionsEl.appendChild(makeEl("label", {}, 'Indent <select id="opt-indent"><option value="2">2 spaces</option><option value="4">4 spaces</option><option value="tab">Tab</option></select>'));
    } else if (fmt === "sql") {
      optionsEl.appendChild(makeEl("label", {}, 'Keywords <select id="opt-case"><option value="upper">UPPER</option><option value="lower">lower</option><option value="capitalize">Capitalize</option></select>'));
      optionsEl.appendChild(makeEl("label", {}, 'Indent <select id="opt-indent"><option value="2">2</option><option value="4">4</option></select>'));
      optionsEl.appendChild(makeEl("label", {}, '<input type="checkbox" id="opt-strip-comments"> Strip comments on Minify'));
    } else if (fmt === "yaml") {
      const note = makeEl("span", { style: "color:var(--text-faint);font-size:.82rem;" });
      note.textContent = "Format normalizes YAML via a round-trip parse — comments and anchors are not preserved.";
      optionsEl.appendChild(note);
    } else if (fmt === "markdown") {
      const note = makeEl("span", { style: "color:var(--text-faint);font-size:.82rem;" });
      note.textContent = "Live preview — lightweight renderer (no tables).";
      optionsEl.appendChild(note);
    }
  }

  function renderActions(fmt) {
    actionRow.innerHTML = "";
    const make = (id, label, primary, handler) => {
      const b = makeEl("button", { class: "btn" + (primary ? " primary" : ""), id, type: "button" });
      b.textContent = label;
      b.addEventListener("click", handler);
      actionRow.appendChild(b);
      return b;
    };
    if (fmt === "json") {
      make("format-btn", "Format ⌘⏎", true, () => jsonAction("format"));
      make("minify-btn", "Minify", false, () => jsonAction("minify"));
      make("validate-btn", "Validate", false, () => jsonAction("validate"));
    } else if (fmt === "xml") {
      make("format-btn", "Format ⌘⏎", true, () => xmlAction("format"));
      make("minify-btn", "Minify", false, () => xmlAction("minify"));
      make("validate-btn", "Validate", false, () => xmlAction("validate"));
    } else if (fmt === "yaml") {
      make("format-btn", "Format ⌘⏎", true, () => yamlAction("format"));
      make("validate-btn", "Validate", false, () => yamlAction("validate"));
    } else if (fmt === "sql") {
      make("format-btn", "Format ⌘⏎", true, () => sqlAction("format"));
      make("minify-btn", "Minify", false, () => sqlAction("minify"));
    } else if (fmt === "markdown") {
      make("format-btn", "Preview ⌘⏎", true, markdownAction);
    }
  }

  function copyOutput(btn) {
    // For markdown: offer HTML; for everything else: plain text
    const text = current === "markdown" ? (stateFor(current).outputHtml || "") : output.textContent;
    Anvil.copyText(text, btn);
  }

  const MIME = { json: "application/json", xml: "application/xml", yaml: "text/yaml", sql: "text/plain", markdown: "text/html" };
  function downloadOutput() {
    const ext  = { json: "json", xml: "xml", yaml: "yaml", sql: "sql", markdown: "html" }[current] || "txt";
    const text = current === "markdown" ? (stateFor(current).outputHtml || "") : output.textContent;
    Anvil.download(`formatted.${ext}`, text, MIME[current] || "text/plain");
  }

  // ── Format switching — PRESERVES ALL STATE ────────────────────────────
  function selectFormat(fmt) {
    if (fmt === current) return;

    // Save state for the format we're leaving
    saveState(current);
    current = fmt;

    // Update tab active state
    tabs.forEach((t) => {
      const active = t.dataset.format === fmt;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
    });

    if (!SUPPORTED.has(fmt)) {
      workspace.hidden = true;
      unsupported.hidden = false;
      unsupportedText.textContent = UNSUPPORTED_TEXT[fmt] || "This format isn't available yet.";
      return;
    }

    unsupported.hidden = true;
    workspace.hidden = false;
    input.placeholder = PLACEHOLDERS[fmt] || "";
    inputLabel.textContent  = fmt === "markdown" ? "Markdown" : "Input";
    outputLabel.textContent = fmt === "markdown" ? "Preview" : "Output";

    // Rebuild options UI for new format, then restore saved option values
    renderOptions(fmt);
    renderActions(fmt);

    // Restore the saved state for this format (input, output, options, scroll)
    restoreState(fmt);

    Anvil.setStatus(status, `${fmt.toUpperCase()} — paste input and run, or press ⌘/Ctrl+Enter.`, "neutral");
  }

  // ── Ctrl/Cmd+Enter on the formatter page ─────────────────────────────
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      // Only fire if this page has a format-btn (i.e. it's the formatter)
      const btn = document.getElementById("format-btn");
      if (btn) { e.preventDefault(); btn.click(); }
    }
  });

  // ── Input listener ────────────────────────────────────────────────────
  input.addEventListener("input", () => {
    inputCount.textContent = input.value ? `${input.value.length} chars` : "";
    if (current === "markdown") markdownAction();
  });

  // ── Tab clicks ───────────────────────────────────────────────────────
  tabs.forEach((btn) => btn.addEventListener("click", () => selectFormat(btn.dataset.format)));

  // ── Button listeners ─────────────────────────────────────────────────
  document.getElementById("copy-btn").addEventListener("click",     (e) => copyOutput(e.currentTarget));
  document.getElementById("download-btn").addEventListener("click", downloadOutput);
  document.getElementById("clear-btn").addEventListener("click", () => {
    // Explicit destructive action — clear ONLY current format's state
    const s = stateFor(current);
    s.input = ""; s.output = ""; s.outputHtml = ""; s.hasError = false; s.options = {};
    input.value = "";
    output.textContent = ""; output.classList.remove("has-error");
    meta.textContent = ""; inputCount.textContent = "";
    Anvil.setStatus(status, "Cleared.", "neutral");
  });

  // ── File drop ────────────────────────────────────────────────────────
  ["dragover", "drop"].forEach((evt) => dropZone.addEventListener(evt, (e) => e.preventDefault()));
  dropZone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      input.value = reader.result;
      const btn = document.getElementById("format-btn");
      if (btn) btn.click();
    };
    reader.readAsText(file);
  });

  // ── Init ─────────────────────────────────────────────────────────────
  selectFormat("json");

  // We don't call saveState here because there's nothing to save yet —
  // restoreState for "json" will populate from the empty initial state.
})();

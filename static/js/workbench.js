/**
 * Workbench — one page for every operation.
 *
 * STATE INVARIANT:
 *   Changing operation, format, action, or direction NEVER clears the input
 *   text. Only explicit destructive buttons (Clear / Reset) may do so.
 *
 * ARCHITECTURE:
 *   - INPUT drafts live in DRAFTS[slot], one per input slot. A slot is the
 *     operation (formats inside it share the text); the JWT and diff layouts
 *     have their own slots. Switching into an empty slot carries the current
 *     text over, so the text survives every selector change.
 *   - OUTPUT, options and the selected action live in STATE[operation:format].
 *   - Input drafts are saved to sessionStorage through WorkbenchDrafts
 *     (workbench-drafts.js), so a refresh in the same tab keeps them.
 *   - Layouts are built once and shown/hidden; the DOM is not rebuilt on
 *     every selector change.
 *   - The Run button always reads actionSelect.value — never hardcodes the
 *     first action.
 *   - Every Run creates a run record. A response is shown only if it belongs
 *     to the latest run, the same view is still active, and the input is
 *     unchanged since the run started. Otherwise it is discarded.
 */
(function () {
  "use strict";

  // ── DOM refs ─────────────────────────────────────────────────────────────
  const opSelect     = document.getElementById("op-select");
  const formatSelect = document.getElementById("format-select");
  const actionSelect = document.getElementById("action-select");
  const runBtn       = document.getElementById("run-btn");
  const optionsEl    = document.getElementById("wb-options");
  const bodyEl       = document.getElementById("wb-body");
  const statusEl     = document.getElementById("wb-status");
  const unsupported  = document.getElementById("wb-unsupported");
  const unsupportedText = document.getElementById("wb-unsupported-text");

  // ── Operation / format config ─────────────────────────────────────────────
  const OPS = {
    formatter: {
      label: "Formatter",
      formats: ["json", "xml", "yaml", "sql", "markdown"],
      actions: {
        json: ["Beautify", "Minify", "Validate"],
        xml:  ["Beautify", "Minify", "Validate"],
        yaml: ["Beautify", "Validate"],
        sql:  ["Beautify", "Minify"],
        markdown: ["Preview"],
      },
    },
    validator: {
      label: "Validator",
      formats: ["json", "xml", "yaml"],
      actions: { json: ["Validate"], xml: ["Validate"], yaml: ["Validate"] },
    },
    converter: {
      label: "Converter",
      formats: ["json-xml", "json-yaml", "csv-json"],
      formatLabels: { "json-xml": "JSON ↔ XML", "json-yaml": "JSON ↔ YAML", "csv-json": "CSV ↔ JSON" },
      actions: { "json-xml": ["Convert"], "json-yaml": ["Convert"], "csv-json": ["Convert"] },
    },
    encoder: {
      label: "Encoder",
      formats: ["base64", "url", "html-entity"],
      actions: { base64: ["Encode"], url: ["Encode"], "html-entity": ["Encode"] },
    },
    decoder: {
      label: "Decoder",
      formats: ["base64", "jwt", "url", "html-entity"],
      actions: { base64: ["Decode"], jwt: ["Decode"], url: ["Decode"], "html-entity": ["Decode"] },
    },
    diff: {
      label: "Diff / Compare",
      formats: ["json", "text"],
      actions: { json: ["Compare"], text: ["Compare"] },
    },
    generator: {
      label: "Generator",
      formats: ["uuid", "password", "hash"],
      actions: { uuid: ["Generate"], password: ["Generate"], hash: ["Generate"] },
    },
    viewer: { label: "Viewer", formats: ["json"], actions: { json: ["Open"] }, linkOut: true },
    tester: { label: "Tester", formats: ["regex", "cron", "http-status"], actions: { regex: ["Open"], cron: ["Open"], "http-status": ["Open"] }, linkOut: true },
  };

  const LINK_OUT = {
    "viewer:json":       "/json-tree-viewer",
    "tester:regex":      "/regex-tester",
    "tester:cron":       "/cron-parser",
    "tester:http-status":"/http-status-lookup",
  };

  // Current workbench selection
  const wb = { operation: "formatter", format: "json", action: "Beautify" };

  // ── Output / option state, per (operation:format) ─────────────────────────
  // Each key is "operation:format", e.g. "formatter:json", "diff:text"
  const STATE = {};
  function stateKey() { return `${wb.operation}:${wb.format}`; }
  function stateFor(key) {
    if (!STATE[key]) {
      STATE[key] = {
        output: "", outputHtml: "",
        hasError: false, options: {}, scrollA: 0, scrollB: 0,
        action: null,   // last selected action for this op:fmt
      };
    }
    return STATE[key];
  }

  // ── Input drafts, per input slot ──────────────────────────────────────────
  // a = primary input, b = secondary input (diff only).
  // Formats of one operation share a slot, so changing format never changes
  // the text. The JWT box and the diff pair have their own slots.
  const DRAFTS = {};
  function slotFor(op, fmt) {
    if (op === "diff") return "diff";
    if (op === "decoder" && fmt === "jwt") return "jwt";
    return op;
  }
  function currentSlot() { return slotFor(wb.operation, wb.format); }
  function draftFor(slot) {
    if (!DRAFTS[slot]) DRAFTS[slot] = { a: "", b: "" };
    return DRAFTS[slot];
  }

  // ── Draft persistence (sessionStorage; see workbench-drafts.js) ───────────
  const draftStore = (window.WorkbenchDrafts && window.WorkbenchDrafts.createDraftStore)
    ? window.WorkbenchDrafts.createDraftStore()
    : null;
  const draftNote = document.getElementById("wb-draft-note");
  const storageOk = Boolean(draftStore && draftStore.available());
  let _persistTimer = null;

  function persistDrafts() {
    if (draftStore) draftStore.save(DRAFTS);   // failures fall back to in-memory drafts
  }
  function schedulePersist() {
    clearTimeout(_persistTimer);
    _persistTimer = setTimeout(persistDrafts, 250);
  }
  function flushPersist() {
    clearTimeout(_persistTimer);
    persistDrafts();
  }

  function updateDraftNote() {
    if (!draftNote) return;
    draftNote.textContent = storageOk
      ? "Drafts are kept in this tab until you close it. Use Clear to remove them. JWT text is never saved."
      : "Browser storage is unavailable, so drafts are kept only until you refresh the page.";
  }

  // ── Request / run tracking ────────────────────────────────────────────────
  // Every Run gets an id. A response is applied only if its run is still the
  // latest, the view has not changed, and the input still matches what was
  // sent. Anything else is dropped, so an old response can never overwrite a
  // newer result or the user's current text.
  let _runSeq = 0;
  let currentRun = null;
  function startRun() {
    currentRun = {
      id: ++_runSeq,
      key: stateKey(),
      text: primaryText(),
    };
    return currentRun;
  }
  function primaryText() {
    const el = getPrimaryInputEl();
    return el ? el.value : "";
  }
  // Applies `apply()` for a finished run, or explains why it was dropped.
  function present(run, apply) {
    if (!run || run.id !== _runSeq) return false;           // superseded by a newer Run
    if (run.key !== stateKey()) return false;               // user changed operation or format
    if (primaryText() !== run.text) {                       // input edited while running
      setStatus("Input changed while this was running — press Run to update the result.", "neutral");
      return false;
    }
    apply();
    return true;
  }

  // ── Security helpers ──────────────────────────────────────────────────────
  function escHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }
  const SAFE_URL = /^(https?:|mailto:|#|\/)/i;
  function sanitizeHref(href) {
    const t = String(href || "").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").trim();
    const safe = /^(https?:|mailto:|#)/i.test(t) || (t.startsWith("/") && !t.startsWith("//") && !t.startsWith("/\\"));
    return safe ? t : "#";
  }

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function mkEl(tag, attrs) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") e.className = v;
      else e.setAttribute(k, v);
    });
    return e;
  }
  function setTxt(el, text) { el.textContent = text; }
  function setStatus(msg, type) { Anvil.setStatus(statusEl, msg, type || "neutral"); }

  // ── Layout-aware input element lookup ────────────────────────────────────
  // Different layouts expose different primary input elements.
  // These helpers return the correct DOM elements for the current state.
  function getPrimaryInputEl() {
    if (wb.operation === "diff")                              return g("wb-input-a");
    if (wb.operation === "decoder" && wb.format === "jwt")   return g("wb-jwt-input");
    // hash generator uses the two-panel layout which has wb-input
    return g("wb-input");
  }
  function getSecondaryInputEl() {
    if (wb.operation === "diff") return g("wb-input-b");
    return null;
  }

  // ── Save / restore state ─────────────────────────────────────────────────
  function saveState() {
    const key = stateKey();
    const s   = stateFor(key);
    const d   = draftFor(currentSlot());
    const inpA = getPrimaryInputEl();
    const inpB = getSecondaryInputEl();
    const out  = getOutputEl();
    if (inpA) { d.a = inpA.value; s.scrollA = inpA.scrollTop; s.cursorA = inpA.selectionStart; }
    if (inpB) { d.b = inpB.value; s.scrollB = inpB.scrollTop; }
    if (out)  {
      s.output     = out.textContent;
      s.outputHtml = out.innerHTML;
      s.hasError   = out.classList.contains("has-error");
      s.outputScrollTop = out.scrollTop;
    }
    s.action = actionSelect.value;
    // Snapshot option control values
    s.options = {};
    optionsEl.querySelectorAll("select,input[type=checkbox],input[type=number]").forEach((el) => {
      s.options[el.id] = el.type === "checkbox" ? el.checked : el.value;
    });
    // JWT specific: save decoded panels
    if (wb.operation === "decoder" && wb.format === "jwt") {
      const h = g("wb-jwt-header"), p = g("wb-jwt-payload"), sg = g("wb-jwt-sig");
      s.jwtHeader  = h  ? h.textContent  : "";
      s.jwtPayload = p  ? p.textContent  : "";
      s.jwtSig     = sg ? sg.textContent : "";
    }
  }

  function restoreState() {
    const key = stateKey();
    const s   = stateFor(key);
    const d   = draftFor(currentSlot());
    const inpA = getPrimaryInputEl();
    const inpB = getSecondaryInputEl();
    const out  = getOutputEl();
    if (inpA) { inpA.value = d.a || ""; }
    if (inpB) { inpB.value = d.b || ""; }
    if (out) {
      if (s.hasError) {
        out.className = "output-area has-error";
        out.innerHTML = s.outputHtml || "";
      } else if (wb.operation === "formatter" && wb.format === "markdown") {
        out.className = "output-area";
        out.innerHTML = s.outputHtml || "";
      } else {
        out.className = "output-area";
        out.textContent = s.output || "";
      }
    }
    // JWT specific: restore decoded panels
    if (wb.operation === "decoder" && wb.format === "jwt") {
      const h = g("wb-jwt-header"), p = g("wb-jwt-payload"), sg = g("wb-jwt-sig");
      if (h)  h.textContent  = s.jwtHeader  || "";
      if (p)  p.textContent  = s.jwtPayload || "";
      if (sg) sg.textContent = s.jwtSig     || "";
    }
    // Restore action selector
    if (s.action) {
      const opt = actionSelect.querySelector(`option[value="${CSS.escape(s.action)}"]`);
      if (opt) { actionSelect.value = s.action; wb.action = s.action; }
    }
    // Restore option controls after they're in DOM
    requestAnimationFrame(() => {
      Object.entries(s.options).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.type === "checkbox") el.checked = Boolean(val);
        else el.value = val;
      });
      if (inpA) {
        inpA.scrollTop = s.scrollA || 0;
        if (s.cursorA != null) {
          try { inpA.setSelectionRange(s.cursorA, s.cursorA); } catch (_) {}
        }
      }
      if (inpB) inpB.scrollTop = s.scrollB || 0;
      if (out)  out.scrollTop  = s.outputScrollTop || 0;
    });
  }

  function updateMeta(id, text) {
    const m = document.getElementById(id + "-meta");
    if (m) setTxt(m, `${Anvil.formatBytes(new Blob([text]).size)} · ${text.split("\n").length} lines`);
  }

  // ── Layout sections ────────────────────────────────────────────────────────
  // All sections are built once and toggled with hidden.
  let _layoutsBuilt = false;
  const SECTIONS = ["sec-twopanel", "sec-diff", "sec-jwt", "sec-gen-noinput", "sec-linkout"];

  function showSection(id) {
    SECTIONS.forEach((s) => {
      const el = document.getElementById(s);
      if (el) el.hidden = (s !== id);
    });
  }

  function buildLayouts() {
    if (_layoutsBuilt) return;
    _layoutsBuilt = true;
    bodyEl.innerHTML = "";

    // ── Two-panel (used for most operations) ─────────────────────────────
    const twoPanel = mkEl("div", { id: "sec-twopanel" });
    const twoGrid = mkEl("div", { class: "workbench-editors" });
    twoGrid.appendChild(buildPanel("wb-input",  "Input",  false));
    twoGrid.appendChild(buildPanel("wb-output", "Output", true));
    twoPanel.appendChild(twoGrid);
    twoPanel.appendChild(buildActionRow("main"));
    twoPanel.appendChild(buildUtilRow("main"));
    bodyEl.appendChild(twoPanel);

    // ── Diff layout ──────────────────────────────────────────────────────
    const diffSec = mkEl("div", { id: "sec-diff", hidden: "" });
    const diffEds = mkEl("div", { class: "workbench-editors" });
    diffEds.appendChild(buildPanel("wb-input-a", "Left (A)", false));
    diffEds.appendChild(buildPanel("wb-input-b", "Right (B)", false));
    // Map "wb-input" to "wb-input-a" for diff — alias
    diffSec.appendChild(diffEds);
    const diffResult = mkEl("div", { class: "workbench-result-panel", style: "margin-top:1rem;" });
    const diffHead = mkEl("div", { class: "panel-head" });
    const diffHeadSpan = mkEl("span"); setTxt(diffHeadSpan, "Result");
    diffHead.appendChild(diffHeadSpan);
    diffResult.appendChild(diffHead);
    const diffOut = mkEl("div", { id: "wb-diff-output", class: "output-area", role: "status", style: "min-height:160px;" });
    diffResult.appendChild(diffOut);
    diffSec.appendChild(diffResult);
    diffSec.appendChild(buildActionRow("diff"));
    diffSec.appendChild(buildUtilRow("diff"));
    bodyEl.appendChild(diffSec);

    // ── JWT layout ───────────────────────────────────────────────────────
    const jwtSec = mkEl("div", { id: "sec-jwt", hidden: "" });
    jwtSec.appendChild(buildPanel("wb-jwt-input", "JWT", false, "eyJhbGciOi..."));
    const jwtGrid = mkEl("div", { class: "jwt-grid", style: "margin-top:1rem;" });
    jwtGrid.appendChild(buildPanel("wb-jwt-header",  "Header",  true));
    jwtGrid.appendChild(buildPanel("wb-jwt-payload", "Payload", true));
    jwtSec.appendChild(jwtGrid);
    const jwtSigPanel = buildPanel("wb-jwt-sig", "Signature", true);
    jwtSigPanel.style.marginTop = "1rem";
    jwtSec.appendChild(jwtSigPanel);
    jwtSec.appendChild(buildActionRow("jwt"));
    jwtSec.appendChild(buildUtilRow("jwt"));
    bodyEl.appendChild(jwtSec);

    // ── Generator (no input needed) ──────────────────────────────────────
    const genSec = mkEl("div", { id: "sec-gen-noinput", hidden: "" });
    const genResult = mkEl("div", { class: "workbench-result-panel" });
    const genHead = mkEl("div", { class: "panel-head" });
    const genHeadSpan = mkEl("span"); setTxt(genHeadSpan, "Result");
    genHead.appendChild(genHeadSpan);
    genResult.appendChild(genHead);
    const genOut = mkEl("div", { id: "wb-gen-output", class: "output-area", role: "status", style: "min-height:120px;" });
    genResult.appendChild(genOut);
    genSec.appendChild(genResult);
    genSec.appendChild(buildActionRow("gen"));
    genSec.appendChild(buildUtilRow("gen"));
    bodyEl.appendChild(genSec);

    // ── Link-out placeholder ─────────────────────────────────────────────
    const linkSec = mkEl("div", { id: "sec-linkout", hidden: "" });
    const linkInner = mkEl("div", { class: "workbench-unsupported", id: "wb-linkout-inner" });
    linkSec.appendChild(linkInner);
    bodyEl.appendChild(linkSec);

    // Wire utility button handlers and draft tracking for every input box
    wireUtilButtons();
    wireDraftInputs();
  }

  function buildPanel(id, label, isOutput, placeholder) {
    const panel = mkEl("div", { class: "editor-panel", id: id + "-panel" });
    const head  = mkEl("div", { class: "panel-head" });
    const lbl   = mkEl("span", { id: id + "-label" }); setTxt(lbl, label);
    const meta  = mkEl("span", { id: id + "-meta",  class: "meta-row", style: "padding:0;" });
    head.appendChild(lbl); head.appendChild(meta);
    panel.appendChild(head);
    const body = mkEl("div", { class: "editor-body" });
    if (isOutput) {
      body.appendChild(mkEl("div", { id, class: "output-area", role: "status" }));
    } else {
      const ta = mkEl("textarea", { id });
      if (placeholder) ta.placeholder = placeholder;
      body.appendChild(ta);
    }
    panel.appendChild(body);
    return panel;
  }

  function buildActionRow(variant) {
    const bar = mkEl("div", { class: "tool-options wb-action-bar", style: "padding-top:.8rem;" });
    const row = mkEl("div", { class: "btn-row", id: "wb-act-row-" + variant });
    bar.appendChild(row);
    return bar;
  }

  function buildUtilRow(variant) {
    const row  = mkEl("div", { class: "btn-row", style: "margin-top:.5rem;" });
    // The main two-panel row uses un-suffixed ids (wb-copy / wb-download /
    // wb-clear) to match wireUtilButtons(); other variants get "-diff" etc.
    const sfx  = variant && variant !== "main" ? "-" + variant : "";
    ["Copy", "Download", "Clear"].forEach((label) => {
      const btn = mkEl("button", { class: "btn", id: "wb-" + label.toLowerCase() + sfx, type: "button" });
      setTxt(btn, label);
      row.appendChild(btn);
    });
    return row;
  }

  function wireUtilButtons() {
    // ── Main two-panel ───────────────────────────────────────────────────
    on("wb-copy", (e) => {
      const out = g("wb-output");
      Anvil.copyText(out ? (out.textContent || "") : "", e.currentTarget);
    });
    on("wb-download", () => {
      const out = g("wb-output");
      const ext = wb.format.split("-")[0] || "txt";
      const mime = { json:"application/json", xml:"application/xml", yaml:"text/yaml", csv:"text/csv", markdown:"text/html" }[ext] || "text/plain";
      Anvil.download(`result.${ext}`, out ? (out.textContent || "") : "", mime);
    });
    on("wb-clear", () => {
      clearSlotInput(currentSlot());
      const inp = g("wb-input"); if (inp) inp.value = "";
      const out = g("wb-output"); if (out) { out.textContent = ""; out.className = "output-area"; }
      clearOutputState();
      setStatus("Cleared.", "neutral");
    });

    // ── Diff ─────────────────────────────────────────────────────────────
    on("wb-copy-diff", (e) => { const o = g("wb-diff-output"); Anvil.copyText(o ? o.innerText : "", e.currentTarget); });
    on("wb-download-diff", () => { const o = g("wb-diff-output"); Anvil.download("diff.txt", o ? o.innerText : "", "text/plain"); });
    on("wb-clear-diff", () => {
      clearSlotInput("diff");
      const a = g("wb-input-a"); if (a) a.value = "";
      const b = g("wb-input-b"); if (b) b.value = "";
      const o = g("wb-diff-output"); if (o) o.innerHTML = "";
      clearOutputState();
      setStatus("Cleared.", "neutral");
    });

    // ── JWT ──────────────────────────────────────────────────────────────
    on("wb-copy-jwt",    (e) => Anvil.copyText((g("wb-jwt-payload") || {}).textContent || "", e.currentTarget));
    on("wb-download-jwt", () => Anvil.download("jwt-payload.json", (g("wb-jwt-payload") || {}).textContent || "", "application/json"));
    on("wb-clear-jwt",   () => {
      clearSlotInput("jwt");
      const inp = g("wb-jwt-input"); if (inp) inp.value = "";
      ["wb-jwt-header","wb-jwt-payload","wb-jwt-sig"].forEach((id) => { const el = g(id); if (el) el.textContent = ""; });
      clearOutputState();
      setStatus("Cleared.", "neutral");
    });

    // ── Generator ────────────────────────────────────────────────────────
    on("wb-copy-gen",    (e) => { const o = g("wb-gen-output"); Anvil.copyText(o ? (o.textContent || o.innerText) : "", e.currentTarget); });
    on("wb-download-gen", () => { const o = g("wb-gen-output"); Anvil.download("generated.txt", o ? (o.textContent || o.innerText) : "", "text/plain"); });
    on("wb-clear-gen",   () => {
      const o = g("wb-gen-output"); if (o) o.textContent = "";
      clearOutputState();
      setStatus("Cleared.", "neutral");
    });

    // ── Saved drafts (explicit, user-triggered) ──────────────────────────
    on("wb-clear-drafts", () => {
      const hasText = Object.values(DRAFTS).some((d) => d.a || d.b);
      if (hasText && !window.confirm("Remove all saved input drafts from this tab?")) return;
      Object.keys(DRAFTS).forEach((slot) => { DRAFTS[slot] = { a: "", b: "" }; });
      if (draftStore) draftStore.discard();
      restoreState();
      setStatus("Saved drafts removed.", "neutral");
    });
  }

  // Input-level clears only touch the draft for the given slot.
  function clearSlotInput(slot) {
    const d = draftFor(slot);
    d.a = "";
    d.b = "";
    schedulePersist();
  }
  // Clears the output and its cached state for the current view. Options and
  // the selected action are kept, so an explicit Clear does not reset settings.
  function clearOutputState() {
    const s = stateFor(stateKey());
    s.output = ""; s.outputHtml = ""; s.hasError = false;
    const out = getOutputEl();
    if (out) { out.textContent = ""; out.className = "output-area"; }
  }

  // Keep drafts in sync with what the user types, and persist them.
  function wireDraftInputs() {
    ["wb-input", "wb-input-a", "wb-input-b", "wb-jwt-input"].forEach((id) => {
      const el = g(id);
      if (!el) return;
      el.addEventListener("input", () => {
        if (el === getSecondaryInputEl()) draftFor(currentSlot()).b = el.value;
        else if (el === getPrimaryInputEl()) draftFor(currentSlot()).a = el.value;
        else return;
        schedulePersist();
      });
    });
  }

  function g(id) { return document.getElementById(id); }

  // Defensive wiring: a missing element skips its handler instead of
  // throwing and breaking every button wired after it.
  function on(id, fn) { const el = g(id); if (el) el.onclick = fn; }

  // ── Which DOM elements are active for the current view ──────────────────
  function getInputEl()  { if (wb.operation === "diff") return g("wb-input-a"); if (wb.operation === "decoder" && wb.format === "jwt") return g("wb-jwt-input"); return g("wb-input"); }
  function getOutputEl() { if (wb.operation === "diff") return g("wb-diff-output"); if (wb.operation === "generator" && wb.format !== "hash") return g("wb-gen-output"); return g("wb-output"); }
  function getVariant()  { if (wb.operation === "diff") return "diff"; if (wb.operation === "decoder" && wb.format === "jwt") return "jwt"; if (wb.operation === "generator" && wb.format !== "hash") return "gen"; return "main"; }

  // ── Options panel ─────────────────────────────────────────────────────────
  function renderOptions() {
    optionsEl.innerHTML = "";
    const op = wb.operation, fmt = wb.format;

    if (op === "formatter") {
      if (fmt === "json") {
        addSelect("opt-indent", "Indent", [["2","2 spaces"],["4","4 spaces"],["tab","Tab"]]);
        addCheck("opt-sort", "Sort keys");
      } else if (fmt === "xml") {
        addSelect("opt-indent", "Indent", [["2","2 spaces"],["4","4 spaces"]]);
      } else if (fmt === "sql") {
        addSelect("opt-case", "Keywords", [["upper","UPPER"],["lower","lower"],["capitalize","Capitalize"]]);
        addSelect("opt-indent", "Indent", [["2","2"],["4","4"]]);
        addCheck("opt-strip-comments", "Strip comments on Minify");
      } else if (fmt === "yaml") {
        addNote("Format normalizes YAML via round-trip parse — comments and anchors are not preserved.");
      } else if (fmt === "markdown") {
        addNote("Live preview — lightweight renderer (no tables).");
      }
    } else if (op === "converter") {
      const [from, to] = fmt.split("-");
      addSelect("opt-direction", "Direction", [
        ["forward", `${from.toUpperCase()} → ${to.toUpperCase()}`],
        ["reverse", `${to.toUpperCase()} → ${from.toUpperCase()}`],
      ]);
    } else if (op === "encoder" && fmt === "base64") {
      addCheck("opt-urlsafe", "URL-safe (- and _ instead of + and /)");
    } else if (op === "diff" && fmt === "json") {
      addCheck("opt-ignore-key-order",   "Ignore object key order");
      addCheck("opt-ignore-array-order", "Ignore array order");
    } else if (op === "generator") {
      if (fmt === "uuid") {
        addNumberInput("opt-count", "Quantity", 1, 1, 100);
        addCheck("opt-upper",    "Uppercase");
        addCheck("opt-nohyphens","No hyphens");
      } else if (fmt === "password") {
        addNumberInput("opt-length", "Length", 16, 4, 128);
        addCheck("opt-pw-upper",  "Uppercase", true);
        addCheck("opt-pw-lower",  "Lowercase", true);
        addCheck("opt-pw-digits", "Numbers",   true);
        addCheck("opt-pw-symbols","Symbols",   false);
      }
    }
  }

  function addSelect(id, label, opts) {
    const lbl = document.createElement("label");
    const span = document.createElement("span"); span.textContent = label + " ";
    const sel = document.createElement("select"); sel.id = id;
    opts.forEach(([val, text]) => { const o = document.createElement("option"); o.value = val; o.textContent = text; sel.appendChild(o); });
    lbl.appendChild(span); lbl.appendChild(sel);
    optionsEl.appendChild(lbl);
  }
  function addCheck(id, text, defaultChecked) {
    const lbl = document.createElement("label");
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.id = id;
    if (defaultChecked) cb.checked = true;
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + text));
    optionsEl.appendChild(lbl);
  }
  function addNumberInput(id, label, def, min, max) {
    const lbl = document.createElement("label");
    lbl.textContent = label + " ";
    const inp = document.createElement("input");
    inp.type = "number"; inp.id = id; inp.value = def; inp.min = min; inp.max = max;
    inp.style.width = "4.5rem";
    lbl.appendChild(inp);
    optionsEl.appendChild(lbl);
  }
  function addNote(text) {
    const span = document.createElement("span");
    span.style.cssText = "color:var(--text-faint);font-size:.82rem;";
    span.textContent = text;
    optionsEl.appendChild(span);
  }

  // ── Action buttons ────────────────────────────────────────────────────────
  function updateActionButtons() {
    const cfg     = OPS[wb.operation];
    const actions = (cfg.actions && cfg.actions[wb.format]) || ["Run"];
    const rowId   = "wb-act-row-" + getVariant();
    const row     = g(rowId);
    if (!row) return;
    row.innerHTML = "";
    actions.forEach((label, i) => {
      const btn = document.createElement("button");
      btn.className = "btn" + (i === 0 ? " primary" : "");
      btn.type = "button";
      btn.textContent = label;
      btn.addEventListener("click", () => {
        actionSelect.value = label;
        wb.action = label;
        runAction(label);
      });
      row.appendChild(btn);
    });
  }

  // ── Section visibility ────────────────────────────────────────────────────
  function updateSection() {
    const cfg = OPS[wb.operation];
    const isUnsup = cfg.unsupported && cfg.unsupported.includes(wb.format);

    if (isUnsup) {
      unsupported.hidden = false;
      bodyEl.hidden = true;
      optionsEl.hidden = true;
      runBtn.disabled = true;
      unsupportedText.textContent = "This format isn't supported yet — try another.";
      return;
    }
    unsupported.hidden = true;
    bodyEl.hidden = false;
    optionsEl.hidden = false;
    runBtn.disabled = false;

    if (cfg.linkOut) {
      showSection("sec-linkout");
      const inner = g("wb-linkout-inner");
      if (inner) {
        inner.textContent = "";
        const strong = document.createElement("strong"); strong.textContent = "Specialized layout";
        inner.appendChild(strong);
        const span = document.createElement("span"); span.textContent = " — this operation has its own purpose-built interface. ";
        const a = document.createElement("a");
        a.href = LINK_OUT[`${wb.operation}:${wb.format}`] || "/tools";
        a.textContent = "Open it →";
        span.appendChild(a);
        inner.appendChild(span);
      }
      return;
    }

    if (wb.operation === "diff") {
      showSection("sec-diff");
    } else if (wb.operation === "decoder" && wb.format === "jwt") {
      showSection("sec-jwt");
    } else if (wb.operation === "generator" && wb.format === "hash") {
      // Hash needs an input textarea — use the standard two-panel layout
      showSection("sec-twopanel");
      const lbl = g("wb-input-label"); if (lbl) setTxt(lbl, "Text to hash");
      const olbl = g("wb-output-label"); if (olbl) setTxt(olbl, "Hashes");
    } else if (wb.operation === "generator") {
      showSection("sec-gen-noinput");
    } else {
      showSection("sec-twopanel");
    }
  }

  function updatePlaceholder() {
    const inp = g("wb-input"); if (!inp) return;
    const ph = {
      "formatter:json":  '{"name":"John","age":30}',
      "formatter:xml":   "<root><item>1</item></root>",
      "formatter:yaml":  "name: John\nage: 30",
      "formatter:sql":   "select * from users where active = 1",
      "formatter:markdown": "# Hello\n\nSome **bold** text.",
      "validator:json":  '{"name":"John"}',
      "validator:xml":   "<root><item/></root>",
      "validator:yaml":  "name: John",
    };
    inp.placeholder = ph[stateKey()] || "";
  }

  // ── Safe API fetch ────────────────────────────────────────────────────────
  // Resolves with the JSON body, or null on network failure. The input text is
  // never touched here, so a failed request leaves the user's draft intact.
  // Use runApi(), which passes the reply through present() to drop stale ones.
  async function safeApiFetch(endpoint, body) {
    const run = currentRun;
    runBtn.disabled = true;
    const startedAt = (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now();
    // Fire a tool_run analytics event so the admin dashboard can count
    // usage by tool + format. The browser never sends tool input or
    // output — only the tool_slug, operation, format, and action keys.
    if (window.AnvilAnalytics) {
      try {
        window.AnvilAnalytics.trackEvent("tool_run", {
          tool_slug: state.tool,
          page_path: window.location.pathname.slice(0, 2048),
          properties: {
            operation: state.operation,
            format: state.format,
            action: state.action,
            input_size_bytes: typeof (body && body.text) === "string" ? body.text.length : 0,
          },
        });
      } catch (_) { /* analytics must never break the tool */ }
    }
    try {
      const res  = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json();
      // Fire success/error events so the dashboard can compute success rate.
      if (window.AnvilAnalytics && data) {
        try {
          var dur = (typeof performance !== "undefined" && performance.now)
            ? Math.max(0, Math.round(performance.now() - startedAt))
            : 0;
          var ok = !!data.ok;
          window.AnvilAnalytics.trackEvent(ok ? "tool_success" : "tool_error", {
            tool_slug: state.tool,
            page_path: window.location.pathname.slice(0, 2048),
            properties: {
              operation: state.operation,
              format: state.format,
              action: state.action,
              duration_ms: dur,
              outcome: ok ? "ok" : "error",
              error_code: ok ? "" : ((data.error || "").slice(0, 60)),
            },
          });
        } catch (_) { /* ignore */ }
      }
      return data;
    } catch (_) {
      if (run && run.id === _runSeq) setStatus("Network error — could not reach the server.", "error");
      if (window.AnvilAnalytics) {
        try {
          window.AnvilAnalytics.trackEvent("tool_error", {
            tool_slug: state.tool,
            page_path: window.location.pathname.slice(0, 2048),
            properties: {
              operation: state.operation, format: state.format, action: state.action,
              outcome: "error", error_code: "network_error",
            },
          });
        } catch (_) {}
      }
      return null;
    } finally {
      if (run && run.id === _runSeq) runBtn.disabled = false;
    }
  }

  // Sends a request for the current run and hands the reply to `handler`
  // only if present() accepts it. Use this for every async result.
  function runApi(endpoint, body, handler) {
    const run = currentRun;
    return safeApiFetch(endpoint, body).then((data) => {
      if (data) present(run, () => handler(data));
    });
  }

  function clearOutput(msg) {
    const out = getOutputEl();
    if (out) { out.textContent = msg || ""; out.className = "output-area has-error"; }
  }

  // ── JSON helpers ─────────────────────────────────────────────────────────
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
    const m = /position (\d+)/.exec(err.message);
    if (!m) return err.message;
    const pos = Number(m[1]);
    const up  = text.slice(0, pos);
    const line = up.split("\n").length;
    const col  = pos - up.lastIndexOf("\n");
    const lineText = text.split("\n")[line - 1] || "";
    return `${err.message.replace(/position \d+/, `line ${line}, column ${col}`)}\n\n${lineText}\n${" ".repeat(Math.max(0, col - 1))}^`;
  }

  // ── Markdown helpers ──────────────────────────────────────────────────────
  function mdInline(escaped) {
    let t = escaped;
    t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/(?<![\w])_([^_]+)_(?![\w])/g, "<em>$1</em>");
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, lt, href) =>
      `<a href="${escHtml(sanitizeHref(href))}" target="_blank" rel="noopener noreferrer">${lt}</a>`
    );
    return t;
  }
  function mdRender(md) {
    const lines = md.split("\n");
    let html = "", inCode = false, listOpen = null;
    const cl = () => { if (listOpen) { html += `</${listOpen}>`; listOpen = null; } };
    lines.forEach((line) => {
      if (line.trim().startsWith("```")) { inCode = !inCode; html += inCode ? "<pre><code>" : "</code></pre>"; return; }
      if (inCode) { html += escHtml(line) + "\n"; return; }
      const h = /^(#{1,6})\s+(.*)/.exec(line);
      if (h)               { cl(); html += `<h${h[1].length}>${mdInline(escHtml(h[2]))}</h${h[1].length}>`; return; }
      if (/^>\s?/.test(line)) { cl(); html += `<blockquote>${mdInline(escHtml(line.replace(/^>\s?/, "")))}</blockquote>`; return; }
      if (/^\s*[-*]\s+/.test(line)) { if (listOpen !== "ul") { cl(); html += "<ul>"; listOpen = "ul"; } html += `<li>${mdInline(escHtml(line.replace(/^\s*[-*]\s+/, "")))}</li>`; return; }
      if (/^\s*\d+\.\s+/.test(line)) { if (listOpen !== "ol") { cl(); html += "<ol>"; listOpen = "ol"; } html += `<li>${mdInline(escHtml(line.replace(/^\s*\d+\.\s+/, "")))}</li>`; return; }
      cl();
      if (line.trim() === "") return;
      html += `<p>${mdInline(escHtml(line))}</p>`;
    });
    cl();
    return html;
  }

  // ── Unbiased random selection ─────────────────────────────────────────────
  function unbiasedRandom(charset, length) {
    const n = charset.length;
    const limit = 256 - (256 % n);
    const result = [];
    while (result.length < length) {
      const batch = crypto.getRandomValues(new Uint8Array(length * 2));
      for (let i = 0; i < batch.length && result.length < length; i++) {
        if (batch[i] < limit) result.push(charset[batch[i] % n]);
      }
    }
    return result.join("");
  }

  // ── Runners ───────────────────────────────────────────────────────────────

  function runFormatter(action) {
    const fmt    = wb.format;
    const inp    = g("wb-input");
    const output = g("wb-output");
    const text   = inp ? inp.value : "";

    if (!text.trim() && fmt !== "markdown") { setStatus("Enter some input first.", "neutral"); return; }

    if (fmt === "json") {
      let data;
      try { data = JSON.parse(text); }
      catch (err) {
        if (output) { output.className = "output-area has-error"; output.textContent = "Invalid JSON\n" + locateJsonError(text, err); }
        setStatus("Invalid JSON — see output for details.", "error"); return;
      }
      if (output) output.className = "output-area";
      if (action === "Validate") { if (output) output.textContent = "Valid JSON."; setStatus("Valid JSON.", "ok"); return; }
      if (g("opt-sort") && g("opt-sort").checked) data = sortObjectKeys(data);
      const ind = g("opt-indent");
      const indent = ind ? (ind.value === "tab" ? "\t" : Number(ind.value)) : 2;
      const result = action === "Minify" ? JSON.stringify(data) : JSON.stringify(data, null, indent);
      if (output) { output.textContent = result; updateMeta("wb-output", result); }
      setStatus(action === "Minify" ? "Minified." : "Formatted.", "ok"); return;
    }

    if (fmt === "xml") {
      if (action === "Validate") {
        runApi("/api/xml-validate", { text }, (data) => {
          const out = g("wb-output"); if (!out) return;
          if (data.valid) {
            out.className = "output-area"; out.textContent = "Valid XML."; setStatus("Valid XML.", "ok");
          } else {
            const li = data.line ? ` at line ${data.line}, column ${data.column}` : "";
            out.className = "output-area has-error"; out.textContent = `Invalid XML${li}\n${data.message || ""}`;
            setStatus("Invalid XML.", "error");
          }
        }); return;
      }
      const ind = g("opt-indent");
      const indent = ind ? (ind.value === "tab" ? "\t" : " ".repeat(Number(ind.value))) : "  ";
      runApi("/api/xml-format", { text, minify: action === "Minify", indent }, (data) => {
        const out = g("wb-output"); if (!out) return;
        if (!data.ok) { out.className = "output-area has-error"; out.textContent = data.error || "Error."; setStatus(data.error || "Error.", "error"); return; }
        out.className = "output-area"; out.textContent = data.result; updateMeta("wb-output", data.result);
        setStatus(action === "Minify" ? "Minified." : "Formatted.", "ok");
      }); return;
    }

    if (fmt === "yaml") {
      setStatus("Processing…", "neutral");
      const run = currentRun;
      const postJson = (url, payload) =>
        fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
          .then((r) => r.json());
      const showError = (msg) => { clearOutput(msg); setStatus(msg, "error"); };
      const showOutput = (text, status) => {
        const out = g("wb-output");
        if (out) { out.className = "output-area"; out.textContent = text; updateMeta("wb-output", text); }
        setStatus(status, "ok");
      };
      postJson("/api/yaml-to-json", { text })
        .then((toJson) => {
          if (!toJson.ok) return present(run, () => showError(toJson.error || "Invalid YAML."));
          if (action === "Validate") return present(run, () => {
            const out = g("wb-output"); if (out) { out.className = "output-area"; out.textContent = "Valid YAML."; }
            setStatus("Valid YAML.", "ok");
          });
          return postJson("/api/json-to-yaml", { text: toJson.result }).then((toYaml) =>
            present(run, () => {
              if (!toYaml.ok) return showError(toYaml.error || "Error.");
              showOutput(toYaml.result, "Formatted. (Comments and anchors are not preserved.)");
            })
          );
        })
        .catch(() => { if (run.id === _runSeq) setStatus("Network error.", "error"); })
        .finally(() => { if (run.id === _runSeq) runBtn.disabled = false; });
      return;
    }

    if (fmt === "sql") {
      const cSel = g("opt-case"), iSel = g("opt-indent"), sc = g("opt-strip-comments");
      runApi("/api/sql-format", {
        text, action: action === "Minify" ? "minify" : "format",
        keyword_case: cSel ? cSel.value : "upper",
        indent_width: iSel ? Number(iSel.value) : 2,
        strip_comments: action === "Minify" && sc ? sc.checked : false,
      }, (data) => {
        const out = g("wb-output"); if (!out) return;
        if (!data.ok) { out.className = "output-area has-error"; out.textContent = data.error || "Error."; setStatus(data.error || "Error.", "error"); return; }
        out.className = "output-area"; out.textContent = data.result; updateMeta("wb-output", data.result);
        setStatus(action === "Minify" ? "Minified." : "Formatted.", "ok");
      }); return;
    }

    if (fmt === "markdown") {
      const out = g("wb-output"); if (!out) return;
      const html = mdRender(text);
      out.innerHTML = html || `<span style="color:var(--text-faint)">Preview appears here as you type.</span>`;
      stateFor(stateKey()).outputHtml = html;
      setStatus("Preview updated.", "ok");
    }
  }

  function runValidator() {
    const fmt  = wb.format;
    const text = g("wb-input") ? g("wb-input").value : "";
    const out  = g("wb-output");
    if (!text.trim()) { setStatus("Enter some input first.", "neutral"); return; }

    if (fmt === "json") {
      try {
        JSON.parse(text);
        if (out) { out.className = "output-area"; const ok = document.createElement("span"); ok.style.color = "var(--good)"; ok.textContent = "✓ Valid JSON"; out.textContent = ""; out.appendChild(ok); }
        setStatus("Valid JSON.", "ok");
      } catch (err) {
        if (out) { out.className = "output-area has-error"; out.textContent = "✗ Invalid JSON\n\n" + locateJsonError(text, err); }
        setStatus("Invalid JSON.", "error");
      }
      return;
    }
    if (fmt === "xml") {
      runApi("/api/xml-validate", { text }, (data) => {
        const o = g("wb-output"); if (!o) return;
        if (data.valid) {
          o.className = "output-area"; const ok = document.createElement("span"); ok.style.color = "var(--good)"; ok.textContent = "✓ Valid XML"; o.textContent = ""; o.appendChild(ok);
          setStatus("Valid XML.", "ok");
        } else {
          const li = data.line ? ` at line ${data.line}, column ${data.column}` : "";
          o.className = "output-area has-error"; o.textContent = `✗ Invalid XML${li}\n\n${data.message || ""}`;
          setStatus("Invalid XML.", "error");
        }
      }); return;
    }
    if (fmt === "yaml") {
      runApi("/api/yaml-to-json", { text }, (data) => {
        const o = g("wb-output"); if (!o) return;
        if (data.ok) {
          o.className = "output-area"; const ok = document.createElement("span"); ok.style.color = "var(--good)"; ok.textContent = "✓ Valid YAML"; o.textContent = ""; o.appendChild(ok);
          setStatus("Valid YAML.", "ok");
        } else {
          o.className = "output-area has-error"; o.textContent = "✗ Invalid YAML\n\n" + (data.error || "");
          setStatus("Invalid YAML.", "error");
        }
      });
    }
  }

  function runConverter() {
    const pair = wb.format;
    const dir  = (g("opt-direction") || {}).value || "forward";
    const text = g("wb-input") ? g("wb-input").value : "";
    if (!text.trim()) { setStatus("Enter some input first.", "neutral"); return; }
    const map = {
      "json-xml":  { forward: "/api/json-to-xml",  reverse: "/api/xml-to-json" },
      "json-yaml": { forward: "/api/json-to-yaml",  reverse: "/api/yaml-to-json" },
      "csv-json":  { forward: "/api/csv-to-json",   reverse: "/api/json-to-csv" },
    };
    const endpoint = map[pair][dir === "forward" ? "forward" : "reverse"];
    runApi(endpoint, { text }, (data) => {
      const o = g("wb-output"); if (!o) return;
      if (!data.ok) { o.className = "output-area has-error"; o.textContent = data.error || "Error."; setStatus(data.error || "Error.", "error"); return; }
      o.className = "output-area"; o.textContent = data.result; updateMeta("wb-output", data.result);
      setStatus("Converted.", "ok");
    });
  }

  function runEncoder() {
    const fmt  = wb.format;
    const text = g("wb-input") ? g("wb-input").value : "";
    const out  = g("wb-output");
    if (!text) { setStatus("Enter some text first.", "neutral"); return; }
    try {
      if (fmt === "base64") {
        const us = g("opt-urlsafe") && g("opt-urlsafe").checked;
        let r = btoa(unescape(encodeURIComponent(text)));
        if (us) r = r.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
        if (out) out.textContent = r;
      } else if (fmt === "url") {
        if (out) out.textContent = encodeURIComponent(text);
      } else if (fmt === "html-entity") {
        if (out) out.textContent = text.replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
      }
      if (out) out.className = "output-area";
      setStatus("Encoded.", "ok");
    } catch (e) { setStatus("Could not encode: " + e.message, "error"); }
  }

  function b64UrlDecode(str) {
    str = str.replace(/-/g, "+").replace(/_/g, "/");
    while (str.length % 4) str += "=";
    return decodeURIComponent(atob(str).split("").map((c) => "%" + c.charCodeAt(0).toString(16).padStart(2, "0")).join(""));
  }

  function runDecoder() {
    const fmt = wb.format;
    if (fmt === "jwt") { runJwt(g("wb-jwt-input") ? g("wb-jwt-input").value : ""); return; }
    const text = g("wb-input") ? g("wb-input").value : "";
    const out  = g("wb-output");
    if (!text.trim()) { setStatus("Enter some input first.", "neutral"); return; }
    if (fmt === "base64") {
      try { if (out) { out.textContent = decodeURIComponent(escape(atob(text.replace(/-/g,"+").replace(/_/g,"/")))); out.className = "output-area"; } setStatus("Decoded.", "ok"); }
      catch (e) { if (out) { out.textContent = "Invalid Base64."; out.className = "output-area has-error"; } setStatus("Invalid Base64.", "error"); }
      return;
    }
    if (fmt === "url") {
      try { if (out) { out.textContent = decodeURIComponent(text); out.className = "output-area"; } setStatus("Decoded.", "ok"); }
      catch (e) { if (out) { out.textContent = "Invalid URL-encoded input."; out.className = "output-area has-error"; } setStatus("Invalid URL-encoded input.", "error"); }
      return;
    }
    if (fmt === "html-entity") {
      const ta = document.createElement("textarea"); ta.innerHTML = text;
      if (out) { out.textContent = ta.value; out.className = "output-area"; }
      setStatus("Decoded.", "ok");
    }
  }

  function runJwt(token) {
    if (!token || !token.trim()) { setStatus("Enter a JWT first.", "neutral"); return; }
    const parts = token.trim().split(".");
    if (parts.length !== 3) { setStatus("Not a valid JWT — expected 3 dot-separated parts.", "error"); return; }
    try {
      const header  = JSON.parse(b64UrlDecode(parts[0]));
      const payload = JSON.parse(b64UrlDecode(parts[1]));
      const hEl = g("wb-jwt-header");  if (hEl) hEl.textContent = JSON.stringify(header, null, 2);
      const pEl = g("wb-jwt-payload"); if (pEl) pEl.textContent = JSON.stringify(payload, null, 2);
      const sEl = g("wb-jwt-sig");
      if (sEl) {
        sEl.textContent = "";
        const warn = document.createElement("span"); warn.style.color = "var(--warn)";
        warn.textContent = "Decoded — signature NOT verified.";
        sEl.appendChild(warn);
        sEl.appendChild(document.createTextNode("\nDo not trust these claims without verifying against the issuer's key."));
      }
      setStatus("Decoded (not verified).", "ok");
    } catch (e) { setStatus("Could not decode token — " + e.message, "error"); }
  }

  function runDiff() {
    const fmt = wb.format;
    const a   = g("wb-input-a") ? g("wb-input-a").value : "";
    const b   = g("wb-input-b") ? g("wb-input-b").value : "";
    const out = g("wb-diff-output");
    if (!out) return;
    out.innerHTML = "";

    if (fmt === "json") {
      let docA, docB;
      try { docA = JSON.parse(a); } catch (e) { setStatus("Left document: invalid JSON — " + e.message, "error"); return; }
      try { docB = JSON.parse(b); } catch (e) { setStatus("Right document: invalid JSON — " + e.message, "error"); return; }

      const ignoreKeyOrder   = g("opt-ignore-key-order")   && g("opt-ignore-key-order").checked;
      const ignoreArrayOrder = g("opt-ignore-array-order") && g("opt-ignore-array-order").checked;

      function normalise(v) {
        if (Array.isArray(v)) {
          const items = v.map(normalise);
          if (ignoreArrayOrder) items.sort((a, b) => { const sa = JSON.stringify(a), sb = JSON.stringify(b); return sa < sb ? -1 : sa > sb ? 1 : 0; });
          return items;
        }
        if (v && typeof v === "object") {
          const out = {};
          const keys = Object.keys(v);
          if (ignoreKeyOrder) keys.sort();
          keys.forEach((k) => (out[k] = normalise(v[k])));
          return out;
        }
        return v;
      }

      function buildPath(prefix, key) {
        const ks = String(key);
        const simple = /^[a-zA-Z_$][a-zA-Z0-9_$]*$/.test(ks);
        return prefix ? (simple ? `${prefix}.${ks}` : `${prefix}["${ks.replace(/\\/g,"\\\\").replace(/"/g,'\\"')}"]`) : (simple ? ks : `["${ks}"]`);
      }

      const results = [];
      (function diffRec(path, oV, nV) {
        const oObj = oV && typeof oV === "object";
        const nObj = nV && typeof nV === "object";
        if (!oObj || !nObj || Array.isArray(oV) !== Array.isArray(nV)) {
          if (JSON.stringify(oV) !== JSON.stringify(nV)) results.push({ type: "changed", path, oV, nV });
          return;
        }
        const oKeys = Array.isArray(oV) ? oV.map((_, i) => i) : Object.keys(oV);
        const nKeys = Array.isArray(nV) ? nV.map((_, i) => i) : Object.keys(nV);
        new Set([...oKeys, ...nKeys]).forEach((k) => {
          const cp = buildPath(path, k);
          const inO = Object.prototype.hasOwnProperty.call(oV, k);
          const inN = Object.prototype.hasOwnProperty.call(nV, k);
          if (inO && !inN) results.push({ type: "removed", path: cp, oV: oV[k] });
          else if (!inO && inN) results.push({ type: "added",   path: cp, nV: nV[k] });
          else diffRec(cp, oV[k], nV[k]);
        });
      })("", normalise(docA), normalise(docB));

      if (results.length === 0) {
        const ok = document.createElement("span"); ok.style.color = "var(--good)"; ok.textContent = "No differences — documents are structurally identical."; out.appendChild(ok);
      } else {
        results.forEach((r) => {
          const div = document.createElement("div");
          div.className = r.type === "added" ? "diff-add" : r.type === "removed" ? "diff-del" : "diff-mod";
          if (r.type === "added")   div.textContent = `+ ${r.path}: ${JSON.stringify(r.nV)}`;
          else if (r.type === "removed") div.textContent = `- ${r.path}: ${JSON.stringify(r.oV)}`;
          else div.textContent = `${r.path}\n  OLD: ${JSON.stringify(r.oV)}\n  NEW: ${JSON.stringify(r.nV)}`;
          out.appendChild(div);
        });
      }
      setStatus(`Compared — ${results.length} difference(s).`, results.length ? "error" : "ok");
      return;
    }

    // Text diff using LCS
    function lcsDiff(lA, lB) {
      const n = lA.length, m = lB.length;
      const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
      for (let i = n - 1; i >= 0; i--)
        for (let j = m - 1; j >= 0; j--)
          dp[i][j] = lA[i] === lB[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);
      const res = [];
      let i = 0, j = 0;
      while (i < n && j < m) {
        if (lA[i] === lB[j]) { res.push({ t: "=", l: lA[i] }); i++; j++; }
        else if (dp[i+1][j] >= dp[i][j+1]) { res.push({ t: "-", l: lA[i] }); i++; }
        else { res.push({ t: "+", l: lB[j] }); j++; }
      }
      while (i < n) { res.push({ t: "-", l: lA[i++] }); }
      while (j < m) { res.push({ t: "+", l: lB[j++] }); }
      return res;
    }
    const linesA = a.split("\n"), linesB = b.split("\n");
    const diff = lcsDiff(linesA, linesB);
    let changes = 0;
    diff.forEach(({ t, l }) => {
      const div = document.createElement("div");
      if (t === "+") { div.className = "diff-add"; div.textContent = "+ " + l; changes++; }
      else if (t === "-") { div.className = "diff-del"; div.textContent = "- " + l; changes++; }
      else { div.textContent = "  " + l; }
      out.appendChild(div);
    });
    setStatus(`Compared — ${changes} changed line(s).`, changes ? "error" : "ok");
  }

  async function subtleHash(algo, bytes) {
    const buf = await crypto.subtle.digest(algo, bytes);
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function runGenerator() {
    const fmt = wb.format;
    const out = g("wb-gen-output") || g("wb-output");
    if (!out) return;

    if (fmt === "uuid") {
      const count   = Math.max(1, Math.min(Number((g("opt-count") || {}).value || 1), 100));
      const upper   = g("opt-upper")    && g("opt-upper").checked;
      const noHyph  = g("opt-nohyphens")&& g("opt-nohyphens").checked;
      const ids = Array.from({ length: count }, () => crypto.randomUUID())
        .map((id) => noHyph ? id.replace(/-/g,"") : id)
        .map((id) => upper  ? id.toUpperCase()     : id);
      out.textContent = ids.join("\n");
      setStatus(`Generated ${ids.length} UUID(s).`, "ok");
    } else if (fmt === "password") {
      const length = Math.max(4, Math.min(Number((g("opt-length") || {}).value || 16), 128));
      let charset = "";
      if (!g("opt-pw-upper")  || g("opt-pw-upper").checked)  charset += "ABCDEFGHJKLMNPQRSTUVWXYZ";
      if (!g("opt-pw-lower")  || g("opt-pw-lower").checked)  charset += "abcdefghijkmnpqrstuvwxyz";
      if (!g("opt-pw-digits") || g("opt-pw-digits").checked) charset += "23456789";
      if (g("opt-pw-symbols") && g("opt-pw-symbols").checked) charset += "!@#$%^&*-_=+";
      if (!charset) { setStatus("Enable at least one character set.", "error"); return; }
      out.textContent = unbiasedRandom(charset, length);
      setStatus("Generated.", "ok");
    } else if (fmt === "hash") {
      const text = g("wb-input") ? g("wb-input").value : "";
      if (!text) { setStatus("Enter some text first.", "neutral"); return; }
      const run = currentRun;
      const bytes = new TextEncoder().encode(text);
      const rows = [];
      for (const { id, weak } of [{ id:"MD5",weak:true },{ id:"SHA-1",weak:true },{ id:"SHA-256",weak:false },{ id:"SHA-384",weak:false },{ id:"SHA-512",weak:false }]) {
        const hash = id === "MD5" ? md5(text) : await subtleHash(id, bytes);
        rows.push({ id, weak, hash });
      }
      // The awaits above can outlive the view, so write only if still current.
      present(run, () => {
        const target = getOutputEl();
        if (!target) return;
        target.textContent = "";
        rows.forEach(({ id, weak, hash }) => {
          const div = document.createElement("div");
          const strong = document.createElement("strong"); strong.textContent = id;
          div.appendChild(strong);
          if (weak) { const badge = document.createElement("span"); badge.className = "badge weak"; badge.textContent = " weak"; div.appendChild(badge); }
          div.appendChild(document.createTextNode(": " + hash));
          target.appendChild(div);
        });
        setStatus("Generated.", "ok");
      });
    }
  }

  // ── Run dispatcher ────────────────────────────────────────────────────────
  function runAction(action) {
    wb.action = action;
    startRun();   // every Run supersedes older in-flight responses
    if (wb.operation === "formatter")  return runFormatter(action);
    if (wb.operation === "validator")  return runValidator();
    if (wb.operation === "converter")  return runConverter();
    if (wb.operation === "encoder")    return runEncoder();
    if (wb.operation === "decoder")    return runDecoder();
    if (wb.operation === "diff")       return runDiff();
    if (wb.operation === "generator")  return runGenerator();
  }

  // Run button always reads the selected action — never hardcodes actions[0]
  runBtn.addEventListener("click", () => runAction(actionSelect.value || wb.action));

  // ── Selector population ───────────────────────────────────────────────────
  function fmtLabel(op, fmt) {
    const cfg = OPS[op];
    return (cfg.formatLabels && cfg.formatLabels[fmt]) ? cfg.formatLabels[fmt] : fmt.toUpperCase();
  }

  function populateFormats() {
    const cfg = OPS[wb.operation];
    formatSelect.innerHTML = "";
    cfg.formats.forEach((f) => {
      const o = document.createElement("option"); o.value = f; o.textContent = fmtLabel(wb.operation, f);
      formatSelect.appendChild(o);
    });
    if (!cfg.formats.includes(wb.format)) wb.format = cfg.formats[0];
    formatSelect.value = wb.format;
  }

  function populateActions() {
    const cfg     = OPS[wb.operation];
    const actions = (cfg.actions && cfg.actions[wb.format]) || ["Run"];
    actionSelect.innerHTML = "";
    actions.forEach((a) => {
      const o = document.createElement("option"); o.value = a; o.textContent = a;
      actionSelect.appendChild(o);
    });
    // Restore last action for this op:fmt if saved
    const saved = stateFor(stateKey()).action;
    if (saved && actions.includes(saved)) { actionSelect.value = saved; wb.action = saved; }
    else { actionSelect.value = actions[0]; wb.action = actions[0]; }
  }

  // ── Selector change handlers ──────────────────────────────────────────────
  // Switches the view without losing text. The outgoing draft is saved first.
  // If the incoming slot uses the same input box and has no text yet, the
  // current text is carried over, so changing operation never empties the box.
  // A slot that already has its own text keeps it.
  function switchView(mutate) {
    const prevEl  = getPrimaryInputEl();
    const prevId  = prevEl ? prevEl.id : null;
    const carry   = prevEl ? prevEl.value : "";
    saveState();
    mutate();
    const nextEl = getPrimaryInputEl();
    if (nextEl && nextEl.id === prevId && carry) {
      const d = draftFor(currentSlot());
      if (!d.a) d.a = carry;
    }
    render();
    schedulePersist();
  }

  opSelect.addEventListener("change", () => {
    switchView(() => {
      wb.operation = opSelect.value;
      populateFormats();
      populateActions();
    });
    setStatus(`${OPS[wb.operation].label} · ${fmtLabel(wb.operation, wb.format)} — press Run or ⌘/Ctrl+Enter.`, "neutral");
  });

  formatSelect.addEventListener("change", () => {
    switchView(() => {
      wb.format = formatSelect.value;
      populateActions();
    });
  });

  actionSelect.addEventListener("change", () => { wb.action = actionSelect.value; });

  // ── Full render ───────────────────────────────────────────────────────────
  function render() {
    buildLayouts();          // idempotent
    renderOptions();
    updateSection();
    updateActionButtons();
    updatePlaceholder();
    restoreState();          // restore saved input/output/options for this op:fmt

    // Wire markdown live preview for formatter:markdown
    const mdInp = g("wb-input");
    if (wb.operation === "formatter" && wb.format === "markdown" && mdInp && !mdInp._mdWired) {
      mdInp._mdWired = true;
      mdInp.addEventListener("input", () => { if (wb.format === "markdown") runFormatter("Preview"); });
    }
  }

  // ── Op select population ─────────────────────────────────────────────────
  opSelect.innerHTML = "";
  Object.entries(OPS).forEach(([id, cfg]) => {
    const o = document.createElement("option"); o.value = id; o.textContent = cfg.label;
    opSelect.appendChild(o);
  });

  // ── Restore saved input drafts (same tab, survives refresh) ──────────────
  if (draftStore) Object.assign(DRAFTS, draftStore.load());
  updateDraftNote();
  window.addEventListener("pagehide", flushPersist);

  // ── Deep link ────────────────────────────────────────────────────────────
  (function applyDeepLink() {
    const p = new URLSearchParams(window.location.search);
    const op = p.get("operation"), fmt = p.get("format");
    if (op && OPS[op]) wb.operation = op;
    opSelect.value = wb.operation;
    populateFormats();
    if (fmt && OPS[wb.operation].formats.includes(fmt)) wb.format = fmt;
    formatSelect.value = wb.format;
    populateActions();
    render();
    setStatus(`${OPS[wb.operation].label} · ${fmtLabel(wb.operation, wb.format)} — press Run or ⌘/Ctrl+Enter.`, "neutral");
  })();

  // ── Keyboard shortcut: Ctrl/Cmd+Enter ────────────────────────────────────
  // Only fires when focus is inside the workbench, never for header navigation.
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      const active = document.activeElement;
      const inWb   = bodyEl.contains(active) || active === document.body || active === document.documentElement;
      if (inWb) { e.preventDefault(); runAction(actionSelect.value || wb.action); }
    }
  });
})();

(function () {
  "use strict";
  const input = document.getElementById("input");
  const output = document.getElementById("output");
  const status = document.getElementById("status");
  const meta = document.getElementById("meta-row");
  const inputCount = document.getElementById("input-count");

  function currentIndent() {
    const v = document.getElementById("indent").value;
    return v === "tab" ? "\t" : Number(v);
  }

  function sortObjectKeys(value) {
    if (Array.isArray(value)) return value.map(sortObjectKeys);
    if (value && typeof value === "object") {
      const sorted = {};
      Object.keys(value).sort().forEach((k) => (sorted[k] = sortObjectKeys(value[k])));
      return sorted;
    }
    return value;
  }

  function locateError(text, err) {
    const match = /position (\d+)/.exec(err.message);
    if (!match) return err.message;
    const pos = Number(match[1]);
    const upToError = text.slice(0, pos);
    const line = upToError.split("\n").length;
    const column = pos - upToError.lastIndexOf("\n");
    return `${err.message.replace(/position \d+/, `line ${line}, column ${column}`)}`;
  }

  function parseOrFail() {
    const text = input.value;
    if (!text.trim()) {
      Anvil.setStatus(status, "Enter some JSON first.", "neutral");
      output.textContent = "";
      meta.textContent = "";
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (err) {
      Anvil.setStatus(status, "Invalid JSON — " + locateError(text, err), "error");
      output.textContent = "";
      meta.textContent = "";
      return undefined;
    }
  }

  function updateMeta(text) {
    const lines = text.split("\n").length;
    meta.textContent = `${Anvil.formatBytes(new Blob([text]).size)} · ${lines} lines`;
  }

  function format() {
    let data = parseOrFail();
    if (data === null || data === undefined) return;
    if (document.getElementById("sort-keys").checked) data = sortObjectKeys(data);
    const result = JSON.stringify(data, null, currentIndent());
    output.textContent = result;
    updateMeta(result);
    Anvil.setStatus(status, "Valid JSON — formatted.", "ok");
  }

  function minify() {
    let data = parseOrFail();
    if (data === null || data === undefined) return;
    if (document.getElementById("sort-keys").checked) data = sortObjectKeys(data);
    const result = JSON.stringify(data);
    output.textContent = result;
    updateMeta(result);
    Anvil.setStatus(status, "Valid JSON — minified.", "ok");
  }

  document.getElementById("format-btn").addEventListener("click", format);
  document.getElementById("minify-btn").addEventListener("click", minify);
  document.getElementById("clear-btn").addEventListener("click", () => {
    input.value = ""; output.textContent = ""; meta.textContent = "";
    Anvil.setStatus(status, "Cleared.", "neutral");
  });
  document.getElementById("copy-btn").addEventListener("click", (e) => Anvil.copyText(output.textContent, e.target));
  document.getElementById("download-btn").addEventListener("click", () => Anvil.download("formatted.json", output.textContent, "application/json"));

  input.addEventListener("input", () => inputCount.textContent = input.value ? `${input.value.length} chars` : "");

  const dropZone = document.getElementById("drop-zone");
  ["dragover", "drop"].forEach((evt) => dropZone.addEventListener(evt, (e) => e.preventDefault()));
  dropZone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { input.value = reader.result; format(); };
    reader.readAsText(file);
  });
})();

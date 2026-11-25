(function () {
  "use strict";
  const a = document.getElementById("input-a");
  const b = document.getElementById("input-b");
  const output = document.getElementById("output");
  const status = document.getElementById("status");

  const MAX_DEPTH = 100;
  const MAX_INPUT_SIZE = 2_000_000; // 2 million characters

  // Safe path builder: use bracket notation for keys containing dots/special chars
  function buildPath(pathPrefix, key) {
    const keyStr = String(key);
    // Use dot notation only for simple identifiers; bracket notation otherwise
    if (/^[a-zA-Z_$][a-zA-Z0-9_$]*$/.test(keyStr)) {
      return pathPrefix ? `${pathPrefix}.${keyStr}` : keyStr;
    }
    return pathPrefix ? `${pathPrefix}["${keyStr.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]` : `["${keyStr.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"]`;
  }

  function diff(pathPrefix, oldVal, newVal, results, depth = 0) {
    // Protect against excessively deep nesting
    if (depth > MAX_DEPTH) {
      throw new Error(`JSON structure exceeds maximum nesting depth of ${MAX_DEPTH}.`);
    }

    const oldIsObj = oldVal && typeof oldVal === "object";
    const newIsObj = newVal && typeof newVal === "object";

    if (!oldIsObj || !newIsObj || Array.isArray(oldVal) !== Array.isArray(newVal)) {
      if (JSON.stringify(oldVal) !== JSON.stringify(newVal)) {
        results.push({ type: "changed", path: pathPrefix, oldVal, newVal });
      }
      return;
    }

    const oldKeys = Array.isArray(oldVal) ? oldVal.map((_, i) => i) : Object.keys(oldVal);
    const newKeys = Array.isArray(newVal) ? newVal.map((_, i) => i) : Object.keys(newVal);
    const allKeys = new Set([...oldKeys, ...newKeys]);

    allKeys.forEach((key) => {
      const childPath = buildPath(pathPrefix, key);
      const inOld = Object.prototype.hasOwnProperty.call(oldVal, key);
      const inNew = Object.prototype.hasOwnProperty.call(newVal, key);
      if (inOld && !inNew) results.push({ type: "removed", path: childPath, oldVal: oldVal[key] });
      else if (!inOld && inNew) results.push({ type: "added", path: childPath, newVal: newVal[key] });
      else diff(childPath, oldVal[key], newVal[key], results, depth + 1);
    });
  }

  function render(results) {
    output.innerHTML = "";
    if (results.length === 0) {
      const span = document.createElement("span");
      span.style.color = "var(--good)";
      span.textContent = "No differences — documents are structurally identical.";
      output.appendChild(span);
      return;
    }
    results.forEach((r) => {
      const div = document.createElement("div");
      div.className = r.type === "added" ? "diff-add" : r.type === "removed" ? "diff-del" : "diff-mod";
      // SECURITY: use textContent — never innerHTML with user data
      if (r.type === "added") {
        div.textContent = `+ ${r.path}: ${JSON.stringify(r.newVal)}`;
      } else if (r.type === "removed") {
        div.textContent = `- ${r.path}: ${JSON.stringify(r.oldVal)}`;
      } else {
        div.textContent = `${r.path}\n  OLD: ${JSON.stringify(r.oldVal)}\n  NEW: ${JSON.stringify(r.newVal)}`;
      }
      output.appendChild(div);
    });
  }

  function normalise(v, ignoreKeyOrder, ignoreArrayOrder, depth = 0) {
    if (depth > MAX_DEPTH) {
      throw new Error(`JSON structure exceeds maximum nesting depth of ${MAX_DEPTH}.`);
    }

    if (Array.isArray(v)) {
      const items = v.map((i) => normalise(i, ignoreKeyOrder, ignoreArrayOrder, depth + 1));
      if (ignoreArrayOrder) items.sort((a, b) => { const sa = JSON.stringify(a), sb = JSON.stringify(b); return sa < sb ? -1 : sa > sb ? 1 : 0; });
      return items;
    }
    if (v && typeof v === "object") {
      const out = {};
      const keys = Object.keys(v);
      if (ignoreKeyOrder) keys.sort();
      keys.forEach((k) => (out[k] = normalise(v[k], ignoreKeyOrder, ignoreArrayOrder, depth + 1)));
      return out;
    }
    return v;
  }

  function compare() {
    // Clear previous results
    output.innerHTML = "";

    // Validate input sizes
    if (a.value.length > MAX_INPUT_SIZE) {
      Anvil.setStatus(status, `Document A exceeds maximum size (${MAX_INPUT_SIZE.toLocaleString()} characters).`, "error");
      return;
    }
    if (b.value.length > MAX_INPUT_SIZE) {
      Anvil.setStatus(status, `Document B exceeds maximum size (${MAX_INPUT_SIZE.toLocaleString()} characters).`, "error");
      return;
    }

    let docA, docB;
    try {
      docA = JSON.parse(a.value);
    } catch (e) {
      Anvil.setStatus(status, "Document A is invalid JSON — " + e.message, "error");
      return;
    }
    try {
      docB = JSON.parse(b.value);
    } catch (e) {
      Anvil.setStatus(status, "Document B is invalid JSON — " + e.message, "error");
      return;
    }

    try {
      const ignoreKeyOrder = document.getElementById("ignore-order") && document.getElementById("ignore-order").checked;
      const ignoreArrayOrder = document.getElementById("ignore-array-order") && document.getElementById("ignore-array-order").checked;

      const normA = normalise(docA, ignoreKeyOrder, ignoreArrayOrder);
      const normB = normalise(docB, ignoreKeyOrder, ignoreArrayOrder);

      const results = [];
      diff("", normA, normB, results);
      render(results);
      // Enable the Copy button only after we have a result to copy.
      const copyBtn = document.getElementById("copy-btn");
      if (copyBtn) copyBtn.disabled = false;
      Anvil.setStatus(status, `Compared — ${results.length} difference(s) found.`, results.length ? "error" : "ok");
    } catch (err) {
      Anvil.setStatus(status, `Error: ${err.message}`, "error");
    }
  }

  document.getElementById("diff-btn").addEventListener("click", compare);
  document.getElementById("copy-btn").addEventListener("click", (e) => Anvil.copyText(output.innerText, e.target));
})();

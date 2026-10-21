(function () {
  "use strict";
  const input = document.getElementById("input");
  const output = document.getElementById("output");
  const status = document.getElementById("status");

  function locate(text, pos) {
    const upTo = text.slice(0, pos);
    const line = upTo.split("\n").length;
    const column = pos - upTo.lastIndexOf("\n");
    return { line, column };
  }

  function validate() {
    const text = input.value;
    if (!text.trim()) {
      Anvil.setStatus(status, "Enter some JSON first.", "neutral");
      output.innerHTML = "";
      return;
    }
    try {
      JSON.parse(text);
      output.textContent = "";
      const ok = document.createElement("span"); ok.style.color = "var(--good)"; ok.textContent = "✓ Valid JSON";
      output.appendChild(ok);
      Anvil.setStatus(status, "Valid JSON.", "ok");
    } catch (err) {
      const match = /position (\d+)/.exec(err.message);
      let detail = err.message;
      if (match) {
        const { line, column } = locate(text, Number(match[1]));
        detail = err.message.replace(/position \d+/, `line ${line}, column ${column}`);
      }
      output.textContent = "";
      const bad = document.createElement("span"); bad.style.color = "var(--bad)"; bad.textContent = "✗ Invalid JSON";
      output.appendChild(bad);
      output.appendChild(document.createTextNode("\n\n" + detail));
      Anvil.setStatus(status, "Invalid JSON — see details.", "error");
    }
  }

  document.getElementById("validate-btn").addEventListener("click", validate);
  document.getElementById("clear-btn").addEventListener("click", () => {
    input.value = ""; output.innerHTML = "";
    Anvil.setStatus(status, "Cleared.", "neutral");
  });
})();

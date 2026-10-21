// Shared runner for tools that need server-side processing (XML, YAML, CSV,
// SQL). Kept generic so each tool page only needs a few lines of config.
window.AnvilApi = {
  // Per-endpoint request counter — when a new request for the same endpoint
  // is fired while an older one is still in flight, the older response is
  // discarded. Prevents stale-output races when the user clicks Format
  // twice quickly with different inputs.
  _reqIds: {},

  async run({ endpoint, body, output, status, meta, successMessage }) {
    Anvil.setStatus(status, "Processing…", "neutral");
    const reqId = (this._reqIds[endpoint] = (this._reqIds[endpoint] || 0) + 1);
    // Hard timeout so a hung server can't leave the UI stuck on "Processing…".
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      // Read body as text first so we can distinguish JSON-error, HTML-error,
      // and 413 Payload Too Large (which Flask returns as an HTML page).
      const text = await res.text();
      let data = null;
      try { data = JSON.parse(text); } catch (_) { /* not JSON */ }
      if (!res.ok) {
        // Older in-flight request — discard.
        if (reqId !== this._reqIds[endpoint]) return null;
        const msg = (data && data.error) || (text && text.slice(0, 200)) || res.statusText || `HTTP ${res.status}`;
        Anvil.setStatus(status, msg, "error");
        output.textContent = "";
        if (meta) meta.textContent = "";
        return null;
      }
      // Even on 200, validate the expected envelope.
      if (!data || data.ok !== true) {
        if (reqId !== this._reqIds[endpoint]) return null;
        const msg = (data && data.error) || "Malformed response from the server.";
        Anvil.setStatus(status, msg, "error");
        output.textContent = "";
        if (meta) meta.textContent = "";
        return null;
      }
      if (reqId !== this._reqIds[endpoint]) return null; // stale
      output.textContent = data.result;
      if (meta) meta.textContent = Anvil.formatBytes(new Blob([data.result]).size) + " · " + data.result.split("\n").length + " lines";
      Anvil.setStatus(status, successMessage || "Done.", "ok");
      return data;
    } catch (err) {
      if (reqId !== this._reqIds[endpoint]) return null; // stale
      const msg = (err && err.name === "AbortError")
        ? "The request took too long and was cancelled."
        : "Network error — could not reach the server.";
      Anvil.setStatus(status, msg, "error");
      return null;
    } finally {
      clearTimeout(timeout);
    }
  },
};

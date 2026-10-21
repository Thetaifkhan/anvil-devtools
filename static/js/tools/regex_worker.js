/* Isolated regex execution: the UI can terminate this worker when a pattern
 * exhibits catastrophic backtracking, keeping the main browser thread usable. */
self.addEventListener("message", (event) => {
  const { pattern, flags, text, maxMatches = 5000 } = event.data || {};
  try {
    const re = new RegExp(pattern, flags);
    const matches = [];
    if (re.global) {
      let match;
      let capped = false;
      while ((match = re.exec(text)) !== null) {
        matches.push({
          text: match[0],
          index: match.index,
          captures: Array.from(match).slice(1),
          groups: match.groups || null,
        });
        if (match[0] === "") {
          // Advance by one Unicode code point when /u is enabled.
          const codePoint = text.codePointAt(re.lastIndex);
          re.lastIndex += re.unicode && codePoint > 0xFFFF ? 2 : 1;
        }
        if (matches.length >= maxMatches) {
          // Distinguish exactly maxMatches results from a genuinely truncated set.
          capped = re.exec(text) !== null;
          break;
        }
      }
      self.postMessage({ ok: true, matches, capped });
      return;
    }

    const match = re.exec(text);
    if (match) {
      matches.push({
        text: match[0],
        index: match.index,
        captures: Array.from(match).slice(1),
        groups: match.groups || null,
      });
    }
    self.postMessage({ ok: true, matches, capped: false });
  } catch (error) {
    self.postMessage({ ok: false, error: error && error.message ? error.message : "Invalid regular expression." });
  }
});

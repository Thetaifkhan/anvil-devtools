// Subtle "developer terminal" atmosphere behind the app content. Purely
// decorative, static demo strings only — never renders real user input.
// Respects prefers-reduced-motion and pauses when the tab is hidden.
(function () {
  "use strict";

  const canvas = document.getElementById("terminal-canvas");
  if (!canvas) return;
  // Skip the decorative animation entirely on phones — the CSS hides it
  // there, and not running it saves battery and main-thread time.
  if (window.matchMedia && window.matchMedia("(max-width: 767px)").matches) return;
  const ctx = canvas.getContext("2d");

  const LINES = [
    "> anvil.boot()",
    "[OK] loading tools...",
    "[OK] json parser initialized",
    "[OK] xml service ready",
    "[OK] sql formatter ready",
    "> formatter.run({ format: 'json' })",
    '{ "status": "ready" }',
    "const result = parse(input);",
    "if (result.valid) {",
    "  return format(result);",
    "}",
    "> converter.convert('json', 'xml')",
    "[OK] 42 keys mapped",
    "GET /api/xml-format 200 18ms",
    "POST /api/sql-format 200 9ms",
    "> validator.check(doc)",
    "0xF3A9  0x1C7B  0x00FF",
    "function encode(bytes) {",
    "  return btoa(bytes);",
    "}",
    "> hash.sha256(input)",
    "a3f5...9e21",
    "[build] compiling workbench.js",
    "[build] 0 errors, 0 warnings",
    "diff: +3 -1 ~2",
    "09:41:02 request received",
    "09:41:02 response sent · 12ms",
    "export function run(op) {",
    "  switch (op) { ... }",
    "}",
    "> uuid.v4()",
    "550e8400-e29b-41d4-a716",
    "$ anvil --format json --pretty",
    "[queue] 0 pending jobs",
  ];

  const THEME_COLORS = {
    light:       ["#7C4FD1", "#0E7C8C", "#8B8371", "#C23B8C"],
    dracula:     ["#BD93F9", "#8BE9FD", "#50FA7B", "#FF79C6", "#FFB86C"],
    "night-owl": ["#82AAFF", "#7FDBCA", "#C792EA", "#ADDB67"],
  };

  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let width, height, drops, colors, fontSize = 14;
  let raf = null;
  let visible = true;

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "dracula";
  }

  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
    const columns = Math.max(Math.floor(width / (fontSize * 12)), 4);
    drops = new Array(columns).fill(0).map(() => ({
      line: LINES[Math.floor(Math.random() * LINES.length)],
      y: Math.random() * -height,
      speed: 0.15 + Math.random() * 0.25,
      x: 0,
    }));
    drops.forEach((d, i) => (d.x = (i / drops.length) * width + Math.random() * 20));
    colors = THEME_COLORS[currentTheme()] || THEME_COLORS.dracula;
  }

  function drawStatic() {
    ctx.clearRect(0, 0, width, height);
    ctx.font = fontSize + "px 'JetBrains Mono', monospace";
    ctx.globalAlpha = 0.05;
    drops.forEach((d, i) => {
      ctx.fillStyle = colors[i % colors.length];
      ctx.fillText(d.line, d.x, (i % 12) * (height / 12) + 20);
    });
    ctx.globalAlpha = 1;
  }

  function frame() {
    if (!visible) { raf = null; return; }
    ctx.clearRect(0, 0, width, height);
    ctx.font = fontSize + "px 'JetBrains Mono', monospace";
    drops.forEach((d, i) => {
      d.y += d.speed;
      if (d.y > height + 20) {
        d.y = -20;
        d.line = LINES[Math.floor(Math.random() * LINES.length)];
      }
      ctx.globalAlpha = 0.06;
      ctx.fillStyle = colors[i % colors.length];
      ctx.fillText(d.line, d.x, d.y);
    });
    ctx.globalAlpha = 1;
    raf = requestAnimationFrame(frame);
  }

  function start() {
    resize();
    if (reduceMotion) {
      drawStatic();
      return;
    }
    if (!raf) raf = requestAnimationFrame(frame);
  }

  window.addEventListener("resize", () => { resize(); if (reduceMotion) drawStatic(); });
  window.addEventListener("anvil-theme-change", () => { colors = THEME_COLORS[currentTheme()] || THEME_COLORS.dracula; if (reduceMotion) drawStatic(); });
  document.querySelectorAll(".theme-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => { colors = THEME_COLORS[btn.dataset.theme] || colors; if (reduceMotion) drawStatic(); });
  });

  document.addEventListener("visibilitychange", () => {
    visible = !document.hidden;
    if (visible && !reduceMotion && !raf) raf = requestAnimationFrame(frame);
  });

  document.addEventListener("DOMContentLoaded", start);
})();

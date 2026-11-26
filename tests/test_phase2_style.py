"""Phase 2 §2.5 animation and style options — tests.

Static checks: the high-contrast theme tokens actually meet WCAG AA
(contrast computed from the real CSS values), the reduce-motion override
exists for BOTH the media query and the header toggle, the four required
animations exist, choices live in localStorage only, and the pre-paint
script applies theme + motion before rendering.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
MAIN_CSS = (ROOT / "static/css/main.css").read_text(encoding="utf-8")
MAIN_JS = (ROOT / "static/js/main.js").read_text(encoding="utf-8")
BASE_HTML = (ROOT / "templates/base.html").read_text(encoding="utf-8")


# ── WCAG contrast helpers ──────────────────────────────────────────────────

def _rel_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg: str, bg: str) -> float:
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _theme_tokens(theme: str) -> dict:
    m = re.search(
        rf'\[data-theme="{re.escape(theme)}"\] \{{(.*?)\}}', MAIN_CSS, re.S)
    assert m, f"theme {theme} not defined in main.css"
    tokens = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"\s*--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", line)
        if mm:
            tokens[mm.group(1)] = mm.group(2)
    return tokens


# ── High-contrast theme meets WCAG AA ──────────────────────────────────────

def test_high_contrast_theme_exists():
    tokens = _theme_tokens("high-contrast")
    assert tokens, "high-contrast theme must define tokens"


def test_high_contrast_body_text_meets_aa():
    tokens = _theme_tokens("high-contrast")
    body = _contrast(tokens["text"], tokens["bg"])
    dim = _contrast(tokens["text-dim"], tokens["bg"])
    faint = _contrast(tokens["text-faint"], tokens["bg"])
    assert body >= 4.5, f"body text contrast {body:.2f} < 4.5"
    assert dim >= 4.5, f"secondary text contrast {dim:.2f} < 4.5"
    assert faint >= 4.5, f"faint text contrast {faint:.2f} < 4.5"


def test_high_contrast_links_and_primary_meet_aa():
    tokens = _theme_tokens("high-contrast")
    link = _contrast(tokens["link"], tokens["bg"])
    primary = _contrast(tokens["primary"], tokens["bg"])
    good = _contrast(tokens["good"], tokens["bg"])
    assert link >= 4.5, f"link contrast {link:.2f} < 4.5"
    assert primary >= 4.5, f"primary contrast {primary:.2f} < 4.5"
    assert good >= 4.5, f"success colour contrast {good:.2f} < 4.5"


def test_existing_themes_still_defined():
    for theme in ("light", "dracula", "night-owl"):
        assert _theme_tokens(theme), f"{theme} theme lost its tokens"


# ── Reduce motion ──────────────────────────────────────────────────────────

def test_reduce_motion_respects_media_query_and_toggle():
    # Media query version (OS setting).
    assert "prefers-reduced-motion: reduce" in MAIN_CSS
    # Toggle version (html[data-motion="reduced"]).
    assert 'html[data-motion="reduced"]' in MAIN_CSS
    assert "animation: none !important" in MAIN_CSS


def test_motion_toggle_in_header_and_mobile():
    assert BASE_HTML.count('id="motion-toggle"') >= 2, (
        "motion toggle must appear in the desktop header and mobile nav")


def test_motion_choice_stored_in_localstorage_only():
    assert "anvil-reduced-motion" in MAIN_JS
    assert "localStorage" in MAIN_JS
    # No server storage: the motion choice is never sent anywhere.
    assert not re.search(r"fetch\([^)]*anvil-reduced-motion", MAIN_JS)
    assert not re.search(r"fetch\([^)]*anvil-theme", MAIN_JS)


def test_high_contrast_theme_button_present():
    assert BASE_HTML.count('data-theme="high-contrast"') >= 2


# ── The four required animations (§2.5) ────────────────────────────────────

def test_required_animations_exist():
    # 1. Subtle fade-in for cards.
    assert "anvil-card-in" in MAIN_CSS and ".card" in MAIN_CSS
    # 2. Pulse on the copy control after copying.
    assert "anvil-copy-pulse" in MAIN_CSS and ".copied" in MAIN_CSS
    # 3. Smooth height change for the output box.
    assert "anvil-output-expand" in MAIN_CSS and "just-filled" in MAIN_CSS
    # 4. Loading shimmer while a tool runs.
    assert "anvil-shimmer" in MAIN_CSS and "is-running" in MAIN_CSS


def test_animations_use_only_gpu_friendly_properties():
    """60 fps: animate opacity/transform/background-position only - never
    layout properties (width, bare height, top/left/margin/padding).
    max-height is allowed for the output box: the spec explicitly asks for
    a smooth height change there, and the box is bounded (24rem/65vh)."""
    for name in ("anvil-card-in", "anvil-copy-pulse", "anvil-output-expand",
                 "anvil-shimmer"):
        m = re.search(rf"@keyframes {name} \{{(.*?)\}}", MAIN_CSS, re.S)
        assert m, f"{name} keyframes missing"
        body = m.group(1)
        for forbidden in (r"(?<![-\w])width:", r"(?<![-\w])height:",
                          r"(?<![-\w])top:", r"(?<![-\w])left:",
                          r"(?<![-\w])margin:", r"(?<![-\w])padding:"):
            assert not re.search(forbidden, body), (
                f"{name} animates {forbidden} (layout property) - not 60fps-safe")


def test_shimmer_hooks_into_tool_run():
    assert "is-running" in MAIN_JS, "setStatus must toggle the shimmer class"
    assert "Processing" in (ROOT / "static/js/tools/api_tool.js").read_text(
        encoding="utf-8")


def test_pre_paint_script_applies_theme_and_motion():
    head = BASE_HTML.split("</head>")[0]
    assert "anvil-theme" in head and "anvil-reduced-motion" in head
    assert "prefers-reduced-motion" in head, (
        "the OS motion preference must be honoured pre-paint")
    # The script must be inline (no external fetch) to run before paint.
    assert "matchMedia" in head


def test_output_growth_watcher_uses_rAF():
    """Restarting the expansion animation via requestAnimationFrame keeps
    it off the layout-blocking path."""
    assert "requestAnimationFrame" in MAIN_JS
    assert "MutationObserver" in MAIN_JS

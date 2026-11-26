"""Regression tests for tool input persistence (static checks, no browser)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_base_template_loads_tool_drafts_before_page_scripts():
    base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    drafts = base.index("js/tool-drafts.js")
    page_scripts = base.index("{% block scripts %}")
    assert drafts < page_scripts


def test_jwt_input_is_never_persisted():
    tpl = (ROOT / "templates" / "tools" / "jwt_decoder.html").read_text(encoding="utf-8")
    assert 'id="input" data-no-draft' in tpl


def test_draft_script_skips_workbench_admin_and_secrets():
    js = (ROOT / "static" / "js" / "tool-drafts.js").read_text(encoding="utf-8")
    assert '"/workbench"' in js and '"/admin"' in js
    assert 'data-no-draft' in js
    assert 'el.id.indexOf("wb-") === 0' in js


def test_draft_script_uses_session_storage_only():
    js = (ROOT / "static" / "js" / "tool-drafts.js").read_text(encoding="utf-8")
    assert "localStorage" not in js
    assert "sessionStorage" in js

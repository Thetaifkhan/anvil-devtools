import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "static" / "css" / "main.css").read_text(encoding="utf-8")


def test_page_content_does_not_zero_horizontal_gutters():
    # A `padding: X 0` shorthand on .page-content overrides .wrap's side padding,
    # removing the left/right margin on every page that uses it.
    rule = re.search(r"\.page-content\s*\{([^}]*)\}", CSS)
    assert rule, ".page-content rule missing"
    assert not re.search(r"(?<![-\w])padding\s*:", rule.group(1)), (
        "use padding-top/padding-bottom so .wrap side padding is kept"
    )

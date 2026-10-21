import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import create_app
from tool_registry import TOOLS, capability_index, tools_for_operation, OPERATIONS


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        yield client


def test_workbench_loads(client):
    resp = client.get("/workbench")
    assert resp.status_code == 200
    assert b"Workbench" in resp.data


def test_workbench_accepts_deep_link_params(client):
    resp = client.get("/workbench?operation=converter&format=json-xml")
    assert resp.status_code == 200


def test_old_tool_routes_still_work(client):
    # Every previously-existing route must keep working — Workbench is
    # additive, not a replacement.
    for slug in ["json-formatter", "json-validator", "xml-formatter", "base64-encode", "jwt-decoder"]:
        resp = client.get("/" + slug)
        assert resp.status_code == 200, slug


def test_every_tool_has_capability_metadata():
    for tool in TOOLS:
        assert "operation" in tool
        assert tool["operation"] in OPERATIONS or tool["operation"] == "tool"
        assert isinstance(tool["formats"], list)
        assert isinstance(tool["actions"], list)


def test_capability_index_shape():
    idx = capability_index()
    assert len(idx) == len(TOOLS)
    for entry in idx:
        assert {"id", "slug", "name", "operation", "formats", "actions", "category", "short"} <= set(entry.keys())


def test_tools_for_operation_formatter_includes_json():
    slugs = [t["slug"] for t in tools_for_operation("formatter")]
    assert "json-formatter" in slugs
    assert "formatter" in slugs

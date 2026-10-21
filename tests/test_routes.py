import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        yield client


def test_homepage_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Anvil" in resp.data


def test_known_tool_page_loads(client):
    resp = client.get("/json-formatter")
    assert resp.status_code == 200
    assert b"JSON Formatter" in resp.data


def test_unknown_tool_returns_404(client):
    resp = client.get("/not-a-real-tool")
    assert resp.status_code == 404


def test_robots_txt(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert b"Sitemap" in resp.data


def test_sitemap_xml(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert b"<urlset" in resp.data


def test_static_pages_load(client):
    for path in ["/privacy-policy", "/terms-of-service", "/disclaimer", "/contact", "/tools"]:
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_api_json_to_xml(client):
    resp = client.post("/api/json-to-xml", json={"text": '{"name": "John"}', "root": "root"})
    data = resp.get_json()
    assert data["ok"] is True
    assert "<name>John</name>" in data["result"]


def test_api_json_to_xml_invalid(client):
    resp = client.post("/api/json-to-xml", json={"text": "{bad}"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_xml_validate(client):
    resp = client.post("/api/xml-validate", json={"text": "<a><b>1</b></a>"})
    data = resp.get_json()
    assert data["valid"] is True


def test_api_sql_format(client):
    resp = client.post("/api/sql-format", json={"text": "select 1", "action": "format"})
    data = resp.get_json()
    assert data["ok"] is True
    assert "SELECT" in data["result"]


def test_tool_pages_disclose_server_processing(client):
    response = client.get("/json-to-xml")
    assert response.status_code == 200
    assert b"Input is sent to Anvil&#39;s server" in response.data or b"Input is sent to Anvil's server" in response.data


def test_client_side_tool_page_discloses_local_processing(client):
    response = client.get("/json-formatter")
    assert response.status_code == 200
    assert b"nothing you type is sent to Anvil&#39;s server" in response.data or b"nothing you type is sent to Anvil's server" in response.data


def test_homepage_does_not_claim_zero_data_uploads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"data uploaded*" not in response.data

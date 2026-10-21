from app import create_app


def test_healthz_reports_ok_without_config_details():
    client = create_app({"TESTING": True}).test_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}

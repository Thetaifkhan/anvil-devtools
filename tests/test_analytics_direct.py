"""Analytics are written straight to the database when healthy, else queued."""
from unittest import mock

import pytest

import routes.analytics as analytics


def test_direct_write_when_database_healthy(monkeypatch):
    submitted = []
    monkeypatch.setattr(analytics, "_db_available", lambda: True)
    monkeypatch.setattr(analytics._DIRECT_POOL, "submit",
                        lambda fn, *a: submitted.append((fn, a)))
    with mock.patch.object(analytics, "enqueue") as q:
        analytics._deliver_analytics({"event_name": "x"}, event_key="k1")
    assert submitted and submitted[0][1] == ({"event_name": "x"}, "k1")
    q.assert_not_called()


def test_queues_when_database_unavailable(monkeypatch):
    monkeypatch.setattr(analytics, "_db_available", lambda: False)
    with mock.patch.object(analytics, "enqueue") as q:
        analytics._deliver_analytics({"event_name": "x"}, event_key="k2")
    q.assert_called_once_with("analytics_event", {"event_name": "x"}, event_key="k2")


def test_failed_direct_write_falls_back_to_queue(monkeypatch):
    monkeypatch.setattr(analytics, "_handle_analytics_event",
                        mock.Mock(side_effect=RuntimeError("db down")))
    with mock.patch.object(analytics, "enqueue") as q:
        analytics._direct_write({"event_name": "y"}, "k3")
    q.assert_called_once_with("analytics_event", {"event_name": "y"}, event_key="k3")

"""Only connectivity errors may mark the database as down."""
from contextlib import contextmanager

import psycopg
import pytest

import supabase_client as sc


@pytest.fixture(autouse=True)
def fresh_health():
    sc._record_success()
    yield
    sc._record_success()


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    @contextmanager
    def cursor(self):
        yield _FakeCursor()


class _FakePool:
    @contextmanager
    def connection(self):
        yield _FakeConn()


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(sc, "_get_pool", lambda: _FakePool())
    monkeypatch.setattr(sc, "_skip_db_calls", lambda: False)


def test_foreign_key_error_does_not_mark_down(fake_pool):
    for _ in range(5):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with sc.cursor():
                raise psycopg.errors.ForeignKeyViolation("missing session")
    assert sc._HEALTH["state"] == "healthy"


def test_connection_error_does_mark_down(fake_pool):
    for _ in range(3):
        with pytest.raises(psycopg.OperationalError):
            with sc.cursor():
                raise psycopg.OperationalError("connection refused")
    assert sc._HEALTH["state"] == "down"

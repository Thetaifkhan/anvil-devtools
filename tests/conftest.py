"""Shared test environment.

Pins the environment so the suite is hermetic: no ambient DATABASE_URL or
secrets leak in from the host, the delivery worker never starts, and the
random SECRET_KEY fallback stays available (tests are not production).
Individual tests may still monkeypatch these values.
"""
import os

os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("SECRET_KEY", "test-only-signing-key-0123456789abcdef")
os.environ["SUPABASE_WORKER_ENABLED"] = "0"

# The app under test must never see the host machine's databases or keys.
for _k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
           "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
           "SUPABASE_SSL_ROOT_CERT", "OUTBOX_DURABLE"):
    os.environ.pop(_k, None)

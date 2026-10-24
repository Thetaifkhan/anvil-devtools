import os
import secrets


class Config:
    """Central configuration, driven by environment variables.

    Nothing sensitive lives here by default because this app does not
    require a database or secret keys for its core tools. Values are
    still routed through env vars so deployments can override them.
    """

    ENV = os.environ.get("FLASK_ENV", "production")
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Flask session cookies (used by the admin dashboard auth). The cookie
    # is signed by SECRET_KEY, never encrypted, so the secret must be
    # high-entropy. SESSION_COOKIE_SAMESITE defaults to lax which protects
    # against most CSRF vectors; we set the rest explicitly below.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Render terminates TLS at the platform; Flask sees HTTP. We use the
    # SITE_URL check to decide HSTS in app.py and rely on the platform
    # for the actual cookie transport. SESSION_COOKIE_SECURE is set at
    # runtime in app.py based on the same check.

    # Never accept a missing, known-placeholder, or short signing key from the
    # environment. The random fallback is safe for local use but changes on each
    # process start, so production deployments must configure a stable secret.
    _RAW_SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
    _SECRET_KEY_PLACEHOLDERS = {
        "change-me-in-production", "replace-with-a-random-secret", "changeme", "secret"
    }
    SECRET_KEY_CONFIGURED = (
        len(_RAW_SECRET_KEY) >= 32
        and _RAW_SECRET_KEY.lower() not in _SECRET_KEY_PLACEHOLDERS
    )
    SECRET_KEY = _RAW_SECRET_KEY if SECRET_KEY_CONFIGURED else secrets.token_urlsafe(32)

    # Server-side processing limits. These exist purely to protect the
    # server from abuse/huge payloads -- nothing is ever stored.
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_BYTES", 5 * 1024 * 1024))  # 5 MB
    MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", 2_000_000))

    SITE_NAME = os.environ.get("SITE_NAME", "Anvil")
    SITE_URL = os.environ.get("SITE_URL", "http://localhost:5000").rstrip("/")
    SITE_TAGLINE = "Developer tools, forged for daily use."

    # Analytics is opt-in and configured entirely through env vars.
    # If unset, no analytics script is rendered at all.
    ANALYTICS_PROVIDER = os.environ.get("ANALYTICS_PROVIDER", "")  # "ga4" | "plausible" | ""
    ANALYTICS_ID = os.environ.get("ANALYTICS_ID", "")

    # AdSense is disabled unless a publisher id is supplied. The template
    # renders empty placeholder containers either way so layout never shifts.
    ADSENSE_CLIENT_ID = os.environ.get("ADSENSE_CLIENT_ID", "")
    ADSENSE_ENABLED = bool(ADSENSE_CLIENT_ID)

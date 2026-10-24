import os

from flask import Flask, render_template, request, send_from_directory, Response, jsonify

from config import Config
from xml.sax.saxutils import escape as xml_escape
from tool_registry import TOOLS, workbench_supports
from routes.pages import pages_bp
from routes.convert_tools import convert_bp
from routes.contact import contact_bp
from routes.analytics import analytics_bp
from routes.admin import admin_bp


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    # Session cookies carry the admin session. Mark them Secure whenever the
    # canonical site is HTTPS so they are never sent over plaintext HTTP.
    # The explicit SITE_URL is used rather than request.is_secure, because a
    # TLS-terminating proxy hides the scheme from WSGI.
    if str(app.config.get("SITE_URL", "")).lower().startswith("https://"):
        app.config["SESSION_COOKIE_SECURE"] = True

    has_explicit_secret = bool(test_config and test_config.get("SECRET_KEY")) or bool(
        app.config.get("SECRET_KEY_CONFIGURED")
    )
    if not has_explicit_secret and app.config.get("ENV") == "production":
        app.logger.warning(
            "A valid SECRET_KEY is not configured. A temporary process key is in use; "
            "set SECRET_KEY to a stable, high-entropy value (at least 32 characters) for production."
        )

    @app.after_request
    def add_security_headers(response):
        """Set baseline browser protections on HTML, API, and static responses."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        # Inline scripts remain allowed for the current no-build-step template
        # architecture. Dynamic code evaluation is deliberately not allowed.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "script-src 'self' 'unsafe-inline' https://plausible.io "
            "https://www.googletagmanager.com https://pagead2.googlesyndication.com "
            "https://googleads.g.doubleclick.net https://tpc.googlesyndication.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self' https://plausible.io https://www.google-analytics.com "
            "https://analytics.google.com https://stats.g.doubleclick.net; "
            "frame-src 'self' https://googleads.g.doubleclick.net "
            "https://tpc.googlesyndication.com https://www.google.com; "
            "worker-src 'self' blob:"
        )
        if request.path.startswith("/api/"):
            # Conversion output can contain transformed user input. Never let
            # a shared/browser cache retain API results.
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        # A TLS-terminating reverse proxy may leave WSGI's request.is_secure
        # false. Trust the explicit canonical HTTPS deployment setting rather
        # than an untrusted X-Forwarded-Proto header.
        configured_https = (
            app.config.get("ENV") == "production"
            and str(app.config.get("SITE_URL", "")).lower().startswith("https://")
        )
        if request.is_secure or configured_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    app.register_blueprint(pages_bp)
    app.register_blueprint(convert_bp)
    app.register_blueprint(contact_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(admin_bp)

    # Configure the durable outbox ONCE per process boot. Tests that
    # override the path do so BEFORE calling create_app() so this call
    # is a no-op for them (the path is already set). For production this
    # is the only call that matters — every request inherits the
    # already-configured path.
    from delivery.outbox import configure as configure_outbox
    from delivery import outbox as _outbox_mod
    if _outbox_mod.get_db_path_safe() is None:
        configure_outbox()

    # Start the queue worker thread ONCE per process boot. We must NOT
    # call this from a per-request hook — that would attempt to start
    # the thread on every request (even though start_worker() is
    # idempotent, the call itself is wasteful and would also race with
    # gunicorn's multi-process model).
    #
    # ``start_worker()`` is a no-op when ``SUPABASE_WORKER_ENABLED=0``.
    # The recommended production topology is to set that env var to 0
    # on the web service and run a separate Background Worker via
    # ``python -m delivery.worker_cli``. See RENDER_DEPLOYMENT.md.
    #
    # Skip in TESTING mode so unit tests don't accumulate worker threads.
    # Tests that need the worker set SUPABASE_WORKER_ENABLED=1 and call
    # start_worker() explicitly.
    is_testing = bool(test_config and test_config.get("TESTING"))
    if not is_testing:
        from delivery.worker import start_worker
        start_worker()

    @app.context_processor
    def inject_globals():
        from datetime import datetime, timezone
        return {"current_year": datetime.now(timezone.utc).year, "workbench_supports": workbench_supports}

    @app.context_processor
    def inject_csrf():
        from auth import admin as _auth_admin
        return {"csrf_token": _auth_admin.csrf_token}

    @app.context_processor
    def inject_tool_index():
        import json as _json
        index = [
            {
                "slug": t["slug"],
                "name": t["name"],
                "short": t["short"],
                "keywords": t["keywords"],
                "category": t["category"],
                "operation": t.get("operation", "tool"),
                "formats": t.get("formats", []),
            }
            for t in TOOLS
        ]
        serialized = _json.dumps(index, ensure_ascii=False)
        # JSON embedded in a script element must not contain a literal </script>.
        serialized = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        return {"tool_index_json": serialized}

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Not found."}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "An unexpected error occurred."}), 500
        return render_template("errors/500.html"), 500

    @app.errorhandler(413)
    def too_large(e):
        if request.path.startswith("/api/"):
            return {"ok": False, "error": "Request body is too large."}, 413
        return render_template("errors/413.html"), 413

    @app.get("/healthz")
    def healthz():
        """Liveness probe for container orchestrators.

        Reveals only whether the app is alive — never config details or DB
        connectivity (use /admin/queue for that, behind auth)."""
        return {"ok": True}

    @app.get("/api/admin/healthz")
    def admin_healthz():
        """Admin-facing readiness probe — requires admin auth.

        Returns connectivity + queue status so an operator can decide
        whether the deployment is ready. Lives under /api/admin/* so it's
        covered by the same auth decorator as the dashboard.
        """
        from auth import admin as auth_admin
        if not auth_admin.current_admin_user_id():
            return jsonify({"ok": False, "error": "auth required"}), 401
        from delivery.worker import status_snapshot
        return jsonify({"ok": True, "queue": status_snapshot()})

    @app.get("/robots.txt")
    def robots():
        site_url = app.config["SITE_URL"].rstrip("/")
        content = (
            "User-agent: *\n"
            "Allow: /\n"
            f"Sitemap: {site_url}/sitemap.xml\n"
        )
        return Response(content, mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap():
        site_url = app.config["SITE_URL"].rstrip("/")
        static_paths = ["", "tools", "privacy-policy", "terms-of-service", "disclaimer", "contact"]
        urls = [f"{site_url}/{p}" if p else site_url for p in static_paths]
        urls += [f"{site_url}/{t['slug']}" for t in TOOLS]
        xml_items = "\n".join(f"  <url><loc>{xml_escape(u)}</loc></url>" for u in urls)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{xml_items}\n"
            "</urlset>"
        )
        return Response(xml, mimetype="application/xml")

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(os.path.join(app.root_path, "static"), "manifest.webmanifest")

    @app.get("/service-worker.js")
    def service_worker():
        # Served from root so its scope covers the whole site.
        return send_from_directory(os.path.join(app.root_path, "static", "js"), "service-worker.js", mimetype="application/javascript")

    return app


# The module-level ``app = create_app()`` line is required so that
# gunicorn can target ``app:app``. We DO want the worker to start in
# production (where SUPABASE_WORKER_ENABLED is set), but we don't want
# it to start in tests. Tests typically import create_app rather than
# the module-level ``app`` object, so we gate the worker-thread
# startup on the env var here too — start_worker() is a no-op when
# SUPABASE_WORKER_ENABLED=0 (the default in tests and in the
# recommended production topology where a separate Background Worker
# runs the delivery loop).
app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=app.config["DEBUG"])

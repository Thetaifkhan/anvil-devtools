# Anvil — Developer Tools

A fast, privacy-conscious collection of developer utilities: JSON, XML, YAML and
CSV formatting and conversion, a JWT decoder, hash and UUID generators, a regex
tester, diff checkers, and more.

Most tools run **entirely in the browser**. A few tools (XML, YAML, CSV and SQL
conversion or formatting) send their input to the application server, which
processes it in memory and does not intentionally store or log it. Avoid pasting
secrets into server-processed tools. Each tool page states its processing mode.

## Quick start

Requires Python 3.12 or newer.

```bash
git clone https://github.com/<your-account>/anvil-devtools.git
cd anvil-devtools
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                      # http://localhost:5000
```

Runtime dependencies are in `requirements.txt`. Test tools are in
`requirements-dev.txt`. Local runs need no configuration; a temporary signing
key is generated at startup.

## Run with Docker

```bash
docker build -t anvil .
docker run -p 8080:8080 anvil     # http://localhost:8080
```

The image runs as a non-root user and includes a health check at `/healthz`.
To pass settings, use `--env-file` (see [Configuration](#configuration)).

## Publish on GitHub

1. Create an empty repository on GitHub (no README or license, so the first push has no conflicts).
2. Push this project:

   ```bash
   git remote add origin https://github.com/<your-account>/anvil-devtools.git
   git push -u origin main
   ```

3. GitHub Actions then runs:
   - **CI** (`.github/workflows/ci.yml`) on every push and pull request: compiles the code, runs the Python and JavaScript tests, and audits dependencies with `pip-audit`.
   - **Docker image** (`.github/workflows/docker.yml`) after CI passes on `main` or a `v*.*.*` tag. It publishes the image to `ghcr.io/<your-account>/anvil-devtools`.

To pull and run the published image:

```bash
docker run -p 8080:8080 -e SECRET_KEY="$SECRET_KEY" -e SITE_URL=https://your-domain.example \
  ghcr.io/<your-account>/anvil-devtools:main
```

The first publish may need the package set to public under the repository's
**Packages** settings.

GitHub Pages cannot run this Python server, so use a container host (below) for
the full app.

## Hosting

Any platform that runs a Docker image or a Python web app works. Set the
configuration below on the platform, and put the app behind HTTPS.

- **Render, Railway, Fly.io, Google Cloud Run, Azure Container Apps:** deploy the Dockerfile or the GHCR image. Set the port to `8080`.
- **A VPS:** run the image with `docker run --restart unless-stopped -p 127.0.0.1:8080:8080 --env-file .env ...` and put nginx or Caddy in front for TLS.
- **Without Docker:** `pip install -r requirements.txt && gunicorn -w 4 -b 0.0.0.0:8080 app:app`.

Terminate TLS at the platform or a reverse proxy. When `SITE_URL` starts with
`https://` in production, Anvil sends HSTS. It does not trust forwarded-protocol
headers from the client.

## Configuration

Copy `.env.example` to set values for Docker (`--env-file .env`) or your
platform's environment settings. Flask does not load `.env` by itself, and the
`.dockerignore` file keeps a local `.env` out of the image.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Stable signing key, at least 32 characters. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Required for multi-instance or restart-safe deployments. |
| `SITE_URL` | Public HTTPS address, used for canonical URLs, the sitemap, and Open Graph tags. |
| `PORT` | Listening port (default `5000` locally, `8080` in Docker). |
| `MAX_CONTENT_LENGTH_BYTES` | Maximum request body size (default 5 MB). |
| `MAX_TEXT_CHARS` | Maximum characters accepted by server-side tools (default 2,000,000). |
| `ANALYTICS_PROVIDER` / `ANALYTICS_ID` | `plausible` or `ga4`. Blank disables analytics. |
| `ADSENSE_CLIENT_ID` | Blank disables ads. Ad containers stay hidden, so the layout does not shift. |

## Project layout

```
anvil-devtools/
├── app.py                 # App factory, security headers, error pages, sitemap, /healthz
├── config.py              # Environment-driven configuration
├── tool_registry.py       # Single source of truth for every tool's metadata
├── routes/
│   ├── pages.py           # Home, tool pages, static pages
│   └── convert_tools.py   # /api/* endpoints for server-side conversions
├── services/
│   ├── conversion_service.py  # JSON, XML, YAML and CSV logic
│   ├── sql_service.py         # SQL formatting
│   └── errors.py              # ToolError (user-facing errors)
├── templates/             # Jinja templates (base, tool pages, static pages, errors)
├── static/
│   ├── css/main.css       # Design system
│   ├── js/                # Shared helpers, per-tool logic, service worker
│   └── icons/, manifest.webmanifest
├── tests/                 # pytest suite, plus Node tests in tests/js
├── Dockerfile
└── .github/workflows/     # ci.yml (checks) and docker.yml (image publish)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v          # 235 tests
node --test tests/js/*.test.cjs     # 4 worker tests
```

For end-to-end smoke and headless-Chromium render checks (requires
`pip install playwright && playwright install chromium`):

```bash
python scripts/smoke_e2e.py        # 161 live-server + API checks
python scripts/render_check.py     # 292 render + responsive checks
python scripts/functional_check.py  # 25 functional-output checks
python scripts/verify_fixes.py      # 32 bug-fix regression checks
```

The scripts assume gunicorn is available in the project's `.venv`.

## Adding a tool

1. Add an entry to `TOOLS` in `tool_registry.py` (slug, name, category, short description, keywords).
2. Create `templates/tools/<slug>.html`, extending `tools/_tool_base.html`.
3. For server-side logic, add a function in `services/` and a route in `routes/convert_tools.py`. Otherwise add browser code in `static/js/tools/<slug>.js`.

The homepage, search, sitemap, and navigation pick up the new tool from the registry.

## Security notes

- XML parsing uses `lxml` with entity resolution, DTD loading, network access, and huge-tree mode disabled. Documents with a `DOCTYPE` are rejected.
- YAML is parsed with `yaml.safe_load`.
- Request bodies are limited by `MAX_CONTENT_LENGTH_BYTES`, and text by `MAX_TEXT_CHARS`.
- The application does not intentionally log or store tool input. Hosting platform logs are outside the app and should be configured separately.
- Responses include a Content Security Policy, frame restrictions, MIME-sniffing protection, a strict referrer policy, and a permissions policy.
- Password and UUID generation use `crypto` APIs, not `Math.random()`.
- The JWT decoder shows the decoded header, payload, and raw signature. It never verifies signatures, and the page says so.

See `AUDIT_REPORT.md` for the audit history, verification results, and open risks.

## License

No license file is included yet. Add one (for example MIT) before publishing
publicly, so others know how they may use the code.

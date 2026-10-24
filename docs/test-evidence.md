# Test evidence

Produced by running each command in the project root, with PostgreSQL 16.15
listening on port 55432 (a disposable local cluster). Results come from this
run. "Real PG" means the tests executed SQL against PostgreSQL. "Mocks" means
the test replaced a dependency. Nothing here was run against live Supabase,
Render, or any backup.

## Results

| Test group | Command | Exit | Result | Data source |
|---|---|---|---|---|
| Full application suite | `PG_PORT=55432 python -m pytest tests/ -q` | 0 | 600 passed, 1 skipped | Mixed: real PG for DB tests; mocks for Supabase HTTP and the admin token verifier |
| Analytics upsert | `pytest tests/test_analytics_upsert_pg.py -q` | 0 | 14 passed | Real PG 16 |
| RLS as ordinary roles | `pytest tests/test_rls_access.py -q` | 0 | 12 passed | Real PG 16, role switching |
| Migrations end-to-end | `pytest tests/test_migrations_e2e.py -q` | 0 | 15 passed | Real PG 16, stubbed base schema |
| CSRF on admin writes | `pytest tests/test_csrf.py -q` | 0 | 14 passed | Flask test client; DB mocked |
| Hardening regressions | `pytest tests/test_hardening_regressions.py -q` | 0 | 41 passed | Real PG for TLS and lease checks; forked processes; mocks for network |
| Dependency audit | `python -m pip_audit -r requirements-dev.txt` | 0 | No known vulnerabilities | PyPI advisory data |
| Bytecode compile | `python -m compileall -q <packages>` | 0 | clean | n/a |
| JavaScript unit tests | `node --test tests/js/*.test.cjs` | see below | # pass 22 # fail 0  | Node 22 |
| JavaScript syntax | `node --check` on every `static/js` file | 0 | clean | n/a |

## Skipped and not run

| Test | Reason | Prerequisite |
|---|---|---|
| `tests/test_workbench_browser.py` (1 test) | Playwright not installed | `pip install playwright && playwright install chromium` (needs network to the browser CDN) |

## Not run at all

- Any test against the live Supabase project (no credentials, no access).
- Any Render deployment test.
- Backup and restore rehearsal (no backup exists to restore).
- The GitHub Actions workflow (edited, validated as YAML, never executed).
- Migration 005 against the live catalog.

## Reproduce

```bash
# disposable PostgreSQL (Ubuntu): sudo apt-get install postgresql, then initdb and start on 55432
python -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
PG_PORT=55432 python -m pytest tests/ -q
node --test tests/js/*.test.cjs
python -m pip_audit -r requirements-dev.txt
```

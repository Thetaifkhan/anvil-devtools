"""Verify RENDER_DEPLOYMENT.md and render.yaml match the actual code.

The deployment docs have drifted from the code in the past (env vars
referenced in the doc that no longer exist in the code, default values
that don't match, etc.). These tests catch that drift.
"""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RENDER_DEPLOYMENT_MD = ROOT / "RENDER_DEPLOYMENT.md"
RENDER_YAML = ROOT / "render.yaml"
ENV_EXAMPLE = ROOT / ".env.example"


def _extract_env_vars_from_text(text: str) -> set:
    """Extract env-var names mentioned in a text.

    Matches: ``SOMETHING_VAR``, ``SOMETHING_VAR=value``,
    ``SOMETHING_VAR=``, ``SOMETHING_VAR`` in backticks.
    """
    # Match identifier-like patterns that are UPPERCASE_WITH_UNDERSCORES.
    return set(re.findall(r'\b([A-Z][A-Z0-9_]{3,})\b', text))


def _extract_env_vars_from_python(text: str) -> set:
    """Extract env var names read via os.environ in Python source."""
    vars_found = set()
    # os.environ.get("X")
    for m in re.finditer(r'os\.environ\.get\s*\(\s*["\']([^"\']+)["\']', text):
        vars_found.add(m.group(1))
    # os.environ["X"]
    for m in re.finditer(r'os\.environ\s*\[\s*["\']([^"\']+)["\']\s*\]', text):
        vars_found.add(m.group(1))
    # os.environ.setdefault("X", ...)
    for m in re.finditer(r'os\.environ\.setdefault\s*\(\s*["\']([^"\']+)["\']', text):
        vars_found.add(m.group(1))
    return vars_found


def _all_python_files():
    """Yield (path, contents) for every Python file in the project."""
    exclude_dirs = {".venv", ".git", "node_modules", "__pycache__", ".pytest_cache"}
    for path in ROOT.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        yield path, path.read_text(encoding="utf-8", errors="ignore")


def test_render_yaml_env_vars_all_exist_in_code():
    """Every env var referenced in render.yaml must actually be read
    by the Python code OR documented as an operational env var.
    Catches drift (e.g. a renamed env var in code that the YAML
    still references).

    Operational env vars (read by gunicorn, the backup script, or
    the Docker image) are whitelisted.
    """
    yaml_text = RENDER_YAML.read_text(encoding="utf-8")
    yaml_vars = _extract_env_vars_from_text(yaml_text)
    # Filter to vars that look like Anvil-specific.
    relevant = {v for v in yaml_vars if any(v.startswith(p) for p in (
        "SUPABASE_", "OUTBOX_", "FLASK_", "SECRET_KEY", "SITE_",
        "IP_HMAC", "MAX_", "PORT", "WEB_", "BACKUP_",
        "ANALYTICS_", "ADSENSE_", "LOG_LEVEL",
    ))}
    assert relevant, "No relevant env vars found in render.yaml"

    code_vars = set()
    for _, text in _all_python_files():
        code_vars |= _extract_env_vars_from_python(text)
    code_vars |= _extract_env_vars_from_text(ENV_EXAMPLE.read_text(encoding="utf-8"))

    # Operational vars: read by gunicorn, the backup script, or the
    # Docker image — not by the Python app code directly.
    operational_whitelist = {
        "WEB_CONCURRENCY",  # gunicorn reads this
        "PORT",  # gunicorn / Docker
        "BACKUP_GPG_PASSPHRASE",  # backup-database.sh
        "BACKUP_S3_BUCKET",  # backup-database.sh
        "AWS_ACCESS_KEY_ID",  # awscli
        "AWS_SECRET_ACCESS_KEY",  # awscli
    }

    missing = relevant - code_vars - operational_whitelist
    assert not missing, (
        f"render.yaml references env vars that are NOT read by any Python "
        f"code, not in .env.example, and not in the operational whitelist: "
        f"{sorted(missing)}"
    )


def test_render_deployment_md_env_vars_all_exist_in_code():
    """Every env var referenced in RENDER_DEPLOYMENT.md must actually
    be read by the Python code OR documented in .env.example OR be
    an operational env var (gunicorn/backup-script)."""
    md_text = RENDER_DEPLOYMENT_MD.read_text(encoding="utf-8")
    md_vars = _extract_env_vars_from_text(md_text)
    relevant = {v for v in md_vars if any(v.startswith(p) for p in (
        "SUPABASE_", "OUTBOX_", "FLASK_", "SECRET_KEY", "SITE_",
        "IP_HMAC", "MAX_", "PORT", "WEB_", "BACKUP_",
    ))}

    code_vars = set()
    for _, text in _all_python_files():
        code_vars |= _extract_env_vars_from_python(text)
    code_vars |= _extract_env_vars_from_text(ENV_EXAMPLE.read_text(encoding="utf-8"))

    operational_whitelist = {
        "WEB_CONCURRENCY",
        "PORT",
        "BACKUP_GPG_PASSPHRASE",
        "BACKUP_S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    }

    missing = relevant - code_vars - operational_whitelist
    assert not missing, (
        f"RENDER_DEPLOYMENT.md references env vars that are NOT read by "
        f"any Python code, not in .env.example, and not in the operational "
        f"whitelist: {sorted(missing)}"
    )


def test_env_example_default_for_analytics_max_age_is_zero():
    """The .env.example must default
    SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS to 0 (disabled), matching
    the code default (DEFAULT_ANALYTICS_MAX_AGE_DAYS = 0).

    Looks for an assignment line (not a comment) at the start of a line.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # Match lines like "SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=0" (no #).
    match = re.search(
        r"^\s*SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=(\d+)\s*$",
        text, re.MULTILINE
    )
    assert match, (
        "SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=... assignment not found "
        "in .env.example"
    )
    assert match.group(1) == "0", (
        f"Default should be 0 (disabled, long-term retention preferred); "
        f"got {match.group(1)}"
    )


def test_env_example_default_for_outbox_durable_is_zero():
    """OUTBOX_DURABLE must default to 0 (best-effort). The operator
    opts in to durability only after verifying a persistent disk.

    Looks for an assignment line (not a comment) at the start of a line.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^\s*OUTBOX_DURABLE=(\d+)\s*$", text, re.MULTILINE)
    assert match, "OUTBOX_DURABLE=... assignment not found in .env.example"
    assert match.group(1) == "0", (
        f"OUTBOX_DURABLE must default to 0; got {match.group(1)}"
    )


def test_render_yaml_default_for_analytics_max_age_is_zero():
    """The render.yaml must default SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS
    to 0 (matching the code default)."""
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(
        r"SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS\s*\n\s*value:\s*\"(\d+)\"",
        text
    )
    assert match, "SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS not found in render.yaml"
    assert match.group(1) == "0", (
        f"render.yaml default should be 0; got {match.group(1)}"
    )


def test_render_yaml_default_for_outbox_durable_is_zero():
    """render.yaml must default OUTBOX_DURABLE to 0 (best-effort)."""
    text = RENDER_YAML.read_text(encoding="utf-8")
    match = re.search(r"OUTBOX_DURABLE\s*\n\s*value:\s*\"(\d+)\"", text)
    assert match, "OUTBOX_DURABLE not found in render.yaml"
    assert match.group(1) == "0", (
        f"render.yaml OUTBOX_DURABLE must default to 0; got {match.group(1)}"
    )


def test_render_yaml_does_not_share_sqlite_between_services():
    """The render.yaml must NOT claim that the web service and the
    Background Worker share a SQLite file via SUPABASE_QUEUE_PATH.
    Two separate Render services do NOT share a filesystem."""
    text = RENDER_YAML.read_text(encoding="utf-8")
    # The old, incorrect claim was:
    # "Shares the SQLite outbox file with the web service via SUPABASE_QUEUE_PATH"
    assert "Shares the SQLite outbox file" not in text, (
        "render.yaml still claims the Background Worker shares a SQLite "
        "file with the web service. Two separate Render services do NOT "
        "share a filesystem."
    )
    assert "DO NOT share a filesystem" in text, (
        "render.yaml must explicitly state that two separate Render "
        "services do NOT share a filesystem."
    )


def test_render_deployment_md_documents_persistence_verification():
    """The deployment doc must tell operators to VERIFY the disk is
    actually persistent before setting OUTBOX_DURABLE=1."""
    text = RENDER_DEPLOYMENT_MD.read_text(encoding="utf-8")
    assert "persistence-test" in text, (
        "RENDER_DEPLOYMENT.md must include a verification procedure "
        "(write a file, restart, check it's still there) before "
        "setting OUTBOX_DURABLE=1."
    )

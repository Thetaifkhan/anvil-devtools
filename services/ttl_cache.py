"""Small thread-safe time-based cache for expensive read-only queries.

Used for admin dashboard numbers so repeated page loads do not re-run heavy
database aggregates. Caches data, never rendered pages (those carry CSRF tokens).
"""
from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable


def _testing() -> bool:
    # Caching is off under test so each test sees fresh data.
    try:
        from flask import current_app
        return bool(current_app and current_app.config.get("TESTING"))
    except RuntimeError:
        return False


def ttl_cache(seconds: float) -> Callable:
    def decorator(fn: Callable) -> Callable:
        store: dict = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _testing():
                return fn(*args, **kwargs)
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit is not None and now - hit[0] < seconds:
                    return hit[1]
            value = fn(*args, **kwargs)
            with lock:
                if len(store) > 64:
                    store.clear()
                store[key] = (time.monotonic(), value)
            return value

        def clear() -> None:
            with lock:
                store.clear()

        wrapper.cache_clear = clear  # type: ignore[attr-defined]
        return wrapper

    return decorator

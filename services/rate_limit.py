"""Shared utility: in-memory rate limiter.

Used by the contact form and analytics endpoints to throttle abuse
without storing IPs. We key on the HMAC of the IP (when configured) or
on a session cookie the browser sends. The limiter is per-process; in a
multi-worker deployment it is approximate but good enough to stop a
single attacker driving 1000s of requests per second from one client.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Deque, Dict, Tuple


class RateLimiter:
    """A simple sliding-window rate limiter."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max = max(1, int(max_calls))
        self._window = max(0.5, float(window_seconds))
        self._lock = threading.Lock()
        self._buckets: Dict[str, Deque[float]] = collections.defaultdict(
            lambda: collections.deque()
        )

    def check(self, key: str) -> Tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``."""
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            dq = self._buckets[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self._max:
                retry_after = max(0.5, self._window - (now - dq[0]))
                return False, retry_after
            dq.append(now)
            # Cap memory growth: drop keys whose bucket is empty.
            if len(self._buckets) > 4096:
                stale = [k for k, v in self._buckets.items() if not v]
                for k in stale:
                    del self._buckets[k]
            return True, 0.0


# Module-level limiters for the public endpoints.
# Tunables: contact form is conservative; analytics is permissive because
# the browser sends ~1 event per page view per session.
CONTACT_LIMITER = RateLimiter(max_calls=5, window_seconds=600.0)   # 5 / 10 min
ANALYTICS_LIMITER = RateLimiter(max_calls=120, window_seconds=60.0)  # 2/sec sustained
ADMIN_LIMITER = RateLimiter(max_calls=20, window_seconds=60.0)  # admin login throttling

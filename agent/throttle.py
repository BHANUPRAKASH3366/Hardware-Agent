"""Per-provider request pacing.

A single search is three requests, one per distributor, and nobody notices.
A 250-line bill of materials is three requests per line, and distributors
answer that with `403 Account Over Queries Per Second Limit`. The failure is
worse than it looks: the search still succeeds from whoever did answer, so a
supplier silently drops out of the comparison and the cheapest offer for that
line can vanish with it.

So calls to any one provider are spaced out. The gate is per provider, not
global: the point is to stay under each distributor's own per-second limit,
and holding up Digi-Key because Farnell is busy would only make the whole BOM
slower for no benefit.
"""
import threading
import time

from . import config


class Gate:
    """Lets one caller per key through at a time, no faster than `interval`."""

    def __init__(self, interval):
        self.interval = max(0.0, float(interval))
        self._locks = {}
        self._next_free = {}
        self._guard = threading.Lock()

    def _lock_for(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    def wait(self, key):
        """Block until this key is allowed to make its next call."""
        if self.interval <= 0:
            return
        with self._lock_for(key):
            now = time.monotonic()
            earliest = self._next_free.get(key, 0.0)
            if earliest > now:
                time.sleep(earliest - now)
                now = time.monotonic()
            self._next_free[key] = now + self.interval


PROVIDER_GATE = Gate(config.PROVIDER_MIN_INTERVAL_MS / 1000.0)


# Phrases distributors use when the answer means "you are going too fast".
# Matched on the message because the status codes disagree: Farnell says 403,
# most others say 429, and a few say 503.
_RATE_PHRASES = (
    "over queries per second",
    "queries per second limit",
    "rate limit",
    "ratelimit",
    "too many requests",
    "quota",
    "throttl",
)


def is_rate_limited(exc):
    """Is this failure the kind that a moment's wait would fix?"""
    status = getattr(exc, "status", None)
    if status in (429, 503):
        return True
    text = str(exc).lower()
    if status == 403 and any(p in text for p in _RATE_PHRASES):
        return True
    return status is None and any(p in text for p in _RATE_PHRASES)


def is_retryable(exc):
    """Rate limits, plus the transient network faults worth one more go."""
    if is_rate_limited(exc):
        return True
    text = str(exc).lower()
    return ("timed out" in text or "network unreachable" in text
            or "connection failed" in text)

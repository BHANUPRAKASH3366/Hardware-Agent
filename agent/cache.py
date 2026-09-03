"""Thread-safe TTL cache. Keeps repeat searches instant and spares API quota."""
import threading
import time

from . import config


class TTLCache:
    def __init__(self, ttl=None, max_entries=512):
        self.ttl = ttl if ttl is not None else config.CACHE_TTL
        self.max_entries = max_entries
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key):
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires, value = entry
            if expires < now:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value, ttl=None):
        """Store `value`. `ttl` overrides the cache default for this entry only.

        OAuth tokens need that override: the issuer decides how long its token
        lives, and caching one for longer than it is valid guarantees a 401.
        """
        ttl = self.ttl if ttl is None else ttl
        if ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self.max_entries:
                # Cheap eviction: drop everything already expired, then the oldest.
                now = time.time()
                for k in [k for k, v in self._data.items() if v[0] < now]:
                    self._data.pop(k, None)
                if len(self._data) >= self.max_entries:
                    oldest = min(self._data, key=lambda k: self._data[k][0])
                    self._data.pop(oldest, None)
            self._data[key] = (time.time() + ttl, value)

    def drop(self, key):
        """Forget one entry -- used when a cached token is rejected upstream."""
        with self._lock:
            self._data.pop(key, None)

    def clear(self):
        with self._lock:
            self._data.clear()

    def stats(self):
        now = time.time()
        with self._lock:
            live = sum(1 for v in self._data.values() if v[0] >= now)
        return {"entries": live, "ttl_seconds": self.ttl}


SEARCH_CACHE = TTLCache()
TOKEN_CACHE = TTLCache(ttl=1500)

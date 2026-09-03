"""Per-distributor daily call counting.

Only Digi-Key reports quota back (`x-ratelimit-limit` / `-remaining`). Mouser
and element14 return nothing at all, so the only way to know what has been
spent with them is to count locally.

Counts are kept on disk rather than in memory: a tally that resets whenever the
server restarts would read as plenty of quota left on the day you restarted
most, which is exactly the day you had least. Where a distributor does report
its own figure that is preferred -- it is authoritative, and it counts calls
this process never made.
"""
import datetime
import json
import os
import threading

from . import config

_PATH = os.path.join(config._ROOT, ".usage.json")
_LOCK = threading.Lock()
_STATE = None


def _today():
    return datetime.date.today().isoformat()


def _blank():
    return {"date": _today(), "providers": {}}


def _load():
    """Read the tally, discarding it if it is from a previous day."""
    global _STATE
    if _STATE is not None and _STATE.get("date") == _today():
        return _STATE
    data = None
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("date") != _today():
        data = _blank()
    data.setdefault("providers", {})
    _STATE = data
    return _STATE


def _save(state):
    # Written via a temporary file and replaced, so a crash mid-write cannot
    # leave a truncated file that reads back as "no calls made today".
    tmp = _PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, _PATH)
    except OSError:
        # Usage tracking must never be the reason a search fails.
        try:
            os.unlink(tmp)
        except OSError:
            pass


def record(key, label=None, calls=1, limit=None, remaining=None):
    """Note `calls` upstream requests to a provider, plus any quota it reported."""
    # calls=0 is a legitimate call: it is how a provider reports the quota it
    # read from a response header without also counting the request twice.
    if not key or (calls <= 0 and limit is None and remaining is None):
        return
    with _LOCK:
        state = _load()
        entry = state["providers"].setdefault(
            key, {"label": label or key, "calls": 0, "limit": None,
                  "remaining": None})
        if label:
            entry["label"] = label
        entry["calls"] += max(0, int(calls))
        if limit is not None:
            entry["limit"] = int(limit)
        if remaining is not None:
            entry["remaining"] = int(remaining)
        _save(state)


def snapshot():
    """Today's usage per provider, for the meta panel."""
    with _LOCK:
        state = _load()
        out = []
        for key, entry in sorted(state["providers"].items()):
            limit = entry.get("limit")
            remaining = entry.get("remaining")
            # A figure the distributor reported always wins. Only fall back to
            # the configured allowance when it never told us anything.
            if limit is None:
                configured = config.DAILY_LIMITS.get(key) or 0
                limit = configured or None
            # Our own count is a floor, not a total: it misses anything spent
            # from another machine or another tool sharing the same key.
            if remaining is None and limit is not None:
                remaining = max(0, limit - entry["calls"])
            out.append({
                "key": key,
                "label": entry.get("label") or key,
                "calls": entry["calls"],
                "limit": limit,
                "remaining": remaining,
                # Whose number this is, so the UI never presents a local
                # estimate as though the distributor had confirmed it.
                "reported": entry.get("remaining") is not None,
            })
        return {"date": state["date"], "providers": out}


def remaining_for(key):
    """Best known remaining calls for one provider, or None if unknown."""
    for entry in snapshot()["providers"]:
        if entry["key"] == key:
            return entry["remaining"]
    return None

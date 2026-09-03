"""Currency helpers.

Rates are pulled live from a free, keyless endpoint and refreshed in the
background; the static table in `config.FX_RATES` is only the fallback for when
the network is unavailable. Every rate is stored the same way -- how many US
dollars one unit of that currency is worth -- so `convert` is a single division
regardless of where the number came from.
"""
import threading
import time

from . import config

SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "INR": "₹",
    "JPY": "¥", "CNY": "¥", "CAD": "CA$", "AUD": "A$",
    "SGD": "S$", "CHF": "CHF ", "SEK": "kr ", "HKD": "HK$",
}

# Keyless, no registration, refreshed once a day upstream.
RATES_URL = "https://open.er-api.com/v6/latest/USD"
RATES_TTL = 6 * 3600
# Don't hammer a failing endpoint on every single conversion.
RETRY_AFTER_FAILURE = 600

_lock = threading.Lock()
_state = {"rates": None, "fetched": 0.0, "asof": None, "source": "static", "tried": 0.0}


def _fetch_live_rates():
    """{currency: USD per unit}, or None if the endpoint cannot be reached."""
    from . import net
    data = net.request_json(RATES_URL, timeout=8)
    if not isinstance(data, dict) or data.get("result") != "success":
        return None, None
    quoted = data.get("rates")
    if not isinstance(quoted, dict):
        return None, None
    out = {}
    for code, per_usd in quoted.items():
        # `per_usd` is units of `code` per 1 USD; we store the inverse.
        if isinstance(per_usd, (int, float)) and not isinstance(per_usd, bool) and per_usd > 0:
            out[str(code).upper()] = 1.0 / float(per_usd)
    if "USD" not in out:
        return None, None
    return out, data.get("time_last_update_utc")


def _rates():
    """Current rate table, refreshing in-band at most once per TTL."""
    if not config.ENABLE_LIVE_FX:
        return dict(config.FX_RATES)
    now = time.time()
    with _lock:
        fresh = _state["rates"] and (now - _state["fetched"]) < RATES_TTL
        if fresh:
            return _state["rates"]
        if (now - _state["tried"]) < RETRY_AFTER_FAILURE and _state["rates"]:
            return _state["rates"]
        if (now - _state["tried"]) < RETRY_AFTER_FAILURE and not _state["rates"]:
            return dict(config.FX_RATES)
        _state["tried"] = now
    try:
        live, asof = _fetch_live_rates()
    except Exception:
        live, asof = None, None
    with _lock:
        if live:
            # Keep any currency the static table knows that the feed omits.
            merged = dict(config.FX_RATES)
            merged.update(live)
            _state.update({"rates": merged, "fetched": time.time(),
                           "asof": asof, "source": "live"})
            return merged
        if _state["rates"]:
            return _state["rates"]
        return dict(config.FX_RATES)


def convert(amount, from_ccy, to_ccy):
    """Convert `amount`; returns None when either currency is unknown."""
    if amount is None:
        return None
    src = (from_ccy or "USD").upper()
    dst = (to_ccy or "USD").upper()
    if src == dst:
        return round(float(amount), 6)
    rates = _rates()
    if src not in rates or dst not in rates or not rates[dst]:
        return None
    usd = float(amount) * rates[src]
    return round(usd / rates[dst], 6)


def supported():
    """Currencies offered in the UI.

    Deliberately limited to the ones with a symbol, so the picker stays short
    and every option renders properly, even though the live feed carries ~160.
    """
    rates = _rates()
    return sorted(c for c in SYMBOLS if c in rates)


def status():
    """Where today's rates came from, for the /api/meta panel."""
    _rates()
    with _lock:
        return {
            "source": _state["source"],
            "asOf": _state["asof"],
            "live": _state["source"] == "live",
        }

"""Configuration loading. Reads .env (if present) then process environment."""
import os
import json

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_ROOT, ".env")


def _load_env_file(path):
    """Minimal .env parser -- no external dependency."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Real environment always wins over the file.
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file(_ENV_PATH)

ROOT = _ROOT
WEB_DIR = os.path.join(_ROOT, "web")


def get(name, default=""):
    return os.environ.get(name, default).strip()


def get_int(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


HOST = get("HOST", "127.0.0.1")
# Shared password for the whole app. Empty means no gate at all, which is the
# right default for something running on your own machine. Set it before the
# app is reachable from anywhere else: without it, whoever finds the URL is
# spending the distributor quota the API keys below are paying for.
APP_PASSWORD = get("APP_PASSWORD", "").strip()
PORT = get_int("PORT", 8080)

# Per-provider network budget. Providers that blow it are reported as timed out
# rather than being allowed to stall the whole search.
REQUEST_TIMEOUT = get_int("REQUEST_TIMEOUT", 12)
MAX_RESULTS_PER_PROVIDER = get_int("MAX_RESULTS_PER_PROVIDER", 12)
# Distributor prices do not move minute to minute, and a 58-line BOM takes
# most of a minute to run -- so a short TTL meant re-uploading the same file
# re-queried everything and rolled the rate-limit dice again, giving a
# different total each time. Long enough to make a repeat run reproducible.
CACHE_TTL = get_int("CACHE_TTL", 900)

# Credentials -- a provider stays dormant until its keys are present.
MOUSER_API_KEY = get("MOUSER_API_KEY")
DIGIKEY_CLIENT_ID = get("DIGIKEY_CLIENT_ID")
DIGIKEY_CLIENT_SECRET = get("DIGIKEY_CLIENT_SECRET")
DIGIKEY_SITE = get("DIGIKEY_SITE", "US")
DIGIKEY_CURRENCY = get("DIGIKEY_CURRENCY", "USD")
NEXAR_CLIENT_ID = get("NEXAR_CLIENT_ID")
NEXAR_CLIENT_SECRET = get("NEXAR_CLIENT_SECRET")
FARNELL_API_KEY = get("FARNELL_API_KEY")
FARNELL_STORE = get("FARNELL_STORE", "uk.farnell.com")
TME_TOKEN = get("TME_TOKEN")
TME_SECRET = get("TME_SECRET")

# Local vision model (Ollama) used to identify a component from a photo. It
# runs on this machine, so the picture never leaves it -- but inference is far
# slower than an HTTP API call, hence the separate, generous timeout.
def _normalise_host(raw):
    host = (raw or "").strip().rstrip("/")
    if not host:
        return "http://127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    return host


ENABLE_VISION = get("ENABLE_VISION", "true").lower() not in ("0", "false", "no")
OLLAMA_HOST = _normalise_host(get("OLLAMA_HOST", "http://127.0.0.1:11434"))
# Blank means "auto": pick the best installed model that can read images.
OLLAMA_VISION_MODEL = get("OLLAMA_VISION_MODEL")
OLLAMA_TIMEOUT = get_int("OLLAMA_TIMEOUT", 180)
# How long Ollama holds the model in VRAM after a request. Keeping it resident
# turns the second photo from a cold load into a warm one.
OLLAMA_KEEP_ALIVE = get("OLLAMA_KEEP_ALIVE", "10m")
# Number of transformer layers to keep on the GPU. A vision model also has to
# fit its image encoder's scratch buffers in VRAM on top of its weights, and on
# a 4 GB card (GTX 1650 and similar) a 3B model that loads fine then crashes
# the moment a photo is encoded -- "an existing connection was forcibly closed"
# is the runner process dying on an out-of-memory allocation. Capping the
# offload leaves room for the encoder. 0 means "let Ollama decide" (default).
OLLAMA_NUM_GPU = get_int("OLLAMA_NUM_GPU", 0)
# Context window for the vision turns. The default 4096 is plenty for a photo
# prompt; a smaller value frees a little more VRAM on a tight card.
OLLAMA_NUM_CTX = get_int("OLLAMA_NUM_CTX", 0)
# Read the package in a separate pass before identifying it. Costs a second
# round of inference, and is the single biggest thing keeping a small model
# from inventing a part number. Turn off only if you need the speed.
VISION_OCR_PASS = get("VISION_OCR_PASS", "true").lower() not in ("0", "false", "no")
# Check proposed part numbers against the live distributors. A part nobody
# stocks is almost always one the model made up.
VISION_VERIFY = get("VISION_VERIFY", "true").lower() not in ("0", "false", "no")
# Let the local model work out which columns of an uploaded bill of materials
# hold the part number and the quantity, when the header names alone are not
# enough to tell. Its answer is checked against the data before it is used.
BOM_MODEL_ASSIST = get("BOM_MODEL_ASSIST", "true").lower() not in ("0", "false", "no")
# Part numbers priced at once. Each one fans out to every supplier, so this is
# really a cap on concurrent upstream calls -- raising it hits rate limits.
BOM_CONCURRENCY = get_int("BOM_CONCURRENCY", 3)
BOM_MAX_LINES = get_int("BOM_MAX_LINES", 250)
# How many rows to ask each distributor for when pricing one BOM line. It is
# not how many are shown -- the line keeps one offer per supplier -- it is how
# far down the distributor's own keyword ranking the exact part is allowed to
# sit and still be found. Six was too few: Digi-Key answers Amphenol's
# DD78M4R7NT2S with ODD78M4R7NT2S first and buries the exact part below the
# cut, so the line priced a different connector. One request either way, so a
# generous figure costs no extra quota.
BOM_RESULTS_PER_PROVIDER = get_int("BOM_RESULTS_PER_PROVIDER", 25)
# Minimum gap between two calls to the SAME distributor. Distributors police a
# per-second rate, and a rate-limited supplier does not fail loudly -- it just
# stops appearing in the comparison, taking its prices with it.
# Daily call allowances. Only Digi-Key reports its own, and that reported
# figure always wins; these are for the distributors that publish a limit on
# their dashboard but never in a response header. 0 means "unknown", which
# shows a running count with no bar rather than a made-up ceiling.
DAILY_LIMITS = {
    "digikey": get_int("DIGIKEY_DAILY_LIMIT", 0),
    "mouser": get_int("MOUSER_DAILY_LIMIT", 0),
    "farnell": get_int("FARNELL_DAILY_LIMIT", 0),
    "nexar": get_int("NEXAR_DAILY_LIMIT", 0),
}
PROVIDER_MIN_INTERVAL_MS = get_int("PROVIDER_MIN_INTERVAL_MS", 500)
# Extra attempts when a call fails in a way that waiting would fix.
PROVIDER_RETRIES = get_int("PROVIDER_RETRIES", 4)
PROVIDER_RETRY_BACKOFF_MS = get_int("PROVIDER_RETRY_BACKOFF_MS", 800)

ENABLE_NEXAR = get("ENABLE_NEXAR", "true").lower() not in ("0", "false", "no")

# Live exchange rates from a keyless daily feed. Turn off to pin conversions
# to the static FX_RATES table below (useful offline, or for repeatable runs).
ENABLE_LIVE_FX = get("ENABLE_LIVE_FX", "true").lower() not in ("0", "false", "no")
# "auto" stands the sample catalogue down as soon as a real source is live, so
# generated figures can never sit in the same table as real ones.
DEMO_MODE = get("ENABLE_DEMO", "auto").lower()

DEFAULT_CURRENCY = get("DISPLAY_CURRENCY", "USD")

# Approximate FX rates, expressed as "1 unit of X = N USD". Override with a JSON
# blob in FX_RATES to keep cross-currency sorting meaningful for your region.
_DEFAULT_FX = {
    "USD": 1.0,
    "EUR": 1.09,
    "GBP": 1.27,
    "INR": 0.012,
    "JPY": 0.0064,
    "CNY": 0.138,
    "CAD": 0.73,
    "AUD": 0.66,
    "SGD": 0.74,
    "CHF": 1.13,
    "SEK": 0.095,
    "HKD": 0.128,
}


def _load_fx():
    raw = get("FX_RATES")
    if not raw:
        return dict(_DEFAULT_FX)
    try:
        merged = dict(_DEFAULT_FX)
        merged.update({k.upper(): float(v) for k, v in json.loads(raw).items()})
        return merged
    except (ValueError, TypeError, AttributeError):
        return dict(_DEFAULT_FX)


FX_RATES = _load_fx()

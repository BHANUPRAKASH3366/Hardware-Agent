"""Mouser Electronics -- official Search API v1. Free key, ~1000 calls/day."""
import re

from .. import config
from ..normalize import make_offer

KEY = "mouser"
LABEL = "Mouser Electronics"
HOMEPAGE = "https://www.mouser.com"
DOCS = "https://www.mouser.com/api-hub/"
ENDPOINT = "https://api.mouser.com/api/v1/search/keyword"

# A single Search API request is capped at 50 records.
MAX_RECORDS = 50

# "17336 In Stock" -- take only the leading integer, never the trailing prose,
# which can carry its own digits (lead times, pack quantities).
_IN_STOCK_RE = re.compile(r"^\s*([\d,]+)\s*(?:in\s*stock)?\s*$", re.I)

# ProductAttributes carries a mix of logistics and physical facts. Only these
# describe the physical part; "Packaging" (Reel / Cut Tape) and "Standard Pack
# Qty" are shipping details and must not be shown as the package.
PACKAGE_ATTRS = (
    "Package / Case", "Package/Case", "Case Style", "Case/Package",
    "Mounting Style", "Package Type",
)


def available():
    if not config.MOUSER_API_KEY:
        return False, "Set MOUSER_API_KEY in .env to enable live Mouser pricing."
    return True, "Live via Mouser Search API v1 (in-stock parts, your account currency)."


def _fix_mojibake(text):
    """Undo the double-encoding Mouser applies to non-ASCII text.

    Mouser serves UTF-8 bytes that were already decoded once as cp1252, so a
    rupee sign arrives as 'â‚¹'. Round-tripping repairs it; anything that does
    not survive the round trip is left exactly as received.
    """
    if not text or not isinstance(text, str):
        return text
    if not any("" <= ch <= "ÿ" for ch in text):
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _stock_of(part):
    """On-hand, orderable stock. Never guesses from free text.

    `AvailabilityInStock` is the authoritative digits-only field. `Availability`
    is prose ("17336 In Stock", but also "None"), so it is only used when it is
    unambiguously a leading count. `FactoryStock` is deliberately ignored: it is
    stock at the manufacturer, not stock Mouser can ship today.
    """
    raw = part.get("AvailabilityInStock")
    if raw is not None and str(raw).strip() != "":
        digits = str(raw).replace(",", "").strip()
        if digits.isdigit():
            return int(digits)
    match = _IN_STOCK_RE.match(str(part.get("Availability") or ""))
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def _package_of(part):
    for attr in part.get("ProductAttributes") or []:
        if not isinstance(attr, dict):
            continue
        name = (attr.get("AttributeName") or "").strip()
        value = (attr.get("AttributeValue") or "").strip()
        if name in PACKAGE_ATTRS and value and value != "-":
            return _fix_mojibake(value)
    return None


def _as_int(value):
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _price_unit_of(part):
    """Mouser prices per piece; only an order multiple changes what you buy."""
    mult = _as_int(part.get("Mult"))
    if mult and mult > 1:
        return "Each (multiples of %d)" % mult
    return None


def _price_of(raw):
    """Mouser prices are display strings ('â‚¹31.68', '$0.15', 'N/A').

    Only the numeric part is kept, and a value with no digits at all is
    rejected outright so a malformed record drops that tier instead of
    silently pricing the part at zero.
    """
    text = str(raw or "")
    if not any(ch.isdigit() for ch in text):
        return None
    return text


def _breaks_of(part, moq):
    """Price breaks, with tiers below the order minimum removed.

    Mouser's `Min` is usually 1, but where it is not, a tier below it cannot be
    ordered and quoting it would understate the real cost of the smallest legal
    order. Unreachable tiers are dropped unless that would leave no price.
    """
    raw = []
    for entry in part.get("PriceBreaks") or []:
        if not isinstance(entry, dict):
            continue
        qty = _as_int(entry.get("Quantity"))
        price = _price_of(entry.get("Price"))
        if qty is None or price is None:
            continue
        raw.append({
            "qty": qty,
            "price": price,
            "currency": (entry.get("Currency") or "USD").strip().upper()[:3],
        })
    if moq and moq > 1:
        reachable = [b for b in raw if b["qty"] >= moq]
        if reachable:
            return reachable
    return raw


def search(query, quantity=1, display_currency="USD", limit=12, category=None, net=None):
    from .. import net as netmod
    net = net or netmod
    want = max(1, min(int(limit or 1), MAX_RECORDS))
    url = "%s?apiKey=%s" % (ENDPOINT, config.MOUSER_API_KEY)
    payload = {
        "SearchByKeywordRequest": {
            "keyword": query,
            "records": want,
            "startingRecord": 0,
            # Only parts Mouser can ship now, so no row promises stock it lacks.
            "searchOptions": "InStock",
        }
    }
    try:
        data = net.request_json(url, method="POST", body=payload)
    except netmod.HttpError as exc:
        # Mouser answers a burst with 403 + TooManyRequests. Surfacing the raw
        # JSON body helps nobody; say what actually happened and what to do.
        if "TooManyRequests" in str(exc) or "MaxCallPerMinute" in str(exc):
            raise netmod.HttpError(
                "Mouser rate limit reached (max calls per minute). "
                "Wait a moment and search again.", status=429) from exc
        raise
    if not isinstance(data, dict):
        return []

    errors = data.get("Errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        msg = first.get("Message") or first.get("Code") or "unknown API error"
        raise netmod.HttpError("Mouser API: %s" % msg)

    parts = ((data.get("SearchResults") or {}).get("Parts")) or []
    offers = []
    for part in parts[:want]:
        if not isinstance(part, dict):
            continue
        moq = _as_int(part.get("Min"))
        breaks = _breaks_of(part, moq)
        offers.append(make_offer(
            source=KEY,
            source_label=LABEL,
            mpn=part.get("ManufacturerPartNumber"),
            manufacturer=part.get("Manufacturer"),
            description=_fix_mojibake(part.get("Description")),
            stock=_stock_of(part),
            breaks=breaks,
            currency=(breaks[0]["currency"] if breaks else "USD"),
            url=part.get("ProductDetailUrl"),
            image=part.get("ImagePath"),
            sku=part.get("MouserPartNumber"),
            moq=moq,
            multiple=_as_int(part.get("Mult")),
            package=_package_of(part),
            price_unit=_price_unit_of(part),
            units_per_price=1,
            base_unit="piece",
            datasheet=part.get("DataSheetUrl"),
            lead_time=part.get("LeadTime"),
            category=_fix_mojibake(part.get("Category")),
            quantity=quantity,
            display_currency=display_currency,
        ))
    return offers

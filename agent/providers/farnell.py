"""Farnell / element14 / Newark -- official product search REST API."""
import re
import urllib.parse

from .. import config
from ..normalize import make_offer

KEY = "farnell"
LABEL = "Farnell / element14"
HOMEPAGE = "https://www.farnell.com"
DOCS = "https://partner.element14.com/"
ENDPOINT = "https://api.element14.com/catalog/products"

# The API caps a single page at 50 records regardless of what we ask for.
MAX_RESULTS = 50

# Each element14 regional store quotes in its own currency.
STORE_CURRENCY = {
    "uk.farnell.com": "GBP", "www.newark.com": "USD", "canada.newark.com": "CAD",
    "de.farnell.com": "EUR", "fr.farnell.com": "EUR", "it.farnell.com": "EUR",
    "es.farnell.com": "EUR", "nl.farnell.com": "EUR", "be.farnell.com": "EUR",
    "at.farnell.com": "EUR", "ie.farnell.com": "EUR", "se.farnell.com": "SEK",
    "dk.farnell.com": "DKK", "no.farnell.com": "NOK", "fi.farnell.com": "EUR",
    "pl.farnell.com": "PLN", "cz.farnell.com": "CZK", "hu.farnell.com": "HUF",
    "ch.farnell.com": "CHF", "in.element14.com": "INR", "sg.element14.com": "SGD",
    "my.element14.com": "MYR", "th.element14.com": "THB", "ph.element14.com": "PHP",
    "vn.element14.com": "VND", "kr.element14.com": "KRW", "tw.element14.com": "TWD",
    "au.element14.com": "AUD", "nz.element14.com": "NZD", "hk.element14.com": "HKD",
    "cn.element14.com": "CNY", "jp.element14.com": "JPY",
    "mx.farnell.com": "MXN", "br.farnell.com": "BRL",
}

# attributeLabel values that carry the physical package, best first. element14
# names this per product family, so we probe the known labels and then fall back
# to a fuzzy match rather than guessing.
PACKAGE_LABELS = (
    "IC Case / Package", "Package / Case", "Case Style", "Package Type",
    "Transistor Case Style", "Capacitor Case Style", "Resistor Case Style",
    "Diode Case Style", "Amplifier Case Style", "Case / Package",
)


def available():
    if not config.FARNELL_API_KEY:
        return False, "Set FARNELL_API_KEY in .env (free at partner.element14.com)."
    store = config.FARNELL_STORE
    currency = STORE_CURRENCY.get(store)
    if not currency:
        return True, ("Live via element14 catalog API (store: %s). This store's "
                      "currency is unknown, so prices are read as GBP -- add it to "
                      "STORE_CURRENCY if that is wrong." % store)
    return True, "Live via element14 catalog API (%s, prices in %s)." % (store, currency)


def _attrs(product):
    out = {}
    for attr in product.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        label = (attr.get("attributeLabel") or "").strip()
        value = attr.get("attributeValue")
        # element14 fills unknown attributes with "-" rather than omitting them.
        if label and value not in (None, "", "-"):
            out[label] = str(value).strip()
    return out


def _package_of(attrs):
    for label in PACKAGE_LABELS:
        if attrs.get(label):
            return attrs[label]
    for label, value in attrs.items():
        low = label.lower()
        if "case style" in low or "package" in low:
            return value
    return None


def _stock_of(product):
    """Free stock for the configured store.

    element14 reports this in three places and they disagree when a record is
    only partly populated, so prefer the authoritative `stock.level`, then the
    sum of the per-warehouse breakdown, then the legacy `inv` field. Returns
    None -- not 0 -- when the API says nothing at all, so the UI can show
    "unknown" instead of claiming the part is out of stock.
    """
    stock = product.get("stock")
    if isinstance(stock, dict):
        level = stock.get("level")
        if isinstance(level, (int, float)) and not isinstance(level, bool):
            return int(level)
        breakdown = stock.get("breakdown")
        if isinstance(breakdown, list):
            total = 0
            seen = False
            for row in breakdown:
                inv = row.get("inv") if isinstance(row, dict) else None
                if isinstance(inv, (int, float)) and not isinstance(inv, bool):
                    total += int(inv)
                    seen = True
            if seen:
                return total
    inv = product.get("inv")
    if isinstance(inv, (int, float)) and not isinstance(inv, bool):
        return int(inv)
    return None


def _lead_time_of(product):
    """`leastLeadTime` is a whole number of days; 0 means it ships from stock."""
    stock = product.get("stock")
    if not isinstance(stock, dict):
        return None
    days = stock.get("leastLeadTime")
    if not isinstance(days, (int, float)) or isinstance(days, bool) or days <= 0:
        return None
    days = int(days)
    return "%d day%s" % (days, "" if days == 1 else "s")


def _description_of(product, manufacturer, mpn):
    """displayName is "BRAND - MPN - Real description"; keep the last part.

    Splitting blind would corrupt descriptions that legitimately contain " - ",
    so only the leading brand and part-number segments are removed, and only
    when they actually match this product.
    """
    name = (product.get("displayName") or "").strip()
    if not name:
        return (product.get("translatedPrimaryDescription") or "").strip() or None
    for prefix in (manufacturer, mpn):
        prefix = (prefix or "").strip()
        if prefix and name.upper().startswith(prefix.upper() + " - "):
            name = name[len(prefix) + 3:].lstrip()
    return name or None


def _image_of(product, store):
    """Product photo URL: /productimages/<vrntPath>/standard/<baseName>.

    The variant segment is not decoration -- it selects the image library the
    photo lives in ("farnell/", "nio/", ...) and the path 404s without it. The
    locale-style ".../standard/en_GB/<name>" form serves farnell images only,
    which is why non-farnell rows came back blank.
    """
    image = product.get("image")
    if not isinstance(image, dict):
        return None
    base = (image.get("baseName") or "").strip().strip("/")
    if not base:
        return None
    variant = (image.get("vrntPath") or "").strip().strip("/")
    if not variant:
        return None
    return "https://%s/productimages/%s/standard/%s" % (store, variant, base)


def _slug(text, limit=60):
    """element14's URL slugs: lowercase, non-alphanumerics collapsed to hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rsplit("-", 1)[0].strip("-")
    return slug


def _product_url(store, sku, manufacturer, mpn, description):
    """Canonical element14 deep link: /brand/mpn/description/dp/SKU.

    A bare "https://store/SKU" is refused by element14's WAF with a 403, and a
    bare "/dp/SKU" 404s -- the storefront resolves a product only from the full
    slug path, so all three leading segments are built from the record itself.
    Without them there is nothing to link to but the search page.
    """
    if not sku:
        return "https://%s" % store
    parts = [_slug(manufacturer, 40), _slug(mpn, 40), _slug(description)]
    if all(parts):
        return "https://%s/%s/dp/%s" % (store, "/".join(parts), sku)
    return "https://%s/search?st=%s" % (store, urllib.parse.quote(sku))


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _units_per_price(product, attrs):
    """(count, base unit) that one priced unit contains.

    A "REEL OF" SKU with a 100 m reel length is priced per reel but holds 100
    metres; a pack holds packSize pieces. Everything else is one of whatever it
    is. Returning this lets the ranking compare a reel against a cut metre.
    """
    uom = (product.get("unitOfMeasure") or "").strip().upper()
    if "REEL" in uom:
        metres = _num(attrs.get("Reel Length (Metric)"))
        if metres and metres > 0:
            return metres, "metre"
        feet = _num(attrs.get("Reel Length (Imperial)"))
        if feet and feet > 0:
            return feet * 0.3048, "metre"
    if uom in ("METRE", "METER", "M"):
        return 1.0, "metre"
    pack = _as_int(product.get("packSize"))
    return float(pack or 1), "piece"


def _num(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _price_unit_of(product, attrs):
    """What one priced unit is: a piece, a metre, or a reel of a given length.

    element14 lists the same cable MPN as SKU 1491567 priced per METRE and SKU
    5051289 priced per 100 m REEL. Both figures are correct; side by side with
    no unit shown they read as the same part at two different prices.
    """
    uom = (product.get("unitOfMeasure") or "").strip()
    if not uom:
        return None
    # element14 shouts these ("EACH (SUPPLIED ON CUT TAPE)"); sentence case reads
    # as a label rather than a warning.
    unit = uom[:1].upper() + uom[1:].lower()
    length = attrs.get("Reel Length (Metric)") or attrs.get("Reel Length (Imperial)")
    if length and "REEL" in uom.upper():
        suffix = "m" if attrs.get("Reel Length (Metric)") else "ft"
        unit = "%s of %s%s" % (unit.split(" of")[0].rstrip(), length, suffix)
    pack = _as_int(product.get("packSize"))
    if pack and pack > 1:
        unit = "%s (pack of %d)" % (unit, pack)
    return unit


def _breaks_of(product, currency, moq):
    """Price breaks, with tiers below MOQ dropped because they cannot be bought.

    A reel SKU may publish a 10-off price while enforcing a 500-piece minimum.
    Quoting the 10-off tier would understate what the smallest legal order
    actually costs, so unreachable tiers are discarded -- unless that would
    leave no price at all, in which case the raw tiers are kept rather than
    blanking the row.
    """
    raw = []
    for entry in product.get("prices") or []:
        if not isinstance(entry, dict):
            continue
        qty = entry.get("from")
        cost = entry.get("cost")
        if qty is None or cost is None:
            continue
        raw.append({"qty": qty, "price": cost, "currency": currency})
    if moq and moq > 1:
        reachable = []
        for item in raw:
            qty = _as_int(item["qty"])
            if qty is not None and qty >= moq:
                reachable.append(item)
        if reachable:
            return reachable
    return raw


# element14's catalog API publishes a minimum order quantity
# (`translatedMinimumOrderQuality`, per SKU) but no order multiple -- it is
# absent from every responseGroup: small, medium, large, prices and inventory.
# The storefront does show one ("Cut Tape (Min: 10 / Mult: 10)"), so it exists,
# but it is not in the feed. Deriving it from the loose/reeled SKU pairing was
# tried and produced wrong steps -- a re-reel with a published Min of 100 came
# out as multiples of 5 from its sibling, where the site says 10 -- and an
# invented step silently changes the quantity priced. So no multiple is
# reported for element14: Min is what element14 states, and nothing else.


def _lowest_moq_by_mpn(products):
    """The smallest minimum element14 publishes for each part number.

    One element14 product page offers the same part in several packagings, each
    its own SKU with its own minimum -- Cut Tape at Min 10 beside Re-Reel at
    Min 500. The smallest of those is the answer to "what is the least of this
    part I can buy here", so it is what the results table reports as Min.

    Every figure here is element14's own published minimum. Nothing is derived:
    this only picks the lowest of the numbers the API already returned, and a
    SKU whose minimum the API does not state contributes nothing.
    """
    lowest = {}
    for product in products:
        if not isinstance(product, dict):
            continue
        mpn = (product.get("translatedManufacturerPartNumber")
               or product.get("manufacturerPartNumber") or "").strip().upper()
        moq = _as_int(product.get("translatedMinimumOrderQuality")
                      or product.get("minimumOrderQuantity"))
        if not mpn or not moq or moq < 1:
            continue
        if mpn not in lowest or moq < lowest[mpn]:
            lowest[mpn] = moq
    return lowest


def search(query, quantity=1, display_currency="USD", limit=12, category=None, net=None):
    from .. import net as netmod
    net = net or netmod
    store = config.FARNELL_STORE
    currency = STORE_CURRENCY.get(store, "GBP")
    want = max(1, min(int(limit or 1), MAX_RESULTS))
    params = {
        "term": "any:%s" % query,
        "storeInfo.id": store,
        "resultsSettings.offset": 0,
        "resultsSettings.numberOfResults": want,
        "resultsSettings.responseGroup": "large",
        "callInfo.responseDataFormat": "json",
        "callInfo.apiKey": config.FARNELL_API_KEY,
    }
    url = "%s?%s" % (ENDPOINT, urllib.parse.urlencode(params))
    data = net.request_json(url)
    if not isinstance(data, dict):
        return []

    root = (data.get("keywordSearchReturn")
            or data.get("premierFarnellPartNumberReturn")
            or data.get("manufacturerPartNumberSearchReturn")
            or {})
    products = root.get("products") or []
    lowest_moq = _lowest_moq_by_mpn(products)
    offers = []
    for product in products[:want]:
        if not isinstance(product, dict):
            continue
        sku = product.get("sku")
        sku = str(sku).strip() if sku not in (None, "") else None
        mpn = (product.get("translatedManufacturerPartNumber")
               or product.get("manufacturerPartNumber"))
        manufacturer = product.get("brandName") or product.get("vendorName")
        attrs = _attrs(product)
        moq = _as_int(product.get("translatedMinimumOrderQuality")
                      or product.get("minimumOrderQuantity"))

        description = _description_of(product, manufacturer, mpn)

        datasheet = None
        for sheet in product.get("datasheets") or []:
            if isinstance(sheet, dict) and sheet.get("url"):
                datasheet = sheet["url"]
                break

        offers.append(make_offer(
            source=KEY,
            source_label=LABEL,
            mpn=mpn,
            manufacturer=manufacturer,
            description=description,
            stock=_stock_of(product),
            breaks=_breaks_of(product, currency, moq),
            currency=currency,
            url=_product_url(store, sku, manufacturer, mpn, description),
            image=_image_of(product, store),
            sku=sku,
            moq=moq,
            # This SKU's own minimum still drives its pricing; the lowest
            # across the part's packagings is what the Min column shows.
            packaging_moq=lowest_moq.get((mpn or "").strip().upper()),
            package=_package_of(attrs),
            price_unit=_price_unit_of(product, attrs),
            units_per_price=_units_per_price(product, attrs)[0],
            base_unit=_units_per_price(product, attrs)[1],
            datasheet=datasheet,
            lead_time=_lead_time_of(product),
            quantity=quantity,
            display_currency=display_currency,
        ))
    return offers

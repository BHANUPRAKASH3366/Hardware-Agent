"""Digi-Key -- Product Information v4 with OAuth2 client-credentials."""
from .. import config, usage
from ..cache import TOKEN_CACHE
from ..normalize import make_offer

KEY = "digikey"
LABEL = "Digi-Key"
HOMEPAGE = "https://www.digikey.com"
DOCS = "https://developer.digikey.com/"
TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"


def available():
    if not (config.DIGIKEY_CLIENT_ID and config.DIGIKEY_CLIENT_SECRET):
        return False, "Set DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET in .env."
    return True, "Live via Digi-Key Product Information v4."


# Digi-Key decides how long its bearer token lives and has changed that figure
# before, so the lifetime is read from the response rather than assumed. The
# margin covers the round trip: a token about to lapse must not be handed out.
_TOKEN_MARGIN = 120
_TOKEN_FALLBACK_TTL = 300


def _token(net, force_refresh=False):
    if not force_refresh:
        cached = TOKEN_CACHE.get("digikey")
        if cached:
            return cached
    data = net.request_json(TOKEN_URL, method="POST", form={
        "client_id": config.DIGIKEY_CLIENT_ID,
        "client_secret": config.DIGIKEY_CLIENT_SECRET,
        "grant_type": "client_credentials",
    })
    token = data.get("access_token")
    if not token:
        raise net.HttpError("Digi-Key did not return an access token")
    try:
        lifetime = int(float(data.get("expires_in")))
    except (TypeError, ValueError):
        lifetime = 0
    # Cache for the issued lifetime less the margin. A token too short-lived to
    # be worth caching is still returned -- it just is not stored.
    TOKEN_CACHE.set("digikey", token, ttl=max(0, lifetime - _TOKEN_MARGIN)
                    if lifetime else _TOKEN_FALLBACK_TTL)
    return token


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _pick_variation(product):
    """v4 nests pricing under ProductVariations; choose the one with real tiers."""
    variations = product.get("ProductVariations") or []
    best = None
    for var in variations:
        if not isinstance(var, dict):
            continue
        if var.get("StandardPricing"):
            # Prefer cut-tape / lowest MOQ so the quoted price is reachable.
            moq = var.get("MinimumOrderQuantity") or 1
            if best is None or moq < (best.get("MinimumOrderQuantity") or 1):
                best = var
    return best or (variations[0] if variations else {})


def search(query, quantity=1, display_currency="USD", limit=12, category=None, net=None):
    from .. import net as netmod
    net = net or netmod
    def _search(token):
        headers = {
            "Authorization": "Bearer %s" % token,
            "X-DIGIKEY-Client-Id": config.DIGIKEY_CLIENT_ID,
            "X-DIGIKEY-Locale-Site": config.DIGIKEY_SITE,
            "X-DIGIKEY-Locale-Currency": config.DIGIKEY_CURRENCY,
            "X-DIGIKEY-Locale-Language": "en",
        }
        # Digi-Key is the one distributor that states the caller's remaining
        # daily quota, in response headers. Recorded here because it is the
        # only place it is visible.
        seen = {}
        payload = net.request_json(
            SEARCH_URL, method="POST", headers=headers,
            body={"Keywords": query, "Limit": min(int(limit), 50), "Offset": 0},
            headers_out=seen,
        )
        usage.record(KEY, LABEL, calls=0,
                     limit=_as_int(seen.get("x-ratelimit-limit")),
                     remaining=_as_int(seen.get("x-ratelimit-remaining")))
        return payload

    try:
        data = _search(_token(net))
    except net.HttpError as exc:
        # A token can still be refused mid-flight: the clock skews, Digi-Key
        # revokes early, or a token cached before a credential change is stale.
        # Nothing is wrong with the request itself, so mint a new token and try
        # once more rather than reporting a failure the user cannot act on.
        if exc.status != 401:
            raise
        TOKEN_CACHE.drop("digikey")
        data = _search(_token(net, force_refresh=True))

    products = data.get("Products") or []
    offers = []
    for product in products[: int(limit)]:
        if not isinstance(product, dict):
            continue
        var = _pick_variation(product)
        breaks = [
            {"qty": b.get("BreakQuantity"), "price": b.get("UnitPrice"),
             "currency": config.DIGIKEY_CURRENCY}
            for b in (var.get("StandardPricing") or [])
            if isinstance(b, dict)
        ]
        desc = product.get("Description") or {}
        if isinstance(desc, dict):
            desc_text = desc.get("ProductDescription") or desc.get("DetailedDescription")
        else:
            desc_text = str(desc)
        manufacturer = product.get("Manufacturer") or {}
        # Distinct from the `category` argument, which is the caller's taxonomy
        # node -- overwriting that here would silently change what a later
        # iteration sees.
        product_category = product.get("Category") or {}
        offers.append(make_offer(
            source=KEY,
            source_label=LABEL,
            mpn=product.get("ManufacturerProductNumber") or product.get("ManufacturerPartNumber"),
            manufacturer=manufacturer.get("Name") if isinstance(manufacturer, dict) else manufacturer,
            description=desc_text,
            stock=product.get("QuantityAvailable"),
            breaks=breaks,
            currency=config.DIGIKEY_CURRENCY,
            url=product.get("ProductUrl"),
            image=product.get("PhotoUrl"),
            sku=var.get("DigiKeyProductNumber"),
            moq=var.get("MinimumOrderQuantity"),
            package=(var.get("PackageType") or {}).get("Name")
            if isinstance(var.get("PackageType"), dict) else var.get("PackageType"),
            datasheet=product.get("DatasheetUrl"),
            lead_time=product.get("ManufacturerLeadWeeks"),
            category=product_category.get("Name")
            if isinstance(product_category, dict) else product_category,
            quantity=quantity,
            display_currency=display_currency,
        ))
    return offers

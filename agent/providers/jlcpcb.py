"""LCSC live stock and pricing, with JLCPCB used as the search index.

No API key required.

Two stages, and the split matters:

1. **Search** goes to JLCPCB's parts endpoint, which has a good keyword index
   and is reachable without credentials. LCSC's own search endpoint is
   bot-protected, so it cannot be used for this.
2. **Figures** come from LCSC's product-detail endpoint, one call per hit, run
   in parallel.

Stage 2 exists because the two systems report *different inventories*. JLCPCB's
`stockCount` is its SMT assembly warehouse; LCSC's `stockNumber` is retail stock
on the page the user actually clicks through to. Quoting the first while linking
to the second means the number on screen disagrees with the number on the
supplier's site -- so every displayed figure is taken from LCSC, the same source
as the link.

Where a part exists only in JLCPCB's catalogue and has no LCSC listing, the row
falls back to JLCPCB's own figures and is labelled "JLCPCB" so the supplier
column always names whoever the stock, price and link belong to.

Disable with ENABLE_JLCPCB=false.
"""
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..normalize import make_offer

KEY = "jlcpcb"
LABEL = "LCSC"
HOMEPAGE = "https://www.lcsc.com"
DOCS = "https://www.lcsc.com"
KIND = "distributor"

SEARCH_URL = ("https://jlcpcb.com/api/overseas-pcb-order/v1"
              "/shoppingCart/smtGood/selectSmtComponentList")
DETAIL_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=%s"
LCSC_HEADERS = {"Referer": "https://www.lcsc.com/", "Origin": "https://www.lcsc.com"}
JLC_HEADERS = {"Origin": "https://jlcpcb.com", "Referer": "https://jlcpcb.com/parts"}
JLC_PRODUCT_URL = "https://jlcpcb.com/partdetail/%s"
LCSC_PRODUCT_URL = "https://www.lcsc.com/product-detail/%s.html"
JLC_IMAGE_URL = "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/%s"


def available():
    if not config.ENABLE_JLCPCB:
        return False, "Disabled via ENABLE_JLCPCB=false."
    return True, ("Live LCSC stock and pricing, verified against the product "
                  "page each row links to -- no API key needed.")


# --------------------------------------------------------------------------- #
# Stage 2: authoritative figures from LCSC
# --------------------------------------------------------------------------- #

def _detail(net, code):
    """LCSC's own record for a product code, or None if it has no listing."""
    if not code:
        return None
    try:
        payload = net.request_json(DETAIL_URL % code, headers=LCSC_HEADERS, timeout=10)
    except Exception:
        return None  # one bad lookup must not sink the whole search
    result = payload.get("result")
    return result if isinstance(result, dict) and result.get("productCode") else None


def _lcsc_breaks(detail):
    out = []
    for tier in detail.get("productPriceList") or []:
        if not isinstance(tier, dict):
            continue
        price = tier.get("usdPrice")
        if price is None:
            price = tier.get("productPrice")
        out.append({"qty": tier.get("ladder"), "price": price, "currency": "USD"})
    return out


def _jlc_breaks(product):
    tiers = product.get("componentPrices") or product.get("buyComponentPrices") or []
    out = [{"qty": t.get("startNumber"), "price": t.get("productPrice"), "currency": "USD"}
           for t in tiers if isinstance(t, dict)]
    if not out and product.get("initialPrice") is not None:
        out.append({"qty": 1, "price": product["initialPrice"], "currency": "USD"})
    return out


def _jlc_image(product):
    for field in ("productBigImageAccessId", "minImageAccessId"):
        if product.get(field):
            return JLC_IMAGE_URL % product[field]
    return product.get("componentImageUrl") or None


# --------------------------------------------------------------------------- #

def search(query, quantity=1, display_currency="USD", limit=12, category=None, net=None):
    from .. import net as netmod
    net = net or netmod

    # Over-fetch: the response is salted with rows dropped below, and without
    # headroom the filters would shrink the result set.
    data = net.request_json(
        SEARCH_URL, method="POST", headers=JLC_HEADERS,
        body={"currentPage": 1, "pageSize": min(int(limit) * 3, 60), "keyword": query},
    )
    if data.get("code") not in (200, 0, None):
        raise netmod.HttpError("JLCPCB search returned code %s: %s"
                               % (data.get("code"), str(data.get("message"))[:120]))

    products = ((data.get("data") or {}).get("componentPageInfo") or {}).get("list") or []

    # Drop rows that cannot be bought at all. The catalogue carries
    # assembly-service placeholders -- branded "JLCPCB Assembly" or
    # "[JLCSMT]-...", zero stock, with a per-joint assembly fee (~$0.02) in the
    # price field. Left in, they sort to the top of a cheapest-first table and
    # quote a price that does not exist.
    candidates = []
    for product in products:
        if not isinstance(product, dict):
            continue
        if not product.get("isBuyComponent") or product.get("noBuyReason"):
            continue
        if str(product.get("componentModelEn") or "").startswith("["):
            continue
        candidates.append(product)
        if len(candidates) >= int(limit) * 2:
            break

    # Stage 2, in parallel: ~10 lookups land in well under a second together,
    # where serially they would add several seconds to every search.
    codes = [p.get("componentCode") for p in candidates]
    if codes:
        with ThreadPoolExecutor(max_workers=min(10, len(codes))) as pool:
            details = list(pool.map(lambda c: _detail(net, c), codes))
    else:
        details = []

    offers = []
    for product, detail in zip(candidates, details):
        if len(offers) >= int(limit):
            break
        code = product.get("componentCode")

        if detail:
            # LCSC is the source of truth: same system as the linked page.
            stock = detail.get("stockNumber")
            if not (stock or 0) > 0:
                continue
            images = detail.get("productImages") or []
            offers.append(make_offer(
                source=KEY,
                source_label=LABEL,
                mpn=detail.get("productModel") or product.get("componentModelEn"),
                manufacturer=detail.get("brandNameEn") or product.get("componentBrandEn"),
                description=(detail.get("productIntroEn")
                             or product.get("componentTypeEn")),
                stock=stock,
                breaks=_lcsc_breaks(detail),
                currency="USD",
                url=(product.get("lcscGoodsUrl") or LCSC_PRODUCT_URL % code),
                image=(images[0] if images and isinstance(images[0], str)
                       else _jlc_image(product)),
                sku=code,
                moq=detail.get("minBuyNumber") or product.get("minPurchaseNum"),
                package=(detail.get("encapStandard")
                         or product.get("componentSpecificationEn")),
                datasheet=(detail.get("pdfUrl")
                           or product.get("dataManualOfficialLink")),
                category=(detail.get("catalogName") or product.get("secondSortName")),
                units_per_price=1,
            base_unit="piece",
            quantity=quantity,
                display_currency=display_currency,
            ))
            continue

        # No LCSC listing: a JLCPCB-only part. Show its figures, but label and
        # link it as JLCPCB so the row is internally consistent.
        stock = product.get("stockCount")
        if not (stock or 0) > 0:
            continue
        offers.append(make_offer(
            source=KEY,
            source_label="JLCPCB",
            mpn=product.get("componentModelEn") or code,
            manufacturer=product.get("componentBrandEn"),
            description=product.get("componentTypeEn"),
            stock=stock,
            breaks=_jlc_breaks(product),
            currency="USD",
            url=JLC_PRODUCT_URL % code if code else None,
            image=_jlc_image(product),
            sku=code,
            moq=product.get("minPurchaseNum"),
            package=product.get("componentSpecificationEn"),
            datasheet=(product.get("dataManualOfficialLink")
                       or product.get("dataManualUrl")),
            category=product.get("secondSortName") or product.get("componentTypeEn"),
            units_per_price=1,
            base_unit="piece",
            quantity=quantity,
            display_currency=display_currency,
        ))
    return offers

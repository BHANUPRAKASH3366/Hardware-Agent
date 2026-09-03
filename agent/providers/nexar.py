"""Nexar (Octopart) -- GraphQL aggregator covering many distributors at once.

One call here can return offers from Digi-Key, Mouser, LCSC, Arrow, TME and
others, which makes it the single highest-value key to configure.
"""
from .. import config
from ..cache import TOKEN_CACHE
from ..normalize import make_offer

KEY = "nexar"
LABEL = "Nexar / Octopart"
HOMEPAGE = "https://nexar.com"
DOCS = "https://portal.nexar.com/"
KIND = "aggregator"
TOKEN_URL = "https://identity.nexar.com/connect/token"
GRAPHQL_URL = "https://api.nexar.com/graphql"

QUERY = """
query SearchMpn($q: String!, $limit: Int!, $currency: String!) {
  supSearchMpn(q: $q, limit: $limit, currency: $currency) {
    results {
      part {
        mpn
        shortDescription
        manufacturer { name }
        bestImage { url }
        bestDatasheet { url }
        category { name }
        sellers(includeBrokers: false) {
          company { name }
          offers {
            sku
            inventoryLevel
            moq
            packaging
            clickUrl
            factoryLeadDays
            prices { quantity price currency }
          }
        }
      }
    }
  }
}
"""


def available():
    if not (config.NEXAR_CLIENT_ID and config.NEXAR_CLIENT_SECRET):
        return False, "Set NEXAR_CLIENT_ID and NEXAR_CLIENT_SECRET in .env."
    if not config.ENABLE_NEXAR:
        # Nexar hands out working credentials on an evaluation app but grants
        # the Supply API a part limit of 0, so every query is rejected until
        # the account is provisioned. Keeping it off avoids a permanent error
        # on every search; flip ENABLE_NEXAR back on once access is granted.
        return False, ("Credentials present but disabled via ENABLE_NEXAR=false "
                       "-- evaluation accounts have a Supply part limit of 0.")
    return True, "Live via Nexar GraphQL -- aggregates many distributors."


def _token(net):
    cached = TOKEN_CACHE.get("nexar")
    if cached:
        return cached
    data = net.request_json(TOKEN_URL, method="POST", form={
        "grant_type": "client_credentials",
        "client_id": config.NEXAR_CLIENT_ID,
        "client_secret": config.NEXAR_CLIENT_SECRET,
        "scope": "supply.domain",
    })
    token = data.get("access_token")
    if not token:
        raise net.HttpError("Nexar did not return an access token")
    TOKEN_CACHE.set("nexar", token)
    return token


def search(query, quantity=1, display_currency="USD", limit=12, category=None, net=None):
    from .. import net as netmod
    net = net or netmod
    token = _token(net)
    data = net.request_json(
        GRAPHQL_URL, method="POST",
        headers={"Authorization": "Bearer %s" % token},
        body={"query": QUERY, "variables": {
            "q": query,
            "limit": min(int(limit), 20),
            "currency": (display_currency or "USD").upper(),
        }},
    )
    if data.get("errors"):
        msg = (data["errors"][0] or {}).get("message", "unknown GraphQL error")
        raise netmod.HttpError("Nexar API: %s" % msg)

    results = (((data.get("data") or {}).get("supSearchMpn") or {}).get("results")) or []
    offers = []
    for result in results:
        part = (result or {}).get("part") or {}
        manufacturer = (part.get("manufacturer") or {}).get("name")
        image = (part.get("bestImage") or {}).get("url")
        datasheet = (part.get("bestDatasheet") or {}).get("url")
        part_category = (part.get("category") or {}).get("name")
        for seller in part.get("sellers") or []:
            company = ((seller or {}).get("company") or {}).get("name") or "Unknown seller"
            for offer in (seller or {}).get("offers") or []:
                if not isinstance(offer, dict):
                    continue
                breaks = [
                    {"qty": p.get("quantity"), "price": p.get("price"),
                     "currency": p.get("currency")}
                    for p in (offer.get("prices") or []) if isinstance(p, dict)
                ]
                lead = offer.get("factoryLeadDays")
                offers.append(make_offer(
                    source=KEY,
                    # Surface the real seller, otherwise every row reads "Nexar".
                    source_label=company,
                    mpn=part.get("mpn"),
                    manufacturer=manufacturer,
                    description=part.get("shortDescription"),
                    stock=offer.get("inventoryLevel"),
                    breaks=breaks,
                    currency=(breaks[0].get("currency") if breaks else display_currency),
                    url=offer.get("clickUrl"),
                    image=image,
                    sku=offer.get("sku"),
                    moq=offer.get("moq"),
                    package=offer.get("packaging"),
                    datasheet=datasheet,
                    lead_time=("%s days" % lead) if lead else None,
                    category=part_category,
                    quantity=quantity,
                    display_currency=display_currency,
                ))
    return offers

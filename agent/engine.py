"""Search orchestration.

Fans a query out to every enabled provider in parallel, normalises the replies
into one table and reports per-provider status. A provider that fails, times out
or returns nothing never blocks or breaks the others -- its status is surfaced to
the UI so the user can see exactly which sources answered.
"""
import datetime
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config, providers, relevance, taxonomy, throttle, usage
from .cache import SEARCH_CACHE

SORTS = {
    "price_asc": lambda o: (o["_sortPrice"], -(o["stock"] or 0)),
    "price_desc": lambda o: (-o["_sortPrice"] if o["_sortPrice"] < float("inf") else float("inf"),),
    "stock_desc": lambda o: (-(o["stock"] or 0), o["_sortPrice"]),
    "stock_asc": lambda o: ((o["stock"] if o["stock"] is not None else 1 << 40), o["_sortPrice"]),
    "name_asc": lambda o: (o["mpn"].lower(), o["_sortPrice"]),
    "supplier_asc": lambda o: ((o["sourceLabel"] or "").lower(), o["_sortPrice"]),
}
DEFAULT_SORT = "price_asc"


def _now_iso():
    """UTC timestamp for when a result set was actually fetched."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _norm_mpn(mpn):
    """Loose key for grouping the same part across distributors."""
    return re.sub(r"[^a-z0-9]", "", (mpn or "").lower())


def apply_packaging_moq(offers):
    """Stamp each offer with the least of that part a supplier will sell.

    A distributor lists one part number once per packaging option, each its own
    SKU with its own minimum: cut tape at 10, re-reel at 500, full reel at
    5,000. "What is the minimum here" is answered by the smallest of those, not
    by whichever SKU happened to win the price comparison -- a line priced off
    the reel was tagged "min 5,000" for a part the same supplier sells ten of.

    Grouped by supplier AND part number, because a minimum is a fact about one
    distributor's packaging of one part; the lowest across two distributors is
    not a quantity either of them will actually ship.

    Every value here is a minimum the supplier published. This only picks the
    smallest of the numbers already returned -- nothing is derived, and a SKU
    whose minimum the supplier does not state contributes nothing. Providers
    that work it out themselves (element14 sees packaging SKUs that relevance
    filtering may later drop) keep the lower of the two answers.
    """
    lowest = {}
    for offer in offers:
        moq = offer.get("moq")
        if not isinstance(moq, int) or isinstance(moq, bool) or moq < 1:
            continue
        key = (offer.get("source"), _norm_mpn(offer.get("mpn")))
        if key not in lowest or moq < lowest[key]:
            lowest[key] = moq

    for offer in offers:
        key = (offer.get("source"), _norm_mpn(offer.get("mpn")))
        low = lowest.get(key)
        if low is None:
            continue
        current = offer.get("packagingMoq")
        offer["packagingMoq"] = low if current is None else min(current, low)
    return offers


def _run_one(provider, query, quantity, currency, limit, category=None,
             assume_part_number=False):
    """Execute one provider, returning (status_dict, offers)."""
    started = time.time()
    ok, reason = provider.available()
    if not ok:
        return {
            "key": provider.KEY, "label": provider.LABEL, "state": "disabled",
            "message": reason, "count": 0, "ms": 0,
            "kind": getattr(provider, "KIND", "distributor"),
            "docs": getattr(provider, "DOCS", ""),
        }, []

    attempts = 0
    try:
        # Rate limits are the common failure on a big BOM, and they are
        # temporary by definition. Pace the calls, then give a throttled
        # distributor a moment and ask again -- dropping it from the
        # comparison silently is the one outcome that produces a wrong answer
        # rather than a slow one.
        while True:
            attempts += 1
            throttle.PROVIDER_GATE.wait(provider.KEY)
            try:
                offers = provider.search(
                    query, quantity=quantity, display_currency=currency,
                    limit=limit, category=category,
                ) or []
                break
            except Exception as exc:
                if attempts > config.PROVIDER_RETRIES or not throttle.is_retryable(exc):
                    raise
                time.sleep(config.PROVIDER_RETRY_BACKOFF_MS * attempts / 1000.0)
        # Distributor keyword engines answer a part number with near-misses as
        # well as the part itself. Those rows are live and real but they are a
        # different component at a different price, so they are dropped rather
        # than left to distort the comparison.
        offers, dropped = relevance.filter_offers(
            query, offers, assume_part_number=assume_part_number)
        # Record which rows are the exact part rather than a tolerated variant,
        # so ranking can prefer them. See relevance.is_exact.
        for offer in offers:
            offer["exactMatch"] = relevance.is_exact(query, offer)
        elapsed = int((time.time() - started) * 1000)
        state = "ok" if offers else "empty"
        if offers:
            message = "%d offer%s" % (len(offers), "" if len(offers) == 1 else "s")
            if dropped:
                message += " (%d unrelated match%s hidden)" % (
                    dropped, "" if dropped == 1 else "es")
        elif dropped:
            message = ("Returned %d part%s, none of them the one searched for."
                       % (dropped, "" if dropped == 1 else "s"))
        else:
            message = "No matching parts at this distributor."
        if attempts > 1:
            message += " (after %d attempts)" % attempts
        usage.record(provider.KEY, provider.LABEL, calls=attempts)
        return {
            "key": provider.KEY, "label": provider.LABEL, "state": state,
            "message": message, "count": len(offers), "ms": elapsed,
            "attempts": attempts,
            "kind": getattr(provider, "KIND", "distributor"),
            "docs": getattr(provider, "DOCS", ""),
        }, offers
    except Exception as exc:  # a bad upstream must not take down the search
        elapsed = int((time.time() - started) * 1000)
        usage.record(provider.KEY, provider.LABEL, calls=attempts)
        return {
            "key": provider.KEY, "label": provider.LABEL, "state": "error",
            "message": str(exc)[:400] or exc.__class__.__name__,
            "count": 0, "ms": elapsed, "attempts": attempts,
            "rateLimited": throttle.is_rate_limited(exc),
            "kind": getattr(provider, "KIND", "distributor"),
            "docs": getattr(provider, "DOCS", ""),
        }, []


def _decorate(offers, quantity, sort, in_stock_only):
    """Add sort keys and best-price / best-availability markers."""
    # Before anything is filtered or ranked away, while every packaging SKU of
    # the part is still present to be compared.
    apply_packaging_moq(offers)
    # Rank on a like-for-like basis. Where a supplier prices a bundle -- a 100 m
    # reel, a pack of 10 -- the headline figure is not comparable with a
    # per-piece row, and sorting on it hands "best price" to the dearer option.
    # The per-base-unit price is only substituted when every offer being
    # compared measures the same base unit, so pieces are never ranked against
    # metres.
    base_units = {o.get("baseUnit") for o in offers if o.get("baseUnit")}
    uniform = len(base_units) == 1
    for offer in offers:
        price = None
        if uniform and offer.get("pricePerBaseUnitDisplay") is not None:
            price = offer["pricePerBaseUnitDisplay"]
        elif uniform and offer.get("pricePerBaseUnit") is not None:
            price = offer["pricePerBaseUnit"]
        if price is None:
            price = offer.get("unitPriceDisplay")
        if price is None:
            price = offer.get("unitPrice")
        offer["_sortPrice"] = float(price) if price is not None else float("inf")
        offer["comparedPerBaseUnit"] = bool(
            uniform and offer.get("unitsPerPrice") and offer["unitsPerPrice"] > 1)

    if in_stock_only:
        offers = [o for o in offers if (o.get("stock") or 0) > 0]

    key = SORTS.get(sort or DEFAULT_SORT, SORTS[DEFAULT_SORT])
    offers.sort(key=key)

    # Per part number: cheapest offer overall, and cheapest that can actually
    # ship the required quantity today.
    by_part = {}
    for offer in offers:
        by_part.setdefault(_norm_mpn(offer["mpn"]), []).append(offer)

    for group in by_part.values():
        priced = [o for o in group if o["_sortPrice"] < float("inf")]
        # Only meaningful when there is actually a competing offer to beat.
        if len(priced) > 1:
            # A distributor lists the same part once per packaging option, and
            # the cheapest of those is routinely a full reel with a minimum in
            # the thousands. Calling that "best price" for someone who wants
            # one piece points at a price they cannot buy at, so an offer
            # orderable at the required quantity is preferred; the badge falls
            # back to plain cheapest only when none of them is.
            def _badge_key(offer):
                return (bool(offer.get("moqRaised")), offer["_sortPrice"])
            cheapest = min(priced, key=_badge_key)
            cheapest["isBestPrice"] = True
            fulfillable = [o for o in priced if (o.get("stock") or 0) >= quantity]
            if len(fulfillable) > 1:
                min(fulfillable, key=_badge_key)["isBestAvailable"] = True

    for offer in offers:
        offer.pop("_sortPrice", None)
        offer.setdefault("isBestPrice", False)
        offer.setdefault("isBestAvailable", False)
    return offers


def _summarise(offers, quantity, currency):
    priced = [o["unitPriceDisplay"] for o in offers if o.get("unitPriceDisplay") is not None]
    in_stock = [o for o in offers if (o.get("stock") or 0) > 0]
    fulfillable = [o for o in offers if (o.get("stock") or 0) >= quantity]
    parts = {_norm_mpn(o["mpn"]) for o in offers}
    suppliers = {o["sourceLabel"] for o in offers if o.get("sourceLabel")}
    return {
        "offers": len(offers),
        "uniqueParts": len(parts),
        "suppliers": len(suppliers),
        "inStock": len(in_stock),
        "fulfillable": len(fulfillable),
        "lowestUnitPrice": round(min(priced), 6) if priced else None,
        "highestUnitPrice": round(max(priced), 6) if priced else None,
        "currency": currency,
        "requiredQty": quantity,
    }


def _resolve_category(query):
    """Interpret a category search and pick the keyword to send distributors.

    An exact part number is left completely alone -- only a phrase that resolves
    to a category node gets rewritten, and the caller reports the interpretation
    back to the user so nothing happens silently.
    """
    node, crumb = taxonomy.match(query)
    if not node:
        return query, None, None
    term = taxonomy.search_term(node) or query
    return term, node["id"], {
        "id": node["id"],
        "name": node["name"],
        "breadcrumb": crumb,
        "searchTerm": term,
        "rewritten": term.lower() != query.strip().lower(),
    }


def _sources_to_run(sources):
    """Resolve the requested source keys to provider modules."""
    if not sources:
        return list(providers.ALL)
    wanted = {s.strip().lower() for s in sources if s and s.strip()}
    chosen = [p for p in providers.ALL if p.KEY in wanted]
    return chosen or list(providers.ALL)


def search(query, quantity=1, currency=None, sources=None, sort=DEFAULT_SORT,
           in_stock_only=False, limit=None, use_cache=True,
           assume_part_number=False):
    query = (query or "").strip()
    if not query:
        raise ValueError("Enter a component name, part number or category to search.")
    if len(query) > 120:
        raise ValueError("Search term is too long (120 characters max).")

    quantity = max(1, min(int(quantity or 1), 10_000_000))
    currency = (currency or config.DEFAULT_CURRENCY).upper()
    if currency not in config.FX_RATES:
        currency = "USD"
    limit = int(limit or config.MAX_RESULTS_PER_PROVIDER)
    sort = sort if sort in SORTS else DEFAULT_SORT
    chosen = _sources_to_run(sources)
    provider_query, category_id, category = _resolve_category(query)

    # Cache the raw provider payloads only; sorting and filtering stay live so
    # the UI can re-sort without another round trip to the distributors.
    cache_key = "|".join([
        provider_query.lower(), str(category_id or ""), str(quantity), currency,
        str(limit), ",".join(sorted(p.KEY for p in chosen)),
        "mpn" if assume_part_number else "",
    ])
    cached = SEARCH_CACHE.get(cache_key) if use_cache else None

    started = time.time()
    if cached:
        statuses, raw = cached
        statuses = [dict(s, cached=True) for s in statuses]
        offers = [dict(o) for o in raw]
    else:
        statuses, offers = [], []
        with ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
            futures = {
                pool.submit(_run_one, p, provider_query, quantity, currency,
                            limit, category_id, assume_part_number): p
                for p in chosen
            }
            for future in as_completed(futures):
                status, found = future.result()
                statuses.append(status)
                offers.extend(found)
        statuses.sort(key=lambda s: (s["state"] != "ok", s["label"].lower()))
        # Only a complete answer is worth remembering. Caching one where a
        # supplier was rate-limited freezes its absence in: every later search
        # for that part returns the same short comparison, and the cheapest
        # offer stays missing until the entry expires.
        # "Complete" means every supplier that was asked actually answered --
        # including the case where they all answered "we do not stock this".
        # That is a real, cacheable result: without it, every not-found line in
        # a BOM re-queries all suppliers on every run and burns quota to be
        # told the same thing.
        answered = [s for s in statuses if s["state"] in ("ok", "empty")]
        complete = bool(answered) and not any(s["state"] == "error" for s in statuses)
        if complete:
            SEARCH_CACHE.set(cache_key, (statuses, [dict(o) for o in offers]))

    offers = _decorate(offers, quantity, sort, in_stock_only)
    return {
        "query": query,
        "category": category,
        "quantity": quantity,
        "currency": currency,
        "sort": sort,
        "inStockOnly": bool(in_stock_only),
        "cached": bool(cached),
        "fetchedAt": _now_iso(),
        "elapsedMs": int((time.time() - started) * 1000),
        "providers": statuses,
        "summary": _summarise(offers, quantity, currency),
        "results": offers,
    }


def search_stream(query, quantity=1, currency=None, sources=None, sort=DEFAULT_SORT,
                  in_stock_only=False, limit=None):
    """Yield (event_name, payload) as each provider replies, then a final merge.

    Streaming keeps the table responsive: a fast distributor shows up in under a
    second instead of waiting on the slowest one in the fan-out.
    """
    query = (query or "").strip()
    if not query:
        yield "error", {"message": "Enter a component name, part number or category to search."}
        return

    quantity = max(1, min(int(quantity or 1), 10_000_000))
    currency = (currency or config.DEFAULT_CURRENCY).upper()
    if currency not in config.FX_RATES:
        currency = "USD"
    limit = int(limit or config.MAX_RESULTS_PER_PROVIDER)
    sort = sort if sort in SORTS else DEFAULT_SORT
    chosen = _sources_to_run(sources)
    provider_query, category_id, category = _resolve_category(query)

    yield "start", {
        "query": query, "quantity": quantity, "currency": currency,
        "category": category,
        "providers": [
            {"key": p.KEY, "label": p.LABEL, "state": "pending",
             "kind": getattr(p, "KIND", "distributor")}
            for p in chosen
        ],
    }

    started = time.time()
    statuses, collected = [], []
    with ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
        futures = [
            pool.submit(_run_one, p, provider_query, quantity, currency, limit,
                        category_id)
            for p in chosen
        ]
        for future in as_completed(futures):
            status, found = future.result()
            statuses.append(status)
            collected.extend(found)
            yield "provider", {"provider": status, "results": found}

    merged = _decorate(collected, quantity, sort, in_stock_only)
    statuses.sort(key=lambda s: (s["state"] != "ok", s["label"].lower()))
    yield "done", {
        "query": query, "category": category,
        "quantity": quantity, "currency": currency,
        "sort": sort, "inStockOnly": bool(in_stock_only),
        # The streaming path never reads the cache -- it always hits the
        # distributors, so anything it returns is live by construction.
        "cached": False,
        "fetchedAt": _now_iso(),
        "elapsedMs": int((time.time() - started) * 1000),
        "providers": statuses,
        "summary": _summarise(merged, quantity, currency),
        "results": merged,
    }

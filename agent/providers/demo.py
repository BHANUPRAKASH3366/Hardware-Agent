"""Offline reference catalogue.

This provider exists so the application is fully usable the moment you start it,
before any API keys are configured. Its parts come from the category tree in
`agent/taxonomy.py`; the stock figures and prices are generated locally and are
NOT live market data -- the UI badges every one of these rows as SAMPLE so they
can never be mistaken for a real quote. The product links are genuine
distributor search URLs for the part number, so they still take you to the right
place.

Turn it off with ENABLE_DEMO=false once your live providers are configured.
"""
import hashlib
import urllib.parse

from .. import config, images, taxonomy
from ..normalize import make_offer

KEY = "demo"
LABEL = "Reference catalogue"
HOMEPAGE = ""
DOCS = ""
KIND = "sample"

# Virtual storefronts the sample rows are spread across, with a price multiplier
# and the URL template used to deep-link the part number into their real search.
STOREFRONTS = [
    ("Digi-Key", 1.00, "https://www.digikey.com/en/products/result?keywords={q}"),
    ("Mouser", 0.98, "https://www.mouser.com/c/?q={q}"),
    ("Farnell", 1.09, "https://uk.farnell.com/search?st={q}"),
    ("Arrow", 1.03, "https://www.arrow.com/en/products/search?q={q}"),
    ("RS Components", 1.16, "https://uk.rs-online.com/web/c/?searchTerm={q}"),
    ("Robu.in", 1.22, "https://robu.in/?s={q}"),
]

BREAK_TIERS = [1, 10, 25, 100, 500, 1000]

_CATALOG = None


def catalog():
    """Every reference part, flattened from the category tree once and cached."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = list(taxonomy.all_parts())
    return _CATALOG


def available():
    mode = config.DEMO_MODE
    if mode in ("0", "false", "no", "off"):
        return False, "Disabled via ENABLE_DEMO=false."
    if mode not in ("1", "true", "yes", "on"):
        # "auto" (the default): step aside the moment a real source can answer,
        # so sample figures never mix into a table of live ones.
        if _live_sources():
            return False, ("Standing down -- a live distributor is configured, so "
                           "results are real. Set ENABLE_DEMO=true to force it on.")
    return True, ("Offline sample data (%d parts across %d categories) -- "
                  "indicative only, never a live quote."
                  % (len(catalog()), len(list(taxonomy.iter_nodes()))))


def _live_sources():
    """Names of non-sample providers that are ready to answer right now."""
    from . import digikey, farnell, mouser, nexar
    return [p.LABEL for p in (mouser, digikey, nexar, farnell)
            if p.available()[0]]


def _seed(*parts):
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:6], "big")


def _score(entry, terms):
    """Rank a catalogue row against the query terms; 0 means no match."""
    haystacks = (
        (entry["mpn"].lower(), 12),
        (entry["manufacturer"].lower(), 4),
        (entry["category"].lower(), 6),
        (entry["description"].lower(), 3),
        (entry["package"].lower(), 2),
    )
    total = 0
    for term in terms:
        hit = 0
        for text, weight in haystacks:
            if term == text:
                hit = max(hit, weight * 3)
            elif text.startswith(term):
                hit = max(hit, weight * 2)
            elif term in text:
                hit = max(hit, weight)
        if hit == 0:
            return 0  # every term must appear somewhere
        total += hit
    return total


def _expand(terms):
    """Map broad words onto the vocabulary the catalogue actually uses."""
    synonyms = {
        "pcb": ["resistor", "capacitor", "connector", "header", "led", "inductor"],
        "embedded": ["mcu", "microcontroller", "module", "sensor"],
        "microcontroller": ["mcu"],
        "mcu": ["microcontroller"],
        "wifi": ["wi-fi"],
        "bluetooth": ["ble"],
        "opamp": ["op amp"],
        "board": ["module"],
    }
    expanded = []
    for term in terms:
        expanded.extend(synonyms.get(term, []))
    return expanded


def _offers_for(entry, quantity, display_currency):
    """Two or three deterministic storefront offers for one catalogue part."""
    mpn = entry["mpn"]
    base = entry["price"]
    quoted = urllib.parse.quote(mpn)
    seed = _seed(mpn)
    count = 2 + (seed % 2)
    start = seed % len(STOREFRONTS)

    offers = []
    for offset in range(count):
        name, multiplier, url_template = STOREFRONTS[(start + offset) % len(STOREFRONTS)]
        local = _seed(mpn, name)
        unit = base * multiplier * (0.88 + (local % 25) / 100.0)
        breaks = []
        for position, tier in enumerate(BREAK_TIERS):
            # Roughly 8.5% off per tier, floored so it stays plausible.
            discount = max(0.55, 1.0 - 0.085 * position)
            breaks.append({"qty": tier, "price": round(unit * discount, 5), "currency": "USD"})
        stock = (local % 9000) + ((local >> 8) % 400) * 5
        offers.append(make_offer(
            source=KEY,
            source_label=name,
            mpn=mpn,
            manufacturer=entry["manufacturer"],
            description=entry["description"],
            stock=stock,
            breaks=breaks,
            currency="USD",
            url=url_template.format(q=quoted),
            sku="%s-%s" % (name[:3].upper(), str(local)[-6:]),
            moq=1,
            package=entry["package"],
            category=entry["category"],
            # Real manufacturer product photo where one is published; the UI
            # falls back to a drawn package outline when this is None.
            image=images.for_part(mpn),
            quantity=quantity,
            display_currency=display_currency,
        ))
    return offers


def search(query, quantity=1, display_currency="USD", limit=12, category=None, net=None):
    # A resolved category wins: return that branch and everything under it, so
    # "op amps" reaches the audio / precision / high-speed parts one level down.
    if category:
        entries = taxonomy.subtree_parts(category)
        if entries:
            entries.sort(key=lambda e: (e["depth"], e["mpn"]))
            matches = entries[: max(1, int(limit))]
            offers = []
            for entry in matches:
                offers.extend(_offers_for(entry, quantity, display_currency))
            return offers

    terms = [t for t in query.lower().replace(",", " ").split() if t]
    if not terms:
        return []

    scored = [(e, _score(e, terms)) for e in catalog()]
    matches = [(e, s) for e, s in scored if s > 0]

    if not matches:
        # Fall back to broader words so a vague query still returns something.
        seen = set()
        for term in _expand(terms):
            for entry in catalog():
                score = _score(entry, [term])
                if score > 0 and entry["mpn"] not in seen:
                    seen.add(entry["mpn"])
                    matches.append((entry, score))

    matches.sort(key=lambda pair: (-pair[1], pair[0]["mpn"]))
    matches = matches[: max(1, int(limit))]

    offers = []
    for entry, _score_value in matches:
        offers.extend(_offers_for(entry, quantity, display_currency))
    return offers

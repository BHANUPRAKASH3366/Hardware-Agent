#!/usr/bin/env python3
"""Regenerate agent/catalogue.py from a distributor's published category tree.

The hand-written tree in agent/taxonomy.py covers the semiconductor taxonomy in
depth and carries reference part numbers. It does not cover the rest of what a
distributor actually sells -- passives, connectors, electromechanical, cable,
enclosures, test gear -- so the category browser stops well short of the real
catalogue.

This pulls that breadth from Digi-Key's Product Information v4 category
endpoint, which is the only one of the configured suppliers that publishes a
machine-readable tree. Mouser's Search API v1 and element14's catalog API
expose no category listing, and LCSC has no documented endpoint. The names are
Digi-Key's, but they are used as search keywords against every configured
supplier, exactly as the hand-written categories already are -- so a broader
tree broadens the search everywhere, not just at Digi-Key.

Usage:  python tools/fetch_categories.py [--depth 2]

Requires DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET in .env.
"""
import argparse
import datetime
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import config, net                      # noqa: E402
from agent.providers import digikey                # noqa: E402

ROOT_URL = "https://api.digikey.com/products/v4/search/categories"
NODE_URL = ROOT_URL + "/%s"
OUT_PATH = os.path.join(config.ROOT, "agent", "catalogue.py")

# Category names that carry no meaning on their own. Every distributor repeats
# these under dozens of parents, and as a search keyword they are useless.
_SKIP = {
    "accessories", "kits", "uncategorized", "unclassified", "misc",
    "miscellaneous", "other", "obsolete", "sample kits", "educational kits",
}

# Every distributor hangs an accessories bucket off each family -- brackets,
# covers, spare screws. They are not components, and left in they outrank the
# component category they sit beside: a search for "USB Type-C connector"
# lands on "USB, DVI, HDMI Connector Accessories".
_SKIP_PATTERN = re.compile(
    r"accessor|spare part|replacement part|^gift|coupon", re.I)


def _headers(token):
    return {
        "Authorization": "Bearer %s" % token,
        "X-DIGIKEY-Client-Id": config.DIGIKEY_CLIENT_ID,
        "X-DIGIKEY-Locale-Site": config.DIGIKEY_SITE,
        "X-DIGIKEY-Locale-Currency": config.DIGIKEY_CURRENCY,
        "X-DIGIKEY-Locale-Language": "en",
    }


def _slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:60] or "category"


def _search_term(name):
    """The keyword worth sending a distributor for this category.

    Distributor category names are built for a faceted browse tree, not for a
    keyword box: "Linear - Amplifiers - Instrumentation, OP Amps, Buffer Amps"
    returns nothing typed literally. The lead segment is the part that reads
    like something an engineer would actually search for, so the dash-joined
    qualifiers are flattened and the trailing enumeration is dropped.
    """
    term = (name or "").replace(" - ", " ")
    pieces = [p.strip() for p in term.split(",") if p.strip()]
    head = pieces[0] if pieces else term.strip()
    # An enumeration that leads with an acronym loses its subject when it is
    # cut at the first comma: "USB, DVI, HDMI Connectors" would become "USB".
    # The head noun lives in the final segment, so it comes along.
    if len(pieces) > 1 and len(head.split()) == 1 and head.isupper():
        last_word = pieces[-1].split()[-1]
        if last_word.lower() not in head.lower():
            head = "%s %s" % (head, last_word)
    head = re.sub(r"\s*\([^)]*\)", "", head).strip()
    return head[:60] or name


def _aliases(name):
    """Ways someone might type this category, beyond its exact name."""
    out = []
    flat = name.replace(" - ", " ").strip()
    if flat.lower() != name.lower():
        out.append(flat)
    term = _search_term(name)
    if term.lower() not in (name.lower(), flat.lower()):
        out.append(term)
    # "Connectors, Interconnects" is also searched as "interconnects".
    for piece in re.split(r"[,/]", name):
        piece = piece.strip()
        if len(piece) > 3 and piece.lower() not in [a.lower() for a in out + [name]]:
            out.append(piece)
    return out[:4]


def _convert(node, depth, max_depth, seen_ids):
    name = (node.get("Name") or "").strip()
    if not name or name.lower() in _SKIP or _SKIP_PATTERN.search(name):
        return None

    node_id = "dk-%s" % _slug(name)
    # Distributors reuse a child name under several parents; the id has to stay
    # unique because the whole app indexes categories by it.
    if node_id in seen_ids:
        node_id = "%s-%s" % (node_id, node.get("CategoryId") or len(seen_ids))
    seen_ids.add(node_id)

    children = []
    if depth < max_depth:
        for child in node.get("Children") or []:
            converted = _convert(child, depth + 1, max_depth, seen_ids)
            if converted:
                children.append(converted)

    entry = {
        "id": node_id,
        "name": name,
        "aliases": _aliases(name),
        "term": _search_term(name),
        "supplierParts": int(node.get("ProductCount") or 0),
    }
    if children:
        entry["children"] = children
    return entry


def _render(nodes, indent=4):
    """Emit the tree as readable Python source rather than a JSON blob."""
    pad = " " * indent
    out = []
    for node in nodes:
        out.append("%s{" % pad)
        out.append('%s    "id": %r,' % (pad, node["id"]))
        out.append('%s    "name": %r,' % (pad, node["name"]))
        out.append('%s    "aliases": %r,' % (pad, node["aliases"]))
        out.append('%s    "term": %r,' % (pad, node["term"]))
        out.append('%s    "supplierParts": %d,' % (pad, node["supplierParts"]))
        if node.get("children"):
            out.append('%s    "children": [' % pad)
            out.append(_render(node["children"], indent + 8))
            out.append("%s    ]," % pad)
        out.append("%s}," % pad)
    return "\n".join(out)


HEADER = '''"""Distributor category catalogue -- the breadth of what suppliers sell.

GENERATED FILE -- do not edit by hand.
Regenerate with:  python tools/fetch_categories.py

Source: Digi-Key Product Information v4 category tree, fetched %(when)s.
%(tops)d top-level categories, %(total)d in total, covering %(parts)s
distributor line items.

These sit alongside the hand-written tree in taxonomy.py, which stays the
authority wherever the two overlap: it carries reference part numbers and
hand-tuned aliases, and its nodes win any name collision. Category names here
are sent as search keywords to every configured supplier, not just Digi-Key.
"""

SUPPLIER_TREE = [
%(body)s
]
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, default=2,
                        help="levels to keep (1 = top only, default 2)")
    args = parser.parse_args()

    ok, reason = digikey.available()
    if not ok:
        raise SystemExit("  Digi-Key credentials are needed to regenerate: %s" % reason)

    print("  Fetching Digi-Key category tree...")
    token = digikey._token(net)
    headers = _headers(token)
    root = net.request_json(ROOT_URL, headers=headers, timeout=30)
    tops = root.get("Categories") or []
    print("  %d top-level categories" % len(tops))

    seen_ids, tree = set(), []
    for i, top in enumerate(tops, 1):
        name = top.get("Name") or "?"
        try:
            # The listing endpoint returns top-level nodes with no children;
            # each subtree has to be asked for by id.
            detail = net.request_json(
                NODE_URL % top.get("CategoryId"), headers=headers, timeout=30)
            node = detail.get("Category") or top
        except net.HttpError as exc:
            print("    ! %-44s %s" % (name, exc))
            node = top
        converted = _convert(node, 0, args.depth, seen_ids)
        if converted:
            tree.append(converted)
            kids = len(converted.get("children") or [])
            print("    %2d/%d  %-46s %d sub" % (i, len(tops), name[:46], kids))
        time.sleep(0.15)          # stay well inside the rate limit

    total = len(seen_ids)
    parts = sum(n["supplierParts"] for n in tree)
    body = _render(tree)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(HEADER % {
            "when": datetime.date.today().isoformat(),
            "tops": len(tree), "total": total, "parts": "{:,}".format(parts),
            "body": body,
        })
    print("\n  Wrote %s" % OUT_PATH)
    print("  %d categories (%d top-level), %s distributor line items."
          % (total, len(tree), "{:,}".format(parts)))
    print("  Restart the app to pick them up:  python app.py")


if __name__ == "__main__":
    main()

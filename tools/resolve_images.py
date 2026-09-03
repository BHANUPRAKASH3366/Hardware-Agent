#!/usr/bin/env python3
"""Resolve real manufacturer product photos for the offline catalogue.

Live distributors return their own product photography, so this only fills the
gap for the built-in reference catalogue. Texas Instruments publishes product
images at a predictable, key-free path keyed on the *device root* rather than
the orderable part number (LM358, not LM358P), so this script trims each part
number back to its root, verifies the image actually exists, and writes the
confirmed URLs to agent/images.py.

No other manufacturer in the catalogue publishes a derivable image path, so
their parts keep the drawn package outline until a distributor API key supplies
a real photo.

Run it from the project root whenever the catalogue changes:

    python tools/resolve_images.py
"""
import os
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import taxonomy  # noqa: E402

TI_IMAGE = "https://www.ti.com/graphics/folders/partimages/%s.jpg"
OUTPUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "agent", "images.py")

_CTX = ssl.create_default_context()
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"}


def image_exists(url):
    """True only for a real image body -- some hosts 200 on an error page."""
    try:
        request = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(request, timeout=12, context=_CTX) as response:
            if response.status != 200:
                return False
            if not response.headers.get("Content-Type", "").startswith("image"):
                return False
            return len(response.read(2048)) > 800
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False


def candidates(mpn):
    """Device-root candidates for a part number, most specific first.

    LM358P -> LM358, TXS0108EPWR -> TXS0108E, LM2596S-ADJ -> LM2596. Longest
    first matters: LM358 trimmed too far becomes LM35, a different device whose
    image would load happily and be wrong.
    """
    base = mpn.upper().split("/")[0]
    seen, out = set(), []
    for stem in (base, base.split("-")[0]):
        for length in range(len(stem), 4, -1):
            candidate = stem[:length].rstrip("-")
            if candidate and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def resolve(part):
    if "texas instruments" not in part["manufacturer"].lower():
        return part["mpn"], None
    for candidate in candidates(part["mpn"]):
        url = TI_IMAGE % candidate
        if image_exists(url):
            return part["mpn"], url
    return part["mpn"], None


def main():
    parts = list(taxonomy.all_parts())
    print("Resolving product photos for %d catalogue parts..." % len(parts))

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(resolve, parts))

    found = {mpn: url for mpn, url in results if url}
    eligible = [p for p in parts if "texas instruments" in p["manufacturer"].lower()]
    print("  %d resolved (of %d Texas Instruments parts, %d total)"
          % (len(found), len(eligible), len(parts)))

    with open(OUTPUT, "w", encoding="utf-8") as fh:
        fh.write('"""Verified product photo URLs for the offline catalogue.\n\n'
                 "GENERATED FILE -- do not edit by hand.\n"
                 "Regenerate with:  python tools/resolve_images.py\n\n"
                 "Every URL here returned a real image body when this file was written.\n"
                 "Parts without an entry fall back to the drawn package outline in the UI,\n"
                 "and any URL that later goes dead does the same at render time.\n"
                 '"""\n\n')
        fh.write("PART_IMAGES = {\n")
        for mpn in sorted(found):
            fh.write('    %r: %r,\n' % (mpn, found[mpn]))
        fh.write("}\n\n\n")
        fh.write("def for_part(mpn):\n")
        fh.write('    """Product photo URL for a part number, or None."""\n')
        fh.write("    return PART_IMAGES.get((mpn or '').strip())\n")

    print("  wrote %s" % OUTPUT)
    missing = [m for m, u in results
               if not u and any(p["mpn"] == m and "texas instruments" in p["manufacturer"].lower()
                                for p in parts)]
    if missing:
        print("  no image published for: %s" % ", ".join(sorted(missing)))


if __name__ == "__main__":
    main()

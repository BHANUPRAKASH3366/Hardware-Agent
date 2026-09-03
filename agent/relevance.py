"""Drop offers a distributor returned that are not actually the part searched for.

Distributor keyword engines are deliberately generous: searching "STM32F103C8T6"
on Mouser also returns an unrelated dev board at 260x the price, and element14
answers "ESP32-WROOM-32E" with three Arduino shields. Those rows are real live
data, but they are not the part the user asked for, and they wreck a price
comparison -- the cheapest and dearest rows in the table stop being comparable.

Filtering only makes sense when the query names a specific part. A descriptive
search ("10k resistor 0603") legitimately returns parts whose MPN shares nothing
with the query, so for those the distributor's own ranking is left alone.
"""
import re

_ALNUM = re.compile(r"[^a-z0-9]")
# A part number: no spaces, mixes letters and digits, long enough to be specific.
_MPN_SHAPED = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9][a-z0-9._/+-]{4,}$", re.I)


def _key(text):
    return _ALNUM.sub("", (text or "").lower())


def looks_like_part_number(query):
    """True when the query names one specific part rather than describing a class.

    Conservative on purpose, and it has to stay that way: this guesses, and
    guessing "part number" about "10k resistor 0603" would throw away every
    legitimate answer to it. A space is the strongest signal of a description,
    so it disqualifies the query outright.

    That is right for a typed search and wrong for a BOM, where the column
    heading already said these are manufacturer part numbers -- and real ones
    do carry spaces: Micron ships MT46V32M8P-5B IT:M. Such a line skipped
    filtering altogether and took whatever the distributor's keyword engine
    ranked first, which is how a 32Mx8 DRAM came back priced as a 16Mx16. A
    caller that knows better says so via `assume_part_number`.
    """
    query = (query or "").strip()
    if not query or " " in query:
        return False
    return bool(_MPN_SHAPED.match(query))


def matches(query, offer):
    """Is this offer plausibly the part the query named?

    Accepts a longer variant of the same part -- a packaging or reel suffix such
    as STM32F103C8T6**TR** -- and also the reverse, where the distributor lists
    the base part for a query that carried a suffix. Distributor SKUs are
    checked too, because Mouser truncates its own part numbers.

    Only the identifiers count. Descriptions are deliberately not searched: a
    dev board's description names the module soldered onto it, so matching on
    description lets a 957-rupee FireBeetle board answer a search for the
    350-rupee ESP32-WROOM-32E module it carries.
    """
    want = _key(query)
    if not want:
        return True
    for field in ("mpn", "sku"):
        have = _key(offer.get(field))
        if have and (want in have or have in want):
            return True
    return False


def is_exact(query, offer):
    """Is this offer the part number that was searched for, character for
    character once punctuation and case are set aside?

    The substring rule in `matches` is deliberately generous, and it has to be:
    a reel suffix is the same component. But generous cuts both ways. Searching
    Amphenol's DD78M4R7NT2S also matches ODD78M4R7NT2S -- a different connector
    that happens to contain the query as a substring, and happened to be
    cheaper, so a cheapest-wins comparison quoted the wrong part at a price the
    BOM would never have been charged. A near-match may stand in when nothing
    else does; it may never outrank the part that was actually asked for.
    """
    want = _key(query)
    if not want:
        return False
    return any(_key(offer.get(field)) == want for field in ("mpn", "sku"))


def filter_offers(query, offers, assume_part_number=False):
    """(kept, dropped) -- offers actually matching `query`.

    Descriptive queries are passed through untouched. For a part-number query
    an empty result is a legitimate answer: it means this distributor does not
    list the part, which is more useful than a table row quoting an unrelated
    component at an unrelated price.

    `assume_part_number` is for callers that already know -- BOM pricing reads
    its queries out of a column headed "Manufacturer Part Number", so there is
    nothing left to guess about.
    """
    if not (assume_part_number or looks_like_part_number(query)):
        return offers, 0
    kept = [o for o in offers if matches(query, o)]
    return kept, len(offers) - len(kept)

"""Canonical offer model shared by every provider.

Distributors disagree about field names, currencies and how price breaks are
expressed. Everything funnels through `make_offer` so the UI only ever sees one
shape, and so a malformed upstream record degrades into a partial row rather
than killing the whole search.
"""
import hashlib
import re

from . import fx


def _num(value):
    """Coerce loose upstream values ('1,234', '$0.15', '5 000') to a float."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u00a0", " ")
    # Strip currency symbols/letters, keep digits, separators and sign.
    cleaned = re.sub(r"[^\d,.\-]", "", text)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # Whichever separator comes last is the decimal point.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        # "0,15" is a decimal comma; "1,234" is a thousands separator.
        cleaned = ".".join(parts) if len(parts[-1]) == 2 and len(parts) == 2 else "".join(parts)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _int(value):
    n = _num(value)
    if n is None:
        return None
    try:
        return int(n)
    except (ValueError, OverflowError):
        return None


def clean_breaks(raw_breaks, default_currency="USD"):
    """Normalise price breaks to a sorted, de-duplicated list."""
    out = {}
    for item in raw_breaks or []:
        if not isinstance(item, dict):
            continue
        qty = _int(item.get("qty"))
        price = _num(item.get("price"))
        if qty is None or qty < 1 or price is None or price < 0:
            continue
        ccy = (item.get("currency") or default_currency or "USD").upper()[:3]
        # Later duplicates for the same quantity win only if cheaper.
        prev = out.get(qty)
        if prev is None or price < prev["price"]:
            out[qty] = {"qty": qty, "price": round(price, 6), "currency": ccy}
    return [out[q] for q in sorted(out)]


def price_at(breaks, quantity):
    """Unit price for `quantity`: the cheapest break whose qty <= quantity.

    Falls back to the smallest break when the requested quantity is below the
    first tier, so the row still shows an indicative price instead of a blank.
    """
    if not breaks:
        return None, None, False
    quantity = max(1, int(quantity or 1))
    eligible = [b for b in breaks if b["qty"] <= quantity]
    if eligible:
        chosen = min(eligible, key=lambda b: b["price"])
        return chosen["price"], chosen["currency"], True
    first = breaks[0]
    return first["price"], first["currency"], False


def make_offer(
    source,
    source_label,
    mpn,
    manufacturer=None,
    description=None,
    stock=None,
    breaks=None,
    currency="USD",
    url=None,
    image=None,
    sku=None,
    moq=None,
    multiple=None,
    packaging_moq=None,
    package=None,
    price_unit=None,
    units_per_price=None,
    base_unit=None,
    datasheet=None,
    lead_time=None,
    category=None,
    quantity=1,
    display_currency="USD",
):
    breaks = clean_breaks(breaks, currency)
    qty = max(1, int(quantity or 1))

    stock_val = _int(stock)
    if stock_val is not None and stock_val < 0:
        stock_val = 0

    # A minimum is only a minimum if the supplier states one. Digi-Key returns
    # MinimumOrderQuantity 0 on variations it has not populated, which is not a
    # quantity anybody can order -- carrying it as 0 turns "not published" into
    # a number on the row. Anything below 1 is therefore no answer at all.
    moq_val = _int(moq)
    if moq_val is not None and moq_val < 1:
        moq_val = None

    # The least of this part the supplier will sell in ANY of its packagings.
    # A distributor lists one part as several SKUs -- cut tape at 10, re-reel at
    # 500 -- and "what is the minimum here" is answered by the smallest of them,
    # not by whichever SKU a given row happens to be. Reported for the Min
    # column only; each row still prices against its own SKU's minimum above,
    # so the money never quotes a quantity that SKU cannot sell.
    packaging_moq_val = _int(packaging_moq)
    if packaging_moq_val is not None and packaging_moq_val < 1:
        packaging_moq_val = None
    if moq_val is not None:
        packaging_moq_val = (moq_val if packaging_moq_val is None
                             else min(packaging_moq_val, moq_val))

    # Price the quantity the customer asked for, raised to the supplier's order
    # minimum. Distributors quote the requested quantity even when on-hand stock
    # is lower -- Mouser will sell 1000 of a part with 150 on the shelf and
    # charge the 1000-off price, shipping the balance on lead time -- so pricing
    # against stock instead would quote a figure the supplier does not charge.
    # A stock shortfall is real and stays visible, but it belongs in the stock
    # column, not folded into the price.
    priced_qty = qty
    if moq_val and moq_val > priced_qty:
        priced_qty = moq_val

    # An order multiple is a second, independent constraint: Mouser's "Mult" of
    # 5 means 12 pieces is not an orderable quantity and the basket becomes 15.
    # Reading it and then pricing 12 anyway understates the line, so the whole
    # BOM total comes out low -- round up to the next orderable step.
    multiple_val = _int(multiple)
    multiple_applied = bool(multiple_val and multiple_val > 1
                            and priced_qty % multiple_val)
    if multiple_applied:
        priced_qty += multiple_val - (priced_qty % multiple_val)

    # The headline price is for the quantity that was actually asked for, never
    # for the supplier's minimum. A BOM total built on minimums answers a
    # question nobody asked -- "what would the basket cost if I let every
    # distributor round my order up to their smallest sellable batch" -- and it
    # can run several times over the real requirement, which makes the figure
    # useless for costing a build. The minimum is still a fact about the line
    # and it is still reported, in its own fields and on its own row tag, but it
    # does not get to define what "total" means.
    unit, unit_ccy, tier_met = price_at(breaks, qty)

    unit_display = fx.convert(unit, unit_ccy, display_currency)
    extended = round(unit * qty, 4) if unit is not None else None
    extended_display = (round(unit_display * qty, 4)
                        if unit_display is not None else None)

    # What the same line would cost if the order were raised to the supplier's
    # minimum, priced at whatever tier that lands on. Carried alongside so a
    # buyer can see the cash difference before committing, without it silently
    # inflating the number they are costing the build against.
    moq_unit, moq_ccy, _moq_tier = price_at(breaks, priced_qty)
    moq_unit_display = fx.convert(moq_unit, moq_ccy, display_currency)
    moq_extended = round(moq_unit * priced_qty, 4) if moq_unit is not None else None
    moq_extended_display = (round(moq_unit_display * priced_qty, 4)
                            if moq_unit_display is not None else None)

    # A priced unit that contains several base units makes the headline price
    # incomparable with a per-piece row; carry the divisor so ranking can undo it.
    units_mult = _num(units_per_price)
    if not units_mult or units_mult <= 0:
        units_mult = 1.0
    per_base = round(unit / units_mult, 6) if unit is not None else None
    per_base_display = (round(unit_display / units_mult, 6)
                        if unit_display is not None else None)

    mpn = (mpn or "").strip() or (sku or "").strip() or "Unknown part"
    ident = "%s|%s|%s" % (source, sku or "", mpn)

    return {
        "id": hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16],
        "source": source,
        "sourceLabel": source_label,
        "mpn": mpn,
        "sku": (sku or "").strip() or None,
        "manufacturer": (manufacturer or "").strip() or None,
        "description": (description or "").strip() or None,
        "category": (category or "").strip() or None,
        "package": (package or "").strip() or None,
        # What one unit of the quoted price actually buys. Distributors sell the
        # same MPN as a piece, a metre or a whole reel, at prices that differ by
        # orders of magnitude; without this the rows look like a pricing bug.
        "priceUnit": (price_unit or "").strip() or None,
        # How many base units (pieces, metres) one priced unit contains, and
        # the resulting like-for-like price. A 100 m reel at 87,120.72 is
        # 871.21 per metre -- cheaper than a 35,895.88 single metre, even
        # though its headline number is larger. Ranking on the headline alone
        # awards "best price" to the dearer option.
        "unitsPerPrice": units_mult,
        "baseUnit": (base_unit or "").strip() or None,
        "pricePerBaseUnit": per_base,
        "pricePerBaseUnitDisplay": per_base_display,
        "stock": stock_val,
        "inStock": bool(stock_val) if stock_val is not None else None,
        "requiredQty": qty,
        # The smallest quantity this supplier will actually sell: the required
        # quantity, raised to the MOQ and rounded to the order multiple. The
        # money columns are NOT priced against it -- see the note above
        # price_at -- but a buyer has to know the basket will hold this many.
        "pricedQty": priced_qty,
        # True when the supplier's minimum is above what was asked for, so the
        # quoted total buys fewer pieces than the basket would contain.
        "moqRaised": priced_qty > qty,
        # Kept for the UI: stock cannot cover the priced quantity today.
        "pricedShort": (stock_val is not None and stock_val < priced_qty),
        # True when the distributor actually publishes a tier at this quantity.
        "stockSufficient": (stock_val >= qty) if stock_val is not None else None,
        "moq": moq_val,
        # Lowest minimum across this part's packaging options at this supplier.
        # This is what the Min column shows; `moq` above stays this SKU's own.
        "packagingMoq": packaging_moq_val,
        "multiple": multiple_val,
        # Which constraint forced the extra pieces, so the UI can say why.
        "moqApplied": bool(moq_val and moq_val > qty),
        "multipleApplied": multiple_applied,
        "priceBreaks": breaks,
        "unitPrice": unit,
        "currency": unit_ccy or (currency or "USD").upper(),
        "priceTierMet": tier_met,
        "extendedPrice": extended,
        "unitPriceDisplay": unit_display,
        "extendedPriceDisplay": extended_display,
        # The minimum-order reality, reported but never totalled.
        "moqUnitPrice": moq_unit,
        "moqUnitPriceDisplay": moq_unit_display,
        "moqExtendedPrice": moq_extended,
        "moqExtendedPriceDisplay": moq_extended_display,
        "displayCurrency": (display_currency or "USD").upper(),
        "leadTime": (lead_time or "").strip() or None,
        "datasheet": (datasheet or "").strip() or None,
        "image": (image or "").strip() or None,
        "url": (url or "").strip() or None,
    }

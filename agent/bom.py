"""Turning an uploaded bill of materials into part numbers and quantities.

A BOM arrives as a spreadsheet or a PDF, and no two are laid out the same way.
The part number column might be headed "MPN", "Mfr Part #", "Manufacturer Part
Number" or nothing at all; the quantity column might be "Qty", "QTY/BOARD" or
"Quantity per assembly". There are usually title rows above the table, notes
below it, and merged cells in between.

Two passes handle that. Deterministic header matching goes first, because when
it works it is exact, free and explainable. Only when it cannot find the
columns does the local model get asked -- it is good at "which of these columns
holds part numbers", which is a reading problem rather than a factual one, and
its answer is a pair of column indexes that the code then checks against the
data before trusting.
"""
import decimal
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config, engine, quantities, relevance

MAX_LINES = 500

# Header synonyms, most specific first -- "manufacturer part number" has to win
# over a bare "part number" when a sheet carries both a manufacturer part and an
# internal one, because only the manufacturer's is orderable at a distributor.
_MPN_HEADERS = [
    r"manufacturer'?s?\s*part\s*(number|no|#)?",
    r"mfr?\.?\s*part\s*(number|no|#)?",
    r"mfg\.?\s*part\s*(number|no|#)?",
    r"mfr?\.?\s*p/?n",
    r"mfg\.?\s*p/?n",
    r"\bmpn\b",
    r"manufacturer\s*p/?n",
    r"vendor\s*part\s*(number|no|#)?",
    r"supplier\s*part\s*(number|no|#)?",
    r"part\s*(number|no|#)",
    r"\bp/?n\b",
    r"\bpart\b",
    r"component",
]

_QTY_HEADERS = [
    r"qty\s*/?\s*(board|assy|assembly|unit|pcb)",
    r"quantity\s*(per|/)\s*(board|assy|assembly|unit)",
    r"\bquantity\b",
    r"\bqty\b",
    r"\bqnty\b",
    r"\bpcs\b",
    r"\bcount\b",
    r"\bamount\b",
]

# Columns that must never be mistaken for the part number, whatever else
# matches. A designator column is full of short alphanumeric strings that look
# like part numbers to a loose matcher.
_NEVER_MPN = [
    r"^ref(erence)?\s*des", r"designator", r"\bref\s*#?$", r"location",
    r"footprint", r"package", r"^value$", r"description", r"^supplier$",
    r"^manufacturer$", r"^mfr\.?$", r"unit\s*price", r"^price", r"cost",
    r"^item$", r"^line$", r"^no\.?$", r"^s\.?\s*no", r"datasheet", r"^url$",
]

_DESC_HEADERS = [r"description", r"^value$", r"comment", r"^part\s*name"]
_MFR_HEADERS = [r"^manufacturer$", r"^mfr\.?$", r"^mfg\.?$", r"^brand$", r"^maker$"]
_REF_HEADERS = [r"ref(erence)?\s*des", r"designator", r"^ref\s*#?$"]


class BomError(Exception):
    """An upload that cannot be interpreted, phrased for the end user."""


def _norm(text):
    return re.sub(r"[\s_]+", " ", str(text or "").strip().lower()).strip(" .:#")


def _matches(text, patterns):
    text = _norm(text)
    if not text:
        return -1
    for rank, pattern in enumerate(patterns):
        if re.search(pattern, text):
            return rank
    return -1


def _find_column(header, patterns, exclude=None):
    """Index of the best-matching column, or None."""
    best, best_rank = None, len(patterns) + 1
    for index, cell in enumerate(header):
        if exclude and _matches(cell, exclude) >= 0:
            continue
        rank = _matches(cell, patterns)
        if rank >= 0 and rank < best_rank:
            best, best_rank = index, rank
    return best


# --------------------------------------------------------------- quantities

def parse_qty(value):
    """A quantity cell to a positive integer, or None.

    Handles the forms a spreadsheet actually produces: "200", "200.0", "1,000",
    "2 pcs", "10 ea". A zero or a negative is treated as "not a quantity" --
    a do-not-populate line has no order attached to it.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return None
        text = re.sub(r"\b(pcs?|pieces?|ea|nos?|units?|off)\b", "", text)
        text = re.sub(r"[,\s]", "", text).strip()
        match = re.match(r"^\d+(\.\d+)?$", text)
        if not match:
            return None
        number = float(text)
    if number <= 0 or number > 10_000_000:
        return None
    return int(round(number))


# Cells that are a placeholder or a footer, never a part number.
_NOT_A_PART = {
    "n/a", "na", "n.a.", "none", "nil", "tbd", "tba", "dnp", "dnf", "do not populate",
    "not populated", "no part", "-", "--", "---", "x", "xx", "total", "subtotal",
    "grand total", "end", "end of bom", "notes", "note", "spare", "blank",
}


def usable_mpn(value):
    """Is this cell usable as a part number, its column already being known?

    Deliberately far more permissive than looks_like_mpn. Once the header has
    said "this column is the manufacturer part number", throwing a cell away
    because it does not look the way part numbers usually look is destroying
    real data -- and real catalogues are full of part numbers that break every
    rule of thumb:

        885012005033                Wurth, all digits, no letters at all
        53261-0571                  Molex, digits and a hyphen
        H.FL-R-SMT-C-(10)           Hirose, dots and brackets
        DF40HC(4.0)-50DS-0.4V(51)   Hirose, brackets and decimals

    So this only rejects what cannot be a part number: prose, placeholders,
    footer labels, dates, and bare short numbers that are really a line count.
    """
    text = str(value or "").strip()
    if len(text) < 3 or len(text) > 48:
        return False
    if text.lower() in _NOT_A_PART:
        return False
    if len(text.split()) > 2:               # a sentence, not a part number
        return False
    if re.match(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$", text):
        return False                        # a date
    if re.fullmatch(r"\d+(\.\d+)?", text):
        # A bare number is a part number only when it is too long to be a
        # quantity, a line number or a price.
        return "." not in text and len(text) >= 5
    # Everything else needs either a digit or a separator; that is what tells
    # "53261-0571" and "H.FL-R-SMT" apart from a stray word like "Total".
    return bool(re.search(r"\d", text) or re.search(r"[-/._]", text))


def looks_like_mpn(value):
    """Is this cell plausibly a manufacturer part number?

    The strict test, used to *identify* which column holds part numbers -- it
    has to tell an MPN column apart from a designator or quantity column, so it
    stays conservative. Once the column is known, extraction uses usable_mpn
    instead.
    """
    text = str(value or "").strip()
    if not text or len(text) < 3 or len(text) > 42:
        return False
    if " " in text.strip():
        # A few real part numbers carry a space, but a cell with several words
        # is a description, so only a single trailing token is tolerated.
        if len(text.split()) > 2:
            return False
    if not re.search(r"\d", text):
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    if re.match(r"^\d+(\.\d+)?$", text):
        return False
    # Dates and prices sneak in from neighbouring columns.
    if re.match(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}$", text):
        return False
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9\-_./+#]*( [A-Za-z0-9\-_./+#]+)?$", text))


# ------------------------------------------------------------ header finding

def _score_header_row(row):
    """How strongly a row reads as a header rather than data."""
    score = 0
    if _find_column(row, _MPN_HEADERS, exclude=_NEVER_MPN) is not None:
        score += 3
    if _find_column(row, _QTY_HEADERS) is not None:
        score += 3
    for cell in row:
        if _matches(cell, _DESC_HEADERS + _MFR_HEADERS + _REF_HEADERS) >= 0:
            score += 1
    # A header row is words, not part numbers.
    if sum(1 for c in row if looks_like_mpn(c)) > 1:
        score -= 3
    return score


def _locate_header(rows):
    """(row index, score) of the most header-like row near the top."""
    best, best_score = None, 0
    for index, row in enumerate(rows[:30]):
        if not any(str(c).strip() for c in row):
            continue
        score = _score_header_row(row)
        if score > best_score:
            best, best_score = index, score
    return best, best_score


# --------------------------------------------------- column choice from data

def _columns_from_data(rows):
    """Guess the part-number and quantity columns by what the cells contain.

    The fallback for a sheet with no usable header at all: the column with the
    most part-number-shaped cells, and the column of small positive integers
    that is not that one.
    """
    width = max((len(r) for r in rows), default=0)
    if not width:
        return None, None

    mpn_hits = [0] * width
    qty_hits = [0] * width
    for row in rows:
        for i in range(min(len(row), width)):
            cell = row[i]
            if looks_like_mpn(cell):
                mpn_hits[i] += 1
            qty = parse_qty(cell)
            if qty is not None and qty <= 100000:
                qty_hits[i] += 1

    mpn_col = max(range(width), key=lambda i: mpn_hits[i])
    if mpn_hits[mpn_col] < 2:
        return None, None

    qty_col = None
    best = 1
    for i in range(width):
        if i == mpn_col:
            continue
        if qty_hits[i] > best:
            qty_col, best = i, qty_hits[i]
    return mpn_col, qty_col


# ------------------------------------------------------------- model assist

_COLUMN_PROMPT = """You are reading a bill of materials for an electronics \
sourcing tool. Below are the first rows of an uploaded table, as a JSON array \
of arrays. Column indexes start at 0.

Identify which column holds the MANUFACTURER PART NUMBER (the orderable part \
number, like LM358P or GRM188R71H104KA93D -- not a reference designator like \
C1 or R14, and not an internal item number) and which column holds the \
QUANTITY needed.

Also say which row index the data starts on, skipping any title or header rows.

If a column is genuinely not present, use -1. Do not guess wildly; -1 is a \
better answer than a wrong column.

Rows:
%(rows)s

Reply with JSON only:
{"mpnColumn": 0, "qtyColumn": 0, "firstDataRow": 0, "note": ""}"""


def _ask_model(rows):
    """Ask the local model which columns to use. Returns (mpn, qty, first_row)."""
    if not config.BOM_MODEL_ASSIST:
        return None, None, None
    from . import vision           # the Ollama plumbing lives there

    probe = vision._PROBE.get()
    if not probe.get("reachable"):
        return None, None, None
    # Any installed model can read a table; a vision model is not needed, and a
    # small text model is much faster here.
    model = vision.pick_model(probe) or (probe.get("models") or [None])[0]
    if not model:
        return None, None, None

    sample = json.dumps([row[:12] for row in rows[:14]], ensure_ascii=False)[:4000]
    try:
        reply = vision._chat_text(model, _COLUMN_PROMPT % {"rows": sample},
                                  num_predict=200)
    except Exception:
        return None, None, None

    parsed = vision._extract_json(reply)
    if not isinstance(parsed, dict):
        return None, None, None

    def _index(key):
        try:
            value = int(parsed.get(key, -1))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    return _index("mpnColumn"), _index("qtyColumn"), _index("firstDataRow")


# -------------------------------------------------------------- spreadsheets

def _column_holds_parts(body, column):
    """Does this column actually hold part numbers?

    Proportional rather than "does any cell look right", because whole
    catalogues exist where every part number breaks the usual shape -- a
    connector BOM can be nothing but Molex numbers like 53261-0571. Most of a
    real part number column is usable; a designator or description column is
    not.
    """
    if column is None:
        return False
    cells = [str(r[column]).strip() for r in body if len(r) > column and str(r[column]).strip()]
    if not cells:
        return False
    if any(looks_like_mpn(cell) for cell in cells):
        return True
    usable = sum(1 for cell in cells if usable_mpn(cell))
    return usable >= max(2, int(len(cells) * 0.6))


def from_rows(rows, source="spreadsheet"):
    """Extract BOM lines from a table. Returns (items, meta)."""
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise BomError("That file has no data in it.")

    header_index, score = _locate_header(rows)
    header = rows[header_index] if header_index is not None else []
    mpn_col = _find_column(header, _MPN_HEADERS, exclude=_NEVER_MPN) if header else None
    qty_col = _find_column(header, _QTY_HEADERS) if header else None
    desc_col = _find_column(header, _DESC_HEADERS) if header else None
    mfr_col = _find_column(header, _MFR_HEADERS) if header else None
    ref_col = _find_column(header, _REF_HEADERS) if header else None
    first_data = (header_index + 1) if header_index is not None else 0
    how = "headers"

    body = rows[first_data:]
    if not _column_holds_parts(body, mpn_col):
        # The header said one thing and the data disagrees, or there was no
        # usable header. Ask the model, then fall back to reading the data.
        asked_mpn, asked_qty, asked_row = _ask_model(rows)
        if asked_mpn is not None:
            candidate_body = rows[asked_row:] if asked_row else body
            if _column_holds_parts(candidate_body, asked_mpn):
                mpn_col, how = asked_mpn, "local model"
                if asked_qty is not None:
                    qty_col = asked_qty
                if asked_row is not None:
                    first_data = asked_row
                    body = rows[first_data:]

    if not _column_holds_parts(body, mpn_col):
        guess_mpn, guess_qty = _columns_from_data(rows)
        if guess_mpn is None:
            raise BomError(
                "No column of manufacturer part numbers could be found in that file. "
                "Make sure one column holds the part numbers and another the quantity.")
        mpn_col, how = guess_mpn, "column contents"
        if qty_col is None:
            qty_col = guess_qty
        # Without a header the data starts at the top.
        if header_index is None or score < 3:
            first_data = 0
            body = rows

    # Every quantity column on the sheet, not just the chosen one. A BOM built
    # for a batch carries both "QTY" per board and "QTY of 5 units", and only
    # the person ordering knows which one they meant -- so both travel to the
    # UI and the choice is theirs.
    qty_columns = []
    if header:
        for index, cell in enumerate(header):
            if _matches(cell, _QTY_HEADERS) >= 0:
                qty_columns.append({"index": index, "name": str(cell).strip()})
    if qty_col is not None and not any(c["index"] == qty_col for c in qty_columns):
        qty_columns.insert(0, {"index": qty_col, "name": (
            str(header[qty_col]).strip() if qty_col < len(header) else "Quantity")})

    # Work out what those columns mean before reading a single one of them: a
    # figure written for a five-unit build is not a quantity until it is known
    # to be one. See agent/quantities.py.
    series = {c["index"]: {} for c in qty_columns}
    for position, row in enumerate(body):
        for candidate in qty_columns:
            index = candidate["index"]
            value = parse_qty(row[index]) if index < len(row) else None
            if value is not None:
                series[index][position] = value
    analysis = quantities.analyze(qty_columns, series, len(body))
    base_col = analysis["baseColumn"]
    divisor = analysis["divisor"] or 1
    if base_col is not None:
        qty_col = base_col

    items, skipped = [], []
    for row in body:
        if mpn_col >= len(row):
            continue
        mpn = str(row[mpn_col] or "").strip()
        if not mpn:
            continue
        row_quantities = {}
        for candidate in qty_columns:
            index = candidate["index"]
            value = parse_qty(row[index]) if index < len(row) else None
            if value is not None:
                row_quantities[str(index)] = value
        cells = {
            "reference": str(row[ref_col]).strip() if (ref_col is not None and ref_col < len(row)) else "",
            "description": str(row[desc_col]).strip() if (desc_col is not None and desc_col < len(row)) else "",
            "manufacturer": str(row[mfr_col]).strip() if (mfr_col is not None and mfr_col < len(row)) else "",
        }
        if not usable_mpn(mpn):
            # Reported rather than dropped in silence: a skipped row is either
            # a footer, or a part number this reader failed to recognise, and
            # the user is the one who can tell which.
            if len(skipped) < 40:
                skipped.append(dict(cells, mpn=mpn,
                                    quantity=row_quantities.get(str(qty_col)) if qty_col is not None else None))
            continue
        # perUnit is the figure everything downstream is built from: the order
        # quantity is perUnit x however many units are wanted, so a change of
        # requirement is a multiplication rather than a re-read of the file.
        base = row_quantities.get(str(base_col)) if base_col is not None else None
        per_unit = None
        if base is not None:
            per_unit = max(1, int(round(base / divisor))) if divisor > 1 else base
        items.append(dict(cells, mpn=mpn, quantities=row_quantities,
                          perUnit=per_unit,
                          quantity=(per_unit * analysis["unitsInFile"]
                                    if per_unit is not None else None)))
        if len(items) >= MAX_LINES:
            break

    meta = {
        "source": source,
        "how": how,
        "headerRow": header_index,
        "mpnColumn": mpn_col,
        "mpnColumnName": (str(header[mpn_col]).strip()
                          if header and mpn_col is not None and mpn_col < len(header) else ""),
        "qtyColumn": qty_col,
        "qtyColumns": qty_columns,
        "quantities": analysis,
        "unitsInFile": analysis["unitsInFile"],
        "columns": [str(c) for c in header] if header else [],
        "skippedRows": len(skipped),
        "skipped": skipped,
        "quantityFound": qty_col is not None,
    }
    return _counted(items, meta)


# ------------------------------------------------------------------ PDF text

# A BOM line inside a PDF, once the columns have been flattened to runs of
# whitespace: somewhere on the line there is a part number, and somewhere there
# is a small integer.
def from_lines(lines, source="pdf"):
    """Extract BOM lines from the text lines of a PDF."""
    rows = []
    for line in lines:
        cells = [c.strip() for c in re.split(r"\s{2,}|\t", line) if c.strip()]
        if len(cells) >= 2:
            rows.append(cells)

    if not rows:
        raise BomError(
            "No table could be found in that PDF. If the part numbers and quantities "
            "are laid out as a table, try exporting it as Excel or CSV instead.")

    # A PDF table has ragged rows -- a missing cell shifts everything left --
    # so the spreadsheet path is tried first and the per-line scan is the
    # fallback, rather than the other way round.
    try:
        items, meta = from_rows(rows, source=source)
        if items:
            return items, meta
    except BomError:
        pass

    items = []
    for cells in rows:
        mpn = next((c for c in cells if looks_like_mpn(c)), None)
        if not mpn:
            continue
        qty = None
        for cell in cells:
            if cell is mpn:
                continue
            value = parse_qty(cell)
            # Prefer a small count over a stock figure or a price that happens
            # to parse; a per-board quantity is rarely above a few thousand.
            if value is not None and value <= 100000:
                qty = value if qty is None else min(qty, value)
        items.append({"mpn": mpn, "quantity": qty, "perUnit": qty,
                      "reference": "", "description": "", "manufacturer": ""})
        if len(items) >= MAX_LINES:
            break

    if not items:
        raise BomError(
            "No manufacturer part numbers could be recognised in that PDF.")
    return _counted(items, {
        "source": source, "how": "line scan", "headerRow": None,
        "mpnColumn": None, "qtyColumn": None, "columns": [],
        "skippedRows": 0, "quantityFound": any(i["quantity"] for i in items),
        "unitsInFile": 1,
        "quantities": {"columns": [], "baseColumn": None, "unitsInFile": 1,
                       "divisor": 1, "how": "line scan", "confident": False,
                       "note": "A PDF has no columns to compare, so each "
                               "quantity is read as one unit's worth."},
    })


# --------------------------------------------------------------------- shared


def _counted(items, meta):
    """Merge repeats, and record honestly how many rows went in.

    The merge is right -- ordering the same part twice is a real mistake -- but
    it used to be the only record of itself, so a file with 101 part numbers on
    it produced 99 lines and said nothing about the other two. That reads as a
    reader that lost them. The counts travel with the result instead, and every
    input row is accounted for: read, merged into another, or skipped.
    """
    merged = _merge(items)
    meta = dict(meta)
    meta["partRows"] = len(items)
    meta["uniqueParts"] = len(merged)
    meta["duplicateRows"] = len(items) - len(merged)
    meta["duplicates"] = [
        {"mpn": i["mpn"], "rows": i["mergedLines"], "quantity": i.get("quantity")}
        for i in merged if i.get("mergedLines", 1) > 1
    ]
    return merged, meta

def _merge(items):
    """Combine repeated part numbers, adding their quantities together.

    A BOM often lists the same capacitor on several lines, once per group of
    designators. Ordering it twice is a real mistake, so the lines are merged
    and the designators joined.
    """
    out, index = [], {}
    for item in items:
        key = relevance._key(item["mpn"])
        if key in index:
            existing = out[index[key]]
            if item["quantity"]:
                existing["quantity"] = (existing["quantity"] or 0) + item["quantity"]
            if item.get("perUnit"):
                existing["perUnit"] = (existing.get("perUnit") or 0) + item["perUnit"]
            # Every quantity column merges, not just the selected one, so
            # switching column after the merge still gives the right totals.
            for column, value in (item.get("quantities") or {}).items():
                merged = existing.setdefault("quantities", {})
                merged[column] = (merged.get(column) or 0) + value
            for field in ("reference", "description", "manufacturer"):
                if item[field] and item[field] not in existing[field]:
                    existing[field] = (existing[field] + ", " + item[field]).strip(", ")
            existing["mergedLines"] = existing.get("mergedLines", 1) + 1
            continue
        index[key] = len(out)
        item = dict(item)
        item.setdefault("mergedLines", 1)
        out.append(item)
    for position, item in enumerate(out, start=1):
        item["line"] = position
    return out


def parse(data, filename=""):
    """Read an uploaded BOM. Returns (items, meta). Raises BomError."""
    from . import pdftext, sheets

    name = (filename or "").lower()
    if data[:5].startswith(b"%PDF") or name.endswith(".pdf"):
        try:
            lines = pdftext.extract_lines(data)
        except pdftext.PdfError as exc:
            raise BomError(str(exc))
        return from_lines(lines, source="pdf")

    try:
        rows = sheets.read(data, filename)
    except sheets.SheetError as exc:
        raise BomError(str(exc))
    return from_rows(rows, source="spreadsheet")


# --------------------------------------------------------------------------- #
# Pricing a whole bill of materials
# --------------------------------------------------------------------------- #


def line_cost(offer):
    """What this line costs at the quantity the BOM actually calls for.

    Not the supplier's minimum order. A distributor with a 500-piece minimum
    will not sell you three, and that is worth knowing -- but it is not what
    the build costs, and a BOM total assembled out of minimums is not a costing
    of anything anyone is buying. So the comparison and the total both run on
    the required quantity, and the minimum travels beside them as its own
    figure. See make_offer in agent/normalize.py.
    """
    cost = offer.get("extendedPriceDisplay")
    if cost is None:
        cost = offer.get("extendedPrice")
    if cost is None:
        unit = offer.get("unitPriceDisplay")
        if unit is None:
            unit = offer.get("unitPrice")
        if unit is None:
            return None
        cost = unit * (offer.get("requiredQty") or 1)
    return float(cost)


def moq_cost(offer):
    """The same line if the order were raised to the supplier's minimum."""
    cost = offer.get("moqExtendedPriceDisplay")
    if cost is None:
        cost = offer.get("moqExtendedPrice")
    return float(cost) if cost is not None else None


def _best_per_supplier(offers, quantity):
    """One offer per supplier: the cheapest way to actually buy this line.

    Falls back to the cheapest offer at all when nobody holds enough stock,
    because a part that has to go on lead time still needs a price against it.

    An offer with no price at all is kept too, at the back. A distributor that
    lists a part but holds none of it often publishes no price breaks with it,
    and dropping those rows made the line indistinguishable from a part number
    no distributor has ever heard of -- the BOM said "no supplier lists this
    part number" about a part sitting on Digi-Key's site with a 26 week lead
    time and a working link. It is out of stock, which is a different problem
    with a different answer, so the row survives and carries its link.
    """
    # Whether this distributor listed the exact part number at all. If it did,
    # its near-matches are not candidates: a cheaper row for a neighbouring
    # part number is not a cheaper way to buy the part the BOM asked for.
    exact_seen = {}
    for offer in offers:
        key = offer.get("source") or offer.get("sourceLabel")
        exact_seen[key] = exact_seen.get(key, False) or bool(offer.get("exactMatch"))

    by_supplier = {}
    for offer in offers:
        key = offer.get("source") or offer.get("sourceLabel")
        if exact_seen.get(key) and not offer.get("exactMatch"):
            continue
        current = by_supplier.get(key)
        if current is None or _offer_rank(offer, quantity) < current[0]:
            by_supplier[key] = (_offer_rank(offer, quantity), offer)

    out = [entry[1] for entry in by_supplier.values()]
    # Across suppliers the near-matches are kept but ranked below the exact
    # part, rather than dropped. Within one distributor a near-match alongside
    # the exact part is noise; across two, it is often the same component under
    # a reel suffix at a distributor that does not list the bare number, and
    # discarding it would hide a real alternative.
    out.sort(key=lambda o: _offer_rank(o, quantity))
    return out


def _offer_rank(offer, quantity=1):
    """Sort key for competing offers on one BOM line, best first.

    Order matters, and the second term is the one that is easy to get wrong.
    Distributors list the same part once per packaging option, each its own SKU
    with its own minimum: element14 sells ASDMB-50.000MHZ-XY-T as a 1,000-piece
    full reel at 259.72, and as cut tape, one piece, at 359.54. Ranked on unit
    price alone the reel wins and the line quotes 259.72 for a single part --
    a price that does not exist for anyone buying one. An offer you can order
    at the quantity you need beats a cheaper one you cannot, every time.
    """
    cost = line_cost(offer)
    return (not offer.get("exactMatch"),        # the part asked for leads
            bool(offer.get("moqRaised")),       # then what you can actually order
            cost is None,                       # then anything with a price
            (offer.get("stock") or 0) < quantity,   # then what is in stock
            cost if cost is not None else float("inf"))  # then cheapest


# A parenthetical made of words, e.g. "(Marketplace)", "(RoHS)", "(obsolete)".
# Anything with a digit in it is left alone: real part numbers carry brackets --
# Hirose ships DF40HC(4.0)-50DS-0.4V(51) -- and stripping those would turn a
# correct search into a wrong one.
_ANNOTATION = re.compile(r"\s*[\(\[]\s*[A-Za-z][A-Za-z /&.-]{1,23}\s*[\)\]]\s*")


def _fallback_mpns(mpn):
    """Cleaned-up spellings to try after the literal part number found nothing.

    BOMs carry buyer's notes welded onto the part number itself --
    "S29GL256P10TFI020(Marketplace)" is one part number and one annotation, and
    no distributor lists the pair. Searched literally it comes back as a part
    nobody has ever heard of, sitting one row above the very same part spelled
    plainly. So the annotation is stripped and the search tried again, but only
    ever as a fallback: the literal string is what the file said, and it gets
    the first and best attempt.
    """
    out, seen = [], {relevance._key(mpn)}
    stripped = _ANNOTATION.sub(" ", mpn).strip(" 	-_,;")
    for candidate in (stripped, re.sub(r"\s+", "", stripped)):
        key = relevance._key(candidate)
        if candidate and key and key not in seen and usable_mpn(candidate):
            seen.add(key)
            out.append(candidate)
    return out


def _availability_note(offers):
    """Why a listed part has no price, in the words a buyer needs.

    Reached only when every supplier that listed the part quoted nothing for
    it, which in practice means none of them is holding any.
    """
    named, lead = [], None
    for offer in offers:
        label = offer.get("sourceLabel") or offer.get("source") or ""
        if label and label not in named:
            named.append(label)
        if lead is None and offer.get("leadTime"):
            lead = str(offer["leadTime"]).strip()
    who = " and ".join([", ".join(named[:-1]), named[-1]]) if len(named) > 1 else (
        named[0] if named else "the supplier")
    note = "Out of stock at %s -- listed, but no price published." % who
    if lead:
        note += (" Manufacturer lead time %s weeks." % lead
                 if lead.replace(".", "", 1).isdigit() else " Lead time %s." % lead)
    return note


def price_line(item, currency, in_stock_only=False):
    """Price one BOM line across every configured supplier."""
    from . import engine

    quantity = item.get("quantity") or 1
    result = {
        "line": item.get("line"),
        "mpn": item["mpn"],
        "quantity": quantity,
        "quantityGiven": item.get("quantity") is not None,
        "reference": item.get("reference", ""),
        "description": item.get("description", ""),
        "manufacturer": item.get("manufacturer", ""),
        "offers": [], "best": None, "status": "none", "message": "",
        "shortOfStock": False, "outOfStock": False, "listed": False,
        "suppliers": 0, "leadTime": "", "searchedAs": "",
        "partial": False, "missingSuppliers": [],
    }

    def _look(term):
        return engine.search(term, quantity=quantity, currency=currency,
                             in_stock_only=in_stock_only,
                             limit=config.BOM_RESULTS_PER_PROVIDER,
                             assume_part_number=True)

    try:
        payload = _look(item["mpn"])
        # Nothing, and nothing failed either -- so the part number as written is
        # not in any catalogue. Before calling it missing, try it without the
        # buyer's annotation welded onto it. See _fallback_mpns.
        if not (payload.get("results") or []) and not [
                pr for pr in payload.get("providers") or [] if pr.get("state") == "error"]:
            for alternate in _fallback_mpns(item["mpn"]):
                retry = _look(alternate)
                if retry.get("results"):
                    payload = retry
                    result["searchedAs"] = alternate
                    break
    except Exception as exc:
        result["status"] = "error"
        result["message"] = str(exc)[:200]
        return result

    # Every packaging SKU this supplier returned for the part is still in
    # `results` here; _best_per_supplier is about to keep one of them. The Min
    # the row reports is the least of the part the supplier will sell in ANY
    # packaging, so it has to be worked out before that reduction throws the
    # other packagings away.
    results = payload.get("results") or []
    engine.apply_packaging_moq(results)
    offers = _best_per_supplier(results, quantity)
    result["offers"] = offers
    failed = [p for p in payload.get("providers") or [] if p.get("state") == "error"]
    if not offers:
        result["message"] = (
            "No supplier lists this part number." if not failed else
            "Not found, and %d supplier%s failed to answer."
            % (len(failed), "" if len(failed) == 1 else "s"))
        # "Not found" is only trustworthy when every supplier actually
        # answered. If one was rate-limited, the part may well be listed there
        # -- which is why the not-found count moved between runs. Marking it
        # partial puts it in the repair pass with the rest.
        result["missingSuppliers"] = [p.get("label") for p in failed]
        result["partial"] = bool(failed)
        return result

    result["listed"] = True
    priced = [o for o in offers if line_cost(o) is not None]
    if not priced:
        # Listed, but nobody quoted a price -- the distributor holds none and
        # publishes no price breaks against it. This is an out-of-stock line,
        # not a missing part number, and it keeps its supplier rows so the
        # links reach the buyer the same way a priced line's do.
        result["status"] = "outOfStock"
        result["outOfStock"] = True
        result["shortOfStock"] = True
        result["suppliers"] = len(offers)
        result["leadTime"] = next((str(o.get("leadTime")).strip()
                                   for o in offers if o.get("leadTime")), "")
        result["message"] = _availability_note(offers)
        result["missingSuppliers"] = [p.get("label") for p in failed]
        result["partial"] = bool(failed)
        return result

    fulfilling = [o for o in priced if (o.get("stock") or 0) >= quantity]
    best = min(fulfilling or priced, key=lambda o: _offer_rank(o, quantity))
    result["status"] = "ok"
    result["best"] = best
    result["shortOfStock"] = not fulfilling
    result["outOfStock"] = not any((o.get("stock") or 0) > 0 for o in priced)
    result["suppliers"] = len(priced)
    result["leadTime"] = str(best.get("leadTime") or "").strip()
    # A supplier that failed on this line was NOT compared, so "best" means
    # best of those that answered. Previously this was recorded only when the
    # line found nothing at all, which meant the common case -- one supplier
    # rate-limited out of three, the other two answered -- looked identical to
    # a complete comparison. The cheapest price can be the one that is missing,
    # so the line has to say so.
    result["missingSuppliers"] = [p.get("label") for p in failed]
    result["partial"] = bool(failed)
    # A minimum order quantity means the line total covers more pieces than the
    # BOM asked for. That is a real cost and it stays in the total, but it has
    # to be visible or the figure looks like an arithmetic bug.
    result["moqRaised"] = bool(best.get("moqRaised"))
    result["pricedQty"] = best.get("pricedQty") or quantity
    return result


_CENTS = decimal.Decimal("0.01")


def line_extended(offer):
    """One line's cost in the display currency, or None if it has none.

    Deliberately does NOT fall back to the native-currency figure. That
    fallback looks harmless and is not: when a rate is missing, adding a
    dollar amount to a rupee running total produces a number that is wrong
    but perfectly plausible, with nothing on screen to say so. A line that
    cannot be converted is excluded and counted instead.
    """
    if not offer:
        return None
    extended = offer.get("extendedPriceDisplay")
    if extended is None:
        return None
    return decimal.Decimal(str(extended)).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)


def _moq_extended(offer):
    """One line at the supplier's minimum, in the display currency, or None."""
    if not offer:
        return None
    extended = offer.get("moqExtendedPriceDisplay")
    if extended is None:
        return None
    return decimal.Decimal(str(extended)).quantize(_CENTS, rounding=decimal.ROUND_HALF_UP)


def _totals(lines, currency):
    priced = [l for l in lines if l.get("best")]
    # Exact decimal arithmetic, and each line rounded to the currency's own
    # precision before it is added. Summing floats and rounding at the end
    # gives a footer that disagrees with the column of figures above it by a
    # paisa or two -- the first thing a buyer notices when checking a sheet.
    total = decimal.Decimal("0")
    moq_total = decimal.Decimal("0")
    counted = unconverted = 0
    for line in priced:
        extended = line_extended(line.get("best"))
        if extended is None:
            unconverted += 1
            continue
        total += extended
        # What the same basket would cost if every line were rounded up to its
        # supplier's minimum. Never the headline figure, but a buyer about to
        # place the order needs it, so it is computed over exactly the same
        # lines as the total it sits beside.
        at_min = _moq_extended(line.get("best"))
        moq_total += at_min if at_min is not None else extended
        counted += 1
    return {
        "lines": len(lines),
        "priced": len(priced),
        # Every line the run reached a definite answer about: priced, or
        # located at a supplier but out of stock. What is left is genuinely
        # unresolved, and that is the number worth acting on.
        "resolved": sum(1 for l in lines
                        if l.get("best") or l.get("status") == "outOfStock"),
        # "Not found" means no supplier has ever heard of the part number.
        # A part that is listed but unbuyable today is counted separately --
        # conflating the two sent people hunting for typos in part numbers
        # that were perfectly correct and simply out of stock.
        "notFound": sum(1 for l in lines if l.get("status") == "none"),
        "outOfStock": sum(1 for l in lines if l.get("status") == "outOfStock"),
        "failed": sum(1 for l in lines if l.get("status") == "error"),
        "shortOfStock": sum(1 for l in lines
                            if l.get("shortOfStock") and l.get("status") == "ok"),
        "moqRaised": sum(1 for l in lines if l.get("moqRaised")),
        # Lines whose comparison was incomplete because a supplier failed.
        "partial": sum(1 for l in lines if l.get("partial")),
        # How many priced lines the total actually covers, so the figure is
        # never read as covering more of the BOM than it does.
        "totalLines": counted,
        "unconverted": unconverted,
        "totalCost": float(total) if counted else None,
        # The same lines costed at supplier minimums, and how many lines differ.
        "moqTotalCost": float(moq_total) if counted else None,
        "currency": currency,
        "noQuantity": sum(1 for l in lines if not l.get("quantityGiven")),
    }


# ------------------------------------------------------------------ job store

# A BOM of 250 parts is 250 fan-outs, which is minutes of work -- far too long
# to hold one request open for. The run happens on a background thread and the
# browser follows it over an event stream, then downloads the exports from the
# finished job.
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600


def _prune():
    now = time.time()
    with _JOBS_LOCK:
        for key in [k for k, v in _JOBS.items() if now - v["created"] > _JOB_TTL]:
            _JOBS.pop(key, None)


def get_job(job_id):
    _prune()
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def job_state(job):
    """The part of a job that is safe and useful to send to the browser."""
    return {
        "id": job["id"],
        "done": job["done"],
        "total": job["total"],
        "completed": len(job["lines"]),
        "currency": job["currency"],
        "units": job["units"],
        "elapsedMs": job["elapsedMs"],
        "totals": job["totals"],
        "error": job.get("error"),
    }


def start_job(items, currency, in_stock_only=False, units=1):
    """Kick off pricing in the background and return the job id.

    `units` is how many of the product are being built. It does not change the
    pricing -- the caller has already turned per-unit figures into order
    quantities -- but it is what the export headings are written against, so it
    travels with the job.
    """
    _prune()
    items = items[:config.BOM_MAX_LINES]
    job = {
        "id": uuid.uuid4().hex[:16],
        "created": time.time(),
        "currency": currency,
        "units": max(1, int(units or 1)),
        "items": items,
        "lines": [],
        "done": False,
        "total": len(items),
        "startedAt": time.time(),
        "elapsedMs": None,
        "totals": None,
        # Lines re-priced after the first pass, drained by the stream so the
        # browser can replace rows it has already drawn.
        "repairs": [],
    }
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job

    def run():
        workers = max(1, min(config.BOM_CONCURRENCY, len(items) or 1))
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for line in pool.map(
                        lambda item: price_line(item, currency, in_stock_only), items):
                    with _JOBS_LOCK:
                        job["lines"].append(line)

            # Second pass over the lines a supplier failed on. The first pass
            # is the burst that triggers the rate limits; by the time it ends
            # the limit has cleared, so asking again usually gets the missing
            # supplier. This is what stops the same file totalling differently
            # on every upload: without it a line's winner depends on which
            # distributors happened to answer during the burst.
            by_line = {item.get("line"): item for item in items}
            for index, line in enumerate(list(job["lines"])):
                if not line.get("partial"):
                    continue
                item = by_line.get(line.get("line"))
                if item is None:
                    continue
                retry = price_line(item, currency, in_stock_only)
                # Keep the retry only if it actually compared more suppliers.
                if len(retry.get("missingSuppliers") or []) >=                         len(line.get("missingSuppliers") or []):
                    continue
                with _JOBS_LOCK:
                    job["lines"][index] = retry
                    job["repairs"].append(retry)
        except Exception as exc:                 # never leave a job hanging
            job["error"] = str(exc)[:200]
        finally:
            job["lines"].sort(key=lambda l: l.get("line") or 0)
            job["totals"] = _totals(job["lines"], currency)
            job["elapsedMs"] = int((time.time() - job["startedAt"]) * 1000)
            job["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return job["id"]


# --------------------------------------------------------------------- export

EXPORT_COLUMNS = [
    ("Line", "r"),
    ("Manufacturer Part Number", "l"),
    ("Manufacturer", "l"),
    ("Description", "l"),
    ("Required qty", "r"),
    ("Supplier", "l"),
    ("Supplier min. qty", "r"),
    ("Stock", "r"),
    ("Unit price", "r"),
    ("Total price", "r"),
    ("Currency", "l"),
    # The least of this part the supplier will sell in ANY of its packagings --
    # cut tape at 10 beside a re-reel at 500 makes this 10. The column beside it
    # is the minimum on the packaging this row is actually priced from, which is
    # a different number whenever the cheapest SKU is not the smallest one.
    ("Min", "r"),
    ("Min (priced SKU)", "r"),
    ("Product link", "l"),
]


def _export_column(label):
    """Index of a column by its heading.

    Written as a lookup rather than a literal so renaming or reordering a
    column cannot leave the totals row writing into the wrong cell -- the
    failure it prevents is silent, and lands in the money columns.
    """
    return next(i for i, (name, _align) in enumerate(EXPORT_COLUMNS)
                if name == label)


_TOTAL_COLUMN = _export_column("Total price")
_CURRENCY_COLUMN = _export_column("Currency")


def export_rows(job):
    """The finished job as a flat table: one row per line, per supplier."""
    rows = []
    for line in job["lines"]:
        if not line.get("offers"):
            row = [""] * len(EXPORT_COLUMNS)
            row[0] = line.get("line")
            row[1] = line["mpn"]
            row[2] = line.get("manufacturer", "")
            row[3] = line.get("description", "")
            row[4] = line.get("quantity")
            row[5] = (line.get("message")
                      or ("out of stock at every supplier"
                          if line.get("status") == "outOfStock"
                          else "not found at any supplier"))
            rows.append(row)
            continue
        # Without a "Best" column, position is the only thing left that says
        # which offer was chosen -- so the chosen one leads its line and the
        # alternatives follow it.
        best_id = (line.get("best") or {}).get("id")
        ordered = sorted(line["offers"], key=lambda o: o.get("id") != best_id)
        for offer in ordered:
            # This sheet has a Currency column, so a figure that could not be
            # converted is still usable -- provided the row is labelled with
            # the currency it is really in, not the one that was requested.
            unit = offer.get("unitPriceDisplay")
            extended = offer.get("extendedPriceDisplay")
            ccy = offer.get("displayCurrency")
            if extended is None:
                unit = offer.get("unitPrice")
                extended = offer.get("extendedPrice")
                ccy = offer.get("currency")
            rows.append([
                line.get("line"),
                line["mpn"],
                offer.get("manufacturer") or line.get("manufacturer", ""),
                offer.get("description") or line.get("description", ""),
                line.get("quantity"),
                offer.get("sourceLabel", ""),
                offer.get("pricedQty") if offer.get("moqRaised") else "",
                offer.get("stock"),
                unit,
                extended,
                ccy or "",
                offer.get("packagingMoq") or offer.get("moq"),
                offer.get("moq"),
                offer.get("url") or "",
            ])

    totals = job.get("totals") or {}
    if totals.get("totalCost") is not None:
        rows.append([""] * len(EXPORT_COLUMNS))
        summary = [""] * len(EXPORT_COLUMNS)
        summary[1] = "TOTAL -- best offer per line"
        summary[_TOTAL_COLUMN] = round(totals["totalCost"], 2)
        summary[_CURRENCY_COLUMN] = totals.get("currency", "")
        rows.append(summary)
    return rows


def export_subtitle(job):
    totals = job.get("totals") or {}
    bits = ["%d part number%s" % (totals.get("lines", 0),
                                  "" if totals.get("lines") == 1 else "s")]
    if totals.get("priced"):
        bits.append("%d priced" % totals["priced"])
    if totals.get("outOfStock"):
        bits.append("%d out of stock" % totals["outOfStock"])
    if totals.get("notFound"):
        bits.append("%d not found" % totals["notFound"])
    if totals.get("totalCost") is not None:
        bits.append("total %s %.2f" % (totals.get("currency", ""), totals["totalCost"]))
    return "  ·  ".join(bits)


# ------------------------------------------------- sourcing sheet export

# The layout a buyer actually circulates: one row per part, the chosen supplier
# only, and the build size written into the two column headings that depend on
# it. It deliberately mirrors the sheet a distributor's own BOM tool returns,
# so it drops straight into an existing procurement workflow.
SOURCING_COLUMNS = [
    ("Index", "r"),
    ("Manufacturer Part Number", "l"),
    ("Manufacturer Name", "l"),
    ("Description", "l"),
    ("Availability", "r"),
    ("Stock Status", "l"),
    ("Quantity of %(units)s Units", "r"),
    ("Supplier", "l"),
    ("%(supplier)s Part Number 1", "l"),
    # The least of this part the supplier will sell in any of its packagings.
    # A buyer reading this sheet wants to know the smallest order that is
    # possible at all, which is not necessarily the minimum on the packaging
    # the price column happens to come from.
    ("Min Order Qty", "r"),
    ("Unit Price 1", "r"),
    ("Extended Price %(units)s units", "r"),
    # Reported, never totalled: the Extended Price column above is for the
    # quantity asked for, and this says what the supplier's smallest sellable
    # batch would cost instead. Blank when the two are the same.
    ("Supplier Min. Order Qty", "r"),
    ("Cost at Supplier Min.", "r"),
    ("Requested Part Number", "l"),
    ("Product link", "l"),
]

# Where the totals line goes. Found by label rather than written as a number,
# so inserting a column cannot silently put the total under the wrong heading.
_EXTENDED_COLUMN = next(i for i, (label, _align) in enumerate(SOURCING_COLUMNS)
                        if label.startswith("Extended Price"))


def sourcing_supplier(job):
    """The supplier name to put in the SKU heading.

    A BOM sourced entirely from one distributor gets that distributor's name,
    exactly as their own export would write it. A BOM whose lines come from
    different distributors cannot honestly claim one, so it says "Supplier" and
    the row still carries the SKU it belongs to.
    """
    names = {(line.get("best") or {}).get("sourceLabel")
             for line in job["lines"] if line.get("best")}
    names.discard(None)
    return names.pop() if len(names) == 1 else "Supplier"


def sourcing_headers(job):
    fields = {"units": job.get("units") or 1, "supplier": sourcing_supplier(job)}
    return [label % fields for label, _align in SOURCING_COLUMNS]


def _stock_status(offer, quantity):
    """The words a buyer needs: can this line be filled from stock today?"""
    stock = offer.get("stock")
    if stock is None:
        # Unknown rather than zero. There is a Product link column now, so this
        # cell says what it knows instead of doubling as the link.
        return "Stock not published"
    if stock >= quantity:
        return "In Stock"
    if stock > 0:
        return "Partial stock"
    return "Out of Stock"


def sourcing_rows(job):
    """The finished job in the buyer's layout: one row per BOM line."""
    rows = []
    total = decimal.Decimal("0")
    for line in job["lines"]:
        quantity = line.get("quantity") or 0
        offer = line.get("best")
        if not offer:
            # An out-of-stock line still knows which supplier lists it and
            # where, so it exports as a real row -- supplier, SKU, stock and
            # link -- with only the price cells empty. Previously it exported
            # as "not found", which is a different instruction to a buyer.
            listed = (line.get("offers") or [None])[0]
            out_of_stock = line.get("status") == "outOfStock"
            rows.append([
                line.get("line"),
                (listed or {}).get("mpn") or line["mpn"],
                (listed or {}).get("manufacturer") or line.get("manufacturer", ""),
                (listed or {}).get("description") or line.get("description", ""),
                (listed or {}).get("stock") if listed else "",
                "Out of Stock" if out_of_stock else (
                    line.get("message") or "Not found at any supplier"),
                quantity,
                (listed or {}).get("sourceLabel") or "",
                (listed or {}).get("sku") or "",
                (listed or {}).get("packagingMoq") or (listed or {}).get("moq") or "",
                "", "", "", "",
                line["mpn"],
                (listed or {}).get("url") or "",
            ])
            continue
        # This sheet has no currency column, so every money cell has to be in
        # the one currency the headings imply. A figure that could not be
        # converted is left blank rather than printed as if it were rupees.
        unit = offer.get("unitPriceDisplay")
        # Same arithmetic as the on-screen total, from the same helper, so the
        # sheet and the screen can never disagree.
        exact = line_extended(offer)
        extended = float(exact) if exact is not None else None
        if exact is not None:
            total += exact
        rows.append([
            line.get("line"),
            offer.get("mpn") or line["mpn"],
            offer.get("manufacturer") or line.get("manufacturer", ""),
            offer.get("description") or line.get("description", ""),
            offer.get("stock"),
            _stock_status(offer, quantity),
            # The quantity the BOM calls for, which is what the price beside it
            # is now for. The supplier's minimum is a separate column.
            quantity,
            offer.get("sourceLabel") or "",
            offer.get("sku") or "",
            offer.get("packagingMoq") or offer.get("moq"),
            unit,
            extended,
            offer.get("pricedQty") if offer.get("moqRaised") else "",
            moq_cost(offer) if offer.get("moqRaised") else "",
            line["mpn"],
            offer.get("url") or "",
        ])

    if rows:
        summary = [""] * len(SOURCING_COLUMNS)
        summary[_EXTENDED_COLUMN] = "Total: %s" % total
        rows.append(summary)
    return rows


def sourcing_subtitle(job):
    totals = job.get("totals") or {}
    units = job.get("units") or 1
    bits = ["%d unit%s" % (units, "" if units == 1 else "s"),
            "%d part number%s" % (totals.get("lines", 0),
                                  "" if totals.get("lines") == 1 else "s")]
    if totals.get("outOfStock"):
        bits.append("%d out of stock" % totals["outOfStock"])
    if totals.get("notFound"):
        bits.append("%d not found" % totals["notFound"])
    if totals.get("totalCost") is not None:
        bits.append("total %s %.2f" % (totals.get("currency", ""), totals["totalCost"]))
    return "  ·  ".join(bits)

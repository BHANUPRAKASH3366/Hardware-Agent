"""Working out what a bill of materials' quantity columns actually mean.

A BOM written for a build carries the same part twice over: "QTY" is what one
board needs, "QTY Of 5 Units" is what the whole batch needs. Reading either one
as "the quantity" is wrong half the time -- order the first and five boards'
worth of parts never arrive, order the second and a request for twenty boards
still buys five.

So the quantity is never taken at face value. Every quantity column is reduced
to a **per-unit** figure and the build size it was written for, and the order
quantity is then per-unit x however many units are actually wanted. That is the
only form that survives the requirement changing, which it always does.

Three things establish the build size, in descending order of trust:

1. **Ratios between columns.** When one column is consistently an exact integer
   multiple of another across the whole sheet, that multiple *is* the build
   size. This is arithmetic over every row, not a guess, so it wins outright.
2. **The header.** "QTY Of 5 Units", "Quantity of 8 Units", "Qty for 10 boards"
   all say it in words.
3. **The local model**, asked only when the first two disagree or say nothing.
   Which column is per-board is a reading problem, which is what it is good at;
   its answer is checked against the data before it is used.
"""
import json
import re
import statistics

from . import config

# "QTY Of 5 Units", "Quantity of 8 Units", "Qty for 10 boards", "Total (x25)".
_UNITS_IN_HEADER = [
    r"(?:of|for|per)\s+(\d{1,6})\s*(?:units?|nos?|sets?|boards?|pcbs?|assy|assemblies|pcs)",
    r"\b(\d{1,6})\s*(?:units?|sets?|boards?|pcbs?|assemblies)\b",
    r"[x×]\s*(\d{1,6})\b",
]

# A column that is explicitly one board's worth, whatever the numbers say.
_PER_UNIT_HEADER = [
    r"\bper\s*(?:board|unit|assy|assembly|pcb|piece|set)\b",
    r"/\s*(?:board|unit|assy|assembly|pcb)\b",
    r"\bqty\s*/\s*(?:board|unit|assy|assembly|pcb)\b",
    r"\bunit\s*qty\b",
]

# A column that is explicitly the whole build, without naming the size.
_TOTAL_HEADER = [r"\btotal\b", r"\bextended\b", r"\bgrand\b", r"\boverall\b",
                 r"\bbuild\b", r"\bbatch\b", r"\border\s*qty\b"]

MAX_UNITS = 1_000_000


def _norm(text):
    return re.sub(r"[\s_]+", " ", str(text or "").strip().lower())


def units_in_header(text):
    """The build size a column heading names, or None."""
    text = _norm(text)
    if not text:
        return None
    for pattern in _UNITS_IN_HEADER:
        match = re.search(pattern, text)
        if match:
            value = int(match.group(1))
            if 2 <= value <= MAX_UNITS:
                return value
    return None


def _says(text, patterns):
    text = _norm(text)
    return any(re.search(p, text) for p in patterns)


# ------------------------------------------------------------------- ratios

def _ratio_between(values_a, values_b):
    """The exact integer multiple b/a, when the whole sheet agrees on one.

    Both arguments are {row: quantity}. Returns (multiple, agreement) where
    agreement is the share of shared rows that hold to it, or (None, 0.0).

    Exactness is the point. A column that is *roughly* five times another is
    two unrelated columns; a column that is exactly five times another on 57 of
    59 rows is the same column scaled, and the two odd rows are a typo in the
    source sheet -- which is worth surviving, hence a share rather than all.
    """
    shared = [row for row in values_a if row in values_b]
    if len(shared) < 3:
        return None, 0.0
    ratios = []
    for row in shared:
        base = values_a[row]
        if base <= 0:
            continue
        ratios.append(values_b[row] / base)
    if not ratios:
        return None, 0.0
    # The mode rather than the mean: one bad row drags a mean off the integer
    # it should be sitting on, while the mode is simply the answer most rows
    # give.
    candidate = statistics.mode([round(r, 6) for r in ratios])
    if candidate < 2 or candidate > MAX_UNITS or abs(candidate - round(candidate)) > 1e-9:
        return None, 0.0
    multiple = int(round(candidate))
    agreement = sum(1 for r in ratios if abs(r - multiple) < 1e-9) / len(ratios)
    return (multiple, agreement) if agreement >= 0.8 else (None, agreement)


def _divisible_by(values, divisor):
    """Share of rows that divide cleanly -- a batch column always does."""
    if divisor <= 1 or not values:
        return 0.0
    return sum(1 for v in values.values() if v % divisor == 0) / len(values)


# -------------------------------------------------------------- model assist

_PROMPT = """You are reading the quantity columns of an electronics bill of \
materials, for a tool that has to order parts for a build.

The sheet has %(rows)d data rows. These are its quantity columns, with the \
values they hold on a sample of rows spread across the whole file:

%(columns)s

%(ratios)s

Decide, for the build the sheet was written for:
- perUnitColumn: the index of the column holding the quantity for ONE unit \
(one board / one assembly). If every quantity column is a batch total and none \
is per-unit, use -1.
- unitsInFile: how many units the sheet's totals were written for. Use 1 if the \
sheet is per-unit only.

A column headed like "QTY Of 5 Units" is 5 units' worth, not one. A column that \
is an exact multiple of another column on every row is that column times the \
build size.

Reply with JSON only:
{"perUnitColumn": 0, "unitsInFile": 1, "note": "one short sentence"}"""


def _sample_rows(count, limit=24):
    """Row indexes spread across the whole file, not just the top of it."""
    if count <= limit:
        return list(range(count))
    step = count / float(limit)
    return sorted({int(i * step) for i in range(limit)})


def _ask_model(columns, values, row_count, ratio_notes):
    """Ask the local model to read the quantity columns. Returns (col, units)."""
    if not config.BOM_MODEL_ASSIST:
        return None, None
    from . import vision

    probe = vision._PROBE.get()
    if not probe.get("reachable"):
        return None, None
    model = vision.pick_model(probe) or (probe.get("models") or [None])[0]
    if not model:
        return None, None

    sample = _sample_rows(row_count)
    described = []
    for column in columns:
        series = values.get(column["index"], {})
        shown = [series.get(row) for row in sample if row in series][:20]
        described.append('  column %d, headed "%s": %s'
                         % (column["index"], column["name"],
                            json.dumps(shown, ensure_ascii=False)))

    prompt = _PROMPT % {
        "rows": row_count,
        "columns": "\n".join(described)[:3000],
        "ratios": ("What the arithmetic already shows:\n" + "\n".join(ratio_notes)
                   if ratio_notes else
                   "The columns are not exact multiples of one another."),
    }
    try:
        reply = vision._chat_text(model, prompt, num_predict=200)
    except Exception:
        return None, None
    parsed = vision._extract_json(reply)
    if not isinstance(parsed, dict):
        return None, None

    try:
        column = int(parsed.get("perUnitColumn", -1))
    except (TypeError, ValueError):
        column = -1
    try:
        units = int(parsed.get("unitsInFile", 1))
    except (TypeError, ValueError):
        units = 1
    known = {c["index"] for c in columns}
    return (column if column in known else None,
            units if 1 <= units <= MAX_UNITS else None)


# ------------------------------------------------------------------ analysis

def analyze(qty_columns, values, row_count):
    """Reduce a sheet's quantity columns to one per-unit column and a build size.

    `qty_columns` is [{"index": int, "name": str}]; `values` is
    {column index: {row index: quantity}}. Returns a dict describing what each
    column means, which one to read, and how many units the file was written
    for -- everything the caller needs to restate the BOM at any build size.
    """
    result = {
        "columns": [], "baseColumn": None, "unitsInFile": 1,
        "divisor": 1, "how": "single column", "note": "", "confident": True,
    }
    if not qty_columns:
        return result

    present = [c for c in qty_columns if values.get(c["index"])]
    if not present:
        return result

    # 1. Ratios. Every pair, so a sheet with three columns (per board, per 5,
    #    per 25) resolves as well as one with two.
    scaled_by = {}                      # column -> (base column, multiple)
    ratio_notes = []
    for base in present:
        for other in present:
            if base["index"] == other["index"]:
                continue
            multiple, agreement = _ratio_between(values[base["index"]],
                                                 values[other["index"]])
            if multiple:
                previous = scaled_by.get(other["index"])
                if previous is None or multiple > previous[1]:
                    scaled_by[other["index"]] = (base["index"], multiple)
                ratio_notes.append(
                    "  column %d is exactly column %d x %d on %d%% of rows"
                    % (other["index"], base["index"], multiple, round(agreement * 100)))

    for column in present:
        index = column["index"]
        header_units = units_in_header(column["name"])
        scaled = scaled_by.get(index)
        entry = {
            "index": index,
            "name": column["name"],
            "role": "per-unit",
            "units": 1,
            "why": "",
        }
        if scaled:
            entry["role"] = "batch"
            entry["units"] = scaled[1]
            entry["why"] = ("exactly %d x column %d across the sheet"
                            % (scaled[1], scaled[0]))
            # The header agreeing is worth saying; the header disagreeing is
            # worth saying louder, because the arithmetic still wins.
            if header_units and header_units != scaled[1]:
                entry["why"] += (" -- the heading says %d, the numbers say %d"
                                 % (header_units, scaled[1]))
        elif header_units:
            entry["role"] = "batch"
            entry["units"] = header_units
            entry["why"] = "the heading names %d units" % header_units
        elif _says(column["name"], _PER_UNIT_HEADER):
            entry["why"] = "the heading says per board"
        elif _says(column["name"], _TOTAL_HEADER):
            entry["role"] = "batch"
            entry["units"] = 0              # a total of an unknown build size
            entry["why"] = "the heading says total, but not of how many"
        result["columns"].append(entry)

    per_unit = [c for c in result["columns"] if c["role"] == "per-unit"]
    batches = [c for c in result["columns"] if c["role"] == "batch" and c["units"] > 1]

    if per_unit:
        # The per-unit column is read directly; nothing has to be divided.
        base = per_unit[0]
        result["baseColumn"] = base["index"]
        result["divisor"] = 1
        result["unitsInFile"] = min((c["units"] for c in batches), default=1)
        result["how"] = "ratios" if scaled_by else (
            "headers" if any(c["why"] for c in result["columns"]) else "single column")
        result["note"] = ("Column \"%s\" is one unit's worth%s."
                          % (base["name"],
                             "; %s is %d units" % (batches[0]["name"], batches[0]["units"])
                             if batches else ""))
        return result

    if batches:
        # Only batch columns: the smallest build size is the one to divide by,
        # and it has to divide cleanly or it is not what the column means.
        batch = min(batches, key=lambda c: c["units"])
        share = _divisible_by(values[batch["index"]], batch["units"])
        if share >= 0.9:
            result["baseColumn"] = batch["index"]
            result["divisor"] = batch["units"]
            result["unitsInFile"] = batch["units"]
            result["how"] = "headers"
            result["note"] = ("Column \"%s\" holds %d units' worth, so one unit "
                              "is that divided by %d."
                              % (batch["name"], batch["units"], batch["units"]))
            return result
        # It does not divide: the heading is describing something else. Fall
        # through and let the model look, rather than inventing fractions.
        result["note"] = ("Column \"%s\" says %d units but %d%% of its values do "
                          "not divide by %d."
                          % (batch["name"], batch["units"],
                             round((1 - share) * 100), batch["units"]))
        result["confident"] = False

    asked_column, asked_units = _ask_model(present, values, row_count, ratio_notes)
    if asked_column is not None:
        result["baseColumn"] = asked_column
        result["divisor"] = 1
        result["unitsInFile"] = asked_units or 1
        result["how"] = "local model"
        result["confident"] = False
        result["note"] = ("The local model read column %d as one unit's worth."
                          % asked_column)
        return result

    # Nothing established a build size. Read the first quantity column as it
    # stands: that is what the sheet literally says, and the note tells the
    # user what could not be worked out.
    result["baseColumn"] = present[0]["index"]
    result["divisor"] = 1
    result["unitsInFile"] = 1
    result["how"] = "as written"
    result["confident"] = False
    if not result["note"]:
        result["note"] = ("Column \"%s\" is being read as one unit's worth; "
                          "nothing in the file says otherwise." % present[0]["name"])
    return result

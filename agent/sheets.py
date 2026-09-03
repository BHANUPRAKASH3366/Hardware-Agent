"""Reading uploaded spreadsheets, using only the standard library.

An .xlsx file is a ZIP of XML parts, so `zipfile` plus `ElementTree` is all it
takes to read one -- no openpyxl. CSV goes through the `csv` module with the
delimiter sniffed, because a bill of materials exported in Europe is very often
semicolon-separated.

Cells keep their column position. That matters: a BOM with an empty column
between the part number and the quantity would otherwise silently shift every
value one place to the left.
"""
import csv
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_ROWS = 5000
MAX_COLS = 64


class SheetError(Exception):
    """An upload that cannot be read, phrased for the end user."""


def _local(tag):
    """Strip the XML namespace: '{...}row' -> 'row'."""
    return tag.rsplit("}", 1)[-1]


def _col_index(ref):
    """'C7' -> 2. Returns None when the reference is missing or malformed."""
    match = re.match(r"([A-Za-z]+)", ref or "")
    if not match:
        return None
    index = 0
    for ch in match.group(1).upper():
        index = index * 26 + (ord(ch) - 64)
    return index - 1


# --------------------------------------------------------------------- xlsx

def _shared_strings(zf):
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    out = []
    for node in ElementTree.fromstring(raw):
        if _local(node.tag) != "si":
            continue
        # A string can be split across several runs; concatenate every <t>.
        out.append("".join(t.text or "" for t in node.iter()
                           if _local(t.tag) == "t"))
    return out


def _first_sheet_path(zf):
    """The path of the first worksheet, following the workbook relationships."""
    names = zf.namelist()
    try:
        book = ElementTree.fromstring(zf.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ElementTree.ParseError):
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise SheetError("This workbook has no worksheets in it.")
        return sheets[0]

    targets = {}
    for node in rels:
        rel_id = node.attrib.get("Id")
        target = node.attrib.get("Target", "")
        if rel_id and target:
            targets[rel_id] = target.lstrip("/")

    for node in book.iter():
        if _local(node.tag) != "sheet":
            continue
        rel_id = next((v for k, v in node.attrib.items() if _local(k) == "id"), None)
        target = targets.get(rel_id)
        if not target:
            continue
        for candidate in (target, "xl/" + target, target.replace("../", "")):
            if candidate in names:
                return candidate

    sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
    if not sheets:
        raise SheetError("This workbook has no worksheets in it.")
    return sheets[0]


def read_xlsx(data):
    """[[cell, cell, ...], ...] for the first worksheet."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SheetError("This file is not a readable .xlsx workbook.")

    with zf:
        strings = _shared_strings(zf)
        try:
            sheet_xml = zf.read(_first_sheet_path(zf))
        except KeyError:
            raise SheetError("This workbook's first sheet could not be opened.")

        try:
            root = ElementTree.fromstring(sheet_xml)
        except ElementTree.ParseError as exc:
            raise SheetError("This workbook's XML is damaged (%s)." % exc)

    rows = []
    for row_node in root.iter():
        if _local(row_node.tag) != "row":
            continue
        row = []
        for cell in row_node:
            if _local(cell.tag) != "c":
                continue
            index = _col_index(cell.attrib.get("r"))
            if index is None:
                index = len(row)
            if index >= MAX_COLS:
                continue
            while len(row) < index:
                row.append("")

            kind = cell.attrib.get("t", "n")
            value = ""
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter()
                                if _local(t.tag) == "t")
            else:
                node = next((c for c in cell if _local(c.tag) == "v"), None)
                text = (node.text or "") if node is not None else ""
                if kind == "s":
                    try:
                        value = strings[int(text)]
                    except (ValueError, IndexError):
                        value = ""
                elif kind == "b":
                    value = "TRUE" if text == "1" else "FALSE"
                else:
                    value = text
            row.append((value or "").strip())
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break

    return rows


# ---------------------------------------------------------------------- csv

def _decode(data):
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


def read_csv(data):
    text = _decode(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        # Sniffing fails on a single-column file; count separators instead.
        counts = {d: sample.count(d) for d in ",;\t|"}
        delimiter = max(counts, key=counts.get) if max(counts.values()) else ","

    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows.append([(cell or "").strip() for cell in row[:MAX_COLS]])
        if len(rows) >= MAX_ROWS:
            break
    return rows


# -------------------------------------------------------------------- entry

def read(data, filename=""):
    """Read an uploaded spreadsheet into rows of text. Raises SheetError."""
    name = (filename or "").lower()
    if data[:4] == b"PK\x03\x04":
        return read_xlsx(data)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise SheetError(
            "This is an old .xls workbook. Open it in Excel and save it as .xlsx "
            "or .csv, then upload that.")
    if name.endswith((".csv", ".txt", ".tsv")) or b"," in data[:4096] or b";" in data[:4096]:
        return read_csv(data)
    raise SheetError("This file is not a spreadsheet the agent can read. Use .xlsx or .csv.")

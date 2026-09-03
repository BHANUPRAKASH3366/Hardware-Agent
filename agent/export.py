"""Spreadsheet and PDF writers, built on the standard library alone.

An .xlsx file is a ZIP of XML documents, and a PDF is a small object graph with
a text stream inside it. Both are written here directly rather than pulling in
openpyxl and reportlab, which keeps the project's "clone it and run it" promise
intact -- there is still nothing to install.

Scope is deliberately narrow: one sheet of tabular data with a header row, and
a paginated landscape table. That is exactly what a sourcing result is, and
nothing more is worth hand-rolling.
"""
import datetime
import io
import re
import struct
import zipfile
import zlib

# --------------------------------------------------------------------------- #
# Shared
# --------------------------------------------------------------------------- #


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float):
        # Trim the float noise that turns 12.83 into 12.830000000000002.
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return str(value)


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #

# Excel rejects a file outright if it carries characters XML forbids, so they
# are stripped rather than escaped -- there is no valid encoding for them.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_escape(text):
    text = _ILLEGAL_XML.sub("", str(text))
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def _col_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# Two cell formats: 0 is plain, 1 is the bold header with a fill and a border.
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF14705A"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _workbook_xml(sheet_name):
    return ("""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets>
</workbook>""" % _xml_escape(sheet_name[:31] or "Sheet1"))


def _sheet_xml(headers, rows, widths):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']

    if widths:
        out.append("<cols>")
        for i, width in enumerate(widths):
            out.append('<col min="%d" max="%d" width="%.1f" customWidth="1"/>'
                       % (i + 1, i + 1, width))
        out.append("</cols>")

    out.append("<sheetData>")

    if headers:
        out.append('<row r="1" ht="20" customHeight="1">')
        for i, label in enumerate(headers):
            out.append('<c r="%s1" s="1" t="inlineStr"><is><t>%s</t></is></c>'
                       % (_col_letter(i), _xml_escape(label)))
        out.append("</row>")

    start = 2 if headers else 1
    for r, row in enumerate(rows):
        out.append('<row r="%d">' % (r + start))
        for c, value in enumerate(row):
            ref = "%s%d" % (_col_letter(c), r + start)
            if _is_number(value):
                # Whole numbers are written without a decimal part so a
                # quantity column reads as "200", not "200.0".
                number = ("%d" % value) if float(value).is_integer() else repr(float(value))
                out.append('<c r="%s"><v>%s</v></c>' % (ref, number))
            else:
                text = _text(value)
                if not text:
                    continue        # an empty cell is better left out entirely
                out.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                           % (ref, _xml_escape(text)))
        out.append("</row>")

    out.append("</sheetData>")
    if headers:
        # Freeze the header so a long BOM stays readable while scrolling, and
        # switch on autofilter so columns can be sorted in Excel itself.
        last = "%s%d" % (_col_letter(len(headers) - 1), len(rows) + 1)
        out.append('<autoFilter ref="A1:%s"/>' % last)
    out.append("</worksheet>")

    # sheetView has to precede sheetData in the schema, so it is spliced in.
    pane = ('<sheetViews><sheetView workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>') if headers else ""
    body = "".join(out)
    return body.replace("<sheetData>", pane + "<sheetData>", 1)


def _auto_widths(headers, rows, cap=52):
    widths = [len(str(h)) + 4 for h in headers] if headers else []
    for row in rows[:400]:              # sampling is enough to size a column
        for i, value in enumerate(row):
            if i >= len(widths):
                widths.extend([10] * (i - len(widths) + 1))
            widths[i] = max(widths[i], min(len(_text(value)) + 2, cap))
    return [max(8, min(w, cap)) for w in widths]


def write_xlsx(headers, rows, sheet_name="Results"):
    """Return the bytes of a single-sheet .xlsx workbook."""
    buf = io.BytesIO()
    widths = _auto_widths(headers, rows)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(headers, rows, widths))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

# Character widths for the two base-14 fonts used, in 1/1000 em, for ASCII
# 32..126. Needed to fit and right-align columns; without them every number
# column drifts.
_W_REG = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 "
    "556 556 556 556 556 556 556 556 278 278 584 584 584 556 1015 667 667 722 "
    "722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 611 722 "
    "667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 "
    "556 222 222 500 222 833 556 556 556 556 333 500 278 556 500 722 500 500 "
    "500 334 260 334 584")
_W_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 "
    "556 556 556 556 556 556 556 556 333 333 584 584 584 611 975 722 722 722 "
    "722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 611 722 "
    "667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 "
    "611 278 278 556 278 889 611 611 611 611 389 556 333 611 556 778 556 556 "
    "500 389 280 389 584")
_WIDTHS = {
    False: [int(n) for n in _W_REG.split()],
    True: [int(n) for n in _W_BOLD.split()],
}


def text_width(text, size, bold=False):
    """Rendered width of a string in points, for fitting and alignment."""
    table = _WIDTHS[bool(bold)]
    total = 0
    for ch in text:
        code = ord(ch)
        total += table[code - 32] if 32 <= code <= 126 else 556
    return total * size / 1000.0


def _fit(text, width, size, bold=False):
    """Truncate to fit, with an ellipsis, so a long description never overruns."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if text_width(text, size, bold) <= width:
        return text
    ell = "..."
    budget = width - text_width(ell, size, bold)
    out = ""
    for ch in text:
        if text_width(out + ch, size, bold) > budget:
            break
        out += ch
    return (out.rstrip() + ell) if out else ""


# Symbols an electronics description reaches for that WinAnsi has no room for.
_TRANSLITERATE = {
    "Ω": "ohm", "Ω": "ohm", "μ": "u", "℃": "C", "℉": "F",
    "≤": "<=", "≥": ">=", "≈": "~", "→": "->", "×": "x", "·": "-",
    "…": "...", "™": "(TM)", "€": "EUR ", "₹": "INR ", "元": "CNY ",
}


def _pdf_escape(text):
    # WinAnsi is a single-byte encoding; anything outside it is transliterated
    # rather than silently producing mojibake in the output.
    out = []
    for ch in str(text):
        if ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= ord(ch) <= 126:
            out.append(ch)
        elif ch in "–—":
            out.append("-")
        elif ch in "‘’":
            out.append("'")
        elif ch in "“”":
            out.append('"')
        elif ch in _TRANSLITERATE:
            # The base-14 fonts only cover WinAnsi, and component descriptions
            # are full of symbols that sit outside it. Spelling them out beats
            # a row of question marks.
            out.append(_TRANSLITERATE[ch])
        elif ord(ch) < 256:
            out.append("\\%03o" % ord(ch))
        else:
            out.append("?")
    return "".join(out)


class _Pdf:
    """A very small PDF writer: pages of positioned text and thin rules."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.pages = []
        self.current = []

    def new_page(self):
        if self.current:
            self.pages.append(self.current)
        self.current = []

    def text(self, x, y, value, size=9, bold=False, gray=0.0):
        value = _pdf_escape(value)
        if not value:
            return
        self.current.append(
            "BT /%s %.1f Tf %.3f g %.2f %.2f Td (%s) Tj ET"
            % ("FB" if bold else "FR", size, gray, x, self.height - y, value))

    def text_right(self, x_right, y, value, size=9, bold=False, gray=0.0):
        width = text_width(str(value), size, bold)
        self.text(x_right - width, y, value, size, bold, gray)

    def rule(self, x1, y, x2, gray=0.75, thickness=0.5):
        self.current.append(
            "%.3f G %.2f w %.2f %.2f m %.2f %.2f l S"
            % (gray, thickness, x1, self.height - y, x2, self.height - y))

    def band(self, x, y, width, height, gray=0.93):
        self.current.append(
            "%.3f g %.2f %.2f %.2f %.2f re f" % (gray, x, self.height - y - height,
                                                 width, height))

    def build(self):
        if self.current:
            self.pages.append(self.current)
        if not self.pages:
            self.pages = [[]]

        objects = []                    # 1-indexed on output

        def add(body):
            objects.append(body)
            return len(objects)

        # Reserve 1 for the catalogue and 2 for the page tree so the page
        # objects can point back at a number that is already known.
        objects.extend([None, None])
        font_r = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                     "/Encoding /WinAnsiEncoding >>")
        font_b = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                     "/Encoding /WinAnsiEncoding >>")

        page_ids = []
        for ops in self.pages:
            stream = "\n".join(ops).encode("latin-1", "replace")
            packed = zlib.compress(stream)
            content_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n"
                             % len(packed) + packed + b"\nendstream")
            page_ids.append(add(
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                "/Resources << /Font << /FR %d 0 R /FB %d 0 R >> >> "
                "/Contents %d 0 R >>"
                % (self.width, self.height, font_r, font_b, content_id)))

        objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
        objects[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>"
                      % (" ".join("%d 0 R" % pid for pid in page_ids), len(page_ids)))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i
            out += body if isinstance(body, (bytes, bytearray)) else body.encode("latin-1")
            out += b"\nendobj\n"

        xref_at = len(out)
        out += b"xref\n0 %d\n" % (len(objects) + 1)
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += b"%010d 00000 n \n" % off
        out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objects) + 1, xref_at))
        return bytes(out)


A4_LANDSCAPE = (841.89, 595.28)


def write_pdf(headers, rows, title="Results", subtitle="", aligns=None,
              page=A4_LANDSCAPE):
    """Render a table to a paginated PDF and return its bytes.

    `aligns` is an optional list of "l" / "r" per column; numbers read far
    better right-aligned, and the column widths are proportional to the widest
    content each column actually carries.
    """
    width, height = page
    margin = 30.0
    doc = _Pdf(width, height)

    headers = list(headers or [])
    aligns = list(aligns or ["l"] * len(headers))
    aligns += ["l"] * (len(headers) - len(aligns))

    # Size columns from the content, then scale to the usable width. Sampling
    # the first 300 rows keeps a 2,000-row export from taking noticeable time.
    usable = width - 2 * margin
    natural = []
    for i, label in enumerate(headers):
        widest = text_width(str(label), 8, True)
        for row in rows[:300]:
            if i < len(row):
                widest = max(widest, text_width(_text(row[i])[:60], 8))
        natural.append(max(28.0, widest + 12))
    total = sum(natural) or 1
    cols = [w * usable / total for w in natural]
    xs, run = [], margin
    for w in cols:
        xs.append(run)
        run += w

    row_h, header_h = 15.0, 19.0
    top = margin + 46
    bottom = height - margin - 16

    stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    page_no = [0]

    def start_page():
        page_no[0] += 1
        if page_no[0] > 1:
            doc.new_page()
        doc.text(margin, margin + 14, title, size=15, bold=True, gray=0.1)
        if subtitle:
            doc.text(margin, margin + 30, subtitle, size=9, gray=0.4)
        doc.text_right(width - margin, margin + 14, stamp, size=8, gray=0.5)

        doc.band(margin, top - 13, usable, header_h, gray=0.90)
        for i, label in enumerate(headers):
            if aligns[i] == "r":
                doc.text_right(xs[i] + cols[i] - 6, top, _fit(label, cols[i] - 12, 8, True),
                               size=8, bold=True, gray=0.1)
            else:
                doc.text(xs[i] + 6, top, _fit(label, cols[i] - 12, 8, True),
                         size=8, bold=True, gray=0.1)
        doc.rule(margin, top + 6, width - margin, gray=0.55)
        return top + 6 + row_h

    y = start_page()
    for index, row in enumerate(rows):
        if y > bottom:
            doc.text(margin, height - margin + 4, "Page %d" % page_no[0], size=8, gray=0.55)
            y = start_page()
        if index % 2 == 1:
            doc.band(margin, y - 11, usable, row_h, gray=0.965)
        for i in range(len(headers)):
            value = _text(row[i]) if i < len(row) else ""
            if not value:
                continue
            if aligns[i] == "r":
                doc.text_right(xs[i] + cols[i] - 6, y, _fit(value, cols[i] - 12, 8),
                               size=8, gray=0.15)
            else:
                doc.text(xs[i] + 6, y, _fit(value, cols[i] - 12, 8), size=8, gray=0.15)
        y += row_h

    doc.text(margin, height - margin + 4, "Page %d" % page_no[0], size=8, gray=0.55)
    return doc.build()

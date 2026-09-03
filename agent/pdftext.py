"""Text extraction from PDF files, using only the standard library.

A PDF is a graph of numbered objects; the words live inside compressed content
streams as text-showing operators with their own coordinate system. This module
walks that graph far enough to pull the text back out with its positions, then
regroups the fragments into lines -- which is what a bill of materials needs,
because a BOM in a PDF is a table and the columns only survive if the geometry
does.

What it handles: FlateDecode streams, simple and hex strings, the four
text-showing operators, positioned text, and CID fonts through their ToUnicode
map. What it does not handle: encrypted files, and scanned pages, which carry
no text layer at all -- those are detected and reported rather than returned as
gibberish.
"""
import re
import zlib

from .export import text_width

# Objects look like "12 0 obj ... endobj". Non-greedy so nested objects do not
# swallow their neighbours.
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_STREAM = re.compile(rb"stream\r?\n?(.*?)\r?\n?endstream", re.S)


class PdfError(Exception):
    """A PDF that cannot be read, phrased for the end user."""


# --------------------------------------------------------------- object graph

def _objects(data):
    """{object number: raw body bytes}."""
    out = {}
    for match in _OBJ.finditer(data):
        out[int(match.group(1))] = match.group(3)
    return out


def _head(body):
    """The dictionary part of an object, before any stream payload."""
    cut = body.find(b"stream")
    return (body[:cut] if cut != -1 else body).decode("latin-1", "replace")


def _value(text, key):
    """Read the value that follows /key in a dictionary string."""
    match = re.search(r"/%s\b" % re.escape(key), text)
    if not match:
        return None
    rest = text[match.end():].lstrip()
    if not rest:
        return None
    if rest.startswith("<<"):
        depth, i = 0, 0
        while i < len(rest) - 1:
            if rest[i:i + 2] == "<<":
                depth += 1
                i += 2
                continue
            if rest[i:i + 2] == ">>":
                depth -= 1
                i += 2
                if depth == 0:
                    return rest[:i]
                continue
            i += 1
        return rest
    if rest.startswith("["):
        end = rest.find("]")
        return rest[:end + 1] if end != -1 else rest
    token = re.match(r"(\d+)\s+(\d+)\s+R\b", rest)
    if token:
        return "%s R" % token.group(1)
    token = re.match(r"/?[^\s/\[\]<>()]+", rest)
    return token.group(0) if token else None


def _ref(value):
    """Object number from a '12 R' value, else None."""
    if not value:
        return None
    match = re.match(r"(\d+)\s+R$", value.strip())
    return int(match.group(1)) if match else None


def _refs(value):
    """Every object number inside a value, whether scalar or array.

    Accepts both the raw "12 0 R" form and the "12 R" form that _value()
    normalises a single reference down to.
    """
    if not value:
        return []
    return [int(n) for n in re.findall(r"(\d+)\s+(?:\d+\s+)?R\b", value)]


def _stream_bytes(body, head):
    match = _STREAM.search(body)
    if not match:
        return None
    raw = match.group(1)
    filters = _value(head, "Filter") or ""
    if "FlateDecode" in filters:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                # Some writers leave a stray leading byte or omit the checksum.
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    if "ASCIIHexDecode" in filters:
        hexed = re.sub(rb"[^0-9A-Fa-f]", b"", raw.split(b">")[0])
        if len(hexed) % 2:
            hexed += b"0"
        try:
            return bytes.fromhex(hexed.decode("ascii"))
        except ValueError:
            return None
    if filters and "FlateDecode" not in filters:
        return None                     # LZW, JBIG2, DCT: not text anyway
    return raw


# ------------------------------------------------------------------- ToUnicode

_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_HEXTOK = re.compile(rb"<([0-9A-Fa-f]+)>")


def _utf16_of(hex_text):
    """Decode a ToUnicode target, which is UTF-16BE, possibly several chars."""
    try:
        raw = bytes.fromhex(hex_text.decode("ascii"))
    except ValueError:
        return ""
    if len(raw) % 2:
        raw += b"\x00"
    try:
        return raw.decode("utf-16-be", "ignore")
    except UnicodeDecodeError:
        return ""


def _parse_tounicode(stream):
    """{character code: text} from a ToUnicode CMap."""
    table = {}
    if not stream:
        return table

    for block in _BFCHAR.findall(stream):
        tokens = _HEXTOK.findall(block)
        for i in range(0, len(tokens) - 1, 2):
            try:
                table[int(tokens[i], 16)] = _utf16_of(tokens[i + 1])
            except ValueError:
                continue

    for block in _BFRANGE.findall(stream):
        # Two forms: "<lo> <hi> <dst>" and "<lo> <hi> [<d1> <d2> ...]".
        for entry in re.finditer(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            try:
                lo = int(entry.group(1), 16)
                hi = int(entry.group(2), 16)
            except ValueError:
                continue
            if hi < lo or hi - lo > 65535:
                continue
            target = entry.group(3)
            if target.startswith(b"["):
                for offset, tok in enumerate(_HEXTOK.findall(target)):
                    table[lo + offset] = _utf16_of(tok)
            else:
                start = _utf16_of(_HEXTOK.findall(target)[0]) if _HEXTOK.search(target) else ""
                if not start:
                    continue
                base = ord(start[-1])
                prefix = start[:-1]
                for offset in range(hi - lo + 1):
                    table[lo + offset] = prefix + chr(base + offset)
    return table


def _font_table(objects, font_ref):
    """(two_byte, {code: text}) for one font object."""
    body = objects.get(font_ref)
    if body is None:
        return False, {}
    head = _head(body)
    encoding = _value(head, "Encoding") or ""
    two_byte = "Identity" in encoding or "UCS2" in encoding or "UTF16" in encoding

    table = {}
    tu_ref = _ref(_value(head, "ToUnicode"))
    if tu_ref is not None and tu_ref in objects:
        tu_body = objects[tu_ref]
        table = _parse_tounicode(_stream_bytes(tu_body, _head(tu_body)))
        if table and max(table) > 255:
            two_byte = True

    # A Type0 font is always multi-byte in practice.
    if "Type0" in (_value(head, "Subtype") or ""):
        two_byte = True

    # Descendant fonts carry the real encoding for Type0.
    for desc in _refs(_value(head, "DescendantFonts") or ""):
        if desc in objects and not table:
            desc_head = _head(objects[desc])
            desc_tu = _ref(_value(desc_head, "ToUnicode"))
            if desc_tu is not None and desc_tu in objects:
                table = _parse_tounicode(
                    _stream_bytes(objects[desc_tu], _head(objects[desc_tu])))
    return two_byte, table


# ------------------------------------------------------------ string decoding

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
            "(": "(", ")": ")", "\\": "\\"}


def _raw_codes(token, two_byte):
    """A PDF string token to a list of character codes."""
    if token.startswith("<"):
        hexed = re.sub(r"[^0-9A-Fa-f]", "", token[1:-1])
        if len(hexed) % 2:
            hexed += "0"
        raw = bytes.fromhex(hexed) if hexed else b""
    else:
        out = bytearray()
        i, body = 0, token[1:-1]
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt in _ESCAPES:
                    out += _ESCAPES[nxt].encode("latin-1")
                    i += 2
                    continue
                octal = re.match(r"[0-7]{1,3}", body[i + 1:])
                if octal:
                    out.append(int(octal.group(0), 8) & 0xFF)
                    i += 1 + len(octal.group(0))
                    continue
                if nxt == "\n":         # line continuation
                    i += 2
                    continue
                out += nxt.encode("latin-1", "replace")
                i += 2
                continue
            out += ch.encode("latin-1", "replace")
            i += 1
        raw = bytes(out)

    if two_byte:
        if len(raw) % 2:
            raw += b"\x00"
        return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
    return list(raw)


def _decode(token, two_byte, table):
    codes = _raw_codes(token, two_byte)
    out = []
    for code in codes:
        if table:
            mapped = table.get(code)
            if mapped is not None:
                out.append(mapped)
                continue
            if two_byte:
                continue                # unmapped glyph id: nothing to show
        out.append(chr(code) if 9 <= code <= 0x2FFFF else "")
    return "".join(out)


# ---------------------------------------------------------- content scanning

# One pass over a content stream, in operator order.
_TOKEN = re.compile(rb"""
      (?P<str>\((?:\\.|[^()\\])*\)|<[0-9A-Fa-f\s]*>)   # a string
    | (?P<arr>\[(?:[^\[\]\\]|\\.)*\])                   # a TJ array
    | (?P<num>-?\d+\.?\d*)
    | (?P<name>/[^\s/\[\]<>()]+)
    | (?P<op>[A-Za-z'"*]+)
""", re.X | re.S)

_ARR_STR = re.compile(rb"\((?:\\.|[^()\\])*\)|<[0-9A-Fa-f\s]*>", re.S)


def _fragments(stream, fonts):
    """[(y, x, text)] for one content stream."""
    out = []
    stack = []                     # operands seen since the last operator
    x = y = 0.0
    line_x = line_y = 0.0
    leading = 0.0
    size = 10.0
    font = (False, {})

    for match in _TOKEN.finditer(stream):
        kind = match.lastgroup
        value = match.group(kind)
        if kind != "op":
            stack.append(value)
            continue

        op = value.decode("latin-1")

        if op == "BT":
            x = y = line_x = line_y = 0.0
        elif op == "Tf" and len(stack) >= 2:
            name = stack[-2].decode("latin-1")
            font = fonts.get(name, (False, {}))
            size = _f(stack[-1]) or size
        elif op == "TL" and stack:
            leading = _f(stack[-1])
        elif op == "Td" and len(stack) >= 2:
            line_x += _f(stack[-2])
            line_y += _f(stack[-1])
            x, y = line_x, line_y
        elif op == "TD" and len(stack) >= 2:
            leading = -_f(stack[-1])
            line_x += _f(stack[-2])
            line_y += _f(stack[-1])
            x, y = line_x, line_y
        elif op == "Tm" and len(stack) >= 6:
            line_x, line_y = _f(stack[-2]), _f(stack[-1])
            x, y = line_x, line_y
        elif op == "T*":
            line_y -= leading
            x, y = line_x, line_y
        elif op in ("Tj", "'", '"') and stack:
            if op != "Tj":
                line_y -= leading
                x, y = line_x, line_y
            text = _decode(stack[-1].decode("latin-1"), font[0], font[1])
            if text.strip():
                out.append((y, x, text, size))
        elif op == "TJ" and stack:
            pieces = []
            for tok in _ARR_STR.findall(stack[-1]):
                pieces.append(_decode(tok.decode("latin-1"), font[0], font[1]))
            text = "".join(pieces)
            if text.strip():
                out.append((y, x, text, size))

        stack = []
    return out


def _f(token):
    try:
        return float(token)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------- assembly

def _page_fonts(objects, resources_text):
    """{font name in the content stream: (two_byte, tounicode table)}."""
    fonts = {}
    font_dict = _value(resources_text or "", "Font") or ""
    if font_dict.strip().endswith("R") and not font_dict.startswith("<<"):
        ref = _ref(font_dict)
        if ref in objects:
            font_dict = _head(objects[ref])
    for name, num in re.findall(r"/([^\s/\[\]<>()]+)\s+(\d+)\s+\d+\s+R", font_dict or ""):
        fonts["/" + name] = _font_table(objects, int(num))
    return fonts


def _lines_from(fragments, y_tol=2.6, gap=2.0):
    """Group positioned fragments into visual lines, left to right.

    A gap wider than a space is a column boundary, not a word break, and it is
    marked with a double space. That separator is the only thing that lets a
    table inside a PDF be read back as columns instead of one run-on line, so
    the advance is measured with real glyph widths rather than a
    characters-times-a-guess estimate.
    """
    lines = []
    for y, x, text, size in sorted(fragments, key=lambda f: (-f[0], f[1])):
        if lines and abs(lines[-1]["y"] - y) <= y_tol:
            lines[-1]["parts"].append((x, text, size))
        else:
            lines.append({"y": y, "parts": [(x, text, size)]})

    out = []
    for line in lines:
        parts = sorted(line["parts"], key=lambda p: p[0])
        text = ""
        last_end = None
        for x, piece, size in parts:
            if last_end is not None and not text.endswith(" "):
                blank = x - last_end
                space = text_width(" ", size)
                if blank > max(gap, space * 1.4):
                    text += "  "
                elif blank > space * 0.4:
                    text += " "
            text += piece
            last_end = x + text_width(piece, size)
        text = re.sub(r"[ \t]{3,}", "  ", text).strip()
        if text:
            out.append(text)
    return out


def extract_pages(data):
    """[[line, line, ...], ...] -- text lines per page, in reading order."""
    if not data[:5].startswith(b"%PDF"):
        raise PdfError("That file is not a PDF.")
    if b"/Encrypt" in data[:2048] or re.search(rb"/Encrypt\s+\d+\s+\d+\s+R", data):
        raise PdfError("This PDF is password protected, so its text cannot be read.")

    objects = _objects(data)
    if not objects:
        raise PdfError("This PDF could not be parsed. Try re-saving or exporting it again.")

    pages = []
    for num in sorted(objects):
        head = _head(objects[num])
        if "/Type" not in head or "/Page" not in head:
            continue
        if not re.search(r"/Type\s*/Page\b", head):
            continue

        fonts = _page_fonts(objects, _value(head, "Resources"))
        fragments = []
        for content in _refs(_value(head, "Contents") or ""):
            body = objects.get(content)
            if body is None:
                continue
            stream = _stream_bytes(body, _head(body))
            if stream:
                fragments.extend(_fragments(stream, fonts))
        pages.append(_lines_from(fragments))

    if not pages:
        # No page objects resolved -- fall back to scanning every stream, which
        # still works on files whose page tree this parser cannot follow.
        fragments = []
        for num in sorted(objects):
            body = objects[num]
            stream = _stream_bytes(body, _head(body))
            if stream and b"BT" in stream and (b"Tj" in stream or b"TJ" in stream):
                fragments.extend(_fragments(stream, {}))
        if fragments:
            pages = [_lines_from(fragments)]

    if not any(pages):
        raise PdfError(
            "No text could be read from this PDF. If it is a scan or a photo of a "
            "document, export the bill of materials as Excel or CSV instead.")
    return pages


def extract_lines(data, max_lines=4000):
    """Every text line in the document, pages joined in order."""
    out = []
    for page in extract_pages(data):
        out.extend(page)
        if len(out) >= max_lines:
            break
    return out[:max_lines]



"""Photo identification through a locally installed Ollama vision model.

The user uploads a picture of a board or a loose part; a multimodal model
running on this machine reads the silkscreen markings and the package shape and
names the component. Nothing leaves the machine -- Ollama is reached over
localhost, so photos of unreleased hardware never touch a third party.

Ollama exposes the models it has pulled at /api/tags, and reports per-model
capabilities at /api/show. Only a model that reports "vision" can be handed an
image, so the picker filters on that rather than trusting the name.
"""
import base64
import binascii
import json
import re
import time

from . import config, net, taxonomy

# A vision model on a small GPU takes far longer than a distributor API call, so
# it gets its own budget rather than the shared REQUEST_TIMEOUT.
_TAGS_TIMEOUT = 4

# Older Ollama builds do not report capabilities. Fall back to the families that
# are multimodal by construction so those installs still work.
_KNOWN_VISION = re.compile(
    r"llava|bakllava|moondream|llama3\.2-vision|llama4|minicpm-v|"
    r"qwen2-vl|qwen2\.5vl|qwen3-vl|gemma3|mistral-small3|granite3\.\d-vision|"
    r"internvl|pixtral|glm-4v|cogvlm",
    re.I,
)

# Preference order when the user has not pinned OLLAMA_VISION_MODEL. Reading a
# part number off a chip is an OCR problem before it is a recognition problem,
# so the models that are strong at small text rank above the ones that are not.
_PREFERENCE = [
    r"qwen3-vl", r"qwen2\.5vl", r"qwen2-vl", r"internvl", r"minicpm-v",
    r"llama3\.2-vision", r"gemma3", r"mistral-small3", r"granite3\.\d-vision",
    r"llava-llama3", r"llava", r"bakllava", r"moondream",
]

_MAX_IMAGE_BYTES = 8 * 1024 * 1024   # decoded size; the UI downscales first
_MAX_CANDIDATES = 6

_PROMPT = """You are a component identification assistant for an electronics \
sourcing tool. Look at the photograph and identify the electronic hardware \
components visible in it.

Work like a technician with the board in hand:
1. Read every marking printed on the part and put ALL of it in "markings", \
top line first, exactly as printed -- part number line, date code, lot code, \
manufacturer logo text. Do not leave the top line out of "markings" just \
because you also report it as the part number. If nothing on the package is \
legible, leave "markings" empty rather than describing what you think ought \
to be printed there.
2. Identify the package from its shape and pin count (DIP-8, SOIC-16, TQFP-64, \
SOT-23, TO-220, 0805 chip, screw terminal, USB-C receptacle, and so on).
3. Only then say what the part is. Top-line marking codes are abbreviated on \
small packages -- if the marking is a code rather than a full part number, say \
so in "markings" and put your best full part number in "mpn".

Rules:
- List the most prominent component first. At most %(max)d entries.
- An OCR pass over this same photograph is quoted below. Those characters are \
the ground truth for what is printed. Your "mpn" must be one of those strings, \
or a standard expansion of one of them (a top-line marking code expanded to the \
full orderable part number). If the OCR found nothing, you have no part number \
-- say so by leaving "mpn" empty.
- "mpn" is the full manufacturer part number ONLY if you can actually read or \
confidently infer it. Otherwise leave it as an empty string. Never invent one.
- If the only thing printed on the part is a manufacturer logo, or the top is \
blank or unreadable, leave "mpn" empty, describe what you can see in "name" \
and "package", and set "confidence" below 0.4. A generic package render with \
no printed part number is exactly this case. Guessing a specific part number \
from the package shape alone is the one thing you must not do.
- "searchTerm" is what you would type into a distributor search box to find \
this part: the part number when you have it, otherwise a generic description \
such as "10k 0805 resistor" or "ESP32-WROOM module".
- "confidence" is 0.0 to 1.0 and must reflect real uncertainty. A blurry or \
unmarked part is low confidence, not a guess dressed up as a fact.
- If the photo has no electronic component in it, return an empty components \
list and explain in "summary".

Reply with JSON only, in exactly this shape:
{"components": [{"name": "", "type": "", "mpn": "", "manufacturer": "", \
"package": "", "markings": "", "searchTerm": "", \
"confidence": 0.0, "notes": ""}], "summary": ""}"""


# Pass one. Reading and identifying are different jobs, and a model asked to do
# both at once will let the identification steer the reading -- it decides the
# part is a TMS320 and then "reads" markings that agree. Transcription first,
# with identification explicitly forbidden, keeps the characters honest.
_OCR_PROMPT = """Transcribe the text printed on the electronic component in \
this photograph. You are an OCR engine, not an identification assistant.

Rules:
- Report characters exactly as printed, one entry per printed line, in the \
order they appear. Preserve case, spaces, dashes and dots.
- Include everything: part number lines, date codes, lot codes, logos rendered \
as text, pin-1 dots described as "dot".
- If a character is ambiguous, give your single best reading of it. Do not \
offer alternatives.
- Do NOT say what the component is. Do NOT expand an abbreviated code into a \
full part number. Do NOT add any text that is not physically printed in the \
photograph.
- If nothing is legible, return an empty list. An empty list is a perfectly \
good answer and is much better than a guess.

Reply with JSON only:
{"lines": ["", ""], "legible": true}"""


class VisionError(Exception):
    """Anything that stops an identification, phrased for the end user."""


# --------------------------------------------------------------- model probe

class _Probe:
    """Cached view of what Ollama currently has installed.

    Probing costs two round trips per model, and the answer only changes when
    the user pulls something, so it is held briefly rather than re-run on every
    page load.
    """

    def __init__(self):
        self.at = 0.0
        self.value = None

    def get(self, force=False):
        if not force and self.value is not None and (time.time() - self.at) < 30:
            return self.value
        self.value = self._probe()
        self.at = time.time()
        return self.value

    def _probe(self):
        base = config.OLLAMA_HOST
        try:
            tags = net.request_json("%s/api/tags" % base, timeout=_TAGS_TIMEOUT)
        except net.HttpError as exc:
            return {
                "reachable": False, "models": [], "vision": [],
                "reason": "Ollama is not answering at %s (%s)." % (base, exc),
            }

        names = [m.get("name") or m.get("model") or "" for m in (tags.get("models") or [])]
        names = [n for n in names if n]
        vision = [n for n in names if self._has_vision(base, n)]

        if not names:
            reason = "Ollama is running but has no models pulled yet."
        elif not vision:
            reason = ("Ollama is running, but none of the %d installed model%s can read "
                      "images." % (len(names), "" if len(names) == 1 else "s"))
        else:
            reason = "%d vision model%s available." % (
                len(vision), "" if len(vision) == 1 else "s")
        return {"reachable": True, "models": names, "vision": vision, "reason": reason}

    @staticmethod
    def _has_vision(base, name):
        try:
            info = net.request_json(
                "%s/api/show" % base, method="POST", body={"model": name},
                timeout=_TAGS_TIMEOUT,
            )
        except net.HttpError:
            # /api/show is missing or failed -- fall back to the name.
            return bool(_KNOWN_VISION.search(name))
        caps = info.get("capabilities")
        if isinstance(caps, list):
            return "vision" in [str(c).lower() for c in caps]
        # Pre-capability Ollama: a projector in the model families is the tell.
        families = (info.get("details") or {}).get("families") or []
        if any("clip" in str(f).lower() or "vision" in str(f).lower() for f in families):
            return True
        return bool(_KNOWN_VISION.search(name))


_PROBE = _Probe()


def _rank(name):
    for i, pattern in enumerate(_PREFERENCE):
        if re.search(pattern, name, re.I):
            return i
    return len(_PREFERENCE)


def pick_model(probe=None):
    """The vision model to use, or None when there is nothing usable."""
    probe = probe or _PROBE.get()
    vision = probe.get("vision") or []
    pinned = config.OLLAMA_VISION_MODEL
    if pinned:
        # Accept "llava" for an installed "llava:latest" so the setting can be
        # written the way the user types it at the ollama prompt.
        for name in vision:
            if name == pinned or name.split(":")[0] == pinned.split(":")[0]:
                return name
        # Pinned but not detected as multimodal: honour it anyway if it is
        # installed at all -- the user knows their model better than the probe.
        for name in probe.get("models") or []:
            if name == pinned or name.split(":")[0] == pinned.split(":")[0]:
                return name
        return None
    return sorted(vision, key=lambda n: (_rank(n), n))[0] if vision else None


def status(force=False):
    """Describe the local vision setup for /api/meta and the UI."""
    if not config.ENABLE_VISION:
        return {
            "enabled": False, "reachable": False, "model": None, "models": [],
            "installed": [], "host": config.OLLAMA_HOST,
            "reason": "Photo identification is switched off (ENABLE_VISION=false).",
            "suggest": [],
        }
    probe = _PROBE.get(force=force)
    model = pick_model(probe)
    reason = probe["reason"]
    if probe["reachable"] and config.OLLAMA_VISION_MODEL and not model:
        reason = ("OLLAMA_VISION_MODEL is set to %r, but that model is not installed. "
                  "Run: ollama pull %s"
                  % (config.OLLAMA_VISION_MODEL, config.OLLAMA_VISION_MODEL))
    return {
        "enabled": bool(model),
        "reachable": probe["reachable"],
        "model": model,
        "models": probe.get("vision") or [],
        "installed": probe.get("models") or [],
        "host": config.OLLAMA_HOST,
        "reason": reason,
        # Small enough for a 4 GB card, ordered best-OCR-first.
        "suggest": ["qwen2.5vl:3b", "moondream", "llava:7b"],
    }


def available():
    st = status()
    return bool(st["enabled"]), st["reason"]


# ----------------------------------------------------------------- inference

def _decode_image(data):
    """Accept a data: URL or bare base64 and return a clean base64 payload."""
    if not data or not isinstance(data, str):
        raise VisionError("No image was received.")
    if data.startswith("data:"):
        head, _, payload = data.partition(",")
        if not payload:
            raise VisionError("The uploaded image was empty.")
        if "image/" not in head:
            raise VisionError("That file is not an image.")
        data = payload
    data = re.sub(r"\s+", "", data)
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VisionError("The image could not be decoded (%s)." % exc) from exc
    if not raw:
        raise VisionError("The uploaded image was empty.")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise VisionError(
            "Image is %.1f MB after decoding; the limit is %d MB."
            % (len(raw) / 1048576.0, _MAX_IMAGE_BYTES // 1048576))
    if not _looks_like_image(raw):
        raise VisionError("That file does not look like a JPEG, PNG or WebP image.")
    return base64.b64encode(raw).decode("ascii")


def _looks_like_image(raw):
    return (raw[:3] == b"\xff\xd8\xff"                       # JPEG
            or raw[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
            or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
            or raw[:6] in (b"GIF87a", b"GIF89a")
            or raw[:2] == b"BM")


def _extract_json(text):
    """Pull the JSON object out of a model reply.

    format=json makes Ollama emit clean JSON, but a model can still wrap it in
    a fence or prepend a sentence, so the braces are located rather than
    assumed.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, depth = None, 0
    for i, ch in enumerate(text):
        if ch == "{":
            if start is None:
                start = i
            depth += 1
        elif ch == "}" and start is not None:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    start, depth = None, 0
    return None


# Models answer "unknown" instead of leaving a field blank; an empty string is
# what the UI wants, so those placeholders are stripped back out.
_PLACEHOLDERS = {
    "unknown", "n/a", "na", "none", "null", "-", "--", "not visible",
    "not legible", "unreadable", "illegible", "unclear", "not applicable",
    "no marking", "no markings", "unidentified", "string",
}


def _clean(value, limit=120):
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if text.lower() in _PLACEHOLDERS:
        return ""
    return text[:limit]


def _confidence(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1:                 # a model that answered in percent
        num /= 100.0
    return round(min(max(num, 0.0), 1.0), 2)


_MPN_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_./+]{2,39}$")


def _plausible_mpn(mpn):
    """Reject prose and bare package names that landed in the mpn field."""
    if not mpn or " " in mpn or not _MPN_SHAPE.match(mpn):
        return ""
    if not re.search(r"\d", mpn):        # every real MPN carries a digit
        return ""
    if re.fullmatch(r"(?i)(dip|soic|sop|sot|tqfp|lqfp|qfn|bga|to)[-_]?\d+", mpn):
        return ""                        # that is a package, not a part
    return mpn


def _alnum(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _marking_supports(mpn, markings):
    """Does the reported marking text actually back up the claimed part number?

    Top-line markings are abbreviated, so an exact match is too strict: the
    test is whether a run of the part number appears in what was read off the
    package (NE555 vs "NE555P", LM358 vs "LM358N 24AB").
    """
    a, b = _alnum(mpn), _alnum(markings)
    # Under four transcribed characters there is nothing to corroborate
    # anything with -- and a one-character marking like "T" would otherwise
    # "match" every part number that happens to start with it.
    if len(a) < 4 or len(b) < 4:
        return False
    if a in b or b in a:
        return True
    # The root of the part number before the package suffix is what is printed.
    root = a[:5] if len(a) > 5 else a
    return len(root) >= 4 and root in b


def _shape_candidate(raw):
    """Normalise one model-proposed component into the UI's shape."""
    if not isinstance(raw, dict):
        return None
    mpn = _plausible_mpn(_clean(raw.get("mpn") or raw.get("partNumber"), 40))
    name = _clean(raw.get("name") or raw.get("component") or raw.get("type"))
    kind = _clean(raw.get("type") or raw.get("category"))
    package = _clean(raw.get("package") or raw.get("footprint"), 60)
    markings = _clean(raw.get("markings") or raw.get("marking"), 80)
    manufacturer = _clean(raw.get("manufacturer") or raw.get("brand"), 60)
    search = _clean(raw.get("searchTerm") or raw.get("query"), 120)

    if not search:
        search = mpn or " ".join(p for p in (name, kind) if p)[:120]
    # A model that could not read the part sometimes answers with a fragment --
    # "T" off a logo. Sent to four distributors that returns pure noise, so a
    # usable term is rebuilt from whatever else was observed, and if there is
    # nothing to rebuild from the candidate is simply not searchable.
    if len(search) < 4 and not mpn:
        search = " ".join(p for p in (kind, name, package) if p and len(p) > 3)[:120]
    if len(search) < 4:
        search = ""
    if not (search or mpn or name):
        return None
    # Models routinely leave "mpn" blank and put the part number in
    # "searchTerm" instead. Treat that as the part number it plainly is.
    if not mpn and taxonomy.looks_like_part_number(search):
        mpn = _plausible_mpn(search)
    # A distributor keyword engine answers a bare part number far better than
    # a part number with the package bolted on ("LM358P SOP-8"), so once the
    # part number is known it is what gets searched.
    if mpn and search != mpn and mpn.lower() in search.lower():
        search = mpn
    if not name:
        name = mpn or kind or search

    # How much the photo itself backs up the claimed part number. Both signals
    # are read off the transcription rather than taken on trust: asking the
    # model whether it could see a part number produces an answer uncorrelated
    # with whether it could -- in testing it said no for a plainly marked chip
    # and yes for a blank package render it had hallucinated a part number for.
    #
    # A match is strong evidence and gets said out loud. Its absence is not
    # evidence of a guess -- models often transcribe only the date code -- so
    # that stays silent. What does warrant a warning is a specific part number
    # asserted over a package with essentially nothing transcribed off it,
    # which is the hallucination case exactly.
    marking_match = bool(mpn) and _marking_supports(mpn, markings)
    thin_markings = bool(mpn) and not marking_match and len(_alnum(markings)) < 4

    # Snap the guess onto the built-in catalogue where it lands on a real
    # category. That turns a loose phrase like "small signal npn transistor"
    # into the term the distributors actually index.
    node, crumb = taxonomy.match(search)
    category = None
    if node:
        category = {"id": node["id"], "name": node["name"], "breadcrumb": crumb}

    return {
        "name": name,
        "type": kind,
        "mpn": mpn,
        "manufacturer": manufacturer,
        "package": package,
        "markings": markings,
        "searchTerm": search,
        "confidence": _confidence(raw.get("confidence")),
        "notes": _clean(raw.get("notes") or raw.get("reason"), 240),
        "category": category,
        "markingMatch": marking_match,
        "thinMarkings": thin_markings,
        # A read part number is searchable as-is; a description is a starting
        # point the user may want to refine before spending distributor quota.
        "exact": bool(mpn),
    }


def _tuning_options():
    """VRAM-related knobs, only sent when the user has set them."""
    opts = {}
    if config.OLLAMA_NUM_GPU > 0:
        opts["num_gpu"] = config.OLLAMA_NUM_GPU
    if config.OLLAMA_NUM_CTX > 0:
        opts["num_ctx"] = config.OLLAMA_NUM_CTX
    return opts


def _chat(model, prompt, image_b64, num_predict=600):
    """One turn at the local model, with the photo attached."""
    try:
        reply = net.request_json(
            "%s/api/chat" % config.OLLAMA_HOST,
            method="POST",
            timeout=config.OLLAMA_TIMEOUT,
            body={
                "model": model,
                "stream": False,
                "format": "json",
                "keep_alive": config.OLLAMA_KEEP_ALIVE,
                "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
                "options": dict(_tuning_options(), **{
                    # Identification wants the most likely reading of the
                    # markings, not a creative one. Temperature 0 with a fixed
                    # seed also makes the same photo give the same answer, so a
                    # result the user disagrees with can actually be chased down.
                    "temperature": 0,
                    "top_p": 0.9,
                    "seed": 42,
                    "num_predict": num_predict,
                }),
            },
        )
    except net.HttpError as exc:
        message = str(exc)
        if "timed out" in message:
            raise VisionError(
                "%s took longer than %ss to answer. Vision models are slow on a "
                "small GPU -- raise OLLAMA_TIMEOUT in .env, or pull a smaller "
                "model such as moondream." % (model, config.OLLAMA_TIMEOUT)) from exc
        if ("forcibly closed" in message or "connection reset" in message.lower()
                or "EOF" in message or "out of memory" in message.lower()
                or "500" in message):
            raise VisionError(
                "%s could not be loaded -- Ollama ran out of memory. On a 4 GB "
                "card with little free RAM this model's image encoder (~1.2 GB in "
                "one block) does not fit. Close other apps to free system RAM, or "
                "switch to a lighter model:  ollama pull moondream  and set "
                "OLLAMA_VISION_MODEL=moondream in .env. Details: %s"
                % (model, message[:200])) from exc
        raise VisionError("Ollama rejected the request: %s" % message[:300]) from exc
    return (reply.get("message") or {}).get("content", "")


def _chat_text(model, prompt, num_predict=300):
    """One text-only turn at the local model.

    Used by the bill-of-materials reader, which has a table to interpret rather
    than a picture to look at -- so any installed model can serve, not just a
    vision one.
    """
    try:
        reply = net.request_json(
            "%s/api/chat" % config.OLLAMA_HOST,
            method="POST",
            timeout=config.OLLAMA_TIMEOUT,
            body={
                "model": model,
                "stream": False,
                "format": "json",
                "keep_alive": config.OLLAMA_KEEP_ALIVE,
                "messages": [{"role": "user", "content": prompt}],
                "options": dict(_tuning_options(), **{
                    "temperature": 0, "top_p": 0.9, "seed": 42,
                    "num_predict": num_predict}),
            },
        )
    except net.HttpError as exc:
        raise VisionError("Ollama rejected the request: %s" % str(exc)[:300]) from exc
    return (reply.get("message") or {}).get("content", "")


def _transcribe(model, image_b64):
    """Pass one: what is actually printed on the part, as characters."""
    parsed = _extract_json(_chat(model, _OCR_PROMPT, image_b64, num_predict=300))
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("lines")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    lines = []
    for entry in raw[:12]:
        text = _clean(entry, 60)
        if text and text.lower() not in [l.lower() for l in lines]:
            lines.append(text)
    return lines


# A printed token worth trying as a part number: long enough to be one, and
# carrying both letters and digits the way real part numbers do.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_./+]{3,39}")


def _printed_candidates(lines):
    """Part-number-shaped strings from the OCR, most promising first."""
    out = []
    for line in lines:
        for token in _TOKEN.findall(line):
            token = token.strip("-_./+")
            if (len(token) >= 4 and re.search(r"\d", token) and re.search(r"[A-Za-z]", token)
                    and _plausible_mpn(token) and token.upper() not in
                    [t.upper() for t in out]):
                out.append(token)
    # Longer strings carry more information and are far less likely to be a
    # date code that happens to look like a part number.
    out.sort(key=len, reverse=True)
    return out[:6]


def _lookup(term):
    """Ask the live distributors whether this part number exists.

    The whole point: a part number the model invented is not stocked anywhere,
    and a part number it read off the package usually is. `relevance` already
    drops near-misses from a part-number search, so anything that survives is
    genuinely the part asked for.
    """
    from . import engine, relevance   # imported here to keep the import graph acyclic

    # Only a specific part number can be verified this way. A descriptive term
    # matches thousands of unrelated parts, so "it returned rows" would prove
    # nothing at all.
    if not relevance.looks_like_part_number(term):
        return None
    try:
        payload = engine.search(term, quantity=1, limit=5)
    except Exception:
        # Verification is a bonus. A distributor being down must never turn
        # into a failed identification.
        return None

    want = relevance._key(term)
    suppliers, stock, exact = [], 0, None
    for row in payload.get("results") or []:
        got = relevance._key(row.get("mpn"))
        # The distributor's keyword engine answers a part number with its
        # variants too. A reel-suffix variant is a different orderable part at
        # a different price, so only the part actually asked for counts as
        # proof that the part actually asked for exists.
        if got != want:
            continue
        exact = row.get("mpn")
        label = row.get("sourceLabel")
        if label and label not in suppliers:
            suppliers.append(label)
        stock = max(stock, row.get("stock") or 0)
    if not exact:
        return None
    return {"mpn": exact, "suppliers": suppliers, "stock": stock}


def _verify(candidate, printed):
    """Ground one candidate against what the distributors actually stock.

    Tried in order of trustworthiness: the model's part number first, then the
    strings the OCR pass read off the package. A printed string that is stocked
    beats a model guess that is not -- that is the case where the model has
    quietly swapped in a different component, and it gets corrected rather than
    just flagged.
    """
    guess = candidate.get("mpn") or ""
    agreeing, other = [], []
    for term in printed:
        key, want = _alnum(term), _alnum(guess)
        # A printed string the model's answer agrees with is the best of both:
        # it is what the package actually says, and the model read it the same
        # way. It is tried before the model's own expansion, so a model that
        # inflates "LM358P" into the "LM358PWR" reel variant does not quietly
        # swap the part out from under a photo that plainly says otherwise.
        if want and key and (key in want or want in key):
            agreeing.append(term)
        else:
            other.append(term)

    attempts = []
    for term in agreeing + [guess] + other:
        # Only part-number-shaped strings are worth a distributor round trip;
        # a logo or a date code would just return whatever the keyword engine
        # felt like and prove nothing.
        if term and _plausible_mpn(term) and term.upper() not in [a.upper() for a in attempts]:
            attempts.append(term)

    for term in attempts[:4]:
        found = _lookup(term)
        if not found:
            continue
        candidate["verified"] = True
        candidate["verifiedBy"] = found["suppliers"]
        candidate["verifiedStock"] = found["stock"]
        if _alnum(found["mpn"]) != _alnum(guess):
            # What was searched is not what the model first said. Both halves
            # are reported: the user can see the model's reading and the string
            # that actually turned out to be orderable.
            candidate["correctedFrom"] = guess
            candidate["markingMatch"] = True
        candidate["mpn"] = found["mpn"]
        candidate["searchTerm"] = found["mpn"]
        candidate["exact"] = True
        candidate["thinMarkings"] = False
        return candidate

    candidate["verified"] = False
    candidate["verifiedBy"] = []
    return candidate


def _verify_all(candidates, printed):
    """Verify the leading candidates in parallel, leaving the rest untouched."""
    from concurrent.futures import ThreadPoolExecutor

    head = candidates[:3]
    if not head:
        return candidates
    with ThreadPoolExecutor(max_workers=len(head)) as pool:
        list(pool.map(lambda c: _verify(c, printed), head))
    for candidate in candidates[3:]:
        candidate.setdefault("verified", None)      # not checked, not disproved
        candidate.setdefault("verifiedBy", [])
    return candidates


def identify(image, note="", model=None):
    """Run one photo through the local vision model.

    Returns {"model", "ms", "summary", "candidates": [...]}. Raises VisionError
    with a message that is safe and useful to show the user.
    """
    if not config.ENABLE_VISION:
        raise VisionError("Photo identification is switched off (ENABLE_VISION=false).")

    payload_b64 = _decode_image(image)
    probe = _PROBE.get()
    if not probe["reachable"]:
        raise VisionError("%s Start Ollama, then try again." % probe["reason"])
    if model and model not in (probe.get("models") or []):
        raise VisionError("Model %r is not installed in Ollama." % model)
    chosen = model or pick_model(probe)
    if not chosen:
        raise VisionError(
            "No vision-capable model is installed in Ollama. Pull one first, "
            "for example:  ollama pull qwen2.5vl:3b")

    started = time.time()

    # Pass one: read the package.
    printed = _transcribe(chosen, payload_b64) if config.VISION_OCR_PASS else []

    # Pass two: identify, with the transcription quoted back as ground truth.
    prompt = _PROMPT % {"max": _MAX_CANDIDATES}
    if config.VISION_OCR_PASS:
        prompt += ("\n\nOCR pass over this photograph read exactly this: %s"
                   % (json.dumps(printed) if printed
                      else "nothing legible. There is no part number to report."))
    hint = _clean(note, 200)
    if hint:
        prompt += ("\n\nThe user adds this context about the photo: %r. Use it to "
                   "disambiguate, but do not let it override what you can see."
                   % hint)

    parsed = _extract_json(_chat(chosen, prompt, payload_b64))
    if parsed is None:
        raise VisionError(
            "%s replied, but not with usable JSON. Try again, or pick another "
            "model with OLLAMA_VISION_MODEL in .env." % chosen)

    raw_list = parsed.get("components")
    if isinstance(raw_list, dict):
        raw_list = [raw_list]
    if not isinstance(raw_list, list):
        raw_list = []

    candidates, seen = [], set()
    for entry in raw_list[: _MAX_CANDIDATES * 2]:
        shaped = _shape_candidate(entry)
        if not shaped:
            continue
        key = (shaped["mpn"] or shaped["searchTerm"]).lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(shaped)
        if len(candidates) >= _MAX_CANDIDATES:
            break

    # A part number nobody stocks is the signature of a hallucination, so the
    # distributors get the final say on the ordering.
    if config.VISION_VERIFY and candidates:
        candidates = _verify_all(candidates, printed)

    # Verified parts first, then ones with a part number at all, then by
    # confidence -- the model's own confidence is the weakest signal here and
    # only breaks ties.
    candidates.sort(key=lambda c: (
        c.get("verified") is not True,
        not c["exact"],
        -(c["confidence"] or 0),
    ))

    return {
        "model": chosen,
        "host": config.OLLAMA_HOST,
        "ms": int((time.time() - started) * 1000),
        "summary": _clean(parsed.get("summary"), 400),
        "note": hint,
        "printed": printed,
        "verified": bool(config.VISION_VERIFY),
        "candidates": candidates,
    }

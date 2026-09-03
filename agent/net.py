"""Tiny HTTP client built on urllib so the project stays dependency-free."""
import gzip
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 HardwareAgent/1.0"
)

_SSL_CTX = ssl.create_default_context()


class HttpError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# The sentence a human should see, dug out of whatever shape the API returned.
# Distributors answer errors with RFC 7807 bodies, bare {"message": ...} objects
# or HTML; pasting the raw payload into a toast shows the user a wall of JSON
# with the one useful sentence buried in the middle of it.
_DETAIL_KEYS = ("detail", "message", "error_description", "title",
                "ErrorMessage", "errorMessage", "Message")


def _error_detail(text, limit=200):
    text = (text or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        # Some APIs nest the real error one level down.
        for container in (parsed, parsed.get("error"), parsed.get("Error")):
            if not isinstance(container, dict):
                continue
            for key in _DETAIL_KEYS:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:limit]
    # Not JSON, or JSON with nothing quotable in it: collapse the whitespace so
    # it reads as one line rather than a pasted document.
    return " ".join(text.split())[:limit]


def request(url, method="GET", headers=None, body=None, timeout=None, form=None,
            headers_out=None):
    """Perform an HTTP request and return the decoded text body.

    `body` is sent as JSON; `form` is sent url-encoded. Raises HttpError with a
    human-readable message so providers can surface a useful status instead of a
    stack trace.

    Pass a dict as `headers_out` to receive the response headers. Some APIs
    report the caller's remaining quota there and nowhere else, and that figure
    is worth more than any local estimate.
    """
    timeout = timeout or config.REQUEST_TIMEOUT
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)

    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            if headers_out is not None:
                headers_out.update({k.lower(): v for k, v in resp.headers.items()})
            payload = resp.read()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                payload = gzip.decompress(payload)
            return payload.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            detail = _error_detail(raw.decode("utf-8", errors="replace"))
        except Exception:
            pass
        raise HttpError(
            "HTTP %s%s" % (exc.code, (": " + detail) if detail else ""), exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise HttpError("network unreachable (%s)" % (exc.reason,)) from exc
    except TimeoutError as exc:
        raise HttpError("request timed out after %ss" % timeout) from exc
    except ssl.SSLError as exc:
        raise HttpError("TLS error (%s)" % (exc,)) from exc
    except OSError as exc:
        raise HttpError("connection failed (%s)" % (exc,)) from exc


def request_json(url, **kwargs):
    text = request(url, **kwargs)
    try:
        return json.loads(text)
    except ValueError as exc:
        snippet = text[:160].replace("\n", " ")
        raise HttpError("upstream returned non-JSON response: %s" % snippet) from exc

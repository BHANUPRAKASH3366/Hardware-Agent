#!/usr/bin/env python3
"""Hardware Agent -- component sourcing search across distributor APIs.

Dependency-free HTTP server: `python app.py` and open the printed URL.
"""
import base64
import binascii
import csv
import datetime
import html
import io
import json
import re
import time
import mimetypes
import os
import posixpath
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent import (auth, bom, config, engine, export, fx, providers, taxonomy,
                   usage, vision)
from agent.cache import SEARCH_CACHE

# A downscaled photo arrives as base64 inside JSON, so the body cap has to
# allow for the ~33% base64 overhead on top of the decoded image limit.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

CSV_COLUMNS = [
    ("mpn", "Component"),
    ("manufacturer", "Manufacturer"),
    ("description", "Description"),
    ("sourceLabel", "Supplier"),
    ("sku", "Supplier SKU"),
    ("stock", "Available stock"),
    ("requiredQty", "Required stock"),
    # Lowest minimum across the part's packaging options at that supplier,
    # then the minimum on the SKU this row is priced from.
    ("packagingMoq", "Min"),
    ("moq", "Min (priced SKU)"),
    ("unitPriceDisplay", "Unit price"),
    ("extendedPriceDisplay", "Extended price"),
    ("displayCurrency", "Currency"),
    ("package", "Package"),
    ("leadTime", "Lead time"),
    ("url", "Product link"),
]


# Served in place of any page when a password is set and the visitor has not
# entered it. Self-contained on purpose: every static file is behind the same
# gate, so this cannot pull in the app stylesheet.
_LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Hardware Agent</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #f6f7f9; color: #1b1f24; padding: 24px; }
  .card { width: 100%; max-width: 360px; background: #fff; padding: 28px;
          border: 1px solid #e3e6ea; border-radius: 12px;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  h1 { margin: 0 0 4px; font-size: 19px; }
  p { margin: 0 0 20px; font-size: 13px; color: #6b7280; }
  label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 6px; }
  input { width: 100%; padding: 10px 12px; font-size: 15px; border-radius: 8px;
          border: 1px solid #cbd2d9; background: #fff; color: inherit; }
  input:focus { outline: 2px solid #2563eb; outline-offset: 1px; border-color: #2563eb; }
  button { width: 100%; margin-top: 14px; padding: 10px; font-size: 15px;
           font-weight: 600; color: #fff; background: #2563eb; border: 0;
           border-radius: 8px; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  .err { margin: 0 0 14px; padding: 9px 12px; font-size: 13px; border-radius: 8px;
         background: #fdecec; color: #b42318; border: 1px solid #f5c2c0; }
  @media (prefers-color-scheme: dark) {
    body { background: #14161a; color: #e6e8eb; }
    .card { background: #1c1f24; border-color: #2b3038; }
    p { color: #9aa3ad; }
    input { background: #14161a; border-color: #363c45; }
    .err { background: #3a1d1d; color: #f5a3a0; border-color: #5c2b2b; }
  }
</style></head>
<body>
  <form class="card" method="POST" action="/login">
    <h1>Hardware Agent</h1>
    <p>Enter the shared password to continue.</p>
    __ERROR__
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
           autofocus required>
    <button type="submit">Sign in</button>
  </form>
</body></html>"""


def _login_page(error=""):
    block = ('<p class="err">%s</p>' % html.escape(error)) if error else ""
    return _LOGIN_HTML.replace("__ERROR__", block)


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int(value, default):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "HardwareAgent/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- output

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status, payload, extra=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status, message, hint=None):
        self._json(status, {"error": {"status": status, "message": message, "hint": hint}})

    def log_message(self, fmt, *args):
        if os.environ.get("HW_AGENT_QUIET"):
            return
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- params

    def _params(self, query):
        raw = urllib.parse.parse_qs(query, keep_blank_values=True)
        get = lambda k, d="": (raw.get(k, [d])[0] or "").strip()
        sources = [s for s in get("sources").split(",") if s.strip()]
        return {
            "query": get("q"),
            "quantity": max(1, _int(get("qty"), 1)),
            "currency": (get("currency") or config.DEFAULT_CURRENCY).upper(),
            "sources": sources,
            "sort": get("sort") or engine.DEFAULT_SORT,
            "in_stock_only": _bool(get("inStock")),
            "limit": max(1, min(_int(get("limit"), config.MAX_RESULTS_PER_PROVIDER), 50)),
            "no_cache": _bool(get("fresh")),
        }

    # ---------------------------------------------------------------- routes

    def do_HEAD(self):
        self.do_GET()

    def _is_secure(self):
        """Is the browser talking to us over HTTPS?

        Behind a tunnel or a platform router the TLS ends there, so the request
        reaches this process as plain HTTP; the proxy records the original
        scheme in a header. Getting this right decides whether the session
        cookie is marked Secure.
        """
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        return proto.lower() == "https"

    def _gate(self, route):
        """True when the request may proceed. Otherwise it has been answered."""
        if not auth.enabled() or route in ("/login", "/api/health"):
            return True
        if auth.authorised(self.headers):
            return True
        # An API caller wants a status it can act on; a browser wants the page.
        if route.startswith("/api/"):
            self._error(401, "Not signed in.", "Open the app and enter the password.")
        else:
            self._send(200, _login_page(), "text/html; charset=utf-8",
                       {"Cache-Control": "no-store"})
        return False

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path.rstrip("/") or "/"
        try:
            if not self._gate(route):
                return
            if route == "/logout":
                return self._send(302, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": auth.clearing_cookie(),
                    "Cache-Control": "no-store"})
            if route == "/api/health":
                return self._json(200, {
                    "status": "ok",
                    "activeProviders": providers.active_keys(),
                    "cache": SEARCH_CACHE.stats(),
                })
            if route == "/api/vision":
                # `?refresh=1` re-probes Ollama, so pulling a model shows up
                # without restarting the server.
                fresh = _bool(urllib.parse.parse_qs(parsed.query).get("refresh", [""])[0])
                return self._json(200, vision.status(force=fresh))
            if route == "/api/meta":
                return self._route_meta()
            if route == "/api/categories":
                tree = taxonomy.public_tree()
                return self._json(200, {
                    "categories": tree,
                    "totalParts": len(list(taxonomy.all_parts())),
                    "totalCategories": len(list(taxonomy.iter_nodes())),
                    # Line items the distributors list across the tree. Only the
                    # top level is summed -- children are already counted in it.
                    "totalSupplierParts": sum(n.get("supplierParts") or 0 for n in tree),
                })
            if route == "/api/providers":
                return self._json(200, {"providers": providers.describe()})
            if route == "/api/search":
                return self._route_search(parsed.query)
            if route == "/api/search/stream":
                return self._route_stream(parsed.query)
            if route == "/api/bom/stream":
                return self._route_bom_stream(parsed.query)
            if route == "/api/bom/export.xlsx":
                return self._route_bom_export(parsed.query)
            if route == "/api/export.csv":
                return self._route_csv(parsed.query)
            if route.startswith("/api/"):
                return self._error(404, "Unknown API route: %s" % route)
            return self._route_static(parsed.path)
        except BrokenPipeError:
            pass  # client navigated away mid-response
        except ConnectionResetError:
            pass
        except Exception as exc:  # never leak a traceback to the browser
            sys.stderr.write("  ! unhandled error on %s: %r\n" % (route, exc))
            try:
                self._error(500, "Internal server error.", str(exc)[:200])
            except Exception:
                pass

    def do_POST(self):
        route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if route == "/login":
                return self._route_login()
            if not self._gate(route):
                return
            if route == "/api/cache/clear":
                SEARCH_CACHE.clear()
                return self._json(200, {"status": "cleared"})
            if route == "/api/identify":
                return self._route_identify()
            if route == "/api/bom/parse":
                return self._route_bom_parse()
            if route == "/api/bom/price":
                return self._route_bom_price()
            return self._error(404, "Unknown API route: %s" % route)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as exc:
            sys.stderr.write("  ! unhandled error on %s: %r\n" % (route, exc))
            try:
                self._error(500, "Internal server error.", str(exc)[:200])
            except Exception:
                pass

    def _route_login(self):
        """Check the submitted password and start a session."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if 0 < length <= 4096 else ""
        supplied = urllib.parse.parse_qs(raw).get("password", [""])[0]
        if not auth.password_ok(supplied):
            # No detail about why, and a small delay, so the form cannot be
            # used to hunt for the password quickly.
            time.sleep(0.5)
            return self._send(200, _login_page("That password was not right."),
                              "text/html; charset=utf-8", {"Cache-Control": "no-store"})
        return self._send(302, b"", "text/plain", {
            "Location": "/",
            "Set-Cookie": auth.cookie_header(auth.issue(), self._is_secure()),
            "Cache-Control": "no-store",
        })

    def _read_json_body(self):
        """Read a JSON request body, or return None after answering with 4xx."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            self._error(400, "Request body is missing.")
            return None
        if length > MAX_UPLOAD_BYTES:
            # The body is never read, so this connection cannot be reused --
            # whatever is left of the upload would be parsed as the next
            # request line.
            self.close_connection = True
            self._error(413, "Upload is too large (limit %d MB)."
                        % (MAX_UPLOAD_BYTES // 1048576))
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "Request body is not valid JSON.")
            return None
        if not isinstance(payload, dict):
            self._error(400, "Request body must be a JSON object.")
            return None
        return payload

    def _route_identify(self):
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            result = vision.identify(
                payload.get("image"),
                note=payload.get("note") or "",
                model=(payload.get("model") or "").strip() or None,
            )
        except vision.VisionError as exc:
            # A missing model or an unreadable upload is the user's to fix, and
            # the message already says how -- so it goes back as a 400, not a
            # 500 with a stack trace behind it.
            return self._json(400, {
                "error": {"status": 400, "message": str(exc), "hint": None},
                "vision": vision.status(),
            })
        return self._json(200, result)

    def _route_meta(self):
        return self._json(200, {
            "providers": providers.describe(),
            "currencies": fx.supported(),
            "currencySymbols": fx.SYMBOLS,
            "fx": fx.status(),
            "defaultCurrency": config.DEFAULT_CURRENCY,
            "sorts": [
                {"key": "price_asc", "label": "Price: low to high"},
                {"key": "price_desc", "label": "Price: high to low"},
                {"key": "stock_desc", "label": "Stock: high to low"},
                {"key": "stock_asc", "label": "Stock: low to high"},
                {"key": "name_asc", "label": "Component name (A-Z)"},
                {"key": "supplier_asc", "label": "Supplier (A-Z)"},
            ],
            "vision": vision.status(),
            "usage": usage.snapshot(),
            "cacheTtl": config.CACHE_TTL,
            "catalogueParts": len(list(taxonomy.all_parts())),
            "catalogueCategories": len(list(taxonomy.iter_nodes())),
            "liveProviderCount": len([
                p for p in providers.describe()
                if p["enabled"] and p["kind"] not in ("sample",)
            ]),
        })

    def _route_search(self, query_string):
        params = self._params(query_string)
        if not params["query"]:
            return self._error(400, "Enter a component name, part number or category.")
        try:
            payload = engine.search(
                params["query"], quantity=params["quantity"], currency=params["currency"],
                sources=params["sources"], sort=params["sort"],
                in_stock_only=params["in_stock_only"], limit=params["limit"],
                use_cache=not params["no_cache"],
            )
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._json(200, payload)

    def _route_stream(self, query_string):
        params = self._params(query_string)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def emit(event, data):
            chunk = "event: %s\ndata: %s\n\n" % (
                event, json.dumps(data, ensure_ascii=False, allow_nan=False)
            )
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        try:
            if not params["query"]:
                emit("error", {"message": "Enter a component name, part number or category."})
                return
            for event, data in engine.search_stream(
                params["query"], quantity=params["quantity"], currency=params["currency"],
                sources=params["sources"], sort=params["sort"],
                in_stock_only=params["in_stock_only"], limit=params["limit"],
            ):
                emit(event, data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                emit("error", {"message": "Search failed: %s" % str(exc)[:200]})
            except Exception:
                pass

    def _route_csv(self, query_string):
        params = self._params(query_string)
        if not params["query"]:
            return self._error(400, "Enter a component name, part number or category.")
        try:
            payload = engine.search(
                params["query"], quantity=params["quantity"], currency=params["currency"],
                sources=params["sources"], sort=params["sort"],
                in_stock_only=params["in_stock_only"], limit=params["limit"],
            )
        except ValueError as exc:
            return self._error(400, str(exc))

        buf = io.StringIO(newline="")
        writer = csv.writer(buf)
        writer.writerow([label for _key, label in CSV_COLUMNS])
        for row in payload["results"]:
            writer.writerow([
                "" if row.get(key) is None else row.get(key) for key, _label in CSV_COLUMNS
            ])
        safe = "".join(c for c in params["query"] if c.isalnum() or c in "-_")[:40] or "search"
        # utf-8-sig so Excel opens the part numbers correctly.
        body = buf.getvalue().encode("utf-8-sig")
        self._send(200, body, "text/csv; charset=utf-8", {
            "Content-Disposition": 'attachment; filename="hardware-agent-%s.csv"' % safe,
            "Cache-Control": "no-store",
        })

    # ------------------------------------------------------ bill of materials

    def _route_bom_parse(self):
        """Read an uploaded BOM and return the part numbers found in it."""
        payload = self._read_json_body()
        if payload is None:
            return
        raw = payload.get("file") or ""
        if raw.startswith("data:"):
            raw = raw.partition(",")[2]
        try:
            data = base64.b64decode(re.sub(r"\s+", "", raw), validate=True)
        except (binascii.Error, ValueError):
            return self._error(400, "The uploaded file could not be decoded.")
        if not data:
            return self._error(400, "The uploaded file is empty.")

        try:
            items, meta = bom.parse(data, payload.get("name") or "")
        except bom.BomError as exc:
            # A file the agent cannot interpret is the user's to fix, and the
            # message says how -- so it is a 400, not a 500.
            return self._json(400, {"error": {"status": 400, "message": str(exc),
                                              "hint": None}})
        return self._json(200, {
            "items": items[:config.BOM_MAX_LINES],
            "meta": meta,
            "truncated": len(items) > config.BOM_MAX_LINES,
            "maxLines": config.BOM_MAX_LINES,
        })

    def _route_bom_price(self):
        """Start pricing a list of part numbers; returns a job id to follow."""
        payload = self._read_json_body()
        if payload is None:
            return
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return self._error(400, "No part numbers were sent to price.")

        # How many of the product are being built. Every line's order quantity
        # is its per-unit figure times this, so the same uploaded BOM answers
        # "five of them" and "two hundred of them" without being re-read.
        units = max(1, min(_int(payload.get("units"), 1), 1_000_000))

        items = []
        for index, entry in enumerate(raw_items[:config.BOM_MAX_LINES], start=1):
            if not isinstance(entry, dict):
                continue
            mpn = str(entry.get("mpn") or "").strip()[:120]
            if not mpn:
                continue
            # perUnit is authoritative when it is there: scaling on the server
            # means a stale figure in the browser cannot quietly order the
            # wrong number of parts.
            per_unit = bom.parse_qty(entry.get("perUnit"))
            quantity = (per_unit * units if per_unit is not None
                        else bom.parse_qty(entry.get("quantity")))
            items.append({
                "line": _int(entry.get("line"), index),
                "mpn": mpn,
                "perUnit": per_unit,
                "quantity": quantity,
                "reference": str(entry.get("reference") or "")[:120],
                "description": str(entry.get("description") or "")[:200],
                "manufacturer": str(entry.get("manufacturer") or "")[:80],
            })
        if not items:
            return self._error(400, "None of those lines carried a part number.")

        currency = (payload.get("currency") or config.DEFAULT_CURRENCY).upper()
        if currency not in config.FX_RATES:
            currency = "USD"

        # Each line costs one call per active supplier. Running a BOM that
        # cannot finish wastes the very quota it runs out of: the early lines
        # spend it, the rest come back unpriced, and the total is wrong without
        # being obviously wrong. So it is checked before anything is spent.
        needed = len(items)
        short = [u for u in usage.snapshot()["providers"]
                 if u["remaining"] is not None and u["remaining"] < needed
                 and u["key"] in providers.active_keys()]
        if short and not _bool(payload.get("ignoreQuota")):
            worst = min(short, key=lambda u: u["remaining"])
            return self._json(409, {"error": {
                "status": 409,
                "code": "quota",
                "message": "%s has %d API call%s left today, and this file needs %d."
                           % (worst["label"], worst["remaining"],
                              "" if worst["remaining"] == 1 else "s", needed),
                "hint": "Price fewer lines, wait for the daily reset, or run it "
                        "anyway to get prices from the other suppliers.",
                "provider": worst["label"],
                "remaining": worst["remaining"],
                "needed": needed,
            }})

        job_id = bom.start_job(items, currency, _bool(payload.get("inStock")),
                               units=units)
        return self._json(200, {"jobId": job_id, "lines": len(items),
                                "currency": currency, "units": units})

    def _route_bom_stream(self, query_string):
        """Stream a pricing job's lines as they finish."""
        raw = urllib.parse.parse_qs(query_string)
        job = bom.get_job((raw.get("job", [""])[0] or "").strip())
        if job is None:
            return self._error(404, "That pricing job has expired. Upload the file again.")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def emit(event, data):
            chunk = "event: %s\ndata: %s\n\n" % (
                event, json.dumps(data, ensure_ascii=False, allow_nan=False))
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        try:
            emit("start", bom.job_state(job))
            sent = 0
            repaired = 0
            # Poll the job rather than sharing a queue with it: the worker pool
            # is already the interesting concurrency here, and a poll keeps the
            # producer side free of any per-client state.
            while True:
                lines = job["lines"][sent:]
                for line in lines:
                    emit("line", line)
                    sent += 1
                # A repaired line was already drawn once, so it is sent as its
                # own event and the browser replaces that row rather than
                # appending a duplicate.
                fixes = job["repairs"][repaired:]
                for line in fixes:
                    emit("repair", line)
                    repaired += 1
                if job["done"] and sent >= len(job["lines"])                         and repaired >= len(job["repairs"]):
                    break
                time.sleep(0.25)
            emit("done", bom.job_state(job))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                emit("error", {"message": "Pricing failed: %s" % str(exc)[:200]})
            except Exception:
                pass

    def _route_bom_export(self, query_string):
        raw = urllib.parse.parse_qs(query_string)
        job = bom.get_job((raw.get("job", [""])[0] or "").strip())
        if job is None:
            return self._error(404, "That pricing job has expired. Upload the file again.")
        if not job["lines"]:
            return self._error(409, "That job has no priced lines yet.")

        # Two shapes of the same job: the sourcing sheet a buyer circulates
        # (one row per part, chosen supplier only) and the full comparison
        # (every supplier that quoted, so the choice can be checked).
        wants_offers = (raw.get("format", [""])[0] or "").strip() == "offers"
        if wants_offers:
            headers = [label for label, _align in bom.EXPORT_COLUMNS]
            rows = bom.export_rows(job)
            sheet_name = "All quotes"
            name = "hardware-agent-all-quotes"
        else:
            headers = bom.sourcing_headers(job)
            rows = bom.sourcing_rows(job)
            sheet_name = "Best price quotes"
            name = "Best price quotes - %d unit%s" % (
                job.get("units") or 1, "" if (job.get("units") or 1) == 1 else "s")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d")

        body = export.write_xlsx(headers, rows, sheet_name=sheet_name)
        ctype = ("application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet")
        name = "%s %s.xlsx" % (name, stamp)

        self._send(200, body, ctype, {
            "Content-Disposition": 'attachment; filename="%s"' % name,
            "Cache-Control": "no-store",
        })

    def _route_static(self, path):
        rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if rel in ("", "."):
            rel = "index.html"
        target = os.path.normpath(os.path.join(config.WEB_DIR, rel))
        # Refuse anything that escapes the web directory.
        if not target.startswith(os.path.normpath(config.WEB_DIR) + os.sep) \
                and target != os.path.normpath(config.WEB_DIR):
            return self._error(403, "Forbidden")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return self._error(404, "Not found: %s" % path)

        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype, {"Cache-Control": "no-cache"})


def _pick_port(host, preferred):
    """Use the configured port, or step forward if something already owns it."""
    for candidate in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(
        "No free port between %d and %d. Set PORT in .env." % (preferred, preferred + 19)
    )


def _lan_addresses():
    """This machine's own IPv4 addresses on the local network.

    Only meaningful when the server is bound to every interface. The UDP
    socket is never actually sent to -- connect() on a datagram socket just
    makes the OS pick the interface it would route through, which is the
    address a colleague on the same network has to type.
    """
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.4)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith(("127.", "169.254.")):
                found.append(addr)
    except OSError:
        pass
    return found


def main():
    port = _pick_port(config.HOST, config.PORT)
    # 0.0.0.0 means "listen on every interface". It is not an address anything
    # can browse to, so the local URL is always written as loopback and the
    # shareable ones are listed separately.
    shared = config.HOST in ("0.0.0.0", "::")
    url = "http://127.0.0.1:%d/" % port if shared else "http://%s:%d/" % (config.HOST, port)
    described = providers.describe()
    live = [p for p in described if p["enabled"] and p["kind"] != "sample"]

    print("\n  Hardware Agent")
    print("  " + "-" * 58)
    print("  Serving   %s" % url)
    if shared:
        addresses = _lan_addresses()
        note = "sign-in required" if auth.enabled() else "no login -- anyone can use it"
        for addr in addresses:
            print("  Share     http://%s:%d/   (same network, %s)" % (addr, port, note))
        if not addresses:
            print("  Share     no network address found -- check you are connected")
    else:
        print("            this machine only. Set HOST=0.0.0.0 in .env to let")
        print("            others on your network reach it.")
    if auth.enabled():
        print("  Password  on -- visitors must sign in")
    elif shared:
        # Bound beyond this machine with no gate: worth saying plainly, because
        # the cost of not noticing is someone else's use of your API quota.
        print("  Password  OFF -- anyone who can reach the address above gets in")
        print("            set APP_PASSWORD in .env to require one")
    print("  Providers %d live, %d total" % (len(live), len(described)))
    for entry in described:
        mark = "on " if entry["enabled"] else "off"
        print("    [%s] %-22s %s" % (mark, entry["label"], entry["reason"]))
    vstat = vision.status()
    print("  Photo ID  %s" % (
        ("on  via %s (%s)" % (vstat["model"], vstat["host"])) if vstat["enabled"]
        else "off -- %s" % vstat["reason"]))
    if vstat["reachable"] and not vstat["enabled"] and config.ENABLE_VISION:
        print("            Pull a vision model to enable it, e.g.:")
        print("              ollama pull %s" % (vstat["suggest"] or ["qwen2.5vl:3b"])[0])

    if not live:
        print("\n  No live distributor API is configured yet, so results come from the")
        print("  built-in reference catalogue and are labelled SAMPLE in the table.")
        print("  Copy .env.example to .env and add a key to get real-time pricing.")
    print("  " + "-" * 58)
    print("  Ctrl+C to stop\n")
    sys.stdout.flush()  # show the banner even when stdout is redirected to a file

    httpd = ThreadingHTTPServer((config.HOST, port), Handler)
    httpd.daemon_threads = True
    if os.environ.get("HW_AGENT_OPEN", "1") != "0":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

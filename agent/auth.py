"""Optional shared-password gate.

Off by default: with no APP_PASSWORD set nothing changes, which keeps the
local, single-user case exactly as it was. Set one and every request has to
carry a valid session cookie, so a public URL does not hand strangers the
distributor API quota the keys in .env are paying for.

Deliberately a shared password rather than accounts. The thing being protected
is an API budget, not personal data, and one password that can be rotated in
.env is proportionate to that -- a user table would be more machinery, more to
get wrong, and no more protective here.
"""
import base64
import hashlib
import hmac
import http.cookies
import os
import time

from . import config

COOKIE = "hwagent_session"
# A week: long enough not to nag a colleague who uses it daily, short enough
# that a shared link stops working within a working cycle if it leaks.
SESSION_TTL = 7 * 24 * 3600

# Signing key for session cookies. Regenerated on every start, so restarting
# the server invalidates outstanding sessions -- the cheapest revocation there
# is, and there is nothing worth persisting across a restart.
_SIGNING_KEY = os.urandom(32)


def enabled():
    return bool(config.APP_PASSWORD)


def _sign(payload):
    return hmac.new(_SIGNING_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue():
    """A signed session token that expires on its own."""
    expires = str(int(time.time() + SESSION_TTL))
    return "%s.%s" % (expires, _sign(expires))


def valid(token):
    if not token or "." not in token:
        return False
    expires, signature = token.rsplit(".", 1)
    # compare_digest, not ==, so a wrong signature cannot be narrowed down by
    # timing how long the rejection took.
    if not hmac.compare_digest(signature, _sign(expires)):
        return False
    try:
        return float(expires) > time.time()
    except ValueError:
        return False


def password_ok(supplied):
    """Constant-time check of the supplied password against the configured one."""
    if not enabled():
        return True
    return hmac.compare_digest(
        hashlib.sha256((supplied or "").encode("utf-8")).digest(),
        hashlib.sha256(config.APP_PASSWORD.encode("utf-8")).digest(),
    )


def token_from_headers(headers):
    raw = headers.get("Cookie")
    if not raw:
        return None
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
    except http.cookies.CookieError:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


def authorised(headers):
    """Is this request allowed through the gate?"""
    if not enabled():
        return True
    if valid(token_from_headers(headers)):
        return True
    # A shared link is easier to use from a script or curl with a header than
    # with a cookie, so the same password is accepted as a bearer token.
    header = headers.get("Authorization") or ""
    if header.startswith("Bearer "):
        return password_ok(header[7:].strip())
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return False
        return password_ok(decoded.split(":", 1)[-1])
    return False


def cookie_header(token, secure):
    bits = [
        "%s=%s" % (COOKIE, token),
        "Path=/",
        "Max-Age=%d" % SESSION_TTL,
        # Unreadable from JavaScript, and not sent on cross-site requests, so a
        # link from elsewhere cannot act as a logged-in user.
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


def clearing_cookie():
    return "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE

#!/usr/bin/env python3
"""Verify every distributor credential in .env against the live API.

Run this after pasting a key. It performs a real search per provider and
reports exactly what came back, so a bad key is diagnosed here in two seconds
rather than showing up as an empty table later.

    python tools/check_keys.py
    python tools/check_keys.py LM358P      # test with your own search term
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import config, providers  # noqa: E402

# What a failure most likely means, keyed on a fragment of the error text.
HINTS = [
    ("invalid clientid", "DIGIKEY_CLIENT_ID is wrong, or the app is not approved yet."),
    ("invalid_client", "Client ID/secret pair rejected. Re-copy both from the portal."),
    ("invalid unique identifier", "MOUSER_API_KEY is malformed. It should be a UUID."),
    ("developer inactive", "FARNELL_API_KEY is not activated yet. Check the partner portal."),
    ("401", "Credentials rejected. Re-copy the key and check for stray spaces."),
    ("403", "Access denied. The key may lack permission for this endpoint."),
    ("429", "Rate limited. You have used up the quota for now."),
    ("timed out", "Network or firewall issue reaching this distributor."),
    ("bot-protected", "This endpoint is blocked from your network."),
]


def hint_for(message):
    lowered = (message or "").lower()
    for needle, advice in HINTS:
        if needle in lowered:
            return advice
    return None


def main():
    term = sys.argv[1] if len(sys.argv) > 1 else "LM358P"
    print("\n  Checking distributor credentials with a live search for %r\n" % term)
    print("  %-22s %-9s %s" % ("SOURCE", "STATUS", "DETAIL"))
    print("  " + "-" * 74)

    live = configured = 0
    for provider in providers.ALL:
        if getattr(provider, "KIND", "") == "sample":
            continue
        ok, reason = provider.available()
        if not ok:
            print("  %-22s %-9s %s" % (provider.LABEL[:22], "not set", reason[:44]))
            continue

        configured += 1
        started = time.time()
        try:
            offers = provider.search(term, quantity=1, display_currency="USD", limit=3) or []
            elapsed = int((time.time() - started) * 1000)
            if offers:
                live += 1
                sample = offers[0]
                print("  %-22s %-9s %d offers in %d ms (e.g. %s, stock %s)"
                      % (provider.LABEL[:22], "OK", len(offers), elapsed,
                         sample["mpn"][:18], sample["stock"]))
            else:
                print("  %-22s %-9s reachable, but no match for %r"
                      % (provider.LABEL[:22], "empty", term))
        except Exception as exc:
            message = str(exc)
            print("  %-22s %-9s %s" % (provider.LABEL[:22], "FAILED", message[:44]))
            advice = hint_for(message)
            if advice:
                print("  %-22s %-9s -> %s" % ("", "", advice))

    print("  " + "-" * 74)
    print("  %d of %d configured source(s) returned live data.\n" % (live, configured))
    if live == 0:
        print("  Nothing is live. Add a key to .env and re-run this check.")
        print("  Start with Nexar (portal.nexar.com) -- one key covers many distributors.\n")
    else:
        print("  Restart the app to pick up any changes:  python app.py\n")


if __name__ == "__main__":
    main()

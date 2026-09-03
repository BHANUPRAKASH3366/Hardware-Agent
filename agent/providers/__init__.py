"""Provider registry.

A provider is any module exposing KEY / LABEL / HOMEPAGE, an `available()`
returning (bool, reason) and a `search(...)` returning normalized offers.
"""
from . import demo, digikey, farnell, mouser, nexar

ALL = [mouser, digikey, nexar, farnell, demo]
BY_KEY = {p.KEY: p for p in ALL}


def describe():
    out = []
    for p in ALL:
        ok, reason = p.available()
        out.append({
            "key": p.KEY,
            "label": p.LABEL,
            "homepage": getattr(p, "HOMEPAGE", ""),
            "kind": getattr(p, "KIND", "distributor"),
            "enabled": ok,
            "reason": reason,
            "docs": getattr(p, "DOCS", ""),
        })
    return out


def active_keys():
    return [p.KEY for p in ALL if p.available()[0]]

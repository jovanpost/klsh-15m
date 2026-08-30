"""
Kalshi REST client. READ ONLY — there is no order-placing code in this file
and none should ever be added to this repo.

Book shape, confirmed live:
    GET /markets/{ticker}/orderbook
    -> {"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}
    each level is [price_string, size_string]
    sorted LOW -> HIGH, so the BEST bid is the LAST element
    sizes are fractional ("14.10"), prices are 4-decimal dollars ("0.0910")

Both sides are BIDS. A YES bid at X is a NO ask at 1 - X.
"""

import base64
import time
from decimal import Decimal

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import API_PREFIX, BASE_URL


class Kalshi:
    def __init__(self, key_id, private_key_pem):
        self.key_id = key_id
        self.pk = serialization.load_pem_private_key(
            private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem,
            password=None,
        )
        self.session = requests.Session()

    def _headers(self, method, path):
        ts = str(int(time.time() * 1000))
        sig = self.pk.sign(
            (ts + method + path).encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Accept": "application/json",
        }

    def get(self, path, params=None, timeout=10):
        r = self.session.get(
            BASE_URL + path,
            headers=self._headers("GET", API_PREFIX + path),
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    # ---- endpoints -----------------------------------------------------

    def open_markets(self, series_ticker, limit=20):
        data = self.get(
            "/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": limit},
        )
        return data.get("markets", []) or []

    def orderbook(self, ticker):
        return self.get(f"/markets/{ticker}/orderbook")


# ---- parsing -----------------------------------------------------------


def _levels(raw):
    """[[price_str, size_str], ...] -> [(Decimal price, Decimal size), ...]"""
    out = []
    for lvl in raw or []:
        try:
            out.append((Decimal(str(lvl[0])), Decimal(str(lvl[1]))))
        except Exception:
            continue
    return out


def parse_book(payload):
    """
    Returns a plain dict for one snapshot, or None if the book is unusable.

    Sorted low -> high, so best bid is the LAST element. If Kalshi ever
    changes that ordering this would silently read the worst level, so we
    take max() by price instead of trusting position.
    """
    inner = (payload or {}).get("orderbook_fp")
    if not isinstance(inner, dict):
        return None

    yes = _levels(inner.get("yes_dollars"))
    no = _levels(inner.get("no_dollars"))

    yes_best = max(yes, key=lambda p: p[0]) if yes else (None, None)
    no_best = max(no, key=lambda p: p[0]) if no else (None, None)

    spread = None
    if yes_best[0] is not None and no_best[0] is not None:
        spread = Decimal("1") - yes_best[0] - no_best[0]

    return {
        "yes_bid": yes_best[0],
        "yes_bid_size": yes_best[1],
        "no_bid": no_best[0],
        "no_bid_size": no_best[1],
        "yes_depth_total": sum((s for _, s in yes), Decimal("0")) if yes else Decimal("0"),
        "no_depth_total": sum((s for _, s in no), Decimal("0")) if no else Decimal("0"),
        "yes_levels": len(yes),
        "no_levels": len(no),
        "spread": spread,
    }

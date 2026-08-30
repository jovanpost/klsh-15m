"""Kalshi REST. READ ONLY."""

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

    def get(self, path, params=None, timeout=15):
        r = self.session.get(
            BASE_URL + path,
            headers=self._headers("GET", API_PREFIX + path),
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    def open_markets(self, series_ticker, limit=20):
        data = self.get(
            "/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": limit},
        )
        return data.get("markets", []) or []

    def orderbook(self, ticker):
        # depth=0 means all levels
        return self.get(f"/markets/{ticker}/orderbook", params={"depth": 0})

    def market(self, ticker):
        data = self.get(f"/markets/{ticker}")
        return data.get("market") or data


def _levels(raw):
    out = []
    for lvl in raw or []:
        try:
            out.append((Decimal(str(lvl[0])), Decimal(str(lvl[1]))))
        except Exception:
            continue
    return out


def _book_json(levels):
    """[[price, size], ...] numbers, best bid first."""
    ranked = sorted(levels, key=lambda p: p[0], reverse=True)
    return [[float(p), float(s)] for p, s in ranked]


def parse_book(payload):
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

    # depth=0 is "all"; exactly 100 on a side is the old capped default
    truncated = (len(yes) == 100) or (len(no) == 100)

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
        "yes_book": _book_json(yes),
        "no_book": _book_json(no),
        "book_truncated": truncated,
    }
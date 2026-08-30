"""
STEP 2 — probe only. Prints one live order book as raw JSON.

Purpose: confirm the field names and the bids-only shape with your own eyes
BEFORE any parsing code gets written. Field names have changed before
(_dollars vs _fp fixed-point strings).

Places no orders. Reads only.

Run locally:
    export KALSHI_KEY_ID=...
    export KALSHI_PRIVATE_KEY_PATH=./kalshi_key.pem
    python scripts/probe_orderbook.py KXGOLD15M
"""

import base64
import json
import os
import sys
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://external-api.kalshi.com/trade-api/v2"


def load_key():
    pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM")
    if pem:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "./kalshi_key.pem")
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def headers(method, path, key_id, private_key):
    """Kalshi signs: timestamp_ms + METHOD + path (no query string)."""
    ts = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Accept": "application/json",
    }


def get(path, key_id, private_key, params=None):
    url = BASE + path
    r = requests.get(
        url,
        headers=headers("GET", "/trade-api/v2" + path, key_id, private_key),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def main():
    series = sys.argv[1] if len(sys.argv) > 1 else "KXGOLD15M"
    key_id = os.environ["KALSHI_KEY_ID"]
    pk = load_key()

    # 1. Find the currently open market in this series.
    markets = get(
        "/markets",
        key_id,
        pk,
        params={"series_ticker": series, "status": "open", "limit": 20},
    ).get("markets", [])

    if not markets:
        print(f"No open markets for {series}. Is the series ticker right?")
        return

    print(f"--- {len(markets)} open market(s) in {series} ---")
    for m in markets:
        print(" ", m.get("ticker"), "| closes", m.get("close_time"))

    ticker = markets[0]["ticker"]

    # 2. Raw market object — so we can see which fields carry close time etc.
    print("\n--- RAW market object:", ticker, "---")
    print(json.dumps(markets[0], indent=2)[:3000])

    # 3. Raw order book — the thing we actually need to confirm.
    book = get(f"/markets/{ticker}/orderbook", key_id, pk)
    print("\n--- RAW orderbook:", ticker, "---")
    print(json.dumps(book, indent=2)[:4000])

    print("\n--- top-level keys ---")
    print(list(book.keys()))
    inner = book.get("orderbook", book)
    if isinstance(inner, dict):
        print("inner keys:", list(inner.keys()))
        for k, v in inner.items():
            print(f"  {k}: type={type(v).__name__}", end="")
            if isinstance(v, list) and v:
                print(f" len={len(v)} first_entry={v[0]!r}")
            else:
                print()


if __name__ == "__main__":
    main()

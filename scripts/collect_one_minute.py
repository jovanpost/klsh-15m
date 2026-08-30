"""
Smoke test: poll one series for ~60s, write one depth_minute row, exit.

Places no orders. Reads the book and inserts into the shared DB.

Colab:
    os.environ["KALSHI_KEY_ID"] = "..."
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = "/content/kalshi_key.txt"
    os.environ["DATABASE_URL"] = "..."   # dt-klsh-bot pooler URL, never commit
    python scripts/collect_one_minute.py KXDOGE15M
"""

from __future__ import annotations

import base64
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = "https://external-api.kalshi.com/trade-api/v2"
POLL_SEC = 5
RUN_SEC = 62
EXPECT_SAMPLES = 12


def load_key():
    pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM")
    if pem:
        return serialization.load_pem_private_key(pem.encode(), password=None)
    path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    with open(path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def headers(method, path, key_id, private_key):
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
    r = requests.get(
        BASE + path,
        headers=headers("GET", "/trade-api/v2" + path, key_id, private_key),
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def dec(value):
    if value is None or value == "":
        return None
    return Decimal(str(value))


def parse_side(levels):
    """levels: [[price_str, size_str], ...] low -> high. Best bid = last."""
    if not levels:
        return None, None, Decimal("0"), 0
    price = dec(levels[-1][0])
    size = dec(levels[-1][1])
    depth = sum((dec(row[1]) or Decimal("0")) for row in levels)
    return price, size, depth, len(levels)


def parse_book(book):
    ob = book.get("orderbook_fp") or {}
    yes_p, yes_s, yes_d, yes_n = parse_side(ob.get("yes_dollars") or [])
    no_p, no_s, no_d, no_n = parse_side(ob.get("no_dollars") or [])
    spread = None
    if yes_p is not None and no_p is not None:
        spread = Decimal("1") - yes_p - no_p
    return {
        "yes_bid": yes_p,
        "yes_bid_size": yes_s,
        "no_bid": no_p,
        "no_bid_size": no_s,
        "yes_depth_total": yes_d,
        "no_depth_total": no_d,
        "yes_levels": yes_n,
        "no_levels": no_n,
        "spread": spread,
    }


def minute_floor(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def main():
    series = sys.argv[1] if len(sys.argv) > 1 else "KXDOGE15M"
    key_id = os.environ["KALSHI_KEY_ID"]
    pk = load_key()
    db_url = os.environ["DATABASE_URL"]
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://") :]

    markets = get(
        "/markets",
        key_id,
        pk,
        params={"series_ticker": series, "status": "open", "limit": 20},
    ).get("markets", [])
    if not markets:
        print(f"No open markets for {series}")
        return

    markets = sorted(markets, key=lambda m: m.get("close_time") or "")
    market = markets[0]
    ticker = market["ticker"]
    close_time = market.get("close_time")
    close_dt = None
    if close_time:
        close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))

    print(f"polling {ticker} every {POLL_SEC}s for ~{RUN_SEC}s")

    samples = []
    deadline = time.time() + RUN_SEC
    while time.time() < deadline:
        now = datetime.now(timezone.utc)
        book = get(f"/markets/{ticker}/orderbook", key_id, pk)
        parsed = parse_book(book)
        minutes_left = None
        if close_dt is not None:
            minutes_left = int((close_dt - now).total_seconds() // 60)
        parsed["ts"] = now
        parsed["minute_ts"] = minute_floor(now)
        parsed["minutes_left"] = minutes_left
        samples.append(parsed)
        print(
            f"  {now.isoformat()} yes={parsed['yes_bid']} no={parsed['no_bid']} "
            f"spread={parsed['spread']} yes_n={parsed['yes_levels']} no_n={parsed['no_levels']}"
        )
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(POLL_SEC, remaining))

    if not samples:
        print("no samples")
        return

    # One row for the minute that got the most samples (usually the current one).
    buckets = {}
    for s in samples:
        buckets.setdefault(s["minute_ts"], []).append(s)
    minute_ts, group = max(buckets.items(), key=lambda kv: (len(kv[1]), kv[0]))
    first, last = group[0], group[-1]
    spreads = [s["spread"] for s in group if s["spread"] is not None]
    row = {
        "series": series,
        "ticker": ticker,
        "minute_ts": minute_ts,
        "minutes_left": last["minutes_left"],
        "yes_bid": last["yes_bid"],
        "yes_bid_size": last["yes_bid_size"],
        "no_bid": last["no_bid"],
        "no_bid_size": last["no_bid_size"],
        "yes_depth_total": last["yes_depth_total"],
        "no_depth_total": last["no_depth_total"],
        "yes_levels": last["yes_levels"],
        "no_levels": last["no_levels"],
        "spread_close": last["spread"],
        "spread_min": min(spreads) if spreads else None,
        "spread_max": max(spreads) if spreads else None,
        "yes_bid_first": first["yes_bid"],
        "no_bid_first": first["no_bid"],
        "yes_bid_last": last["yes_bid"],
        "no_bid_last": last["no_bid"],
        "samples": len(group),
        "gap_flag": len(group) < 10,
    }

    print("writing row:", row)

    sql = """
        insert into depth_minute (
            series, ticker, minute_ts, minutes_left,
            yes_bid, yes_bid_size, no_bid, no_bid_size,
            yes_depth_total, no_depth_total, yes_levels, no_levels,
            spread_close, spread_min, spread_max,
            yes_bid_first, no_bid_first, yes_bid_last, no_bid_last,
            samples, gap_flag
        ) values (
            %(series)s, %(ticker)s, %(minute_ts)s, %(minutes_left)s,
            %(yes_bid)s, %(yes_bid_size)s, %(no_bid)s, %(no_bid_size)s,
            %(yes_depth_total)s, %(no_depth_total)s, %(yes_levels)s, %(no_levels)s,
            %(spread_close)s, %(spread_min)s, %(spread_max)s,
            %(yes_bid_first)s, %(no_bid_first)s, %(yes_bid_last)s, %(no_bid_last)s,
            %(samples)s, %(gap_flag)s
        )
        on conflict (ticker, minute_ts) do update set
            minutes_left = excluded.minutes_left,
            yes_bid = excluded.yes_bid,
            yes_bid_size = excluded.yes_bid_size,
            no_bid = excluded.no_bid,
            no_bid_size = excluded.no_bid_size,
            yes_depth_total = excluded.yes_depth_total,
            no_depth_total = excluded.no_depth_total,
            yes_levels = excluded.yes_levels,
            no_levels = excluded.no_levels,
            spread_close = excluded.spread_close,
            spread_min = excluded.spread_min,
            spread_max = excluded.spread_max,
            yes_bid_first = excluded.yes_bid_first,
            no_bid_first = excluded.no_bid_first,
            yes_bid_last = excluded.yes_bid_last,
            no_bid_last = excluded.no_bid_last,
            samples = excluded.samples,
            gap_flag = excluded.gap_flag
        returning ticker, minute_ts, samples, yes_bid, no_bid, spread_close,
                  yes_levels, no_levels, gap_flag;
    """
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, row)
                written = cur.fetchone()
                print("db returned:", written)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

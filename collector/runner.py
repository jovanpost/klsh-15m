"""
The collector thread.

Shape of the loop, in plain terms:
  every 30s  - ask Kalshi which market is currently open in each series
  every  5s  - fetch that market's order book and drop the snapshot in a bucket
  when the clock ticks over to a new minute - empty the bucket into one
               database row, then start a fresh bucket

The bucket is why we poll 12 times but only write once. Raw 5-second rows
would be ~86,000 a day and would eat the Supabase free tier in weeks.

Read only. This thread never places, cancels, or modifies an order.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import (
    DISCOVERY_SECONDS,
    EXPECTED_SAMPLES,
    GAP_THRESHOLD,
    POLL_SECONDS,
)
from .kalshi import Kalshi, parse_book
from .store import insert_minute, make_engine

log = logging.getLogger("collector")


def _minute_floor(dt):
    return dt.replace(second=0, microsecond=0)


def _parse_close(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


class Bucket:
    """One minute's worth of snapshots for one market."""

    def __init__(self, series, ticker, minute_ts, close_time):
        self.series = series
        self.ticker = ticker
        self.minute_ts = minute_ts
        self.close_time = close_time
        self.samples = []

    def add(self, snap):
        self.samples.append(snap)

    def to_row(self):
        if not self.samples:
            return None
        first, last = self.samples[0], self.samples[-1]
        spreads = [s["spread"] for s in self.samples if s["spread"] is not None]

        minutes_left = None
        if self.close_time:
            delta = (self.close_time - self.minute_ts).total_seconds()
            minutes_left = int(delta // 60)

        n = len(self.samples)
        return {
            "series": self.series,
            "ticker": self.ticker,
            "minute_ts": self.minute_ts,
            "minutes_left": minutes_left,
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
            "samples": n,
            "gap_flag": n < GAP_THRESHOLD,
        }


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kalshi = Kalshi(cfg.kalshi_key_id, cfg.kalshi_private_key)
        self.engine = make_engine(cfg.database_url)

        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

        # dashboard state
        self.started_at = None
        self.last_poll_at = None
        self.last_write_at = None
        self.last_error = None
        self.rows_written = 0
        self.poll_errors = 0
        self.active = {}          # series -> ticker
        self._buckets = {}        # ticker -> Bucket
        self._close_times = {}    # ticker -> datetime
        self._last_discovery = 0.0

    # ---- lifecycle -----------------------------------------------------

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.started_at = datetime.now(timezone.utc)
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def alive(self):
        return bool(self._thread and self._thread.is_alive())

    # ---- work ----------------------------------------------------------

    def _discover(self):
        for series in self.cfg.series:
            try:
                markets = self.kalshi.open_markets(series)
            except Exception as exc:
                self.last_error = f"discover {series}: {exc}"
                continue

            best, best_close = None, None
            for m in markets:
                close = _parse_close(m.get("close_time"))
                if close is None:
                    continue
                # the soonest-closing open market is the live window
                if best_close is None or close < best_close:
                    best, best_close = m.get("ticker"), close

            if best:
                self.active[series] = best
                self._close_times[best] = best_close
            else:
                self.active.pop(series, None)
        self._last_discovery = time.time()

    def _flush(self, ticker):
        bucket = self._buckets.pop(ticker, None)
        if not bucket:
            return
        row = bucket.to_row()
        if not row:
            return
        try:
            insert_minute(self.engine, row)
            self.rows_written += 1
            self.last_write_at = datetime.now(timezone.utc)
        except Exception as exc:
            self.last_error = f"write {ticker}: {exc}"

    def _poll_once(self):
        now = datetime.now(timezone.utc)
        minute = _minute_floor(now)
        live = set()

        for series, ticker in list(self.active.items()):
            live.add(ticker)
            try:
                snap = parse_book(self.kalshi.orderbook(ticker))
            except Exception as exc:
                self.poll_errors += 1
                self.last_error = f"book {ticker}: {exc}"
                continue
            if snap is None:
                continue

            bucket = self._buckets.get(ticker)
            if bucket is None:
                bucket = Bucket(series, ticker, minute, self._close_times.get(ticker))
                self._buckets[ticker] = bucket
            elif bucket.minute_ts != minute:
                self._flush(ticker)
                bucket = Bucket(series, ticker, minute, self._close_times.get(ticker))
                self._buckets[ticker] = bucket

            bucket.add(snap)

        # a market that rolled over or closed still owes us its last minute
        for ticker in [t for t in self._buckets if t not in live]:
            self._flush(ticker)

        self.last_poll_at = now

    def _run(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                if started - self._last_discovery > DISCOVERY_SECONDS:
                    self._discover()
                self._poll_once()
            except Exception as exc:  # never let the thread die
                self.last_error = f"loop: {exc}"
                log.exception("collector loop error")
            sleep_for = POLL_SECONDS - (time.time() - started)
            if sleep_for > 0:
                self._stop.wait(sleep_for)

    # ---- dashboard -----------------------------------------------------

    def status(self):
        stale = None
        if self.last_poll_at:
            stale = (datetime.now(timezone.utc) - self.last_poll_at).total_seconds()
        return {
            "alive": self.alive(),
            "started_at": self.started_at,
            "last_poll_at": self.last_poll_at,
            "seconds_since_poll": stale,
            "last_write_at": self.last_write_at,
            "rows_written": self.rows_written,
            "poll_errors": self.poll_errors,
            "last_error": self.last_error,
            "active": dict(self.active),
            "expected_samples": EXPECTED_SAMPLES,
            "pending_minutes": len(self._buckets),
        }

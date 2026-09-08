"""Minute rows always. Full book on minutes 14 and 3 for all series, plus
minutes 12-7 for the three paper series (KXBTC15M/KXXRP15M/KXDOGE15M) so the
decision window has book depth on record. 5s touch samples at open."""

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from .config import (
    DISCOVERY_SECONDS,
    EXPECTED_SAMPLES,
    GAP_THRESHOLD,
    POLL_SECONDS,
)
from .kalshi import Kalshi, parse_book
from .paper import (
    evaluate as paper_eval,
    close_window as paper_close,
    forget as paper_forget,
    PAPER_SERIES,
    MIN_ML as PAPER_MIN_ML,
    MAX_ML as PAPER_MAX_ML,
)
from .store import (
    insert_book_sample,
    insert_minute,
    insert_sample,
    make_engine,
    pending_settlement,
    upsert_settlement,
)

log = logging.getLogger("collector")

BOOK_MINUTES = {14, 3}
SAMPLE_MIN_LEFT = 14
SETTLE_EVERY = 1800

# Full-window book sampling, all series (separate from the paper-strategy
# edge/decision-band book capture above, and from depth_minute/depth_sample).
# 1 in N minute-flushes per ticker gets a full order-book snapshot recorded
# to depth_book_sample. Deterministic counter, not random, so coverage is
# even across the window rather than clustering by chance.
BOOK_SAMPLE_EVERY = 5


def _minute_floor(dt):
    return dt.replace(second=0, microsecond=0)


def _parse_close(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _num(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class Bucket:
    def __init__(self, series, ticker, minute_ts, close_time):
        self.series = series
        self.ticker = ticker
        self.minute_ts = minute_ts
        self.close_time = close_time
        self.samples = []

    def add(self, snap):
        self.samples.append(snap)

    def minutes_left(self):
        if not self.close_time:
            return None
        return int((self.close_time - self.minute_ts).total_seconds() // 60)

    def to_row(self):
        if not self.samples:
            return None
        first, last = self.samples[0], self.samples[-1]
        spreads = [s["spread"] for s in self.samples if s["spread"] is not None]
        ml = self.minutes_left()
        keep_book = ml in BOOK_MINUTES or (
            self.series in PAPER_SERIES
            and ml is not None
            and PAPER_MIN_ML <= ml <= PAPER_MAX_ML
        )
        n = len(self.samples)
        return {
            "series": self.series,
            "ticker": self.ticker,
            "minute_ts": self.minute_ts,
            "minutes_left": ml,
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
            "yes_book": last.get("yes_book") if keep_book else None,
            "no_book": last.get("no_book") if keep_book else None,
            "book_sample_ts": last.get("ts") if keep_book else None,
            "book_truncated": last.get("book_truncated") if keep_book else None,
        }


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kalshi = Kalshi(cfg.kalshi_key_id, cfg.kalshi_private_key)
        self.engine = make_engine(cfg.database_url)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.started_at = None
        self.last_poll_at = None
        self.last_write_at = None
        self.last_error = None
        self.rows_written = 0
        self.poll_errors = 0
        self.active = {}
        self._buckets = {}
        self._close_times = {}
        self._book_sample_counter = {}
        self._last_discovery = 0.0
        self._last_settle = 0.0

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

        # Full-window book sampling (all series). Independent of the write
        # above: a failure here never touches depth_minute or rows_written.
        count = self._book_sample_counter.get(ticker, 0) + 1
        self._book_sample_counter[ticker] = count
        if count % BOOK_SAMPLE_EVERY == 0 and bucket.samples:
            last = bucket.samples[-1]
            try:
                insert_book_sample(
                    self.engine,
                    {
                        "series": bucket.series,
                        "ticker": ticker,
                        "sample_ts": last.get("ts"),
                        "minutes_left": row["minutes_left"],
                        "yes_bid": last.get("yes_bid"),
                        "no_bid": last.get("no_bid"),
                        "spread": last.get("spread"),
                        "yes_depth_total": last.get("yes_depth_total"),
                        "no_depth_total": last.get("no_depth_total"),
                        "yes_book": last.get("yes_book"),
                        "no_book": last.get("no_book"),
                        "book_truncated": last.get("book_truncated"),
                    },
                )
            except Exception as exc:
                log.exception("book sample failed for %s: %s", ticker, exc)

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
            snap["ts"] = now
            close_dt = self._close_times.get(ticker)
            ml = None
            if close_dt is not None:
                ml = int((close_dt - minute).total_seconds() // 60)

            if series in PAPER_SERIES and ml is not None and ml <= 12:
                try:
                    paper_eval(self.engine, self.kalshi, series, ticker, snap, ml, close_dt)
                except Exception:
                    log.exception("paper eval failed for %s", ticker)

            bucket = self._buckets.get(ticker)
            if bucket is None:
                bucket = Bucket(series, ticker, minute, close_dt)
                self._buckets[ticker] = bucket
            elif bucket.minute_ts != minute:
                self._flush(ticker)
                bucket = Bucket(series, ticker, minute, close_dt)
                self._buckets[ticker] = bucket
            bucket.add(snap)

            if ml is not None and ml >= SAMPLE_MIN_LEFT:
                try:
                    insert_sample(
                        self.engine,
                        {
                            "series": series,
                            "ticker": ticker,
                            "sample_ts": now,
                            "minutes_left": ml,
                            "yes_bid": snap["yes_bid"],
                            "yes_bid_size": snap["yes_bid_size"],
                            "no_bid": snap["no_bid"],
                            "no_bid_size": snap["no_bid_size"],
                            "yes_depth_total": snap["yes_depth_total"],
                            "no_depth_total": snap["no_depth_total"],
                            "yes_levels": snap["yes_levels"],
                            "no_levels": snap["no_levels"],
                            "spread": snap["spread"],
                        },
                    )
                except Exception as exc:
                    self.last_error = f"sample {ticker}: {exc}"

        for ticker in [t for t in self._buckets if t not in live]:
            self._flush(ticker)
            try:
                paper_close(self.engine, ticker)
            except Exception:
                log.exception("paper close failed for %s", ticker)
            paper_forget(ticker)
            self._book_sample_counter.pop(ticker, None)
        self.last_poll_at = now

    def _sweep_settlement(self):
        try:
            rows = pending_settlement(self.engine)
        except Exception as exc:
            self.last_error = f"settle list: {exc}"
            return
        for row in rows:
            ticker = row["ticker"]
            series = row["series"]
            try:
                m = self.kalshi.market(ticker) or {}
            except Exception as exc:
                self.last_error = f"settle {ticker}: {exc}"
                try:
                    upsert_settlement(
                        self.engine,
                        {
                            "ticker": ticker,
                            "series": series,
                            "close_time": None,
                            "result": None,
                            "expiration_value": None,
                            "floor_strike": None,
                            "volume_fp": None,
                            "attempts": 1,
                        },
                    )
                except Exception:
                    pass
                continue
            result = (m.get("result") or "").strip().lower() or None
            if result not in ("yes", "no"):
                result = None
            attempts_bump = 0 if result else 1
            try:
                upsert_settlement(
                    self.engine,
                    {
                        "ticker": ticker,
                        "series": series,
                        "close_time": _parse_close(m.get("close_time")),
                        "result": result,
                        "expiration_value": _num(m.get("expiration_value")),
                        "floor_strike": _num(m.get("floor_strike")),
                        "volume_fp": _num(m.get("volume_fp")),
                        "attempts": attempts_bump,
                    },
                )
            except Exception as exc:
                self.last_error = f"settle write {ticker}: {exc}"
        try:
            from .store import settle_paper
            settle_paper(self.engine)
        except Exception as exc:
            self.last_error = f"settle paper: {exc}"
        self._last_settle = time.time()

    def _run(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                if started - self._last_discovery > DISCOVERY_SECONDS:
                    self._discover()
                self._poll_once()
                if started - self._last_settle > SETTLE_EVERY:
                    self._sweep_settlement()
            except Exception as exc:
                self.last_error = f"loop: {exc}"
                log.exception("collector loop error")
            sleep_for = POLL_SECONDS - (time.time() - started)
            if sleep_for > 0:
                self._stop.wait(sleep_for)

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

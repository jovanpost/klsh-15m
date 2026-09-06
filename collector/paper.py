"""
Paper-only up-continuation tracker.

NO ORDER PLACEMENT. Nothing in this file calls a Kalshi write endpoint.
It reads the book, decides, and writes one row per ticker to paper_upcont.

Rules are fixed by the analytics spec. Do not "improve" them.
"""

import logging
from sqlalchemy import text

log = logging.getLogger(__name__)

PAPER_SERIES = ("KXBTC15M", "KXXRP15M", "KXDOGE15M")

MIN_ML = 7
MAX_ML = 12
MIN_MOVE = 0.05
BID_LO = 0.55
BID_HI = 0.95
FEE_RATE = 0.07

# ---- in-memory state (restart-safe: unique(ticker) is the real guard) ----
_done = set()            # tickers already written
_pending_skip = {}       # ticker -> last skip reason seen in 12..7
_bid14 = {}              # ticker -> (bid14, gap_flag) or None when known-missing
_series = {}             # ticker -> series, so close_window doesn't need it passed in


def _f(snap, name):
    """Read a field off the parsed book whether it's a dict or an object."""
    if snap is None:
        return None
    if isinstance(snap, dict):
        v = snap.get(name)
    else:
        v = getattr(snap, name, None)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ask_from(snap):
    """ask = yes_bid + spread, which is the same as 1 - no_bid.
    parse_book() returns the key as "spread"; if this is ever called on a
    depth_minute row instead of a live snap, it will be "spread_close" there.
    """
    yes_bid = _f(snap, "yes_bid")
    spread = _f(snap, "spread")
    if spread is None:
        spread = _f(snap, "spread_close")
    if yes_bid is not None and spread is not None:
        return round(yes_bid + spread, 4), spread
    no_bid = _f(snap, "no_bid")
    if yes_bid is not None and no_bid is not None:
        ask = round(1.0 - no_bid, 4)
        return ask, round(ask - yes_bid, 4)
    return None, None


def _load_bid14(engine, ticker):
    """First minute-14 row for this ticker. None means 'not in DB yet'."""
    if ticker in _bid14:
        return _bid14[ticker]
    sql = text(
        "select yes_bid, gap_flag from depth_minute "
        "where ticker = :t and minutes_left = 14 "
        "order by minute_ts limit 1"
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"t": ticker}).fetchone()
    if row is None:
        return None  # not cached — window may still be filling in
    bid, gap = row[0], row[1]
    val = (float(bid) if bid is not None else None, bool(gap) if gap is not None else False)
    _bid14[ticker] = val
    return val


def _insert(engine, **kw):
    sql = text(
        "insert into paper_upcont "
        "(ticker, series, decision_ts, minutes_left, bid14, bid_at_decision, "
        " spread_at_decision, ask_at_decision, ask_observed, slippage, "
        " qualified, skipped_reason, fee_modeled) "
        "values (:ticker, :series, now(), :minutes_left, :bid14, :bid_at_decision, "
        " :spread_at_decision, :ask_at_decision, :ask_observed, :slippage, "
        " :qualified, :skipped_reason, :fee_modeled) "
        "on conflict (ticker) do nothing"
    )
    params = {
        "ticker": None, "series": None, "minutes_left": None, "bid14": None,
        "bid_at_decision": None, "spread_at_decision": None, "ask_at_decision": None,
        "ask_observed": None, "slippage": None, "qualified": False,
        "skipped_reason": None, "fee_modeled": None,
    }
    params.update(kw)
    with engine.begin() as conn:
        conn.execute(sql, params)


def evaluate(engine, kalshi, series, ticker, snap, ml, close_dt=None):
    """
    Call once per successful parse_book, for PAPER_SERIES tickers with ml <= 12.
    Writes at most one row per ticker, ever.
    """
    if series not in PAPER_SERIES or ml is None:
        return
    if ticker in _done:
        return
    _series[ticker] = series

    # window has left the entry band without qualifying -> write the control row
    if ml < MIN_ML:
        reason = _pending_skip.pop(ticker, "no_signal")
        try:
            _insert(engine, ticker=ticker, series=series, minutes_left=ml,
                    qualified=False, skipped_reason=reason)
        except Exception:
            log.exception("paper: control insert failed for %s", ticker)
        _done.add(ticker)
        _bid14.pop(ticker, None)
        return

    if ml > MAX_ML:
        return

    base = _load_bid14(engine, ticker)
    if base is None:
        return  # minute-14 row not written yet; try again on the next poll
    bid14, gap_flag = base
    if bid14 is None or gap_flag:
        _pending_skip[ticker] = "no_bid14"
        return

    bid = _f(snap, "yes_bid")
    if bid is None:
        return

    if bid - bid14 < MIN_MOVE:
        _pending_skip[ticker] = "no_move"
        return
    if not (BID_LO <= bid <= BID_HI):
        _pending_skip[ticker] = "bid_out_of_band"
        return

    # qualified — second, fresh book read is the slippage measurement
    ask_cached, spread = _ask_from(snap)
    ask_obs = None
    try:
        from collector.kalshi import parse_book
        fresh = parse_book(kalshi.orderbook(ticker))
        ask_obs, _ = _ask_from(fresh)
    except Exception:
        log.exception("paper: fresh book read failed for %s", ticker)

    slippage = None
    if ask_obs is not None and ask_cached is not None:
        slippage = round(ask_obs - ask_cached, 4)

    fee_base = ask_obs if ask_obs is not None else ask_cached
    fee = round(FEE_RATE * fee_base * (1.0 - fee_base), 6) if fee_base is not None else None

    try:
        _insert(engine, ticker=ticker, series=series, minutes_left=ml,
                bid14=round(bid14, 4), bid_at_decision=round(bid, 4),
                spread_at_decision=spread, ask_at_decision=ask_cached,
                ask_observed=ask_obs, slippage=slippage,
                qualified=True, skipped_reason=None, fee_modeled=fee)
    except Exception:
        log.exception("paper: qualified insert failed for %s", ticker)
        return

    _done.add(ticker)
    _pending_skip.pop(ticker, None)
    _bid14.pop(ticker, None)
    log.info("paper: ENTRY %s ml=%s bid=%.3f ask=%s slip=%s", ticker, ml, bid, ask_obs, slippage)


def close_window(engine, ticker):
    """
    Called when a ticker drops out of the active list. Writes the control row
    for a window that never qualified and never reached minute 6.
    Safe to call for any ticker — no-ops unless we were tracking it.
    """
    if ticker in _done:
        return
    series = _series.get(ticker)
    if series not in PAPER_SERIES:
        return  # never a paper series, or never seen in the 12..7 band
    reason = _pending_skip.get(ticker, "no_signal")
    try:
        _insert(engine, ticker=ticker, series=series,
                qualified=False, skipped_reason=reason)
        _done.add(ticker)
    except Exception:
        log.exception("paper: close-window insert failed for %s", ticker)


def forget(ticker):
    """Free per-ticker memory. Call AFTER close_window, never before."""
    _done.discard(ticker)
    _pending_skip.pop(ticker, None)
    _bid14.pop(ticker, None)
    _series.pop(ticker, None)

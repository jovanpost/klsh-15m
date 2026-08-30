"""Postgres writes. Do not DDL from here."""

import json

from sqlalchemy import create_engine, text

INSERT_MINUTE = text(
    """
    insert into depth_minute (
        series, ticker, minute_ts, minutes_left,
        yes_bid, yes_bid_size, no_bid, no_bid_size,
        yes_depth_total, no_depth_total,
        yes_levels, no_levels,
        spread_close, spread_min, spread_max,
        yes_bid_first, no_bid_first, yes_bid_last, no_bid_last,
        samples, gap_flag,
        yes_book, no_book, book_sample_ts, book_truncated
    ) values (
        :series, :ticker, :minute_ts, :minutes_left,
        :yes_bid, :yes_bid_size, :no_bid, :no_bid_size,
        :yes_depth_total, :no_depth_total,
        :yes_levels, :no_levels,
        :spread_close, :spread_min, :spread_max,
        :yes_bid_first, :no_bid_first, :yes_bid_last, :no_bid_last,
        :samples, :gap_flag,
        CAST(:yes_book AS jsonb), CAST(:no_book AS jsonb),
        :book_sample_ts, :book_truncated
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
        gap_flag = excluded.gap_flag,
        yes_book = coalesce(excluded.yes_book, depth_minute.yes_book),
        no_book = coalesce(excluded.no_book, depth_minute.no_book),
        book_sample_ts = coalesce(excluded.book_sample_ts, depth_minute.book_sample_ts),
        book_truncated = coalesce(excluded.book_truncated, depth_minute.book_truncated)
    """
)

INSERT_SAMPLE = text(
    """
    insert into depth_sample (
        series, ticker, sample_ts, minutes_left,
        yes_bid, yes_bid_size, no_bid, no_bid_size,
        yes_depth_total, no_depth_total, yes_levels, no_levels, spread
    ) values (
        :series, :ticker, :sample_ts, :minutes_left,
        :yes_bid, :yes_bid_size, :no_bid, :no_bid_size,
        :yes_depth_total, :no_depth_total, :yes_levels, :no_levels, :spread
    )
    on conflict (ticker, sample_ts) do nothing
    """
)

UPSERT_SETTLEMENT = text(
    """
    insert into depth_settlement (
        ticker, series, close_time, result,
        expiration_value, floor_strike, volume_fp,
        fetched_at, attempts
    ) values (
        :ticker, :series, :close_time, :result,
        :expiration_value, :floor_strike, :volume_fp,
        now(), :attempts
    )
    on conflict (ticker) do update set
        result = coalesce(excluded.result, depth_settlement.result),
        expiration_value = coalesce(excluded.expiration_value, depth_settlement.expiration_value),
        floor_strike = coalesce(excluded.floor_strike, depth_settlement.floor_strike),
        volume_fp = coalesce(excluded.volume_fp, depth_settlement.volume_fp),
        fetched_at = now(),
        attempts = excluded.attempts
    """
)

PENDING_SETTLEMENT = text(
    """
    select distinct m.ticker, m.series, max(m.minute_ts) as last_minute
    from depth_minute m
    left join depth_settlement s on s.ticker = m.ticker
    where m.minute_ts >= now() - interval '2 hours'
      and (s.result is null or s.result = '')
      and coalesce(s.attempts, 0) < 12
    group by m.ticker, m.series
    """
)


def make_engine(database_url):
    url = database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)


def _json(value):
    if value is None:
        return None
    return json.dumps(value)


def insert_minute(engine, row):
    payload = dict(row)
    payload["yes_book"] = _json(payload.get("yes_book"))
    payload["no_book"] = _json(payload.get("no_book"))
    with engine.begin() as conn:
        conn.execute(INSERT_MINUTE, payload)


def insert_sample(engine, row):
    with engine.begin() as conn:
        conn.execute(INSERT_SAMPLE, row)


def pending_settlement(engine):
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(PENDING_SETTLEMENT)]


def upsert_settlement(engine, row):
    with engine.begin() as conn:
        conn.execute(UPSERT_SETTLEMENT, row)


def rows_today(engine):
    sql = text(
        """
        select series,
               count(*) as rows,
               sum(case when gap_flag then 1 else 0 end) as gaps,
               max(minute_ts) as last_minute
        from depth_minute
        where minute_ts >= date_trunc('day', now() at time zone 'utc')
        group by series
        order by series
        """
    )
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(sql)]


def recent_rows(engine, limit=25):
    sql = text(
        """
        select series, ticker, minute_ts, minutes_left,
               yes_bid, no_bid, spread_close,
               yes_depth_total, no_depth_total, samples, gap_flag
        from depth_minute
        order by minute_ts desc, id desc
        limit :limit
        """
    )
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(sql, {"limit": limit})]


def total_rows(engine):
    with engine.connect() as conn:
        return conn.execute(text("select count(*) from depth_minute")).scalar()
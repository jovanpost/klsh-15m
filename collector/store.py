"""
Postgres access. The table is revision 2 of sql/schema.sql and is already
live. This file must not create, alter, or drop columns.

Supabase note: DATABASE_URL must be the TRANSACTION POOLER string (port
6543, aws-0-...pooler.supabase.com). The direct db.*.supabase.co host is
IPv6-only and unreachable from Streamlit Cloud.
"""

from sqlalchemy import create_engine, text

INSERT_SQL = text(
    """
    insert into depth_minute (
        series, ticker, minute_ts, minutes_left,
        yes_bid, yes_bid_size, no_bid, no_bid_size,
        yes_depth_total, no_depth_total,
        yes_levels, no_levels,
        spread_close, spread_min, spread_max,
        yes_bid_first, no_bid_first, yes_bid_last, no_bid_last,
        samples, gap_flag
    ) values (
        :series, :ticker, :minute_ts, :minutes_left,
        :yes_bid, :yes_bid_size, :no_bid, :no_bid_size,
        :yes_depth_total, :no_depth_total,
        :yes_levels, :no_levels,
        :spread_close, :spread_min, :spread_max,
        :yes_bid_first, :no_bid_first, :yes_bid_last, :no_bid_last,
        :samples, :gap_flag
    )
    on conflict (ticker, minute_ts) do nothing
    """
)


def make_engine(database_url):
    url = database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)


def insert_minute(engine, row):
    with engine.begin() as conn:
        conn.execute(INSERT_SQL, row)


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

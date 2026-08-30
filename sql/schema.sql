-- 15-minute markets depth logger — one table, one row per market-minute.
-- Create this by hand in the Supabase SQL editor before running anything.
--
-- Naming note: Kalshi's book is BIDS ONLY on both sides. "yes" columns are
-- YES bids, "no" columns are NO bids. A YES bid at X is a NO ask at 100-X.
-- Do not rename these to bid/ask.

create table if not exists depth_minute (
    id              bigserial primary key,

    -- identity
    series          text        not null,          -- KXGOLD15M
    ticker          text        not null,          -- full market ticker
    minute_ts       timestamptz not null,          -- start of the minute, UTC
    minutes_left    integer,                       -- to market close; null if unknown

    -- top of book, close of minute (cents)
    yes_bid         integer,
    yes_bid_size    integer,
    no_bid          integer,
    no_bid_size     integer,

    -- total resting contracts across all levels, close of minute
    yes_depth_total integer,
    no_depth_total  integer,

    -- spread in cents = 100 - yes_bid - no_bid
    spread_close    integer,
    spread_min      integer,
    spread_max      integer,

    -- first and last sample of the minute (detect intra-minute drift)
    yes_bid_first   integer,
    no_bid_first    integer,
    yes_bid_last    integer,
    no_bid_last     integer,

    -- data quality
    samples         integer     not null,          -- expect 12 at 5s polling
    gap_flag        boolean     not null default false,

    inserted_at     timestamptz not null default now(),

    unique (ticker, minute_ts)
);

create index if not exists depth_minute_series_ts_idx
    on depth_minute (series, minute_ts desc);

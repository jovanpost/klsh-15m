-- 15-minute markets depth logger — one row per market-minute.
-- Revision 2: corrected after the live probe.
--   * prices are DOLLAR decimals to 4 places (deci-cent ticks), not integer cents
--   * sizes are FRACTIONAL contracts ("14.10"), not integers
--   * the book lives under orderbook_fp, keys yes_dollars / no_dollars
--   * each level is [price_string, size_string], sorted LOW -> HIGH,
--     so the BEST bid is the LAST element of the array
--
-- Kalshi's book is BIDS ONLY on both sides. "yes_*" columns are YES bids,
-- "no_*" columns are NO bids. A YES bid at X is a NO ask at 1 - X.
-- Do not rename these to bid/ask.
--
-- Spread (dollars) = 1 - yes_bid - no_bid.

create table if not exists depth_minute (
    id              bigserial primary key,

    -- identity
    series          text        not null,          -- KXDOGE15M
    ticker          text        not null,          -- full market ticker
    minute_ts       timestamptz not null,          -- start of the minute, UTC
    minutes_left    integer,                       -- to close_time; null if unknown

    -- top of book at close of minute (dollars / contracts)
    yes_bid         numeric(6,4),
    yes_bid_size    numeric(14,2),
    no_bid          numeric(6,4),
    no_bid_size     numeric(14,2),

    -- total resting size across all levels, close of minute
    yes_depth_total numeric(16,2),
    no_depth_total  numeric(16,2),

    -- how many price levels were resting (thin book vs deep book — H1)
    yes_levels      integer,
    no_levels       integer,

    -- spread within the minute, dollars
    spread_close    numeric(6,4),
    spread_min      numeric(6,4),
    spread_max      numeric(6,4),

    -- first and last sample of the minute (intra-minute drift)
    yes_bid_first   numeric(6,4),
    no_bid_first    numeric(6,4),
    yes_bid_last    numeric(6,4),
    no_bid_last     numeric(6,4),

    -- data quality
    samples         integer     not null,          -- expect 12 at 5s polling
    gap_flag        boolean     not null default false,

    inserted_at     timestamptz not null default now(),

    unique (ticker, minute_ts)
);

create index if not exists depth_minute_series_ts_idx
    on depth_minute (series, minute_ts desc);

"""
Entry point. The page is a status board; the real work happens in one
background thread started once via @st.cache_resource.

Why @st.cache_resource: Streamlit reruns this whole file on every page load,
including every keep-alive ping. Anything held in a module-level variable is
rebuilt and silently lost. cache_resource survives reruns, so the thread is
started once and the same object is handed back afterwards.

Read only. No orders, no controls, no Telegram.
"""

import pandas as pd
import streamlit as st
from sqlalchemy import text

from collector.config import POLL_SECONDS, load_config
from collector.runner import Collector
from collector.store import recent_rows, rows_today, total_rows


@st.cache_resource
def get_collector():
    cfg = load_config()
    if not cfg.ready:
        return None, cfg
    c = Collector(cfg)
    c.start()
    return c, cfg


collector, cfg = get_collector()

# Keep-alive ping: start the thread (the line above already did), then bail
# out before rendering anything. Must come AFTER get_collector().
if st.query_params.get("ping") == "true":
    st.write("ok")
    st.stop()

st.set_page_config(page_title="15m depth logger", layout="wide")
st.title("15-minute markets — depth logger")
st.caption("Read-only order book collector. Places no orders.")

if collector is None:
    st.error("Missing secrets: " + ", ".join(cfg.missing()))
    st.stop()

status = collector.status()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Collector", "alive" if status["alive"] else "DEAD")
since = status["seconds_since_poll"]
c2.metric("Last poll", f"{since:.0f}s ago" if since is not None else "never")
c3.metric("Rows this session", status["rows_written"])
c4.metric("Poll errors", status["poll_errors"])

if since is not None and since > POLL_SECONDS * 6:
    st.warning("Polling has stalled. The app may have been asleep.")

st.subheader("Today")
try:
    today = rows_today(collector.engine)
    if today:
        st.dataframe(pd.DataFrame(today), width="stretch", hide_index=True)
        gaps = sum(int(r["gaps"] or 0) for r in today)
        st.caption(
            f"{sum(int(r['rows']) for r in today)} rows today, {gaps} flagged as gaps "
            f"(fewer than {status['expected_samples']} samples in the minute). "
            f"{total_rows(collector.engine)} rows all time."
        )
    else:
        st.info("No rows yet today.")
except Exception as exc:
    st.error(f"Database read failed: {exc}")

st.subheader("Currently open market per series")
active = status["active"]
if active:
    st.dataframe(
        pd.DataFrame(
            [{"series": s, "ticker": t} for s, t in sorted(active.items())]
        ),
        width="stretch",
        hide_index=True,
    )
else:
    st.info("No open markets found. Some series do not run every hour.")

st.subheader("Last rows written")
try:
    rows = recent_rows(collector.engine)
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
except Exception as exc:
    st.error(f"Database read failed: {exc}")

if status["last_error"]:
    st.caption(f"Last error: {status['last_error']}")

st.subheader("paper up-continuation")
st.caption("Paper only. Places no orders. P&L below is illustrative: $1 notional per trade, modeled fee only, no live slippage applied to price.")

PNL_SQL = """
with trades as (
    select
        ticker, series, ask_observed, result,
        case
            when result = 'yes' then (1.0 / ask_observed) - 1.0 - 0.07 * (1 - ask_observed)
            when result = 'no'  then -1.0 - 0.07 * (1 - ask_observed)
            else null
        end as pnl_dollars
    from paper_upcont
    where qualified
)
select
    series,
    count(*) filter (where result is not null) as settled,
    count(*) filter (where result = 'yes') as wins,
    count(*) filter (where result = 'no') as losses,
    avg(ask_observed) filter (where result is not null) as avg_entry_price,
    sum(pnl_dollars) as total_pnl_dollars,
    avg(pnl_dollars) as avg_pnl_per_trade
from trades
group by grouping sets ((series), ())
order by series nulls last
"""

try:
    with collector.engine.connect() as conn:
        totals = conn.execute(
            text(
                "select count(*) filter (where qualified) as qualified, "
                "       count(*) filter (where not qualified) as skipped, "
                "       max(decision_ts) as last_decision, "
                "       avg(slippage) filter (where qualified) as mean_slip "
                "from paper_upcont"
            )
        ).fetchone()
        pnl_rows = [dict(r._mapping) for r in conn.execute(text(PNL_SQL))]

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Qualifying rows", int(totals.qualified or 0))
    p2.metric("Skip rows", int(totals.skipped or 0))
    p3.metric("Last decision", str(totals.last_decision)[:19] if totals.last_decision else "never")
    p4.metric(
        "Mean slippage",
        f"{float(totals.mean_slip):+.4f}" if totals.mean_slip is not None else "n/a",
    )

    overall = next((r for r in pnl_rows if r["series"] is None), None)
    per_series = [r for r in pnl_rows if r["series"] is not None]

    if overall and overall["settled"]:
        n = int(overall["settled"])
        wins = int(overall["wins"] or 0)
        losses = int(overall["losses"] or 0)
        win_rate = 100.0 * wins / n if n else 0.0
        avg_entry = float(overall["avg_entry_price"] or 0.0)
        total_pnl = float(overall["total_pnl_dollars"] or 0.0)
        avg_pnl_pct = 100.0 * float(overall["avg_pnl_per_trade"] or 0.0)

        st.markdown("**Overall — $1 notional per trade**")
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Settled", n)
        m2.metric("Wins / Losses", f"{wins} / {losses}")
        m3.metric("Win rate", f"{win_rate:.1f}%")
        m4.metric("Avg entry price", f"{avg_entry:.4f}")
        m5.metric("Total P&L ($1/trade)", f"${total_pnl:+.2f}")
        m6.metric("Avg return / trade", f"{avg_pnl_pct:+.2f}%")

        st.markdown("**By series**")
        table = []
        for r in per_series:
            n_s = int(r["settled"] or 0)
            w_s = int(r["wins"] or 0)
            l_s = int(r["losses"] or 0)
            table.append(
                {
                    "series": r["series"],
                    "settled": n_s,
                    "wins": w_s,
                    "losses": l_s,
                    "win_rate_pct": round(100.0 * w_s / n_s, 1) if n_s else None,
                    "avg_entry_price": round(float(r["avg_entry_price"]), 4) if r["avg_entry_price"] is not None else None,
                    "total_pnl_$": round(float(r["total_pnl_dollars"]), 2) if r["total_pnl_dollars"] is not None else None,
                    "avg_return_pct": round(100.0 * float(r["avg_pnl_per_trade"]), 2) if r["avg_pnl_per_trade"] is not None else None,
                }
            )
        st.dataframe(pd.DataFrame(table), width="stretch", hide_index=True)
        st.caption(
            "n is still far below the spec's minimum (600/series or 1,800 pooled, 3+ weeks). "
            "Treat everything above as a progress check, not a result."
        )

        # ---- copyable markdown report ----
        report_lines = [
            f"### paper_upcont — {str(totals.last_decision)[:19] if totals.last_decision else 'n/a'} UTC",
            "",
            f"- Qualifying rows: {int(totals.qualified or 0)}",
            f"- Skip rows: {int(totals.skipped or 0)}",
            f"- Mean slippage: {f'{float(totals.mean_slip):+.4f}' if totals.mean_slip is not None else 'n/a'}",
            "",
            "**Overall — $1 notional per trade**",
            "",
            f"- Settled: {n}",
            f"- Wins / Losses: {wins} / {losses}",
            f"- Win rate: {win_rate:.1f}%",
            f"- Avg entry price: {avg_entry:.4f}",
            f"- Total P&L ($1/trade): ${total_pnl:+.2f}",
            f"- Avg return / trade: {avg_pnl_pct:+.2f}%",
            "",
            "**By series**",
            "",
            "| series | settled | wins | losses | win_rate_pct | avg_entry_price | total_pnl_$ | avg_return_pct |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in table:
            wr = row["win_rate_pct"] if row["win_rate_pct"] is not None else ""
            ap = row["avg_entry_price"] if row["avg_entry_price"] is not None else ""
            tp = row["total_pnl_$"] if row["total_pnl_$"] is not None else ""
            ar = row["avg_return_pct"] if row["avg_return_pct"] is not None else ""
            report_lines.append(
                f"| {row['series']} | {row['settled']} | {row['wins']} | {row['losses']} | "
                f"{wr} | {ap} | {tp} | {ar} |"
            )
        report_lines += [
            "",
            "_n is still far below the spec's minimum (600/series or 1,800 pooled, 3+ weeks). "
            "Progress check, not a result._",
        ]
        report_md = "\n".join(report_lines)

        st.markdown("**Copy stats (markdown)** — hover the block, click the copy icon top-right")
        st.code(report_md, language="markdown")
    else:
        st.info("No settled qualifying trades yet.")
except Exception as exc:
    st.error(f"Paper table read failed: {exc}")

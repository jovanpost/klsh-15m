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
st.caption("Paper only. Places no orders.")
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
        settled = conn.execute(
            text(
                "select count(*) as n, "
                "       avg(case when result = 'yes' then 1.0 else 0.0 end) as realized, "
                "       avg(ask_observed) as avg_ask, "
                "       avg(fee_modeled) as avg_fee "
                "from paper_upcont "
                "where qualified and result is not null"
            )
        ).fetchone()

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Qualifying rows", int(totals.qualified or 0))
    p2.metric("Skip rows", int(totals.skipped or 0))
    p3.metric("Last decision", str(totals.last_decision)[:19] if totals.last_decision else "never")
    p4.metric(
        "Mean slippage",
        f"{float(totals.mean_slip):+.4f}" if totals.mean_slip is not None else "n/a",
    )

    n = int(settled.n or 0)
    if n and settled.realized is not None and settled.avg_ask is not None:
        realized = float(settled.realized)
        avg_ask = float(settled.avg_ask)
        fee = float(settled.avg_fee or 0.0)
        q1, q2, q3 = st.columns(3)
        q1.metric("Settled n", n)
        q2.metric("Realized vs avg ask", f"{realized:.3f} / {avg_ask:.3f}")
        q3.metric("Running edge after fee", f"{realized - avg_ask - fee:+.4f}")
    else:
        st.info("No settled paper trades yet.")
except Exception as exc:
    st.error(f"Paper table read failed: {exc}")

"""
Config. Reads Streamlit secrets first, falls back to environment variables
so scripts/ can run locally without Streamlit.

Only four secrets exist. Do not add more:
    KALSHI_KEY_ID, KALSHI_PRIVATE_KEY, DATABASE_URL, APP_URL
"""

import os
from dataclasses import dataclass, field


def _secret(name, default=None):
    try:
        import streamlit as st

        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


# Ranked by how often a late favourite has room to rest inside the spread.
# BTC is deliberately absent: 1c wide 96% of the time, nothing to rest inside.
SERIES = ["KXDOGE15M", "KXXRP15M", "KXSILVER15M", "KXSOL15M", "KXGOLD15M", "KXBTC15M"]

POLL_SECONDS = 5           # book snapshot cadence
DISCOVERY_SECONDS = 30     # how often to re-check which market is open
EXPECTED_SAMPLES = 12      # 60 / POLL_SECONDS
GAP_THRESHOLD = 10         # fewer samples than this in a minute -> gap_flag

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
API_PREFIX = "/trade-api/v2"


@dataclass
class Config:
    kalshi_key_id: str = ""
    kalshi_private_key: str = ""
    database_url: str = ""
    app_url: str = ""
    series: list = field(default_factory=lambda: list(SERIES))

    @property
    def ready(self):
        return bool(self.kalshi_key_id and self.kalshi_private_key and self.database_url)

    def missing(self):
        out = []
        if not self.kalshi_key_id:
            out.append("KALSHI_KEY_ID")
        if not self.kalshi_private_key:
            out.append("KALSHI_PRIVATE_KEY")
        if not self.database_url:
            out.append("DATABASE_URL")
        return out


def load_config():
    return Config(
        kalshi_key_id=_secret("KALSHI_KEY_ID", "") or "",
        kalshi_private_key=_secret("KALSHI_PRIVATE_KEY", "") or "",
        database_url=_secret("DATABASE_URL", "") or "",
        app_url=_secret("APP_URL", "") or "",
    )

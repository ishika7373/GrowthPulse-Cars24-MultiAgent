"""
data_loader.py
--------------
Single source of truth for the GrowthPulse CSV data.

Loads `growthpulse_campaigns.csv` and `growthpulse_adsets.csv` exactly once,
exposes typed pandas DataFrames, and provides safe lookup helpers used by
every specialist tool. Centralising data access here keeps tool functions
clean and ensures NaN / missing-row edge cases are handled in ONE place.

Cars24 dual-funnel campaign-type breakdown is also computed here so the
Account Summary panel in the UI can render it without re-reading the CSV.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# Resolve paths relative to the project root (NEVER hardcode absolutes).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMPAIGNS_CSV = os.path.join(BASE_DIR, "growthpulse_campaigns.csv")
ADSETS_CSV = os.path.join(BASE_DIR, "growthpulse_adsets.csv")

# Cars24 dual-funnel taxonomy (matches case study Section 9.1)
CAMPAIGN_TYPE_BREAKDOWN: Dict[str, List[str]] = {
    "Seller Acquisition": [f"NB{str(i).zfill(3)}" for i in range(1, 8)],     # NB001-NB007
    "Buyer Intent":       [f"NB{str(i).zfill(3)}" for i in range(8, 15)],    # NB008-NB014
    "Financing & EMI":    [f"NB{str(i).zfill(3)}" for i in range(15, 19)],   # NB015-NB018
    "Retargeting":        [f"NB{str(i).zfill(3)}" for i in range(19, 21)],   # NB019-NB020
    "Brand Awareness":    [f"NB{str(i).zfill(3)}" for i in range(21, 23)],   # NB021-NB022
}


@dataclass
class _DataStore:
    campaigns: pd.DataFrame
    adsets: pd.DataFrame

    def campaign(self, campaign_id: str) -> Optional[pd.Series]:
        """Return one campaign row or None if not found."""
        if not campaign_id:
            return None
        row = self.campaigns[self.campaigns["campaign_id"] == campaign_id.strip().upper()]
        return None if row.empty else row.iloc[0]

    def ad_sets_for(self, campaign_id: str) -> pd.DataFrame:
        return self.adsets[self.adsets["campaign_id"] == campaign_id.strip().upper()].copy()

    def campaign_type_of(self, campaign_id: str) -> str:
        for ctype, ids in CAMPAIGN_TYPE_BREAKDOWN.items():
            if campaign_id in ids:
                return ctype
        return "Unknown"


def _empty_store() -> _DataStore:
    """Empty fallback used when no demo CSVs exist on disk and no upload yet.
    Columns are hardcoded (not pulled from REQUIRED_*_COLS) because this
    function may run at import time, BEFORE those constants are defined."""
    campaign_cols = [
        "campaign_id", "campaign_name", "channel", "objective", "daily_budget",
        "spend_so_far", "impressions", "clicks", "ctr", "cpc", "roas",
        "target_roas", "frequency", "bid_strategy", "start_date", "status",
        "campaign_type",
    ]
    adset_cols = [
        "ad_set_id", "campaign_id", "ad_set_name", "audience_size", "reach",
        "frequency", "ad_set_spend", "ad_set_roas", "ctr", "top_creative_id",
        "audience_overlap_pct",
    ]
    return _DataStore(
        campaigns=pd.DataFrame(columns=campaign_cols),
        adsets=pd.DataFrame(columns=adset_cols),
    )


def _load() -> _DataStore:
    # If the demo CSVs aren't present, return an empty store so the app still
    # boots — every endpoint will simply return zero rows until the user
    # uploads data via the (optional) upload modal.
    if not os.path.exists(CAMPAIGNS_CSV) or not os.path.exists(ADSETS_CSV):
        return _empty_store()

    campaigns = pd.read_csv(CAMPAIGNS_CSV)
    adsets = pd.read_csv(ADSETS_CSV)

    # Normalise IDs to upper-case for safe lookups
    campaigns["campaign_id"] = campaigns["campaign_id"].astype(str).str.strip().str.upper()
    adsets["campaign_id"] = adsets["campaign_id"].astype(str).str.strip().str.upper()
    adsets["ad_set_id"] = adsets["ad_set_id"].astype(str).str.strip().str.upper()

    # Dates
    campaigns["start_date"] = pd.to_datetime(campaigns["start_date"], errors="coerce")

    # Replace NaN numerics with 0 only for spend-side metrics; keep ROAS NaN distinct.
    for col in ["spend_so_far", "impressions", "clicks", "ad_set_spend", "reach", "audience_size"]:
        if col in campaigns.columns:
            campaigns[col] = campaigns[col].fillna(0)
        if col in adsets.columns:
            adsets[col] = adsets[col].fillna(0)

    # Derive a per-campaign Cars24 funnel-type column for filtering in UI/Briefing
    def _ctype(cid: str) -> str:
        for ctype, ids in CAMPAIGN_TYPE_BREAKDOWN.items():
            if cid in ids:
                return ctype
        return "Unknown"

    campaigns["campaign_type"] = campaigns["campaign_id"].apply(_ctype)

    return _DataStore(campaigns=campaigns, adsets=adsets)


# Singleton store — every tool imports DATA once at module load.
DATA = _load()

# Label shown in the dashboard so users know whether the active dataset
# is the bundled demo or their own upload.
DATA_SOURCE: Dict[str, Any] = {
    "type": "demo",
    "label": "Demo dataset (Cars24 sample)",
    "platform": "demo",
}


def swap_in_dataframes(campaigns: pd.DataFrame, adsets: pd.DataFrame, label: str, platform: str = "uploaded") -> Dict[str, Any]:
    """
    Replace the active dataset in-place with already-normalised DataFrames
    (typically produced by ads_analyzer.normalise_files). All importers of
    `DATA` see the new data immediately.
    """
    global DATA_SOURCE
    DATA.campaigns = campaigns
    DATA.adsets = adsets
    DATA_SOURCE = {"type": "uploaded", "label": label, "platform": platform}
    return {
        "ok": True,
        "label": label,
        "platform": platform,
        "campaigns_rows": int(len(campaigns)),
        "adsets_rows": int(len(adsets)),
    }


def reset_to_demo() -> Dict[str, Any]:
    """Reload the bundled Cars24 demo CSVs."""
    global DATA_SOURCE
    fresh = _load()
    DATA.campaigns = fresh.campaigns
    DATA.adsets = fresh.adsets
    DATA_SOURCE = {"type": "demo", "label": "Demo dataset (Cars24 sample)", "platform": "demo"}
    return {"ok": True, "label": DATA_SOURCE["label"]}


# ---------- Helper utilities used by tools ----------

def days_elapsed(start: pd.Timestamp, today: Optional[date] = None) -> int:
    """
    Days the campaign has been live in the CURRENT REPORTING WINDOW.

    Note on the supplied dataset: although the column is documented as
    "Total spend in current billing period", the values supplied in
    growthpulse_campaigns.csv are clearly one-day spend snapshots
    (spend_so_far is roughly equal to daily_budget for almost every row).
    To keep pacing math meaningful, we treat the snapshot as a 1-day
    reporting window when the wall-clock has drifted far past the
    campaign start date. For freshly-started campaigns we honour actual
    elapsed days (max 30 — billing periods are monthly).
    """
    today = today or date.today()
    if pd.isna(start):
        return 1
    delta = (datetime.combine(today, datetime.min.time()) - start.to_pydatetime()).days
    if delta <= 0:
        return 1
    if delta > 30:
        # Sample-data snapshot: treat spend_so_far as a one-day reporting window.
        return 1
    return delta


def safe_div(num: float, den: float, default: float = 0.0) -> float:
    if den is None or den == 0 or pd.isna(den):
        return default
    return float(num) / float(den)


def account_summary() -> Dict[str, Any]:
    """KPI snapshot used by the always-visible Account Summary panel."""
    df = DATA.campaigns
    active = df[df["status"].str.lower() == "active"]
    critical = active[(active["ctr"] < 0.8) | (active["frequency"] > 5) | (active["roas"] < 1.0)]

    sellers = active[active["campaign_type"] == "Seller Acquisition"]
    buyers = active[active["campaign_type"] == "Buyer Intent"]

    blended_seller_cpl = safe_div(sellers["spend_so_far"].sum(), sellers["clicks"].sum())
    blended_buyer_roas = safe_div(
        (buyers["roas"] * buyers["spend_so_far"]).sum(), buyers["spend_so_far"].sum()
    )

    return {
        "total_active_campaigns": int(len(active)),
        "campaigns_with_critical_status": int(len(critical)),
        "blended_seller_cpl_inr": round(blended_seller_cpl, 2),
        "blended_buyer_roas": round(blended_buyer_roas, 2),
        "total_daily_budget_inr": float(active["daily_budget"].sum()),
        "total_spend_so_far_inr": float(active["spend_so_far"].sum()),
        "channels": active["channel"].value_counts().to_dict(),
        "campaign_type_breakdown": {
            ct: int(((active["campaign_type"] == ct)).sum())
            for ct in CAMPAIGN_TYPE_BREAKDOWN.keys()
        },
    }

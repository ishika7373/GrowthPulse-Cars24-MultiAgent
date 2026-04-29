"""
ads_analyzer.py
---------------
Pure-Python pipeline that ingests a Google Ads or Meta Ads export
(CSV or XLSX) and normalises it to GrowthPulse's internal campaign +
ad-set schema. Once normalised, the existing 8 specialist tools and
the multi-agent system work on the user's data without any other change.

How auto-detection works
------------------------
Both platforms have very different column conventions in their default
exports. We sniff the column headers and pick whichever mapping has the
most matches. If neither platform's headers are recognised we treat the
file as 'generic' — any column that already matches the GrowthPulse
schema is kept verbatim.

After mapping we synthesise the columns the rest of the system requires
(target_roas, status, campaign_type, audience_size for ad sets, etc.)
using sensible defaults, so the existing tools never see NaN.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------- Platform header maps ----------------
# Each entry: { "INTERNAL_COL": ["possible source column", ...] }

GOOGLE_ADS_CAMPAIGN_MAP: Dict[str, List[str]] = {
    "campaign_id":   ["Campaign ID", "campaign_id", "Campaign id"],
    "campaign_name": ["Campaign", "campaign_name", "Campaign Name"],
    "channel":       ["Advertising Channel", "Network", "channel"],
    "objective":     ["Campaign type", "Campaign Type", "objective"],
    "daily_budget":  ["Avg. Daily Budget", "Budget", "Daily budget", "daily_budget"],
    "spend_so_far":  ["Cost", "Total cost", "Spend", "Amount spent (INR)", "amount_spent"],
    "impressions":   ["Impr.", "Impressions", "impressions"],
    "clicks":        ["Clicks", "clicks"],
    "ctr":           ["CTR", "Click-through rate", "ctr"],
    "cpc":           ["Avg. CPC", "Cost per click", "cpc", "Average CPC"],
    "roas":          ["Conv. value / cost", "ROAS", "Return on Ad Spend", "roas"],
    "target_roas":   ["Target ROAS", "target_roas"],
    "frequency":     ["Avg. Frequency", "Frequency", "frequency"],
    "bid_strategy":  ["Bid Strategy Type", "bid_strategy", "Bid strategy"],
    "start_date":    ["Start date", "start_date", "Start Date"],
    "status":        ["Campaign state", "Status", "status"],
}

META_ADS_CAMPAIGN_MAP: Dict[str, List[str]] = {
    "campaign_id":   ["Campaign ID", "campaign_id"],
    "campaign_name": ["Campaign name", "Campaign Name", "campaign_name"],
    "channel":       ["Platform", "channel"],
    "objective":     ["Objective", "Campaign objective", "objective"],
    "daily_budget":  ["Daily budget", "daily_budget", "Budget"],
    "spend_so_far":  ["Amount spent (INR)", "Amount spent (USD)", "Amount spent", "spend_so_far"],
    "impressions":   ["Impressions", "impressions"],
    "clicks":        ["Link clicks", "Clicks", "Clicks (all)", "clicks"],
    "ctr":           ["CTR (link click-through rate)", "CTR (all)", "CTR (%)", "ctr"],
    "cpc":           ["CPC (cost per link click)", "CPC (all)", "Cost per click", "cpc"],
    "roas":          ["Purchase ROAS (return on ad spend)", "Website purchase ROAS", "ROAS", "roas"],
    "target_roas":   ["target_roas"],
    "frequency":     ["Frequency", "frequency"],
    "bid_strategy":  ["Bid strategy", "bid_strategy"],
    "start_date":    ["Reporting starts", "Start date", "start_date"],
    "status":        ["Delivery status", "Status", "status"],
}


GOOGLE_ADS_ADSET_MAP: Dict[str, List[str]] = {
    "ad_set_id":            ["Ad group ID", "ad_set_id"],
    "campaign_id":          ["Campaign ID", "campaign_id"],
    "ad_set_name":          ["Ad group", "Ad group name", "ad_set_name"],
    "audience_size":        ["Audience size", "audience_size"],
    "reach":                ["Reach", "Unique users", "reach"],
    "frequency":            ["Frequency", "frequency"],
    "ad_set_spend":         ["Cost", "Spend", "ad_set_spend"],
    "ad_set_roas":          ["Conv. value / cost", "ROAS", "ad_set_roas"],
    "ctr":                  ["CTR", "ctr"],
    "top_creative_id":      ["Top creative ID", "top_creative_id"],
    "audience_overlap_pct": ["Audience overlap %", "audience_overlap_pct"],
}

META_ADS_ADSET_MAP: Dict[str, List[str]] = {
    "ad_set_id":            ["Ad set ID", "Ad Set ID", "ad_set_id"],
    "campaign_id":          ["Campaign ID", "campaign_id"],
    "ad_set_name":          ["Ad set name", "Ad Set Name", "ad_set_name"],
    "audience_size":        ["Audience size", "audience_size"],
    "reach":                ["Reach", "reach"],
    "frequency":            ["Frequency", "frequency"],
    "ad_set_spend":         ["Amount spent (INR)", "Amount spent", "ad_set_spend"],
    "ad_set_roas":          ["Purchase ROAS", "ROAS", "ad_set_roas"],
    "ctr":                  ["CTR (link click-through rate)", "CTR (%)", "ctr"],
    "top_creative_id":      ["Ad ID", "top_creative_id"],
    "audience_overlap_pct": ["Audience overlap %", "audience_overlap_pct"],
}


@dataclass
class DetectionResult:
    platform: str        # "google" / "meta" / "generic"
    score: int           # number of header matches
    detail: str          # short string for UI display


# ---------------- Helpers ----------------

def _read_any(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(path)
    return pd.read_csv(path)


def _detect_platform(headers: List[str], maps: List[Tuple[str, Dict[str, List[str]]]]) -> DetectionResult:
    """Score each platform's column-map against the file's headers."""
    norm_headers = {h.strip(): h for h in headers}
    best = DetectionResult("generic", 0, "Generic CSV — using GrowthPulse schema as-is")
    for name, mapping in maps:
        score = 0
        for _, candidates in mapping.items():
            if any(c in norm_headers for c in candidates):
                score += 1
        if score > best.score:
            best = DetectionResult(name, score, f"{name.title()} Ads format detected — matched {score} columns")
    return best


def _apply_mapping(df: pd.DataFrame, mapping: Dict[str, List[str]]) -> pd.DataFrame:
    """Return a new DataFrame with INTERNAL column names where source columns matched."""
    out = pd.DataFrame()
    for internal, candidates in mapping.items():
        for c in candidates:
            if c in df.columns:
                out[internal] = df[c]
                break
    return out


def _coerce_percentage(series: pd.Series) -> pd.Series:
    """CTR exports often look like '1.23%' — strip the % and parse to float."""
    return pd.to_numeric(series.astype(str).str.replace("%", "", regex=False), errors="coerce")


def _coerce_currency(series: pd.Series) -> pd.Series:
    """Strip currency symbols/commas: 'INR 1,200', '₹1200', '$15.00' -> 1200/15.0"""
    return pd.to_numeric(
        series.astype(str)
              .str.replace(r"[^\d.\-]", "", regex=True),
        errors="coerce",
    )


def _ensure_required_columns(campaigns: pd.DataFrame, adsets: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fill missing-but-required columns with defensible defaults so the
    existing tools never see NaN/missing."""
    # Campaign IDs / names — must exist
    if "campaign_id" not in campaigns.columns:
        campaigns["campaign_id"] = [f"CMP{str(i).zfill(3)}" for i in range(1, len(campaigns) + 1)]
    if "campaign_name" not in campaigns.columns:
        campaigns["campaign_name"] = campaigns["campaign_id"].astype(str)

    # Numerical defaults
    for col, default in [
        ("daily_budget", 5000.0),
        ("spend_so_far", 0.0),
        ("impressions",  0),
        ("clicks",       0),
        ("ctr",          0.0),
        ("cpc",          0.0),
        ("roas",         0.0),
        ("target_roas",  2.0),
        ("frequency",    1.0),
    ]:
        if col not in campaigns.columns:
            campaigns[col] = default
        else:
            if col in {"ctr"}:
                campaigns[col] = _coerce_percentage(campaigns[col]).fillna(default)
            elif col in {"daily_budget", "spend_so_far", "cpc"}:
                campaigns[col] = _coerce_currency(campaigns[col]).fillna(default)
            else:
                campaigns[col] = pd.to_numeric(campaigns[col], errors="coerce").fillna(default)

    # Strings / metadata defaults
    for col, default in [
        ("channel",       "Unknown"),
        ("objective",     "Conversions"),
        ("bid_strategy",  "Manual CPC"),
        ("status",        "Active"),
    ]:
        if col not in campaigns.columns:
            campaigns[col] = default
        else:
            campaigns[col] = campaigns[col].fillna(default).astype(str)

    if "start_date" not in campaigns.columns:
        campaigns["start_date"] = pd.Timestamp.today().normalize() - pd.Timedelta(days=14)
    else:
        campaigns["start_date"] = pd.to_datetime(campaigns["start_date"], errors="coerce")

    # Cars24-style funnel taxonomy doesn't apply — assign by objective heuristic.
    def _ctype(row: pd.Series) -> str:
        obj = str(row.get("objective", "")).lower()
        name = str(row.get("campaign_name", "")).lower()
        if any(k in obj for k in ("lead", "sell")) or "sell" in name:
            return "Seller Acquisition"
        if "awareness" in obj or "brand" in name:
            return "Brand Awareness"
        if any(k in name for k in ("emi", "loan", "finance")):
            return "Financing & EMI"
        if any(k in name for k in ("retarget", "rlsa", "abandoner")):
            return "Retargeting"
        return "Buyer Intent"
    campaigns["campaign_type"] = campaigns.apply(_ctype, axis=1)

    # ----- Ad sets -----
    if "ad_set_id" not in adsets.columns:
        adsets["ad_set_id"] = [f"AS{str(i).zfill(3)}" for i in range(1, len(adsets) + 1)]
    if "campaign_id" not in adsets.columns and not campaigns.empty:
        # Best effort: assign each ad set to the first campaign
        adsets["campaign_id"] = campaigns["campaign_id"].iloc[0]
    if "ad_set_name" not in adsets.columns:
        adsets["ad_set_name"] = adsets["ad_set_id"].astype(str)

    for col, default in [
        ("audience_size",        1_000_000),
        ("reach",                100_000),
        ("frequency",            1.0),
        ("ad_set_spend",         0.0),
        ("ad_set_roas",          0.0),
        ("ctr",                  0.0),
        ("audience_overlap_pct", 0.0),
    ]:
        if col not in adsets.columns:
            adsets[col] = default
        else:
            if col == "ctr":
                adsets[col] = _coerce_percentage(adsets[col]).fillna(default)
            elif col == "ad_set_spend":
                adsets[col] = _coerce_currency(adsets[col]).fillna(default)
            else:
                adsets[col] = pd.to_numeric(adsets[col], errors="coerce").fillna(default)

    if "top_creative_id" not in adsets.columns:
        adsets["top_creative_id"] = [f"CR{str(i).zfill(3)}" for i in range(1, len(adsets) + 1)]

    # Normalise IDs to upper-case strings (matches data_loader)
    campaigns["campaign_id"] = campaigns["campaign_id"].astype(str).str.strip().str.upper()
    adsets["campaign_id"]    = adsets["campaign_id"].astype(str).str.strip().str.upper()
    adsets["ad_set_id"]      = adsets["ad_set_id"].astype(str).str.strip().str.upper()

    return campaigns, adsets


# ---------------- Public API ----------------

def normalise_files(campaigns_path: str, adsets_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Read one or two ad-platform exports and produce normalised
    GrowthPulse DataFrames + a detection summary.

    `adsets_path` is optional — many users only have a campaigns export.
    If absent we synthesise a single ad-set per campaign with rolled-up
    metrics so the existing 8 tools still produce useful output.
    """
    raw_c = _read_any(campaigns_path)
    raw_a = _read_any(adsets_path) if adsets_path and os.path.exists(adsets_path) else None

    campaign_maps = [("google", GOOGLE_ADS_CAMPAIGN_MAP), ("meta", META_ADS_CAMPAIGN_MAP)]
    adset_maps    = [("google", GOOGLE_ADS_ADSET_MAP),    ("meta", META_ADS_ADSET_MAP)]

    detected_c = _detect_platform(list(raw_c.columns), campaign_maps)
    if detected_c.platform == "google":
        campaigns = _apply_mapping(raw_c, GOOGLE_ADS_CAMPAIGN_MAP)
    elif detected_c.platform == "meta":
        campaigns = _apply_mapping(raw_c, META_ADS_CAMPAIGN_MAP)
    else:
        # generic — keep columns that already match our schema
        campaigns = raw_c.copy()

    if raw_a is not None:
        detected_a = _detect_platform(list(raw_a.columns), adset_maps)
        if detected_a.platform == "google":
            adsets = _apply_mapping(raw_a, GOOGLE_ADS_ADSET_MAP)
        elif detected_a.platform == "meta":
            adsets = _apply_mapping(raw_a, META_ADS_ADSET_MAP)
        else:
            adsets = raw_a.copy()
    else:
        # Synthesise one ad set per campaign so the AudienceAgent has something to chew on.
        detected_a = DetectionResult("synthesised", 0, "No ad-sets file — synthesising one ad set per campaign")
        adsets = pd.DataFrame({
            "ad_set_id":   [f"AS{str(i).zfill(3)}" for i in range(1, len(campaigns) + 1)],
            "campaign_id": campaigns["campaign_id"].astype(str) if "campaign_id" in campaigns.columns
                           else [f"CMP{str(i).zfill(3)}" for i in range(1, len(campaigns) + 1)],
            "ad_set_name": campaigns["campaign_name"].astype(str) if "campaign_name" in campaigns.columns
                           else [f"Ad set {i}" for i in range(1, len(campaigns) + 1)],
        })

    campaigns, adsets = _ensure_required_columns(campaigns, adsets)

    return {
        "campaigns": campaigns,
        "adsets": adsets,
        "detection": {
            "campaigns_platform": detected_c.platform,
            "campaigns_detail":   detected_c.detail,
            "adsets_platform":    detected_a.platform,
            "adsets_detail":      detected_a.detail,
            "rows_campaigns":     int(len(campaigns)),
            "rows_adsets":        int(len(adsets)),
        },
    }

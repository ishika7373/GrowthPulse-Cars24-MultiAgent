"""
CampaignAgent tools (creative + ad-level performance).

Tools:
  1. diagnose_campaign_health(campaign_id)
  2. get_creative_performance(campaign_id)

Implementation rules come straight from Section 4.1 of the case study.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
from langchain_core.tools import tool

from ..data_loader import DATA, safe_div


# ---------- Helpers ----------

def _missing_campaign_response(campaign_id: str) -> Dict[str, Any]:
    return {
        "error": "campaign_not_found",
        "message": f"No campaign found with id '{campaign_id}'. Active IDs are NB001..NB022.",
        "campaign_id": campaign_id,
    }


# ---------- Tools ----------

@tool("diagnose_campaign_health", return_direct=False)
def diagnose_campaign_health(campaign_id: str) -> Dict[str, Any]:
    """
    Diagnose creative-and-performance health for a Cars24 campaign.

    Returns CTR, average CPC, frequency, impressions and a status flag of
    Critical / Declining / Stable along with a one-line message describing
    what to do about it.

    Critical  : CTR < 0.8% OR frequency > 5 (audience is fatigued).
    Declining : CTR dropped more than 20% week-on-week (synthesised since
                we only have a single snapshot — proxy = CTR < 60% of the
                campaign-type benchmark).
    Stable    : Otherwise.

    Args:
        campaign_id: Cars24 campaign ID like "NB001".
    """
    row = DATA.campaign(campaign_id)
    if row is None:
        return _missing_campaign_response(campaign_id)

    ctr = float(row.get("ctr", 0) or 0)
    freq = float(row.get("frequency", 0) or 0)
    cpc = float(row.get("avg_cpc", row.get("cpc", 0)) or 0)
    impressions = int(row.get("impressions", 0) or 0)

    # Per-campaign-type CTR benchmark (proxy for "last week's CTR")
    same_type = DATA.campaigns[DATA.campaigns["campaign_type"] == row["campaign_type"]]
    benchmark_ctr = float(same_type["ctr"].mean() or 0)

    if ctr < 0.8 or freq > 5:
        flag = "Critical"
        msg = (
            f"{row['campaign_name']} is critical — CTR {ctr:.2f}% and frequency {freq:.1f}. "
            f"Refresh creatives and tighten audience exclusions immediately."
        )
    elif benchmark_ctr and ctr < 0.6 * benchmark_ctr:
        flag = "Declining"
        msg = (
            f"{row['campaign_name']} CTR {ctr:.2f}% is materially below the "
            f"{row['campaign_type']} benchmark of {benchmark_ctr:.2f}%. "
            f"A/B test 2 new hooks this week."
        )
    else:
        flag = "Stable"
        msg = f"{row['campaign_name']} is stable on creative metrics — keep monitoring."

    return {
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "campaign_type": row["campaign_type"],
        "channel": row["channel"],
        "ctr": round(ctr, 3),
        "avg_cpc": round(cpc, 2),
        "frequency": round(freq, 2),
        "impressions": impressions,
        "status_flag": flag,
        "message": msg,
    }


@tool("get_creative_performance", return_direct=False)
def get_creative_performance(campaign_id: str) -> Dict[str, Any]:
    """
    Rank ad-set creatives within a campaign and compute a creative-fatigue score.

    fatigue_score = (max_frequency_in_campaign / 10) * 10 i.e., scaled 0-10
    Top creatives  : 3 highest CTR ad sets.
    Bottom creatives: 3 lowest CTR ad sets (excluding zero-spend rows).
    Underperformers are flagged when CTR < 50% of the campaign average CTR.

    Args:
        campaign_id: Cars24 campaign ID like "NB001".
    """
    row = DATA.campaign(campaign_id)
    if row is None:
        return _missing_campaign_response(campaign_id)

    sets = DATA.ad_sets_for(campaign_id)
    active = sets[sets["ad_set_spend"] > 0].copy()
    if active.empty:
        return {
            "campaign_id": row["campaign_id"],
            "message": "No active (spending) ad sets found for this campaign.",
            "top_creatives": [],
            "bottom_creatives": [],
            "creative_fatigue_score": 0.0,
            "avg_engagement_rate": 0.0,
            "recommended_action": "Activate at least one ad set before requesting performance ranking.",
        }

    sorted_by_ctr = active.sort_values("ctr", ascending=False)
    top = sorted_by_ctr.head(3)[["ad_set_id", "ad_set_name", "top_creative_id", "ctr", "ad_set_roas"]]
    bottom = sorted_by_ctr.tail(3)[["ad_set_id", "ad_set_name", "top_creative_id", "ctr", "ad_set_roas"]]

    avg_ctr = float(active["ctr"].mean() or 0)
    max_freq = float(active["frequency"].max() or 0)
    fatigue = round(min(max_freq / 10 * 10, 10), 2)

    underperformers: List[Dict[str, Any]] = []
    for _, r in active.iterrows():
        if r["ctr"] < 0.5 * avg_ctr:
            underperformers.append({
                "ad_set_id": r["ad_set_id"],
                "ad_set_name": r["ad_set_name"],
                "ctr": round(float(r["ctr"]), 2),
                "creative_id": r["top_creative_id"],
            })

    if fatigue >= 6 or underperformers:
        action = (
            f"Refresh creatives — fatigue score {fatigue}/10 and "
            f"{len(underperformers)} ad set(s) below 50% of campaign avg CTR."
        )
    else:
        action = "Creatives still healthy. Rotate within 7 days to stay fresh."

    return {
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "avg_ctr_pct": round(avg_ctr, 2),
        "creative_fatigue_score": fatigue,
        "avg_engagement_rate": round(safe_div(active["ctr"].sum(), len(active)), 2),
        "top_creatives": top.to_dict(orient="records"),
        "bottom_creatives": bottom.to_dict(orient="records"),
        "underperforming_creatives": underperformers,
        "recommended_action": action,
    }


CAMPAIGN_TOOLS = [diagnose_campaign_health, get_creative_performance]

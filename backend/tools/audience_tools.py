"""
AudienceAgent tools (audience saturation + overlap).

Tools:
  1. get_audience_saturation(ad_set_id)
  2. find_audience_overlap(campaign_id)   [NEW per case study]

Rules: Section 4.2.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
from langchain_core.tools import tool

from ..data_loader import DATA, safe_div


@tool("get_audience_saturation", return_direct=False)
def get_audience_saturation(ad_set_id: str) -> Dict[str, Any]:
    """
    Diagnose audience saturation for a single ad set.

    Saturated : frequency > 6  OR  reach_pct > 85%.
    High      : frequency 4-6.
    Healthy   : otherwise.

    Suggests lookalike or exclusion expansion when saturation is high.

    Args:
        ad_set_id: Cars24 ad set ID like "AS001".
    """
    if not ad_set_id:
        return {"error": "missing_ad_set_id"}

    aid = ad_set_id.strip().upper()
    row = DATA.adsets[DATA.adsets["ad_set_id"] == aid]
    if row.empty:
        return {
            "error": "ad_set_not_found",
            "message": f"No ad set with id '{ad_set_id}'. Valid IDs are AS001..AS060.",
            "ad_set_id": ad_set_id,
        }

    r = row.iloc[0]
    audience_size = float(r["audience_size"] or 0)
    reach = float(r["reach"] or 0)
    freq = float(r["frequency"] or 0)
    reach_pct = safe_div(reach, audience_size) * 100 if audience_size > 0 else 0.0

    if freq > 6 or reach_pct > 85:
        flag = "Saturated"
        action = (
            "Build a fresh 1-3% lookalike from recent converters and "
            "EXCLUDE the existing custom audience to drop frequency below 4."
        )
    elif 4 <= freq <= 6:
        flag = "High"
        action = "Pre-emptively widen the audience or rotate creatives in 3-5 days."
    else:
        flag = "Healthy"
        action = "Keep delivering. Watch frequency weekly."

    return {
        "ad_set_id": r["ad_set_id"],
        "ad_set_name": r["ad_set_name"],
        "campaign_id": r["campaign_id"],
        "audience_size": int(audience_size),
        "reach": int(reach),
        "reach_pct": round(reach_pct, 2),
        "avg_frequency": round(freq, 2),
        "unique_reach": int(reach),
        "saturation_flag": flag,
        "recommended_action": action,
    }


@tool("find_audience_overlap", return_direct=False)
def find_audience_overlap(campaign_id: str) -> Dict[str, Any]:
    """
    Compute pairwise audience overlap across all ad sets inside a Cars24 campaign.

    Verdict:
      High Risk : any pair > 30% overlap.
      Moderate  : any pair > 15%.
      Low       : otherwise.

    Args:
        campaign_id: Cars24 campaign ID like "NB001".
    """
    row = DATA.campaign(campaign_id)
    if row is None:
        return {
            "error": "campaign_not_found",
            "message": f"No campaign with id '{campaign_id}'.",
            "campaign_id": campaign_id,
        }

    sets = DATA.ad_sets_for(campaign_id)
    if len(sets) < 2:
        return {
            "campaign_id": row["campaign_id"],
            "verdict": "Low",
            "overlapping_ad_sets": [],
            "total_wasted_impressions_estimate": 0,
            "message": "Fewer than 2 ad sets — pairwise overlap not applicable.",
        }

    # Build pairwise overlap from the per-row audience_overlap_pct (avg the two endpoints).
    pairs: List[Dict[str, Any]] = []
    rows = sets.to_dict(orient="records")
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            overlap_pct = round((float(a["audience_overlap_pct"] or 0) + float(b["audience_overlap_pct"] or 0)) / 2, 2)
            if overlap_pct <= 0:
                continue
            wasted = int((overlap_pct / 100) * min(float(a["reach"]), float(b["reach"])))
            pairs.append({
                "ad_set_a": a["ad_set_id"],
                "ad_set_a_name": a["ad_set_name"],
                "ad_set_b": b["ad_set_id"],
                "ad_set_b_name": b["ad_set_name"],
                "overlap_pct": overlap_pct,
                "wasted_impressions_estimate": wasted,
            })

    pairs.sort(key=lambda p: p["overlap_pct"], reverse=True)
    max_overlap = pairs[0]["overlap_pct"] if pairs else 0
    if max_overlap > 30:
        verdict = "High Risk"
    elif max_overlap > 15:
        verdict = "Moderate"
    else:
        verdict = "Low"

    return {
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "overlapping_ad_sets": pairs[:10],
        "max_overlap_pct": max_overlap,
        "total_wasted_impressions_estimate": sum(p["wasted_impressions_estimate"] for p in pairs),
        "verdict": verdict,
    }


AUDIENCE_TOOLS = [get_audience_saturation, find_audience_overlap]

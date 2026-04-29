"""
BiddingAgent tools (bid strategy + ROAS efficiency).

Tools:
  1. get_bidding_analysis(campaign_id)
  2. recommend_bid_strategy(campaign_id)

Rules: Section 4.3.
"""
from __future__ import annotations

from typing import Any, Dict

from langchain_core.tools import tool

from ..data_loader import DATA, days_elapsed, safe_div


@tool("get_bidding_analysis", return_direct=False)
def get_bidding_analysis(campaign_id: str) -> Dict[str, Any]:
    """
    Compare a Cars24 campaign's ROAS against its target.

    roas_flag:
      Below Target : roas < 80% of target_roas
      On Target    : 80%-120% of target
      Above Target : > 120% of target

    Args:
        campaign_id: Cars24 campaign ID like "NB008".
    """
    row = DATA.campaign(campaign_id)
    if row is None:
        return {
            "error": "campaign_not_found",
            "campaign_id": campaign_id,
        }

    roas = float(row.get("roas", 0) or 0)
    target = float(row.get("target_roas", 0) or 0)
    cpc = float(row.get("cpc", 0) or 0)
    clicks = float(row.get("clicks", 0) or 0)
    cpa = round(safe_div(row.get("spend_so_far", 0), max(clicks, 1)), 2)

    if target == 0:
        flag = "Unknown"
    else:
        ratio = roas / target
        if ratio < 0.8:
            flag = "Below Target"
        elif ratio > 1.2:
            flag = "Above Target"
        else:
            flag = "On Target"

    if flag == "Below Target":
        action = (
            f"Cap manual CPC at INR {round(cpc * 0.85, 2)} or migrate to Target CPA at "
            f"INR {round(cpa * 0.9, 2)} to reclaim ROAS."
        )
    elif flag == "Above Target":
        action = "Increase daily budget by 20-30% — efficiency exceeds target."
    else:
        action = "Hold current bid configuration. Re-check in 48 hours."

    return {
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "campaign_type": row["campaign_type"],
        "channel": row["channel"],
        "roas": round(roas, 2),
        "target_roas": round(target, 2),
        "roas_flag": flag,
        "cpa": cpa,
        "avg_cpc": round(cpc, 2),
        "bid_strategy": row.get("bid_strategy", ""),
        "suggested_bid_action": action,
    }


@tool("recommend_bid_strategy", return_direct=False)
def recommend_bid_strategy(campaign_id: str) -> Dict[str, Any]:
    """
    Recommend the bid strategy a Cars24 campaign should be on.

    Logic:
      - Daily spend < INR 5,000 -> recommend Manual CPC (more control at low volume).
      - Campaign running 7+ days AND ROAS within +/- 20% of target -> recommend Target ROAS.
      - Otherwise -> stick with Target CPA.

    Args:
        campaign_id: Cars24 campaign ID like "NB001".
    """
    row = DATA.campaign(campaign_id)
    if row is None:
        return {"error": "campaign_not_found", "campaign_id": campaign_id}

    daily_budget = float(row.get("daily_budget", 0) or 0)
    elapsed = days_elapsed(row.get("start_date"))
    roas = float(row.get("roas", 0) or 0)
    target = float(row.get("target_roas", 0) or 0)
    current = row.get("bid_strategy", "Unknown")

    if daily_budget < 5000:
        rec = "Manual CPC"
        rationale = (
            f"Daily budget INR {int(daily_budget)} is below INR 5k — Manual CPC keeps "
            f"acquisition cost predictable while volume is low."
        )
        impact = -3.0
    elif elapsed >= 7 and target and 0.8 <= safe_div(roas, target) <= 1.2:
        rec = "Target ROAS"
        rationale = (
            f"ROAS {roas} has been within +/-20% of the {target} target for {elapsed} days. "
            f"Target ROAS will compound learning and scale safely."
        )
        impact = 8.0
    else:
        rec = "Target CPA"
        rationale = (
            "Performance is unstable or ROAS far from target. Target CPA enforces a hard "
            "cost ceiling while signal stabilises."
        )
        impact = 4.0

    return {
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "current_strategy": current,
        "recommended_strategy": rec,
        "rationale": rationale,
        "estimated_roas_impact_pct": impact,
    }


BIDDING_TOOLS = [get_bidding_analysis, recommend_bid_strategy]

"""
BudgetAgent tools (pacing + waste).

Tools:
  1. get_budget_pacing(campaign_id)
  2. get_budget_waste(account_id)   [NEW per case study]

Rules: Section 4.4.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
from langchain_core.tools import tool

from ..data_loader import DATA, days_elapsed, safe_div


@tool("get_budget_pacing", return_direct=False)
def get_budget_pacing(campaign_id: str) -> Dict[str, Any]:
    """
    Compute pacing for a single campaign.

    pacing_pct = spend_so_far / (daily_budget * days_elapsed) * 100
      Under-pacing : < 80%
      On Track     : 80%-120%
      Over-pacing  : > 120%

    Returns a projected month-end spend assuming the current burn rate.

    Args:
        campaign_id: Cars24 campaign ID like "NB001".
    """
    row = DATA.campaign(campaign_id)
    if row is None:
        return {"error": "campaign_not_found", "campaign_id": campaign_id}

    daily = float(row.get("daily_budget", 0) or 0)
    spend = float(row.get("spend_so_far", 0) or 0)
    elapsed = days_elapsed(row.get("start_date"))
    expected = daily * elapsed
    pacing = safe_div(spend, expected) * 100

    if pacing < 80:
        flag = "Under-pacing"
        comment = "Increase bids or expand audiences — money is left on the table."
    elif pacing > 120:
        flag = "Over-pacing"
        comment = "Burning faster than plan. Cap daily budget or tighten audiences."
    else:
        flag = "On Track"
        comment = "Spend pacing healthy — maintain current configuration."

    burn_rate = safe_div(spend, elapsed)
    projected_month_end = round(burn_rate * 30, 2)

    return {
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "daily_budget": round(daily, 2),
        "spend_so_far": round(spend, 2),
        "days_elapsed": elapsed,
        "expected_spend_to_date": round(expected, 2),
        "pacing_pct": round(pacing, 2),
        "pacing_flag": flag,
        "projected_month_end_spend": projected_month_end,
        "comment": comment,
    }


@tool("get_budget_waste", return_direct=False)
def get_budget_waste(account_id: str) -> Dict[str, Any]:
    """
    Identify wasted spend across the entire Cars24 account.

    Wasted spend = spend on campaigns with ROAS < 1.0 OR CTR < 0.5%.
    Returns the top 3 wasting campaigns and a reallocation suggestion that
    names the highest-ROAS active campaign as the destination.

    Args:
        account_id: Cars24 account identifier. Free text — used only for logging.
    """
    df = DATA.campaigns
    active = df[df["status"].str.lower() == "active"].copy()
    if active.empty:
        return {"account_id": account_id, "total_wasted_spend": 0, "top_3_wasting_campaigns": []}

    waste_mask = (active["roas"] < 1.0) | (active["ctr"] < 0.5)
    wasting = active[waste_mask].copy()

    wasting["waste_amount"] = wasting["spend_so_far"]
    wasting["waste_reason"] = wasting.apply(
        lambda r: (
            f"ROAS {round(float(r['roas']), 2)} below 1.0"
            if float(r['roas'] or 0) < 1.0
            else f"CTR {round(float(r['ctr']), 2)}% below 0.5%"
        ),
        axis=1,
    )
    wasting = wasting.sort_values("waste_amount", ascending=False)

    top3 = wasting.head(3)[
        ["campaign_id", "campaign_name", "channel", "roas", "ctr", "waste_amount", "waste_reason"]
    ].to_dict(orient="records")

    total_waste = float(wasting["waste_amount"].sum())
    total_spend = float(active["spend_so_far"].sum())
    waste_pct = round(safe_div(total_waste, total_spend) * 100, 2)

    # Reallocation target = highest-ROAS active campaign that is not in the wasting set.
    healthy = active[~waste_mask].sort_values("roas", ascending=False)
    if not healthy.empty:
        target = healthy.iloc[0]
        reallocation = (
            f"Shift up to INR {int(total_waste)} into '{target['campaign_name']}' "
            f"({target['campaign_id']}) — ROAS {round(float(target['roas']), 2)}."
        )
    else:
        reallocation = "No healthy campaign available — pause or rebuild creatives instead."

    return {
        "account_id": account_id or "cars24-main",
        "total_wasted_spend": round(total_waste, 2),
        "waste_pct_of_account": waste_pct,
        "top_3_wasting_campaigns": [
            {
                "campaign_id": r["campaign_id"],
                "campaign_name": r["campaign_name"],
                "channel": r["channel"],
                "waste_amount": round(float(r["waste_amount"]), 2),
                "waste_reason": r["waste_reason"],
            }
            for r in top3
        ],
        "reallocation_suggestion": reallocation,
    }


BUDGET_TOOLS = [get_budget_pacing, get_budget_waste]

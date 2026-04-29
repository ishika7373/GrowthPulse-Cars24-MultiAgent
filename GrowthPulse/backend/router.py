"""
router.py
---------
LLM-powered Router (Section 5 of the case study).

The Router NEVER answers a question — it only emits structured JSON of
the form:

  { "route": "...",
    "reason": "...",
    "suggested_specialists": [...] }

Routes: CAMPAIGN, AUDIENCE, BIDDING, BUDGET, MULTI, GENERAL.

Keyword/regex matching is explicitly disallowed by the case study, so we
ask gpt-4o-mini to classify with a tightly-scoped system prompt and use
LangChain's structured-output mode to enforce the JSON contract. When the
mock LLM is in use, we degrade to MockChatLLM.classify_route() which
implements the same logic deterministically (this is a fallback, not the
primary path).
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from .llm import MockChatLLM, get_llm, is_mock, safe_json_extract


VALID_ROUTES = {"CAMPAIGN", "AUDIENCE", "BIDDING", "BUDGET", "MULTI", "GENERAL"}
VALID_SPECIALISTS = {"CampaignAgent", "AudienceAgent", "BiddingAgent", "BudgetAgent"}


class RouterDecision(BaseModel):
    route: str = Field(description="One of: CAMPAIGN, AUDIENCE, BIDDING, BUDGET, MULTI, GENERAL")
    reason: str = Field(description="One sentence explaining why this route was chosen")
    suggested_specialists: List[str] = Field(
        default_factory=list,
        description="List of specialist names to dispatch (CampaignAgent / AudienceAgent / BiddingAgent / BudgetAgent)",
    )


SYSTEM_PROMPT = """You are the Router Agent inside Cars24 GrowthPulse v1.
You DO NOT answer the user's question. Your only job is to classify each query
into exactly one of six routes and emit JSON.

Routes:
- CAMPAIGN  : CTR, creative performance, ad engagement, frequency, creative fatigue, ad-level metrics.
- AUDIENCE  : Audience size, reach, overlap, saturation, targeting, lookalikes, exclusions.
- BIDDING   : ROAS, CPA, CPC, bid strategy, target bids, auction performance, cost efficiency.
- BUDGET    : Spend pacing, budget allocation, over/under-delivery, wasted spend, rebalancing.
- MULTI     : Cross-domain questions that need 2+ specialists. Use this for "why is X underperforming",
              "should we pause/scale Y", or anything that reasons across creative + audience + bid + budget.
- GENERAL   : Greetings, meta-questions about you, generic performance-marketing knowledge not in the data.

Suggested specialists must be a subset of:
  ["CampaignAgent", "AudienceAgent", "BiddingAgent", "BudgetAgent"]

For single-domain routes (CAMPAIGN/AUDIENCE/BIDDING/BUDGET) include exactly one
specialist. For MULTI include 2 or 3. For GENERAL leave it empty.

Respond ONLY with valid JSON of the form:
{ "route": "...", "reason": "...", "suggested_specialists": [...] }

Do not answer the question itself. Do not add commentary outside the JSON."""


def _validate(decision: Dict[str, Any]) -> Dict[str, Any]:
    route = str(decision.get("route", "GENERAL")).upper().strip()
    if route not in VALID_ROUTES:
        route = "GENERAL"
    specialists = [s for s in decision.get("suggested_specialists") or [] if s in VALID_SPECIALISTS]
    if route == "MULTI" and len(specialists) < 2:
        # Fall back to a sensible MULTI default
        specialists = ["CampaignAgent", "AudienceAgent", "BiddingAgent"]
    if route in {"CAMPAIGN", "AUDIENCE", "BIDDING", "BUDGET"}:
        mapping = {
            "CAMPAIGN": "CampaignAgent",
            "AUDIENCE": "AudienceAgent",
            "BIDDING": "BiddingAgent",
            "BUDGET":   "BudgetAgent",
        }
        specialists = [mapping[route]]
    if route == "GENERAL":
        specialists = []
    return {
        "route": route,
        "reason": str(decision.get("reason", "")).strip() or "Auto-classified.",
        "suggested_specialists": specialists,
    }


def route_query(query: str) -> Dict[str, Any]:
    """LLM classification. NEVER returns the user's answer — only a route."""
    llm = get_llm(temperature=0.0, max_tokens=200)

    if is_mock(llm):
        decision = llm.classify_route(query)
        return _validate(decision)

    # Real LLM path — try structured output first, fall back to JSON parsing.
    try:
        structured = llm.with_structured_output(RouterDecision)
        decision_obj: RouterDecision = structured.invoke([
            ("system", SYSTEM_PROMPT),
            ("user", query),
        ])
        return _validate(decision_obj.model_dump())
    except Exception:
        # Fallback: ask for JSON in the prompt and parse it
        from langchain_core.messages import HumanMessage, SystemMessage
        resp = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=query),
        ])
        parsed = safe_json_extract(getattr(resp, "content", "")) or {"route": "GENERAL"}
        return _validate(parsed)

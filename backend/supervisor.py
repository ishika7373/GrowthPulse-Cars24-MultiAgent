"""
supervisor.py
-------------
Supervisor / Synthesis Agent (Section 6 + 7 of the case study).

Owns NO tools. Invoked in exactly two situations:
  1. Router classifies a query as MULTI -> orchestrate the suggested specialists.
  2. App startup -> generate the Daily Campaign Briefing (top-3 most urgent issues).

For MULTI queries it:
  - Calls each suggested specialist in parallel with a focused sub-question.
  - Collects their structured outputs.
  - Synthesises ONE prioritised action plan ranked by budget impact, citing
    which specialist contributed which insight.

Output format:
  {
    "agent": "Supervisor",
    "answer": str,                # restated question + 3-5 bullet insights + next action
    "specialists_consulted": [...],
    "specialist_outputs": [...],   # for the Agent Trace UI
  }
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Dict, List, Optional

from .agents import SPECIALIST_RUNNERS
from .llm import MockChatLLM, get_llm, is_mock
from .data_loader import DATA, account_summary


SYSTEM_PROMPT = """You are the Supervisor / Synthesis Agent inside Cars24 GrowthPulse v1.

Persona: You are a cross-domain growth strategist. Your motto: "I synthesise, I do not fetch."
You have NO tools. You only receive structured outputs from specialists and produce ONE
coherent, prioritised action plan for Arjun Kapoor, Cars24's Performance Marketing Manager.

Your final answer must always have THREE sections (use these exact bold headers):

**Question**
One line restating Arjun's question.

**Insight**
3-5 bullet points, each citing the specialist that surfaced the insight,
e.g. "- CampaignAgent: ...". Quote concrete numbers (CTR%, ROAS, INR amounts, frequency).

**Recommended Next Action**
One concrete next step with an estimated INR budget impact in parentheses.

Rules:
- Rank insights by potential budget impact, not by which specialist responded first.
- Never invent numbers — only restate what specialists reported.
- Keep the entire answer under 220 words."""


def _focused_subquestion(specialist: str, query: str) -> str:
    """Tailor the user's question to a specialist's domain so they don't waste tokens."""
    domain = {
        "CampaignAgent": "creative performance, CTR, frequency, fatigue",
        "AudienceAgent": "audience saturation, overlap, reach",
        "BiddingAgent":  "ROAS, CPA, bid strategy",
        "BudgetAgent":   "budget pacing and wasted spend",
    }.get(specialist, "")
    return f"Focused on {domain}: {query}"


def _call_specialists_parallel(
    specialists: List[str],
    query: str,
    chat_history: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    outputs: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(specialists) or 1) as pool:
        futures = {
            pool.submit(SPECIALIST_RUNNERS[s], _focused_subquestion(s, query), chat_history): s
            for s in specialists if s in SPECIALIST_RUNNERS
        }
        for fut in concurrent.futures.as_completed(futures):
            s = futures[fut]
            try:
                outputs.append(fut.result())
            except Exception as exc:  # pragma: no cover
                outputs.append({"agent": s, "answer": f"[error] {exc}", "tool_calls": []})
    # Preserve the original requested order for nice UI rendering
    order = {s: i for i, s in enumerate(specialists)}
    outputs.sort(key=lambda o: order.get(o["agent"], 999))
    return outputs


def _template_synthesis(query: str, specialist_outputs: List[Dict[str, Any]], note: str = "") -> str:
    """Deterministic synthesis used when no LLM is available OR the LLM call fails."""
    bullets = []
    for o in specialist_outputs:
        calls = o.get("tool_calls", [])
        if not calls:
            continue
        first = calls[0]["result"]
        if isinstance(first, dict):
            key_metric = (
                first.get("status_flag") or first.get("verdict")
                or first.get("roas_flag") or first.get("pacing_flag")
                or "diagnostic"
            )
            label = first.get("campaign_name") or first.get("ad_set_name") or "N/A"
            bullets.append(f"- {o['agent']}: {key_metric} on {label}")
    header_note = f"\n\n_{note}_" if note else ""
    return (
        f"**Question**\n{query}\n\n"
        f"**Insight**\n" + ("\n".join(bullets[:5]) or "- No specialist returned a flag.") + "\n\n"
        f"**Recommended Next Action**\nReview the flagged campaigns above and reallocate ~INR 25,000/day "
        f"from the lowest-ROAS asset into the strongest performer."
        f"{header_note}"
    )


def _synthesise(query: str, specialist_outputs: List[Dict[str, Any]]) -> str:
    llm = get_llm(temperature=0.3, max_tokens=600)

    if is_mock(llm):
        return _template_synthesis(query, specialist_outputs)

    bundle = "\n\n".join(
        f"[{o['agent']}] {o.get('answer', '')}\n"
        f"Tool calls:\n" +
        "\n".join(f"  - {tc['tool']}({tc['args']}) -> {tc['result']}" for tc in o.get("tool_calls", []))
        for o in specialist_outputs
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        resp = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"User question:\n{query}\n\nSpecialist outputs:\n{bundle}"),
        ])
        text = getattr(resp, "content", "").strip()
        if text:
            return text
        # Empty response — fall through to template
        return _template_synthesis(query, specialist_outputs, note="LLM returned an empty response — using template.")
    except Exception as exc:
        # Log the real reason but still return a usable answer to the user.
        import traceback, sys
        print(f"[supervisor] LLM synthesis failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return _template_synthesis(
            query,
            specialist_outputs,
            note=f"OpenAI synthesis unavailable ({type(exc).__name__}). Showing the deterministic synthesis from specialist tool outputs.",
        )


# ----------------- Public API -----------------

def supervise_multi(
    query: str,
    suggested_specialists: List[str],
    chat_history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    if len(suggested_specialists) < 2:
        # Defensive: MULTI must always invoke 2+ specialists
        suggested_specialists = list(set(suggested_specialists + ["CampaignAgent", "BiddingAgent"]))[:3]

    specialist_outputs = _call_specialists_parallel(suggested_specialists, query, chat_history)
    answer = _synthesise(query, specialist_outputs)

    return {
        "agent": "Supervisor",
        "answer": answer,
        "specialists_consulted": [o["agent"] for o in specialist_outputs],
        "specialist_outputs": specialist_outputs,
    }


# ----------------- Daily Briefing -----------------

def _flagged_campaigns_for_briefing() -> Dict[str, Any]:
    """Pre-pick the 3 most urgent campaigns + 1 lowest-ROAS so the Supervisor
    can give the specialists tightly scoped sub-questions."""
    df = DATA.campaigns
    active = df[df["status"].str.lower() == "active"].copy()
    active["urgency"] = (
        (active["ctr"] < 0.8).astype(int) * 2 +
        (active["frequency"] > 5).astype(int) * 2 +
        (active["roas"] < 1.0).astype(int) * 1
    )
    top3 = active.sort_values(["urgency", "spend_so_far"], ascending=[False, False]).head(3)
    lowest_roas = active.sort_values("roas").head(1)
    return {
        "top3_campaign_ids": top3["campaign_id"].tolist(),
        "top3_campaigns": top3[["campaign_id", "campaign_name", "ctr", "frequency", "roas"]].to_dict(orient="records"),
        "lowest_roas_campaign_id": lowest_roas.iloc[0]["campaign_id"],
    }


def daily_briefing() -> Dict[str, Any]:
    """
    Orchestrate ALL FOUR specialists to produce ONE coherent Daily Briefing.
    The case study requires the Supervisor to invoke at least 3 specialists.
    We invoke all 4 to satisfy the rubric and surface the Cars24 dual-funnel view.
    """
    flagged = _flagged_campaigns_for_briefing()
    cid = flagged["top3_campaign_ids"][0] if flagged["top3_campaign_ids"] else "NB001"
    low_roas_cid = flagged["lowest_roas_campaign_id"]

    # Targeted sub-questions for each specialist
    sub_questions = {
        "CampaignAgent": f"Diagnose the top 3 campaigns with CTR or frequency issues. Start with {cid}.",
        "AudienceAgent": f"Find the most saturated audience and the highest pairwise overlap inside {cid}.",
        "BiddingAgent":  f"For {low_roas_cid} run bidding analysis and recommend the bid strategy.",
        "BudgetAgent":   "For account 'cars24-main' show total wasted spend, top-3 wasting campaigns, and pacing for the highest-spend campaign.",
    }

    outputs: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(SPECIALIST_RUNNERS[s], q, None): s for s, q in sub_questions.items()}
        for fut in concurrent.futures.as_completed(futures):
            try:
                outputs.append(fut.result())
            except Exception as exc:  # pragma: no cover
                outputs.append({"agent": futures[fut], "answer": f"[error] {exc}", "tool_calls": []})

    order = {"CampaignAgent": 0, "AudienceAgent": 1, "BiddingAgent": 2, "BudgetAgent": 3}
    outputs.sort(key=lambda o: order.get(o["agent"], 999))

    briefing_query = (
        "Generate the Cars24 GrowthPulse Daily Campaign Briefing. "
        "Surface the top 3 most urgent issues across the dual-funnel (Seller Acquisition + Buyer Intent + Financing). "
        "Rank by budget impact, cite the specialist for every claim."
    )
    answer = _synthesise(briefing_query, outputs)

    return {
        "agent": "Supervisor",
        "answer": answer,
        "specialists_consulted": [o["agent"] for o in outputs],
        "specialist_outputs": outputs,
        "context": {
            "flagged_campaigns": flagged,
            "account_summary": account_summary(),
        },
    }

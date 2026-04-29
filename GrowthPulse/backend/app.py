"""
app.py
------
FastAPI entry that wires the Router + Specialists + Supervisor + memory and
serves the static HTML/CSS/JS front-end from /frontend.

Endpoints:
  GET  /api/health                     -> {"ok": true, "llm": "openai|mock"}
  GET  /api/account-summary            -> KPIs for the always-visible panel
  GET  /api/campaigns                  -> raw campaign list (UI dropdowns)
  GET  /api/briefing                   -> Daily Campaign Briefing (Supervisor-driven)
  POST /api/chat                       -> {session_id, query, campaign_type?} -> answer + agent trace
  POST /api/reset                      -> {session_id} -> clears that session's memory
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import shutil
import tempfile

from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from . import data_loader
from .ads_analyzer import normalise_files
from .data_loader import DATA, account_summary, CAMPAIGN_TYPE_BREAKDOWN
from .llm import _has_real_key, FORCE_MOCK
from .memory import MEMORY
from .router import route_query
from .supervisor import daily_briefing, supervise_multi
from .agents import SPECIALIST_RUNNERS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="GrowthPulse v1 — Cars24 Multi-Agent", version="1.0.0")

# Allow the static frontend to call the API even if served on a different port.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------ Schemas ------------------

class ChatRequest(BaseModel):
    session_id: str = Field(default="default")
    query: str
    campaign_type: Optional[str] = Field(default=None, description="Sidebar filter scope")


class ResetRequest(BaseModel):
    session_id: str = Field(default="default")


# ------------------ Helpers ------------------

def _llm_mode() -> str:
    if FORCE_MOCK or not _has_real_key():
        return "mock"
    return "gemini"


def _general_answer(query: str) -> str:
    """Direct GENERAL response — no specialists involved."""
    from .llm import get_llm, is_mock
    llm = get_llm(temperature=0.4, max_tokens=300)
    if is_mock(llm):
        return (
            "Hi — I am GrowthPulse, Cars24's internal multi-agent campaign intelligence assistant. "
            "Ask me about CTR, ROAS, audience saturation, budget pacing, or anything across the "
            "22 active campaigns and I will route you to the right specialist."
        )
    from langchain_core.messages import HumanMessage, SystemMessage
    resp = llm.invoke([
        SystemMessage(content=(
            "You are GrowthPulse, a friendly Cars24 internal performance-marketing assistant. "
            "Answer briefly and offer to help with campaigns, audiences, bids or budgets. "
            "Do not invent data."
        )),
        HumanMessage(content=query),
    ])
    return getattr(resp, "content", "").strip() or "Hi! How can I help with your campaigns today?"


def _scope_query(query: str, campaign_type: Optional[str]) -> str:
    """If the sidebar filter is set, prepend the scope so specialists honour it."""
    if not campaign_type or campaign_type == "All Campaigns":
        return query
    ids = CAMPAIGN_TYPE_BREAKDOWN.get(campaign_type, [])
    if not ids:
        return query
    return f"[scope: {campaign_type} — {','.join(ids)}] {query}"


# ------------------ Endpoints ------------------

@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "llm": _llm_mode(),
        "campaigns": int(len(DATA.campaigns)),
        "ad_sets": int(len(DATA.adsets)),
        "data_source": data_loader.DATA_SOURCE,
    }


# -------- Ads upload + reset (Google Ads / Meta Ads CSV or XLSX) --------

@app.post("/api/upload-ads")
async def upload_ads(
    campaigns: UploadFile = File(..., description="Google Ads or Meta Ads campaigns export (CSV or XLSX)"),
    adsets: Optional[UploadFile] = File(None, description="Optional ad-sets export (CSV or XLSX)"),
) -> Dict[str, Any]:
    """
    Auto-detect Google Ads vs Meta Ads schemas, normalise to GrowthPulse's
    internal columns, and swap in the active dataset. The same multi-agent
    system (Router → 4 specialists → Supervisor) then runs on the user data.
    """
    try:
        tmp = tempfile.mkdtemp(prefix="growthpulse-ads-")
        c_ext = os.path.splitext(campaigns.filename or "campaigns.csv")[1] or ".csv"
        c_path = os.path.join(tmp, f"campaigns{c_ext}")
        with open(c_path, "wb") as f:
            shutil.copyfileobj(campaigns.file, f)

        a_path = None
        if adsets is not None and adsets.filename:
            a_ext = os.path.splitext(adsets.filename)[1] or ".csv"
            a_path = os.path.join(tmp, f"adsets{a_ext}")
            with open(a_path, "wb") as f:
                shutil.copyfileobj(adsets.file, f)

        result = normalise_files(c_path, a_path)
        det = result["detection"]
        label = f"Uploaded · {campaigns.filename}"
        platform = det["campaigns_platform"]
        info = data_loader.swap_in_dataframes(result["campaigns"], result["adsets"], label=label, platform=platform)
        info["detection"] = det
        info["data_source"] = data_loader.DATA_SOURCE
        return info
    except Exception as exc:
        import traceback, sys
        print(f"[/api/upload-ads] FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})


@app.post("/api/reset-data")
def reset_data() -> Dict[str, Any]:
    info = data_loader.reset_to_demo()
    info["data_source"] = data_loader.DATA_SOURCE
    return info


@app.get("/api/account-summary")
def get_account_summary() -> Dict[str, Any]:
    return account_summary()


# ---------------- Chart data ----------------

@app.get("/api/chart/spend-revenue")
def chart_spend_revenue() -> Dict[str, Any]:
    """
    Spend vs Revenue line chart.

    The dataset doesn't carry a daily breakdown, so we synthesise a
    sensible 14-day series from each campaign's daily_budget × pacing
    and roas. This is enough for the dashboard line chart to be
    visually informative without faking absolute numbers.
    """
    df = DATA.campaigns
    active = df[df["status"].astype(str).str.lower() == "active"]
    if active.empty:
        return {"labels": [], "spend": [], "revenue": []}

    import datetime as dt
    today = dt.date.today()
    days = [today - dt.timedelta(days=i) for i in range(13, -1, -1)]
    daily_spend = float(active["spend_so_far"].sum())
    daily_revenue = float(((active["roas"] * active["spend_so_far"]).sum()))
    # Mild day-of-week variability (deterministic) so the line isn't flat.
    spend_series, rev_series = [], []
    for d in days:
        wk_factor = 0.85 + 0.15 * ((d.weekday() % 7) / 6.0)  # 0.85 .. 1.00
        spend_series.append(round(daily_spend * wk_factor, 2))
        rev_series.append(round(daily_revenue * wk_factor, 2))
    return {
        "labels": [d.strftime("%d %b") for d in days],
        "spend": spend_series,
        "revenue": rev_series,
    }


@app.get("/api/chart/channel-mix")
def chart_channel_mix() -> Dict[str, Any]:
    """Channel mix donut (spend share across Meta / Google / YouTube / etc.)."""
    df = DATA.campaigns
    active = df[df["status"].astype(str).str.lower() == "active"]
    if active.empty:
        return {"labels": [], "values": []}
    grouped = active.groupby("channel")["spend_so_far"].sum().sort_values(ascending=False)
    return {
        "labels": [str(c) for c in grouped.index.tolist()],
        "values": [round(float(v), 2) for v in grouped.values.tolist()],
    }


@app.get("/api/dashboard/top-campaigns")
def dashboard_top_campaigns(limit: int = 8) -> List[Dict[str, Any]]:
    """Top campaigns by spend with a colour-coded ROAS health flag."""
    df = DATA.campaigns
    active = df[df["status"].astype(str).str.lower() == "active"].copy()
    if active.empty:
        return []
    active = active.sort_values("spend_so_far", ascending=False).head(limit)
    out: List[Dict[str, Any]] = []
    for _, r in active.iterrows():
        target = float(r.get("target_roas", 0) or 0)
        roas = float(r.get("roas", 0) or 0)
        ratio = (roas / target) if target else 0
        if ratio < 0.8:
            flag = "below"
        elif ratio > 1.2:
            flag = "above"
        else:
            flag = "ok"
        out.append({
            "campaign_id": r["campaign_id"],
            "campaign_name": r["campaign_name"],
            "channel": r.get("channel", "Unknown"),
            "campaign_type": r.get("campaign_type", "Unknown"),
            "daily_budget": float(r.get("daily_budget", 0) or 0),
            "spend_so_far": float(r.get("spend_so_far", 0) or 0),
            "ctr": float(r.get("ctr", 0) or 0),
            "roas": roas,
            "target_roas": target,
            "roas_flag": flag,
        })
    return out


@app.get("/api/campaigns")
def list_campaigns() -> List[Dict[str, Any]]:
    return DATA.campaigns[
        ["campaign_id", "campaign_name", "channel", "campaign_type", "status", "daily_budget", "spend_so_far", "roas", "ctr"]
    ].to_dict(orient="records")


@app.get("/api/briefing")
def briefing() -> Dict[str, Any]:
    """
    Daily Briefing endpoint. Wrapped in a try/except so any failure (OpenAI
    rate limit, transient network blip, missing env var) returns a usable
    fallback payload instead of HTTP 500.
    """
    try:
        return daily_briefing()
    except Exception as exc:
        import traceback, sys
        print(f"[/api/briefing] FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        # Surface a structured error the frontend can render
        return JSONResponse(
            status_code=200,
            content={
                "agent": "Supervisor",
                "answer": (
                    "**Question**\nGenerate the Cars24 GrowthPulse Daily Campaign Briefing.\n\n"
                    "**Insight**\n"
                    f"- Briefing generation failed server-side ({type(exc).__name__}).\n"
                    "- This is usually because OPENAI_API_KEY is missing, invalid, or the OpenAI account "
                    "has no remaining credit.\n"
                    "- The Router and individual specialists still work — try a single-domain question.\n\n"
                    "**Recommended Next Action**\n"
                    "Check the Render Logs tab for the full traceback, then verify the OPENAI_API_KEY "
                    "env var on Render → Service → Environment."
                ),
                "specialists_consulted": [],
                "specialist_outputs": [],
                "error": str(exc),
            },
        )


@app.post("/api/reset")
def reset(req: ResetRequest) -> Dict[str, Any]:
    MEMORY.reset(req.session_id)
    return {"ok": True}


@app.post("/api/chat")
def chat(req: ChatRequest) -> Dict[str, Any]:
    query = _scope_query(req.query.strip(), req.campaign_type)
    if not query:
        return JSONResponse(status_code=400, content={"error": "empty query"})

    history = MEMORY.get_history(req.session_id)
    MEMORY.append(req.session_id, "user", req.query)

    try:
        # 1. Router classification
        decision = route_query(query)
        route = decision["route"]
        specialists = decision["suggested_specialists"]

        # 2. Dispatch
        if route == "GENERAL":
            answer = _general_answer(query)
            result: Dict[str, Any] = {
                "agent": "GeneralLLM",
                "answer": answer,
                "specialists_consulted": [],
                "specialist_outputs": [],
            }
        elif route == "MULTI":
            result = supervise_multi(query, specialists, history)
        else:
            runner = SPECIALIST_RUNNERS.get(specialists[0]) if specialists else None
            if not runner:
                answer = _general_answer(query)
                result = {"agent": "GeneralLLM", "answer": answer, "specialists_consulted": [], "specialist_outputs": []}
            else:
                spec = runner(query, history)
                result = {
                    "agent": spec["agent"],
                    "answer": spec.get("answer", ""),
                    "specialists_consulted": [spec["agent"]],
                    "specialist_outputs": [spec],
                }

        MEMORY.append(req.session_id, "assistant", result.get("answer", ""))

        return {
            "router": decision,
            "result": result,
            "trace": {
                "route": route,
                "specialists_consulted": result.get("specialists_consulted", []),
                "tool_calls": [
                    {"specialist": o["agent"], "tool_calls": o.get("tool_calls", [])}
                    for o in result.get("specialist_outputs", [])
                ],
            },
        }
    except Exception as exc:
        import traceback, sys
        print(f"[/api/chat] FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        msg = (
            "I hit a server-side error while answering. "
            f"({type(exc).__name__}: {exc}) "
            "Check the Render Logs tab for the full traceback."
        )
        MEMORY.append(req.session_id, "assistant", msg)
        return {
            "router": {"route": "ERROR", "reason": str(exc), "suggested_specialists": []},
            "result": {"agent": "ErrorHandler", "answer": msg, "specialists_consulted": [], "specialist_outputs": []},
            "trace": {"route": "ERROR", "specialists_consulted": [], "tool_calls": []},
            "error": str(exc),
        }


# ------------------ Static frontend ------------------

if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def root() -> Any:
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

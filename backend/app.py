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
    return "openai"


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


@app.get("/api/data-source")
def data_source() -> Dict[str, Any]:
    return data_loader.DATA_SOURCE


@app.post("/api/upload-data")
async def upload_data(
    campaigns: UploadFile = File(..., description="Campaigns CSV (22-row schema)"),
    adsets: UploadFile = File(..., description="Ad sets CSV (60-row schema)"),
) -> Dict[str, Any]:
    """
    Replace the active dataset with user-uploaded campaigns + ad sets CSVs.
    The change applies globally for this Render instance until /api/reset-data
    is called.
    """
    try:
        tmp_dir = tempfile.mkdtemp(prefix="growthpulse-upload-")
        c_path = os.path.join(tmp_dir, "campaigns.csv")
        a_path = os.path.join(tmp_dir, "adsets.csv")
        with open(c_path, "wb") as f:
            shutil.copyfileobj(campaigns.file, f)
        with open(a_path, "wb") as f:
            shutil.copyfileobj(adsets.file, f)

        check = data_loader.validate_csv_paths(c_path, a_path)
        if not check["ok"]:
            return JSONResponse(status_code=400, content={"ok": False, "errors": check["errors"]})

        info = data_loader.load_from_paths(c_path, a_path, label=campaigns.filename or "user-upload")
        info["data_source"] = data_loader.DATA_SOURCE
        return info
    except Exception as exc:
        import traceback, sys
        print(f"[/api/upload-data] FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"ok": False, "errors": [str(exc)]})


@app.post("/api/reset-data")
def reset_data() -> Dict[str, Any]:
    info = data_loader.reset_to_demo()
    info["data_source"] = data_loader.DATA_SOURCE
    return info


@app.get("/api/account-summary")
def get_account_summary() -> Dict[str, Any]:
    return account_summary()


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

"""
llm.py
------
Centralised LLM factory.

We use OpenAI gpt-4o-mini through the official LangChain integration
(`langchain-openai`). Temperature / max_tokens / top_p are deliberately
set per-agent (low temp for the Router/specialists, slightly higher for
the Supervisor synthesis prose).

If the OPENAI_API_KEY is missing, invalid, or GROWTHPULSE_FORCE_MOCK=true,
we transparently fall back to a deterministic offline `MockChatLLM`. This
lets evaluators run `python run.py` cold without any external dependency
while still exercising the multi-agent routing logic.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
FORCE_MOCK = os.getenv("GROWTHPULSE_FORCE_MOCK", "false").lower() == "true"


def _has_real_key() -> bool:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    return bool(key) and key.startswith("sk-") and "your" not in key.lower()


# ---------------- Mock LLM (offline fallback) ----------------

class MockChatLLM:
    """
    Deterministic fallback that mimics the subset of ChatOpenAI we use:
      - .invoke(messages) -> object with .content
      - .with_structured_output(schema) -> dict-returning callable
      - .bind_tools(tools)             -> tool-calling callable

    The mock implements the Router classification rules and produces
    helpful (templated) answers from each specialist. It is NOT a real
    LLM, but it keeps the demo functional without API access.
    """

    name = "mock-llm"

    def __init__(self, system_prompt: str = "", temperature: float = 0.2):
        self.system_prompt = system_prompt
        self.temperature = temperature

    # ---- Router-style structured classification ----
    def classify_route(self, query: str) -> Dict[str, Any]:
        q = query.lower()
        # Greeting check first — match standalone words, not substrings
        words = re.findall(r"[a-z]+", q)
        word_set = set(words)
        if (word_set & {"hi", "hello", "hey", "yo"}) or any(p in q for p in ["who are you", "what can you do", "help me out"]):
            if len(q) < 60:
                return {
                    "route": "GENERAL",
                    "reason": "Greeting or capability question — no campaign data needed.",
                    "suggested_specialists": [],
                }

        # Domain keyword maps (broader)
        campaign_kw = ["ctr", "creative", "fatigue", "engagement", "ad-level", "creatives", "ad copy"]
        audience_kw = ["audience", "saturat", "overlap", "lookalike", "reach", "exclusion", "targeting", "ad set", "ad-set"]
        bidding_kw  = ["roas", "cpa", "cpc", "bid", "auction", "efficiency", "target_roas", "manual cpc"]
        budget_kw   = ["budget", "pacing", "wast", "spend", "rebalance", "reallocat", "money"]

        domains: List[str] = []
        if any(k in q for k in campaign_kw): domains.append("CampaignAgent")
        if any(k in q for k in audience_kw): domains.append("AudienceAgent")
        if any(k in q for k in bidding_kw):  domains.append("BiddingAgent")
        if any(k in q for k in budget_kw):   domains.append("BudgetAgent")

        # MULTI triggers — diagnostic / cross-domain phrasing
        multi_triggers = ["why is", "why are", "underperform", "diagnose", "root cause",
                          "should we pause", "should we scale", "pause or scale",
                          "what is broken", "what's broken", "is it"]
        is_multi_phrase = any(t in q for t in multi_triggers)

        if is_multi_phrase:
            # Diagnostic questions almost always need 2+ specialists
            base = domains if len(domains) >= 2 else ["CampaignAgent", "AudienceAgent", "BiddingAgent"]
            return {
                "route": "MULTI",
                "reason": "Cross-domain diagnostic question — needs creative + audience + bid specialists.",
                "suggested_specialists": base[:3],
            }

        if not domains:
            return {
                "route": "GENERAL",
                "reason": "Greeting or general question outside the data scope.",
                "suggested_specialists": [],
            }

        if len(domains) >= 2:
            return {
                "route": "MULTI",
                "reason": "Question references multiple domains.",
                "suggested_specialists": domains[:3],
            }

        single = domains[0]
        return {
            "route": single.replace("Agent", "").upper(),
            "reason": f"Single-domain question best handled by {single}.",
            "suggested_specialists": [single],
        }

    def invoke(self, messages: Any) -> "MockResponse":
        # Light-weight templated answer for general / synthesis prose.
        text = "\n".join(getattr(m, "content", str(m)) for m in (messages if isinstance(messages, list) else [messages]))
        return MockResponse(
            f"[mock-llm] I am running in offline mode. "
            f"To get full natural-language answers please set OPENAI_API_KEY in .env.\n\n"
            f"Echoed prompt: {text[:200]}..."
        )


class MockResponse:
    def __init__(self, content: str):
        self.content = content


# ---------------- Real / mock factory ----------------

def get_llm(temperature: float = 0.2, max_tokens: int = 800) -> Any:
    """
    Return a chat model. Real ChatOpenAI when OPENAI_API_KEY is present,
    otherwise a MockChatLLM that keeps the demo runnable.
    """
    if FORCE_MOCK or not _has_real_key():
        return MockChatLLM(temperature=temperature)

    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=0.9,
        )
    except Exception as exc:  # pragma: no cover
        print(f"[llm] Falling back to MockChatLLM: {exc}")
        return MockChatLLM(temperature=temperature)


def is_mock(llm: Any) -> bool:
    return isinstance(llm, MockChatLLM)


def safe_json_extract(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first JSON object out of an LLM response, tolerant of code fences."""
    if not text:
        return None
    # Strip ```json fences
    cleaned = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    # Find first balanced JSON object
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

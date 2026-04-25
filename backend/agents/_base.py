"""
_base.py
--------
Shared scaffolding for the four specialist agents.

Each specialist:
  1. Has its OWN scoped tool list (passed in by the calling module).
  2. Has its OWN system prompt / persona.
  3. Is a tool-calling LangChain agent built from `create_tool_calling_agent`.
  4. Falls back to invoking its tools directly when running on the MockLLM
     (so the demo still produces useful output offline).

Specialist isolation: a specialist NEVER imports another specialist's tools.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from ..llm import MockChatLLM, get_llm, is_mock


def _direct_tool_dispatch(query: str, tools: List[Any]) -> Dict[str, Any]:
    """
    Mock-mode shortcut. We don't have a real LLM, so we parse the query for
    obvious IDs (NB001..NB022 / AS001..AS060) and call every tool whose
    signature accepts that ID. This keeps the demo working offline.
    """
    cid_match = re.search(r"\bNB\d{3}\b", query.upper())
    aid_match = re.search(r"\bAS\d{3}\b", query.upper())
    cid = cid_match.group(0) if cid_match else "NB001"
    aid = aid_match.group(0) if aid_match else "AS001"

    results: List[Dict[str, Any]] = []
    for t in tools:
        try:
            param_name = next(iter(t.args_schema.model_fields.keys())) if t.args_schema else "campaign_id"
        except Exception:
            param_name = "campaign_id"

        arg = aid if "ad_set" in param_name else cid
        if "account" in param_name:
            arg = "cars24-main"
        try:
            result = t.invoke({param_name: arg})
        except Exception as exc:  # pragma: no cover
            result = {"error": str(exc)}
        results.append({"tool": t.name, "args": {param_name: arg}, "result": result})

    return {
        "answer": f"[mock-llm] Ran {len(results)} tool(s): {[r['tool'] for r in results]}.",
        "tool_calls": results,
    }


def run_specialist(
    *,
    name: str,
    system_prompt: str,
    tools: List[Any],
    query: str,
    chat_history: Optional[List[Dict[str, str]]] = None,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    """
    Execute a specialist's reasoning loop.

    Returns:
        {
          "agent": str,
          "answer": str,
          "tool_calls": [ {tool, args, result}, ... ],
        }
    """
    llm = get_llm(temperature=temperature, max_tokens=600)

    if is_mock(llm):
        out = _direct_tool_dispatch(query, tools)
        out["agent"] = name
        return out

    # Real LangChain tool-calling agent
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder("chat_history", optional=True),
        ("user", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=False,
        return_intermediate_steps=True,
        handle_parsing_errors=True,
        max_iterations=4,
    )

    history_msgs = []
    if chat_history:
        for turn in chat_history:
            history_msgs.append(("human", turn["content"]) if turn["role"] == "user" else ("ai", turn["content"]))

    response = executor.invoke({"input": query, "chat_history": history_msgs})

    tool_calls: List[Dict[str, Any]] = []
    for action, observation in response.get("intermediate_steps", []):
        tool_calls.append({
            "tool": getattr(action, "tool", "unknown"),
            "args": getattr(action, "tool_input", {}),
            "result": observation,
        })

    return {
        "agent": name,
        "answer": response.get("output", ""),
        "tool_calls": tool_calls,
    }

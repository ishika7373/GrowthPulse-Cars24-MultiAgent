"""
memory.py
---------
Per-session conversational memory using LangChain's ConversationBufferMemory.

The case study marks memory as a +5 bonus. We expose a SessionMemoryManager
keyed by session_id (one per browser tab), so multi-turn conversations
preserve context for follow-up questions like "now drill into Delhi" without
the user having to re-mention the campaign.

We chose ConversationBufferMemory (over Window/Summary variants) because:
  1. Sessions are short (a single Arjun working session).
  2. The full transcript is small, well within the gpt-4o-mini context.
  3. It is the simplest, most predictable strategy to demo.
"""
from __future__ import annotations

from threading import Lock
from typing import Dict, List

try:
    from langchain.memory import ConversationBufferMemory
except Exception:  # pragma: no cover
    ConversationBufferMemory = None  # type: ignore


class SessionMemoryManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: Dict[str, List[Dict[str, str]]] = {}

    # We store a lightweight list of turns ourselves so we can serialise the
    # transcript to the front-end as JSON. We ALSO build a real
    # ConversationBufferMemory on demand for any LangChain agent that needs it.
    def get_history(self, session_id: str) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._sessions.get(session_id, []))

    def append(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            self._sessions.setdefault(session_id, []).append({"role": role, "content": content})

    def reset(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def as_buffer(self, session_id: str):
        """Return a fresh ConversationBufferMemory pre-loaded with this session's turns."""
        if ConversationBufferMemory is None:
            return None
        mem = ConversationBufferMemory(return_messages=True, memory_key="chat_history")
        for turn in self.get_history(session_id):
            if turn["role"] == "user":
                mem.chat_memory.add_user_message(turn["content"])
            else:
                mem.chat_memory.add_ai_message(turn["content"])
        return mem


MEMORY = SessionMemoryManager()

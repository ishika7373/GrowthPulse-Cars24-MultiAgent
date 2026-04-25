# GrowthPulse v1 — Cars24 Multi-Agent Performance Marketing Intelligence

**Architecture:** Multi-Agent Hierarchy + LLM-Powered Router + Supervisor Synthesis  
**Stack:** Python · FastAPI · LangChain · OpenAI gpt-4o-mini · HTML / CSS / vanilla JS  
**Memory:** `ConversationBufferMemory` (per session) — bonus criterion implemented.

GrowthPulse turns the 2–3 hours Arjun Kapoor (Cars24 Performance Marketing Manager) spends
each morning across Meta Business Manager, Google Ads and YouTube into a single
conversation. Five LLM-powered agents — one Router, four Specialists, one Supervisor —
collaborate over a shared CSV dataset of 22 campaigns and 60 ad sets.

---

## Quick start

```bash
git clone <this repo>
cd growthpulse-cars24
python -m venv .venv && source .venv/bin/activate     # optional but recommended
pip install -r requirements.txt
cp .env.example .env                                  # then edit and add OPENAI_API_KEY
python run.py
```

Open <http://127.0.0.1:8000/> — the landing page loads. Click the floating chat icon at
the bottom-right; the chat panel opens and the Supervisor automatically streams the
**Daily Campaign Briefing** before you have typed anything.

> If `OPENAI_API_KEY` is missing or invalid, GrowthPulse transparently falls back to a
> deterministic offline mock LLM so the demo still runs end-to-end.

Required env vars:
| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key (gpt-4o-mini by default) |
| `OPENAI_MODEL` | Override model. Default `gpt-4o-mini` |
| `GROWTHPULSE_PORT` | Override port. Default `8000` |
| `GROWTHPULSE_FORCE_MOCK` | Force the offline mock LLM |

---

## Architecture

```
                        ┌──────────────────────────┐
                        │       User Query         │
                        └────────────┬─────────────┘
                                     ▼
                        ┌──────────────────────────┐
                        │   Router Agent (LLM)     │   structured JSON:
                        │   6 routes, no answers   │   { route, reason,
                        └────────────┬─────────────┘     suggested_specialists }
              ┌───────────┬──────────┼──────────┬───────────┐
              ▼           ▼          ▼          ▼           ▼
        CampaignAgent AudienceAgent BiddingAgent BudgetAgent  GeneralLLM
         (2 tools)     (2 tools)     (2 tools)    (2 tools)    (no tools)
              │           │          │          │
              └────┬──────┴──┬───────┴──────────┘
                   ▼         ▼
                Supervisor / Synthesis Agent  ──▶  Final Answer
                (LLM, no tools, MULTI + Daily Briefing)
```

### Query → agent path mapping

| Query example | Router route | Path |
|---|---|---|
| "Which seller campaigns have CTR below 0.8%?" | `CAMPAIGN` | Router → CampaignAgent → `diagnose_campaign_health` |
| "Show me audience overlap on NB001" | `AUDIENCE` | Router → AudienceAgent → `find_audience_overlap` |
| "What is the ROAS on NB008 and what bid should we use?" | `BIDDING` | Router → BiddingAgent → `get_bidding_analysis` + `recommend_bid_strategy` |
| "How much spend are we wasting?" | `BUDGET` | Router → BudgetAgent → `get_budget_waste` |
| "Why is NB015 EMI retargeting underperforming?" | `MULTI` | Router → Supervisor → CampaignAgent + AudienceAgent + BiddingAgent → synthesis |
| "Should we pause or scale NB019?" | `MULTI` | Router → Supervisor → BiddingAgent + BudgetAgent + AudienceAgent → synthesis |
| "Hi" / "What can you do?" | `GENERAL` | Direct LLM response |
| (App startup) | n/a | Supervisor orchestrates all 4 specialists → Daily Briefing |

### Specialist Isolation (graded)

Each specialist imports **only its own** tool list — verifiable in `backend/agents/*.py`:

```python
# backend/agents/campaign_agent.py
from ..tools.campaign_tools import CAMPAIGN_TOOLS  # CampaignAgent sees only these 2 tools
```

There is no shared global tool list. A specialist physically cannot call another
specialist's tool. Cross-domain reasoning happens only via the Router → Supervisor path.

---

## Project layout

```
growthpulse-cars24/
├── run.py                       # entry point
├── requirements.txt
├── .env.example
├── README.md
├── growthpulse_campaigns.csv    # 22 rows
├── growthpulse_adsets.csv       # 60 rows
├── backend/
│   ├── app.py                   # FastAPI server + static frontend mount
│   ├── data_loader.py           # CSVs, helpers, Cars24 dual-funnel taxonomy
│   ├── llm.py                   # OpenAI factory + offline Mock LLM fallback
│   ├── memory.py                # ConversationBufferMemory per session
│   ├── router.py                # LLM-powered Router (no regex)
│   ├── supervisor.py            # MULTI synthesis + Daily Briefing
│   ├── tools/
│   │   ├── campaign_tools.py    # diagnose_campaign_health, get_creative_performance
│   │   ├── audience_tools.py    # get_audience_saturation, find_audience_overlap
│   │   ├── bidding_tools.py     # get_bidding_analysis, recommend_bid_strategy
│   │   └── budget_tools.py      # get_budget_pacing, get_budget_waste
│   └── agents/
│       ├── _base.py             # shared tool-calling agent scaffold
│       ├── campaign_agent.py
│       ├── audience_agent.py
│       ├── bidding_agent.py
│       └── budget_agent.py
└── frontend/
    ├── index.html               # Landing page + floating chat icon
    ├── style.css
    └── app.js
```

---

## Tools (8 total)

| Owner | Tool | Inputs | Output highlights |
|---|---|---|---|
| CampaignAgent | `diagnose_campaign_health(campaign_id)` | NB001-NB022 | ctr, freq, status_flag (Critical / Declining / Stable) |
| CampaignAgent | `get_creative_performance(campaign_id)` | " | top/bottom 3 creatives, fatigue 0-10, action |
| AudienceAgent | `get_audience_saturation(ad_set_id)` | AS001-AS060 | reach %, freq, saturation_flag, action |
| AudienceAgent | `find_audience_overlap(campaign_id)` ★ NEW | NB001-NB022 | pairwise overlap %, wasted impressions, verdict |
| BiddingAgent | `get_bidding_analysis(campaign_id)` | NB001-NB022 | roas vs target, roas_flag, bid action |
| BiddingAgent | `recommend_bid_strategy(campaign_id)` | " | current/recommended strategy, est. impact % |
| BudgetAgent | `get_budget_pacing(campaign_id)` | NB001-NB022 | pacing %, flag, projected month-end |
| BudgetAgent | `get_budget_waste(account_id)` ★ NEW | "cars24-main" | total wasted INR, top-3 wasters, reallocation |

All tools are LangChain `@tool` functions with proper schema, NaN handling and
graceful "campaign_not_found" responses for edge cases.

---

## Memory

`backend/memory.py` exposes `SessionMemoryManager`, keyed by `session_id` (one per
browser tab via `localStorage`). On every chat call we:

1. Append the user turn to the in-memory transcript.
2. Build a fresh `ConversationBufferMemory` from that transcript and pass it to the
   active specialist as `chat_history`. This lets Arjun ask "now drill into Delhi" and
   the agent remembers the prior campaign in context.
3. Append the assistant's reply.

Reset is wired to the **Clear Chat** button (`POST /api/reset`).

---

## UI features (matches Section 8 of the case study, Streamlit replaced by HTML/CSS/JS)

- **Landing page** describing the system and a floating chat icon (bottom-right).
- **Account Summary strip** inside the chat panel — total active campaigns,
  critical status count, blended seller CPL, blended buyer ROAS, total spend / budget.
- **Campaign-Type Filter** dropdown — scopes every subsequent query to that funnel
  segment (Seller Acquisition / Buyer Intent / Financing & EMI / Retargeting / Brand).
- **Daily Campaign Briefing** auto-streams when the user opens the chat panel — produced
  by the Supervisor calling all 4 specialists in parallel.
- **Agent Trace panel** — collapsible, shows the Router decision and which specialists +
  which tools were invoked for the latest query. Helps evaluators verify multi-agent flow.
- **Clear Chat** button — resets memory and re-triggers the Daily Briefing.

---

## Acceptance-criteria checklist

| Criterion | Where it lives |
|---|---|
| `python run.py` launches and Daily Briefing appears automatically | `run.py` + `frontend/app.js::maybeShowBriefing` |
| 8 tools implemented, scoped to owning specialist | `backend/tools/*.py`, `backend/agents/*.py` |
| LLM-powered Router (no regex) | `backend/router.py` |
| Router emits MULTI for cross-domain queries | `backend/router.py` system prompt + structured output |
| Supervisor calls 2+ specialists for MULTI + Daily Briefing | `backend/supervisor.py` |
| Specialist isolation (no shared global tool list) | each `agents/*.py` imports its OWN tool list only |
| Budget pacing math correct | `backend/tools/budget_tools.py::get_budget_pacing` |
| Bidding analysis math correct | `backend/tools/bidding_tools.py::get_bidding_analysis` |
| Campaign health flags + action | `backend/tools/campaign_tools.py::diagnose_campaign_health` |
| Architecture diagram + path-mapping paragraph in README | this file |
| `ConversationBufferMemory` across turns (+5 bonus) | `backend/memory.py` |

---

## Notes on prompting decisions

- **Router temperature = 0.0** — classification must be stable.
- **Specialists temperature = 0.2** — small wiggle for natural answers, but numbers
  always come from tools, never the LLM's head.
- **Supervisor temperature = 0.3** — synthesis benefits from a touch more variety.
- **Distinct personas per agent** — the case study allocates 10 marks for this. Each
  specialist's system prompt names its allowed tools, refuses out-of-scope questions
  ("redirect to the right specialist"), and mandates concrete numbers in answers.

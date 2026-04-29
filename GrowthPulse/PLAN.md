ddddd# GrowthPulse v2 — Dashboard + Upload + Gemini

## Context

GrowthPulse currently boots from two bundled Cars24 CSVs and routes queries through a 5-agent hierarchy on top of OpenAI `gpt-4o-mini`, with a deterministic mock-LLM fallback. The landing page mixes the business pitch (hero stats, daily briefing) with the tech explainer (architecture diagram, agent cards, funnel grid) into a single scroll.

Three changes are needed:

1. **UI overhaul** — split the page into a business dashboard zone (metrics + issue cards + upload) and a separate tech-explainer zone (architecture / agents / funnel) below.
2. **Ads analysis on uploaded data** — let users upload CSV or XLSX. Header-detect whether the file is the bundled GrowthPulse schema or a native Google Ads / Meta Ads export, normalize, and re-run the existing analysis tools against the uploaded data.
3. **Gemini-only LLM** — replace OpenAI with `gemini-3.1-flash-lite-preview` via the `GEMINI_API_KEY` already in `.env`.

**Decisions locked with the user:** schema = both (header-detected); provider = Gemini-only; upload storage = per-session in-memory (lost on restart, multi-user safe).

## Architecture changes at a glance

```
Singleton DATA  →  SessionDataManager keyed by session_id, with
                   "demo" default that mirrors today's bundled CSVs.
                   Tools read the active store via a contextvars.ContextVar
                   set at /api/chat and /api/briefing entry.
ChatOpenAI      →  ChatGoogleGenerativeAI (langchain-google-genai).
Single-zone     →  Two-zone landing page: Dashboard above, Tech below.
landing page
```

## Implementation plan

### A. Gemini-only provider

**Files:** `requirements.txt`, `.env.example`, `render.yaml`, `backend/llm.py`, `backend/app.py::_llm_mode`

- `requirements.txt`: drop `langchain-openai`; add `langchain-google-genai`, `openpyxl`.
- `backend/llm.py`:
  - `_has_real_key()` checks `GEMINI_API_KEY` starts with `AIza` and isn't a placeholder.
  - `get_llm(temperature, max_tokens)` returns `ChatGoogleGenerativeAI(model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview"), temperature=..., max_output_tokens=...)` when a real key is set; else `MockChatLLM` (unchanged).
  - `MockChatLLM`, `safe_json_extract` stay as-is — both Router (`with_structured_output(RouterDecision)`) and specialists (`create_tool_calling_agent`) work identically against `langchain-google-genai`.
- `_llm_mode()` in `backend/app.py` returns `"gemini" | "mock"`.
- `.env.example`: drop `OPENAI_API_KEY` / `OPENAI_MODEL`; add `GEMINI_API_KEY=`, `GEMINI_MODEL=gemini-3.1-flash-lite-preview`.
- `render.yaml`: replace `OPENAI_*` envvars with `GEMINI_API_KEY` (sync:false) + `GEMINI_MODEL`.
- The system-prompt strings in `router.py`, `supervisor.py`, and each `agents/*.py` are model-agnostic — no changes.

Verify by grepping for `OPENAI` and `langchain_openai` after the edit and removing every hit outside docs that explicitly describe the old config.

### B. Per-session DataStore + contextvar plumbing

**Files:** `backend/data_loader.py`, `backend/tools/{campaign,audience,bidding,budget}_tools.py`, `backend/supervisor.py`, `backend/app.py`

- In `backend/data_loader.py`:
  - Keep the dataclass `_DataStore` and the existing `_load()` for the demo store.
  - Add `DEMO_STORE = _load()` (rename of today's `DATA`). Keep a deprecated `DATA = DEMO_STORE` alias to avoid breaking any straggler import — to be removed after the tools migration.
  - New `SessionDataManager` with `_lock`, `_stores: Dict[str, _DataStore]`, methods `set(session_id, store)`, `get(session_id) -> _DataStore | None`, `clear(session_id)`. Singleton `SESSIONS = SessionDataManager()`.
  - New `_ACTIVE_SESSION: ContextVar[str | None] = ContextVar("active_session", default=None)`.
  - New `current_store() -> _DataStore`: reads `_ACTIVE_SESSION.get()`, returns `SESSIONS.get(sid) or DEMO_STORE`.
  - `account_summary()` becomes session-aware: reads `current_store()` instead of the global `DATA`.
- Tools (`backend/tools/*.py`): change every `DATA.campaigns` / `DATA.adsets` / `DATA.campaign(...)` etc. to `current_store().campaigns` / etc. There are no other `DATA` consumers worth keeping.
- `backend/supervisor.py::_flagged_campaigns_for_briefing` and `_build_issue_cards`: same swap.
- `backend/app.py`:
  - In `/api/chat` and `/api/briefing` (and any new `/api/upload` / `/api/data-source` handler that needs to read), wrap the body with `_ACTIVE_SESSION.set(req.session_id)` and reset on exit (use a small context manager).
  - `/api/account-summary` now takes optional `?session_id=` query so the dashboard reflects the right dataset.

### C. Upload pipeline

**New files:** `backend/upload_normalizer.py`
**Modified:** `backend/app.py`, `requirements.txt` (already covered: `openpyxl`)

- `backend/upload_normalizer.py`:
  - `read_table(file_bytes, filename)` — `read_csv` for `.csv`, `read_excel` for `.xlsx` / `.xls`.
  - `detect_schema(df) -> Literal["growthpulse","google_ads","meta_ads","unknown"]`. Logic:
    - GrowthPulse: header set ⊇ `{campaign_id, ctr, roas, target_roas, frequency, daily_budget, spend_so_far}`.
    - Google Ads: header set ⊇ `{Campaign, Cost, Impressions, Clicks}` (with `Conv. value` / `Conversions` for ROAS).
    - Meta Ads: header set ⊇ `{Campaign name, Amount spent, Impressions, Link clicks}` (or similar Meta export labels).
  - `normalize_google_ads(df) -> (campaigns_df, adsets_df)` — maps columns to GrowthPulse schema, derives `ctr = Clicks/Impressions*100`, `roas = Conv. value / Cost`, `target_roas = 2.0` default, mints synthetic `campaign_id`s (`UP001`, `UP002`, …), fabricates one ad-set row per campaign so audience tools still respond.
  - `normalize_meta_ads(df)` — same shape, different source columns; uses `Frequency` if present, else fills with the dataset average; reach inferred from `Reach` column when present.
  - `normalize_growthpulse(campaigns_df, adsets_df)` — minimal: just enforce column types and uppercase IDs (mirror today's `_load()` post-processing).
  - Returns a `_DataStore` ready to drop into `SessionDataManager`.
- `backend/app.py`:
  - `POST /api/upload` — multipart accepting up to two files. Read each file → detect schema → normalize → build `_DataStore` → `SESSIONS.set(session_id, store)`. Response: `{schema_detected, n_campaigns, n_adsets, warnings: [...], summary: account_summary()}`. Errors return 400 with a structured `{error, hint}` payload.
  - `POST /api/data-source` — `{session_id, source: "demo"}` clears the session override (back to demo). `source: "uploaded"` is implicit after a successful upload.
  - `GET /api/data-source?session_id=...` — returns `{source: "demo"|"uploaded", schema, n_campaigns, n_adsets}`.
  - All four wrapped with the `_ACTIVE_SESSION` contextvar.

### D. UI overhaul — two zones

**Files:** `frontend/index.html`, `frontend/app.js`, `frontend/style.css`

#### Zone 1 — Business dashboard (top of page)

Replace the current single-column hero with a dashboard layout:

- **Top strap**: title + one-line product description + `Chat with GrowthPulse →` CTA.
- **Account Summary card row**: 5 metric tiles (Active / Critical / Seller CPL / Buyer ROAS / Spend vs Budget) — same data shape as today's `acct-strip`, promoted out of the chat panel.
- **Data source widget**: pill showing `Data: Cars24 demo` or `Data: Uploaded · <filename>` + `Upload CSV/XLSX` button + `Reset to demo` link.
- **Issue cards grid**: render `issue_cards[]` from `/api/briefing` directly on the landing page (today they only appear inside the chat panel). Each card keeps its severity, metrics, action, specialist chips, and the existing `Drill in →` button which opens the chat with the prefilled `drill_in_query`.
- **Funnel strip**: condensed horizontal version of today's `#funnel-grid`.

#### Zone 2 — Tech explainer (below dashboard)

- New `<section>` "How GrowthPulse works" containing today's Architecture diagram + Agent cards. Funnel section folds into the dashboard strip and is removed from here.
- Nav links collapse to: `Dashboard` · `How it works` · LLM/data pills.

#### Upload modal

- Recovered from removed commit `40e6a9d` (`git show 40e6a9d -- frontend/`) and adapted: drag-drop zone (1 or 2 files), live schema detection preview after drop (`Detected: Google Ads · 18 rows`), `Use this dataset` and `Cancel` buttons. CSS lifted from the same commit.
- After successful upload, app.js refetches `/api/account-summary`, `/api/briefing`, and `/api/data-source` and rerenders dashboard tiles + issue cards. Chat history is preserved.

#### Chat panel changes

- Remove the inline issue-cards block (now on the landing page); keep the conversation, quick prompts, trace panel, and a one-line "Daily briefing — see dashboard above" link. Account summary strip stays in the chat header for at-a-glance reference during chat.

### E. Files touched

| Path | Reason |
|---|---|
| `requirements.txt` | drop `langchain-openai`; add `langchain-google-genai`, `openpyxl` |
| `.env.example`, `render.yaml` | swap OpenAI env vars for Gemini |
| `backend/llm.py` | Gemini factory; remove OpenAI |
| `backend/data_loader.py` | `SessionDataManager`, `current_store()`, contextvar |
| `backend/upload_normalizer.py` (new) | header detection + Google Ads / Meta Ads / GrowthPulse normalizers |
| `backend/app.py` | new endpoints, contextvar plumbing, llm-mode pill |
| `backend/tools/*_tools.py` (4 files) | `DATA.x` → `current_store().x` |
| `backend/supervisor.py` | same swap inside the briefing helpers |
| `frontend/index.html` | two-zone layout + upload modal + data pill |
| `frontend/app.js` | landing-page issue-card render, upload form wiring, dataset state |
| `frontend/style.css` | dashboard tiles, upload modal, data pill, layout split |

### F. Reused code / inputs (do not duplicate)

- `backend/supervisor.py::_build_issue_cards` already returns dashboard-ready cards (severity, metrics, action, specialists, drill-in query). The landing page should call this — not write a parallel renderer.
- `backend/data_loader.py::account_summary()` and `CAMPAIGN_TYPE_BREAKDOWN` drive the metric tiles and funnel strip.
- The eight existing `@tool` functions stay unchanged in contract — only their data accessor flips from `DATA` to `current_store()`.
- `MockChatLLM` and `safe_json_extract` stay as-is — they're provider-agnostic.
- Upload modal HTML/CSS skeleton lives in commit `40e6a9d`; recover with `git show 40e6a9d -- frontend/` and trim to current style.

### G. Verification

1. **Local cold boot.**
   ```
   pip install -r requirements.txt
   python run.py
   ```
   - `GET /api/health` → `{"ok":true,"llm":"gemini",...}`.
   - Landing page shows the new two-zone layout with the bundled Cars24 demo metrics.
   - Click `Chat` → ask "Which seller campaigns have CTR below 0.8%?" → Gemini answers, trace panel shows `diagnose_campaign_health` tool call.
2. **GrowthPulse-schema upload.** Upload `growthpulse_campaigns.csv` + `growthpulse_adsets.csv` (the bundled files) → response says `schema_detected:"growthpulse"`, dashboard renumbers identically.
3. **Google Ads upload.** Synthetic 3-row CSV with `Campaign,Cost,Impressions,Clicks,Conversions,Conv. value` → `schema_detected:"google_ads"`, dashboard re-renders with `UP001..UP003`, chat query "diagnose UP002" returns numbers from the uploaded row.
4. **Meta upload.** Same with `Campaign name,Amount spent,Impressions,Link clicks,Frequency,Reach`.
5. **Per-session isolation.** Open a second browser tab, do nothing — confirm it still shows the demo dashboard while tab 1 shows the uploaded numbers.
6. **Reset.** "Reset to demo" → `POST /api/data-source` with `source:"demo"` → dashboard returns to bundled numbers.
7. **Mock fallback.** `GROWTHPULSE_FORCE_MOCK=true python run.py` → Router still classifies, specialists still dispatch tools, upload still works (pure pandas).
8. **Render deploy.** Push to `main`. Set `GEMINI_API_KEY` in Render dashboard. Visit deployed URL → repeat upload test. Restart the service from Render → confirm uploaded data is gone (expected) and demo data is back.

### H. Out of scope

- Persisting uploaded datasets across restarts (would need S3/R2 — explicitly deferred per the user's choice of per-session in-memory).
- Multi-file Google Ads exports (campaign + ad-group + audience as separate exports).
- Streaming/chunked uploads for very large files; v1 caps at a soft 5 MB per file.
- Re-checking the LLM mode pill mid-session if `GEMINI_API_KEY` rotates — set once at boot is fine.

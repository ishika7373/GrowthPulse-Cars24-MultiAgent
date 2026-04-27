/**
 * GrowthPulse v1 — front-end controller (vanilla JS).
 *
 * Responsibilities:
 *   - Boot: fetch /api/health + /api/account-summary + /api/briefing
 *           and populate hero stats + Account Summary strip.
 *   - Chat: POST /api/chat with {session_id, query, campaign_type}.
 *   - Render: agent-trace chips, Markdown-ish formatting, multi-turn UI.
 *   - Memory: server-side ConversationBufferMemory keyed on session_id.
 */

(function () {
  const API = ""; // same origin
  const SESSION_ID = localStorage.getItem("gp.session") || ("sess_" + Math.random().toString(36).slice(2, 10));
  localStorage.setItem("gp.session", SESSION_ID);

  // ------- DOM helpers -------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // ------- State -------
  let briefingShown = false;          // has the chat already rendered the briefing?
  let briefingPromise = null;         // single shared fetch — used by hero card + chat panel
  let funnelData = null;

  // ------- API wrappers -------
  async function api(path, opts) {
    const res = await fetch(API + path, opts || {});
    if (!res.ok) throw new Error(path + " failed: " + res.status);
    return res.json();
  }
  const apiGET  = (p)    => api(p);
  const apiPOST = (p, b) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });

  // ------- Format helpers -------
  function inr(n) {
    if (n == null) return "—";
    if (n >= 1e7) return "₹" + (n / 1e7).toFixed(2) + " Cr";
    if (n >= 1e5) return "₹" + (n / 1e5).toFixed(2) + " L";
    if (n >= 1000) return "₹" + (n / 1000).toFixed(1) + "k";
    return "₹" + Math.round(n);
  }

  // Convert the supervisor's markdown-style response into safe HTML.
  // Block-level parser: split into lines, group consecutive bullet lines,
  // wrap **bold** sections as headers, leave everything else as paragraphs.
  function formatAnswer(text) {
    if (!text) return "";
    const lines = String(text).replace(/\r/g, "").split("\n");
    const out = [];
    let listBuf = [];
    let paraBuf = [];

    const flushList = () => {
      if (!listBuf.length) return;
      out.push("<ul>" + listBuf.map(l => `<li>${formatInline(l)}</li>`).join("") + "</ul>");
      listBuf = [];
    };
    const flushPara = () => {
      if (!paraBuf.length) return;
      const joined = paraBuf.join(" ").trim();
      if (joined) out.push(`<p>${formatInline(joined)}</p>`);
      paraBuf = [];
    };

    for (const raw of lines) {
      const line = raw.trim();
      const bullet = line.match(/^[-•]\s+(.+)$/);
      const header = line.match(/^\*\*(.+?)\*\*\s*:?$/);

      if (bullet) {
        flushPara();
        listBuf.push(bullet[1]);
      } else if (header) {
        flushPara();
        flushList();
        out.push(`<h4 class="ans-h">${escapeHtml(header[1])}</h4>`);
      } else if (line === "") {
        flushPara();
        flushList();
      } else {
        flushList();
        paraBuf.push(line);
      }
    }
    flushList();
    flushPara();
    return out.join("\n");
  }

  // Inline formatter for **bold**, `code`, and HTML escaping.
  function formatInline(text) {
    let s = escapeHtml(text);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    return s;
  }

  // ------- Boot -------
  async function boot() {
    try {
      const [health, summary, campaigns] = await Promise.all([
        apiGET("/api/health"),
        apiGET("/api/account-summary"),
        apiGET("/api/campaigns"),
      ]);
      $("#llm-mode-pill").textContent = "LLM: " + (health.llm === "openai" ? "OpenAI gpt-4o-mini" : "Mock (offline)");
      paintDataSource(health.data_source);
      paintAccountSummary(summary);
      paintFunnel(summary);
      funnelData = { summary, campaigns };

      // Kick off the Daily Briefing fetch immediately so the hero preview
      // populates without waiting for the user to open the chat panel.
      // The same promise is reused when the chat opens — no duplicate calls.
      kickoffBriefing();
    } catch (e) {
      console.warn("boot failed", e);
    }
  }

  function kickoffBriefing() {
    if (briefingPromise) return briefingPromise;
    briefingPromise = apiGET("/api/briefing")
      .then(res => { paintHeroBriefing(res); return res; })
      .catch(err => { paintHeroBriefingError(err); throw err; });
    return briefingPromise;
  }

  function paintHeroBriefing(res) {
    const ul = $("#hero-briefing-bullets");
    if (!ul) return;
    const lines = (res.answer || "")
      .split(/\n+/)
      .map(l => l.trim())
      .filter(l => /^[-•]\s+/.test(l))
      .map(l => l.replace(/^[-•]\s+/, ""))
      .slice(0, 3);
    if (lines.length) {
      ul.innerHTML = lines.map(l => `<li>${escapeHtml(l)}</li>`).join("");
    } else {
      // Fallback: take the first non-header sentence
      const text = (res.answer || "").replace(/\*\*[^*]+\*\*/g, "").trim();
      ul.innerHTML = `<li>${escapeHtml(text.slice(0, 240))}${text.length > 240 ? "…" : ""}</li>`;
    }
    const status = $("#hero-briefing-status");
    if (status) {
      const ag = (res.specialists_consulted || []).length;
      status.textContent = `Synthesised from ${ag} specialist${ag === 1 ? "" : "s"} just now.`;
    }
    document.querySelector(".hero-card")?.classList.add("ready");
  }

  function paintHeroBriefingError(err) {
    const ul = $("#hero-briefing-bullets");
    if (ul) ul.innerHTML = `<li style="color:var(--bad);">Could not load briefing: ${escapeHtml(err.message || String(err))}</li>`;
    const status = $("#hero-briefing-status");
    if (status) status.textContent = "Briefing failed — check the server logs.";
  }

  function paintAccountSummary(s) {
    $("#stat-campaigns").textContent = s.total_active_campaigns;
    $("#stat-critical").textContent  = s.campaigns_with_critical_status;
    $("#stat-cpl").textContent       = inr(s.blended_seller_cpl_inr);
    $("#stat-roas").textContent      = (s.blended_buyer_roas || 0).toFixed(2) + "×";

    $("#acct-active").textContent    = s.total_active_campaigns;
    $("#acct-critical").textContent  = s.campaigns_with_critical_status;
    $("#acct-cpl").textContent       = inr(s.blended_seller_cpl_inr);
    $("#acct-roas").textContent      = (s.blended_buyer_roas || 0).toFixed(2) + "×";
    $("#acct-spend").textContent     = inr(s.total_spend_so_far_inr) + " / " + inr(s.total_daily_budget_inr);
  }

  function paintFunnel(s) {
    const grid = $("#funnel-grid");
    grid.innerHTML = "";
    const idMap = {
      "Seller Acquisition": "NB001–NB007",
      "Buyer Intent":       "NB008–NB014",
      "Financing & EMI":    "NB015–NB018",
      "Retargeting":        "NB019–NB020",
      "Brand Awareness":    "NB021–NB022",
    };
    for (const [k, v] of Object.entries(s.campaign_type_breakdown)) {
      const div = document.createElement("div");
      div.className = "funnel-card";
      div.innerHTML = `
        <h4>${k}</h4>
        <div class="count">${v}</div>
        <div class="ids">${idMap[k] || ""}</div>
      `;
      grid.appendChild(div);
    }
  }

  // ------- Briefing on chat open -------
  async function maybeShowBriefing() {
    if (briefingShown) return;
    briefingShown = true;
    const dash = $("#briefing-dash");
    if (dash) dash.hidden = false;

    // If the new dashboard section is present, show its loading state.
    // Otherwise (old HTML cached) drop a placeholder chat bubble.
    let placeholder = null;
    if (_hasBriefingDash()) {
      paintBriefingLoading();
    } else {
      placeholder = appendBot('<span class="typing"><span></span><span></span><span></span></span> Generating Daily Campaign Briefing…');
    }

    try {
      const res = await kickoffBriefing();
      if (placeholder) placeholder.remove();
      paintBriefingDashboard(res);
      paintTrace({
        route: "DAILY_BRIEFING",
        specialists_consulted: res.specialists_consulted || [],
        tool_calls: (res.specialist_outputs || []).map(o => ({ specialist: o.agent, tool_calls: o.tool_calls || [] })),
      });
    } catch (e) {
      if (placeholder) placeholder.innerHTML = "❌ Could not load briefing: " + escapeHtml(e.message || String(e));
      else paintBriefingError(e);
    }
  }

  async function refreshBriefing() {
    briefingPromise = null;          // bust cache
    briefingShown = false;
    const dash = $("#briefing-dash"); if (dash) dash.hidden = false;
    if (_hasBriefingDash()) paintBriefingLoading();
    try {
      const res = await kickoffBriefing();
      paintBriefingDashboard(res);
      paintHeroBriefing(res);
    } catch (e) {
      paintBriefingError(e);
    }
  }

  // Helper: gracefully fall back to chat-bubble briefing when the dashboard
  // section isn't on the page (older cached index.html).
  function _hasBriefingDash() { return !!$("#briefing-cards"); }

  function paintBriefingLoading() {
    const cards = $("#briefing-cards");
    const meta = $("#briefing-meta");
    if (!cards) return;
    cards.innerHTML = `
      <div class="bcard">
        <div class="bcard-title">Supervisor is orchestrating all 4 specialists…</div>
        <div class="bcard-action"><small>This takes ~10-30 seconds with real OpenAI.</small></div>
      </div>`;
    if (meta) meta.textContent = "Supervisor · running…";
  }

  function paintBriefingError(err) {
    const cards = $("#briefing-cards");
    if (!cards) {
      // Fallback: show the error in the chat body so the user sees it
      appendBot(`<strong style="color:var(--bad);">Briefing failed:</strong> ${escapeHtml(err.message || String(err))}`);
      return;
    }
    cards.innerHTML = `
      <div class="bcard severity-critical">
        <div class="bcard-title">Briefing failed</div>
        <div class="bcard-action">${escapeHtml(err.message || String(err))}</div>
      </div>`;
  }

  function paintBriefingDashboard(res) {
    const specialists = res.specialists_consulted || [];
    const cardsEl = $("#briefing-cards");
    const meta = $("#briefing-meta");
    const synth = $("#briefing-synth-body");

    // Fallback path if the new HTML isn't deployed yet (cached index.html).
    if (!cardsEl) {
      const html = specialistsHtml(specialists, "Supervisor") + formatAnswer(res.answer);
      appendBot(html);
      return;
    }

    if (meta) meta.textContent = `Supervisor · synthesised from ${specialists.length} specialist${specialists.length === 1 ? "" : "s"}`;

    const cards = res.issue_cards || [];
    if (!cards.length) {
      cardsEl.innerHTML = `
        <div class="bcard severity-info">
          <div class="bcard-title">No critical issues right now ✓</div>
          <div class="bcard-action">All campaigns are inside healthy CTR / ROAS / pacing bands.</div>
        </div>`;
    } else {
      cardsEl.innerHTML = cards.map(renderCard).join("");
      Array.from(document.querySelectorAll(".bcard-drill")).forEach(btn => {
        btn.addEventListener("click", () => {
          const q = btn.dataset.q;
          if (!q) return;
          $("#chat-input").value = q;
          $("#chat-input").focus();
          sendQuery(q);
        });
      });
    }

    if (synth) synth.innerHTML = formatAnswer(res.answer);
  }

  function renderCard(c) {
    const inrFmt = (n) => {
      if (!n) return "—";
      if (n >= 1e5) return "₹" + (n / 1e5).toFixed(2) + " L";
      if (n >= 1000) return "₹" + (n / 1000).toFixed(1) + "k";
      return "₹" + Math.round(n);
    };
    const metricsHtml = (c.metrics || []).map(m => `
      <div class="bcard-metric flag-${m.flag}">
        <small>${escapeHtml(m.label)}</small>
        <strong>${escapeHtml(m.value)}</strong>
      </div>
    `).join("");
    const specHtml = (c.specialists || []).map(s => `<span class="chip ${s}">${s}</span>`).join("");

    return `
      <article class="bcard severity-${c.severity}">
        <div class="bcard-hd">
          <div class="bcard-title">
            #${c.rank} ${escapeHtml(c.campaign_name)}
            <small>${escapeHtml(c.campaign_id)} · ${escapeHtml(c.channel)} · ${escapeHtml(c.campaign_type)}</small>
          </div>
          <span class="sev-badge severity-${c.severity}">${c.severity}</span>
        </div>
        <div class="bcard-metrics">${metricsHtml}</div>
        <div class="bcard-action">
          <small>RECOMMENDED ACTION · est. impact ${inrFmt(c.estimated_impact_inr)}/day</small><br/>
          ${escapeHtml(c.recommended_action)}
        </div>
        <div class="bcard-foot">
          <div class="bcard-specialists">${specHtml}</div>
          <button class="bcard-drill" data-q="${escapeHtml(c.drill_in_query)}">Drill in →</button>
        </div>
      </article>
    `;
  }

  // ------- Chat send -------
  async function sendQuery(query) {
    if (!query.trim()) return;
    appendUser(query);
    $("#chat-input").value = "";
    setSending(true);
    const placeholder = appendBot('<span class="typing"><span></span><span></span><span></span></span> Routing your question…');

    try {
      const ct = $("#campaign-filter").value;
      const res = await apiPOST("/api/chat", {
        session_id: SESSION_ID,
        query,
        campaign_type: ct === "All Campaigns" ? null : ct,
      });
      placeholder.remove();

      const route = res.router?.route || "GENERAL";
      $("#chat-route-pill").textContent = route;

      const specialists = res.result.specialists_consulted || [];
      const owner = res.result.agent || "GeneralLLM";
      const html = specialistsHtml(specialists.length ? specialists : [owner], owner) + formatAnswer(res.result.answer);
      appendBot(html);

      paintTrace({
        route,
        reason: res.router?.reason,
        specialists_consulted: specialists,
        tool_calls: res.trace?.tool_calls || [],
      });
    } catch (e) {
      placeholder.innerHTML = "❌ Request failed: " + e.message;
    } finally {
      setSending(false);
    }
  }

  function setSending(busy) {
    $("#chat-input").disabled = busy;
    $$(".btn-send").forEach(b => b.disabled = busy);
  }

  function appendUser(text) {
    const div = document.createElement("div");
    div.className = "msg user";
    div.textContent = text;
    $("#chat-body").appendChild(div);
    div.scrollIntoView({ behavior: "smooth", block: "end" });
    return div;
  }
  function appendBot(html) {
    const div = document.createElement("div");
    div.className = "msg bot";
    div.innerHTML = html;
    $("#chat-body").appendChild(div);
    div.scrollIntoView({ behavior: "smooth", block: "end" });
    return div;
  }

  function specialistsHtml(list, owner) {
    if (!list || !list.length) return "";
    const set = Array.from(new Set([owner, ...list].filter(Boolean)));
    return `<div class="specialists">${set.map(s => `<span class="chip ${s}">${s}</span>`).join("")}</div>`;
  }

  function paintTrace(trace) {
    const body = $("#trace-body");
    const route = trace.route || "—";
    const reason = trace.reason || "";
    const chips = (trace.specialists_consulted || []).map(s => `<span class="trace-chip">${s}</span>`).join("");
    const callsHtml = (trace.tool_calls || []).map(group => {
      const tc = (group.tool_calls || []).map(c =>
        `<li><code>${c.tool}</code>(${escapeHtml(JSON.stringify(c.args))})</li>`
      ).join("");
      return `<div><strong>${group.specialist}</strong><ul>${tc || "<li>(no tool calls)</li>"}</ul></div>`;
    }).join("");
    body.innerHTML = `
      <div><span class="trace-chip route">Route: ${route}</span> ${chips}</div>
      ${reason ? `<div class="muted" style="margin-top:6px;">${escapeHtml(reason)}</div>` : ""}
      ${callsHtml ? `<div style="margin-top:8px;">${callsHtml}</div>` : ""}
    `;
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ------- UI wiring -------
  function openChat() {
    $("#chat-panel").classList.add("open");
    $("#chat-panel").setAttribute("aria-hidden", "false");
    setTimeout(maybeShowBriefing, 350);
  }
  function closeChat() {
    $("#chat-panel").classList.remove("open");
    $("#chat-panel").setAttribute("aria-hidden", "true");
  }

  // ------- Data source / upload -------
  function paintDataSource(src) {
    if (!src) return;
    const pill = $("#data-source-pill");
    if (!pill) return;
    if (src.type === "uploaded") {
      pill.textContent = `Using: ${src.campaigns_filename}`;
      pill.title = "Click to upload a different file or reset to demo data";
      pill.classList.add("uploaded");
    } else {
      pill.textContent = "Demo mode · upload optional";
      pill.title = "GrowthPulse works fully on the demo data. Click to upload your own campaigns instead.";
      pill.classList.remove("uploaded");
    }
  }

  function openUpload() {
    const modal = $("#upload-modal");
    if (!modal) return;
    modal.hidden = false;
    ["up-campaigns-name", "up-adsets-name"].forEach(id => {
      const el = $("#" + id); if (el) el.textContent = "No file chosen";
    });
    const status = $("#upload-status");
    if (status) { status.textContent = ""; status.className = "upload-status"; }
    const btn = $("#btn-upload"); if (btn) btn.disabled = true;
    ["zone-campaigns", "zone-adsets"].forEach(id => $("#" + id)?.classList.remove("has-file", "dragover"));
    const c = $("#up-campaigns"); if (c) c.value = "";
    const a = $("#up-adsets"); if (a) a.value = "";
  }

  function closeUpload() { $("#upload-modal").hidden = true; }

  function maybeEnableUpload() {
    const c = $("#up-campaigns")?.files[0];
    const a = $("#up-adsets")?.files[0];
    const btn = $("#btn-upload");
    if (btn) btn.disabled = !(c && a);
  }

  function fileSizeLabel(n) {
    if (!n) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  // Quick line-count of a CSV (excluding header) — gives the user immediate
  // confirmation of how many rows we're about to ingest.
  async function countCsvRows(file) {
    try {
      const txt = await file.text();
      const lines = txt.split(/\r?\n/).filter(l => l.trim().length > 0);
      return Math.max(0, lines.length - 1);
    } catch (_) {
      return null;
    }
  }

  async function showFileMeta(inputId, labelId, zoneId) {
    const f = $("#" + inputId)?.files[0];
    const lbl = $("#" + labelId);
    const zone = $("#" + zoneId);
    if (!lbl) return;
    if (!f) {
      lbl.innerHTML = "No file chosen";
      zone?.classList.remove("has-file");
      return;
    }
    zone?.classList.add("has-file");
    const rows = await countCsvRows(f);
    const meta = `${fileSizeLabel(f.size)}${rows != null ? ` · ${rows} rows` : ""}`;
    lbl.innerHTML = `${escapeHtml(f.name)}<small>${escapeHtml(meta)}</small>`;
  }

  function setupDragDrop(zoneId, inputId) {
    const zone = $("#" + zoneId);
    const input = $("#" + inputId);
    if (!zone || !input) return;
    ["dragenter", "dragover"].forEach(evt =>
      zone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); zone.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach(evt =>
      zone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); zone.classList.remove("dragover"); })
    );
    zone.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files?.[0];
      if (!file) return;
      // Programmatically set the input's FileList using DataTransfer
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  async function doUpload() {
    const c = $("#up-campaigns").files[0];
    const a = $("#up-adsets").files[0];
    if (!c || !a) return;
    const fd = new FormData();
    fd.append("campaigns", c);
    fd.append("adsets", a);
    const status = $("#upload-status");
    status.textContent = "Uploading + validating…";
    status.className = "upload-status";
    $("#btn-upload").disabled = true;
    try {
      const res = await fetch("/api/upload-data", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        status.textContent = "❌ " + (data.errors ? data.errors.join("; ") : "Upload failed");
        status.className = "upload-status error";
        $("#btn-upload").disabled = false;
        return;
      }
      status.textContent = `✅ Loaded ${data.campaigns_rows} campaigns + ${data.adsets_rows} ad sets. Refreshing…`;
      status.className = "upload-status success";
      paintDataSource(data.data_source);
      // Refresh KPIs + briefing + funnel
      const summary = await apiGET("/api/account-summary");
      paintAccountSummary(summary);
      paintFunnel(summary);
      await refreshBriefing();
      setTimeout(() => closeUpload(), 900);
    } catch (e) {
      status.textContent = "❌ " + e.message;
      status.className = "upload-status error";
      $("#btn-upload").disabled = false;
    }
  }

  async function resetData() {
    const status = $("#upload-status");
    status.textContent = "Resetting to demo data…";
    status.className = "upload-status";
    try {
      const res = await apiPOST("/api/reset-data", {});
      paintDataSource(res.data_source);
      const summary = await apiGET("/api/account-summary");
      paintAccountSummary(summary);
      paintFunnel(summary);
      await refreshBriefing();
      status.textContent = "✅ Demo dataset restored.";
      status.className = "upload-status success";
      setTimeout(() => closeUpload(), 900);
    } catch (e) {
      status.textContent = "❌ " + e.message;
      status.className = "upload-status error";
    }
  }

  // Tiny helpers so missing DOM nodes never break wire().
  // (e.g. if browser is showing a cached older index.html.)
  function on(sel, handler) {
    const el = $(sel);
    if (el) el.onclick = handler;
  }
  function bind(sel, evt, handler) {
    const el = $(sel);
    if (el) el.addEventListener(evt, handler);
  }

  function wire() {
    on("#close-chat", closeChat);
    bind("#chat-form", "submit", (e) => {
      e.preventDefault();
      sendQuery($("#chat-input").value);
    });
    $$(".quick").forEach(b => b.onclick = () => {
      $("#chat-input").value = b.dataset.q;
      sendQuery(b.dataset.q);
    });
    on("#trace-toggle", () => $("#trace-panel")?.classList.toggle("collapsed"));
    on("#clear-chat", async () => {
      $("#chat-body").innerHTML = "";
      briefingShown = false;
      try { await apiPOST("/api/reset", { session_id: SESSION_ID }); } catch (_) {}
      maybeShowBriefing();
    });
    on("#briefing-refresh", refreshBriefing);

    // Upload modal wiring — every lookup is null-safe in case the modal HTML
    // isn't deployed yet (e.g. browser cached the older index.html).
    bind("#up-campaigns", "change", async () => {
      await showFileMeta("up-campaigns", "up-campaigns-name", "zone-campaigns");
      maybeEnableUpload();
    });
    bind("#up-adsets", "change", async () => {
      await showFileMeta("up-adsets", "up-adsets-name", "zone-adsets");
      maybeEnableUpload();
    });
    setupDragDrop("zone-campaigns", "up-campaigns");
    setupDragDrop("zone-adsets", "up-adsets");
    bind("#upload-modal", "click", (e) => {
      if (e.target.id === "upload-modal") closeUpload();
    });
  }

  // ------- Public API for inline onclicks -------
  window.GP = { openChat, closeChat, openUpload, closeUpload, doUpload, resetData };

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    boot();
  });
})();

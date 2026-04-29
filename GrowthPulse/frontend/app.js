/**
 * GrowthPulse — front-end controller (vanilla JS, two-view SPA).
 *
 *  Page 1 (#/dashboard, default): business hero, KPIs, charts, top campaigns,
 *                                 ads upload (Google/Meta CSV/XLSX).
 *  Page 2 (#/agents):              architecture + agent cards + embedded chat
 *                                  panel and Daily Briefing dashboard.
 */

(function () {
  const API = "";
  const SESSION_ID = localStorage.getItem("gp.session") || ("sess_" + Math.random().toString(36).slice(2, 10));
  localStorage.setItem("gp.session", SESSION_ID);

  // ------- DOM helpers -------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // ------- State -------
  let briefingShown = false;
  let briefingPromise = null;
  let chartSpendRev = null;
  let chartChannel = null;

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
  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function formatInline(text) {
    let s = escapeHtml(text);
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    return s;
  }
  function formatAnswer(text) {
    if (!text) return "";
    const lines = String(text).replace(/\r/g, "").split("\n");
    const out = []; let listBuf = [], paraBuf = [];
    const flushList = () => { if (listBuf.length) { out.push("<ul>" + listBuf.map(l => `<li>${formatInline(l)}</li>`).join("") + "</ul>"); listBuf = []; } };
    const flushPara = () => { if (paraBuf.length) { const j = paraBuf.join(" ").trim(); if (j) out.push(`<p>${formatInline(j)}</p>`); paraBuf = []; } };
    for (const raw of lines) {
      const line = raw.trim();
      const bullet = line.match(/^[-•]\s+(.+)$/);
      const header = line.match(/^\*\*(.+?)\*\*\s*:?$/);
      if (bullet) { flushPara(); listBuf.push(bullet[1]); }
      else if (header) { flushPara(); flushList(); out.push(`<h4 class="ans-h">${escapeHtml(header[1])}</h4>`); }
      else if (line === "") { flushPara(); flushList(); }
      else { flushList(); paraBuf.push(line); }
    }
    flushList(); flushPara();
    return out.join("\n");
  }

  // ============================================================
  // ROUTING (hash-based)
  // ============================================================
  function currentRoute() {
    const h = (location.hash || "").replace(/^#\/?/, "");
    return h === "agents" ? "agents" : "dashboard";
  }
  function applyRoute() {
    const route = currentRoute();
    $("#view-dashboard").hidden = route !== "dashboard";
    $("#view-agents").hidden    = route !== "agents";
    $$(".nav-link").forEach(a => a.classList.toggle("active", a.dataset.route === route));
    $("#chat-fab").hidden = route !== "agents";
    if (route === "agents" && !briefingShown) {
      setTimeout(openChat, 250);
    }
  }
  window.addEventListener("hashchange", applyRoute);

  // ============================================================
  // BOOT
  // ============================================================
  async function boot() {
    try {
      const [health, summary, _] = await Promise.all([
        apiGET("/api/health"),
        apiGET("/api/account-summary"),
        apiGET("/api/campaigns"),
      ]);
      $("#llm-mode-pill").textContent = "LLM: " + (health.llm === "gemini" ? "Gemini" : "Mock (offline)");
      paintDataSource(health.data_source);
      paintAccountSummary(summary);
      paintFunnel(summary);
      await renderCharts();
      await renderTopCampaigns();
    } catch (e) {
      console.warn("boot failed", e);
    }
  }

  function paintDataSource(src) {
    const pill = $("#data-source-pill");
    if (!pill) return;
    if (!src || src.type === "demo") {
      pill.textContent = "Demo data";
      pill.classList.remove("uploaded");
    } else {
      const lbl = src.platform === "google" ? "Google Ads"
                : src.platform === "meta"   ? "Meta Ads"
                : (src.label || "Uploaded");
      pill.textContent = lbl;
      pill.classList.add("uploaded");
    }
  }

  function paintAccountSummary(s) {
    $("#stat-campaigns").textContent = s.total_active_campaigns;
    $("#stat-critical").textContent  = s.campaigns_with_critical_status;
    $("#stat-spend").textContent     = `${inr(s.total_spend_so_far_inr)} / ${inr(s.total_daily_budget_inr)}`;
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
    if (!grid) return;
    grid.innerHTML = "";
    const idMap = {
      "Seller Acquisition": "NB001–NB007",
      "Buyer Intent":       "NB008–NB014",
      "Financing & EMI":    "NB015–NB018",
      "Retargeting":        "NB019–NB020",
      "Brand Awareness":    "NB021–NB022",
    };
    for (const [k, v] of Object.entries(s.campaign_type_breakdown || {})) {
      const div = document.createElement("div");
      div.className = "funnel-card";
      div.innerHTML = `<h4>${k}</h4><div class="count">${v}</div><div class="ids">${idMap[k] || ""}</div>`;
      grid.appendChild(div);
    }
  }

  // ============================================================
  // CHARTS (Chart.js)
  // ============================================================
  async function renderCharts() {
    const [sr, mix] = await Promise.all([
      apiGET("/api/chart/spend-revenue").catch(() => ({ labels: [], spend: [], revenue: [] })),
      apiGET("/api/chart/channel-mix").catch(() => ({ labels: [], values: [] })),
    ]);
    if (!window.Chart) return;
    const css = getComputedStyle(document.documentElement);
    const accent  = css.getPropertyValue("--accent").trim() || "#6ee7ff";
    const accent2 = css.getPropertyValue("--accent-2").trim() || "#7c5cff";
    const muted   = css.getPropertyValue("--muted").trim() || "#98a2c1";
    const line    = "rgba(255,255,255,0.06)";

    const spendCtx = $("#chart-spend-revenue")?.getContext?.("2d");
    if (spendCtx) {
      if (chartSpendRev) chartSpendRev.destroy();
      chartSpendRev = new Chart(spendCtx, {
        type: "line",
        data: { labels: sr.labels, datasets: [
          { label: "Spend",   data: sr.spend,   borderColor: accent,  backgroundColor: "rgba(110,231,255,0.12)", tension: 0.35, fill: true, borderWidth: 2 },
          { label: "Revenue", data: sr.revenue, borderColor: accent2, backgroundColor: "rgba(124,92,255,0.10)",  tension: 0.35, fill: true, borderWidth: 2 },
        ]},
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: muted, boxWidth: 10 } },
            tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${inr(c.parsed.y)}` } },
          },
          scales: {
            x: { grid: { color: line }, ticks: { color: muted, font: { size: 10 } } },
            y: { grid: { color: line }, ticks: { color: muted, callback: (v) => inr(v), font: { size: 10 } } },
          },
        },
      });
    }
    const mixCtx = $("#chart-channel-mix")?.getContext?.("2d");
    if (mixCtx) {
      if (chartChannel) chartChannel.destroy();
      const palette = [accent, accent2, "#fbbf24", "#34d399", "#f472b6"];
      chartChannel = new Chart(mixCtx, {
        type: "doughnut",
        data: { labels: mix.labels, datasets: [{ data: mix.values, backgroundColor: palette.slice(0, mix.values.length), borderColor: "transparent" }] },
        options: {
          responsive: true, maintainAspectRatio: false, cutout: "62%",
          plugins: {
            legend: { position: "bottom", labels: { color: muted, boxWidth: 12, padding: 12 } },
            tooltip: { callbacks: { label: (c) => `${c.label}: ${inr(c.parsed)}` } },
          },
        },
      });
    }
  }

  // ============================================================
  // TOP CAMPAIGNS TABLE
  // ============================================================
  async function renderTopCampaigns() {
    const tbl = $("#top-campaigns-table tbody");
    if (!tbl) return;
    try {
      const rows = await apiGET("/api/dashboard/top-campaigns?limit=8");
      if (!rows.length) { tbl.innerHTML = `<tr><td colspan="7" class="muted">No active campaigns.</td></tr>`; return; }
      tbl.innerHTML = rows.map(r => `
        <tr class="row" data-q="Why is ${escapeHtml(r.campaign_id)} (${escapeHtml(r.campaign_name)}) underperforming and what should we do?">
          <td><strong>${escapeHtml(r.campaign_name)}</strong><small>${escapeHtml(r.campaign_id)}</small></td>
          <td>${escapeHtml(r.channel)}</td>
          <td>${escapeHtml(r.campaign_type)}</td>
          <td class="num">${inr(r.spend_so_far)}</td>
          <td class="num">${(r.ctr || 0).toFixed(2)}%</td>
          <td class="num">${(r.roas || 0).toFixed(2)}× <small style="color:var(--muted);">/ ${(r.target_roas||0).toFixed(1)}×</small></td>
          <td><span class="tag flag-${r.roas_flag}">${r.roas_flag}</span></td>
        </tr>
      `).join("");
      tbl.querySelectorAll("tr.row").forEach(tr => {
        tr.addEventListener("click", () => {
          location.hash = "#/agents";
          setTimeout(() => {
            openChat();
            const q = tr.dataset.q;
            $("#chat-input").value = q;
            sendQuery(q);
          }, 400);
        });
      });
    } catch (e) {
      tbl.innerHTML = `<tr><td colspan="7" class="muted">Could not load campaigns: ${escapeHtml(e.message)}</td></tr>`;
    }
  }

  async function refreshDashboard() {
    const summary = await apiGET("/api/account-summary");
    paintAccountSummary(summary);
    paintFunnel(summary);
    await renderCharts();
    await renderTopCampaigns();
  }

  // ============================================================
  // ADS UPLOAD (Google / Meta CSV or XLSX, auto-detect)
  // ============================================================
  function fileSizeLabel(n) {
    if (!n) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }
  async function countCsvRows(file) {
    const ext = (file.name || "").split(".").pop().toLowerCase();
    if (ext !== "csv") return null;
    try { return Math.max(0, (await file.text()).split(/\r?\n/).filter(l => l.trim().length).length - 1); }
    catch (_) { return null; }
  }
  async function showFileMeta(inputId, labelId, zoneId) {
    const f = $("#" + inputId)?.files[0];
    const lbl = $("#" + labelId);
    const zone = $("#" + zoneId);
    if (!lbl) return;
    if (!f) { lbl.innerHTML = "No file chosen"; zone?.classList.remove("has-file"); return; }
    zone?.classList.add("has-file");
    const rows = await countCsvRows(f);
    const meta = `${fileSizeLabel(f.size)}${rows != null ? ` · ${rows} rows` : ""}`;
    lbl.innerHTML = `${escapeHtml(f.name)}<small>${escapeHtml(meta)}</small>`;
  }
  function maybeEnableUpload() {
    const c = $("#up-campaigns")?.files[0];
    const btn = $("#btn-upload");
    if (btn) btn.disabled = !c;
  }
  function setupDragDrop(zoneId, inputId) {
    const zone = $("#" + zoneId);
    const input = $("#" + inputId);
    if (!zone || !input) return;
    ["dragenter","dragover"].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); ev.stopPropagation(); zone.classList.add("dragover"); }));
    ["dragleave","drop"].forEach(e => zone.addEventListener(e, ev => { ev.preventDefault(); ev.stopPropagation(); zone.classList.remove("dragover"); }));
    zone.addEventListener("drop", ev => {
      const file = ev.dataTransfer.files?.[0]; if (!file) return;
      const dt = new DataTransfer(); dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  async function doUpload() {
    const c = $("#up-campaigns")?.files[0];
    const a = $("#up-adsets")?.files[0];
    if (!c) return;
    const fd = new FormData();
    fd.append("campaigns", c);
    if (a) fd.append("adsets", a);
    const status = $("#upload-status");
    status.textContent = "Uploading + auto-detecting platform…";
    status.className = "upload-status";
    $("#btn-upload").disabled = true;
    try {
      const res = await fetch("/api/upload-ads", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        status.textContent = "❌ " + (data.error || "Upload failed");
        status.className = "upload-status error";
        $("#btn-upload").disabled = false;
        return;
      }
      const det = data.detection || {};
      status.innerHTML = `✅ ${escapeHtml(det.campaigns_detail || "Loaded")}.  ${data.campaigns_rows} campaigns + ${data.adsets_rows} ad sets active.`;
      status.className = "upload-status success";
      paintDataSource(data.data_source);
      briefingPromise = null; briefingShown = false;
      await refreshDashboard();
    } catch (e) {
      status.textContent = "❌ " + e.message;
      status.className = "upload-status error";
      $("#btn-upload").disabled = false;
    }
  }
  async function resetData() {
    const status = $("#upload-status");
    status.textContent = "Restoring demo dataset…";
    status.className = "upload-status";
    try {
      const res = await apiPOST("/api/reset-data", {});
      paintDataSource(res.data_source);
      briefingPromise = null; briefingShown = false;
      await refreshDashboard();
      status.textContent = "✅ Demo dataset restored.";
      status.className = "upload-status success";
    } catch (e) {
      status.textContent = "❌ " + e.message;
      status.className = "upload-status error";
    }
  }
  function scrollToUpload() {
    location.hash = "#/dashboard";
    setTimeout(() => $("#upload-section")?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  }

  // ============================================================
  // CHAT + BRIEFING (existing logic)
  // ============================================================
  function kickoffBriefing() {
    if (briefingPromise) return briefingPromise;
    briefingPromise = apiGET("/api/briefing");
    return briefingPromise;
  }

  function openChat() {
    $("#chat-panel").classList.add("open");
    $("#chat-panel").setAttribute("aria-hidden", "false");
    setTimeout(maybeShowBriefing, 350);
  }
  function closeChat() {
    $("#chat-panel").classList.remove("open");
    $("#chat-panel").setAttribute("aria-hidden", "true");
  }

  async function maybeShowBriefing() {
    if (briefingShown) return;
    briefingShown = true;
    const dash = $("#briefing-dash"); if (dash) dash.hidden = false;
    paintBriefingLoading();
    try {
      const res = await kickoffBriefing();
      paintBriefingDashboard(res);
      paintTrace({
        route: "DAILY_BRIEFING",
        specialists_consulted: res.specialists_consulted || [],
        tool_calls: (res.specialist_outputs || []).map(o => ({ specialist: o.agent, tool_calls: o.tool_calls || [] })),
      });
    } catch (e) { paintBriefingError(e); }
  }
  async function refreshBriefing() {
    briefingPromise = null; briefingShown = false;
    await maybeShowBriefing();
  }

  function paintBriefingLoading() {
    const cards = $("#briefing-cards"); const meta = $("#briefing-meta");
    if (!cards) return;
    cards.innerHTML = `<div class="bcard"><div class="bcard-title">Supervisor is orchestrating all 4 specialists…</div><div class="bcard-action"><small>~10–30 seconds with real LLM.</small></div></div>`;
    if (meta) meta.textContent = "Supervisor · running…";
  }
  function paintBriefingError(err) {
    const cards = $("#briefing-cards");
    if (!cards) { appendBot(`<strong style="color:var(--bad);">Briefing failed:</strong> ${escapeHtml(err.message || String(err))}`); return; }
    cards.innerHTML = `<div class="bcard severity-critical"><div class="bcard-title">Briefing failed</div><div class="bcard-action">${escapeHtml(err.message || String(err))}</div></div>`;
  }
  function paintBriefingDashboard(res) {
    const specialists = res.specialists_consulted || [];
    const cardsEl = $("#briefing-cards"); const meta = $("#briefing-meta"); const synth = $("#briefing-synth-body");
    if (!cardsEl) { appendBot(specialistsHtml(specialists, "Supervisor") + formatAnswer(res.answer)); return; }
    if (meta) meta.textContent = `Supervisor · synthesised from ${specialists.length} specialist${specialists.length === 1 ? "" : "s"}`;
    const cards = res.issue_cards || [];
    if (!cards.length) {
      cardsEl.innerHTML = `<div class="bcard severity-info"><div class="bcard-title">No critical issues right now ✓</div><div class="bcard-action">All campaigns are inside healthy CTR / ROAS / pacing bands.</div></div>`;
    } else {
      cardsEl.innerHTML = cards.map(renderCard).join("");
      $$(".bcard-drill").forEach(btn => btn.addEventListener("click", () => {
        const q = btn.dataset.q; if (!q) return;
        $("#chat-input").value = q; $("#chat-input").focus(); sendQuery(q);
      }));
    }
    if (synth) synth.innerHTML = formatAnswer(res.answer);
  }
  function renderCard(c) {
    const inrFmt = (n) => !n ? "—" : (n >= 1e5 ? "₹" + (n/1e5).toFixed(2) + " L" : n >= 1000 ? "₹" + (n/1000).toFixed(1) + "k" : "₹" + Math.round(n));
    const metricsHtml = (c.metrics || []).map(m => `<div class="bcard-metric flag-${m.flag}"><small>${escapeHtml(m.label)}</small><strong>${escapeHtml(m.value)}</strong></div>`).join("");
    const specHtml = (c.specialists || []).map(s => `<span class="chip ${s}">${s}</span>`).join("");
    return `
      <article class="bcard severity-${c.severity}">
        <div class="bcard-hd">
          <div class="bcard-title">#${c.rank} ${escapeHtml(c.campaign_name)}<small>${escapeHtml(c.campaign_id)} · ${escapeHtml(c.channel)} · ${escapeHtml(c.campaign_type)}</small></div>
          <span class="sev-badge severity-${c.severity}">${c.severity}</span>
        </div>
        <div class="bcard-metrics">${metricsHtml}</div>
        <div class="bcard-action"><small>RECOMMENDED ACTION · est. impact ${inrFmt(c.estimated_impact_inr)}/day</small><br/>${escapeHtml(c.recommended_action)}</div>
        <div class="bcard-foot"><div class="bcard-specialists">${specHtml}</div><button class="bcard-drill" data-q="${escapeHtml(c.drill_in_query)}">Drill in →</button></div>
      </article>`;
  }

  async function sendQuery(query) {
    if (!query.trim()) return;
    appendUser(query);
    $("#chat-input").value = "";
    setSending(true);
    const placeholder = appendBot('<span class="typing"><span></span><span></span><span></span></span> Routing your question…');
    try {
      const ct = $("#campaign-filter").value;
      const res = await apiPOST("/api/chat", { session_id: SESSION_ID, query, campaign_type: ct === "All Campaigns" ? null : ct });
      placeholder.remove();
      const route = res.router?.route || "GENERAL";
      $("#chat-route-pill").textContent = route;
      const specialists = res.result.specialists_consulted || [];
      const owner = res.result.agent || "GeneralLLM";
      appendBot(specialistsHtml(specialists.length ? specialists : [owner], owner) + formatAnswer(res.result.answer));
      paintTrace({ route, reason: res.router?.reason, specialists_consulted: specialists, tool_calls: res.trace?.tool_calls || [] });
    } catch (e) {
      placeholder.innerHTML = "❌ Request failed: " + e.message;
    } finally { setSending(false); }
  }
  function setSending(b) { $("#chat-input").disabled = b; $$(".btn-send").forEach(x => x.disabled = b); }
  function appendUser(t) { const d = document.createElement("div"); d.className = "msg user"; d.textContent = t; $("#chat-body").appendChild(d); d.scrollIntoView({ behavior:"smooth", block:"end" }); return d; }
  function appendBot(h) { const d = document.createElement("div"); d.className = "msg bot"; d.innerHTML = h; $("#chat-body").appendChild(d); d.scrollIntoView({ behavior:"smooth", block:"end" }); return d; }
  function specialistsHtml(list, owner) {
    if (!list || !list.length) return "";
    const set = Array.from(new Set([owner, ...list].filter(Boolean)));
    return `<div class="specialists">${set.map(s => `<span class="chip ${s}">${s}</span>`).join("")}</div>`;
  }
  function paintTrace(trace) {
    const body = $("#trace-body"); if (!body) return;
    const route = trace.route || "—"; const reason = trace.reason || "";
    const chips = (trace.specialists_consulted || []).map(s => `<span class="trace-chip">${s}</span>`).join("");
    const callsHtml = (trace.tool_calls || []).map(g => {
      const tc = (g.tool_calls || []).map(c => `<li><code>${c.tool}</code>(${escapeHtml(JSON.stringify(c.args))})</li>`).join("");
      return `<div><strong>${g.specialist}</strong><ul>${tc || "<li>(no tool calls)</li>"}</ul></div>`;
    }).join("");
    body.innerHTML = `<div><span class="trace-chip route">Route: ${route}</span> ${chips}</div>${reason ? `<div class="muted" style="margin-top:6px;">${escapeHtml(reason)}</div>` : ""}${callsHtml ? `<div style="margin-top:8px;">${callsHtml}</div>` : ""}`;
  }

  // ============================================================
  // WIRE
  // ============================================================
  function on(sel, h)  { const el = $(sel); if (el) el.onclick = h; }
  function bind(sel, e, h) { const el = $(sel); if (el) el.addEventListener(e, h); }

  function wire() {
    on("#close-chat", closeChat);
    bind("#chat-form", "submit", (e) => { e.preventDefault(); sendQuery($("#chat-input").value); });
    $$(".quick").forEach(b => b.onclick = () => { $("#chat-input").value = b.dataset.q; sendQuery(b.dataset.q); });
    on("#trace-toggle", () => $("#trace-panel")?.classList.toggle("collapsed"));
    on("#clear-chat", async () => {
      $("#chat-body").innerHTML = "";
      briefingShown = false;
      try { await apiPOST("/api/reset", { session_id: SESSION_ID }); } catch (_) {}
      maybeShowBriefing();
    });
    on("#briefing-refresh", refreshBriefing);

    // Upload wiring
    bind("#up-campaigns", "change", async () => { await showFileMeta("up-campaigns", "up-campaigns-name", "zone-campaigns"); maybeEnableUpload(); });
    bind("#up-adsets",    "change", async () => { await showFileMeta("up-adsets",    "up-adsets-name",    "zone-adsets");    maybeEnableUpload(); });
    setupDragDrop("zone-campaigns", "up-campaigns");
    setupDragDrop("zone-adsets",    "up-adsets");
    on("#btn-upload", doUpload);
    on("#btn-reset-demo", resetData);
  }

  // ------- Public surface -------
  window.GP = { openChat, closeChat, scrollToUpload };

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    applyRoute();
    boot();
  });
})();

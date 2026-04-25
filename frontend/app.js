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
  let briefingShown = false;
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
  function formatAnswer(text) {
    if (!text) return "";
    const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    let html = escape(text);
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Bullet lists
    html = html.replace(/(^|\n)\s*[-•]\s+(.+)/g, "$1<li>$2</li>");
    html = html.replace(/(<li>.*?<\/li>(\n<li>.*?<\/li>)*)/gs, "<ul>$1</ul>");
    html = html.replace(/\n{2,}/g, "</p><p>");
    html = html.replace(/\n/g, "<br/>");
    return "<p>" + html + "</p>";
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
      paintAccountSummary(summary);
      paintFunnel(summary);
      funnelData = { summary, campaigns };
    } catch (e) {
      console.warn("boot failed", e);
    }
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
    const placeholder = appendBot('<span class="typing"><span></span><span></span><span></span></span> Generating Daily Campaign Briefing — Supervisor orchestrating all 4 specialists…');
    try {
      const res = await apiGET("/api/briefing");
      placeholder.remove();
      const specialists = res.specialists_consulted || [];
      const html = specialistsHtml(specialists, "Supervisor") + formatAnswer(res.answer);
      appendBot(html);
      paintTrace({
        route: "DAILY_BRIEFING",
        specialists_consulted: specialists,
        tool_calls: (res.specialist_outputs || []).map(o => ({ specialist: o.agent, tool_calls: o.tool_calls || [] })),
      });
      // Hero card preview (first 3 bullets)
      const lines = (res.answer || "").split(/\n+/).filter(l => /^\s*[-•]/.test(l)).slice(0, 3);
      const ul = $("#hero-briefing-bullets");
      if (lines.length) {
        ul.innerHTML = lines.map(l => `<li>${l.replace(/^\s*[-•]\s*/, "")}</li>`).join("");
      } else {
        ul.innerHTML = `<li>${(res.answer || "").slice(0, 220)}…</li>`;
      }
    } catch (e) {
      placeholder.querySelector(".typing")?.remove();
      placeholder.innerHTML = "Could not load briefing: " + e.message;
    }
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

  function wire() {
    $("#close-chat").onclick = closeChat;
    $("#chat-form").addEventListener("submit", (e) => {
      e.preventDefault();
      sendQuery($("#chat-input").value);
    });
    $$(".quick").forEach(b => b.onclick = () => {
      $("#chat-input").value = b.dataset.q;
      sendQuery(b.dataset.q);
    });
    $("#trace-toggle").onclick = () => $("#trace-panel").classList.toggle("collapsed");
    $("#clear-chat").onclick = async () => {
      $("#chat-body").innerHTML = "";
      briefingShown = false;
      try { await apiPOST("/api/reset", { session_id: SESSION_ID }); } catch (_) {}
      maybeShowBriefing();
    };
  }

  // ------- Public API for inline onclicks -------
  window.GP = { openChat, closeChat };

  document.addEventListener("DOMContentLoaded", () => {
    wire();
    boot();
  });
})();

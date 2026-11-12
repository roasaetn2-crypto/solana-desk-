// Desk AI chat view. Talks to /api/ai/chat, /api/ai/recommend, /api/settings and /api/proposals (PAPER).
// Desk AI only PROPOSES setting changes; a change applies only when the operator taps Confirm.
// Uses esc / headImg / SEAT_NAME from app.js. Nothing here calls a trading, mode, reset or kill endpoint.
(() => {
  const view = document.getElementById("aiView");
  if (!view) return;
  const chat = document.getElementById("aiChat");
  const form = document.getElementById("aiForm");
  const input = document.getElementById("aiInput");
  const send = document.getElementById("aiSend");
  const quick = document.getElementById("aiQuick");
  const pill = document.getElementById("aiPill");
  const pillText = document.getElementById("aiPillText");
  const headSprite = document.getElementById("aiHeadSprite");
  const setRow = view.querySelector(".ai-quick-set");
  const setPanel = document.getElementById("aiSet");
  const setBody = document.getElementById("aiSetBody");
  let types = {};
  const KEY = "deskAiChat.v1";
  let msgs = [];
  let busy = false;
  let openedByPush = false;

  try { msgs = JSON.parse(sessionStorage.getItem(KEY) || "[]").slice(-50); } catch (e) { msgs = []; }
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(msgs.slice(-50))); } catch (e) {} };
  const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

  function chipsHtml(agents) {
    if (!agents || !agents.length) return "";
    return '<div class="ai-chips">' + agents.map((a) =>
      `<span class="ai-chip ${esc(a)}">${headImg(a, 24)}${esc(SEAT_NAME[a] || a)}</span>`).join("") + "</div>";
  }

  function rowHtml(m) {
    if (m.role === "user") {
      return `<div class="ai-row me"><div class="ai-bubble">${esc(m.text)}<div class="ai-meta">${hhmm(m.t)}</div></div></div>`;
    }
    const via = m.provider ? " · " + esc(m.provider) : "";
    const cards = (m.proposals || []).map(propHtml).join("");
    const drop = (m.dropped || []).length ? `<div class="ai-prop-drop">Skipped: ${(m.dropped || []).slice(0, 4)
      .map((d) => esc(d.setting + " (" + d.why + ")")).join("; ")}</div>` : "";
    return `<div class="ai-row bot ${m.err ? "err" : ""}"><div class="ai-mini"><span class="ai-sprite blink"></span></div>
      <div class="ai-bubble">${chipsHtml(m.agents)}${esc(m.text)}${cards}${drop}<div class="ai-meta">${hhmm(m.t)}${via}</div></div></div>`;
  }

  function fmt(v, type, unit) {
    if (type === "bool" || typeof v === "boolean") return v ? "on" : "off";
    if (type === "pct" && typeof v === "number") return +(v * 100).toFixed(2) + "%";
    if (typeof v === "number" && unit === "USD") return "$" + v.toLocaleString();
    if (typeof v === "number") return (Math.abs(v) >= 10000 ? v.toLocaleString() : String(+v.toFixed(4))) + (unit && unit.length <= 3 ? unit : "");
    return String(v);
  }

  function propHtml(p) {
    const rows = (p.changes || []).map((c) => `<div class="ai-prop-row"><b>${esc(c.label || c.setting)}</b>
      <span class="v"><s>${esc(fmt(c.current, c.type, c.unit))}</s> → <em>${esc(fmt(c.proposed, c.type, c.unit))}</em></span>
      ${c.reason || c.note ? `<small>${esc([c.reason, c.note].filter(Boolean).join(" · "))}</small>` : ""}</div>`).join("");
    const st = p.status || "pending";
    const tail = st === "pending"
      ? `<div class="ai-prop-btns"><button class="ok" type="button" data-act="confirm" data-id="${esc(p.id)}">Confirm</button>
         <button class="no" type="button" data-act="reject" data-id="${esc(p.id)}">Reject</button></div>`
      : `<div class="ai-prop-st ${esc(st)}">${st === "confirmed" ? "✓ Applied to paper settings" : st === "rejected" ? "✕ Rejected, nothing changed"
          : st === "expired" ? "Expired, nothing changed" : "Not applied: " + esc(p.error || st)}</div>`;
    return `<div class="ai-prop" data-pid="${esc(p.id)}"><div class="ai-prop-t">⚙ ${esc(p.title || "Setting change")} · PAPER</div>
      ${p.reason ? `<div class="ai-prop-why">${esc(p.reason)}</div>` : ""}${rows}${tail}</div>`;
  }

  function setPropStatus(id, patch) {
    msgs.forEach((m) => (m.proposals || []).forEach((p) => { if (p.id === id) Object.assign(p, patch); }));
    save();
    render();
  }

  async function decide(id, act, btn) {
    view.querySelectorAll(`[data-id="${id}"]`).forEach((b) => (b.disabled = true));
    try {
      const r = await fetch(`/api/proposals/${encodeURIComponent(id)}/${act}`, { method: "POST" });
      if (r.status === 401) { setPropStatus(id, {}); toastSafe("Session expired. Sign in again."); return; }
      const d = await r.json();
      if (d.proposal) setPropStatus(id, { status: d.proposal.status, error: d.proposal.error || d.error });
      else setPropStatus(id, { status: "invalid", error: d.error || "failed" });
      toastSafe(d.ok ? (act === "confirm" ? "Paper settings updated" : "Proposal rejected") : (d.error || "Not applied"));
      if (!setPanel.hidden) loadSettings();
    } catch (e) {
      view.querySelectorAll(`[data-id="${id}"]`).forEach((b) => (b.disabled = false));
      toastSafe("Couldn't reach the desk.");
    }
  }

  function toastSafe(t) { try { toast(t); } catch (e) {} }

  async function syncPending() {
    try {
      const r = await fetch("/api/proposals", { cache: "no-store" });
      if (!r.ok) return;
      const d = await r.json();
      const all = [...(d.pending || []), ...(d.recent || [])];
      const known = new Set();
      msgs.forEach((m) => (m.proposals || []).forEach((p) => {
        known.add(p.id);
        const f = all.find((x) => x.id === p.id);
        if (f) { p.status = f.status; p.error = f.error; }
      }));
      const fresh = (d.pending || []).filter((p) => !known.has(p.id));
      if (fresh.length) msgs.push({ role: "ai", t: Date.now(), agents: [], text: "Waiting for your answer:", proposals: fresh });
      save();
      render();
    } catch (e) {}
  }

  async function post(url, body, label) {
    if (busy) return;
    msgs.push({ role: "user", text: label, t: Date.now() });
    save();
    setBusy(true);
    let msg;
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      if (r.status === 401) msg = { text: "Your session expired. Reopen the desk and sign in again.", err: true };
      else {
        const d = await r.json();
        if (d.proposal) msg = { text: "Here's the change. Nothing moves until you tap Confirm.", proposals: [d.proposal] };
        else if (d.reply) msg = { text: d.reply, agents: d.agents || [], provider: d.provider || null, proposals: d.proposals || [], dropped: d.dropped || [] };
        else msg = { text: d.error || "Couldn't make that proposal.", err: true, dropped: d.dropped || [] };
      }
    } catch (e) {
      msg = { text: "Couldn't reach the desk. Check your connection and try again.", err: true };
    }
    msgs.push({ role: "ai", t: Date.now(), ...msg });
    save();
    setBusy(false);
  }

  async function loadSettings() {
    try {
      const r = await fetch("/api/settings", { cache: "no-store" });
      if (!r.ok) { setBody.innerHTML = '<div class="ai-set-note">Sign in again to see settings.</div>'; return; }
      const d = await r.json();
      let h = '<div class="ai-set-note">Current PAPER values with their hard limits. To change one, ask Desk AI (e.g. "set scalp stop loss to 15%"), tap Recommend, or pick a style below. Every change needs your Confirm.</div>';
      h += '<div class="ai-set-g">TRADING STYLE PRESETS</div><div class="ai-set-presets">' +
        Object.entries(d.presets || {}).map(([k, v]) => `<button type="button" data-preset="${esc(k)}">${esc(v.label)}</button>`).join("") + "</div>";
      Object.entries(d.groups || {}).forEach(([g, list]) => {
        h += `<div class="ai-set-g">${esc(g.toUpperCase())}</div>`;
        list.forEach((x) => {
          types[x.key] = x.type;
          const lim = x.type === "choice" ? (x.choices || []).join(" / ") : x.type === "bool" ? "on / off"
            : `${fmt(x.min, x.type, x.unit)} – ${fmt(x.max, x.type, x.unit)}`;
          h += `<div class="ai-set-r"><div class="l">${esc(x.label)}<small>${esc(x.key)} · ${esc(lim)}</small></div>
            <div class="v">${esc(fmt(x.current, x.type, x.unit))}</div></div>`;
        });
      });
      h += `<div class="ai-set-note">Locked here (never proposable): ${esc((d.excluded || []).join(", "))}.</div>`;
      setBody.innerHTML = h;
    } catch (e) {
      setBody.innerHTML = '<div class="ai-set-note">Couldn\'t reach the desk.</div>';
    }
  }
  function openSettings() { setPanel.hidden = false; loadSettings(); }
  function closeSettings() { setPanel.hidden = true; }
  const recommend = () => { closeSettings(); post("/api/ai/recommend", {}, "✨ Recommend settings"); };
  const preset = (k, label) => { closeSettings(); post("/api/proposals", { preset: k }, label || k); };

  function render() {
    const intro = {
      role: "ai", t: Date.now(), agents: [],
      text: "Hi, I'm Desk AI. Ask me about your paper desk: open positions, why a coin was skipped, today's P&L, last trades, breaker status, or what the desk is doing. I remember this chat after a restart.\nI can also suggest paper setting changes (risk, stop loss, scalper mode…). They show up as cards, and nothing changes until you tap Confirm.",
    };
    chat.innerHTML = '<div class="ai-day">TODAY</div>' + rowHtml(intro) + msgs.map(rowHtml).join("") +
      (busy ? `<div class="ai-row bot ai-typing"><div class="ai-mini"><span class="ai-sprite think"></span></div>
        <div class="ai-bubble"><i></i><i></i><i></i></div><span class="ai-label">Desk AI is thinking…</span></div>` : "");
    chat.scrollTop = chat.scrollHeight;
  }

  function setBusy(b) {
    busy = b;
    send.disabled = b;
    quick.querySelectorAll("button").forEach((x) => (x.disabled = b));
    setRow.querySelectorAll("button").forEach((x) => (x.disabled = b));
    headSprite.className = "ai-sprite " + (b ? "think" : "blink");
    render();
  }

  async function status() {
    try {
      const r = await fetch("/api/ai/status", { cache: "no-store" });
      if (r.status === 401) {
        pill.classList.add("off");
        pillText.textContent = "sign in again · PAPER";
        return;
      }
      const s = await r.json();
      const state = s.state || (s.enabled ? "online" : "no_keys");
      pill.classList.toggle("off", state !== "online");
      if (state === "online") pillText.textContent = "online · PAPER";
      else if (state === "busy") {
        const errs = Object.entries(s.errors || {}).filter(([, v]) => v).map(([k, v]) => k + ": " + v);
        pillText.textContent = "AI busy / limited" + (errs.length ? " (" + errs.join(", ") + ")" : "") + " · PAPER";
      } else pillText.textContent = "AI keys not set · PAPER";
    } catch (e) {
      pill.classList.add("off");
      pillText.textContent = "offline · PAPER";
    }
  }

  async function ask(text) {
    text = String(text || "").trim().slice(0, 500);
    if (!text || busy) return;
    const past = msgs.slice(-8).map((m) => ({ role: m.role === "user" ? "user" : "ai", text: m.text }));
    msgs.push({ role: "user", text, t: Date.now() });
    save();
    setBusy(true);
    let msg;
    try {
      const r = await fetch("/api/ai/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: past }),
      });
      if (r.status === 401) msg = { text: "Your session expired. Reopen the desk and sign in again.", err: true };
      else {
        const d = await r.json();
        if (d && d.reply) msg = { text: d.reply, agents: d.agents || [], provider: d.provider || null,
          proposals: d.proposals || [], dropped: d.dropped || [] };
        else msg = { text: (d && d.error) || "Desk AI could not answer that.", err: true };
      }
    } catch (e) {
      msg = { text: "Couldn't reach the desk. Check your connection and try again.", err: true };
    }
    msgs.push({ role: "ai", t: Date.now(), ...msg });
    save();
    setBusy(false);
  }

  async function loadHistory() {
    try {
      const r = await fetch("/api/ai/history", { cache: "no-store" });
      if (r.status === 401 || !r.ok) return;
      const d = await r.json();
      const server = Array.isArray(d.messages) ? d.messages : [];
      if (server.length) {
        msgs = server.slice(-50);
        save();
        render();
      } else if (msgs.length) {
        await fetch("/api/ai/history", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: msgs }),
        });
      }
    } catch (e) {}
  }

  async function clearChat() {
    if (busy) return;
    if (!window.confirm("Clear Desk AI chat history?")) return;
    try {
      const r = await fetch("/api/ai/chat/clear", { method: "POST" });
      if (r.status === 401) { toastSafe("Session expired. Sign in again."); return; }
      msgs = [];
      save();
      render();
    } catch (e) {
      toastSafe("Couldn't reach the desk.");
    }
  }

  function open(push) {
    if (!view.hidden) return;
    view.hidden = false;
    document.body.classList.add("ai-open");
    if (push) { history.pushState({ ai: 1 }, "", "#ai"); openedByPush = true; }
    render();
    status();
    syncPending();
    loadHistory();
    setTimeout(() => input.focus({ preventScroll: true }), 50);
  }
  function close(fromPop) {
    if (view.hidden) return;
    view.hidden = true;
    document.body.classList.remove("ai-open");
    if (!fromPop) {
      if (openedByPush) history.back();
      else history.replaceState(null, "", location.pathname + location.search);
    }
    openedByPush = false;
  }

  document.getElementById("aiFab").onclick = () => open(true);
  document.getElementById("aiBack").onclick = () => close(false);
  window.addEventListener("popstate", () => { if (location.hash === "#ai") open(false); else close(true); });
  form.addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; ask(t); });
  quick.addEventListener("click", (e) => { const b = e.target.closest("button[data-q]"); if (b) ask(b.dataset.q); });
  chat.addEventListener("click", (e) => { const b = e.target.closest("button[data-act]"); if (b && !b.disabled) decide(b.dataset.id, b.dataset.act, b); });
  setRow.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    if (b.id === "aiRecommend") recommend();
    else if (b.id === "aiSettingsBtn") openSettings();
    else if (b.dataset.preset) preset(b.dataset.preset, b.textContent.trim());
  });
  setBody.addEventListener("click", (e) => { const b = e.target.closest("button[data-preset]"); if (b) preset(b.dataset.preset, b.textContent.trim()); });
  document.getElementById("aiSetBack").onclick = closeSettings;
  document.getElementById("aiSetRec").onclick = recommend;
  const clearBtn = document.getElementById("aiClear");
  if (clearBtn) clearBtn.onclick = clearChat;
  if (location.hash === "#ai") open(false);
})();

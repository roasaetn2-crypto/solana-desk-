// Desk AI chat view. Talks only to POST /api/ai/chat (advisory, read-only, PAPER).
// Uses esc / headImg / SEAT_NAME from app.js. Nothing here calls a trading endpoint.
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
  const KEY = "deskAiChat.v1";
  let msgs = [];
  let busy = false;
  let openedByPush = false;

  try { msgs = JSON.parse(sessionStorage.getItem(KEY) || "[]").slice(-40); } catch (e) { msgs = []; }
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(msgs.slice(-40))); } catch (e) {} };
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
    return `<div class="ai-row bot ${m.err ? "err" : ""}"><div class="ai-mini"><span class="ai-sprite blink"></span></div>
      <div class="ai-bubble">${chipsHtml(m.agents)}${esc(m.text)}<div class="ai-meta">${hhmm(m.t)}${via}</div></div></div>`;
  }

  function render() {
    const intro = {
      role: "ai", t: Date.now(), agents: [],
      text: "Hi, I'm Desk AI. Ask me about your paper desk: open positions, recent decisions and brain verdicts, today's P&L, or the risk stop.\nI only read and advise. I can't trade or change settings.",
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
    headSprite.className = "ai-sprite " + (b ? "think" : "blink");
    render();
  }

  async function status() {
    try {
      const r = await fetch("/api/ai/status");
      const s = await r.json();
      pill.classList.toggle("off", !s.enabled);
      pillText.textContent = s.enabled ? "online · PAPER" : "AI keys not set · PAPER";
    } catch (e) {
      pill.classList.add("off");
      pillText.textContent = "offline · PAPER";
    }
  }

  async function ask(text) {
    text = String(text || "").trim().slice(0, 500);
    if (!text || busy) return;
    const past = msgs.slice(-6).map((m) => ({ role: m.role === "user" ? "user" : "ai", text: m.text }));
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
        if (d && d.reply) msg = { text: d.reply, agents: d.agents || [], provider: d.provider || null };
        else msg = { text: (d && d.error) || "Desk AI could not answer that.", err: true };
      }
    } catch (e) {
      msg = { text: "Couldn't reach the desk. Check your connection and try again.", err: true };
    }
    msgs.push({ role: "ai", t: Date.now(), ...msg });
    save();
    setBusy(false);
  }

  function open(push) {
    if (!view.hidden) return;
    view.hidden = false;
    document.body.classList.add("ai-open");
    if (push) { history.pushState({ ai: 1 }, "", "#ai"); openedByPush = true; }
    render();
    status();
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
  if (location.hash === "#ai") open(false);
})();

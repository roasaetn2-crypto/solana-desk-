const $ = (id) => document.getElementById(id);

const fmtMoney = (n, d = 2) =>
  "$" + Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtNum = (n) => Number(n || 0).toLocaleString("en-US");
const fmtPct = (n) => (Number(n || 0) >= 0 ? "+" : "") + Number(n || 0).toFixed(2) + "%";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function timeOf(iso) {
  if (!iso) return "--:--";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function heldFor(openedEpoch) {
  const s = Math.max(0, Date.now() / 1000 - openedEpoch);
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}
function priceFmt(p) {
  const n = Number(p || 0);
  if (n === 0) return "0";
  if (n < 0.0001) return n.toExponential(2);
  if (n < 1) return n.toFixed(6);
  return n.toFixed(4);
}

const ICONS = { entry: "🟢", exit: "🟠", reject: "⚪", thesis: "🧠", error: "🔴", scan: "🔵", info: "🟣" };

let lastState = null;

const CAT_REACT_MS = 4500;
let catReactUntil = 0;
let catLastKey = "";
let catRevertTimer = 0;

function eventKey(e) {
  if (!e) return "";
  return [e.time, e.kind, e.message].join("|");
}

function exitPnl(e, s) {
  if (!e || e.kind !== "exit") return null;
  if (e.pnl_usd != null && e.pnl_usd !== "") {
    const n = Number(e.pnl_usd);
    return Number.isFinite(n) ? n : null;
  }
  const closed = (s.broker && s.broker.closed) || [];
  const hit = (e.symbol && closed.find((c) => c.symbol === e.symbol)) || closed[0];
  if (hit && hit.pnl_usd != null) {
    const n = Number(hit.pnl_usd);
    return Number.isFinite(n) ? n : null;
  }
  const m = /pnl \$?(-?[\d.]+)/i.exec(e.message || "");
  return m ? Number(m[1]) : null;
}

function shorten(msg, n = 72) {
  const t = String(msg || "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1) + "\u2026" : t;
}

function renderCat(s) {
  const dock = $("catDock");
  if (!dock) return;
  const ev = (s.events && s.events[0]) || null;
  const key = eventKey(ev);
  const now = Date.now();
  if (key && key !== catLastKey) {
    catLastKey = key;
    if (ev && (ev.kind === "entry" || ev.kind === "exit")) {
      catReactUntil = now + CAT_REACT_MS;
    }
  }
  let mood = s.running ? "scan" : "sleep";
  if (now < catReactUntil && ev) {
    if (ev.kind === "entry") mood = "happy";
    else if (ev.kind === "exit") {
      const pnl = exitPnl(ev, s);
      mood = pnl != null && pnl > 0 ? "happy" : "sad";
    }
  }
  dock.dataset.mood = mood;

  const bubble = $("catSpeech");
  const text = $("catSpeechText");
  if (!bubble || !text) return;
  let line = "";
  if (mood === "sleep") line = s.cycle ? "Paused. I'll wait here." : "Zzz\u2026 tap Start to wake me.";
  else if (mood === "happy") line = ev && ev.message ? ev.message : "Nice one!";
  else if (mood === "sad") line = ev && ev.message ? ev.message : "That one stung.";
  else if (ev && ev.message) line = ev.message;
  else line = "Sniffing for coins\u2026";
  text.textContent = shorten(line);
  bubble.hidden = false;

  clearTimeout(catRevertTimer);
  if (now < catReactUntil) {
    catRevertTimer = setTimeout(() => { if (lastState) renderCat(lastState); }, catReactUntil - now + 30);
  }
}


async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  return r.json();
}

function renderMode(s) {
  const ex = s.execution || {};
  const live = ex.mode === "live";
  const mp = $("modePill");
  if (mp) {
    mp.className = "pill " + (live ? "killed" : "");
    $("modeText").textContent = live ? "LIVE MONEY" : "paper";
  }
  const badge = $("modeBadge");
  if (badge) {
    badge.textContent = live ? "LIVE" : "PAPER";
    badge.style.color = live ? "var(--red)" : "var(--green)";
    badge.style.borderColor = live ? "rgba(255,77,109,.45)" : "rgba(46,229,157,.4)";
  }
  const bd = $("brokerDesc");
  if (bd) bd.textContent = live ? "Jupiter swaps · real fills" : "fills · exits · journal";
  const blurb = $("modeBlurb");
  if (blurb) {
    blurb.textContent = live
      ? "LIVE. Approved setups are swapped through Jupiter with your wallet. Caps are enforced in code."
      : "Paper fills only. No key, no signing, no real money can move.";
    blurb.style.color = live ? "var(--red)" : "";
  }
  const caps = ex.caps || {};
  if ($("modeWallet")) $("modeWallet").textContent = ex.wallet ? ex.wallet.slice(0, 4) + "…" + ex.wallet.slice(-4) : "—";
  if ($("modeLivePnl")) $("modeLivePnl").textContent = fmtMoney(ex.live_pnl);
  if ($("capTrade")) $("capTrade").textContent = caps.max_trade != null ? fmtMoney(caps.max_trade, 0) : "—";
  if ($("capNotional")) $("capNotional").textContent = caps.max_notional != null ? fmtMoney(caps.max_notional, 0) : "—";
  if ($("armPhraseHint")) $("armPhraseHint").textContent = ex.confirm_phrase || "—";
  const msg = $("modeMsg");
  if (msg && !msg.dataset.sticky) {
    msg.textContent = live ? "" : (ex.live_ready ? "" : (ex.live_reason || ""));
    msg.style.color = "var(--muted)";
  }
  const eqLabel = $("equityLabel");
  if (eqLabel) eqLabel.textContent = live ? "Live P&L" : "Equity";
  const arm = $("btnArm");
  if (arm) arm.disabled = !ex.live_ready;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 2200);
}

function render(s) {
  lastState = s;
  const b = s.broker || {};
  const st = s.stats || {};
  const cfg = s.config || {};
  renderMode(s);

  // status
  const pill = $("statusPill");
  pill.className = "pill " + (s.running ? "live" : (s.cycle ? "paused" : ""));
  $("statusText").textContent = s.running ? "agent running" : (s.cycle ? "paused" : "idle");
  $("sourceText").textContent = "source: " + (s.data_source || "—");
  const acc = s.access || {};
  const lp = $("lockPill");
  if (lp) {
    const mode = acc.mode || (acc.locked ? "key" : "open");
    lp.classList.toggle("warn", mode === "open");
    if (mode === "device") {
      $("lockText").textContent = "this device \u00b7 " + (acc.device || "paired");
      lp.title = "This browser is paired. It opens without a key. Other devices need the key.";
    } else if (mode === "key") {
      $("lockText").textContent = "access: key";
      lp.title = "Unlocked with the key. This browser is not paired.";
    } else {
      $("lockText").textContent = "access: open";
      lp.title = "No DESK_TOKEN set \u2014 anyone who can reach this port can use the desk.";
    }
  }
  $("cycleBadge").textContent = "cycle " + (s.cycle || 0);

  // top
  $("topEquity").textContent = fmtMoney(b.equity);
  const pnl = b.day_pnl || 0;
  const tp = $("topPnl");
  tp.textContent = fmtMoney(pnl);
  tp.className = "v " + (pnl > 0 ? "pos" : pnl < 0 ? "neg" : "");

  // KPIs
  $("kEquity").textContent = fmtMoney(b.equity);
  const kp = $("kPnl");
  kp.textContent = fmtMoney(pnl);
  kp.className = "v " + (pnl > 0 ? "pos" : pnl < 0 ? "neg" : "");
  const startEq = 1000;
  $("kPnlPct").textContent = fmtPct((pnl / startEq) * 100);
  $("kOpen").textContent = b.open_count || 0;
  $("kOpenMax").textContent = "max " + (cfg.max_positions || 3);
  const closed = b.closed || [];
  const wins = closed.filter((c) => (c.pnl_usd || 0) > 0).length;
  $("kWin").textContent = closed.length ? Math.round((wins / closed.length) * 100) + "%" : "—";
  $("kClosed").textContent = closed.length + " closed";

  // pipeline
  $("cSeen").textContent = fmtNum(st.seen);
  $("cChain").textContent = fmtNum(st.chain_rejects);
  $("cDev").textContent = fmtNum(st.dev_rejects);
  $("cMeme").textContent = fmtNum(st.meme_rejects);
  $("cAnalyst").textContent = fmtNum(st.analyst_calls);
  $("cRisk").textContent = fmtNum(st.risk_rejects);
  $("cEntries").textContent = fmtNum(st.entries);
  $("sCycles").textContent = fmtNum(st.cycles);
  $("sSeen").textContent = fmtNum(st.seen);
  $("sFree").textContent = fmtNum(st.free_rejects);
  $("sChain").textContent = fmtNum(st.chain_rejects);
  $("sDev").textContent = fmtNum(st.dev_rejects);
  $("sMeme").textContent = fmtNum(st.meme_rejects);
  $("sTheses").textContent = fmtNum(st.analyst_calls);
  $("sDialogues").textContent = fmtNum(st.dialogues);
  $("sEntries").textContent = fmtNum(st.entries);
  $("sExits").textContent = fmtNum(st.exits);
  $("sErrors").textContent = fmtNum(st.errors);
  const br = s.brain || {};
  const brainLabel = br.label
    ? br.label + (br.model ? " · " + br.model : "") + (br.free ? " · free" : "")
    : (s.analyst || "—");
  $("analystLine").textContent = "Brain: " + brainLabel;
  $("analystLine").classList.toggle("warn", !!(br.needs_key && !br.key_present));
  $("analystDesc").textContent = br.needs_key && !br.key_present
    ? "no key for " + br.label + " — using rules"
    : (s.analyst || "typed thesis 0–10");
  const rpc = s.rpc || {};
  const rpcEl = $("rpcLine");
  if (rpcEl) {
    rpcEl.textContent = "RPC: " + (rpc.endpoint || "public") +
      (rpc.configured ? " · holder counts on" : " · free public node");
    rpcEl.classList.toggle("warn", !rpc.configured);
  }

  // active seat from most recent event
  const ev0 = (s.events && s.events[0]) || null;
  const latest = ev0 ? ev0.kind : null;
  const stage = ev0 ? ev0.stage : null;
  const map = { scan: "scanner", thesis: "analyst", entry: "broker", exit: "broker" };
  let active = map[latest] || null;
  if (latest === "reject") active = stage === "dev" ? "dev" : stage === "meme" ? "meme" : "chain";
  document.querySelectorAll(".seat").forEach((el) => el.classList.remove("active"));
  if (active) {
    const el = document.querySelector('.seat[data-seat="' + active + '"]');
    if (el) el.classList.add("active");
  }

  // config
  $("cfgScore").textContent = cfg.min_score ?? 7;
  $("cfgMax").textContent = cfg.max_positions ?? 3;
  $("cfgStop").textContent = "-" + Math.round((cfg.stop_pct ?? 0.35) * 100) + "%";
  $("cfgTake").textContent = (cfg.take_half_at ?? 2).toFixed(1) + "x";

  // positions
  const pos = b.positions || [];
  $("posBadge").textContent = pos.length;
  const pb = $("posBody");
  if (!pos.length) {
    pb.innerHTML = '<tr><td colspan="6" class="empty">No open positions. Start the agent.</td></tr>';
  } else {
    pb.innerHTML = pos.map((p) => {
      const cls = p.pnl_pct > 0 ? "pos" : p.pnl_pct < 0 ? "neg" : "";
      return `<tr>
        <td><span class="sym">${esc(p.symbol)}</span></td>
        <td>${priceFmt(p.entry)}</td>
        <td>${priceFmt(p.last)}</td>
        <td class="${cls}">${fmtPct(p.pnl_pct)}</td>
        <td>${fmtMoney(p.cost)}</td>
        <td>${heldFor(p.opened)}</td>
      </tr>`;
    }).join("");
  }

  // decisions
  const dec = s.decisions || [];
  $("decBadge").textContent = dec.length;
  const db = $("decBody");
  if (!dec.length) {
    db.innerHTML = '<tr><td colspan="7" class="empty">No decisions yet.</td></tr>';
  } else {
    db.innerHTML = dec.slice(0, 18).map((d) => {
      const v = d.thesis ? d.thesis.verdict : "—";
      const tag = d.approved ? "buy" : (v === "watch" ? "watch" : "skip");
      const label = d.approved ? "buy" : v;
      const meme = d.scorecard && typeof d.scorecard.total === "number" ? d.scorecard.total : null;
      const dev = d.dev && typeof d.dev.dev_score === "number" ? d.dev.dev_score : null;
      const memeCell = meme === null ? "—"
        : `<span class="num ${meme >= 70 ? "good" : meme >= 50 ? "warn" : "bad"}">${meme}</span>`;
      const devCell = dev === null ? "—"
        : `<span class="num ${dev >= 70 ? "good" : dev >= 45 ? "warn" : "bad"}">${dev}</span>`;
      return `<tr>
        <td><span class="sym">${esc(d.symbol)}</span></td>
        <td><span class="tag ${tag}">${esc(label)}</span></td>
        <td>${d.thesis ? d.thesis.score : "—"}</td>
        <td>${memeCell}</td>
        <td>${devCell}</td>
        <td>${d.approved ? fmtMoney(d.ticket_usd) : "—"}</td>
        <td class="muted" style="font-family:var(--sans);font-size:11.5px">${esc(d.reason || "")} <span class="tag ${d.source === "demo" ? "demo" : "live"}">${esc(d.source || "—")}</span></td>
      </tr>`;
    }).join("");
  }

  // log
  const ev = s.events || [];
  const log = $("log");
  if (!ev.length) {
    log.innerHTML = '<div class="empty">Waiting for the agent…</div>';
  } else {
    log.innerHTML = ev.map((e) => `<div class="log-row k-${esc(e.kind)}">
      <span class="t">${timeOf(e.time)}</span>
      <span class="i">${ICONS[e.kind] || "•"}</span>
      <span class="m">${esc(e.message)}</span>
    </div>`).join("");
  }

  // theses
  const theses = dec.filter((d) => d.thesis).slice(0, 12);
  const tl = $("theses");
  if (!theses.length) {
    tl.innerHTML = '<div class="empty">No theses yet.</div>';
  } else {
    tl.innerHTML = theses.map((d) => {
      const t = d.thesis;
      const sc = t.score >= 7 ? "hi" : t.score >= 5 ? "mid" : "lo";
      const flags = (t.red_flags || []).slice(0, 3).map((f) => `<div class="flag red">${esc(f)}</div>`).join("")
        + (t.green_flags || []).slice(0, 2).map((f) => `<div class="flag green">${esc(f)}</div>`).join("");
      const sizes = d.sizes || {};
      const maxSize = Math.max(sizes.by_risk || 0, sizes.by_size || 0, sizes.by_pool || 0, 1);
      const bar = (lab, val) => {
        const w = Math.min(100, (val / maxSize) * 100);
        const win = Math.abs(val - (sizes.ticket || 0)) < 0.01 ? "win" : "";
        return `<div class="size-row"><span class="lab">${lab}</span><span class="bar"><i class="${win}" style="width:${w}%"></i></span><span class="amt">${fmtMoney(val)}</span></div>`;
      };
      return `<div class="thesis">
        <div class="top">
          <span class="sym">${esc(d.symbol)}</span>
          <span class="tag ${d.approved ? "buy" : "skip"}">${esc(d.approved ? "approved" : t.verdict)}</span>
          <span class="score ${sc}">${t.score}/10</span>
        </div>
        <div class="flags">${flags || '<div class="flag">no flags</div>'}</div>
        ${d.approved ? `<div style="margin-top:9px">${bar("risk", sizes.by_risk)}${bar("size", sizes.by_size)}${bar("pool", sizes.by_pool)}</div>` : ""}
        <div class="foot">shape: ${esc(t.shape)} · src: ${esc(d.source || "—")} · ${timeOf(d.time)}</div>
      </div>`;
    }).join("");
  }

  // ---------- meme scorecard ----------
  const latestDec = dec[0] || null;
  const sc = latestDec && latestDec.scorecard ? latestDec.scorecard : null;
  const memeTotal = $("memeTotal");
  const memeVerdict = $("memeVerdict");
  if (sc) {
    memeTotal.textContent = sc.total;
    const cls = sc.total >= 70 ? "good" : sc.total >= 50 ? "mid" : "bad";
    memeVerdict.textContent = (sc.verdict || "").replace(/_/g, " ");
    memeVerdict.className = cls;
    $("memeBadge").textContent = latestDec.symbol;
    const colors = { community: "var(--green)", dev_legit: "var(--purple)", narrative: "var(--blue)",
                     distribution: "var(--amber)", utility: "var(--cyan, #22d3ee)" };
    $("memeBars").innerHTML = Object.entries(sc.parts || {}).map(([k, v]) => `
      <div class="bar-row">
        <span class="lab">${esc(k.replace("_", " "))}</span>
        <span class="track"><i style="width:${v}%;background:${colors[k] || "var(--blue)"}"></i></span>
        <span class="num">${v}</span>
      </div>`).join("");
    const n = sc.narrative || {};
    $("memeNarrative").textContent = "Narrative: " + (n.primary || "\u2014") +
      (n.tags && n.tags.length > 1 ? " (" + n.tags.join(", ") + ")" : "") +
      "  \u00b7  " + (sc.verdict || "");
    // horizon
    const h = sc.horizon || {};
    $("horizonBadge").textContent = h.label || "\u2014";
    $("hHold").textContent = h.hold || "\u2014";
    $("hChart").textContent = h.chart || "\u2014";
    $("hRule").textContent = h.rule || "\u2014";
  } else {
    memeTotal.textContent = "\u2014";
    memeVerdict.textContent = "no coin yet";
    memeVerdict.className = "";
    $("memeBadge").textContent = "\u2014";
    $("memeBars").innerHTML = '<div class="empty">Run a cycle to score a coin.</div>';
    $("memeNarrative").textContent = "Narrative: \u2014";
  }

  // ---------- dev investigation ----------
  const dev = latestDec && latestDec.dev ? latestDec.dev : null;
  if (dev) {
    $("devBadge").textContent = latestDec.symbol;
    $("devScore").textContent = dev.dev_score != null ? dev.dev_score : "\u2014";
    $("devHold").textContent = dev.creator_hold_pct != null ? (dev.creator_hold_pct * 100).toFixed(1) + "%" : "\u2014";
    $("devRugs").textContent = dev.creator_prev_rugs != null ? dev.creator_prev_rugs : "n/a";
    $("devSnipers").textContent = dev.sniper_pct != null ? (dev.sniper_pct * 100).toFixed(1) + "%" : "n/a";
    const reasons = (dev.reasons || []).slice(0, 7);
    const bad = new Set(["dev holds", "creator has", "sniper", "mint authority still", "freeze authority still"]);
    $("devReasons").innerHTML = reasons.length
      ? reasons.map((r) => {
          const isBad = [...bad].some((b) => r.toLowerCase().startsWith(b));
          return `<li class="${isBad ? "bad" : "good"}">${esc(r)}</li>`;
        }).join("")
      : '<li class="empty">No findings.</li>';
  } else {
    $("devBadge").textContent = "\u2014";
    ["devScore", "devHold", "devRugs", "devSnipers"].forEach((id) => ($(id).textContent = "\u2014"));
    $("devReasons").innerHTML = '<li class="empty">No investigation yet.</li>';
  }

  // ---------- seat dialogue ----------
  const tr = (s.transcripts && s.transcripts[0]) || null;
  if (tr && tr.messages && tr.messages.length) {
    $("dlgBadge").textContent = tr.backend + " \u00b7 " + tr.symbol;
    $("dialogue").innerHTML = tr.messages.map((m) => `
      <div class="msg ${esc(m.stance || "info")}">
        <div class="head"><b>${esc(m.seat || m.from)}</b><span class="arrow">\u2192</span><b>${esc(m.to)}</b></div>
        <div class="body">${esc(m.text)}</div>
      </div>`).join("");
  } else {
    $("dlgBadge").textContent = "template";
    $("dialogue").innerHTML = '<div class="empty">The seats have not spoken yet.</div>';
  }

  renderCat(s);
}

async function refresh() {
  try {
    const s = await api("/api/state");
    render(s);
  } catch (e) {
    console.error(e);
  }
}

async function act(path, method, msg) {
  try {
    const r = await api(path, method);
    if (msg) toast(msg);
    await refresh();
    return r;
  } catch (e) {
    toast("Action failed");
  }
}

$("btnStart").onclick = () => act("/api/agent/start", "POST", "Agent started");
$("btnPause").onclick = () => act("/api/agent/stop", "POST", "Agent paused");
$("btnStep").onclick = async () => {
  toast("Running one cycle…");
  await act("/api/agent/step?demo=1", "POST", "Cycle complete");
};
$("btnReset").onclick = () => {
  const m = (lastState && lastState.execution && lastState.execution.mode) || "paper";
  if (confirm("Reset the " + m + " book, positions and bench?")) act("/api/agent/reset", "POST", "Desk reset");
};
$("btnKill").onclick = () => {
  if (confirm("Trip the kill switch? This pauses the agent and closes paper positions on its next check.")) {
    act("/api/kill", "POST", "Kill switch tripped");
  }
};

// ---------- execution mode ----------
function modeMsg(text, color) {
  const el = $("modeMsg");
  if (!el) return;
  el.textContent = text;
  el.style.color = color || "var(--muted)";
  el.dataset.sticky = "1";
}
$("btnPaper").onclick = async () => {
  const r = await api("/api/mode", "POST", { mode: "paper" });
  if (r.ok) { toast("Back to paper"); modeMsg("", ""); }
  else modeMsg(r.error || "could not switch", "var(--red)");
  await refresh();
};
$("btnArm").onclick = () => {
  $("armBox").style.display = "block";
  $("armPhrase").value = "";
  $("armPhrase").focus();
};
$("btnArmCancel").onclick = () => { $("armBox").style.display = "none"; };
$("btnArmGo").onclick = async () => {
  const phrase = $("armPhrase").value;
  const r = await api("/api/mode", "POST", { mode: "live", confirm: phrase });
  if (r.ok) {
    $("armBox").style.display = "none";
    toast("LIVE ARMED");
    modeMsg("Live armed. Wallet " + (r.wallet || "") + ". Caps enforced.", "var(--red)");
  } else {
    modeMsg(r.error || "not armed", "var(--red)");
  }
  await refresh();
};

refresh();
setInterval(refresh, 2000);

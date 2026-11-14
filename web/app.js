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


function formatAge(seconds) {
  if (seconds == null || !isFinite(Number(seconds))) return "—";
  let s = Math.floor(Number(seconds));
  if (s < 0) s = 0;
  if (s < 60) return s + "s";
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return minutes + "m";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rem = minutes % 60;
    return rem ? hours + "h " + rem + "m" : hours + "h";
  }
  return Math.floor(hours / 24) + "d";
}
function shortCa(mint) {
  if (!mint) return "";
  if (mint.length <= 8) return mint;
  return mint.slice(0, 4) + "\u2026" + mint.slice(-4);
}
// A small "x0.5" chip when the Groq sizer shrank this ticket. Reads the value
// the agent already stored on the decision (sizes.guard.groq), so it is the
// exact multiplier that sized the fill -- not a re-derivation.
function groqChip(d) {
  const g = d && d.sizes && d.sizes.guard && d.sizes.guard.groq;
  if (!g) return "";
  const mult = Number(g.mult);
  if (!(mult < 1)) return "";
  const why = (g.reasons && g.reasons.length) ? g.reasons.join("; ")
    : (g.source === "groq" ? "no objection recorded" : String(g.source || ""));
  const conf = g.confidence != null ? " \u00b7 conf " + Math.round(g.confidence) : "";
  return `<span class="tag watch" title="${esc(why + conf)}">groq x${mult.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}</span> `;
}

function flagsHtml(row) {
  const flags = (row && Array.isArray(row.flags)) ? row.flags : [];
  const cl = row && row.checklist;
  let out = "";
  if (cl && cl.total) {
    out += `<span class="flag ${cl.ok ? "info" : "block"}" title="pre-trade checklist">\u2713 ${cl.passed}/${cl.total}</span>`;
  }
  const seen = new Set();
  for (const f of flags) {
    if (!f || seen.has(f.t)) continue;
    seen.add(f.t);
    const sev = ["block", "halve", "info"].includes(f.sev) ? f.sev : "info";
    out += `<span class="flag ${sev}" title="${esc((f.src || "") + " \u00b7 " + sev)}">${esc(f.label || f.t)}</span>`;
  }
  return out ? `<div class="flags">${out}</div>` : "";
}
function tokenMetaHtml(row) {
  if (!row || !row.mint) return "";
  const created = row.created_at != null && row.created_at !== "" ? Number(row.created_at) : NaN;
  const age = Number.isFinite(created) ? formatAge(Date.now() / 1000 - created) : "—";
  const ca = shortCa(row.mint);
  const createdAttr = Number.isFinite(created) ? String(created) : "";
  return `<div class="token-meta">
    <span>Age <b class="age" data-created-at="${esc(createdAttr)}">${esc(age)}</b></span>
    <a class="ca" href="https://solscan.io/token/${encodeURIComponent(row.mint)}" target="_blank" rel="noopener noreferrer">${esc(ca)}</a>
    <button type="button" class="copy-ca" data-ca="${esc(row.mint)}" aria-label="Copy contract address">Copy</button>
  </div>${flagsHtml(row)}`;
}
function tickAges() {
  document.querySelectorAll(".age[data-created-at]").forEach((el) => {
    const c = Number(el.dataset.createdAt);
    if (!c) return;
    el.textContent = formatAge(Date.now() / 1000 - c);
  });
}
function bindCaCopy(root) {
  (root || document).querySelectorAll(".copy-ca").forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const ca = btn.dataset.ca;
      if (!ca) return;
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(ca);
        }
        toast("copied");
      } catch (_) {
        toast("copied");
      }
    });
  });
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

function fomoBtn(url, label) {
  if (!url) return "";
  return `<a class="btn-fomo" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label || "Buy on FOMO"}</a>`;
}

const chartCache = new Map(); // mint|tf -> payload
const chartTf = {};            // mint -> selected tf

function tabsHtml(mint, tabs, current) {
  const list = tabs && tabs.length ? tabs : ["15s", "30s"];
  const on = current || list[0];
  return list.map((t) =>
    `<button type="button" data-mint="${esc(mint)}" data-tf="${esc(t)}" ${t === on ? 'aria-selected="true" class="on"' : ""}>${esc(t)}</button>`
  ).join("");
}

function drawCandles(canvas, data) {
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0c0a12";
  ctx.fillRect(0, 0, w, h);
  const candles = (data && data.candles) || [];
  if (!candles.length) {
    ctx.fillStyle = "#7a6e86";
    ctx.font = "16px sans-serif";
    ctx.fillText("Waiting for candles\u2026", 16, h / 2);
    return;
  }
  const padL = 8, padR = 8, padT = 10, volH = Math.floor(h * 0.22), gap = 6;
  const plotH = h - padT - volH - gap - 8;
  const plotW = w - padL - padR;
  let lo = Math.min(...candles.map((c) => c.l));
  let hi = Math.max(...candles.map((c) => c.h));
  const entry = data.entry != null ? Number(data.entry) : null;
  if (entry && Number.isFinite(entry)) { lo = Math.min(lo, entry); hi = Math.max(hi, entry); }
  if (hi === lo) { hi = lo * 1.01 || 1; }
  const maxV = Math.max(...candles.map((c) => c.v || 0), 1e-9);
  const slot = plotW / candles.length;
  const bodyW = Math.max(2, slot * 0.62);
  const y = (px) => padT + (1 - (px - lo) / (hi - lo)) * plotH;
  // entry line
  if (entry && Number.isFinite(entry)) {
    const ey = y(entry);
    ctx.strokeStyle = "rgba(244,240,230,.55)";
    ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.moveTo(padL, ey); ctx.lineTo(w - padR, ey); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(244,240,230,.8)";
    ctx.font = "11px sans-serif";
    ctx.fillText("entry", w - padR - 36, Math.max(12, ey - 4));
  }
  candles.forEach((c, i) => {
    const up = c.c >= c.o;
    const color = up ? "#00d35a" : "#ff453a";
    const x = padL + i * slot + slot / 2;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, y(c.h));
    ctx.lineTo(x, y(c.l));
    ctx.stroke();
    const top = y(Math.max(c.o, c.c));
    const bot = y(Math.min(c.o, c.c));
    const bh = Math.max(1, bot - top);
    ctx.fillStyle = color;
    ctx.fillRect(x - bodyW / 2, top, bodyW, bh);
    const vh = ((c.v || 0) / maxV) * (volH - 2);
    ctx.globalAlpha = 0.45;
    ctx.fillRect(x - bodyW / 2, h - 6 - vh, bodyW, vh);
    ctx.globalAlpha = 1;
  });
}

async function loadChart(mint, tf, canvas, tabsEl) {
  if (!mint || !canvas) return;
  const want = tf || chartTf[mint] || "";
  const key = mint + "|" + want;
  let data = chartCache.get(key);
  const stale = !data || (Date.now() - (data._at || 0) > 25000);
  if (stale) {
    try {
      const q = "/api/chart?mint=" + encodeURIComponent(mint) + (want ? "&tf=" + encodeURIComponent(want) : "");
      const res = await api(q);
      res._at = Date.now();
      chartCache.set(mint + "|" + (res.tf || want), res);
      data = res;
    } catch (e) {
      data = data || { candles: [], tf: want, tabs: [] };
    }
  }
  if (data && data.tf) chartTf[mint] = data.tf;
  if (tabsEl) tabsEl.innerHTML = tabsHtml(mint, data.tabs, data.tf);
  drawCandles(canvas, data);
}

function bindChartTabs(root) {
  if (!root) return;
  root.querySelectorAll(".tf-tabs button").forEach((btn) => {
    btn.onclick = () => {
      const mint = btn.dataset.mint;
      const tf = btn.dataset.tf;
      chartTf[mint] = tf;
      chartCache.delete(mint + "|" + tf); // refresh this tab
      const card = btn.closest(".pos-card, .buy-banner, .ticket-chart") || root;
      const canvas = card.querySelector("canvas.candle-chart");
      loadChart(mint, tf, canvas, btn.parentElement);
    };
  });
}

function catBaseMood(s) {
  if (!s.running) return "sleep";
  const open = (s.broker && (s.broker.open_count || ((s.broker.positions || []).length))) || 0;
  return open > 0 ? "trader" : "scan";
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
  let mood = catBaseMood(s);
  if (now < catReactUntil && ev) {
    if (ev.kind === "entry") mood = "win";
    else if (ev.kind === "exit") {
      const pnl = exitPnl(ev, s);
      mood = pnl != null && pnl > 0 ? "win" : "loss";
    }
  }
  const moodChanged = dock.dataset.mood !== mood;
  if (moodChanged) dock.dataset.mood = mood;
  if (mood === "win" || mood === "loss") {
    const fresh = key && dock._reactKey !== key;
    if (moodChanged || fresh) {
      dock._reactKey = key;
      const sprite = dock.querySelector(".mood-" + mood);
      if (sprite) {
        sprite.style.animation = "none";
        void sprite.offsetWidth;
        sprite.style.animation = "";
        const onEnd = () => {
          sprite.removeEventListener("animationend", onEnd);
          catReactUntil = 0;
          if (lastState) renderCat(lastState);
        };
        sprite.addEventListener("animationend", onEnd);
      }
    }
  }

  const bubble = $("catSpeech");
  const text = $("catSpeechText");
  if (!bubble || !text) return;
  let line = "";
  if (mood === "idle") line = "Tap Start when you're ready.";
  else if (mood === "sleep") line = s.cycle ? "Paused. I'll wait here." : "Tap Start when you're ready.";
  else if (mood === "win") line = ev && ev.message ? ev.message : "Nice one!";
  else if (mood === "loss") line = ev && ev.message ? ev.message : "That one stung.";
  else if (mood === "trader") line = ev && ev.message ? ev.message : "Watching the book.";
  else if (ev && ev.message) line = ev.message;
  else line = "Sniffing for coins\u2026";
  text.textContent = shorten(line);
  bubble.hidden = false;

  clearTimeout(catRevertTimer);
  if (now < catReactUntil) {
    catRevertTimer = setTimeout(() => { if (lastState) renderCat(lastState); }, catReactUntil - now + 30);
  }
}


function renderRisk(s) {
  const rk = s.risk || {};
  const banner = $("riskBanner");
  const btn = $("btnContinue");
  const txt = $("riskText");
  if (!banner || !btn || !txt) return;
  const paper = (rk.mode || (s.execution || {}).mode) === "paper";
  const pct = Math.round((rk.daily_loss_limit_pct || 0.03) * 100);
  const usdSet = Number(rk.daily_loss_limit_usd_setting || 0);
  const usd = Number(rk.daily_loss_limit_usd || 0);
  const limitLabel = usdSet > 0 ? "$" + usd.toFixed(2) : pct + "%";
  if (paper && rk.override_today) {
    banner.hidden = false;
    banner.classList.add("overridden");
    txt.textContent = rk.auto_continue
      ? "Daily stop is auto-continued (" + limitLabel + ") \u2014 entries keep going"
      : "Daily stop overridden for today \u2014 resets at day roll";
    btn.hidden = true;
  } else if (paper && rk.daily_loss_halted) {
    banner.hidden = false;
    banner.classList.remove("overridden");
    txt.textContent = "Daily loss stop hit (" + limitLabel + ", day P&L $" +
      Number(rk.day_pnl || 0).toFixed(2) + "). New paper entries paused for today.";
    btn.hidden = !rk.can_continue;
  } else if (paper && rk.over_risk_budget) {
    // The whole-book risk budget is already spent. Without this the desk just
    // refused every entry with a reason buried in the decision card, which
    // looked like a silent failure. Say it on the dashboard instead.
    banner.hidden = false;
    banner.classList.remove("overridden");
    const over = Number(rk.open_risk_usd || 0) - Number(rk.risk_budget_usd || 0);
    txt.textContent = "Open risk $" + Number(rk.open_risk_usd || 0).toFixed(2) +
      " is over the $" + Number(rk.risk_budget_usd || 0).toFixed(2) +
      " risk budget (over by $" + over.toFixed(2) +
      "). New entries wait until positions close or stops tighten.";
    btn.hidden = true;
  } else {
    banner.hidden = true;
    btn.hidden = true;
  }
  // Daily-stop summary line. Kept inline: the old renderDailyStop() helper was
  // removed with the self-tune cleanup but this call stayed behind, throwing a
  // ReferenceError that aborted render() before the agent cards could paint.
  const dsNow = $("dailyStopNow");
  if (dsNow) dsNow.textContent = limitLabel + (rk.hit ? " \u2014 hit" : "");
}

function initDailyStop() {
  const usd = $("dsUsd");
  const auto = $("dsAuto");
  const save = $("dsSave");
  const note = $("dsNote");
  if (!usd || !auto || !save) return;
  const touch = () => { dsTouched = true; };
  usd.addEventListener("input", touch);
  auto.addEventListener("change", touch);
  save.onclick = async () => {
    const values = {
      "risk.max_daily_loss_usd": Number(usd.value || 0),
      "risk.auto_continue_after_daily_stop": !!auto.checked
    };
    const r = await api("/api/proposals", "POST", { values: values, title: "Daily loss stop" });
    if (!r || !r.ok) {
      if (note) note.textContent = (r && (r.error || r.detail)) || "Could not propose that change";
      toast((r && (r.error || r.detail)) || "Could not propose that change");
      return;
    }
    const p = r.proposal || {};
    const changes = (p.changes || []).map((c) => c.label + ": " + c.current + " \u2192 " + c.proposed);
    if (note) note.textContent = "Proposed \u2014 confirm below to apply.";
    toast("Proposed. Confirm it to apply.");
    if (changes.length && confirm("Apply these changes?\n\n" + changes.join("\n"))) {
      const c = await api("/api/proposals/" + encodeURIComponent(p.id) + "/confirm", "POST", null);
      if (c && c.ok) {
        dsTouched = false;
        if (note) note.textContent = "Applied.";
        toast("Daily stop updated");
        refresh && refresh();
      } else {
        toast((c && (c.error || c.detail)) || "Confirm refused");
      }
    }
  };
}

async function api(path, method = "GET", body = null) {
  // cache: "no-store" matters on a phone: Chrome will otherwise serve a GET
  // from the disk cache when the network is flaky, and the page silently
  // freezes on an old snapshot while the desk keeps trading.
  const opts = { method, headers: {}, cache: "no-store" };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
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


let equityTf = "LIVE";
const EQUITY_WINDOWS = { LIVE: 15 * 60, "1H": 3600, "4H": 4 * 3600, "1D": 86400, "1W": 7 * 86400, ALL: null };

function timeManila(isoOrUnix) {
  if (isoOrUnix == null || isoOrUnix === "") return "—";
  const d = typeof isoOrUnix === "number" ? new Date(isoOrUnix * 1000) : new Date(isoOrUnix);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-US", { timeZone: "Asia/Manila", hour: "numeric", minute: "2-digit", hour12: true });
}

function curveWindow(points, marks, tf, now) {
  const pts = points || [];
  const mks = marks || [];
  const span = EQUITY_WINDOWS[tf];
  if (span == null || !pts.length) return { pts, mks };
  const cut = now - span;
  const wPts = pts.filter((p) => p.t >= cut);
  const wMks = mks.filter((m) => m.t >= cut);
  if (!wPts.length) return { pts, mks };
  return { pts: wPts, mks: wMks };
}

function drawEquity(s) {
  const canvas = $("equityChart");
  const labels = $("equityLabels");
  if (!canvas) return;
  const now = Date.now() / 1000;
  const { pts, mks } = curveWindow(s.equity_curve || [], s.curve_marks || [], equityTf, now);
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 360;
  const h = canvas.clientHeight || 168;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (labels) labels.innerHTML = "";
  if (!pts.length) return;

  const padL = 6, padR = 6, padT = 18, padB = 8;
  const ts = pts.map((p) => p.t);
  const vs = pts.map((p) => p.equity);
  let t0 = Math.min.apply(null, ts), t1 = Math.max.apply(null, ts);
  if (t1 <= t0) t1 = t0 + 1;
  let v0 = Math.min.apply(null, vs), v1 = Math.max.apply(null, vs);
  const vspan = (v1 - v0) || 1;
  v0 -= vspan * 0.15;
  v1 += vspan * 0.22;
  const xOf = (t) => padL + ((t - t0) / (t1 - t0)) * (w - padL - padR);
  const yOf = (v) => padT + (1 - (v - v0) / (v1 - v0)) * (h - padT - padB);

  const firstX = xOf(pts[0].t);
  const lastY = yOf(pts[pts.length - 1].equity);
  const endX = w - padR;

  ctx.beginPath();
  pts.forEach((p, i) => {
    const px = xOf(p.t), py = yOf(p.equity);
    if (i === 0) ctx.moveTo(px, py);
    else {
      ctx.lineTo(px, yOf(pts[i - 1].equity));
      ctx.lineTo(px, py);
    }
  });
  ctx.lineTo(endX, lastY);
  ctx.strokeStyle = "#00d35a";
  ctx.lineWidth = 2;
  ctx.lineJoin = "miter";
  ctx.stroke();
  ctx.lineTo(endX, h - padB);
  ctx.lineTo(firstX, h - padB);
  ctx.closePath();
  ctx.fillStyle = "rgba(0, 211, 90, 0.22)";
  ctx.fill();

  function equityAt(t) {
    let eq = pts[0].equity;
    for (let i = 0; i < pts.length; i++) {
      if (pts[i].t <= t) eq = pts[i].equity;
    }
    return eq;
  }

  mks.forEach((m) => {
    const mx = xOf(m.t), my = yOf(equityAt(m.t));
    ctx.beginPath();
    ctx.arc(mx, my, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = m.kind === "buy" ? "#00d35a" : "#ff453a";
    ctx.fill();
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = "#000";
    ctx.stroke();
  });

  if (labels) {
    const recent = mks.slice(-2);
    labels.innerHTML = recent.map((m) => {
      const mx = xOf(m.t), my = yOf(equityAt(m.t));
      let text = "";
      if (m.kind === "buy") text = "$" + (m.symbol || "") + " bought " + fmtMoney(m.usd || 0, 0);
      else {
        const pnl = m.pnl_usd;
        const sold = pnl == null ? "" : ((pnl >= 0 ? "+" : "") + fmtMoney(pnl));
        text = "$" + (m.symbol || "") + " sold " + sold;
      }
      return `<div class="equity-float" style="left:${mx}px;top:${my}px">${esc(text)}</div>`;
    }).join("");
  }
}

function renderHero(s) {
  const b = s.broker || {};
  const start = Number(s.starting_equity != null ? s.starting_equity : 20);
  const eq = b.equity != null ? Number(b.equity) : start;
  if ($("heroEquity")) $("heroEquity").textContent = fmtMoney(eq);
  const pnl = Number(b.day_pnl || 0);
  // Day P&L % is measured against the equity when the day rolled, not the
  // original bankroll. Using starting_equity overstated/understated the daily
  // move once the book had grown or shrunk.
  const dayStart = Number(b.day_start != null ? b.day_start : start);
  const pct = dayStart ? (pnl / dayStart) * 100 : 0;
  const cls = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "";
  const usd = $("heroPnlUsd");
  if (usd) { usd.textContent = (pnl > 0 ? "+" : "") + fmtMoney(pnl); usd.className = "pnl-usd " + cls; }
  const pc = $("heroPnlPct");
  if (pc) { pc.textContent = fmtPct(pct); pc.className = "pnl-pct " + cls; }
  const chip = $("heroMult");
  if (chip) {
    const mult = start ? eq / start : 1;
    chip.hidden = false;
    chip.textContent = mult.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") + "x";
    if (Math.abs(mult - 1) < 0.0005) chip.textContent = "1.00x";
    chip.className = "chip " + (mult >= 1 ? "pos" : "neg");
  }
  const rc = $("heroRealized");
  if (rc) {
    const real = s.latest_realized;
    if (real && real.pnl_usd != null) {
      const n = Number(real.pnl_usd);
      rc.hidden = false;
      rc.textContent = (n >= 0 ? "+" : "") + fmtMoney(n);
      rc.className = "chip realized " + (n >= 0 ? "pos" : "neg");
    } else rc.hidden = true;
  }
  const src = s.data_source || "—";
  const open = b.open_count || 0;
  if ($("heroMeta")) $("heroMeta").textContent = src + " \u00b7 " + open + " open";
  drawEquity(s);
}

const AGENT_SEATS = [
  { id: "scanner", name: "Scanner", handle: "@scan", color: "#0a84ff", head: "market" },
  { id: "chain", name: "Chain", handle: "@chain", color: "#5ac8ff", head: "research" },
  { id: "dev", name: "Devcheck", handle: "@dev", color: "#bf5af2", head: "reviewer" },
  { id: "meme", name: "Meme", handle: "@meme", color: "#ff9f0a", head: "lead" },
  { id: "analyst", name: "Analyst", handle: "@thesis", color: "#f2b2c2", head: "strategist" },
  { id: "risk", name: "Risk", handle: "@risk", color: "#ffd60a", head: "risk" },
  { id: "broker", name: "Broker", handle: "@fills", color: "#00d35a", head: "executor" },
];
// desk seat id (and the chat's "devcheck") -> pixel head art
const SEAT_HEAD = { scanner: "market", chain: "research", dev: "reviewer", devcheck: "reviewer",
  meme: "lead", analyst: "strategist", risk: "risk", broker: "executor", robot: "robot" };
const SEAT_NAME = { scanner: "Scanner", chain: "Chain", dev: "Devcheck", devcheck: "Devcheck",
  meme: "Meme", analyst: "Analyst", risk: "Risk", broker: "Broker" };
function headImg(id, px, cls) {
  const art = SEAT_HEAD[id];
  if (!art) return "";
  const b = "/static/assets/heads/head_" + art;
  return `<img class="px-head ${cls || ""}" src="${b}_48.png" srcset="${b}_48.png 1x, ${b}_192.png 4x"
    width="${px}" height="${px}" alt="" decoding="async">`;
}

function lastDecision(s) {
  return (s.decisions && s.decisions[0]) || null;
}

function seatRing(s, id) {
  const st = s.stats || {};
  const seen = st.seen || 0;
  const d = lastDecision(s);
  if (id === "scanner") return seen ? Math.min(100, seen) : 0;
  if (id === "chain") return seen ? Math.max(0, Math.min(100, (1 - (st.chain_rejects || 0) / seen) * 100)) : 0;
  if (id === "dev") return d && d.dev && d.dev.dev_score != null ? Number(d.dev.dev_score) : 0;
  if (id === "meme") return d && d.scorecard && d.scorecard.total != null ? Number(d.scorecard.total) : 0;
  if (id === "analyst") return d && d.thesis && d.thesis.score != null ? Number(d.thesis.score) * 10 : 0;
  if (id === "risk") {
    if (!d) return 0;
    return d.approved ? 100 : 25;
  }
  if (id === "broker") {
    const max = (s.config && s.config.max_positions) || 3;
    const open = (s.broker && s.broker.open_count) || 0;
    return max ? Math.min(100, (open / max) * 100) : 0;
  }
  return 0;
}

function seatStatus(s, id) {
  const st = s.stats || {};
  const d = lastDecision(s);
  const sym = d && d.symbol ? d.symbol : "";
  if (id === "scanner") return st.seen ? (st.seen + " coins seen") : "waiting";
  if (id === "chain") return st.chain_rejects ? (st.chain_rejects + " cut") : (st.seen ? "facts clear" : "waiting");
  if (id === "dev") {
    if (d && d.dev && d.dev.dev_score != null) return sym + " dev " + d.dev.dev_score;
    return st.dev_rejects ? (st.dev_rejects + " cut") : "waiting";
  }
  if (id === "meme") {
    if (d && d.scorecard && d.scorecard.total != null) return sym + " meme " + d.scorecard.total;
    return st.meme_rejects ? (st.meme_rejects + " cut") : "waiting";
  }
  if (id === "analyst") {
    if (d && d.thesis && d.thesis.score != null) return sym + " score " + d.thesis.score + "/10";
    return st.analyst_calls ? (st.analyst_calls + " calls") : "waiting";
  }
  if (id === "risk") {
    if (d && d.approved && d.ticket_usd != null) return "ticket " + fmtMoney(d.ticket_usd, 0);
    return st.risk_rejects ? (st.risk_rejects + " veto") : "waiting";
  }
  if (id === "broker") {
    const buy = s.latest_buy;
    if (buy && buy.ticket_usd != null) return "bought " + fmtMoney(buy.ticket_usd, 0);
    return st.entries ? (st.entries + " fills") : "waiting";
  }
  return "waiting";
}


// ---------- AI second opinion thread (display only; never changes a score or trade) ----------
const BOT_NAME = { gemini: "Gemini", groq: "Groq", openrouter: "OpenRouter",
                   cerebras: "Cerebras", cloudflare: "Cloudflare",
                   ollama: "Ollama", local: "Ollama (local)" };
function botBubble(bot, verdict, text, tag, right) {
  const b = String(bot || "ai").toLowerCase();
  const v = String(verdict || "").toLowerCase();
  return `<div class="bt-msg bot-${esc(b)}${right ? " r" : ""}">
    <img class="px-head bt-av" src="/static/assets/heads/head_robot_48.png" width="24" height="24" alt="">
    <div class="bt-bub"><div class="bt-who">${esc(BOT_NAME[b] || bot)}${tag ? ` <span class="bt-tag2">${esc(tag)}</span>` : ""}
      ${v ? `<span class="bt-v v-${esc(v)}">${esc(v)}</span>` : ""}</div>
      <div class="bt-say">${esc(shorten(text || "", 140))}</div></div></div>`;
}
function brainThreadHtml(d) {
  const br = d.brain || {};
  const ops = br.opinions || {};
  const order = (br.providers && br.providers.length) ? br.providers : Object.keys(ops);
  if (!order.length) return "";
  const line = (o) => (o.reasons && o.reasons.length ? o.reasons[0] : (o.red_flags && o.red_flags[0]) || "no reason given");
  const conf = (o) => (o.confidence != null ? " " + o.confidence + "%" : "");
  const out = [];
  const a = ops[order[0]];
  if (a) out.push(botBubble(order[0], a.verdict, line(a), "opinion" + conf(a)));
  const b = order[1] ? ops[order[1]] : null;
  if (b) {
    const agrees = a && a.verdict === b.verdict;
    out.push(botBubble(order[1], b.verdict, line(b), (agrees ? "agrees" : "disagrees") + conf(b), true));
  }
  const deb = Array.isArray(br.debate) ? br.debate.slice(1) : [];
  deb.forEach((ln, i) => out.push(botBubble(ln.bot, ln.verdict, ln.say, i === deb.length - 1 ? "final word" : "debate", ln.bot === order[1])));
  let note;
  if (br.agreement === "single") note = "one bot only · " + (br.verdict || "?");
  else if (br.agreement === "agree") note = "both agree · " + (br.verdict || "?");
  else if (br.final) note = "split → debated · final " + br.final.verdict + " (" + br.final.agree + "/2)";
  else note = "split · cautious call " + (br.verdict || "?") + (deb.length ? "" : " · no debate this time");
  const lim = br.limit_note ? `<div class="bt-note bt-limit">${esc(br.limit_note)}</div>` : "";
  return `<div class="bt-thread"><div class="bt-coin">$${esc(d.symbol || "?")}</div>${lim}${out.join("")}
    <div class="bt-note">${esc(note)} · advisory only</div></div>`;
}
function brainBoxHtml(s) {
  const rows = (s.decisions || []).filter((d) => d.brain && (d.brain.opinions || d.brain.verdict)).slice(0, 2);
  const adv = s.advisor || {};
  const resting = adv.resting || ((s.decisions || [])[0] && ((s.decisions[0].brain || {}).status === "resting"));
  if (resting) return '<div class="bt-empty">AI bots resting (limits), normal analyst</div>';
  if (!rows.length) {
    return `<div class="bt-empty">AI opinions: ${adv.enabled ? "waiting for coins" : "waiting for keys"}</div>`;
  }
  return rows.map(brainThreadHtml).join("");
}

function renderAgents(s) {
  const grid = $("agentsGrid");
  const badge = $("agentsRunning");
  if (badge) badge.textContent = s.running ? "7 running" : "0 running";
  if (!grid) return;
  const r = 30, cx = 32, cy = 32, circ = 2 * Math.PI * r;
  grid.innerHTML = AGENT_SEATS.map((a) => {
    const pct = Math.max(0, Math.min(100, seatRing(s, a.id)));
    const dash = (pct / 100) * circ;
    const isAn = a.id === "analyst";
    return `<article class="agent-card${isAn ? " analyst-card" : ""}" data-seat="${a.id}">
      <div class="avatar-wrap">
        <svg viewBox="0 0 64 64" aria-hidden="true">
          <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#222" stroke-width="3"></circle>
          <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${a.color}" stroke-width="3"
            stroke-linecap="round" stroke-dasharray="${dash.toFixed(2)} ${circ.toFixed(2)}"></circle>
        </svg>
        <div class="face">${headImg(a.id, 48)}</div>
      </div>
      <div class="abody">
        <div class="aname">${a.name}</div>
        <div class="handle">${a.handle}</div>
        <div class="astat">${esc(seatStatus(s, a.id))}</div>
      </div>
      ${isAn ? `<div class="brain-box"><div class="bt-h">AI second opinion</div>${brainBoxHtml(s)}</div>` : ""}
    </article>`;
  }).join("");
}

function renderActivity(s) {
  const feed = $("activityFeed");
  const badge = $("activityCount");
  const rows = s.activity || [];
  if (badge) badge.textContent = rows.length + (rows.length === 1 ? " trade" : " trades");
  if (!feed) return;
  if (!rows.length) {
    feed.innerHTML = '<div class="empty">No trades yet.</div>';
    return;
  }
  feed.innerHTML = rows.map((r) => {
    const bought = r.kind === "entry";
    let title = bought ? ("Bought $" + (r.symbol || "")) : "Sold";
    if (!bought) {
      const frac = r.fraction != null ? (Math.round(Number(r.fraction) * 100) + "% ") : "";
      title = "Sold " + frac + "$" + (r.symbol || "");
    }
    const reason = r.reason || r.message || "";
    let amt = "";
    let cls = "";
    if (bought) amt = fmtMoney(r.usd);
    else if (r.pnl_usd != null) {
      const n = Number(r.pnl_usd);
      amt = (n >= 0 ? "+" : "") + fmtMoney(n);
      cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
    } else amt = r.usd != null ? fmtMoney(r.usd) : "";
    let extra = "";
    if (r.mult != null && isFinite(Number(r.mult))) extra = Number(r.mult).toFixed(2) + "x";
    else if (r.mcap_usd != null) extra = "mc " + fmtMoney(r.mcap_usd, 0);
    return `<div class="activity-row">
      <div class="face" style="background:${bought ? "#00d35a" : "#ff453a"}">${bought ? "B" : "S"}</div>
      <div>
        <div class="ttl">${esc(title)}</div>
        <div class="sub" ${r.review ? `title="${esc(r.review)}"` : ""}>broker \u00b7 ${esc(reason)} \u00b7 ${esc(timeManila(r.time))}</div>
        ${r.review ? `<div class="sub review">${esc(r.review)}</div>` : ""}
      </div>
      <div>
        <div class="amt ${cls}">${amt}</div>
        <div class="meta">${esc(extra)}</div>
      </div>
    </div>`;
  }).join("");
}

let confettiKey = "";
function burstConfetti() {
  const c = $("confetti");
  if (!c) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const dpr = window.devicePixelRatio || 1;
  const w = window.innerWidth, h = window.innerHeight;
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  c.style.width = w + "px";
  c.style.height = h + "px";
  c.hidden = false;
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const colors = ["#00d35a", "#ffd60a", "#0a84ff", "#bf5af2", "#fff"];
  const bits = Array.from({ length: 80 }, () => ({
    x: Math.random() * w,
    y: -10 - Math.random() * 80,
    r: 3 + Math.random() * 4,
    vx: -1.5 + Math.random() * 3,
    vy: 2 + Math.random() * 3.5,
    rot: Math.random() * 6,
    vr: -0.2 + Math.random() * 0.4,
    color: colors[Math.floor(Math.random() * colors.length)],
  }));
  const t0 = performance.now();
  function frame(now) {
    const t = now - t0;
    ctx.clearRect(0, 0, w, h);
    bits.forEach((b) => {
      b.x += b.vx;
      b.y += b.vy;
      b.vy += 0.04;
      b.rot += b.vr;
      ctx.save();
      ctx.translate(b.x, b.y);
      ctx.rotate(b.rot);
      ctx.fillStyle = b.color;
      ctx.fillRect(-b.r, -b.r * 0.6, b.r * 2, b.r * 1.2);
      ctx.restore();
    });
    if (t < 1400) requestAnimationFrame(frame);
    else { ctx.clearRect(0, 0, w, h); c.hidden = true; }
  }
  requestAnimationFrame(frame);
}

function maybeConfetti(s) {
  const ev = (s.events || []).find((e) => e.kind === "exit" && Number(e.pnl_usd) > 0);
  if (!ev) return;
  const key = eventKey(ev);
  const t = ev.time ? new Date(ev.time).getTime() : 0;
  const age = Date.now() - t;
  if (key === confettiKey) return;
  if (!confettiKey && age > 8000) { confettiKey = key; return; }
  if (age > 20000) { confettiKey = key; return; }
  confettiKey = key;
  burstConfetti();
}

function render(s) {
  lastState = s;
  const b = s.broker || {};
  const st = s.stats || {};
  const cfg = s.config || {};
  renderMode(s);
  renderRisk(s);

  // status
  const pill = $("statusPill");
  pill.className = "pill " + (s.running ? "live live-on" : (s.cycle ? "paused" : ""));
  $("statusText").textContent = s.running ? "Live" : (s.cycle ? "paused" : "idle");
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
  const startEq = Number((s.starting_equity != null) ? s.starting_equity : 20);
  const kDayStart = Number(b.day_start != null ? b.day_start : startEq);
  $("kPnlPct").textContent = fmtPct((pnl / kDayStart) * 100);
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

  const gd = s.guard || {};
  const gl = $("guardLine");
  if (gl) {
    const rg = (gd.regime && gd.regime.tag) || "UNKNOWN";
    const bk = gd.breaker || {};
    gl.textContent = "Guard: market " + rg.toLowerCase().replace("_", "-") + " \u00b7 breaker " +
      (bk.state || "ok") + (bk.state && bk.state !== "ok" ? " (" + bk.reason + ")" : "") +
      (gd.coach && gd.coach.summary ? " \u00b7 " + gd.coach.summary : "");
  }
  const ig = s.integrations || {};
  const sl = $("sourcesLine");
  if (sl) {
    sl.textContent = "Sources: " + Object.keys(ig).map((k) => k.replace("_", " ") + " " +
      (ig[k].status || "?")).join(" \u00b7 ");
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

  // latest paper buy banner
  const latestBuy = s.latest_buy || null;
  const banner = $("buyBanner");
  if (banner) {
    if (latestBuy && latestBuy.mint) {
      banner.hidden = false;
      $("buyBannerText").textContent = "$" + (latestBuy.symbol || "") +
        (latestBuy.ticket_usd != null ? " \u2014 " + fmtMoney(latestBuy.ticket_usd) : "");
      const meta = $("buyBannerMeta");
      if (meta) { meta.innerHTML = tokenMetaHtml(latestBuy); bindCaCopy(meta); }
      const link = $("buyBannerLink");
      if (latestBuy.fomo_url) { link.href = latestBuy.fomo_url; link.hidden = false; }
      else { link.removeAttribute("href"); link.hidden = true; }
      const tabs = latestBuy.chart_tabs || ["15s", "30s"];
      const tf = chartTf[latestBuy.mint] || tabs[0];
      $("buyBannerTabs").innerHTML = tabsHtml(latestBuy.mint, tabs, tf);
      $("buyBannerTabs").dataset.mint = latestBuy.mint;
      bindChartTabs(banner);
      loadChart(latestBuy.mint, tf, $("buyBannerChart"), $("buyBannerTabs"));
    } else {
      banner.hidden = true;
    }
  }

  // positions (cards: chart + Buy on FOMO)
  const pos = b.positions || [];
  $("posBadge").textContent = pos.length;
  const pb = $("posCards");
  if (pb) {
    if (!pos.length) {
      pb.innerHTML = '<div class="empty">No open positions. Start the agent.</div>';
    } else {
      pb.innerHTML = pos.map((p) => {
        const cls = p.pnl_pct > 0 ? "pos" : p.pnl_pct < 0 ? "neg" : "";
        const tabs = p.chart_tabs || ["15s", "30s"];
        const tf = chartTf[p.mint] || tabs[0];
        return `<article class="pos-card" data-mint="${esc(p.mint)}">
          <div class="pos-head">
            <span class="sym">${esc(p.symbol)}</span>
            <span class="pnl ${cls}">${fmtPct(p.pnl_pct)}</span>
            ${fomoBtn(p.fomo_url)}
          </div>
          <div class="pos-meta">
            <span>Entry <b>${priceFmt(p.entry)}</b></span>
            <span>Last <b>${priceFmt(p.last)}</b>${p.mark_clamped ? ' <em class="mark-bad" title="quote was outside the sane band; showing the entry price">?</em>' : ''}</span>
            <span>Cost <b>${fmtMoney(p.cost)}</b></span>
            <span>Held <b>${heldFor(p.opened)}</b></span>
          </div>
          ${tokenMetaHtml(p)}
          <div class="tf-tabs">${tabsHtml(p.mint, tabs, tf)}</div>
          <canvas class="candle-chart" width="640" height="220"></canvas>
        </article>`;
      }).join("");
      bindChartTabs(pb);
      bindCaCopy(pb);
      pos.forEach((p) => {
        const card = pb.querySelector('.pos-card[data-mint="' + CSS.escape(p.mint) + '"]');
        if (!card) return;
        loadChart(p.mint, chartTf[p.mint], card.querySelector("canvas"), card.querySelector(".tf-tabs"));
      });
    }
  }

  // decisions
  const dec = s.decisions || [];
  $("decBadge").textContent = dec.length;
  const db = $("decBody");
  if (!dec.length) {
    db.innerHTML = '<tr><td colspan="8" class="empty">No decisions yet.</td></tr>';
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
      const sg = d.signal || null;
      const sgAct = sg ? String(sg.action || "").toUpperCase() : "";
      const sgCls = sgAct === "BUY" ? "buy" : sgAct === "SELL" ? "sell" : sgAct === "AVOID" ? "skip" : "watch";
      const sgTxt = sg
        ? `${esc(sgAct || "\u2014")}${sg.confidence != null ? " " + Math.round(sg.confidence) + "%" : ""}${sg.win_probability != null ? " \u00b7 win " + Math.round(sg.win_probability * 100) + "%" : ""}`
        : "\u2014";
      const sgTitle = sg && sg.prompt ? esc(sg.prompt) : "";
      return `<tr data-mint="${esc(d.mint || "")}">
        <td><span class="sym">${esc(d.symbol)}</span>${tokenMetaHtml(d)}</td>
        <td><span class="tag ${sgCls}" title="${sgTitle}">${sgTxt}</span></td>
        <td><span class="tag ${tag}">${esc(label)}</span></td>
        <td>${d.thesis ? d.thesis.score : "—"}</td>
        <td>${memeCell}</td>
        <td>${devCell}</td>
        <td>${d.approved ? fmtMoney(d.ticket_usd) + " " + groqChip(d) + fomoBtn(d.fomo_url) : "—"}</td>
        <td class="muted" style="font-family:var(--sans);font-size:11.5px">${esc(d.reason || "")} <span class="tag ${d.source === "demo" ? "demo" : "live"}">${esc(d.source || "—")}</span></td>
      </tr>`;
    }).join("");
    bindCaCopy(db);
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
        ${tokenMetaHtml(d)}
        <div class="flags">${flags || '<div class="flag">no flags</div>'}</div>
        ${d.approved ? `<div style="margin-top:9px">${bar("risk", sizes.by_risk)}${bar("size", sizes.by_size)}${bar("pool", sizes.by_pool)}</div>
          ${fomoBtn(d.fomo_url)}` : ""}
        <div class="foot">shape: ${esc(t.shape)} · src: ${esc(d.source || "—")} · ${timeOf(d.time)}</div>
      </div>`;
    }).join("");
    bindCaCopy(tl);
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
        <div class="head">${headImg(m.from, 24, "chip-head")}<b>${esc(m.seat || m.from)}</b><span class="arrow">\u2192</span><b>${esc(m.to)}</b></div>
        <div class="body">${esc(m.text)}</div>
      </div>`).join("");
  } else {
    $("dlgBadge").textContent = "template";
    $("dialogue").innerHTML = '<div class="empty">The seats have not spoken yet.</div>';
  }

  renderHero(s);
  renderAgents(s);
  renderActivity(s);
  maybeConfetti(s);
  renderCat(s);
}


let lastBacktest = null;
let backtestAt = 0;

function renderBacktest(data) {
  const body = $("backtestBody");
  const badge = $("backtestBadge");
  const note = $("backtestNote");
  if (!body) return;
  if (!data || data.ok === false) {
    body.innerHTML = '<div class="empty">Backtest unavailable.</div>';
    return;
  }
  const hours = data.hours != null ? Number(data.hours).toFixed(1) + "h" : "—";
  if (badge) badge.textContent = data.thin ? "thin data · read only" : "read only";
  if (note) {
    note.textContent = (data.candidates || 0) + " candidates · " + (data.price_points || 0) +
      " prices · " + hours + " recorded. Replays gates only; never changes the live desk.";
  }
  const rows = (data.rows || []).filter((r, i, arr) => {
    if (r.current) return true;
    // keep the score sweep plus current; hide the extra isolated rows on the phone card
    return String(r.id || "").startsWith("score_");
  });
  const tr = rows.map((r) => {
    const wr = r.win_rate == null ? "—" : Math.round(r.win_rate * 100) + "%";
    const pnlCls = r.pnl_usd > 0 ? "bt-pos" : (r.pnl_usd < 0 ? "bt-neg" : "");
    const tag = r.current ? '<span class="bt-tag">now</span>' : "";
    return `<tr class="${r.current ? "current" : ""}">
      <td>${esc(r.label)}${tag}</td>
      <td>${r.trades}</td>
      <td>${wr}</td>
      <td class="${pnlCls}">${fmtMoney(r.pnl_usd)}</td>
      <td>${fmtMoney(r.max_drawdown_usd)}</td>
      <td>${r.unknown}</td>
    </tr>`;
  }).join("");
  const caveat = (data.caveats && data.caveats[0]) ? `<div class="bt-caveat">${esc(data.caveats[0])}</div>` : "";
  body.innerHTML = `<table class="bt-table">
    <thead><tr><th>setting</th><th>trades</th><th>win</th><th>pnl</th><th>max dd</th><th>unk</th></tr></thead>
    <tbody>${tr || '<tr><td colspan="6">No recorded tickets yet.</td></tr>'}</tbody>
  </table>${caveat}`;
}

async function refreshBacktest(force) {
  const now = Date.now();
  if (!force && lastBacktest && now - backtestAt < 60000) {
    renderBacktest(lastBacktest);
    return;
  }
  try {
    const data = await api("/api/backtest");
    lastBacktest = data;
    backtestAt = now;
    renderBacktest(data);
  } catch (e) {
    if (!lastBacktest && $("backtestBody")) {
      $("backtestBody").innerHTML = '<div class="empty">Backtest unavailable.</div>';
    }
  }
}

// ---------- AI quota ----------
let lastQuota = null;
let quotaAt = 0;

function renderQuota(q) {
  const box = $("quotaBars");
  if (!box) return;
  const provs = (q && q.providers) || {};
  const names = Object.keys(provs).filter((n) => provs[n] && provs[n].enabled);
  const badge = $("quotaBadge");
  if (badge) {
    const resting = q && q.resting;
    badge.textContent = resting ? "resting" : (names.length ? names.length + " live" : "off");
    badge.style.color = resting ? "var(--amber)" : (names.length ? "var(--green)" : "var(--muted)");
  }
  if (!names.length) {
    box.innerHTML = '<div class="tiny muted">No AI keys set. Add GEMINI_API_KEY, GROQ_API_KEY, CEREBRAS_API_KEY or OPENROUTER_API_KEY.</div>';
    if ($("quotaLine")) $("quotaLine").textContent = "Free-tier calls left today: —";
    return;
  }
  box.innerHTML = names.map((n) => {
    const p = provs[n] || {};
    const used = Number(p.used || 0), budget = Number(p.budget || 0);
    const pct = budget ? Math.min(100, Math.round((used / budget) * 100)) : 0;
    const cooling = Number(p.cooling_s || 0);
    const color = cooling ? "var(--amber)" : pct >= 90 ? "var(--red)" : pct >= 70 ? "var(--amber)" : "var(--green)";
    const keys = Number(p.keys || 0), kc = Number(p.keys_cooling || 0);
    const note = cooling ? "cooling " + cooling + "s"
      : kc ? kc + " of " + keys + " keys cooling"
      : keys > 1 ? keys + " keys" : "";
    return `<div class="bar-row" title="${esc(n)}: ${used}/${budget} calls today${cooling ? ", cooling " + cooling + "s" : ""}">
      <span class="lab">${esc(n)}</span>
      <span class="track"><i style="width:${pct}%;background:${color}"></i></span>
      <span class="num">${budget ? used + "/" + budget : "—"}</span>
    </div>${note ? `<div class="tiny muted" style="margin:-4px 0 2px 82px">${esc(note)}</div>` : ""}`;
  }).join("");
  if ($("quotaLine")) {
    const left = Number(q.total_left || 0), total = Number(q.total_budget || 0);
    $("quotaLine").textContent = "Free-tier calls left today: " + left + " of " + total
      + (q.resting ? " · all providers cooling, desk keeps trading on rules" : "");
  }
}

async function refreshQuota(force) {
  const now = Date.now();
  if (!force && lastQuota && now - quotaAt < 30000) { renderQuota(lastQuota); return; }
  try {
    const q = await api("/api/ai/status");
    lastQuota = q && q.quota ? q.quota : null;
    quotaAt = now;
    if (lastQuota) renderQuota(lastQuota);
  } catch (e) { /* quota is cosmetic; never break the dashboard */ }
}

// ---------- Groq sizer: is the model in action, and what did it do ----------
// Shrink-only by design, so the only interesting states are "cut" (it lowered a
// ticket), "pass" (it looked and said 1.0x) and "fail" (a call that did not
// produce a usable answer -- the desk traded at rule size anyway).
function groqRow(r) {
  const src = String(r.source || "");
  const failed = /^(http_|error:|unparsed|budget)/.test(src);
  const cls = failed ? "fail" : (r.cut ? "cut" : "pass");
  const mult = Number(r.mult);
  const multTxt = failed ? "1.0x" : mult.toFixed(2).replace(/0+$/, "").replace(/\.$/, "") + "x";
  const why = (r.reasons && r.reasons.length) ? r.reasons.join(" \u00b7 ")
    : failed ? "no usable answer, rule size kept" : "no objection";
  const conf = r.confidence != null ? "conf " + Math.round(r.confidence) : "";
  const lat = r.latency_ms != null ? r.latency_ms + "ms" : "";
  const meta = [conf, lat].filter(Boolean).join(" \u00b7 ");
  return `<div class="groq-row ${cls}" title="${esc(why)}">
    <div class="top">
      <span class="sym">${esc(r.symbol || "\u2014")}</span>
      <span class="mult">${multTxt}</span>
      <span class="src">${esc(src || "\u2014")}</span>
    </div>
    <div class="why">${esc(why)}</div>
    ${meta ? `<div class="meta">${esc(meta)}</div>` : ""}
  </div>`;
}

function renderGroq(g) {
  const badge = $("groqBadge");
  if (!badge) return;
  const on = !!(g && g.enabled);
  badge.textContent = on ? "ON" : "OFF";
  badge.style.color = on ? "var(--green)" : "var(--muted)";

  const total = Number((g && g.budget_total) || 0);
  const left = Number((g && g.budget_left) || 0);
  const used = Math.max(0, total - left);
  const pct = total ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const bar = $("groqBudgetBar");
  if (bar) {
    bar.style.width = pct + "%";
    bar.style.background = pct >= 90 ? "var(--red)" : pct >= 70 ? "var(--amber)" : "var(--blue)";
  }
  if ($("groqBudgetNum")) $("groqBudgetNum").textContent = total ? used + "/" + total : "\u2014";

  const line = $("groqLine");
  if (line) {
    line.textContent = on
      ? "Shrink-only, capped at 1.00x. Model: " + (g.model || "\u2014")
      : "Off (" + ((g && g.reason) || "no key") + "). Tickets use rule size.";
  }

  const sum = $("groqSummary");
  if (sum) {
    if (!g || !g.recent_calls) {
      sum.textContent = on ? "No calls yet this session." : "Enable with BRAIN_SIZER_ON=1 and a GROQ_API_KEY.";
    } else {
      const cuts = Number(g.recent_cuts || 0);
      const avg = g.avg_cut != null ? " \u00b7 avg cut " + Number(g.avg_cut).toFixed(2) + "x" : "";
      sum.textContent = cuts + " cut" + (cuts === 1 ? "" : "s") + " of " + g.recent_calls
        + " recent call" + (g.recent_calls === 1 ? "" : "s") + avg;
    }
  }

  const list = $("groqList");
  if (list) {
    const rows = (g && g.recent) || [];
    list.innerHTML = rows.length
      ? rows.map(groqRow).join("")
      : '<div class="tiny muted" style="padding:4px 2px">Nothing yet \u2014 the sizer runs after every rule has already approved a setup.</div>';
  }
}

// ---------- Groq post-mortem: offline pattern read over closed trades ----------
// This panel never triggers a model call. It shows how far the sample is from
// the 30-trade bar, and the last report if one exists. The run itself is a CLI
// step (scripts/groq_postmortem.py) so a dashboard refresh can never burn quota.
function pmTrait(t) {
  const trait = (t && t.trait) || "\\u2014";
  const ev = (t && t.evidence) || "";
  const conf = (t && t.confidence) || "";
  const cls = /high/i.test(conf) ? "high" : /med/i.test(conf) ? "med" : "low";
  return `<div class="pm-trait ${cls}">
    <div class="tt">${esc(trait)}<span class="cf">${esc(conf)}</span></div>
    ${ev ? `<div class="te">${esc(ev)}</div>` : ""}
  </div>`;
}

function renderPostmortem(g) {
  const badge = $("pmBadge");
  if (!badge) return;

  const cases = Number((g && g.cases) || 0);
  const need = Number((g && g.min_trades) || 30);
  const ready = !!(g && g.ready);
  const ok = !!(g && g.ok);
  const on = !!(g && g.enabled);

  const state = !on ? "OFF" : ok ? "REPORT" : ready ? "READY" : "WAITING";
  badge.textContent = state;
  badge.style.color = !on ? "var(--muted)"
    : ok ? "var(--green)" : ready ? "var(--amber)" : "var(--muted)";

  const pct = need ? Math.min(100, Math.round((cases / need) * 100)) : 0;
  const bar = $("pmBar");
  if (bar) {
    bar.style.width = pct + "%";
    bar.style.background = ok ? "var(--green)" : ready ? "var(--amber)" : "var(--blue)";
  }
  if ($("pmNum")) $("pmNum").textContent = cases + "/" + need;

  const line = $("pmLine");
  if (line) {
    line.textContent = !on
      ? "Off (" + ((g && g.reason) || "no GROQ_API_KEY") + "). Nothing is sent anywhere."
      : ok
        ? "Last report on " + ((g && g.ran_on) || 0) + " closed trades. A hypothesis, not an edge."
        : ready
          ? "Sample is large enough. Run scripts/groq_postmortem.py to produce a report."
          : "Collecting: " + cases + " of " + need + " closed trades. Below the bar a model "
            + "would just invent patterns.";
  }

  const split = $("pmSplit");
  if (split) {
    const w = g && g.wins, l = g && g.losses;
    if (ok && w != null && l != null) {
      const wr = g.win_rate != null ? Math.round(Number(g.win_rate) * 100) + "%" : "\\u2014";
      split.innerHTML = `<span class="w">${w}W</span><span class="l">${l}L</span>`
        + `<span class="wr">${wr} win</span>`;
    } else {
      split.innerHTML = "";
    }
  }

  const v = $("pmVerdict");
  if (v) {
    if (ok && (g.verdict || g.null_warning)) {
      v.innerHTML = (g.verdict ? `<div class="vd">${esc(g.verdict)}</div>` : "")
        + (g.null_warning ? `<div class="nw">${esc(g.null_warning)}</div>` : "");
    } else {
      v.innerHTML = "";
    }
  }

  const tr = $("pmTraits");
  if (tr) {
    if (!ok) {
      tr.innerHTML = "";
    } else {
      const losers = g.loser_traits || [];
      const winners = g.winner_traits || [];
      tr.innerHTML =
        `<div class="pm-group"><div class="gh lose">losers share</div>`
        + (losers.length ? losers.map(pmTrait).join("")
           : '<div class="tiny muted">no separating feature found</div>')
        + `</div>`
        + `<div class="pm-group"><div class="gh win">winners share</div>`
        + (winners.length ? winners.map(pmTrait).join("")
           : '<div class="tiny muted">no separating feature found</div>')
        + `</div>`;
    }
  }

  const nx = $("pmNext");
  if (nx) {
    nx.innerHTML = ok && g.next_test
      ? `<div class="pm-group"><div class="gh next">next test</div>`
        + `<div class="nt">${esc(g.next_test)}</div></div>`
      : "";
  }

  const run = $("pmRun");
  if (run) {
    run.textContent = !on
      ? ""
      : ok
        ? "Runs offline. Re-run: python3 scripts/groq_postmortem.py --force"
        : "Runs offline, never on refresh: python3 scripts/groq_postmortem.py";
  }
}

// A visible heartbeat. Without this, a frozen page and a quiet market look
// identical -- you cannot tell "nothing is happening" from "the socket died".
let SYNC_FAILS = 0;

function paintSync(ok) {
  const pill = $("syncPill");
  const txt = $("syncText");
  if (!pill || !txt) return;
  if (ok) {
    SYNC_FAILS = 0;
    pill.style.color = "var(--green)";
    txt.textContent = "sync: " + new Date().toLocaleTimeString();
  } else {
    SYNC_FAILS += 1;
    pill.style.color = "var(--red)";
    txt.textContent = "sync: stale " + SYNC_FAILS + "x";
  }
}

async function refresh() {
  try {
    const s = await api("/api/state");
    render(s);
    renderGroq(s && s.groq_sizer);
    renderPostmortem(s && s.groq_postmortem);
    paintSync(true);
    refreshBacktest(false);
    refreshQuota(false);
  } catch (e) {
    console.error(e);
    paintSync(false);
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
$("btnContinue").onclick = async () => {
  if (!confirm("Resume PAPER trading for the rest of today? The 3% stop comes back at the day roll.")) return;
  const r = await act("/api/risk/continue", "POST", null);
  toast(r && r.ok ? "Paper trading resumed for today" : ((r && r.error) || "Continue refused"));
};
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

const equityTabs = $("equityTabs");
if (equityTabs) {
  equityTabs.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tf]");
    if (!btn) return;
    equityTf = btn.dataset.tf;
    equityTabs.querySelectorAll("button").forEach((b) => {
      const on = b === btn;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (lastState) renderHero(lastState);
  });
}
window.addEventListener("resize", () => { if (lastState) drawEquity(lastState); });

initDailyStop();
refresh();
refreshBacktest(true);
setInterval(refresh, 2000);
setInterval(() => refreshBacktest(false), 60000);
setInterval(tickAges, 1000);

/* ---------- desk tabs: Control / Live / Research ---------- */
(function initDeskTabs() {
  const bar = document.getElementById("deskTabs");
  const layout = document.getElementById("deskLayout");
  if (!bar || !layout) return;

  const KEY = "desk.tab";
  const tabs = Array.from(bar.querySelectorAll(".desk-tab"));
  const cols = { control: ".col-left", live: ".col-center", research: ".col-right" };
  const narrow = () => window.matchMedia("(max-width: 900px)").matches;

  function activate(name, { scroll = true } = {}) {
    if (!cols[name]) name = "live";
    layout.dataset.tab = name;
    tabs.forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    try { localStorage.setItem(KEY, name); } catch (e) {}
    if (scroll) {
      if (narrow()) {
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else {
        const el = layout.querySelector(cols[name]);
        if (el) {
          const y = el.getBoundingClientRect().top + window.scrollY - 110;
          window.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
        }
      }
    }
  }

  tabs.forEach((t) => {
    t.addEventListener("click", () => activate(t.dataset.tab));
    // left/right arrow keys move between tabs
    t.addEventListener("keydown", (e) => {
      const i = tabs.indexOf(t);
      if (e.key === "ArrowRight") { e.preventDefault(); tabs[(i + 1) % tabs.length].focus(); }
      if (e.key === "ArrowLeft") { e.preventDefault(); tabs[(i - 1 + tabs.length) % tabs.length].focus(); }
    });
  });

  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) {}
  activate(saved || "live", { scroll: false });

  // badge: open positions on the Live tab, decisions on Research
  function setBadge(name, n) {
    const t = tabs.find((x) => x.dataset.tab === name);
    if (!t) return;
    let b = t.querySelector(".tab-badge");
    if (!n) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement("span"); b.className = "tab-badge"; t.appendChild(b); }
    b.textContent = n > 99 ? "99+" : String(n);
  }
  function syncBadges() {
    const pos = document.querySelectorAll("#posCards .pos-card").length;
    const dec = document.querySelectorAll("#decBody tr[data-mint]").length;
    setBadge("live", pos);
    setBadge("research", dec);
  }
  const mo = new MutationObserver(syncBadges);
  ["posCards", "decBody"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) mo.observe(el, { childList: true });
  });
  syncBadges();
})();

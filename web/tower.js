/* Control Tower. Polls /api/tower only while this tab is open. */
(function () {
  const root = document.getElementById("towerView");
  if (!root) return;

  const HEADS = {
    market: "/static/assets/heads/head_market_48.png",
    strategist: "/static/assets/heads/head_strategist_48.png",
    robot: "/static/assets/heads/head_robot_48.png",
    executor: "/static/assets/heads/head_executor_48.png",
    risk: "/static/assets/heads/head_risk_48.png",
  };

  let last = null;
  let tokenTab = "holders";
  let timer = null;
  let clock = null;
  let chartMint = "";
  let chartBars = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  const money = (n) => {
    const v = Number(n || 0);
    const body = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (v < 0 ? "-$" : "$") + body;
  };

  const mcap = (n) => {
    const v = Number(n);
    if (!isFinite(v) || v <= 0) return "—";
    if (v >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
    return "$" + v.toFixed(0);
  };

  const rel = (sec) => {
    const s = Math.max(0, Math.round(sec));
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m";
    const h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m";
  };

  const hm = (ms) => new Date(ms).toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit" });

  function shell() {
    root.innerHTML = `
      <div class="tw-banner" id="twBanner" hidden>
        <span id="twBannerText"></span>
        <button type="button" id="twReplay">Replay</button>
      </div>
      <div class="tw">
        <section class="tw-flight" aria-label="Session timeline">
          <div class="tw-flight-top">
            <div class="tw-clock" id="twStart">--:--</div>
            <div class="tw-track" id="twTrack">
              <div class="tw-track-line"></div>
              <div class="tw-plane" id="twPlane" aria-hidden="true">✈</div>
              <div class="tw-now" id="twNow">--:--</div>
            </div>
            <div class="tw-clock" id="twEnd">--:--</div>
          </div>
          <div class="tw-flight-sub">
            <span id="twAgo">Started —</span>
            <span id="twDone">Done in —</span>
          </div>
        </section>

        <section class="tw-stats" aria-label="Cursor stats">
          <div class="tw-stat"><span class="k">CLICKS</span><span class="v" id="twClicks">0</span></div>
          <div class="tw-stat"><span class="k">HOLDS</span><span class="v" id="twHolds">0</span></div>
          <div class="tw-stat"><span class="k">RISK FIRST</span><span class="v" id="twRisk">0</span></div>
          <div class="tw-stat" id="twCollCard"><span class="k">COLLISIONS</span><span class="v" id="twColl">0</span></div>
        </section>

        <section class="tw-agents" id="twAgents" aria-label="Seats"></section>

        <section class="tw-center">
          <div class="tw-card tw-bot-card">
            <div class="tw-bot">
              <img class="tw-head" src="${HEADS.robot}" alt="" width="48" height="48">
              <div>
                <div class="tw-bot-top"><span class="name">Grok Bot</span></div>
                <div class="tw-bot-sub" id="twParked">cursor parked</div>
                <div class="tw-next" id="twNext" hidden><i class="dot"></i><span id="twNextText">next</span></div>
              </div>
            </div>
            <div class="tw-queue" id="twQueue"></div>
          </div>
          <div class="tw-card tw-pnl" id="twPnl">
            <div class="eye">PAPER EQUITY</div>
            <div class="big" id="twEquity">$0.00</div>
            <div class="from" id="twFrom">from $0.00 this session · 1.00x</div>
            <div class="tw-zero"><span class="n" id="twZero">0</span><span>cursor collisions</span></div>
            <div class="tw-note">One thread, one cursor. Risk speaks before a new buy. This view never signs and never changes a rule.</div>
          </div>
        </section>

        <section class="tw-side">
          <div class="tw-card tw-token" id="twToken">
            <div class="tw-token-bar">
              <div class="tw-dots" aria-hidden="true"><i></i><i></i><i></i></div>
              <div class="tw-token-name" id="twSym">—</div>
              <div class="tw-mcap" id="twMcap">—</div>
            </div>
            <div class="tw-tabs" role="tablist">
              <button type="button" data-tok="chart" role="tab">Chart</button>
              <button type="button" data-tok="holders" class="on" role="tab" aria-selected="true">Holders</button>
              <button type="button" data-tok="trades" role="tab">Trades</button>
            </div>
            <div class="tw-tab-body" id="twTab"></div>
            <div class="tw-buyrow">
              <div class="tw-amt" id="twAmt">$0</div>
              <button class="tw-buy" type="button" id="twBuy" disabled>Paper</button>
            </div>
            <div class="tw-positions" id="twPos"></div>
          </div>
          <div class="tw-card tw-read">
            <h3>READ-BACK</h3>
            <ul class="tw-log" id="twLog"></ul>
          </div>
        </section>
      </div>
      <div class="tw-foot">
        <span id="twFoot">Paper only · waiting for the loop</span>
        <button type="button" id="twDemo">Play sample night</button>
      </div>`;
    root.querySelector("#twReplay").onclick = () => postDemo({ enabled: true, replay: true });
    root.querySelector("#twDemo").onclick = () => postDemo({ enabled: !(last && last.demo) });
    root.querySelectorAll(".tw-tabs button").forEach((b) => {
      b.onclick = () => {
        tokenTab = b.dataset.tok;
        paintTabs();
        paintTokenBody();
      };
    });
  }

  async function postDemo(body) {
    try {
      await fetch("/api/tower/demo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await pull();
    } catch (e) { /* the next poll will show whatever the desk still has */ }
  }

  function badgeClass(text) {
    const t = String(text || "").toLowerCase();
    if (t.startsWith("on")) return "on";
    if (t.startsWith("hold")) return "hold";
    if (t.startsWith("ver")) return "verified";
    if (t.startsWith("done")) return "done";
    return "ready";
  }

  function paintAgents(d) {
    const host = root.querySelector("#twAgents");
    host.innerHTML = (d.agents || []).map((a) => {
      const spark = (a.spark || []).map((v) => {
        const h = Math.max(8, Math.round(Number(v) * 100));
        return `<i style="height:${h}%"></i>`;
      }).join("");
      const waits = Number(a.avg_wait_s || 0);
      const wait = waits >= 10 ? waits.toFixed(0) + "s" : waits.toFixed(1) + "s";
      return `<article class="tw-agent" style="--c:${esc(a.color)}">
        <img class="tw-head" alt="" width="42" height="42" src="${HEADS[a.head] || HEADS.robot}">
        <div>
          <div class="tw-agent-top">
            <span class="name">${esc(a.name)}</span>
            <span class="tw-job">${esc(a.job)}</span>
            <span class="tw-badge ${badgeClass(a.badge)}">${esc(a.badge)}</span>
          </div>
          <div class="tw-line">${esc(a.line || "idle")}</div>
          <div class="tw-meta">clicks ${Number(a.clicks || 0)} · held ${Number(a.holds || 0)} · avg wait ${wait}</div>
          <div class="tw-spark" style="--c:${esc(a.color)}">${spark}</div>
        </div>
      </article>`;
    }).join("");
  }

  function paintQueue(d) {
    const host = root.querySelector("#twQueue");
    const rows = d.queue || [];
    if (!rows.length) {
      host.innerHTML = `<div class="tw-empty">No clicks yet. The loop writes them here as it works.</div>`;
      return;
    }
    host.innerHTML = rows.map((a) => `<div class="tw-chip" style="--c:${esc(a.color)}">
      <span class="who">${esc((a.id || "").split("-")[0])}</span>
      <span>${esc(a.id)} ${esc(a.text)}${a.symbol ? " · " + esc(a.symbol) : ""}</span>
    </div>`).join("");
    host.scrollTop = host.scrollHeight;
  }

  function paintLog(d) {
    const host = root.querySelector("#twLog");
    const rows = d.readback || [];
    if (!rows.length) {
      host.innerHTML = `<li>Waiting for the paper loop.</li>`;
      return;
    }
    const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 36;
    host.innerHTML = rows.map((r) => `<li class="${esc(r.level || "ok")}">${esc(r.text)}</li>`).join("");
    if (nearBottom) host.scrollTop = host.scrollHeight;
  }

  function paintPositions(d) {
    const host = root.querySelector("#twPos");
    const rows = d.positions || [];
    if (!rows.length) {
      host.innerHTML = `<div class="tw-empty">No open paper positions.</div>`;
      return;
    }
    host.innerHTML = rows.map((p) => {
      const pnl = Number(p.pnl_usd || 0);
      const cls = pnl >= 0 ? "up" : "down";
      const sign = pnl >= 0 ? "+" : "";
      return `<div class="tw-pos">
        <span class="sym">${esc(p.symbol || "?")}</span>
        <span class="tag">open</span>
        <span class="pnl ${cls}">${sign}${money(pnl)}</span>
      </div>`;
    }).join("");
  }

  function paintTabs() {
    root.querySelectorAll(".tw-tabs button").forEach((b) => {
      const on = b.dataset.tok === tokenTab;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function paintTokenBody() {
    const d = last;
    const host = root.querySelector("#twTab");
    if (!d) return;
    const tok = d.token;
    if (!tok) {
      host.innerHTML = `<div class="tw-empty">No coin on the cursor yet.</div>`;
      return;
    }
    if (tokenTab === "holders") {
      const bars = tok.holders;
      if (!bars) {
        host.innerHTML = `<div class="tw-empty">No holder read on this coin yet. The chain seat fills this when it actually looks.</div>`;
        return;
      }
      host.innerHTML = `<div class="tw-hold">${bars.map((b) => `<div class="tw-hold-row">
        <span>${esc(b.label)}</span>
        <div class="tw-hold-track"><span style="--w:${Math.max(0, Math.min(100, Number(b.pct) || 0))}%"></span></div>
        <span>${Number(b.pct || 0).toFixed(1)}%</span>
      </div>`).join("")}</div>`;
      return;
    }
    if (tokenTab === "trades") {
      const rows = tok.trades || [];
      if (!rows.length) {
        host.innerHTML = `<div class="tw-empty">No clicks on ${esc(tok.symbol || "this coin")} yet.</div>`;
        return;
      }
      host.innerHTML = `<ul class="tw-log">${rows.map((a) =>
        `<li>✓ ${esc(a.id)} ${esc(a.text)}</li>`).join("")}</ul>`;
      return;
    }
    if (!chartBars) {
      host.innerHTML = `<div class="tw-empty">Loading candles…</div>`;
      return;
    }
    if (!chartBars.length) {
      host.innerHTML = `<div class="tw-empty">No candles cached for this mint yet.</div>`;
      return;
    }
    const closes = chartBars.map((c) => Number(c.close ?? c.c ?? c[4] ?? 0)).filter((n) => n > 0);
    if (!closes.length) {
      host.innerHTML = `<div class="tw-empty">No candles cached for this mint yet.</div>`;
      return;
    }
    const lo = Math.min(...closes);
    const hi = Math.max(...closes);
    const span = hi - lo || hi || 1;
    host.innerHTML = `<div class="tw-bars">${closes.slice(-32).map((v) => {
      const h = Math.max(8, Math.round(((v - lo) / span) * 100));
      return `<i style="height:${h}%"></i>`;
    }).join("")}</div>`;
  }

  async function loadChart(mint) {
    if (!mint || String(mint).startsWith("DEMO")) {
      chartMint = mint || "";
      chartBars = [];
      if (tokenTab === "chart") paintTokenBody();
      return;
    }
    if (mint === chartMint && chartBars) return;
    chartMint = mint;
    chartBars = null;
    if (tokenTab === "chart") paintTokenBody();
    try {
      const r = await fetch("/api/chart?mint=" + encodeURIComponent(mint));
      const data = await r.json();
      if (chartMint !== mint) return;
      chartBars = data.candles || data.ohlc || data.bars || [];
    } catch (e) {
      chartBars = [];
    }
    if (tokenTab === "chart") paintTokenBody();
  }

  function paintClock() {
    if (!last || !last.window) return;
    const w = last.window;
    const now = Date.now();
    const start = Number(w.start) * 1000;
    const end = Number(w.end) * 1000;
    const span = Math.max(1, end - start);
    const p = Math.max(0, Math.min(1, (now - start) / span));
    const pct = (p * 100).toFixed(2) + "%";
    const track = root.querySelector("#twTrack");
    if (track) track.style.setProperty("--p", pct);
    root.querySelector("#twStart").textContent = hm(start);
    root.querySelector("#twEnd").textContent = hm(end);
    const nowEl = root.querySelector("#twNow");
    nowEl.textContent = hm(now);
    // The floating time sits on the start or end clock when the plane is at either end.
    nowEl.style.opacity = (p < 0.1 || p > 0.9) ? "0" : "1";
    root.querySelector("#twAgo").innerHTML = "Started <b>" + rel((now - start) / 1000) + "</b> ago";
    const left = (end - now) / 1000;
    root.querySelector("#twDone").innerHTML = left <= 0 ? "<b>Landed</b>" : "Done in <b>" + rel(left) + "</b>";
  }

  function paint(d) {
    last = d;
    const banner = root.querySelector("#twBanner");
    if (d.demo) {
      banner.hidden = false;
      root.querySelector("#twBannerText").textContent = d.demo_label || "DEMO REPLAY";
      root.querySelector("#twDemo").textContent = "Back to live book";
    } else {
      banner.hidden = true;
      root.querySelector("#twDemo").textContent = "Play sample night";
    }
    root.querySelector("#twClicks").textContent = Number(d.clicks || 0).toLocaleString("en-US");
    root.querySelector("#twHolds").textContent = Number(d.holds || 0).toLocaleString("en-US");
    root.querySelector("#twRisk").textContent = Number(d.risk_first || 0).toLocaleString("en-US");
    const coll = Number(d.collisions || 0);
    root.querySelector("#twColl").textContent = String(coll);
    root.querySelector("#twCollCard").classList.toggle("is-ok", coll === 0);
    root.querySelector("#twCollCard").classList.toggle("is-bad", coll > 0);
    root.querySelector("#twZero").textContent = String(coll);
    const parked = d.holder_name ? ("cursor · " + d.holder_name) : "cursor parked";
    root.querySelector("#twParked").textContent = parked;
    const next = root.querySelector("#twNext");
    if (d.next_name) {
      next.hidden = false;
      const agent = (d.agents || []).find((a) => a.id === d.next);
      next.style.setProperty("--c", agent ? agent.color : "#c084fc");
      root.querySelector("#twNextText").textContent = "next " + d.next_name;
    } else {
      next.hidden = true;
    }
    const pnl = d.pnl || {};
    const up = Number(pnl.equity || 0) + 1e-9 >= Number(pnl.start || 0);
    root.querySelector("#twPnl").classList.toggle("is-down", !up);
    root.querySelector("#twEquity").textContent = money(pnl.equity);
    const mult = Number(pnl.multiple || 0);
    const m = (mult >= 10 ? mult.toFixed(1) : mult.toFixed(2)) + "x";
    root.querySelector("#twFrom").textContent =
      "from " + money(pnl.start) + " " + (d.span_label || "this session") + " · " + m;
    const tok = d.token;
    root.querySelector("#twSym").textContent = tok && tok.symbol ? tok.symbol : "—";
    root.querySelector("#twMcap").textContent = tok ? mcap(tok.mcap_usd) : "—";
    const usd = lastUsd(d);
    root.querySelector("#twAmt").textContent = usd == null ? "$0" : money(usd);
    const buy = root.querySelector("#twBuy");
    buy.textContent = tok && tok.symbol ? ("Buy " + tok.symbol) : "Paper";
    buy.title = "The paper loop clicks this. Nothing on this screen sends an order.";
    paintAgents(d);
    paintQueue(d);
    paintLog(d);
    paintPositions(d);
    paintTabs();
    paintTokenBody();
    paintClock();
    const foot = d.running ? "Paper loop running" : "Paper loop paused";
    const cyc = d.cycle ? " · cycle " + d.cycle : "";
    root.querySelector("#twFoot").textContent = (d.demo ? "Sample night · book untouched · " : "") + foot + cyc;
    if (tok && tok.mint && tokenTab === "chart") loadChart(tok.mint);
    else if (tok && tok.mint && tok.mint !== chartMint) {
      chartMint = "";
      chartBars = null;
    }
  }

  function lastUsd(d) {
    const q = d.queue || [];
    for (let i = q.length - 1; i >= 0; i--) {
      if (q[i].role === "executor" && q[i].usd != null) return q[i].usd;
    }
    return null;
  }

  async function pull() {
    const r = await fetch("/api/tower", { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(String(r.status));
    const data = await r.json();
    paint(data);
  }

  function visible() {
    return document.body.dataset.desk === "tower" && !root.hidden;
  }

  function stop() {
    if (timer) { clearTimeout(timer); timer = null; }
  }

  function loop() {
    stop();
    if (!visible()) return;
    const started = Date.now();
    pull().catch(() => {
      const foot = root.querySelector("#twFoot");
      if (foot) foot.textContent = "Tower feed unavailable. The rest of the desk is unchanged.";
    }).finally(() => {
      if (!visible()) return;
      const took = Date.now() - started;
      const wait = took > 8000 ? 15000 : took > 4000 ? 6000 : 2000;
      timer = setTimeout(loop, wait);
    });
  }

  function show() {
    if (!root.dataset.ready) {
      shell();
      root.dataset.ready = "1";
    }
    root.hidden = false;
    if (!clock) clock = setInterval(() => { if (visible()) paintClock(); }, 1000);
    loop();
  }

  function hide() {
    root.hidden = true;
    stop();
  }

  document.addEventListener("desk-tab", (e) => {
    if (e.detail === "tower") show();
    else hide();
  });

  if (document.body.dataset.desk === "tower") show();
})();

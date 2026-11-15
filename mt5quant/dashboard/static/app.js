/* MT5 Quant AI dashboard. No dependencies; all dynamic text is inserted with
   textContent (never innerHTML) because labels come from data. */
(() => {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  const tooltip = $("tooltip");
  let token = sessionStorage.getItem("dashboardToken") || "";
  let tokenRequired = false;
  let range = "7d";
  let currency = "";
  let lastState = null;

  // ------------------------------------------------------------ formatting
  const nf = (d) => new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const money = (v, d = 2) => (v == null ? "—" : nf(d).format(v) + (currency ? " " + currency : ""));
  const signed = (v, d = 2) => (v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(d).format(Math.abs(v)));
  const pct = (v, d = 1) => (v == null ? "—" : nf(d).format(v) + "%");
  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e6) return nf(a >= 1e7 ? 0 : 1).format(v / 1e6) + "M";
    if (a >= 1e4) return nf(a >= 1e5 ? 0 : 1).format(v / 1e3) + "K";
    return nf(a < 100 && a % 1 ? 1 : 0).format(v);
  };
  const price = (v) => (v == null ? "—" : nf(v >= 500 ? 2 : v >= 20 ? 3 : 5).format(v));
  const time = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const svg = (tag, attrs = {}, cls) => {
    const e = document.createElementNS(SVG, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (cls) e.setAttribute("class", cls);
    return e;
  };
  const setDelta = (node, v, text) => {
    node.textContent = text;
    node.classList.toggle("up", v > 0);
    node.classList.toggle("down", v < 0);
  };

  // ------------------------------------------------------------------- api
  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {});
    if (token) headers["X-Dashboard-Token"] = token;
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      if (await askToken()) return api(path, opts);
      throw new Error("unauthorized");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "request failed (" + res.status + ")");
    return data;
  }

  function askToken() {
    return new Promise((resolve) => {
      const dlg = $("token-dialog");
      if (dlg.open) return resolve(false);
      $("token-input").value = "";
      dlg.showModal();
      dlg.addEventListener("close", function done() {
        dlg.removeEventListener("close", done);
        const v = $("token-input").value.trim();
        if (dlg.returnValue === "confirm" && v) {
          token = v;
          sessionStorage.setItem("dashboardToken", v);
          resolve(true);
        } else resolve(false);
      });
    });
  }

  // --------------------------------------------------------------- tooltip
  function showTip(evt, head, rows) {
    tooltip.replaceChildren();
    if (head) tooltip.append(el("div", "t-head", head));
    for (const r of rows) {
      const row = el("div", "t-row");
      if (r.key) row.append(el("span", "tkey " + r.key));
      row.append(el("b", null, r.value), el("span", "n", r.name));
      tooltip.append(row);
    }
    tooltip.hidden = false;
    const pad = 14, w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
    tooltip.style.left = Math.max(8, x) + "px";
    tooltip.style.top = Math.max(8, y) + "px";
  }
  const hideTip = () => { tooltip.hidden = true; };

  function niceTicks(lo, hi, n = 4) {
    if (!(hi > lo)) { hi = lo + 1; lo = lo - 1; }
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const start = Math.floor(lo / step) * step, out = [];
    // The top tick must reach the data maximum so no mark pokes above the plot.
    for (let v = start, i = 0; i < 50; v += step, i++) {
      out.push(+v.toFixed(10));
      if (v >= hi - step * 1e-9) break;
    }
    return out;
  }
  // Axis labels: compact only when the step itself is in thousands, so
  // neighbouring ticks never collapse into the same label.
  const axisFormat = (ticks) => {
    const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
    const top = Math.max(...ticks.map(Math.abs));
    if (top >= 10000 && step >= 1000) return compact;
    const d = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
    return (v) => nf(d).format(v);
  };

  function emptyChart(host, text) {
    host.replaceChildren(el("div", "empty", text));
  }

  // ---------------------------------------------------- line chart (equity)
  function lineChart(host, points) {
    if (!points || points.length < 2) {
      return emptyChart(host, "No equity history yet. The curve appears once the engine has been running.");
    }
    const W = host.clientWidth || 600, H = host.clientHeight || 280;
    const m = { l: 56, r: 64, t: 10, b: 24 };
    const xs = points.map((p) => Date.parse(p.t));
    const all = points.flatMap((p) => [p.equity, p.balance]);
    const ticks = niceTicks(Math.min(...all), Math.max(...all));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const x = (t) => m.l + ((t - xs[0]) / Math.max(xs[xs.length - 1] - xs[0], 1)) * (W - m.l - m.r);
    const y = (v) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
    const fmt = axisFormat(ticks);
    for (const tv of ticks) {
      root.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv) }, "gridline"));
      const t = svg("text", { x: m.l - 8, y: y(tv) + 4, "text-anchor": "end" }, "tick");
      t.textContent = fmt(tv);
      root.append(t);
    }
    root.append(svg("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }, "axisline"));
    const span = xs[xs.length - 1] - xs[0];
    const nx = W < 480 ? 2 : 4;
    for (let i = 0; i <= nx; i++) {
      const tv = xs[0] + (span * i) / nx, d = new Date(tv).toISOString();
      const t = svg("text", { x: x(tv), y: H - 6, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle" }, "tick");
      t.textContent = span > 3 * 864e5 ? d.slice(5, 10) : d.slice(11, 16);
      root.append(t);
    }
    const path = (key) => points.map((p, i) => (i ? "L" : "M") + x(xs[i]).toFixed(1) + " " + y(p[key]).toFixed(1)).join(" ");
    const area = path("equity") + ` L ${x(xs[xs.length - 1]).toFixed(1)} ${H - m.b} L ${x(xs[0]).toFixed(1)} ${H - m.b} Z`;
    root.append(svg("path", { d: area }, "area s1"));
    root.append(svg("path", { d: path("balance") }, "line s2"));
    root.append(svg("path", { d: path("equity") }, "line s1"));
    const last = points[points.length - 1], lx = x(xs[xs.length - 1]);
    // End labels: nudge apart only when the two series end within a text height.
    let ey = y(last.equity), by = y(last.balance);
    const close = Math.abs(ey - by) < 13;
    root.append(svg("circle", { cx: lx, cy: by, r: 4 }, "enddot s2"));
    root.append(svg("circle", { cx: lx, cy: ey, r: 4 }, "enddot s1"));
    const le = svg("text", { x: lx + 9, y: ey + 4 }, "endlabel");
    le.textContent = nf(0).format(last.equity);
    root.append(le);
    if (!close) {
      const lb = svg("text", { x: lx + 9, y: by + 4 }, "endlabel");
      lb.textContent = nf(0).format(last.balance);
      root.append(lb);
    }
    const cross = svg("line", { y1: m.t, y2: H - m.b, x1: 0, x2: 0, visibility: "hidden" }, "crosshair");
    const d1 = svg("circle", { r: 4, visibility: "hidden" }, "enddot s1");
    const d2 = svg("circle", { r: 4, visibility: "hidden" }, "enddot s2");
    const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b }, "hit");
    root.append(cross, d2, d1, hit);
    hit.addEventListener("pointermove", (evt) => {
      const box = root.getBoundingClientRect();
      const px = ((evt.clientX - box.left) / box.width) * W;
      const tv = xs[0] + ((px - m.l) / (W - m.l - m.r)) * (xs[xs.length - 1] - xs[0]);
      let i = 0, best = Infinity;
      for (let k = 0; k < xs.length; k++) { const dd = Math.abs(xs[k] - tv); if (dd < best) { best = dd; i = k; } }
      const p = points[i], cx = x(xs[i]);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
      d1.setAttribute("cx", cx); d1.setAttribute("cy", y(p.equity)); d1.setAttribute("visibility", "visible");
      d2.setAttribute("cx", cx); d2.setAttribute("cy", y(p.balance)); d2.setAttribute("visibility", "visible");
      showTip(evt, time(p.t) + " UTC", [
        { key: "k1", value: money(p.equity), name: "Equity" },
        { key: "k2", value: money(p.balance), name: "Balance" },
      ]);
    });
    hit.addEventListener("pointerleave", () => {
      [cross, d1, d2].forEach((n) => n.setAttribute("visibility", "hidden"));
      hideTip();
    });
    host.replaceChildren(root);
  }

  // ------------------------------------------------- column chart (daily P/L)
  function columnChart(host, days) {
    if (!days || !days.length) return emptyChart(host, "No closed trades in this range.");
    const W = host.clientWidth || 600, H = host.clientHeight || 200;
    const m = { l: 56, r: 12, t: 10, b: 24 };
    const vals = days.map((d) => d.pnl);
    const ticks = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals), 4);
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const y = (v) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);
    const band = (W - m.l - m.r) / days.length, bw = Math.min(24, Math.max(3, band - 2));
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
    for (const tv of ticks) {
      root.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv) }, tv === 0 ? "axisline" : "gridline"));
      const t = svg("text", { x: m.l - 8, y: y(tv) + 4, "text-anchor": "end" }, "tick");
      t.textContent = axisFormat(ticks)(tv);
      root.append(t);
    }
    const every = Math.ceil(days.length / (W < 480 ? 4 : 8));
    days.forEach((d, i) => {
      const cx = m.l + band * (i + 0.5), top = y(Math.max(d.pnl, 0)), h = Math.max(Math.abs(y(d.pnl) - y(0)), 1);
      const r = Math.min(4, bw / 2, h);
      const x0 = cx - bw / 2, x1 = cx + bw / 2, base = y(0);
      // Rounded at the data end, square at the baseline.
      const dpath = d.pnl >= 0
        ? `M${x0} ${base} V${top + r} Q${x0} ${top} ${x0 + r} ${top} H${x1 - r} Q${x1} ${top} ${x1} ${top + r} V${base} Z`
        : `M${x0} ${base} V${base + h - r} Q${x0} ${base + h} ${x0 + r} ${base + h} H${x1 - r} Q${x1} ${base + h} ${x1} ${base + h - r} V${base} Z`;
      const bar = svg("path", { d: dpath }, "bar " + (d.pnl >= 0 ? "pos" : "neg"));
      const hit = svg("rect", { x: m.l + band * i, y: m.t, width: band, height: H - m.t - m.b, tabindex: 0 }, "hit");
      const show = (evt) => {
        bar.classList.add("hot");
        showTip(evt, d.date, [{ key: d.pnl >= 0 ? "pos" : "neg", value: signed(d.pnl) + " " + currency, name: d.pnl >= 0 ? "Gain" : "Loss" }]);
      };
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", () => { bar.classList.remove("hot"); hideTip(); });
      root.append(bar, hit);
      if (i % every === 0) {
        const t = svg("text", { x: cx, y: H - 6, "text-anchor": "middle" }, "tick");
        t.textContent = d.date.slice(5);
        root.append(t);
      }
    });
    host.replaceChildren(root);
    const table = el("table");
    const head = el("tr");
    head.append(el("th", null, "Date"), el("th", "num", "Realised P/L"));
    table.append(head);
    days.slice().reverse().forEach((d) => {
      const tr = el("tr");
      const v = el("td", "num", signed(d.pnl));
      v.classList.add(d.pnl >= 0 ? "up" : "down");
      tr.append(el("td", null, d.date), v);
      table.append(tr);
    });
    $("daily-table").replaceChildren(table);
  }

  // --------------------------------------------- horizontal bars (strategies)
  function strategyBars(host, rows) {
    const data = rows.filter((r) => r.trades > 0);
    if (!data.length) return emptyChart(host, "No closed trades yet.");
    const W = host.clientWidth || 420, rowH = 30, m = { l: 132, r: 76, t: 4, b: 4 };
    const H = m.t + m.b + rowH * data.length;
    host.style.height = H + "px";
    const max = Math.max(...data.map((r) => Math.abs(r.net_profit)), 1e-9);
    const hasNeg = data.some((r) => r.net_profit < 0), hasPos = data.some((r) => r.net_profit > 0);
    const inner = W - m.l - m.r;
    const zero = m.l + (hasNeg ? (hasPos ? inner / 2 : inner) : 0);
    const scale = (hasNeg && hasPos ? inner / 2 : inner) / max;
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
    root.append(svg("line", { x1: zero, x2: zero, y1: m.t, y2: H - m.b }, "axisline"));
    data.forEach((r, i) => {
      const cy = m.t + rowH * (i + 0.5), w = Math.max(Math.abs(r.net_profit) * scale, 1), bh = 14, rad = Math.min(4, w);
      const pos = r.net_profit >= 0, y0 = cy - bh / 2, y1 = cy + bh / 2;
      const xe = pos ? zero + w : zero - w;
      const dpath = pos
        ? `M${zero} ${y0} H${xe - rad} Q${xe} ${y0} ${xe} ${y0 + rad} V${y1 - rad} Q${xe} ${y1} ${xe - rad} ${y1} H${zero} Z`
        : `M${zero} ${y0} H${xe + rad} Q${xe} ${y0} ${xe} ${y0 + rad} V${y1 - rad} Q${xe} ${y1} ${xe + rad} ${y1} H${zero} Z`;
      const bar = svg("path", { d: dpath }, "bar " + (pos ? "pos" : "neg"));
      const name = svg("text", { x: 0, y: cy + 4 }, "barlabel");
      name.textContent = r.name;
      const val = svg("text", { x: W, y: cy + 4, "text-anchor": "end" }, "barvalue");
      val.textContent = signed(r.net_profit);
      const hit = svg("rect", { x: 0, y: cy - rowH / 2, width: W, height: rowH }, "hit");
      hit.addEventListener("pointermove", (evt) => {
        bar.classList.add("hot");
        showTip(evt, r.name, [
          { key: pos ? "pos" : "neg", value: signed(r.net_profit) + " " + currency, name: "net P/L" },
          { value: String(r.trades), name: "trades" },
          { value: r.win_rate == null ? "—" : pct(r.win_rate * 100, 0), name: "win rate" },
          { value: r.expectancy_r == null ? "—" : signed(r.expectancy_r, 2) + "R", name: "expectancy" },
        ]);
      });
      hit.addEventListener("pointerleave", () => { bar.classList.remove("hot"); hideTip(); });
      root.append(bar, name, val, hit);
    });
    host.replaceChildren(root);
  }

  // ---------------------------------------------------------------- tables
  function table(node, columns, rows, emptyText) {
    const head = el("tr");
    columns.forEach((c) => head.append(el("th", c.num ? "num" : null, c.title)));
    const body = [head];
    if (!rows.length) {
      const tr = el("tr", "empty-row"), td = el("td", null, emptyText);
      td.colSpan = columns.length;
      tr.append(td);
      body.push(tr);
    }
    rows.forEach((r) => {
      const tr = el("tr");
      columns.forEach((c) => {
        const td = el("td", (c.num ? "num " : "") + (c.wrap ? "wrap" : ""));
        const out = c.render(r);
        if (out instanceof Node) td.append(out); else td.textContent = out;
        if (c.delta) { const v = c.delta(r); td.classList.toggle("up", v > 0); td.classList.toggle("down", v < 0); }
        tr.append(td);
      });
      body.push(tr);
    });
    node.replaceChildren(...body);
  }
  const tag = (text, kind) => el("span", "tag " + (kind || ""), text);
  const sideText = (s) => el("span", "side", s === "buy" ? "▲ BUY" : "▼ SELL");
  const STATUS_KIND = { validated: "good", rejected: "bad", suspended: "bad", untested: "warn", synthetic_only: "warn" };
  const HEALTH_KIND = { healthy: "good", watch: "warn", degraded: "bad", insufficient_data: "" };

  // ----------------------------------------------------------------- render
  function meter(label, value, limit, unit, note, digits = 2) {
    const ratio = limit > 0 ? Math.min(value / limit, 1) : 0;
    const box = el("div", "meter" + (ratio >= 1 ? " crit" : ratio >= 0.7 ? " warn" : ""));
    const head = el("div", "meter-head");
    head.append(el("span", null, label), el("b", null, `${nf(digits).format(value)}${unit} of ${nf(digits).format(limit)}${unit}`));
    const track = el("div", "meter-track"), fill = el("div", "meter-fill");
    fill.style.width = (ratio * 100).toFixed(1) + "%";
    track.append(fill);
    box.append(head, track);
    if (note) box.append(el("div", "meter-note", (ratio >= 1 ? "⛔ " : ratio >= 0.7 ? "⚠ " : "") + note));
    return box;
  }

  function banners(s) {
    const out = [];
    const add = (kind, title, text) => {
      const b = el("div", "banner " + kind);
      b.append(el("b", null, title), el("span", null, text));
      out.push(b);
    };
    const ks = s.risk.kill_switch || {};
    if (ks.active) add("critical", "⛔ EMERGENCY STOP ACTIVE", `${ks.reason || ""} (by ${ks.actor || "?"}). No new positions until resumed.`);
    if (s.risk.suspended) add("critical", "⛔ Trading suspended", s.risk.suspend_reason || "");
    if (s.risk.daily_halted) add("", "⚠ Daily loss limit reached", "No new positions until the next trading day.");
    if (s.risk.weekly_halted) add("", "⚠ Weekly loss limit reached", "No new positions until next week.");
    if (!s.health.alive) add("critical", "⛔ Engine not running", `Last status: ${s.health.status}. Positions keep their broker-side stops.`);
    if (s.connection.synthetic_data) add("info", "ℹ Synthetic data feed", "Prices are generated for demonstration. They are not market prices and results mean nothing for real trading.");
    if (s.mode === "live") add("critical", "● LIVE MODE", "Orders are sent to a real-money account.");
    if (!s.audit_ok) add("critical", "⛔ Audit log integrity failure", `Chain broken at row ${s.audit_bad_row}.`);
    $("banners").replaceChildren(...out);
  }

  function render(s) {
    lastState = s;
    const a = s.account, sm = s.summary, rk = s.risk;
    currency = a ? a.currency : currency;
    const mode = $("mode");
    mode.textContent = s.mode.toUpperCase();
    mode.className = "badge " + s.mode;
    const conn = $("conn");
    conn.className = "pill " + (s.connection.connected ? "ok" : s.health.alive ? "warn" : "bad");
    conn.querySelector(".txt").textContent = s.connection.connected
      ? `Connected · ${s.connection.broker || "broker"}` : s.health.alive ? `Engine ${s.health.status}` : "Engine offline";
    const mon = $("monitor");
    const blocked = s.can_open === false;
    mon.className = "pill " + (blocked ? "warn" : s.health.alive ? "ok" : "");
    mon.replaceChildren(el("span", "dot"), el("span", "txt", !s.health.alive ? "Not monitoring" : blocked ? "Entries blocked" : "Monitoring markets"));
    $("sessions").textContent = s.sessions && s.sessions.length ? "Sessions: " + s.sessions.join(", ") : "";
    $("clock").textContent = time(s.engine_time) + " UTC" + (s.health.age_seconds != null ? ` · heartbeat ${Math.round(s.health.age_seconds)}s ago` : "");
    banners(s);
    $("resume").hidden = !((rk.kill_switch || {}).active || rk.suspended);

    $("equity").textContent = a ? money(a.equity) : (sm.last_equity != null ? money(sm.last_equity) : "—");
    $("balance").textContent = a ? money(a.balance) : "—";
    $("margin-free").textContent = a ? money(a.margin_free) : "—";
    setDelta($("floating"), a ? a.profit : 0, a ? signed(a.profit) : "—");
    setDelta($("pnl-today"), sm.pnl_today, signed(sm.pnl_today));
    setDelta($("pnl-week"), sm.pnl_week, signed(sm.pnl_week));
    setDelta($("pnl-month"), sm.pnl_month, signed(sm.pnl_month));
    $("trades-today").textContent = `${sm.trades_today} trade${sm.trades_today === 1 ? "" : "s"} closed`;

    const L = rk.limits, ex = s.exposure;
    $("meters").replaceChildren(
      meter("Daily loss", rk.daily_loss_pct, L.max_daily_loss_pct, "%", "halts new entries for the day at the limit"),
      meter("Weekly loss", rk.weekly_loss_pct, L.max_weekly_loss_pct, "%", "halts new entries for the week at the limit"),
      meter("Drawdown from peak", rk.drawdown_pct, L.max_drawdown_pct, "%", "suspends trading at the limit (manual resume)"),
      meter("Open risk (all stops hit)", ex.open_risk_pct, ex.max_open_risk_pct, "%", `${signed(-ex.open_risk)} ${currency} at risk now`),
      meter("Open positions", ex.positions, ex.max_positions, "", `risk per trade ${L.risk_per_trade_pct}% of equity`, 0));
    $("risk-state").textContent = blocked ? "entries blocked" : "within limits";

    const pf = sm.profit_factor;
    const stats = [
      ["Total trades", String(sm.trades)], ["Win rate", sm.trades ? pct(sm.win_rate * 100) : "—"],
      ["Average win", sm.wins ? signed(sm.avg_win) : "—"], ["Average loss", sm.losses ? signed(sm.avg_loss) : "—"],
      ["Profit factor", pf == null ? (sm.wins ? "∞" : "—") : nf(2).format(pf)],
      ["Max drawdown", pct(sm.max_drawdown_pct, 2)],
      ["Expectancy", sm.trades ? signed(sm.expectancy_r, 2) + "R" : "—"], ["Net profit", signed(sm.net_profit)],
    ];
    $("stats").replaceChildren(...stats.map(([k, v]) => {
      const d = el("div", "stat");
      d.append(el("div", "label", k), el("div", "value", v));
      return d;
    }));

    $("pos-count").textContent = `${s.positions.length} open`;
    table($("positions"), [
      { title: "Symbol", render: (p) => p.symbol },
      { title: "Side", render: (p) => sideText(p.side) },
      { title: "Lots", num: 1, render: (p) => nf(2).format(p.volume) },
      { title: "Entry", num: 1, render: (p) => nf(p.digits).format(p.price_open) },
      { title: "Current", num: 1, render: (p) => nf(p.digits).format(p.price_current) },
      { title: "Stop", num: 1, render: (p) => (p.sl ? nf(p.digits).format(p.sl) : "NONE") },
      { title: "Target", num: 1, render: (p) => (p.tp ? nf(p.digits).format(p.tp) : "—") },
      { title: "Risk left", num: 1, render: (p) => (p.risk == null ? "—" : nf(2).format(p.risk)) },
      { title: "P/L", num: 1, render: (p) => signed(p.profit), delta: (p) => p.profit },
      { title: "Strategy", render: (p) => p.strategy },
      { title: "Opened (UTC)", render: (p) => time(p.time_open) },
    ], s.positions, "No open positions.");

    strategyBars($("strategy-chart"), s.strategies);
    table($("strategies"), [
      { title: "Strategy", render: (r) => r.name },
      { title: "Style", render: (r) => `${r.style} · ${r.timeframe}` },
      { title: "Validation", render: (r) => tag(r.status.replace("_", " "), STATUS_KIND[r.status] || "warn") },
      { title: "Live health", render: (r) => tag(r.health.replace("_", " "), HEALTH_KIND[r.health] || "") },
      { title: "Trades", num: 1, render: (r) => String(r.trades) },
      { title: "Win", num: 1, render: (r) => (r.trades ? pct(r.win_rate * 100, 0) : "—") },
      { title: "Exp.", num: 1, render: (r) => (r.trades ? signed(r.expectancy_r, 2) + "R" : "—") },
      { title: "PF", num: 1, render: (r) => (r.trades ? (r.profit_factor == null ? "∞" : nf(2).format(r.profit_factor)) : "—") },
    ], s.strategies, "No strategies enabled.");

    $("can-open").textContent = blocked ? (s.blocks || []).join("; ") : "";
    table($("markets"), [
      { title: "Symbol", render: (r) => r[0] },
      { title: "Status", wrap: 1, render: (r) => tag(r[1], r[1] === "monitoring" ? "good" : r[1].startsWith("data error") ? "bad" : "warn") },
    ], Object.entries(s.markets || {}), "The engine has not reported market status yet.");

    table($("news"), [
      { title: "Time (UTC)", render: (n) => time(n.time) },
      { title: "Ccy", render: (n) => n.currency },
      { title: "Event", wrap: 1, render: (n) => n.title },
      { title: "Impact", render: (n) => tag(n.impact, n.impact === "high" ? "bad" : "warn") },
      { title: "Source", render: (n) => n.source + (n.confirmed ? "" : " (unconfirmed)") },
    ], s.news, "No scheduled medium/high-impact events stored for the next 48 hours.");

    const notes = s.notifications.map((n) => {
      const d = el("details", "log-item"), sum = el("summary");
      sum.append(tag(n.level, n.level === "critical" ? "bad" : n.level === "warning" ? "warn" : "info"),
        el("span", null, n.title), el("span", "when", time(n.time)));
      d.append(sum, el("pre", null, n.body || ""));
      return d;
    });
    $("notifications").replaceChildren(...(notes.length ? notes : [el("div", "log-empty", "No notifications.")]));
  }

  async function refreshState() {
    try { render(await api("/api/state")); } catch (e) {
      const conn = $("conn");
      conn.className = "pill bad";
      conn.querySelector(".txt").textContent = "Dashboard API unreachable";
    }
  }

  async function refreshSlow() {
    try {
      const eq = await api("/api/equity?range=" + range);
      lineChart($("equity-chart"), eq.points);
      columnChart($("daily-chart"), eq.daily);
      const trades = await api("/api/trades?limit=50");
      table($("trades"), [
        { title: "Closed (UTC)", render: (t) => time(t.time_close) },
        { title: "Symbol", render: (t) => t.symbol },
        { title: "Side", render: (t) => sideText(t.side) },
        { title: "Lots", num: 1, render: (t) => nf(2).format(t.volume) },
        { title: "Entry", num: 1, render: (t) => price(t.price_open) },
        { title: "Exit", num: 1, render: (t) => price(t.price_close) },
        { title: "P/L", num: 1, render: (t) => signed(t.profit), delta: (t) => t.profit },
        { title: "R", num: 1, render: (t) => signed(t.r_multiple, 2), delta: (t) => t.r_multiple },
        { title: "Exit reason", render: (t) => t.exit_reason },
        { title: "Strategy", render: (t) => t.strategy },
      ], trades, "No closed trades yet.");
      const dec = await api("/api/decisions?limit=30");
      const items = dec.map((d) => {
        const box = el("details", "log-item"), sum = el("summary");
        sum.append(tag(d.action, d.action === "executed" ? "good" : "warn"),
          el("span", null, `${(d.side || "").toUpperCase()} ${d.symbol} · ${d.strategy}`),
          el("span", "hint", `confidence ${nf(2).format(d.confidence)} · ${d.regime.replace("_", " ")}`),
          el("span", "when", time(d.time)));
        box.append(sum, el("pre", null, d.explanation));
        return box;
      });
      $("decisions").replaceChildren(...(items.length ? items : [el("div", "log-empty", "No signals evaluated yet.")]));
    } catch (e) { /* state poll reports connectivity */ }
  }

  // --------------------------------------------------------------- controls
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("on"));
    chip.classList.add("on");
    range = chip.dataset.range;
    document.querySelectorAll(".chart").forEach((c) => { c.style.opacity = "0.5"; });
    refreshSlow().finally(() => document.querySelectorAll(".chart").forEach((c) => { c.style.opacity = ""; }));
  }));

  $("stop").addEventListener("click", () => {
    const dlg = $("stop-dialog");
    $("stop-reason").value = "";
    dlg.showModal();
  });
  $("stop-dialog").addEventListener("close", async () => {
    if ($("stop-dialog").returnValue !== "confirm") return;
    try {
      const reason = $("stop-reason").value.trim();
      const r = await api("/api/emergency-stop", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }) });
      alert(r.message);
    } catch (e) { alert("Emergency stop FAILED: " + e.message + "\nUse: python main.py stop"); }
    refreshState();
  });
  $("resume").addEventListener("click", async () => {
    if (!confirm("Resume trading? Review why it stopped first. The drawdown reference restarts from current equity.")) return;
    try {
      const r = await api("/api/resume", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }) });
      alert(r.message);
    } catch (e) { alert("Resume failed: " + e.message); }
    refreshState();
  });

  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(refreshSlow, 200); });

  (async () => {
    try { tokenRequired = (await (await fetch("/api/meta")).json()).token_required; } catch (e) { /* offline */ }
    if (tokenRequired && !token) await askToken();
    await refreshState();
    await refreshSlow();
    setInterval(refreshState, 3000);
    setInterval(refreshSlow, 15000);
  })();
})();

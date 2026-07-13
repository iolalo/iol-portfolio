const DATA_URL      = "data/portfolio.json";
const REFRESH_MS    = 10 * 60 * 1000;   // 10 min
const chartRegistry = {};               // symbol → Chart instance
let   activePeriod  = "30";
let   globalData    = null;

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmt(n, decimals = 0) {
  return new Intl.NumberFormat("es-AR", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

function pctClass(v) {
  if (v > 0)  return "pos";
  if (v < 0)  return "neg";
  return "neu";
}

function rsiClass(v) {
  if (v == null) return "";
  if (v > 70)    return "rsi-high";
  if (v < 30)    return "rsi-low";
  return "";
}

/** Convert sparkline/full_history array to {labels, closes} for the active period. */
function sliceHistory(pos) {
  const src = (activePeriod === "all" && pos.full_history?.length)
    ? pos.full_history
    : pos.sparkline;

  if (!src || src.length === 0) return null;

  let data = src;
  if (activePeriod === "30" && src.length > 30) data = src.slice(-30);
  if (activePeriod === "90" && src.length > 90) data = src.slice(-90);

  const labels = data.map(p => p.date || "");
  const closes = data.map(p => p.close);
  return { labels, closes };
}

// ── Render: summary cards ─────────────────────────────────────────────────────

function renderSummary(data) {
  document.getElementById("last-updated").textContent =
    `Última actualización: ${data.last_updated}`;

  document.getElementById("total-ars").textContent      = `$${fmt(data.total_ars)}`;
  document.getElementById("total-invested").textContent = `$${fmt(data.invested_ars ?? data.invested ?? 0)}`;
  document.getElementById("total-positions").textContent = data.total_positions ?? data.positions.length;
  document.getElementById("alert-count").textContent    = data.alert_count;
  document.getElementById("pending-orders").textContent = data.pending_orders_count ?? 0;

  const gain    = data.total_gain ?? 0;
  const gainPct = data.total_gain_pct ?? 0;
  const gainEl  = document.getElementById("total-gain");
  const gainPctEl = document.getElementById("total-gain-pct");
  const gainCard  = document.getElementById("gain-card");

  gainEl.textContent    = `${gain >= 0 ? "+" : ""}$${fmt(Math.abs(gain))}`;
  gainPctEl.textContent = `${gainPct >= 0 ? "+" : ""}${fmt(gainPct, 2)}%`;
  gainEl.className      = `value ${gain >= 0 ? "pos" : "neg"}`;
  gainCard.classList.toggle("card--gain-pos", gain >= 0);
  gainCard.classList.toggle("card--gain-neg", gain < 0);
}

function renderPendingOrders(pendingOrders) {
  const section = document.getElementById("pending-section");
  const body = document.getElementById("pending-body");
  const active = (pendingOrders || []).filter(o => ["pending", "executing"].includes(o.status));

  if (active.length === 0) {
    section.classList.add("hidden");
    body.innerHTML = "";
    return;
  }

  section.classList.remove("hidden");
  body.innerHTML = active.map(order => `
    <tr>
      <td>${(order.timestamp || "").slice(0, 16).replace("T", " ")}</td>
      <td><strong>${order.symbol}</strong></td>
      <td>${order.side === "buy" ? "COMPRA" : "VENTA"}</td>
      <td>${order.qty ?? "—"}</td>
      <td>$${fmt(order.limit_price ?? 0, 2)}</td>
      <td><span class="badge ALERTA">${order.status}</span></td>
      <td>${order.id ?? "—"}</td>
    </tr>
  `).join("");
}

// ── Render: alerts ────────────────────────────────────────────────────────────

function renderAlerts(positions) {
  const alerts    = positions.filter(p =>
    ["COMPRAR", "VENDER", "ALERTA"].includes(p.recommendation)
  );
  const section   = document.getElementById("alerts-section");
  const container = document.getElementById("alerts-container");

  if (alerts.length === 0) { section.classList.add("hidden"); return; }
  section.classList.remove("hidden");

  container.innerHTML = alerts.map(a => `
    <div class="alert-card ${a.recommendation}">
      <h3>${a.symbol}</h3>
      <div class="rec ${a.recommendation}">${a.recommendation}</div>
      <div>
        Precio: $${fmt(a.unit_price)}
        | Día: <span class="${pctClass(a.daily_change_pct)}">${a.daily_change_pct > 0 ? "+" : ""}${fmt(a.daily_change_pct, 2)}%</span>
      </div>
      ${a.signals.length ? `<ul>${a.signals.map(s => `<li>${s}</li>`).join("")}</ul>` : ""}
    </div>
  `).join("");
}

// ── Render: positions table ───────────────────────────────────────────────────

function sparklineHTML(data, id) {
  if (!data || data.length < 2) return "—";
  return `<canvas id="spark-${id}" width="80" height="32" style="vertical-align:middle"></canvas>`;
}

function renderTable(positions) {
  const tbody = document.getElementById("positions-body");
  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12" class="loading">Sin posiciones. Ejecutá el workflow en GitHub Actions.</td></tr>`;
    return;
  }

  tbody.innerHTML = positions.map(p => {
    const rsiVal = p.rsi  != null ? fmt(p.rsi, 1)      : "—";
    const ma20Val = p.ma20 != null ? `$${fmt(p.ma20)}` : "—";
    const gpSign  = p.gain_pct > 0 ? "+" : "";
    return `
      <tr>
        <td><strong>${p.symbol}</strong></td>
        <td>${p.description}</td>
        <td>${fmt(p.quantity)}</td>
        <td>$${fmt(p.unit_price)}</td>
        <td>$${fmt(p.total_value)}</td>
        <td class="${pctClass(p.daily_change_pct)}">${p.daily_change_pct > 0 ? "+" : ""}${fmt(p.daily_change_pct, 2)}%</td>
        <td>$${fmt(p.ppc)}</td>
        <td class="${pctClass(p.gain_pct)}">${gpSign}${fmt(p.gain_pct, 2)}%</td>
        <td>${ma20Val}</td>
        <td class="${rsiClass(p.rsi)}">${rsiVal}</td>
        <td>${sparklineHTML(p.sparkline, p.symbol)}</td>
        <td><span class="badge ${p.recommendation}">${p.recommendation}</span></td>
      </tr>`;
  }).join("");
}

function drawSparklines(positions) {
  positions.forEach(p => {
    const canvas = document.getElementById(`spark-${p.symbol}`);
    if (!canvas || !p.sparkline || p.sparkline.length < 2) return;
    const closes = p.sparkline.map(d => d.close);
    const color  = closes[closes.length - 1] >= closes[0] ? "#22c55e" : "#ef4444";
    new Chart(canvas, {
      type: "line",
      data: {
        labels: closes.map((_, i) => i),
        datasets: [{ data: closes, borderColor: color, borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3 }],
      },
      options: {
        animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
        responsive: false,
      },
    });
  });
}

// ── Render: main charts (portfolio) ──────────────────────────────────────────

function destroyChart(id) {
  if (chartRegistry[id]) {
    chartRegistry[id].destroy();
    delete chartRegistry[id];
  }
}

function buildChart(canvas, p, sliced) {
  const { labels, closes } = sliced;
  const color  = closes[closes.length - 1] >= closes[0] ? "#22c55e" : "#ef4444";
  const datasets = [
    {
      label: p.symbol,
      data: closes,
      borderColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      fill: { target: "origin", above: color + "22" },
      tension: 0.3,
    },
  ];

  // MA20 line
  if (p.ma20 != null) {
    datasets.push({
      label: "MA20",
      data: Array(closes.length).fill(p.ma20),
      borderColor: "#3b82f6",
      borderWidth: 1,
      borderDash: [4, 4],
      pointRadius: 0,
      fill: false,
    });
  }

  // PPC reference line (only for portfolio positions)
  if (p.ppc != null && p.ppc !== p.unit_price) {
    datasets.push({
      label: "PPC",
      data: Array(closes.length).fill(p.ppc),
      borderColor: "#f59e0b",
      borderWidth: 1,
      borderDash: [6, 3],
      pointRadius: 0,
      fill: false,
    });
  }

  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: datasets.length > 1,
          labels: { color: "#8892a4", font: { size: 11 }, boxWidth: 20 },
        },
        tooltip: {
          callbacks: {
            title: ctx => ctx[0].label,
            label: ctx => ` ${ctx.dataset.label}: $${fmt(ctx.parsed.y)}`,
          },
        },
      },
      scales: {
        x: {
          display: true,
          ticks: {
            color: "#8892a4",
            font: { size: 10 },
            maxTicksLimit: 6,
            maxRotation: 0,
          },
          grid: { color: "#2a2d3a" },
        },
        y: {
          display: true,
          ticks: { color: "#8892a4", font: { size: 10 }, callback: v => `$${fmt(v)}` },
          grid: { color: "#2a2d3a" },
        },
      },
    },
  });
}

function renderCharts(positions) {
  const grid = document.getElementById("charts-grid");
  const validPositions = positions.filter(p => {
    const s = sliceHistory(p);
    return s && s.closes.length >= 2;
  });

  if (validPositions.length === 0) {
    grid.innerHTML = `<p class="loading">Sin datos históricos disponibles.</p>`;
    return;
  }

  // Build HTML cards (only for positions without existing canvases)
  validPositions.forEach(p => {
    const cardId   = `chart-card-${p.symbol}`;
    const canvasId = `chart-${p.symbol}`;

    if (!document.getElementById(cardId)) {
      const sliced   = sliceHistory(p);
      const changePct = sliced
        ? ((sliced.closes[sliced.closes.length - 1] - sliced.closes[0]) / sliced.closes[0] * 100).toFixed(2)
        : 0;
      const color    = parseFloat(changePct) >= 0 ? "#22c55e" : "#ef4444";
      const div      = document.createElement("div");
      div.className  = "chart-card";
      div.id         = cardId;
      div.innerHTML  = `
        <div class="chart-card-header">
          <div>
            <h3>${p.symbol} <span class="chart-desc">${p.description}</span></h3>
            <div class="chart-meta">
              $${fmt(p.unit_price)}
              · <span style="color:${color}">${changePct > 0 ? "+" : ""}${changePct}%</span>
              ${p.ma20  ? ` · MA20: $${fmt(p.ma20)}`                : ""}
              ${p.rsi   ? ` · RSI: <span class="${rsiClass(p.rsi)}">${fmt(p.rsi, 1)}</span>` : ""}
              ${p.ppc   ? ` · PPC: $${fmt(p.ppc)}`                 : ""}
            </div>
          </div>
          <span class="badge ${p.recommendation}">${p.recommendation}</span>
        </div>
        <canvas id="${canvasId}" height="100"></canvas>`;
      grid.appendChild(div);
    }

    // Destroy old chart and redraw with current period
    destroyChart(p.symbol);
    const canvas = document.getElementById(canvasId);
    if (canvas) {
      const sliced = sliceHistory(p);
      if (sliced) chartRegistry[p.symbol] = buildChart(canvas, p, sliced);
    }
  });
}

// ── Render: watchlist ─────────────────────────────────────────────────────────

function renderWatchlist(watchlist) {
  const section = document.getElementById("watchlist-section");
  if (!watchlist || watchlist.length === 0) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");

  const query = document.getElementById("ticker-search")?.value.toUpperCase().trim() || "";
  const filtered = query
    ? watchlist.filter(w => w.symbol.includes(query) || w.description.toUpperCase().includes(query))
    : watchlist;

  const grid = document.getElementById("watchlist-grid");
  grid.innerHTML = "";

  if (filtered.length === 0) {
    grid.innerHTML = `<p class="loading">Sin resultados para "${query}".</p>`;
    return;
  }

  filtered.forEach(w => {
    const sliced = sliceHistory(w);
    if (!sliced || sliced.closes.length < 2) return;

    const changePct = ((sliced.closes[sliced.closes.length - 1] - sliced.closes[0]) / sliced.closes[0] * 100).toFixed(2);
    const color = parseFloat(changePct) >= 0 ? "#22c55e" : "#ef4444";
    const canvasId = `wl-chart-${w.symbol}`;

    const div = document.createElement("div");
    div.className = "chart-card";
    div.innerHTML = `
      <div class="chart-card-header">
        <div>
          <h3>${w.symbol}</h3>
          <div class="chart-meta">
            $${fmt(w.unit_price)}
            · Día: <span class="${pctClass(w.daily_change_pct)}">${w.daily_change_pct > 0 ? "+" : ""}${fmt(w.daily_change_pct, 2)}%</span>
            · <span style="color:${color}">${changePct > 0 ? "+" : ""}${changePct}% período</span>
            ${w.ma20 ? ` · MA20: $${fmt(w.ma20)}`                    : ""}
            ${w.rsi  ? ` · RSI: <span class="${rsiClass(w.rsi)}">${fmt(w.rsi, 1)}</span>` : ""}
          </div>
        </div>
        <span class="badge ${w.recommendation}">${w.recommendation}</span>
      </div>
      <canvas id="${canvasId}" height="100"></canvas>`;
    grid.appendChild(div);

    destroyChart("wl-" + w.symbol);
    const canvas = document.getElementById(canvasId);
    if (canvas) {
      chartRegistry["wl-" + w.symbol] = buildChart(canvas, w, sliced);
    }
  });
}

// ── Countdown timer ───────────────────────────────────────────────────────────

let countdownSecs = REFRESH_MS / 1000;

function startCountdown() {
  countdownSecs = REFRESH_MS / 1000;
  const el = document.getElementById("countdown");
  const tick = () => {
    const m = Math.floor(countdownSecs / 60);
    const s = String(countdownSecs % 60).padStart(2, "0");
    if (el) el.textContent = `${m}:${s}`;
    if (countdownSecs > 0) { countdownSecs--; setTimeout(tick, 1000); }
  };
  tick();
}

// ── Period toggle ─────────────────────────────────────────────────────────────

function setupPeriodTabs() {
  document.getElementById("period-tabs")?.addEventListener("click", e => {
    const btn = e.target.closest(".period-btn");
    if (!btn) return;
    document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    activePeriod = btn.dataset.period;
    if (globalData) {
      document.getElementById("charts-grid").innerHTML = "";
      Object.keys(chartRegistry).forEach(k => { chartRegistry[k].destroy(); delete chartRegistry[k]; });
      renderCharts(globalData.positions);
      renderWatchlist(globalData.watchlist);
    }
  });
}

// ── Watchlist search ──────────────────────────────────────────────────────────

function setupSearch() {
  document.getElementById("ticker-search")?.addEventListener("input", () => {
    if (globalData) renderWatchlist(globalData.watchlist);
  });
}

// ── Init & refresh loop ───────────────────────────────────────────────────────

async function loadAndRender() {
  try {
    const resp = await fetch(`${DATA_URL}?t=${Date.now()}`);
    if (!resp.ok) throw new Error("No se pudo cargar portfolio.json");
    globalData = await resp.json();

    renderSummary(globalData);
    renderAlerts(globalData.positions);
    renderTable(globalData.positions);
    drawSparklines(globalData.positions);
    renderPendingOrders(globalData.pending_orders ?? []);

    // Clear existing charts before re-render
    document.getElementById("charts-grid").innerHTML = "";
    Object.keys(chartRegistry).forEach(k => { chartRegistry[k].destroy(); delete chartRegistry[k]; });

    renderCharts(globalData.positions);
    renderWatchlist(globalData.watchlist ?? []);
  } catch (err) {
    document.getElementById("positions-body").innerHTML =
      `<tr><td colspan="12" class="loading">Error al cargar datos: ${err.message}</td></tr>`;
  }
}

// ── Trades log ────────────────────────────────────────────────────────────────

async function loadAndRenderTrades() {
  try {
    const resp = await fetch(`data/trades_log.json?t=${Date.now()}`);
    if (!resp.ok) return;
    const raw = await resp.json();
    const log = Array.isArray(raw) ? raw : (raw.trades ?? []);
    if (log.length === 0) return;

    document.getElementById("trades-section").classList.remove("hidden");

    const today      = new Date().toISOString().slice(0, 10);
    const todayReal  = log.filter(t => (t.date || t.timestamp || "").startsWith(today) && t.status === "executed").length;
    const todaySim   = log.filter(t => (t.date || t.timestamp || "").startsWith(today) && t.status === "dry_run").length;
    const botEl      = document.getElementById("bot-ops");
    if (botEl) {
      if (todayReal > 0) botEl.textContent = `${todayReal} op${todayReal !== 1 ? "s" : ""}`;
      else if (todaySim > 0) botEl.textContent = `${todaySim} sim`;
      else botEl.textContent = "0 ops";
    }

    document.getElementById("trades-body").innerHTML = [...log].reverse().slice(0, 50).map(t => {
      // normalize old format (action/timestamp) to new format (side/date)
      const side      = t.side || (t.action === "compra" ? "buy" : "sell");
      const dateStr   = t.date || t.timestamp || "";
      const price     = t.price ?? 0;
      const limitPrice = t.limit_price ?? t.price ?? 0;
      const status    = t.status || (t.dry_run ? "dry_run" : (t.error ? "failed" : "executed"));

      const sideLabel  = side === "buy" ? "COMPRA" : "VENTA";
      const sideClass  = side === "buy" ? "pos" : "neg";
      const statusBadge = status === "executed"
        ? `<span class="badge COMPRAR">OK</span>`
        : status === "dry_run"
          ? `<span class="badge MANTENER">SIMULACIÓN</span>`
          : `<span class="badge ALERTA">FALLO</span>`;
      return `
        <tr>
          <td>${dateStr.slice(0, 16).replace("T", " ")}</td>
          <td><strong>${t.symbol}</strong></td>
          <td class="${sideClass}"><strong>${sideLabel}</strong></td>
          <td>${t.reason || "—"}</td>
          <td>${t.quantity}</td>
          <td>$${fmt(price)}</td>
          <td>$${fmt(limitPrice, 2)}</td>
          <td>${statusBadge}</td>
        </tr>`;
    }).join("");
  } catch (_) {}
}

async function init() {
  setupPeriodTabs();
  setupSearch();
  await loadAndRender();
  await loadAndRenderTrades();
  startCountdown();

  setInterval(async () => {
    await loadAndRender();
    await loadAndRenderTrades();
    startCountdown();
  }, REFRESH_MS);
}

init();

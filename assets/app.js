const DATA_URL = "data/portfolio.json";
const REFRESH_MS = 10 * 60 * 1000;
const chartRegistry = {};
let activePeriod = "30";
let globalData = null;

function fmt(n, decimals = 0) {
  return new Intl.NumberFormat("es-AR", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n ?? 0);
}

function pctClass(v) {
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "neu";
}

function rsiClass(v) {
  if (v == null) return "";
  if (v > 70) return "rsi-high";
  if (v < 30) return "rsi-low";
  return "";
}

function liquidationLabel(liq) {
  return {
    inmediato: "Contado inmediato",
    hrs24: "Disponible T+1",
    hrs48: "Disponible T+2",
    hrs72: "Disponible T+3",
    masHrs72: "Disponible > T+3",
  }[liq] ?? liq ?? "";
}

function sliceHistory(pos) {
  const src = activePeriod === "all" && pos.full_history?.length
    ? pos.full_history
    : pos.sparkline;

  if (!src || src.length === 0) return null;

  let data = src;
  if (activePeriod === "30" && src.length > 30) data = src.slice(-30);
  if (activePeriod === "90" && src.length > 90) data = src.slice(-90);

  return {
    labels: data.map((p) => p.date || ""),
    closes: data.map((p) => p.close),
  };
}

function renderSummary(data) {
  document.getElementById("last-updated").textContent =
    `Última actualización: ${data.last_updated}`;

  document.getElementById("total-ars").textContent = `$${fmt(data.total_ars)}`;
  document.getElementById("total-invested").textContent = `$${fmt(data.invested_ars ?? data.invested ?? 0)}`;
  document.getElementById("total-positions").textContent = data.total_positions ?? data.positions.length;
  document.getElementById("alert-count").textContent = data.alert_count ?? 0;
  document.getElementById("cash-available").textContent = `$${fmt(data.cash_available ?? 0)}`;
  document.getElementById("cash-liquidation").textContent = liquidationLabel(data.cash_liquidation);

  const gain = data.total_gain ?? 0;
  const gainPct = data.total_gain_pct ?? 0;
  const gainEl = document.getElementById("total-gain");
  const gainPctEl = document.getElementById("total-gain-pct");
  const gainCard = document.getElementById("gain-card");

  gainEl.textContent = `${gain >= 0 ? "+" : "-"}$${fmt(Math.abs(gain))}`;
  gainPctEl.textContent = `${gainPct >= 0 ? "+" : ""}${fmt(gainPct, 2)}%`;
  gainEl.className = `value ${gain >= 0 ? "pos" : "neg"}`;
  gainCard.classList.toggle("card--gain-pos", gain >= 0);
  gainCard.classList.toggle("card--gain-neg", gain < 0);
}

function renderAlerts(positions) {
  const alerts = positions.filter((p) => ["COMPRAR", "VENDER", "ALERTA"].includes(p.recommendation));
  const section = document.getElementById("alerts-section");
  const container = document.getElementById("alerts-container");

  if (alerts.length === 0) {
    section.classList.add("hidden");
    container.innerHTML = "";
    return;
  }

  section.classList.remove("hidden");
  container.innerHTML = alerts.map((a) => `
    <div class="alert-card ${a.recommendation}">
      <h3>${a.symbol}</h3>
      <div class="rec ${a.recommendation}">${a.recommendation}</div>
      <div>
        Precio: $${fmt(a.unit_price)}
        | Día: <span class="${pctClass(a.daily_change_pct)}">${a.daily_change_pct > 0 ? "+" : ""}${fmt(a.daily_change_pct, 2)}%</span>
      </div>
      ${a.signals?.length ? `<ul>${a.signals.map((s) => `<li>${s}</li>`).join("")}</ul>` : ""}
    </div>
  `).join("");
}

function focusTone(recommendation) {
  if (recommendation === "COMPRAR") return "buy";
  if (recommendation === "VENDER") return "sell";
  if (recommendation === "ALERTA") return "alert";
  return "hold";
}

function renderFocus(data) {
  const section = document.getElementById("focus-section");
  const grid = document.getElementById("focus-grid");
  const positions = data.positions ?? [];
  const active = positions
    .filter((p) => ["COMPRAR", "VENDER", "ALERTA"].includes(p.recommendation))
    .sort((a, b) => {
      const order = { VENDER: 0, ALERTA: 1, COMPRAR: 2 };
      return (order[a.recommendation] ?? 9) - (order[b.recommendation] ?? 9);
    })
    .slice(0, 4);

  const cards = [];

  if (active.length > 0) {
    active.forEach((item) => {
      cards.push(`
        <article class="focus-card focus-card--${focusTone(item.recommendation)}">
          <div class="focus-card__eyebrow">${item.recommendation}</div>
          <h3>${item.symbol}</h3>
          <div>Precio: $${fmt(item.unit_price)} | PPC: $${fmt(item.ppc)}</div>
          <div class="${pctClass(item.gain_pct)}">Resultado: ${item.gain_pct > 0 ? "+" : ""}${fmt(item.gain_pct, 2)}%</div>
          <div class="focus-card__meta">${(item.signals ?? []).slice(0, 2).join(" · ") || "Sin detalle adicional"}</div>
        </article>
      `);
    });
  } else {
    cards.push(`
      <article class="focus-card focus-card--hold">
        <div class="focus-card__eyebrow">Sin urgencias</div>
        <h3>Cartera estable</h3>
        <div>No hay señales activas de compra o venta en este momento.</div>
        <div class="focus-card__meta">Revisar de nuevo tras la próxima actualización del portfolio.</div>
      </article>
    `);
  }

  cards.push(`
    <article class="focus-card focus-card--hold">
      <div class="focus-card__eyebrow">Caja disponible</div>
      <h3>$${fmt(data.cash_available ?? 0)}</h3>
      <div>${liquidationLabel(data.cash_liquidation)}</div>
      <div class="focus-card__meta">Referencia rápida para decidir compras manuales.</div>
    </article>
  `);

  section.classList.remove("hidden");
  grid.innerHTML = cards.join("");
}

function sparklineHTML(data, id) {
  if (!data || data.length < 2) return "—";
  return `<canvas id="spark-${id}" width="80" height="32" style="vertical-align:middle"></canvas>`;
}

function renderTable(positions) {
  const tbody = document.getElementById("positions-body");
  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12" class="loading">Sin posiciones disponibles.</td></tr>`;
    return;
  }

  tbody.innerHTML = positions.map((p) => {
    const rsiVal = p.rsi != null ? fmt(p.rsi, 1) : "—";
    const ma20Val = p.ma20 != null ? `$${fmt(p.ma20)}` : "—";
    const gpSign = p.gain_pct > 0 ? "+" : "";
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
  positions.forEach((p) => {
    const canvas = document.getElementById(`spark-${p.symbol}`);
    if (!canvas || !p.sparkline || p.sparkline.length < 2) return;
    const closes = p.sparkline.map((d) => d.close);
    const color = closes[closes.length - 1] >= closes[0] ? "#22c55e" : "#ef4444";
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

function destroyChart(id) {
  if (chartRegistry[id]) {
    chartRegistry[id].destroy();
    delete chartRegistry[id];
  }
}

function buildChart(canvas, p, sliced) {
  const { labels, closes } = sliced;
  const color = closes[closes.length - 1] >= closes[0] ? "#22c55e" : "#ef4444";
  const datasets = [{
    label: p.symbol,
    data: closes,
    borderColor: color,
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 4,
    fill: { target: "origin", above: `${color}22` },
    tension: 0.3,
  }];

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
            title: (ctx) => ctx[0].label,
            label: (ctx) => ` ${ctx.dataset.label}: $${fmt(ctx.parsed.y)}`,
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
          ticks: { color: "#8892a4", font: { size: 10 }, callback: (v) => `$${fmt(v)}` },
          grid: { color: "#2a2d3a" },
        },
      },
    },
  });
}

function renderCharts(positions) {
  const grid = document.getElementById("charts-grid");
  const validPositions = positions.filter((p) => {
    const s = sliceHistory(p);
    return s && s.closes.length >= 2;
  });

  if (validPositions.length === 0) {
    grid.innerHTML = `<p class="loading">Sin datos históricos disponibles.</p>`;
    return;
  }

  validPositions.forEach((p) => {
    const cardId = `chart-card-${p.symbol}`;
    const canvasId = `chart-${p.symbol}`;

    if (!document.getElementById(cardId)) {
      const sliced = sliceHistory(p);
      const changePct = sliced
        ? ((sliced.closes[sliced.closes.length - 1] - sliced.closes[0]) / sliced.closes[0] * 100).toFixed(2)
        : 0;
      const color = parseFloat(changePct) >= 0 ? "#22c55e" : "#ef4444";
      const div = document.createElement("div");
      div.className = "chart-card";
      div.id = cardId;
      div.innerHTML = `
        <div class="chart-card-header">
          <div>
            <h3>${p.symbol} <span class="chart-desc">${p.description}</span></h3>
            <div class="chart-meta">
              $${fmt(p.unit_price)}
              · <span style="color:${color}">${changePct > 0 ? "+" : ""}${changePct}%</span>
              ${p.ma20 ? ` · MA20: $${fmt(p.ma20)}` : ""}
              ${p.rsi ? ` · RSI: <span class="${rsiClass(p.rsi)}">${fmt(p.rsi, 1)}</span>` : ""}
              ${p.ppc ? ` · PPC: $${fmt(p.ppc)}` : ""}
            </div>
          </div>
          <span class="badge ${p.recommendation}">${p.recommendation}</span>
        </div>
        <canvas id="${canvasId}" height="100"></canvas>`;
      grid.appendChild(div);
    }

    destroyChart(p.symbol);
    const canvas = document.getElementById(canvasId);
    if (canvas) {
      const sliced = sliceHistory(p);
      if (sliced) chartRegistry[p.symbol] = buildChart(canvas, p, sliced);
    }
  });
}

let countdownSecs = REFRESH_MS / 1000;

function startCountdown() {
  countdownSecs = REFRESH_MS / 1000;
  const el = document.getElementById("countdown");
  const tick = () => {
    const m = Math.floor(countdownSecs / 60);
    const s = String(countdownSecs % 60).padStart(2, "0");
    if (el) el.textContent = `${m}:${s}`;
    if (countdownSecs > 0) {
      countdownSecs--;
      setTimeout(tick, 1000);
    }
  };
  tick();
}

function setupPeriodTabs() {
  document.getElementById("period-tabs")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".period-btn");
    if (!btn) return;
    document.querySelectorAll(".period-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activePeriod = btn.dataset.period;
    if (globalData) {
      document.getElementById("charts-grid").innerHTML = "";
      Object.keys(chartRegistry).forEach((k) => {
        chartRegistry[k].destroy();
        delete chartRegistry[k];
      });
      renderCharts(globalData.positions);
    }
  });
}

async function loadAndRender() {
  try {
    const resp = await fetch(`${DATA_URL}?t=${Date.now()}`);
    if (!resp.ok) throw new Error("No se pudo cargar portfolio.json");
    globalData = await resp.json();

    renderSummary(globalData);
    renderAlerts(globalData.positions ?? []);
    renderFocus(globalData);
    renderTable(globalData.positions ?? []);
    drawSparklines(globalData.positions ?? []);

    document.getElementById("charts-grid").innerHTML = "";
    Object.keys(chartRegistry).forEach((k) => {
      chartRegistry[k].destroy();
      delete chartRegistry[k];
    });

    renderCharts(globalData.positions ?? []);
  } catch (err) {
    document.getElementById("positions-body").innerHTML =
      `<tr><td colspan="12" class="loading">Error al cargar datos: ${err.message}</td></tr>`;
  }
}

async function loadAndRenderTrades() {
  try {
    const resp = await fetch(`data/trades_log.json?t=${Date.now()}`);
    if (!resp.ok) return;
    const raw = await resp.json();
    const log = (Array.isArray(raw) ? raw : (raw.trades ?? []))
      .filter((t) => (t.status || "executed") === "executed");

    const section = document.getElementById("trades-section");
    const body = document.getElementById("trades-body");
    const today = new Date().toISOString().slice(0, 10);
    const todayReal = log.filter((t) => (t.date || t.timestamp || "").startsWith(today)).length;
    const detailEl = document.getElementById("manual-detail");

    if (detailEl) {
      detailEl.textContent = todayReal > 0
        ? `${todayReal} operación${todayReal !== 1 ? "es" : ""} real${todayReal !== 1 ? "es" : ""} hoy`
        : "Sin operaciones reales hoy";
    }

    section.classList.remove("hidden");
    if (log.length === 0) {
      body.innerHTML = `
        <tr>
          <td colspan="8" class="loading">No hay operaciones reales registradas todavía.</td>
        </tr>`;
      return;
    }

    body.innerHTML = [...log].reverse().slice(0, 50).map((t) => {
      const side = t.side || (t.action === "compra" ? "buy" : "sell");
      const dateStr = t.date || t.timestamp || "";
      const price = t.price ?? 0;
      const limitPrice = t.limit_price ?? t.price ?? 0;
      const sideLabel = side === "buy" ? "COMPRA" : "VENTA";
      const sideClass = side === "buy" ? "pos" : "neg";

      return `
        <tr>
          <td>${dateStr.slice(0, 16).replace("T", " ")}</td>
          <td><strong>${t.symbol}</strong></td>
          <td class="${sideClass}"><strong>${sideLabel}</strong></td>
          <td>${t.reason || "—"}</td>
          <td>${t.quantity}</td>
          <td>$${fmt(price)}</td>
          <td>$${fmt(limitPrice, 2)}</td>
          <td><span class="badge COMPRAR">REAL</span></td>
        </tr>`;
    }).join("");
  } catch (_) {}
}

async function init() {
  setupPeriodTabs();
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

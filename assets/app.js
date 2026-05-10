const DATA_URL = "data/portfolio.json";

async function loadData() {
  const resp = await fetch(`${DATA_URL}?t=${Date.now()}`);
  if (!resp.ok) throw new Error("No se pudo cargar portfolio.json");
  return resp.json();
}

function fmt(n, decimals = 0) {
  return new Intl.NumberFormat("es-AR", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n);
}

function pctClass(v) {
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "neu";
}

function rsiClass(v) {
  if (v === null || v === undefined) return "";
  if (v > 70) return "rsi-high";
  if (v < 30) return "rsi-low";
  return "";
}

function renderSummary(data) {
  document.getElementById("last-updated").textContent =
    `Última actualización: ${data.last_updated}`;
  document.getElementById("total-ars").textContent =
    `$${fmt(data.total_ars)}`;
  document.getElementById("total-positions").textContent =
    data.positions.length;
  document.getElementById("alert-count").textContent =
    data.alert_count;
}

function renderAlerts(positions) {
  const alerts = positions.filter(p =>
    ["COMPRAR", "VENDER", "ALERTA"].includes(p.recommendation)
  );
  const section = document.getElementById("alerts-section");
  const container = document.getElementById("alerts-container");

  if (alerts.length === 0) {
    section.classList.add("hidden");
    return;
  }

  section.classList.remove("hidden");
  container.innerHTML = alerts.map(a => `
    <div class="alert-card ${a.recommendation}">
      <h3>${a.symbol}</h3>
      <div class="rec ${a.recommendation}">${a.recommendation}</div>
      <div>Precio: $${fmt(a.unit_price)} | Día: <span class="${pctClass(a.daily_change_pct)}">${a.daily_change_pct > 0 ? "+" : ""}${fmt(a.daily_change_pct, 2)}%</span></div>
      ${a.signals.length ? `<ul>${a.signals.map(s => `<li>${s}</li>`).join("")}</ul>` : ""}
    </div>
  `).join("");
}

function renderTable(positions) {
  const tbody = document.getElementById("positions-body");
  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12" class="loading">Sin posiciones. Ejecutá el workflow en GitHub Actions.</td></tr>`;
    return;
  }

  tbody.innerHTML = positions.map(p => {
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
      </tr>
    `;
  }).join("");
}

function sparklineHTML(data, id) {
  if (!data || data.length < 2) return "—";
  return `<canvas id="spark-${id}" width="80" height="32" style="vertical-align:middle"></canvas>`;
}

function drawSparklines(positions) {
  positions.forEach(p => {
    const canvas = document.getElementById(`spark-${p.symbol}`);
    if (!canvas || !p.sparkline || p.sparkline.length < 2) return;
    const first = p.sparkline[0];
    const last = p.sparkline[p.sparkline.length - 1];
    const color = last >= first ? "#22c55e" : "#ef4444";
    new Chart(canvas, {
      type: "line",
      data: {
        labels: p.sparkline.map((_, i) => i),
        datasets: [{
          data: p.sparkline,
          borderColor: color,
          borderWidth: 1.5,
          pointRadius: 0,
          fill: false,
          tension: 0.3,
        }],
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

function renderCharts(positions) {
  const grid = document.getElementById("charts-grid");
  grid.innerHTML = positions
    .filter(p => p.sparkline && p.sparkline.length >= 2)
    .map(p => {
      const first = p.sparkline[0];
      const last = p.sparkline[p.sparkline.length - 1];
      const changePct = ((last - first) / first * 100).toFixed(2);
      const color = last >= first ? "#22c55e" : "#ef4444";
      return `
        <div class="chart-card">
          <h3>${p.symbol} — ${p.description}</h3>
          <div class="chart-meta">
            $${fmt(last)} · <span class="${pctClass(parseFloat(changePct))}">${changePct > 0 ? "+" : ""}${changePct}%</span> (30d)
            ${p.ma20 ? ` · MA20: $${fmt(p.ma20)}` : ""}
            ${p.rsi != null ? ` · RSI: <span class="${rsiClass(p.rsi)}">${fmt(p.rsi, 1)}</span>` : ""}
          </div>
          <canvas id="chart-${p.symbol}" height="90"></canvas>
        </div>
      `;
    }).join("");

  positions
    .filter(p => p.sparkline && p.sparkline.length >= 2)
    .forEach(p => {
      const canvas = document.getElementById(`chart-${p.symbol}`);
      if (!canvas) return;
      const first = p.sparkline[0];
      const last = p.sparkline[p.sparkline.length - 1];
      const color = last >= first ? "#22c55e" : "#ef4444";
      const labels = p.sparkline.map((_, i) => `D-${p.sparkline.length - 1 - i}`);

      new Chart(canvas, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: p.symbol,
              data: p.sparkline,
              borderColor: color,
              borderWidth: 2,
              pointRadius: 0,
              fill: {
                target: "origin",
                above: color + "22",
              },
              tension: 0.3,
            },
            ...(p.ma20 ? [{
              label: "MA20",
              data: Array(p.sparkline.length).fill(p.ma20),
              borderColor: "#3b82f6",
              borderWidth: 1,
              borderDash: [4, 4],
              pointRadius: 0,
              fill: false,
            }] : []),
          ],
        },
        options: {
          animation: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              mode: "index",
              intersect: false,
              callbacks: {
                label: ctx => ` $${fmt(ctx.parsed.y)}`,
              },
            },
          },
          scales: {
            x: {
              display: true,
              ticks: { color: "#8892a4", font: { size: 10 }, maxTicksLimit: 6 },
              grid: { color: "#2a2d3a" },
            },
            y: {
              display: true,
              ticks: {
                color: "#8892a4",
                font: { size: 10 },
                callback: v => `$${fmt(v)}`,
              },
              grid: { color: "#2a2d3a" },
            },
          },
        },
      });
    });
}

async function init() {
  try {
    const data = await loadData();
    renderSummary(data);
    renderAlerts(data.positions);
    renderTable(data.positions);
    drawSparklines(data.positions);
    renderCharts(data.positions);
  } catch (err) {
    document.getElementById("positions-body").innerHTML =
      `<tr><td colspan="12" class="loading">Error al cargar datos: ${err.message}</td></tr>`;
  }
}

init();

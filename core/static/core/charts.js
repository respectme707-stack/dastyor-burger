// Графики в тёмной теме. Данные берутся из <script type="application/json" id="...">.
(function () {
  const data = (id) => JSON.parse(document.getElementById(id).textContent);
  const fmt = (v) => Math.round(v).toLocaleString("ru-RU");
  if (window.Chart) {
    Chart.defaults.color = "#9198a1";
    Chart.defaults.borderColor = "#262c36";
    Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  }
  window.barChart = function (canvasId, labelsId, valuesId, opts) {
    const el = document.getElementById(canvasId);
    if (!window.Chart || !el) return;
    opts = opts || {};
    const unit = opts.unit === undefined ? window.t(" сум") : opts.unit;
    const horizontal = !!opts.horizontal;
    const valueAxis = { beginAtZero: true, ticks: { callback: fmt, precision: 0 } };
    new Chart(el, {
      type: "bar",
      data: { labels: data(labelsId), datasets: [{ data: data(valuesId), backgroundColor: opts.color || "#4493f8", borderRadius: 3, maxBarThickness: 36 }] },
      options: {
        maintainAspectRatio: false,
        indexAxis: horizontal ? "y" : "x",
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => fmt(horizontal ? c.parsed.x : c.parsed.y) + unit } } },
        scales: horizontal ? { x: valueAxis, y: { grid: { display: false } } } : { y: valueAxis, x: { grid: { display: false } } },
      },
    });
  };
})();

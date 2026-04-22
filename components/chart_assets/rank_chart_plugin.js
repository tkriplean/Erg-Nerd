/**
 * RankChart — HyperDiv plugin for the Rank Page.
 *
 * Chart.js scatter over a categorical x-axis (one tick per ranked event in a
 * caller-provided order) with a numeric y-axis. Static — no animation, no
 * scrubber, no Python round-trips back from JS.
 *
 * Props (Python → JS):
 *   event_order  [{key, label}, …]             — x-axis ticks, left → right
 *   series       [{label, color, border_color,
 *                  points:[{x_key, y, tooltip}, …]}, …]
 *   y_label      str                           — y-axis title
 *   y_mode       "pct" | "percentile"          — dashed reference line target
 *   is_dark      bool
 *   height_css   CSS length                    — canvas container height
 */

window.hyperdiv.registerPlugin("RankChart", (ctx) => {

  // ── Shadow DOM scaffold ────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; }
    .wrap { position: relative; width: 100%; }
    canvas { display: block; width: 100% !important; height: 100% !important; }
  `;
  ctx.domElement.appendChild(style);

  const wrap = document.createElement("div");
  wrap.className = "wrap";
  const canvas = document.createElement("canvas");
  wrap.appendChild(canvas);
  ctx.domElement.appendChild(wrap);

  // ── State from initial props ───────────────────────────────────────────────
  let eventOrder = ctx.initialProps.event_order || [];
  let series     = ctx.initialProps.series || [];
  let yLabel     = ctx.initialProps.y_label || "";
  let yMode      = ctx.initialProps.y_mode || "pct";
  let isDark     = !!(ctx.initialProps.is_dark);
  let heightCss  = ctx.initialProps.height_css || "55vh";

  wrap.style.height = heightCss;

  let chart = null;

  function keyIndex() {
    const m = new Map();
    eventOrder.forEach((ev, i) => m.set(ev.key, i));
    return m;
  }

  function buildDatasets() {
    const idx = keyIndex();
    return series.map((s) => ({
      label: s.label,
      data: (s.points || [])
        .map((p) => {
          const x = idx.get(p.x_key);
          if (x === undefined) return null;
          return { x, y: p.y, tooltip: p.tooltip || "" };
        })
        .filter(Boolean),
      backgroundColor: s.color || "rgba(59,130,246,0.85)",
      borderColor: s.border_color || s.color || "rgba(29,78,216,1.0)",
      borderWidth: 1.5,
      pointRadius: 5,
      pointHoverRadius: 7,
      showLine: false,
    }));
  }

  function gridColor() { return isDark ? "rgba(148,163,184,0.18)" : "rgba(148,163,184,0.28)"; }
  function textColor() { return isDark ? "rgba(226,232,240,0.88)" : "rgba(30,41,59,0.88)"; }

  function buildConfig() {
    const labels = eventOrder.map((e) => e.label);
    const refY = yMode === "percentile" ? 50 : 100;
    return {
      type: "scatter",
      data: { datasets: buildDatasets() },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (item) => item.raw.tooltip || `${item.parsed.y.toFixed(1)}`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: -0.5,
            max: Math.max(0.5, labels.length - 0.5),
            ticks: {
              stepSize: 1,
              color: textColor(),
              callback: (val) => labels[val] || "",
            },
            grid: { color: gridColor() },
          },
          y: {
            title: { display: !!yLabel, text: yLabel, color: textColor() },
            ticks: {
              color: textColor(),
              callback: (val) => `${val}`,
            },
            grid: { color: gridColor() },
          },
        },
        elements: { point: { hitRadius: 10 } },
      },
      plugins: [
        {
          id: "refLine",
          afterDraw: (ch) => {
            const { ctx: cctx, chartArea, scales } = ch;
            if (!chartArea || !scales.y) return;
            const y = scales.y.getPixelForValue(refY);
            if (y < chartArea.top || y > chartArea.bottom) return;
            cctx.save();
            cctx.strokeStyle = isDark ? "rgba(248,113,113,0.7)" : "rgba(220,38,38,0.7)";
            cctx.lineWidth = 1;
            cctx.setLineDash([4, 3]);
            cctx.beginPath();
            cctx.moveTo(chartArea.left, y);
            cctx.lineTo(chartArea.right, y);
            cctx.stroke();
            cctx.restore();
          },
        },
      ],
    };
  }

  function render() {
    if (chart) {
      chart.destroy();
      chart = null;
    }
    if (!window.Chart || !eventOrder.length) return;
    chart = new window.Chart(canvas, buildConfig());
  }

  // Chart.js may not be present yet (script tag still loading). Poll briefly.
  function ensureChartThenRender() {
    if (window.Chart) { render(); return; }
    let tries = 0;
    const t = setInterval(() => {
      tries += 1;
      if (window.Chart || tries > 50) {
        clearInterval(t);
        render();
      }
    }, 100);
  }
  ensureChartThenRender();

  ctx.onPropUpdate((name, value) => {
    if (name === "event_order") eventOrder = value || [];
    else if (name === "series") series = value || [];
    else if (name === "y_label") yLabel = value || "";
    else if (name === "y_mode") yMode = value || "pct";
    else if (name === "is_dark") isDark = !!value;
    else if (name === "height_css") { heightCss = value || "55vh"; wrap.style.height = heightCss; }
    render();
  });

  const ro = new ResizeObserver(() => { if (chart) chart.resize(); });
  ro.observe(wrap);
});

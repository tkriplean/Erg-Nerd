/**
 * RankChart — HyperDiv plugin for the Rank Page.
 *
 * Chart.js scatter over a logarithmic x-axis (event duration in seconds) with
 * a numeric y-axis. Static — no animation, no scrubber, no Python round-trips.
 *
 * Props (Python → JS):
 *   event_order  [{key, label}, …]             — kept for back-compat / metadata
 *   series       [{label, color, border_color,
 *                  points:[{x, y, event, date, age,
 *                           rank?, rank_total?, percentile?,
 *                           wr_pct?, wr_kind?}, …]}, …]
 *   y_label      str                           — y-axis title
 *   y_mode       "pct" | "percentile"          — dashed reference + tick format
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
    .rc-tip {
      position: absolute;
      pointer-events: none;
      transform: translate(-50%, -100%);
      padding: 8px 10px;
      border-radius: 6px;
      font-size: 12px;
      line-height: 1.35;
      white-space: nowrap;
      box-shadow: 0 4px 12px rgba(0,0,0,0.15);
      transition: opacity 0.08s ease, left 0.08s ease, top 0.08s ease;
      opacity: 0;
      z-index: 10;
    }
    .rc-tip .rc-tip-event {
      font-weight: 700;
      font-size: 13px;
      margin-bottom: 4px;
    }
    .rc-tip .rc-tip-line { font-size: 12px; }
    .rc-tip .rc-tip-num { font-variant-numeric: tabular-nums; }
  `;
  ctx.domElement.appendChild(style);

  const wrap = document.createElement("div");
  wrap.className = "wrap";
  const canvas = document.createElement("canvas");
  wrap.appendChild(canvas);
  const tip = document.createElement("div");
  tip.className = "rc-tip";
  wrap.appendChild(tip);
  ctx.domElement.appendChild(wrap);

  // ── State from initial props ───────────────────────────────────────────────
  let series     = ctx.initialProps.series || [];
  let yLabel     = ctx.initialProps.y_label || "";
  let yMode      = ctx.initialProps.y_mode || "pct";
  let isDark     = !!(ctx.initialProps.is_dark);
  let heightCss  = ctx.initialProps.height_css || "55vh";

  wrap.style.height = heightCss;

  let chart = null;

  // ── Formatting helpers ─────────────────────────────────────────────────────
  function ordinal(n) {
    n = Math.round(n);
    const m100 = n % 100;
    if (m100 >= 11 && m100 <= 13) return n + "th";
    switch (n % 10) {
      case 1: return n + "st";
      case 2: return n + "nd";
      case 3: return n + "rd";
      default: return n + "th";
    }
  }

  function fmtDuration(s) {
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) {
      const m = s / 60;
      return Number.isInteger(m) ? `${m}m` : `${m.toFixed(0)}m`;
    }
    const h = s / 3600;
    return Number.isInteger(h) ? `${h}h` : `${h.toFixed(1)}h`;
  }

  const X_TICKS = [10, 30, 60, 120, 300, 600, 1800, 3600, 7200];

  // ── Datasets ───────────────────────────────────────────────────────────────
  function buildDatasets() {
    return series.map((s) => ({
      label: s.label,
      data: (s.points || [])
        .filter((p) => typeof p.x === "number" && p.x > 0 && typeof p.y === "number")
        .map((p) => ({
          x: p.x,
          y: p.y,
          _meta: p,
        }))
        .sort((a, b) => a.x - b.x),
      backgroundColor: s.color || "rgba(59,130,246,0.85)",
      borderColor: s.border_color || s.color || "rgba(29,78,216,1.0)",
      borderWidth: 1.5,
      pointRadius: 5,
      pointHoverRadius: 7,
      showLine: true,
      tension: 0,
      fill: false,
      spanGaps: true,
    }));
  }

  function gridColor() { return isDark ? "rgba(148,163,184,0.18)" : "rgba(148,163,184,0.28)"; }
  function textColor() { return isDark ? "rgba(226,232,240,0.88)" : "rgba(30,41,59,0.88)"; }
  function tipBg()     { return isDark ? "rgba(15,23,42,0.96)"    : "rgba(255,255,255,0.98)"; }
  function tipFg()     { return isDark ? "rgba(241,245,249,0.95)" : "rgba(15,23,42,0.95)"; }
  function tipBorder() { return isDark ? "rgba(71,85,105,0.6)"    : "rgba(203,213,225,0.9)"; }

  // ── External HTML tooltip ──────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function tooltipBody(meta) {
    const lines = [];
    if (meta.date) lines.push(escapeHtml(meta.date));
    if (typeof meta.age === "number") lines.push(`Age ${meta.age}`);
    if (typeof meta.rank_total === "number" && meta.rank_total > 0) {
      const r = Number(meta.rank).toLocaleString();
      const t = Number(meta.rank_total).toLocaleString();
      lines.push(`<span class="rc-tip-num">Rank ${r} of ${t}</span>`);
    }
    if (typeof meta.percentile === "number") {
      lines.push(`<span class="rc-tip-num">${ordinal(meta.percentile)} percentile</span>`);
    }
    if (typeof meta.wr_pct === "number") {
      const kind = meta.wr_kind === "watts" ? "watts" : "pace";
      lines.push(`<span class="rc-tip-num">${meta.wr_pct.toFixed(1)}% of WR ${kind}</span>`);
    }
    return lines.map((l) => `<div class="rc-tip-line">${l}</div>`).join("");
  }

  function externalTooltip(context) {
    const tt = context.tooltip;
    if (!tt || tt.opacity === 0) {
      tip.style.opacity = "0";
      return;
    }
    const dp = tt.dataPoints && tt.dataPoints[0];
    const meta = dp && dp.raw && dp.raw._meta;
    if (!meta) {
      tip.style.opacity = "0";
      return;
    }

    tip.style.background = tipBg();
    tip.style.color = tipFg();
    tip.style.border = `1px solid ${tipBorder()}`;

    tip.innerHTML =
      `<div class="rc-tip-event">${escapeHtml(meta.event || "")}</div>` +
      tooltipBody(meta);

    // Position above + centered on the data point, with an 12px offset.
    const ch = context.chart;
    const rect = ch.canvas.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    const offsetX = rect.left - wrapRect.left;
    const offsetY = rect.top - wrapRect.top;

    const px = offsetX + dp.element.x;
    const py = offsetY + dp.element.y;

    const tipH = tip.offsetHeight || 0;
    const tipW = tip.offsetWidth || 0;
    const gap = 12;

    let top = py - gap;
    // Flip below if the tooltip would clip out the top of the wrap.
    if (top - tipH < 0) {
      tip.style.transform = "translate(-50%, 0)";
      top = py + gap;
    } else {
      tip.style.transform = "translate(-50%, -100%)";
    }

    // Clamp horizontally inside the wrap.
    let left = px;
    const halfW = tipW / 2;
    if (left - halfW < 4) left = halfW + 4;
    const maxLeft = wrap.clientWidth - halfW - 4;
    if (left > maxLeft) left = maxLeft;

    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    tip.style.opacity = "1";
  }

  function buildConfig() {
    return {
      type: "scatter",
      data: { datasets: buildDatasets() },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            enabled: false,
            external: externalTooltip,
            intersect: false,
            mode: "nearest",
          },
        },
        scales: {
          x: {
            type: "logarithmic",
            min: 8,
            max: 12000,
            title: {
              display: true,
              text: "Event duration (log scale)",
              color: textColor(),
            },
            ticks: {
              color: textColor(),
              autoSkip: false,
              callback: (val) => {
                // Only label our curated round durations.
                if (X_TICKS.indexOf(val) === -1) return "";
                return fmtDuration(val);
              },
            },
            afterBuildTicks: (axis) => {
              axis.ticks = X_TICKS.map((v) => ({ value: v }));
            },
            grid: { color: gridColor() },
          },
          y: {
            title: { display: !!yLabel, text: yLabel, color: textColor() },
            ticks: {
              color: textColor(),
              callback: (val) => {
                if (yMode === "percentile") {
                  if (Math.abs(val - Math.round(val)) > 1e-6) return "";
                  return ordinal(val);
                }
                return `${val}`;
              },
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
            const refY = yMode === "percentile" ? 50 : 100;
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
    tip.style.opacity = "0";
    if (!window.Chart) return;
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
    if (name === "event_order") { /* unused */ }
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

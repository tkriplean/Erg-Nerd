content: window.hyperdiv.registerPlugin("EffortStressChart", (ctx) => {
  // ── Shadow DOM setup ────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; }
    .chart-wrap { position: relative; width: 100%; }
    canvas { display: block; width: 100% !important; }
  `;
  ctx.domElement.appendChild(style);

  const chartWrap = document.createElement("div");
  chartWrap.className = "chart-wrap";
  const canvas = document.createElement("canvas");
  chartWrap.appendChild(canvas);
  ctx.domElement.appendChild(chartWrap);

  let chartInstance = null;
  let _config = null;
  let _height = 220;

  // ── Helpers ─────────────────────────────────────────────────────────────
  function fmtMSS(secs) {
    if (secs == null || isNaN(secs)) return "";
    const s = Math.max(0, Math.round(secs));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${r < 10 ? "0" : ""}${r}`;
  }

  function buildChart(cfg) {
    if (chartInstance) {
      chartInstance.destroy();
      chartInstance = null;
    }
    if (!cfg || !cfg.timeline || !cfg.timeline.length) return;

    const isDark = cfg.is_dark || false;
    const gridColor = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
    const tickColor = isDark ? "#9ca3af" : "#6b7280";
    const intensityColor = cfg.intensity_color || "rgba(220,80,80,0.95)";
    const wBalColor = isDark ? "rgba(120,180,240,0.95)" : "rgba(40,140,210,0.85)";
    const glycogenColor = cfg.glycogen_color || (isDark ? "rgba(245,165,75,0.95)" : "rgba(225,125,35,0.95)");

    canvas.style.height = (typeof _height === "number" ? _height + "px" : _height);

    // ── Datasets ───────────────────────────────────────────────────────────
    // Primary trace: I(t) on the left axis.  Drawn last so it renders on top.
    const intensityPoints = cfg.timeline.map((p) => ({ x: p.t, y: p.intensity }));

    // Six zone-ratio traces on the right axis.  Each band has its own color
    // (matching the rower's familiar power-bin palette), drawn thin so the
    // I(t) line dominates visual hierarchy.
    const zoneSpecs = cfg.zone_specs || [];
    const zoneDatasets = zoneSpecs.map((spec) => {
      const key = String(spec.band_s);
      const data = cfg.timeline.map((p) => ({
        x: p.t,
        y: ((p.zones || {})[key] || 0) * 100, // % of reference
      }));
      return {
        label: `${spec.label} band`,
        data,
        yAxisID: "y2",
        borderColor: spec.color,
        backgroundColor: "spec.color",
        borderWidth: 1.0,
        pointRadius: 0,
        tension: 0.2,
        order: 10, // behind I(t)
      };
    });

    // Optional W'bal trace — dashed, on the right axis, distinguished from
    // the zone lines by stroke pattern + color.
    const wBalPoints = cfg.has_w_bal
      ? cfg.timeline
          .filter((p) => p.w_bal_pct != null)
          .map((p) => ({ x: p.t, y: p.w_bal_pct }))
      : [];

    // Optional Glycogen Used trace — dotted (distinct from W'bal's dash),
    // orange (sibling color of the Anaerobic bin to flag "fuel store").
    // Inverse direction from W'bal: starts at 0, rises monotonically as
    // glycogen depletes.
    const glycogenPoints = cfg.has_glycogen
      ? cfg.timeline
          .filter((p) => p.glycogen_pct != null)
          .map((p) => ({ x: p.t, y: p.glycogen_pct }))
      : [];

    const datasets = [...zoneDatasets];
    if (cfg.has_w_bal && wBalPoints.length) {
      datasets.push({
        label: "W' remaining (%)",
        data: wBalPoints,
        yAxisID: "y2",
        borderColor: wBalColor,
        backgroundColor: "wBalColor",
        borderWidth: 1.4,
        borderDash: [4, 3],
        pointRadius: 0,
        tension: 0.2,
        order: 5,
      });
    }
    if (cfg.has_glycogen && glycogenPoints.length) {
      datasets.push({
        label: "Glycogen used (%)",
        data: glycogenPoints,
        yAxisID: "y2",
        borderColor: glycogenColor,
        backgroundColor: "glycogenColor",
        borderWidth: 1.4,
        borderDash: [2, 2],
        pointRadius: 0,
        tension: 0.2,
        order: 4,
      });
    }
    datasets.push({
      label: "Intensity I(t)",
      data: intensityPoints,
      yAxisID: "y",
      borderColor: intensityColor,
      backgroundColor: "intensityColor",
      borderWidth: 2.0,
      pointRadius: 0,
      tension: 0.2,
      order: 1,
    });

    // ── Severity bands + workout-window highlight ─────────────────────────
    // Severity bands paint as low-opacity backgrounds on the I(t) axis;
    // workout-window paints as a faint vertical band on the time axis.
    const annotations = {};
    (cfg.severity_bands || []).forEach((band, i) => {
      annotations[`sev_${i}`] = {
        type: "box",
        yScaleID: "y",
        yMin: band.from,
        yMax: band.to,
        backgroundColor: band.color,
        borderWidth: 0,
        drawTime: "beforeDatasetsDraw",
      };
    });
    if (cfg.workout_window) {
      annotations.workout_window = {
        type: "box",
        xScaleID: "x",
        xMin: cfg.workout_window.xMin,
        xMax: cfg.workout_window.xMax,
        backgroundColor: cfg.workout_band_color || "rgba(120,120,120,0.10)",
        borderWidth: 0,
        drawTime: "beforeDatasetsDraw",
      };
    }

    const xMax = cfg.session_duration_s || intensityPoints[intensityPoints.length - 1].x || 0;
    const intensityMax = cfg.intensity_y_max || 1.0;
    const zoneMax = (cfg.zone_y_max || 1.5) * 100;

    chartInstance = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        normalized: true,
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: {
              color: tickColor,
              font: { size: 10 },
              boxWidth: 18,
              boxHeight: 2,
            },
          },
          tooltip: {
            mode: "index",
            intersect: false,
            displayColors: true,
            callbacks: {
              title: (items) => fmtMSS(items[0].parsed.x),
              label: (item) => {
                const v = item.parsed.y;
                if (item.dataset.yAxisID === "y") {
                  return `Intensity I(t): ${v.toFixed(2)}`;
                }
                if (item.dataset.label === "W' remaining (%)") {
                  return `W' remaining: ${v.toFixed(0)}%`;
                }
                if (item.dataset.label === "Glycogen used (%)") {
                  return `Glycogen used: ${v.toFixed(0)}%`;
                }
                return `${item.dataset.label}: ${v.toFixed(0)}%`;
              },
            },
          },
          annotation: { annotations },
        },
        scales: {
          x: {
            type: "linear",
            min: 0,
            max: xMax,
            grid: { color: gridColor },
            ticks: {
              color: tickColor,
              callback: (v) => fmtMSS(v),
            },
            title: { display: true, text: "Session time", color: tickColor, font: { size: 10 } },
          },
          y: {
            type: "linear",
            position: "left",
            min: 0,
            max: intensityMax,
            grid: { color: gridColor },
            ticks: { color: intensityColor, callback: (v) => v.toFixed(2) },
            title: { display: true, text: "Intensity I(t)", color: intensityColor, font: { size: 10 } },
          },
          y2: {
            type: "linear",
            position: "right",
            min: 0,
            max: zoneMax,
            grid: { drawOnChartArea: false },
            ticks: { color: tickColor, callback: (v) => `${v}%` },
            title: { display: true, text: "Zone saturation", color: tickColor, font: { size: 10 } },
          },
        },
      },
    });
  }

  ctx.onPropUpdate((name, value) => {
    if (name === "config") {
      _config = value;
      buildChart(_config);
    } else if (name === "height") {
      _height = value != null ? value : 220;
      canvas.style.height = (typeof _height === "number" ? _height + "px" : _height);
      if (chartInstance) chartInstance.resize();
    }
  });

  if (ctx.initialProps.height) {
    _height = ctx.initialProps.height != null ? ctx.initialProps.height : 220;
  }
  if (ctx.initialProps.config) {
    _config = ctx.initialProps.config;
    buildChart(_config);
  }
});
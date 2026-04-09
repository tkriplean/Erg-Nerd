window.hyperdiv.registerPlugin("StrokeChart", (ctx) => {
  // --- Shadow DOM setup ---
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; }
    canvas { display: block; width: 100% !important; }
  `;
  ctx.domElement.appendChild(style);

  const canvas = document.createElement("canvas");
  ctx.domElement.appendChild(canvas);

  let chartInstance = null;

  // -----------------------------------------------------------------------
  // Formatters
  // -----------------------------------------------------------------------

  /** Format raw pace seconds (float) as "M:SS.t" */
  function formatPace(seconds) {
    if (seconds == null || isNaN(seconds)) return "—";
    const s = Math.abs(seconds);
    const mins = Math.floor(s / 60);
    const secs = (s % 60).toFixed(1).padStart(4, "0");
    return `${mins}:${secs}`;
  }

  /** Format elapsed seconds as "M:SS" */
  function formatTime(seconds) {
    const s = Math.abs(seconds);
    const mins = Math.floor(s / 60);
    const secs = Math.floor(s % 60).toString().padStart(2, "0");
    return `${mins}:${secs}`;
  }

  // -----------------------------------------------------------------------
  // Band click detection — zoom to interval on click
  // -----------------------------------------------------------------------

  function findBandAtX(bands, xPx, chart) {
    if (!bands || !bands.length) return null;
    for (const b of bands) {
      const x1 = chart.scales.x.getPixelForValue(b.xMin);
      const x2 = chart.scales.x.getPixelForValue(b.xMax);
      if (xPx >= Math.min(x1, x2) && xPx <= Math.max(x1, x2)) return b;
    }
    return null;
  }

  // -----------------------------------------------------------------------
  // Chart builder
  // -----------------------------------------------------------------------

  function buildChart(cfg) {
    if (chartInstance) {
      chartInstance.destroy();
      chartInstance = null;
    }
    if (!cfg) return;

    const isDark = cfg.isDark || false;
    const gridColor = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
    const tickColor = isDark ? "#9ca3af" : "#6b7280";
    const datasets = cfg.datasets || [];
    const bands = cfg.bands || [];
    const xMin = cfg.xMin != null ? cfg.xMin : undefined;
    const xMax = cfg.xMax != null ? cfg.xMax : undefined;
    const paceYMin = cfg.paceYMin != null ? cfg.paceYMin : undefined;
    const paceYMax = cfg.paceYMax != null ? cfg.paceYMax : undefined;
    const spmYMin  = cfg.spmYMin  != null ? cfg.spmYMin  : 0;
    const spmYMax  = cfg.spmYMax  != null ? cfg.spmYMax  : 30;

    // Build annotation objects for interval background bands
    const annotations = {};
    bands.forEach((b, i) => {
      annotations[`band${i}`] = {
        type: "box",
        xScaleID: "x",
        xMin: b.xMin,
        xMax: b.xMax,
        backgroundColor: b.work
          ? (isDark ? "rgba(217,119,6,0.10)" : "rgba(245,158,11,0.08)")
          : (isDark ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.03)"),
        borderWidth: 0,
        label: {
          display: true,
          content: b.label || "",
          position: "start",
          yAdjust: -2,
          color: isDark ? "rgba(217,119,6,0.7)" : "rgba(180,120,0,0.7)",
          font: { size: 10 },
        },
      };
    });

    // Check if annotation plugin is available
    const hasAnnotation =
      window.Chart &&
      window.Chart.registry &&
      window.Chart.registry.plugins &&
      window.Chart.registry.plugins.get &&
      window.Chart.registry.plugins.get("annotation");

    const chartCfg = {
      type: "line",
      data: { datasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => "@ " + formatTime(items[0].parsed.x),
              label: (item) => {
                const ds = datasets[item.datasetIndex] || {};
                if (ds.yAxisID === "y") {
                  // pace or watts
                  if (cfg.showWatts) return ` ${Math.round(item.parsed.y)} W`;
                  return ` ${formatPace(item.parsed.y)}`;
                }
                if (ds.yAxisID === "yspm") return ` ${item.parsed.y} spm`;
                if (ds.yAxisID === "yhr")  return ` ${item.parsed.y} bpm`;
                return ` ${item.parsed.y}`;
              },
            },
          },
          ...(hasAnnotation ? { annotation: { annotations } } : {}),
        },
        scales: {
          x: {
            type: "linear",
            min: xMin,
            max: xMax,
            grid: { color: gridColor },
            ticks: {
              color: tickColor,
              maxTicksLimit: 12,
              callback: (v) => formatTime(v),
            },
          },
          y: {
            type: "linear",
            position: "left",
            reverse: !cfg.showWatts,  // faster pace = lower number = top of chart
            min: paceYMin,
            max: paceYMax,
            grid: { color: gridColor },
            ticks: {
              color: tickColor,
              callback: (v) => cfg.showWatts ? `${Math.round(v)}W` : formatPace(v),
            },
          },
          yspm: {
            type: "linear",
            position: "right",
            display: true,
            min: spmYMin,
            max: spmYMax,
            grid: { drawOnChartArea: false },
            ticks: { color: isDark ? "#d97706" : "#b45309", callback: (v) => `${v}` },
            title: {
              display: true,
              text: "spm",
              color: isDark ? "#d97706" : "#b45309",
              font: { size: 10 },
            },
          },
          yhr: {
            type: "linear",
            position: "right",
            display: cfg.hasHr || false,
            min: 40,
            max: 220,
            grid: { drawOnChartArea: false },
            ticks: { color: isDark ? "#f87171" : "#dc2626", callback: (v) => `${v}` },
            title: {
              display: true,
              text: "bpm",
              color: isDark ? "#f87171" : "#dc2626",
              font: { size: 10 },
            },
          },
        },
      },
    };

    chartInstance = new Chart(canvas, chartCfg);

    // Band click → zoom
    canvas.addEventListener("click", (evt) => {
      if (!chartInstance) return;
      const rect = canvas.getBoundingClientRect();
      const xPx = evt.clientX - rect.left;
      const band = findBandAtX(bands, xPx, chartInstance);
      if (band) {
        ctx.updateProp("clicked_band_idx", band.idx);
      }
    });
  }

  // -----------------------------------------------------------------------
  // HyperDiv lifecycle
  // -----------------------------------------------------------------------

  // Read initial props and build the chart on first mount.
  let _config = ctx.initialProps.config || null;
  const _h = ctx.initialProps.height;
  if (_h) canvas.style.height = typeof _h === "number" ? _h + "px" : _h;
  buildChart(_config);

  // Rebuild whenever Python pushes new prop values.
  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "height") {
      canvas.style.height = typeof propValue === "number" ? propValue + "px" : propValue;
    }
    if (propName === "config") {
      _config = propValue;
      buildChart(_config);
    }
  });
});

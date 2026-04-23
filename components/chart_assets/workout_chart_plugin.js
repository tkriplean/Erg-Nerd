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
  let clickSeq = 0;

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
  // Shared right-axis scale builder — de-duplicates yspm/yhr between modes
  // -----------------------------------------------------------------------

  function buildRightScales(cfg, isDark, displaySpm, displayHr) {
    const spmColor = isDark ? "#3b82f6" : "#1e40af";
    const hrColor  = isDark ? "#f87171" : "#ef4444";
    const spmYMin  = cfg.spmYMin  != null ? cfg.spmYMin  : 0;
    const spmYMax  = cfg.spmYMax  != null ? cfg.spmYMax  : 30;
    const hrYMin   = cfg.hrYMin   != null ? cfg.hrYMin   : 40;
    const hrYMax   = cfg.hrYMax   != null ? cfg.hrYMax   : 220;
    return {
      yspm: {
        type: "linear",
        position: "right",
        display: displaySpm,
        min: spmYMin,
        max: spmYMax,
        grid: { drawOnChartArea: false },
        ticks: { color: spmColor, callback: (v) => `${v}` },
        title: { display: true, text: "spm", color: spmColor, font: { size: 10 } },
      },
      yhr: {
        type: "linear",
        position: "right",
        display: displayHr,
        min: hrYMin,
        max: hrYMax,
        grid: { drawOnChartArea: false },
        ticks: { color: hrColor, callback: (v) => `${v}` },
        title: { display: true, text: "bpm", color: hrColor, font: { size: 10 } },
      },
    };
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

    // -----------------------------------------------------------------------
    // Stacked mode — one dataset per interval per visible metric, x reset to 0
    // -----------------------------------------------------------------------

    if (cfg.stack) {
      const intervals = cfg.stackedIntervals || [];
      const paceYMin = cfg.paceYMin != null ? cfg.paceYMin : undefined;
      const paceYMax = cfg.paceYMax != null ? cfg.paceYMax : undefined;

      const datasets = [];

      // Pace / Watts — primary Y axis (one per interval)
      if (cfg.showPace !== false) {
        intervals.forEach((iv) => {
          if (!iv.pacePoints || !iv.pacePoints.length) return;
          datasets.push({
            label: iv.label,
            data: iv.pacePoints,
            yAxisID: "y",
            borderColor: iv.color,
            backgroundColor: "transparent",
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.15,
            order: 1,
          });
        });
      }

      // SPM — right axis, dashed (one per interval)
      if (cfg.showSpm !== false) {
        intervals.forEach((iv) => {
          if (!iv.spmPoints || !iv.spmPoints.length) return;
          datasets.push({
            label: "_" + iv.label + " spm",   // "_" prefix hides from legend
            data: iv.spmPoints,
            yAxisID: "yspm",
            borderColor: iv.color,
            backgroundColor: "transparent",
            borderWidth: 1,
            borderDash: [3, 3],
            pointRadius: 0,
            tension: 0.1,
            order: 2,
          });
        });
      }

      // HR — right axis, dotted (one per interval)
      if (cfg.showHr !== false && cfg.hasHr) {
        intervals.forEach((iv) => {
          if (!iv.hrPoints || !iv.hrPoints.length) return;
          datasets.push({
            label: "_" + iv.label + " hr",    // "_" prefix hides from legend
            data: iv.hrPoints,
            yAxisID: "yhr",
            borderColor: iv.color,
            backgroundColor: "transparent",
            borderWidth: 1,
            borderDash: [2, 4],
            pointRadius: 0,
            tension: 0.1,
            order: 3,
          });
        });
      }

      chartInstance = new Chart(canvas, {
        type: "line",
        data: { datasets },
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: {
              display: true,
              position: "top",
              labels: {
                usePointStyle: true,
                pointStyle: "line",
                filter: (item) => !item.text.startsWith("_"),
                color: tickColor,
                font: { size: 11 },
              },
            },
            tooltip: {
              callbacks: {
                title: (items) => formatTime(items[0].parsed.x),
                label: (item) => {
                  const ds = datasets[item.datasetIndex] || {};
                  const name = (ds.label || "").replace(/^_/, "");
                  if (ds.yAxisID === "y") {
                    return cfg.showWatts
                      ? ` ${name}: ${Math.round(item.parsed.y)} W`
                      : ` ${name}: ${formatPace(item.parsed.y)}`;
                  }
                  if (ds.yAxisID === "yspm") return ` ${name}: ${item.parsed.y} spm`;
                  if (ds.yAxisID === "yhr")  return ` ${name}: ${item.parsed.y} bpm`;
                  return ` ${name}: ${item.parsed.y}`;
                },
              },
            },
          },
          scales: {
            x: {
              type: "linear",
              grid: { color: gridColor },
              ticks: {
                color: tickColor,
                maxTicksLimit: 10,
                callback: (v) => formatTime(v),
              },
            },
            y: {
              type: "linear",
              position: "left",
              reverse: !cfg.showWatts,
              min: paceYMin,
              max: paceYMax,
              grid: { color: gridColor },
              ticks: {
                color: tickColor,
                callback: (v) => cfg.showWatts ? `${Math.round(v)}W` : formatPace(v),
              },
            },
            ...buildRightScales(
              cfg, isDark,
              cfg.showSpm !== false,
              cfg.showHr !== false && (cfg.hasHr || false)
            ),
          },
        },
      });
      return;  // stacked chart built — skip normal path
    }

    // -----------------------------------------------------------------------
    // Normal (non-stacked) path
    // -----------------------------------------------------------------------

    const bands = cfg.bands || [];
    const xMin = cfg.xMin != null ? cfg.xMin : undefined;
    const xMax = cfg.xMax != null ? cfg.xMax : undefined;
    const paceYMin = cfg.paceYMin != null ? cfg.paceYMin : undefined;
    const paceYMax = cfg.paceYMax != null ? cfg.paceYMax : undefined;

    // Pre-compute faded x-ranges: rest periods + the first few seconds of
    // each work interval (onset strokes are noisy).
    const ONSET_S = 5;
    const fadedRanges = [
      ...bands.filter(b => !b.work).map(b => [b.xMin, b.xMax]),
      ...bands.filter(b =>  b.work).map(b => [b.xMin, Math.min(b.xMin + ONSET_S, b.xMax)]),
    ];

    function inFaded(x) {
      return fadedRanges.some(([lo, hi]) => x >= lo && x <= hi);
    }
    function segFaded(ctx) {
      return !(xMin || xMax) && inFaded((ctx.p0.parsed.x + ctx.p1.parsed.x) / 2);
    }

    // Read series colors from config (Python is the single source of truth).
    const paceColor      = cfg.paceColor      || "#60a5fa";
    const paceFadedColor = cfg.paceFadedColor || "rgba(96,165,250,0.25)";
    const spmSegColor    = cfg.spmColor       || "#1e40af";
    const spmFadedColor  = cfg.spmFadedColor  || "rgba(30,64,175,0.0)";

    // Clone datasets and attach segment callbacks to the *primary* pace
    // and SPM series.  Compare-overlay datasets are flagged isCompare so
    // we leave their borderColor/borderWidth untouched — each compared
    // workout keeps its own distinct color.
    const datasets = (cfg.datasets || []).map(ds => {
      const d = Object.assign({}, ds);
      if (d.isCompare) return d;
      if (d.borderDash) return d;
      if (d.yAxisID === "y") {
        d.segment = {
          borderColor: ctx => segFaded(ctx) ? paceFadedColor : paceColor,
          borderWidth: ctx => segFaded(ctx) ? 1    : 2.5,
          borderDash:  ctx => segFaded(ctx) ? [4,4] : [],
        };
      } else if (d.yAxisID === "yspm") {
        d.segment = {
          borderColor: ctx => segFaded(ctx) ? spmFadedColor  : spmSegColor,
          borderWidth: ctx => segFaded(ctx) ? 1    : 1.5,
          borderDash:  ctx => segFaded(ctx) ? [4,4] : [],
        };
      }
      return d;
    });

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
          legend: cfg.showLegend
            ? {
                display: true,
                position: "top",
                labels: {
                  usePointStyle: true,
                  pointStyle: "line",
                  filter: (item) => !item.text.startsWith("_"),
                  color: tickColor,
                  font: { size: 11 },
                },
              }
            : { display: false },
          tooltip: {
            callbacks: {
              title: (items) => "@ " + formatTime(items[0].parsed.x),
              label: (item) => {
                const ds = datasets[item.datasetIndex] || {};
                const name = (ds.label || "").replace(/^_/, "");
                const prefix = cfg.showLegend ? ` ${name}: ` : " ";
                if (ds.yAxisID === "y") {
                  // pace or watts
                  if (cfg.showWatts) return `${prefix}${Math.round(item.parsed.y)} W`;
                  return `${prefix}${formatPace(item.parsed.y)}`;
                }
                if (ds.yAxisID === "yspm") return `${prefix}${item.parsed.y} spm`;
                if (ds.yAxisID === "yhr")  return `${prefix}${item.parsed.y} bpm`;
                return `${prefix}${item.parsed.y}`;
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
          ...buildRightScales(cfg, isDark, cfg.showSpm !== false, cfg.hasHr || false),
        },
      },
    };

    chartInstance = new Chart(canvas, chartCfg);

    // Band click → zoom.  Ignore clicks outside the plot area (legend,
    // axes, padding) so legend-item toggles don't hijack a band click.
    canvas.addEventListener("click", (evt) => {
      if (!chartInstance) return;
      const rect = canvas.getBoundingClientRect();
      const xPx = evt.clientX - rect.left;
      const yPx = evt.clientY - rect.top;
      const area = chartInstance.chartArea;
      if (
        !area ||
        xPx < area.left || xPx > area.right ||
        yPx < area.top  || yPx > area.bottom
      ) {
        return;
      }
      const band = findBandAtX(bands, xPx, chartInstance);
      if (band) {
        clickSeq += 1;
        ctx.updateProp("clicked_band_idx", band.idx);
        ctx.updateProp("click_seq", clickSeq);
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

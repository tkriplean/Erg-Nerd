// TrainingLoadChart — Banister CTL/ATL/TSB fitness-fatigue chart with
// brush-style date navigator beneath.  See training_load_chart_builder.py
// for the config shape.
//
// Layout:
//   ┌──────────────────────────────────────────────┐
//   │  Main (focus) chart — windowed view          │
//   │   • CTL solid blue line                      │
//   │   • ATL solid red line                       │
//   │   • TSB filled area (green above 0,          │
//   │     red below 0) on secondary axis           │
//   │   • TSB zone bands as background fills       │
//   ├──────────────────────────────────────────────┤
//   │  Navigator strip — full history, brush rect  │
//   │   • CTL line only, miniature                 │
//   │   • Drag handles on left/right brush edges   │
//   └──────────────────────────────────────────────┘

window.hyperdiv.registerPlugin("TrainingLoadChart", (ctx) => {
  // ── Shadow DOM setup ────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; }
    .wrap { position: relative; width: 100%; }
    .focus-wrap { position: relative; width: 100%; height: 70%; max-height: 60vh; }
    .nav-wrap   { position: relative; width: 100%; height: 30%; max-height: 80px; cursor: pointer; }
    canvas { display: block; width: 100% !important; }
    .brush-handle {
      position: absolute;
      top: 0;
      width: 8px;
      height: 100%;
      background: rgba(128,128,128,0.55);
      cursor: ew-resize;
      z-index: 5;
      border-radius: 1px;
    }
    .brush-handle:hover { background: rgba(128,128,128,0.80); }
    .brush-handle.dragging { background: rgba(128,128,128,0.95); }
  `;
  ctx.domElement.appendChild(style);

  const wrap = document.createElement("div");
  wrap.className = "wrap";
  ctx.domElement.appendChild(wrap);

  const focusWrap = document.createElement("div");
  focusWrap.className = "focus-wrap";
  wrap.appendChild(focusWrap);
  const focusCanvas = document.createElement("canvas");
  focusWrap.appendChild(focusCanvas);

  const navWrap = document.createElement("div");
  navWrap.className = "nav-wrap";
  wrap.appendChild(navWrap);
  const navCanvas = document.createElement("canvas");
  navWrap.appendChild(navCanvas);

  // Brush drag handles (positioned absolutely over the navigator).
  const handleLeft = document.createElement("div");
  handleLeft.className = "brush-handle";
  navWrap.appendChild(handleLeft);
  const handleRight = document.createElement("div");
  handleRight.className = "brush-handle";
  navWrap.appendChild(handleRight);

  let focusChart = null;
  let navChart = null;
  let _config = null;

  // Brush state
  let brushStart = 0;
  let brushEnd = 0;
  let xMinMs = 0;
  let xMaxMs = 0;
  const MIN_BRUSH_MS = 7 * 24 * 3600 * 1000;  // 1 week min window



  function clampBrush() {
    if (brushStart < xMinMs) brushStart = xMinMs;
    if (brushEnd > xMaxMs) brushEnd = xMaxMs;
    if (brushEnd - brushStart < MIN_BRUSH_MS) {
      brushEnd = brushStart + MIN_BRUSH_MS;
      if (brushEnd > xMaxMs) {
        brushEnd = xMaxMs;
        brushStart = brushEnd - MIN_BRUSH_MS;
      }
    }
  }

  // Custom Chart.js plugin — draws TSB zone bands behind data on the focus chart.
  const tsbZoneBandsPlugin = {
    id: "tsbZoneBands",
    beforeDatasetsDraw(chart, _args, opts) {
      const { ctx, chartArea, scales } = chart;
      const yScale = scales.y2;  // TSB axis
      if (!yScale || !chartArea || !opts.zones) return;
      ctx.save();
      for (const z of opts.zones) {
        const yTop = z.to !== null ? yScale.getPixelForValue(z.to) : chartArea.top;
        const yBot = z.from !== null ? yScale.getPixelForValue(z.from) : chartArea.bottom;
        const yMin = Math.max(chartArea.top, Math.min(yTop, yBot));
        const yMax = Math.min(chartArea.bottom, Math.max(yTop, yBot));
        ctx.fillStyle = z.color;
        ctx.fillRect(chartArea.left, yMin, chartArea.right - chartArea.left, yMax - yMin);
      }
      ctx.restore();
    },
  };

  // Custom plugin — paints the brush rect on the navigator chart.
  const brushOverlayPlugin = {
    id: "brushOverlay",
    afterDatasetsDraw(chart) {
      if (chart !== navChart) return;
      const { ctx, chartArea, scales } = chart;
      if (!chartArea || !scales.x) return;
      const xLeft = scales.x.getPixelForValue(brushStart);
      const xRight = scales.x.getPixelForValue(brushEnd);
      // Dim outside the brush window
      ctx.save();
      ctx.fillStyle = "rgba(80,80,80,0.25)";
      ctx.fillRect(chartArea.left, chartArea.top, xLeft - chartArea.left, chartArea.bottom - chartArea.top);
      ctx.fillRect(xRight, chartArea.top, chartArea.right - xRight, chartArea.bottom - chartArea.top);
      // Brush window outline
      ctx.strokeStyle = "rgba(128,128,128,0.85)";
      ctx.lineWidth = 1;
      ctx.strokeRect(xLeft, chartArea.top, xRight - xLeft, chartArea.bottom - chartArea.top);
      ctx.restore();

      // Position the drag handles
      const navRect = navWrap.getBoundingClientRect();
      const canvasRect = navCanvas.getBoundingClientRect();
      const xOff = canvasRect.left - navRect.left;
      handleLeft.style.left = (xOff + xLeft - 4) + "px";
      handleRight.style.left = (xOff + xRight - 4) + "px";
    },
  };

  function buildFocusChart() {
    if (focusChart) {
      focusChart.destroy();
      focusChart = null;
    }
    if (!_config) return;
    const cfg = _config;
    const ctlPts = cfg.ctl_points || [];
    const atlPts = cfg.atl_points || [];
    const tsbPts = cfg.tsb_points || [];

    // For the TSB filled area, split into positive and negative halves so
    // each half can have its own fill color.  Where TSB is positive we
    // render it as one dataset filled to zero (green); where negative,
    // another dataset filled to zero (red).  Points outside the half are
    // set to NaN so Chart.js's spanGaps=false breaks the line cleanly.
    const tsbPos = tsbPts.map((p) => ({ x: p.x, y: p.y > 0 ? p.y : 0 }));
    const tsbNeg = tsbPts.map((p) => ({ x: p.x, y: p.y < 0 ? p.y : 0 }));

    focusChart = new Chart(focusCanvas.getContext("2d"), {
      type: "line",
      data: {
        datasets: [
          {
            label: "TSB+ (recovered)",
            data: tsbPos,
            yAxisID: "y2",
            borderColor: "rgba(0,0,0,0)",
            backgroundColor: cfg.tsb_pos_fill,
            fill: { target: { value: 0 } },
            pointRadius: 0,
            tension: 0.2,
            order: 30,
          },
          {
            label: "TSB− (fatigue)",
            data: tsbNeg,
            yAxisID: "y2",
            borderColor: "rgba(0,0,0,0)",
            backgroundColor: cfg.tsb_neg_fill,
            fill: { target: { value: 0 } },
            pointRadius: 0,
            tension: 0.2,
            order: 30,
          },
          {
            label: "TSB (form)",
            data: tsbPts,
            yAxisID: "y2",
            borderColor: cfg.tsb_line_color,
            borderWidth: 1.0,
            borderDash: [3, 3],
            pointRadius: 0,
            tension: 0.2,
            fill: false,
            order: 20,
          },
          {
            label: "ATL (fatigue, 7-day)",
            data: atlPts,
            yAxisID: "y",
            borderColor: cfg.atl_color,
            borderWidth: 1.6,
            pointRadius: 0,
            tension: 0.25,
            fill: false,
            order: 10,
          },
          {
            label: "CTL (fitness, 42-day)",
            data: ctlPts,
            yAxisID: "y",
            borderColor: cfg.ctl_color,
            borderWidth: 2.0,
            pointRadius: 0,
            tension: 0.25,
            fill: false,
            order: 5,
          },
        ],
      },
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
              color: cfg.tick_color,
              font: { size: 10 },
              boxWidth: 18,
              boxHeight: 2,
              filter: (item) => {
                // Hide the two area-fill datasets from the legend; they're
                // visual aids for TSB sign, not separate metrics.
                return item.text !== "TSB+ (recovered)" && item.text !== "TSB− (fatigue)";
              },
            },
          },
          tooltip: {
            mode: "index",
            intersect: false,
            displayColors: true,
            filter: (item) => {
              // Suppress the two TSB-half datasets in the tooltip.
              return item.dataset.label !== "TSB+ (recovered)"
                  && item.dataset.label !== "TSB− (fatigue)";
            },
            callbacks: {
              title: (items) => {
                const ts = items[0].parsed.x;
                return new Date(ts).toLocaleDateString("en-US", {
                  year: "numeric", month: "short", day: "numeric",
                });
              },
              label: (item) => `${item.dataset.label}: ${item.parsed.y.toFixed(1)}`,
            },
          },
          tsbZoneBands: { zones: cfg.tsb_zones || [] },
        },
        scales: {
          x: {
            type: "time",
            min: brushStart,
            max: brushEnd,
            time: {
              unit: "month",
              displayFormats: {
                day: "MMM d",
                week: "MMM d",
                month: "MMM yy",
                year: "yyyy",
              },
            },
            grid: { color: cfg.grid_color },
            ticks: {
              color: cfg.tick_color,
              autoSkip: true,
              maxRotation: 0,
            },
          },
          y: {
            type: "linear",
            position: "left",
            grid: { color: cfg.grid_color },
            ticks: { color: cfg.tick_color, callback: (v) => v.toFixed(0) },
            title: {
              display: true,
              text: "Training Load (CTL / ATL)",
              color: cfg.tick_color,
              font: { size: 10 },
            },
          },
          y2: {
            type: "linear",
            position: "right",
            grid: { drawOnChartArea: false },
            ticks: { color: cfg.tick_color, callback: (v) => v.toFixed(0) },
            title: {
              display: true,
              text: "Form (TSB)",
              color: cfg.tick_color,
              font: { size: 10 },
            },
          },
        },
      },
      plugins: [tsbZoneBandsPlugin],
    });
  }

  function buildNavChart() {
    if (navChart) {
      navChart.destroy();
      navChart = null;
    }
    if (!_config) return;
    const cfg = _config;
    const ctlPts = cfg.ctl_points || [];

    navChart = new Chart(navCanvas.getContext("2d"), {
      type: "line",
      data: {
        datasets: [
          {
            label: "CTL",
            data: ctlPts,
            borderColor: cfg.ctl_color,
            borderWidth: 1.2,
            pointRadius: 0,
            tension: 0.3,
            fill: {
              target: "origin",
              above: cfg.ctl_color.replace(/[\d.]+\)$/, "0.10)"),
            },
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        normalized: true,
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false },
        },
        scales: {
          x: {
            type: "time",
            min: xMinMs,
            max: xMaxMs,
            grid: { color: cfg.grid_color },
            ticks: {
              color: cfg.tick_color,
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: 6,
            },
            time: {
              displayFormats: {
                month: "MMM yy",
                year: "yyyy",
              },
            },
          },
          y: {
            display: false,
          },
        },
        events: [],  // Disable hover events on navigator.
      },
      plugins: [brushOverlayPlugin],
    });
  }

  function rebuildAll() {
    buildNavChart();
    buildFocusChart();    
  }

  // ── Brush drag interactions ──────────────────────────────────────────────
  let dragMode = null;   // 'left' | 'right' | 'pan'
  let dragStartX = 0;
  let dragStartBrushStart = 0;
  let dragStartBrushEnd = 0;

  function msPerPx() {
    if (!navChart) return 1;
    const area = navChart.chartArea;
    if (!area) return 1;
    return (xMaxMs - xMinMs) / (area.right - area.left);
  }

  function pointerDown(e, mode) {
    dragMode = mode;
    dragStartX = e.clientX;
    dragStartBrushStart = brushStart;
    dragStartBrushEnd = brushEnd;
    e.target.classList.add("dragging");
    e.preventDefault();
  }

  handleLeft.addEventListener("pointerdown", (e) => pointerDown(e, "left"));
  handleRight.addEventListener("pointerdown", (e) => pointerDown(e, "right"));
  navCanvas.addEventListener("pointerdown", (e) => {
    // Click outside brush → re-center; click inside → start pan.
    if (!navChart) return;
    const area = navChart.chartArea;
    const rect = navCanvas.getBoundingClientRect();
    const xPx = e.clientX - rect.left;
    if (xPx < area.left || xPx > area.right) return;
    const ms = navChart.scales.x.getValueForPixel(xPx);
    const halfWindow = (brushEnd - brushStart) / 2;
    if (ms >= brushStart && ms <= brushEnd) {
      pointerDown(e, "pan");
    } else {
      // Re-center brush on click
      brushStart = ms - halfWindow;
      brushEnd = ms + halfWindow;
      clampBrush();
      buildFocusChart();
      navChart.draw();
    }
  });

  document.addEventListener("pointermove", (e) => {
    if (!dragMode) return;
    const dx = e.clientX - dragStartX;
    const dms = dx * msPerPx();
    if (dragMode === "left") {
      brushStart = dragStartBrushStart + dms;
    } else if (dragMode === "right") {
      brushEnd = dragStartBrushEnd + dms;
    } else if (dragMode === "pan") {
      brushStart = dragStartBrushStart + dms;
      brushEnd = dragStartBrushEnd + dms;
    }
    clampBrush();
    if (focusChart) {
      focusChart.options.scales.x.min = brushStart;
      focusChart.options.scales.x.max = brushEnd;
      focusChart.update("none");
    }
    if (navChart) navChart.draw();
  });
  document.addEventListener("pointerup", () => {
    if (!dragMode) return;
    dragMode = null;
    handleLeft.classList.remove("dragging");
    handleRight.classList.remove("dragging");
  });

  // ── Hyperdiv prop sync ───────────────────────────────────────────────────
  ctx.onPropUpdate((name, value) => {
    if (name === "config") {
      _config = value;
      if (_config) {
        xMinMs = _config.x_min_ms || 0;
        xMaxMs = _config.x_max_ms || 0;
        brushStart = _config.brush_start_ms || xMinMs;
        brushEnd = _config.brush_end_ms || xMaxMs;
        clampBrush();
      }
      rebuildAll();
    }
  });

  if (ctx.initialProps.config){
    _config = ctx.initialProps.config
    xMinMs = _config.x_min_ms || 0;
    xMaxMs = _config.x_max_ms || 0;
    brushStart = _config.brush_start_ms || xMinMs;
    brushEnd = _config.brush_end_ms || xMaxMs;
    clampBrush();
    rebuildAll();
  }
});

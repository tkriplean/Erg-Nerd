/**
 * SessionsChart — HyperDiv plugin for the pace-vs-date scatter with brush
 * navigator (focus+context / overview+detail pattern).
 *
 * Layout (flexbox column inside the shadow root):
 *   ┌─────────────────────────────────────────────────┐  flex: 1
 *   │  Main (focus) chart — windowed view             │
 *   ├─────────────────────────────────────────────────┤  1px separator
 *   │  Overview (context) chart — full history (88px) │  fixed height
 *   │  ██████▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓████  ← brush rect      │
 *   └─────────────────────────────────────────────────┘
 *
 * Props — Python → JS (Python-owned, never written by JS):
 *   points               [{x,y,r,r2,c,c33,c70,ivl,sb,dist,
 *                          work_m,rest_m,n_ivl,per_m,date_str,dist_str}]
 *   target_window_start  ms timestamp — drives the brush
 *   target_window_end    ms timestamp — drives the brush
 *   is_dark              bool
 *
 * Props — JS → Python (JS-owned, never written by Python after init):
 *   brush_start  ms timestamp — last brush position after user drag
 *   brush_end    ms timestamp
 *   change_id    monotonically incremented on every user interaction
 *
 * Rendering layers (bottom → top):
 *   3  SB halos     — transparent fill, gold ring, radius = r + 4
 *   2  Regular dots — 33% fill, 1px opaque border, radius = r
 *   1  Interval     — 33% fill (work area), opaque border ring (rest area)
 *                     Single circle: pointRadius = (r + r2)/2,
 *                     borderWidth = r - r2.  Chart.js strokes centered on
 *                     the circumference so: outer edge = r, inner edge = r2.
 *
 * Y-axis: slower pace (more sec/500m) at top, faster at bottom.
 *
 * Chart area is locked by the lockChartAreaPlugin (afterLayout hook) to
 * prevent any shift as tick density or label content changes.
 *
 * Brush interaction (overview canvas only):
 *   drag inside brush  → pan (smooth brush; main chart debounced 100 ms)
 *   click outside      → jump (centre brush on click, immediate rebuild)
 */

window.hyperdiv.registerPlugin("SessionsChart", (ctx) => {

  // ── Shadow DOM styles ──────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
    }
    .main-wrap {
      flex: 1;
      min-height: 0;
      position: relative;
    }
    .overview-wrap {
      height: 88px;
      flex-shrink: 0;
      position: relative;
      border-top: 1px solid rgba(128,128,128,0.20);
      margin-top: 3px;
      cursor: grab;
      user-select: none;
    }
    .overview-wrap.dragging { cursor: grabbing; }
    canvas {
      display: block;
      width: 100% !important;
      height: 100% !important;
    }
  `;
  ctx.domElement.appendChild(style);

  // ── DOM ────────────────────────────────────────────────────────────────────
  const mainWrap = document.createElement("div");
  mainWrap.className = "main-wrap";
  const mainCanvas = document.createElement("canvas");
  mainWrap.appendChild(mainCanvas);
  ctx.domElement.appendChild(mainWrap);

  const overviewWrap = document.createElement("div");
  overviewWrap.className = "overview-wrap";
  const overviewCanvas = document.createElement("canvas");
  overviewWrap.appendChild(overviewCanvas);
  ctx.domElement.appendChild(overviewWrap);

  // ── State ──────────────────────────────────────────────────────────────────
  let points       = ctx.initialProps.points || [];

  let isDark       = !!(ctx.initialProps.is_dark);
  let brushStartMs = ctx.initialProps.target_window_start || 0;
  let brushEndMs   = ctx.initialProps.target_window_end   || 0;
  let changeId     = 0;

  let dragActive      = false;
  let dragAnchorPx    = 0;
  let dragAnchorStart = 0;
  let dragAnchorEnd   = 0;
  let pendingRebuild  = null;

  let mainChart     = null;
  let overviewChart = null;

  // ── Hatch fill generator ──────────────────────────────────────────────────
  //
  // Creates a repeating CanvasPattern with diagonal (45°) stripes.
  // The fraction of the tile area covered by stripes ≈ restFraction,
  // so the visual density directly encodes how much of the total distance
  // was rest.
  //
  //  lightColor  (c25) — fill for the work area (sparse region)
  //  stripeColor (c60) — stripe color for the rest indicator
  //  restFraction      — rest_m / (work_m + rest_m)  in [0, 1]
  //
  // Tile layout: a single diagonal line from (0,0) → (p,p) tiles seamlessly.
  // Adjacent tiles share that corner, producing evenly-spaced parallel stripes
  // at perpendicular spacing p/√2.  Stripe coverage = lw / (p/√2) = restFraction
  // → lw = restFraction * p / √2.

  const _patternCache = new Map();

  function makeHatchFill(lightColor, stripeColor, restFraction) {
    // Cache keyed on colors + 5% bucket of restFraction.
    const bucket = Math.round(restFraction * 20);
    const key    = `${lightColor}|${stripeColor}|${bucket}`;
    if (_patternCache.has(key)) return _patternCache.get(key);

    const p  = 10;  // tile period (px) — perpendicular stripe spacing = p/√2 ≈ 7px
    const oc = document.createElement("canvas");
    oc.width = p; oc.height = p;
    const cx = oc.getContext("2d");

    // Background — work area color
    cx.fillStyle = lightColor;
    cx.fillRect(0, 0, p, p);

    if (restFraction > 0.04) {
      // Stripe width so that area coverage ≈ restFraction.
      const lw = Math.max(0.75, (restFraction * p) / Math.SQRT2);

      // Draw from (-1,-1) to (p+1, p+1) so the stroke fully overlaps tile
      // boundaries — eliminates any sub-pixel gap where tiles meet.
      // "square" lineCap also extends lw/2 past each endpoint.
      cx.strokeStyle = stripeColor;
      cx.lineWidth   = lw;
      cx.lineCap     = "square";
      cx.beginPath();
      cx.moveTo(-1, -1);
      cx.lineTo(p + 1, p + 1);
      cx.stroke();
    }

    const pattern = cx.createPattern(oc, "repeat");
    _patternCache.set(key, pattern);
    return pattern;
  }

  // ── Formatters ────────────────────────────────────────────────────────────
  function formatPace(sec) {
    const s = Math.abs(sec);
    const m = Math.floor(s / 60);
    const r = (s % 60).toFixed(1).padStart(4, "0");
    return `${m}:${r}`;
  }

  function fmtM(meters) {
    if (meters >= 1000) {
      const k = meters / 1000;
      return (Number.isInteger(k) ? k : k.toFixed(1)) + "k";
    }
    return meters.toLocaleString() + "m";
  }

  // ── Data helpers ──────────────────────────────────────────────────────────
  function dataRange() {
    if (!points.length) {
      const now = Date.now();
      return { minMs: now - 365 * 86400e3, maxMs: now };
    }
    let minMs = Infinity, maxMs = -Infinity;
    for (const p of points) {
      if (p.x < minMs) minMs = p.x;
      if (p.x > maxMs) maxMs = p.x;
    }
    return { minMs, maxMs };
  }

  function yRange() {
    if (!points.length) return { yMin: 100, yMax: 300 };
    let lo = Infinity, hi = -Infinity, hiR = 4;
    for (const p of points) {
      if (p.y < lo) lo = p.y;
      if (p.y > hi) { hi = p.y; hiR = p.r || 4; }
    }
    const yMin = Math.max(70, lo - 5);

    // Convert hiR (px) → pace (sec/500m) so the slowest circle isn't clipped.
    // mainWrap.clientHeight reflects the actual rendered container height; subtract
    // the fixed top and bottom margins to get the true plot area height.
    const plotH = Math.max(150, (mainWrap.clientHeight || 400) - CA_TOP - CA_BOTTOM);
    const provisionalRange = hi - yMin;
    const pacePerPx = provisionalRange / plotH;
    // Add one full radius worth of pace units + 4px breathing room.
    const yMax = Math.min(420, hi + (hiR + 4) * pacePerPx);

    return { yMin, yMax };
  }

  // ── Fixed chart-area margins ───────────────────────────────────────────────
  // CA_LEFT must fit the widest y-tick label ("1:45.0" ≈ 5 chars at 12px).
  // CA_BOTTOM must fit one row of x-tick labels ("Jan", "2024").
  // These never change, so the plot area never shifts.
  const CA_LEFT   = 68;
  const CA_TOP    = 18;
  const CA_RIGHT  = 14;   // right breathing room (from canvas right edge)
  const CA_BOTTOM = 30;   // x-axis height (from canvas bottom edge)

  /**
   * afterLayout plugin: forcibly lock the chartArea and scale positions after
   * Chart.js has finished its own layout math.  This is the only reliable way
   * to prevent the plot area from shifting as tick density changes.
   */
  const lockChartAreaPlugin = {
    id: "lockChartArea",
    afterLayout(chart) {
      const W = chart.width, H = chart.height;
      const L = CA_LEFT, T = CA_TOP, R = W - CA_RIGHT, B = H - CA_BOTTOM;

      const ca = chart.chartArea;
      ca.left = L;  ca.top = T;  ca.right = R;  ca.bottom = B;
      ca.width = R - L;  ca.height = B - T;

      const ys = chart.scales.y;
      if (ys) {
        ys.left = 0;  ys.right = L;  ys.width  = L;
        ys.top  = T;  ys.bottom = B; ys.height = B - T;
      }

      const xs = chart.scales.x;
      if (xs) {
        xs.left = L;  xs.right = R;  xs.width  = R - L;
        xs.top  = B;  xs.bottom = H; xs.height = H - B;
      }
    },
  };

  // ── Dataset builders ──────────────────────────────────────────────────────

  function buildMainDatasets(startMs, endMs) {
    const inWindow = points.filter(p => p.x >= startMs && p.x <= endMs);
    const sbPts  = inWindow.filter(p => p.sb);
    const regPts = inWindow.filter(p => !p.ivl);
    const ivlPts = inWindow.filter(p =>  p.ivl);
    const datasets = [];

    // Layer 3 — SB halos (gold ring, rendered first = behind everything)
    if (sbPts.length) {
      datasets.push({
        type:             "scatter",
        label:            "_halo",
        data:             sbPts.map(p => ({ x: p.x, y: p.y })),
        backgroundColor:  sbPts.map(() => "rgba(0,0,0,0)"),
        borderColor:      sbPts.map(() => "rgba(255,210,50,0.90)"),
        borderWidth:      2,
        pointRadius:      sbPts.map(p => p.r + 4),
        pointHoverRadius: sbPts.map(p => p.r + 6),
        order:            3,
      });
    }

    // Layer 2 — regular (non-interval) sessions: 33% fill + 1px opaque border
    if (regPts.length) {
      datasets.push({
        type:             "scatter",
        label:            "_regular",
        data:             regPts.map(p => ({
          x: p.x, y: p.y,
          date_str: p.date_str, dist_str: p.dist_str, is_sb: p.sb,
        })),
        backgroundColor:  regPts.map(p => p.c33),   // 33% opacity fill
        borderColor:      regPts.map(p => p.c),
        borderWidth:      1,
        pointRadius:      regPts.map(p => p.r),
        pointHoverRadius: regPts.map(p => p.r + 3),
        order:            2,
      });
    }

    // Layer 1 — interval sessions.
    //
    // Visual design:
    //   • Single circle; pointRadius = (r + r2) / 2; borderWidth = r - r2.
    //     Outer edge = r (total extent), inner edge = r2 (work extent).
    //   • Border (60% opacity, c60): the rest ring r2 → r.
    //   • Fill: diagonal hatch pattern.
    //       – Background (c25, 25% opacity): work area indicator.
    //       – Stripes  (c60, 60% opacity):  rest density indicator.
    //       – Stripe area ∝ restFraction → denser hatch = more rest.
    if (ivlPts.length) {
      datasets.push({
        type:             "scatter",
        label:            "_ivl",
        data:             ivlPts.map(p => ({
          x: p.x, y: p.y,
          date_str: p.date_str, dist_str: p.dist_str,
          work_m: p.work_m, rest_m: p.rest_m,
          ivl_desc: p.ivl_desc, rest_desc: p.rest_desc,
          is_ivl: true, is_sb: p.sb,
        })),
        backgroundColor: ivlPts.map(p => {
          const total = p.work_m + p.rest_m;
          const restFrac = total > 0 ? p.rest_m / total : 0;
          // p.c25    — hatch tile background (work area tint)
          // p.cHatch — hatch stripe color (rest indicator; independent from border)
          return makeHatchFill(p.c25, p.cHatch, restFrac);
        }),
        borderColor:      ivlPts.map(p => p.c60),  // 60% opacity ring (rest annulus)
        borderWidth:      ivlPts.map(p => Math.max(1, p.r - p.r2)),
        pointRadius:      ivlPts.map(p => (p.r + p.r2) / 2),
        pointHoverRadius: ivlPts.map(p => p.r + 3),
        order:            1,
      });
    }

    return datasets;
  }

  function buildOverviewDatasets(startMs, endMs) {
    const grey = isDark ? "rgba(130,130,130,0.35)" : "rgba(160,160,160,0.28)";
    const outPts = points.filter(p => p.x < startMs || p.x > endMs);
    const inPts  = points.filter(p => p.x >= startMs && p.x <= endMs);
    const datasets = [];

    if (outPts.length) {
      datasets.push({
        type:            "scatter",
        label:           "_ov_out",
        data:            outPts.map(p => ({ x: p.x, y: p.y })),
        backgroundColor: Array(outPts.length).fill(grey),
        borderColor:     Array(outPts.length).fill(grey),
        borderWidth:     0,
        pointRadius:     1.5,
        order:           2,
      });
    }

    if (inPts.length) {
      datasets.push({
        type:            "scatter",
        label:           "_ov_in",
        data:            inPts.map(p => ({ x: p.x, y: p.y })),
        backgroundColor: inPts.map(p => p.c70),
        borderColor:     inPts.map(p => p.c),
        borderWidth:     0.5,
        pointRadius:     2.5,
        order:           1,
      });
    }

    return datasets;
  }

  // ── Scale options ─────────────────────────────────────────────────────────

  function gridColor() {
    return isDark ? "rgba(255,255,255,0.09)" : "rgba(0,0,0,0.07)";
  }

  function buildMainXScale(startMs, endMs) {
    const spanDays = (endMs - startMs) / 86_400_000;

    return {
      type: "linear",
      min:  startMs,
      max:  endMs,
      grid: { color: gridColor() },
      afterBuildTicks(axis) {
        const ticks = [];
        const d = new Date(axis.min);

        if (spanDays <= 21) {
          // Daily ticks — advance to next midnight ≥ axis.min
          d.setHours(0, 0, 0, 0);
          if (d.getTime() < axis.min) d.setDate(d.getDate() + 1);
          while (d.getTime() <= axis.max) {
            ticks.push({ value: d.getTime() });
            d.setDate(d.getDate() + 1);
          }
        } else if (spanDays <= 90) {
          // Weekly ticks on Mondays
          d.setHours(0, 0, 0, 0);
          // Advance to next Monday ≥ axis.min
          const dow = d.getDay();  // 0=Sun … 6=Sat
          const toMon = dow === 1 ? 0 : (8 - dow) % 7;
          d.setDate(d.getDate() + toMon);
          while (d.getTime() <= axis.max) {
            ticks.push({ value: d.getTime() });
            d.setDate(d.getDate() + 7);
          }
        } else {
          // Monthly ticks — start from first month boundary ≥ axis.min
          d.setDate(1); d.setHours(0, 0, 0, 0);
          // Step forward until we're inside the window
          while (d.getTime() < axis.min) d.setMonth(d.getMonth() + 1);
          while (d.getTime() <= axis.max) {
            ticks.push({ value: d.getTime() });
            d.setMonth(d.getMonth() + 1);
          }
        }

        axis.ticks = ticks;
      },
      ticks: {
        maxRotation: 0,
        callback(val) {
          const d = new Date(val);
          if (spanDays <= 21) {
            // "Oct 8"
            return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
          }
          if (spanDays <= 90) {
            // "Oct 8" for first-of-month, else "Oct 8"
            return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
          }
          // Monthly: show year only on January, else abbreviated month
          return d.getMonth() === 0
            ? String(d.getFullYear())
            : d.toLocaleDateString("en-US", { month: "short" });
        },
      },
    };
  }

  function buildMainYScale() {
    const { yMin, yMax } = yRange();
    return {
      type:    "linear",
      min:     yMin,
      max:     yMax,
      reverse: false,   // slower pace (larger value) at top, faster at bottom
      grid:    { color: gridColor() },
      ticks:   {
        callback: (v) => formatPace(v),
        stepSize: 5,
      },
    };
  }

  function buildOverviewXScale() {
    const { minMs, maxMs } = dataRange();
    return {
      type: "linear",
      min:  minMs,
      max:  maxMs,
      grid: { color: "rgba(0,0,0,0)" },
      afterBuildTicks(axis) {
        const d = new Date(axis.min);
        d.setMonth(0); d.setDate(1); d.setHours(0, 0, 0, 0);
        if (d.getTime() < axis.min) d.setFullYear(d.getFullYear() + 1);
        const ticks = [];
        while (d.getTime() <= axis.max) {
          ticks.push({ value: d.getTime() });
          d.setFullYear(d.getFullYear() + 1);
        }
        axis.ticks = ticks;
      },
      ticks: {
        maxRotation: 0,
        font:     { size: 10 },
        callback: (val) => String(new Date(val).getFullYear()),
      },
    };
  }

  // ── Brush plugin ──────────────────────────────────────────────────────────

  const brushPlugin = {
    id: "sessionsBrush",
    afterDatasetsDraw(chart) {
      const xScale = chart.scales.x;
      if (!xScale) return;

      const { left, right, top, bottom } = chart.chartArea;
      const x1 = Math.max(left,  xScale.getPixelForValue(brushStartMs));
      const x2 = Math.min(right, xScale.getPixelForValue(brushEndMs));
      if (x2 <= x1) return;

      const ctx2d = chart.ctx;
      ctx2d.save();

      ctx2d.fillStyle = isDark ? "rgba(100,160,255,0.18)" : "rgba(50,120,230,0.13)";
      ctx2d.fillRect(x1, top, x2 - x1, bottom - top);

      const edgeColor = isDark ? "rgba(100,160,255,0.70)" : "rgba(50,120,230,0.60)";
      ctx2d.strokeStyle = edgeColor;
      ctx2d.lineWidth   = 1.5;
      ctx2d.beginPath(); ctx2d.moveTo(x1, top); ctx2d.lineTo(x1, bottom); ctx2d.stroke();
      ctx2d.beginPath(); ctx2d.moveTo(x2, top); ctx2d.lineTo(x2, bottom); ctx2d.stroke();

      ctx2d.restore();
    },
  };

  // ── Chart initialisation ──────────────────────────────────────────────────

  function initCharts() {
    if (mainChart)     { mainChart.destroy();     mainChart     = null; }
    if (overviewChart) { overviewChart.destroy(); overviewChart = null; }

    mainChart = new Chart(mainCanvas, {
      type: "scatter",
      data: { datasets: buildMainDatasets(brushStartMs, brushEndMs) },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        animation:           false,
        layout: { padding: 0 },
        scales: {
          x: buildMainXScale(brushStartMs, brushEndMs),
          y: buildMainYScale(),
        },
        plugins: {
          legend:  { display: false },
          tooltip: {
            mode:      "nearest",
            intersect: true,
            filter(item) {
              const lbl = item.dataset.label || "";
              return lbl === "_regular" || lbl === "_ivl";
            },
            callbacks: {
              title(items) {
                return items[0]?.raw?.date_str || "";
              },
              label(context) {
                const raw = context.raw;

                console.log("tooltip!", raw)

                if (raw.is_ivl) {
                  const lines = [];

                  // Line 1 — avg pace (most important physiological fact)
                  lines.push(`Avg pace  ${formatPace(raw.y)} / 500m`);

                  // Lines 2…N — interval structure (one line per block from Python)
                  // ivl_desc is a list of strings; each describes one structural block.
                  const desc = raw.ivl_desc;
                  if (Array.isArray(desc) && desc.length) {
                    for (const line of desc) lines.push(line);
                  }

                  // Totals footer — "Xm work  ·  Ym rest"
                  if (raw.rest_desc) lines.push(raw.rest_desc);

                  if (raw.is_sb) lines.push("★ Season Best");

                  return lines;
                }

                // Non-interval: single compact line
                const parts = [`${formatPace(raw.y)} / 500m`];
                if (raw.dist_str) parts.push(raw.dist_str);
                if (raw.is_sb)    parts.push("★ SB");
                return parts.join("  ·  ");
              },
            },
          },
        },
      },
      // lockChartAreaPlugin runs afterLayout to forcibly lock the chartArea
      // dimensions, preventing any shift as tick density or content changes.
      plugins: [lockChartAreaPlugin],
    });

    overviewChart = new Chart(overviewCanvas, {
      type: "scatter",
      data: { datasets: buildOverviewDatasets(brushStartMs, brushEndMs) },
      options: {
        responsive:          true,
        maintainAspectRatio: false,
        animation:           false,
        scales: {
          x: buildOverviewXScale(),
          y: {
            display: false,
            type:    "linear",
            reverse: false,
            ...(() => { const { yMin, yMax } = yRange(); return { min: yMin, max: yMax }; })(),
          },
        },
        plugins: {
          legend:  { display: false },
          tooltip: { enabled: false },
        },
      },
      plugins: [brushPlugin],
    });
  }

  // ── Window update ─────────────────────────────────────────────────────────

  function setWindow(startMs, endMs, { rebuild = true, report = false } = {}) {
    const { minMs, maxMs } = dataRange();
    const windowMs = endMs - startMs;

    if (startMs < minMs) { startMs = minMs; endMs = minMs + windowMs; }
    if (endMs   > maxMs) { endMs   = maxMs; startMs = maxMs - windowMs; }
    if (startMs < minMs)   startMs = minMs;

    brushStartMs = startMs;
    brushEndMs   = endMs;

    if (mainChart) {
      if (rebuild) {
        mainChart.data.datasets = buildMainDatasets(brushStartMs, brushEndMs);
      }
      mainChart.options.scales.x.min = brushStartMs;
      mainChart.options.scales.x.max = brushEndMs;
      mainChart.update("none");
    }

    if (overviewChart) {
      if (rebuild) {
        overviewChart.data.datasets = buildOverviewDatasets(brushStartMs, brushEndMs);
      }
      overviewChart.update("none");
    }

    if (report) {
      changeId++;
      ctx.updateProp("change_id",   changeId);
      ctx.updateProp("brush_start", Math.round(brushStartMs));
      ctx.updateProp("brush_end",   Math.round(brushEndMs));
    }
  }

  // ── Overview mouse / touch interactions ───────────────────────────────────

  function clientXonCanvas(e) {
    return (e.touches ? e.touches[0].clientX : e.clientX)
      - overviewCanvas.getBoundingClientRect().left;
  }

  function startDragOrJump(clientX) {
    if (!overviewChart) return;
    const xScale = overviewChart.scales.x;
    const x1 = xScale.getPixelForValue(brushStartMs);
    const x2 = xScale.getPixelForValue(brushEndMs);

    if (clientX >= x1 && clientX <= x2) {
      dragActive      = true;
      dragAnchorPx    = clientX;
      dragAnchorStart = brushStartMs;
      dragAnchorEnd   = brushEndMs;
      overviewWrap.classList.add("dragging");
    } else {
      const clickMs    = xScale.getValueForPixel(clientX);
      const halfWindow = (brushEndMs - brushStartMs) / 2;
      setWindow(clickMs - halfWindow, clickMs + halfWindow,
                { rebuild: true, report: true });
    }
  }

  function handleDragMove(clientX) {
    if (!dragActive || !overviewChart) return;
    const xScale  = overviewChart.scales.x;
    const pxRange = xScale.getPixelForValue(xScale.max) - xScale.getPixelForValue(xScale.min);
    const msRange = xScale.max - xScale.min;
    const deltaMs = pxRange > 0 ? ((clientX - dragAnchorPx) / pxRange) * msRange : 0;

    // Immediate scale-only update — smooth brush rect movement.
    setWindow(dragAnchorStart + deltaMs, dragAnchorEnd + deltaMs,
              { rebuild: false, report: false });

    // Debounced full rebuild of main chart datasets, 100ms after last movement.
    clearTimeout(pendingRebuild);
    pendingRebuild = setTimeout(() => {
      pendingRebuild = null;
      setWindow(brushStartMs, brushEndMs, { rebuild: true, report: false });
    }, 100);
  }

  function endDrag() {
    if (!dragActive) return;
    dragActive = false;
    overviewWrap.classList.remove("dragging");
    clearTimeout(pendingRebuild);
    pendingRebuild = null;
    setWindow(brushStartMs, brushEndMs, { rebuild: true, report: true });
  }

  overviewCanvas.addEventListener("mousedown",  (e) => { startDragOrJump(clientXonCanvas(e)); e.preventDefault(); });
  document.addEventListener(      "mousemove",  (e) => { if (dragActive) handleDragMove(clientXonCanvas(e)); });
  document.addEventListener(      "mouseup",    endDrag);

  overviewCanvas.addEventListener("touchstart", (e) => { startDragOrJump(clientXonCanvas(e)); e.preventDefault(); }, { passive: false });
  document.addEventListener(      "touchmove",  (e) => { if (dragActive) { handleDragMove(clientXonCanvas(e)); e.preventDefault(); } }, { passive: false });
  document.addEventListener(      "touchend",   endDrag);

  // ── Initial render ────────────────────────────────────────────────────────

  initCharts();

  // ── Prop updates from Python ──────────────────────────────────────────────

  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "points") {
      points = propValue || [];
      initCharts();
    } else if (propName === "target_window_start") {
      setWindow(propValue, brushEndMs, { rebuild: true, report: false });
    } else if (propName === "target_window_end") {
      setWindow(brushStartMs, propValue, { rebuild: true, report: false });
    } else if (propName === "is_dark") {
      isDark = !!propValue;
      initCharts();
    }
  });
});

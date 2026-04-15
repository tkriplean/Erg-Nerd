window.hyperdiv.registerPlugin("PowerCurveChart", (ctx) => {
  // --- Shadow DOM setup ---
  // The plugin renders into a shadow root; fill it completely.
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; height: 100%; }
    canvas { display: block; width: 100% !important; height: 100% !important; }
  `;
  ctx.domElement.appendChild(style);

  const canvas = document.createElement("canvas");
  ctx.domElement.appendChild(canvas);

  let chartInstance = null;

  // -----------------------------------------------------------------------
  // Formatters
  // -----------------------------------------------------------------------

  /** Format raw pace seconds as "M:SS.t" — e.g. 92.3 → "1:32.3" */
  function formatPace(seconds) {
    const s = Math.abs(seconds);
    const mins = Math.floor(s / 60);
    const secs = (s % 60).toFixed(1).padStart(4, "0");
    return `${mins}:${secs}`;
  }

  const DIST_LABELS = {
    100: "100m",
    500: "500m",
    1000: "1K",
    2000: "2K",
    5000: "5K",
    6000: "6K",
    10000: "10K",
    21097: "HM",
    42195: "Mar",
  };

  /** Return a human-friendly distance label for a meters value. */
  function distLabel(meters) {
    const rounded = Math.round(meters);
    if (DIST_LABELS[rounded]) return DIST_LABELS[rounded];
    if (meters >= 1000) {
      const k = meters / 1000;
      return (Number.isInteger(k) ? k : k.toFixed(1)) + "K";
    }
    return rounded + "m";
  }

  /** Format a duration in seconds as "M:SS" or "H:MM:SS". */
  function formatDuration(seconds) {
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    return `${m}:${String(sec).padStart(2, "0")}`;
  }

  // -----------------------------------------------------------------------
  // Config post-processing: attach JS callbacks that can't be serialised
  // -----------------------------------------------------------------------

  function buildOptions(options, showWatts, xMode, rankedDists, rankedDurations) {
    // Deep-clone so we never mutate the prop value.
    const opts = JSON.parse(JSON.stringify(options));
    const useDuration = xMode === "duration";
    const xLabelFn = useDuration ? formatDuration : distLabel;

    // Y-axis: formatter + gridline interval (≥1 per 5 sec pace, ≥1 per 50 W)
    if (opts.scales && opts.scales.y) {
      opts.scales.y.ticks = opts.scales.y.ticks || {};
      opts.scales.y.ticks.callback = showWatts
        ? (val) => Math.round(val) + "W"
        : (val) => formatPace(val);
      opts.scales.y.ticks.stepSize = showWatts ? 50 : 5;
    }

    // X-axis: formatter + gridlines pinned to ranked distances or durations.
    // Values come from Python (config._ranked_dists / config._ranked_durations).
    if (opts.scales && opts.scales.x) {
      opts.scales.x.ticks = opts.scales.x.ticks || {};
      opts.scales.x.ticks.callback = (val) => xLabelFn(val);
      const _xMin = opts.scales.x.min || 0;
      const _xMax = opts.scales.x.max || Infinity;
      const gridValues = useDuration ? rankedDurations : rankedDists;
      opts.scales.x.afterBuildTicks = (axis) => {
        axis.ticks = gridValues
          .filter(v => v >= _xMin && v <= _xMax)
          .map(v => ({ value: v }));
      };
    }

    // Custom tooltip
    opts.plugins = opts.plugins || {};
    opts.plugins.tooltip = {
      callbacks: {
        title(items) {
          if (!items.length) return "";
          const raw = items[0].raw;
          // Crossover point gets its own header.
          if (raw && raw._cp_crossover) return "Critical Power — Sprint/Endurance Crossover";
          // All other prediction points get a "Predicted" header instead of a date.
          if (items[0].dataset.isPrediction) return "Predicted";
          return (raw && raw.date) ? raw.date : "";
        },
        label(context) {
          const label = context.dataset.label || "";
          const raw = context.raw;
          const xStr = xLabelFn(raw.x);
          const valStr = showWatts
            ? raw.y.toFixed(1) + " W"
            : formatPace(raw.y) + " /500m";

          // Prediction points: show event label (if present) or x-axis label + value.
          if (context.dataset.isPrediction) {
            const evLabel = raw._event_label ? raw._event_label : xStr;
            return `${evLabel}  ·  ${valStr}`;
          }
          // Suppress tooltips for other internal overlay datasets.
          if (label.startsWith("_")) return null;

          return `${xStr}  ·  ${valStr}`;
        },
        afterLabel(context) {
          const raw = context.raw;
          if (raw && raw._cp_crossover && raw._t_label) {
            return [
              `At ${raw._t_label} effort duration`,
              "Fast-twitch and aerobic contributions are equal here.",
              "Shorter efforts → sprint-dominant",
              "Longer efforts → endurance-dominant",
            ];
          }
        },
      },
    };

    return opts;
  }

  // -----------------------------------------------------------------------
  // Canvas labels plugin — draws multi-line labels below nominated data points.
  // Label data: chart.config._canvas_labels  [{x, y, line_event, pct_pace,
  //   pct_watts, line_label, color, bold}]
  // Lines are assembled dynamically so watts / pace mode is handled here.
  // Overlapping labels are pushed downward before drawing.
  // -----------------------------------------------------------------------

  const _UI_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";

  const canvasLabelsPlugin = {
    id: "canvasLabels",
    // Stable label positions keyed by identity string (x data value + label type).
    // Persists across redraws so labels don't jump when other labels appear/disappear.
    _offsets: new Map(),

    afterDatasetsDraw(chart) {
      const rawLabels = chart.config._canvas_labels;
      if (!rawLabels || !rawLabels.length) {
        this._offsets.clear();
        return;
      }

      // Split into standard PB-overlay labels and bottom-anchored crossover labels.
      const standardRawLabels = rawLabels.filter(l => !l._anchor);
      const bottomRawLabels   = rawLabels.filter(l => l._anchor === "bottom");

      const ctx2d   = chart.ctx;
      const xScale  = chart.scales.x;
      const yScale  = chart.scales.y;
      const showW   = props.show_watts;  // closed over from outer scope

      const DOT_OFFSET = 20;   // px from dot centre to nearest label edge
      const LINE_H     = 15;   // px between lines
      const H_PAD      = 5;    // horizontal padding around text for overlap box
      const V_GAP      = 4;    // minimum gap between adjacent labels

      ctx2d.save();
      ctx2d.textAlign    = "center";
      ctx2d.textBaseline = "top";

      // Watts mode → labels float above the dot; pace mode → below.
      const above = showW;

      // ---- Build placed-label objects (standard labels only) ----
      // offsetY is signed distance from dot-centre to label top edge.
      // Labels already in _offsets reuse their stored position (anchors).
      // Labels appearing for the first time start at the default position.
      const placed = standardRawLabels.map(({ x, y, line_event, pct_pace, pct_watts, line_label, color, bold }) => {
        const pctStr = showW
          ? (pct_watts > 0 ? `+${pct_watts.toFixed(1)}% power` : null)
          : (pct_pace  > 0 ? `${pct_pace.toFixed(1)}% faster` : null);
        const lines = [line_event, ...(pctStr ? [pctStr] : []), line_label];
        const h = lines.length * LINE_H;
        const initOffset = above ? -(DOT_OFFSET + h) : DOT_OFFSET;
        const key = `${x}|${line_label}`;
        const stored = this._offsets.get(key);
        return {
          key,
          px: xScale.getPixelForValue(x),
          py: yScale.getPixelForValue(y),
          lines,
          color,
          bold,
          offsetY: stored !== undefined ? stored : initOffset,
          isNew:   stored === undefined,
        };
      });

      // ---- Measure bounding box using actual canvas text metrics ----
      function fontFor(bold, isLastLine) {
        return `${bold && isLastLine ? "bold " : ""}12px ${_UI_FONT}`;
      }

      function boxOf(item) {
        let maxW = 0;
        item.lines.forEach((line, i) => {
          ctx2d.font = fontFor(item.bold, i === item.lines.length - 1);
          const w = ctx2d.measureText(line).width;
          if (w > maxW) maxW = w;
        });
        const h = item.lines.length * LINE_H;
        return {
          x1: item.px - maxW / 2 - H_PAD,
          x2: item.px + maxW / 2 + H_PAD,
          y1: item.py + item.offsetY,
          y2: item.py + item.offsetY + h,
        };
      }

      function overlaps(a, b) {
        return a.x1 < b.x2 && a.x2 > b.x1 && a.y1 < b.y2 && a.y2 > b.y1;
      }

      // ---- Place new labels to avoid anchors ----
      // Anchors (already have a stored position) never move.
      // Newcomers are placed one by one, avoiding all anchors and prior newcomers.
      // Above mode → push up; below → push down.
      const allPlaced = placed.filter(p => !p.isNew);
      for (const item of placed.filter(p => p.isNew)) {
        let changed = true;
        while (changed) {
          changed = false;
          for (const anchor of allPlaced) {
            const bi = boxOf(item);
            const bj = boxOf(anchor);
            if (overlaps(bi, bj)) {
              if (above) item.offsetY -= bi.y2 - bj.y1 + V_GAP;
              else       item.offsetY += bj.y2 - bi.y1 + V_GAP;
              changed = true;
              break;
            }
          }
        }
        allPlaced.push(item);
        this._offsets.set(item.key, item.offsetY);
      }

      // Evict stored positions for labels that are no longer present.
      const currentKeys = new Set(placed.map(p => p.key));
      for (const key of this._offsets.keys()) {
        if (!currentKeys.has(key)) this._offsets.delete(key);
      }

      // ---- Draw standard labels ----
      placed.forEach(({ px, py, lines, color, bold, offsetY }) => {
        lines.forEach((line, i) => {
          ctx2d.font      = fontFor(bold, i === lines.length - 1);
          ctx2d.fillStyle = color || "rgba(180,180,180,0.9)";
          ctx2d.fillText(line, px, py + offsetY + i * LINE_H);
        });
      });

      // ---- Draw bottom-anchored labels (crossover annotation) ----
      // These are pinned to the chart bottom and never participate in overlap logic.
      if (bottomRawLabels.length) {
        const bottomY = chart.chartArea.bottom - 8;
        ctx2d.font = `12px ${_UI_FONT}`;
        bottomRawLabels.forEach(({ x, lines, color }) => {
          const px = xScale.getPixelForValue(x);
          // Draw lines from the bottom up: last line at bottomY, first line highest.
          lines.forEach((line, i) => {
            const yPos = bottomY - (lines.length - 1 - i) * LINE_H;
            ctx2d.fillStyle = color || "rgba(180,180,180,0.9)";
            ctx2d.fillText(line, px, yPos);
          });
        });
      }

      ctx2d.restore();
    },
  };

  // -----------------------------------------------------------------------
  // Chart lifecycle
  // -----------------------------------------------------------------------

  function applyConfig(config, showWatts) {
    if (!config) return;
    const xMode = (config._x_mode) || "distance";
    const rankedDists = config._ranked_dists || [100, 500, 1000, 2000, 5000, 6000, 10000, 21097, 42195];
    const rankedDurations = config._ranked_durations || [10, 60, 120, 240, 600, 1800, 3600, 7200];
    const processedOpts = buildOptions(config.options, showWatts, xMode, rankedDists, rankedDurations);
    const canvasLabels = config._canvas_labels || [];

    if (chartInstance) {
      // Update in place — avoids the flash of an empty canvas on re-render.
      chartInstance.data = config.data;
      chartInstance.options = processedOpts;
      chartInstance.config._canvas_labels = canvasLabels;
      chartInstance.update("none");
    } else {
      chartInstance = new Chart(canvas, {
        type: config.type,
        data: config.data,
        options: processedOpts,
        plugins: [canvasLabelsPlugin],
      });
      chartInstance.config._canvas_labels = canvasLabels;
    }
  }

  // -----------------------------------------------------------------------
  // Initialise and respond to Python prop updates
  // -----------------------------------------------------------------------

  let props = {
    config: ctx.initialProps.config || null,
    show_watts: ctx.initialProps.show_watts || false,
  };

  applyConfig(props.config, props.show_watts);

  ctx.onPropUpdate((propName, propValue) => {
    props[propName] = propValue;
    applyConfig(props.config, props.show_watts);
  });
});

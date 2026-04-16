window.hyperdiv.registerPlugin("PowerCurveChart", (ctx) => {
  // --- Shadow DOM setup ---
  // Flex column: transport row (fixed height) on top, canvas (fills rest) below.
  // The transport row is itself a flex row: [Play btn] [Speed btn] [timeline scrubber →]
  const style = document.createElement("style");
  style.textContent = `
    :host {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
      --thumb-size: 22px;
      --tip-bg: var(--sl-tooltip-background-color, #1e293b);
      --tip-fg: var(--sl-tooltip-color, #fff);
    }
    canvas { display: block; width: 100% !important; flex: 1; min-height: 0; }

    /* ── Transport row: play/speed buttons + scrubber in one flex row ──────── */
    .transport {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      flex-shrink: 0;
      padding: 4px 0px 0px 0px;
      width: 100%;
      box-sizing: border-box;
    }

    /* ── Timeline scrubber (grows to fill the transport row) ─────────────── */
    .timeline {
      flex: 1;
      min-width: 0;
      position: relative;
      /* Symmetric top/bottom padding so the row centre aligns with the track,
         giving align-items:center the right anchor point for the buttons. */
      padding: 36px 18px 36px 0px;
    }
    .tip {
      position: absolute;
      bottom: calc(-50% - 8px);
      transform: translateX(-50%);
      background: var(--tip-bg);
      color: var(--tip-fg);
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-family: var(--sl-font-sans, system-ui, sans-serif);
      white-space: nowrap;
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.12s;
      z-index: 9999;
    }
    .tip.show { opacity: 1; }
    .ann-row {
      position: absolute;
      height: 16px;
      margin-top: 9px;
    }
    .dot {
      position: absolute;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      transform: translateX(-50%);
      cursor: pointer;
      top: 0px;
      border: 1px solid var(--sl-color-neutral-0);
      box-sizing: border-box;
      transition: transform 0.1s;
    }
    .dot:hover { transform: translateX(-50%) scale(1.45); }
  `;
  ctx.domElement.appendChild(style);

  // --- Transport row (play/speed buttons + scrubber, all above the canvas) ---
  const transportRow = document.createElement("div");
  transportRow.className = "transport";
  ctx.domElement.appendChild(transportRow);

  // Play/Pause button
  const btnPlay = document.createElement("sl-button");
  btnPlay.setAttribute("size", "medium");
  btnPlay.setAttribute("variant", "primary");
  btnPlay.textContent = "▶  Play";
  transportRow.appendChild(btnPlay);

  // Speed cycle button — JS owns speed state after init
  const SPEED_OPTIONS = ["0.5x", "1x", "4x", "16x"];
  let speedIdx = Math.max(0, SPEED_OPTIONS.indexOf(ctx.initialProps.sim_speed || "1x"));
  const btnSpeed = document.createElement("sl-button");
  btnSpeed.setAttribute("size", "medium");
  btnSpeed.setAttribute("variant", "neutral");
  btnSpeed.textContent = SPEED_OPTIONS[speedIdx];
  transportRow.appendChild(btnSpeed);

  // Timeline scrubber (flex: 1 — fills the remainder of the transport row)
  const tlWrap = document.createElement("div");
  tlWrap.className = "timeline";
  transportRow.appendChild(tlWrap);

  let tlStartDate = ctx.initialProps.timeline_start_date
    ? new Date(ctx.initialProps.timeline_start_date + "T00:00:00")
    : new Date();

  function tlFormatDate(dayOffset) {
    const d = new Date(tlStartDate);
    d.setDate(d.getDate() + Number(dayOffset));
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  const tlInput = document.createElement("sl-range");
  tlInput.min   = ctx.initialProps.timeline_min ?? 0;
  tlInput.max   = ctx.initialProps.timeline_max ?? 100;
  tlInput.step  = 1;
  tlInput.value = 0;
  tlInput.tooltipFormatter = tlFormatDate;
  tlWrap.appendChild(tlInput);

  const tlTip = document.createElement("div");
  tlTip.className = "tip";
  tlWrap.appendChild(tlTip);

  const tlAnnRow = document.createElement("div");
  tlAnnRow.className = "ann-row";
  tlWrap.appendChild(tlAnnRow);

  // --- Canvas (below the scrubber) ---
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
  // Timeline scrubber — helpers, event wiring, and seek handler
  // -----------------------------------------------------------------------

  let tlAnnotations = Array.isArray(ctx.initialProps.timeline_annotations)
    ? ctx.initialProps.timeline_annotations
    : [];
  let tlAnnHoverActive = false;

  function tlUpdateFill() {
    const min = Number(tlInput.min);
    const max = Number(tlInput.max);
    const val = Number(tlInput.value);
    const pct = max > min ? (val - min) / (max - min) * 100 : 0;
    tlInput.style.background =
      `linear-gradient(to right, var(--fill-color) ${pct}%, var(--track-color) ${pct}%)`;
  }

  function tlBuildDots() {
    tlAnnRow.innerHTML = "";
    tlAnnHoverActive = false;
    const min      = Number(tlInput.min);
    const max      = Number(tlInput.max);
    const halfThumb = 11;
    const trackW   = tlInput.offsetWidth || 200;
    tlAnnotations.forEach((ann) => {
      if (ann.day < min || ann.day > max) return;
      const pct  = max > min ? (ann.day - min) / (max - min) : 0;
      const left = halfThumb + pct * (trackW - 2 * halfThumb);
      const dot  = document.createElement("div");
      dot.className    = "dot";
      dot.style.left   = left + "px";
      dot.style.background = ann.color;
      dot.addEventListener("mouseenter", () => {
        tlAnnHoverActive = true;
        tlTip.textContent = ann.label;
        tlTip.style.left  = left + "px";
        tlTip.classList.add("show");
      });
      dot.addEventListener("mouseleave", () => {
        tlAnnHoverActive = false;
        tlTip.classList.remove("show");
      });
      dot.addEventListener("click", (e) => {
        e.stopPropagation();
        // Seek to one day before the SB so the SB appears on the next step.
        const seekDay = Math.max(Number(tlInput.min), ann.day - 1);
        tlHandleSeek(seekDay);
      });
      tlAnnRow.appendChild(dot);
    });
  }

  /** Move the scrubber thumb to the given day without triggering a seek. */
  function tlSetThumb(day) {
    tlInput.value = day;
    tlUpdateFill();
  }

  /**
   * User-initiated seek: pause animation (if running), jump to day, re-render,
   * then resume if the animation was previously playing.  Entirely JS-side —
   * no Python round-trip required.
   */
  function tlHandleSeek(day) {
    const wasPlaying = intervalId !== null;
    pauseAnimation();
    currentDay = day;
    if (cachedBundle) {
      tick_noadvance();  // renders chart, updates thumb, sends sim_day_out
    } else {
      tlSetThumb(day);
      ctx.updateProp("sim_day_out", currentDay);
    }
    if (wasPlaying) startAnimation();
  }

  // Debounced drag send + immediate flush on release.
  let tlDebounceTimer = null;
  tlInput.addEventListener("input", () => {
    clearTimeout(tlDebounceTimer);
    tlDebounceTimer = setTimeout(() => tlHandleSeek(Number(tlInput.value)), 250);
  });
  tlInput.addEventListener("change", () => {
    clearTimeout(tlDebounceTimer);
    tlHandleSeek(Number(tlInput.value));
  });

  // Hide the date tooltip when the drag ends.
  document.addEventListener("mouseup",  () => { if (!tlAnnHoverActive) tlTip.classList.remove("show"); });
  document.addEventListener("touchend", () => { if (!tlAnnHoverActive) tlTip.classList.remove("show"); });

  // Reposition dots when the track width changes (e.g. window resize).
  const tlResizeObserver = new ResizeObserver(() => { tlBuildDots(); tlUpdateFill(); });
  tlResizeObserver.observe(tlInput);

  tlUpdateFill();
  tlBuildDots();

  // -----------------------------------------------------------------------
  // Transport buttons — Play/Pause and Speed
  // -----------------------------------------------------------------------

  let isPlaying = false;
  let rewindDay = ctx.initialProps.rewind_day ?? 0;

  function updatePlayButton() {
    if (isPlaying) {
      btnPlay.textContent = "⏸  Pause";
      btnPlay.setAttribute("variant", "default");
    } else {
      btnPlay.textContent = "▶  Play";
      btnPlay.setAttribute("variant", "primary");
    }
  }

  btnPlay.addEventListener("click", () => {
    console.log("PLAY PRESSED", isPlaying)
    if (isPlaying) {
      isPlaying = false;
      pauseAnimation();
      updatePlayButton();
      ctx.updateProp("sim_playing_out", false);
    } else {
      // If at the end of the timeline, rewind before starting.
      if (currentDay >= Number(tlInput.max)) {
        currentDay = rewindDay;
        tlSetThumb(currentDay);
        ctx.updateProp("sim_day_out", currentDay);
      }
      isPlaying = true;
      if (cachedBundle) startAnimation();
      updatePlayButton();
      ctx.updateProp("sim_playing_out", true);
      console.log(cachedBundle)
    }
  });

  btnSpeed.addEventListener("click", () => {
    speedIdx = (speedIdx + 1) % SPEED_OPTIONS.length;
    currentStepDays = SPEED_DAYS[SPEED_OPTIONS[speedIdx]] || 7;
    btnSpeed.textContent = SPEED_OPTIONS[speedIdx];
  });

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
  // Animation bundle — dataset builders
  // -----------------------------------------------------------------------

  /** Binary search: return last keyframe where kf.day <= currentDay. */
  function findKeyframe(keyframes, currentDay) {
    let lo = 0, hi = keyframes.length - 1, result = keyframes[0];
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (keyframes[mid].day <= currentDay) {
        result = keyframes[mid];
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return result;
  }

  /**
   * Build per-season scatter datasets from the workout manifest.
   * Returns an array of Chart.js dataset dicts.
   */
  function buildScatterDatasets(manifest, cutoffDay, bundle, showWatts) {

    console.log("BUILDING DATASETS", cutoffDay)
    const seasonMeta = bundle.season_meta;
    const pbColor = bundle.pb_color;
    const drawLifetimeLine = bundle.draw_lifetime_line;
    const drawSeasonLines  = bundle.draw_season_lines;

    // Filter workouts to those at or before cutoffDay.
    const visible = manifest.filter(w => w.day <= cutoffDay);

    // For each season: collect points, compute season bests, compute lifetime bests.
    // lifetime_best_pace / _watts come from the keyframe, but we need them here
    // for colouring — we'll compute from the visible manifest for independence.
    // (Keyframe lifetime_best is used later for line drawing.)
    const bySeason = new Map();  // season_idx -> [entry, ...]
    for (const w of visible) {
      if (w.excluded) continue;
      if (!bySeason.has(w.season_idx)) bySeason.set(w.season_idx, []);
      bySeason.get(w.season_idx).push(w);
    }

    // Compute lifetime bests across all visible non-excluded workouts.
    const lbPace = {};  // cat_key_str -> best (lowest) pace
    for (const w of visible) {
      if (w.excluded) continue;
      if (!(w.cat_key_str in lbPace) || w.y_pace < lbPace[w.cat_key_str]) {
        lbPace[w.cat_key_str] = w.y_pace;
      }
    }

    // Season bests: (season_idx, cat_key_str) -> best pace
    const sbPace = {};
    for (const w of visible) {
      if (w.excluded) continue;
      const k = `${w.season_idx}|${w.cat_key_str}`;
      if (!(k in sbPace) || w.y_pace < sbPace[k]) sbPace[k] = w.y_pace;
    }

    const datasets = [];

    // Iterate seasons in order.
    for (let si = 0; si < seasonMeta.length; si++) {
      const meta = seasonMeta[si];
      const pts  = bySeason.get(si) || [];

      // Excluded points for this season.
      const exclPts = manifest.filter(w => w.day <= cutoffDay && w.excluded && w.season_idx === si);

      if (!pts.length && !exclPts.length) continue;

      const data = [], bg = [], border = [], bw = [], radii = [];

      // Included points
      for (const w of pts) {
        const yVal = showWatts ? w.y_watts : w.y_pace;
        const isLb = Math.abs(w.y_pace - (lbPace[w.cat_key_str] ?? Infinity)) < 1e-9;
        const isSb = Math.abs(w.y_pace - (sbPace[`${si}|${w.cat_key_str}`] ?? Infinity)) < 1e-9;
        const alpha = (isLb || isSb) ? 1.0 : 0.40;
        data.push({ x: w.x, y: yVal, date: w.date_label, wtype: w.wtype });
        bg.push(colorWithAlpha(meta.color, alpha));
        if (isLb) {
          border.push(pbColor);
          bw.push(2.5);
          radii.push(6);
        } else {
          border.push(colorWithAlpha(meta.border_color, Math.min(alpha + 0.15, 1.0)));
          bw.push(1);
          radii.push(5);
        }
      }

      // Excluded points (faint)
      for (const w of exclPts) {
        const yVal = showWatts ? w.y_watts : w.y_pace;
        data.push({ x: w.x, y: yVal, date: w.date_label, wtype: w.wtype });
        bg.push(colorWithAlpha(meta.color, 0.18));
        border.push(colorWithAlpha(meta.border_color, 0.25));
        bw.push(0.5);
        radii.push(4);
      }

      if (!data.length) continue;
      datasets.push({
        type: "scatter",
        label: `Season ${meta.label}`,
        data,
        backgroundColor: bg,
        borderColor: border,
        borderWidth: bw,
        pointRadius: radii,
        pointHoverRadius: 8,
        order: 1,
      });
    }

    // ── Lifetime best line ───────────────────────────────────────────────────
    if (drawLifetimeLine) {
      const lbPts = [];
      const seen = new Set();
      for (const w of visible) {
        if (w.excluded) continue;
        const ck = w.cat_key_str;
        if (!seen.has(ck) && Math.abs(w.y_pace - (lbPace[ck] ?? Infinity)) < 1e-9) {
          lbPts.push({ x: w.x, y: showWatts ? w.y_watts : w.y_pace });
          seen.add(ck);
        }
      }
      lbPts.sort((a, b) => a.x - b.x);
      if (lbPts.length) {
        datasets.push({
          type: "line",
          label: "Lifetime Bests",
          data: lbPts,
          borderColor: pbColor,
          backgroundColor: "rgba(0,0,0,0)",
          borderWidth: 7,
          pointRadius: 0,
          tension: 0.15,
          order: 3,
        });
      }
    }

    // ── Season best lines ────────────────────────────────────────────────────
    if (drawSeasonLines) {
      for (let si = 0; si < seasonMeta.length; si++) {
        const meta = seasonMeta[si];
        const pts = bySeason.get(si) || [];
        if (!pts.length) continue;
        const seenCk = new Set();
        const sPts = [];
        for (const w of pts) {
          const k = `${si}|${w.cat_key_str}`;
          if (!seenCk.has(w.cat_key_str) && Math.abs(w.y_pace - (sbPace[k] ?? Infinity)) < 1e-9) {
            sPts.push({ x: w.x, y: showWatts ? w.y_watts : w.y_pace });
            seenCk.add(w.cat_key_str);
          }
        }
        sPts.sort((a, b) => a.x - b.x);
        if (!sPts.length) continue;
        datasets.push({
          type: "line",
          label: `Season ${meta.label}`,
          data: sPts,
          borderColor: meta.color,
          backgroundColor: "rgba(0,0,0,0)",
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.15,
          order: 2,
        });
      }
    }

    return datasets;
  }

  /**
   * Build overlay datasets (ghost dots + arrows) for the lookahead window.
   * Returns { overlayDatasets, canvasLabels }.
   */
  function buildOverlayDatasets(manifest, currentDay, stepDays, bundle, showWatts) {
    const lookaheadEnd = currentDay + 4 * stepDays;
    const isLight = !bundle.is_dark;
    const labelColor = bundle.is_dark ? "rgba(240,240,240,0.92)" : "rgba(40,40,40,0.88)";
    const arrowColor = bundle.is_dark ? "rgba(240,240,240,0.35)" : "rgba(40,40,40,0.35)";
    const seasonMeta = bundle.season_meta;

    // Current best pace per cat_key_str (from visible non-excluded workouts).
    const curBestPace = {};
    const curBestX = {};
    for (const w of manifest) {
      if (w.day > currentDay || w.excluded) continue;
      if (!(w.cat_key_str in curBestPace) || w.y_pace < curBestPace[w.cat_key_str]) {
        curBestPace[w.cat_key_str] = w.y_pace;
        curBestX[w.cat_key_str] = w.x;
      }
    }

    // Find upcoming workouts that beat the current best.
    const upcoming = manifest
      .filter(w => w.day > currentDay && w.day <= lookaheadEnd && !w.excluded)
      .sort((a, b) => a.y_pace - b.y_pace);

    const arrows = [];
    const ghostPts = [];
    const seenThreatCats = new Set();

    for (const w of upcoming) {
      const ck = w.cat_key_str;
      if (
        ck in curBestPace &&
        w.y_pace < curBestPace[ck] &&
        !seenThreatCats.has(ck)
      ) {
        arrows.push({
          fromX: curBestX[ck],
          fromPace: curBestPace[ck],
          toX: w.x,
          toPace: w.y_pace,
          toWatts: w.y_watts,
          toSeasonIdx: w.season_idx,
          eventLine: w.event_line,
        });
        ghostPts.push({
          x: w.x,
          y: showWatts ? w.y_watts : w.y_pace,
          si: w.season_idx,
        });
        seenThreatCats.add(ck);
      }
    }

    const overlayDatasets = [];

    // Ghost dots
    if (ghostPts.length) {
      const gBg = [], gBorder = [];
      for (const gp of ghostPts) {
        const meta = seasonMeta[gp.si] || seasonMeta[0];
        gBg.push(colorWithAlpha(meta.color, 0.22));
        gBorder.push(colorWithAlpha(meta.border_color, 0.45));
      }
      overlayDatasets.push({
        type: "scatter",
        label: "_ghost",
        data: ghostPts.map(gp => ({ x: gp.x, y: gp.y })),
        backgroundColor: gBg,
        borderColor: gBorder,
        borderWidth: 1,
        pointRadius: 5,
        pointHoverRadius: 7,
        order: 0,
      });
    }

    // Arrows (dashed lines from current best → upcoming)
    for (const arr of arrows) {
      const fromY = showWatts ? wattsFromPace(arr.fromPace) : arr.fromPace;
      const toY   = showWatts ? arr.toWatts : arr.toPace;
      overlayDatasets.push({
        type: "line",
        label: "_arrow",
        data: [
          { x: arr.fromX, y: fromY },
          { x: arr.toX,   y: toY   },
        ],
        borderColor: arrowColor,
        backgroundColor: "rgba(0,0,0,0)",
        borderWidth: 1.5,
        borderDash: [5, 4],
        pointRadius: 0,
        tension: 0,
        order: 0,
      });
    }

    // Canvas labels for upcoming PBs
    const canvasLabels = [];
    for (const arr of arrows) {
      const fromY = showWatts ? wattsFromPace(arr.fromPace) : arr.fromPace;
      const toY   = showWatts ? arr.toWatts : arr.toPace;
      const pp = arr.fromPace > arr.toPace
        ? (arr.fromPace - arr.toPace) / arr.fromPace * 100
        : 0;
      const pw = arr.fromPace > arr.toPace
        ? (wattsFromPace(arr.toPace) - wattsFromPace(arr.fromPace)) / wattsFromPace(arr.fromPace) * 100
        : 0;
      canvasLabels.push({
        x: arr.toX,
        y: toY,
        line_event: arr.eventLine,
        pct_pace:  Math.round(pp * 10) / 10,
        pct_watts: Math.round(pw * 10) / 10,
        line_label: "upcoming PB",
        color: labelColor,
        bold: false,
      });
    }

    return { overlayDatasets, canvasLabels };
  }

  // -----------------------------------------------------------------------
  // Colour manipulation helper
  // -----------------------------------------------------------------------

  /**
   * Replace the alpha channel in an hsla/rgba string.
   * Handles strings like "hsla(220,70%,55%,0.90)" and "rgba(240,240,240,0.92)".
   */
  function colorWithAlpha(colorStr, alpha) {
    return colorStr.replace(
      /(hsla|rgba)\(([^,]+),([^,]+),([^,]+),[^)]+\)/,
      (_, fn, a, b, c) => `${fn}(${a},${b},${c},${alpha.toFixed(2)})`
    );
  }

  /** Convert pace (sec/500m) to watts using the standard formula. */
  function wattsFromPace(pace) {
    // 2.80 / pace^3  (Concept2 formula, pace in sec/500m, result in watts)
    return 2.80 / Math.pow(pace / 500, 3);
  }

  // -----------------------------------------------------------------------
  // Sim axes options (built once per bundle, reused every tick)
  // -----------------------------------------------------------------------

  function buildSimOptions(bundle, showWatts) {
    const xMode = bundle.x_mode || "distance";
    const useDuration = xMode === "duration";
    const xLabelFn = useDuration ? formatDuration : distLabel;
    const rankedDists     = [100, 500, 1000, 2000, 5000, 6000, 10000, 21097, 42195];
    const rankedDurations = [10, 60, 120, 240, 600, 1800, 3600, 7200];

    // Use the same axis bounds and types as the static chart so the view is
    // stable during animation.
    const xBounds = bundle.x_bounds;   // [min, max] or null
    const yBounds = bundle.y_bounds;   // [min, max] or null
    const logX    = bundle.log_x === true;
    // Do NOT set reverse — let Chart.js default to false, matching the static chart.

    const xScaleOpts = {
      type: logX ? "logarithmic" : "linear",
      ticks: { callback: (val) => xLabelFn(val) },
      afterBuildTicks: (axis) => {
        const gridValues = useDuration ? rankedDurations : rankedDists;
        const [xMin, xMax] = xBounds || [0, Infinity];
        axis.ticks = gridValues
          .filter(v => v >= xMin && v <= xMax)
          .map(v => ({ value: v }));
      },
    };
    if (xBounds) { xScaleOpts.min = xBounds[0]; xScaleOpts.max = xBounds[1]; }

    const yScaleOpts = {
      type: "linear",
      ticks: {
        callback: showWatts ? (v) => Math.round(v) + "W" : (v) => formatPace(v),
        stepSize: showWatts ? 50 : 5,
      },
    };
    if (yBounds) { yScaleOpts.min = yBounds[0]; yScaleOpts.max = yBounds[1]; }

    const opts = {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: xScaleOpts, y: yScaleOpts },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title(items) {
              if (!items.length) return "";
              const raw = items[0].raw;
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
              if (context.dataset.isPrediction) return `${xStr}  ·  ${valStr}`;
              if (label.startsWith("_")) return null;
              return `${xStr}  ·  ${valStr}`;
            },
          },
        },
      },
      layout: { padding: 16 },
    };
    return opts;
  }

  // -----------------------------------------------------------------------
  // Animation state
  // -----------------------------------------------------------------------

  let cachedBundle    = null;   // last bundle applied (keyed by bundle_key)
  let intervalId      = null;   // setInterval handle
  let currentDay      = 0;
  let currentStepDays = 7;      // days advanced per tick
  let pbBadgeCountdown = 0;     // steps remaining for PB badge display
  let pbBadgeLabels   = [];     // canvas label dicts while badge is showing
  let simDoneCounter  = 0;      // monotonically incremented on completion
  let lastKfDay       = -1;     // tracks which keyframe was last processed for PB badge

  const SPEED_DAYS = { "0.5x": 1, "1x": 7, "4x": 30, "16x": 91 };
  const TICK_MS = 350;

  function applyBundle(bundle) {
    cachedBundle     = bundle;
    currentDay       = bundle.start_day || 0;
    lastKfDay        = -1;
    pbBadgeCountdown = 0;
    pbBadgeLabels    = [];

    // Sync scrubber range to the bundle timeline.
    tlInput.min = 0;
    tlInput.max = bundle.total_days;
    tlSetThumb(currentDay);

    const showW   = props.show_watts;
    const simOpts = buildSimOptions(bundle, showW);

    if (!chartInstance) {
      chartInstance = new Chart(canvas, {
        type: "scatter",
        data: { datasets: [] },
        options: simOpts,
        plugins: [canvasLabelsPlugin],
      });
      chartInstance.config._canvas_labels = [];
    } else {
      // Sync axis settings to match the bundle (bounds, scale type, reverse).
      // This ensures the sim view is stable and matches the static chart config.
      chartInstance.options = simOpts;
      chartInstance.config._canvas_labels = [];
      chartInstance.update("none");
    }
  }

  function tick() {
    if (!cachedBundle) return;
    const bundle   = cachedBundle;
    const showW    = props.show_watts;
    const manifest = bundle.workout_manifest;
    const kf       = findKeyframe(bundle.keyframes, currentDay);

    // ── Scatter + best lines ─────────────────────────────────────────────────
    const scatterDs = buildScatterDatasets(manifest, currentDay, bundle, showW);

    // ── Prediction datasets ──────────────────────────────────────────────────
    const predDs = kf.pred_datasets || [];

    // ── Overlay datasets ─────────────────────────────────────────────────────
    const { overlayDatasets, canvasLabels: overlayLabels } =
      buildOverlayDatasets(manifest, currentDay, currentStepDays, bundle, showW);

    // ── PB badge ─────────────────────────────────────────────────────────────
    // Trigger when we enter a new keyframe (kf.day changed since last tick).
    // We can't check kf.day === currentDay because steps may skip over it.
    if (kf.day > lastKfDay && kf.new_pb_labels && kf.new_pb_labels.length) {
      lastKfDay = kf.day;
      pbBadgeLabels    = kf.new_pb_labels.map(lbl => ({
        x:          lbl.x,
        y:          showW ? lbl.y_watts : lbl.y_pace,
        line_event: lbl.line_event,
        pct_pace:   lbl.pct_pace,
        pct_watts:  lbl.pct_watts,
        line_label: lbl.line_label,
        color:      bundle.pb_color,
        bold:       true,
      }));
      pbBadgeCountdown = bundle.pb_badge_lifetime_steps;
    } else if (kf.day > lastKfDay) {
      lastKfDay = kf.day;
    }

    // Merge PB badge labels with upcoming-PB overlay labels and any
    // predictor canvas labels (e.g. CP fast/slow-twitch crossover annotation).
    let allCanvasLabels = [...overlayLabels];
    if (pbBadgeCountdown > 0) {
      allCanvasLabels = [...pbBadgeLabels, ...allCanvasLabels];
      pbBadgeCountdown--;
    }
    if (kf.pred_canvas_labels && kf.pred_canvas_labels.length) {
      allCanvasLabels = [...allCanvasLabels, ...kf.pred_canvas_labels];
    }

    // ── Update chart ─────────────────────────────────────────────────────────
    chartInstance.data.datasets = [
      ...scatterDs,
      ...predDs,
      ...overlayDatasets,
      ...(bundle.static_datasets || []),
    ];
    chartInstance.config._canvas_labels = allCanvasLabels;
    chartInstance.update("none");

    // ── Back-comm + scrubber sync ─────────────────────────────────────────────
    tlSetThumb(currentDay);
    ctx.updateProp("sim_day_out", currentDay);

    // ── Advance or finish ────────────────────────────────────────────────────
    if (currentDay >= bundle.total_days) {
      // Already at (or past) the end — stop and reset the play button.
      stopAnimation();
      isPlaying = false;
      updatePlayButton();
      simDoneCounter++;
      ctx.updateProp("sim_done", simDoneCounter);
    } else {
      const next = currentDay + currentStepDays;
      // If the next step would overshoot, clamp to exactly total_days so the
      // final frame is always rendered at the correct endpoint.
      currentDay = next >= bundle.total_days ? bundle.total_days : next;
    }
  }

  function startAnimation() {
    console.log("starting animation", intervalId)
    if (intervalId !== null) return;
    intervalId = setInterval(tick, TICK_MS);
  }

  function pauseAnimation() {
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
  }

  function stopAnimation() {
    pauseAnimation();
    currentDay = 0;
    lastKfDay  = -1;
  }

  function seekTo(day) {
    currentDay = day;
    // One-shot redraw only when not actively animating (interval drives its own renders).
    if (cachedBundle && intervalId === null) tick_noadvance();
  }

  /** Like tick() but does not advance currentDay (used for seek). */
  function tick_noadvance() {
    if (!cachedBundle) return;
    const bundle   = cachedBundle;
    const showW    = props.show_watts;
    const manifest = bundle.workout_manifest;
    const kf       = findKeyframe(bundle.keyframes, currentDay);

    const scatterDs = buildScatterDatasets(manifest, currentDay, bundle, showW);
    const predDs    = kf.pred_datasets || [];
    const { overlayDatasets, canvasLabels: overlayLabels } =
      buildOverlayDatasets(manifest, currentDay, currentStepDays, bundle, showW);

    let allCanvasLabels = [...overlayLabels];
    if (pbBadgeCountdown > 0) {
      allCanvasLabels = [...pbBadgeLabels, ...allCanvasLabels];
    }
    if (kf.pred_canvas_labels && kf.pred_canvas_labels.length) {
      allCanvasLabels = [...allCanvasLabels, ...kf.pred_canvas_labels];
    }

    chartInstance.data.datasets = [
      ...scatterDs,
      ...predDs,
      ...overlayDatasets,
      ...(bundle.static_datasets || []),
    ];
    chartInstance.config._canvas_labels = allCanvasLabels;
    chartInstance.update("none");
    tlSetThumb(currentDay);
    ctx.updateProp("sim_day_out", currentDay);
  }

  function handleSimCommand(command) {
    if (!command) return;
    if (command === "play") {
      // "play" is authoritative: start animation and confirm play state.
      isPlaying = true;
      startAnimation();
    } else if (command === "pause") {
      // "pause" only stops the interval — it does NOT clear isPlaying.
      // Python sends "pause" as a hold while the bundle is loading; we don't
      // want that to flip the button back to "Play" and break the feedback loop
      // where the next Play click sends sim_playing_out = true (already true →
      // HyperDiv drops it → Python never hears the click).
      pauseAnimation();
      if (cachedBundle) tick_noadvance();
    } else if (command === "stop") {
      // "stop" is a hard reset (e.g. at_today): clear isPlaying and the interval.
      isPlaying = false;
      stopAnimation();
    }
    // handleSimCommand does NOT send sim_playing_out — only user button clicks do.
    updatePlayButton();
  }

  // -----------------------------------------------------------------------
  // Initialise and respond to Python prop updates
  // -----------------------------------------------------------------------

  let props = {
    config:               ctx.initialProps.config               || null,
    show_watts:           ctx.initialProps.show_watts           || false,
    x_mode:               ctx.initialProps.x_mode               || "distance",
    sim_bundle:           ctx.initialProps.sim_bundle           || null,
    sim_command:          ctx.initialProps.sim_command          || "stop",
    sim_speed:            ctx.initialProps.sim_speed            || "1x",
    timeline_min:         ctx.initialProps.timeline_min         ?? 0,
    timeline_max:         ctx.initialProps.timeline_max         ?? 100,
    timeline_start_date:  ctx.initialProps.timeline_start_date  || "",
    timeline_annotations: ctx.initialProps.timeline_annotations || [],
  };

  currentStepDays = SPEED_DAYS[props.sim_speed] || 7;

  console.log("LOADING!")
  // Apply initial state.
  if (props.sim_bundle) {
    if (!cachedBundle || cachedBundle.bundle_key !== props.sim_bundle.bundle_key) {
      applyBundle(props.sim_bundle);
      console.log("APPLIED BUNDLE")
    }
    handleSimCommand(props.sim_command);
  } else if (props.config) {
    applyConfig(props.config, props.show_watts);
  }

  ctx.onPropUpdate((propName, propValue) => {
    props[propName] = propValue;

    if (propName === "sim_bundle") {
      if (propValue) {
        // New bundle — apply only when the key changes (settings changed).
        if (!cachedBundle || cachedBundle.bundle_key !== propValue.bundle_key) {
          // Preserve the current animation position so that:
          //  a) settings changes (predictor, theme, etc.) resume from where we left off
          //  b) a user seek before the first Play press is honoured on bundle arrival
          // Only fall back to bundle.start_day when currentDay is still at the
          // default initial value (0) and no prior bundle has been loaded.
          const resumeDay = (cachedBundle || currentDay > 0) ? currentDay : null;
          pauseAnimation();
          applyBundle(propValue);
          // applyBundle() resets currentDay to bundle.start_day — restore it.
          if (resumeDay !== null && resumeDay <= propValue.total_days) {
            currentDay = resumeDay;
          }
        }
        // Always apply the current sim_command after receiving a bundle so that
        // "play" is honoured even if the command prop didn't change this cycle.
        handleSimCommand(props.sim_command);
      }
      return;
    }

    if (propName === "sim_command") {
      // If bundle is active, use the animation path.
      if (cachedBundle) {
        handleSimCommand(propValue);
      }
      return;
    }

    if (propName === "sim_speed") {
      currentStepDays = SPEED_DAYS[propValue] || 7;
      return;
    }

    if (propName === "show_watts" || propName === "config" || propName === "x_mode") {
      // Static config update — only re-render if we're not in bundle animation mode.
      if (!cachedBundle || !intervalId) {
        // When paused with an active bundle, sync currentDay to Python's slider
        // position so that pressing Play resumes from where the slider was moved.
        // The config prop always arrives with the updated position embedded as _sim_day.
        if (cachedBundle && !intervalId && props.config && props.config._sim_day !== undefined) {
          currentDay = props.config._sim_day;
          tlSetThumb(currentDay);
        }
        applyConfig(props.config, props.show_watts);
      }
      return;
    }

    // ── Transport button prop updates ─────────────────────────────────────────
    if (propName === "rewind_day") { rewindDay = propValue; return; }

    // ── Timeline scrubber prop updates ────────────────────────────────────────
    if (propName === "timeline_min") {
      tlInput.min = propValue;
      tlBuildDots();
      tlUpdateFill();
      return;
    }
    if (propName === "timeline_max") {
      tlInput.max = propValue;
      tlBuildDots();
      tlUpdateFill();
      return;
    }
    if (propName === "timeline_start_date") {
      tlStartDate = new Date(propValue + "T00:00:00");
      return;
    }
    if (propName === "timeline_annotations") {
      tlAnnotations = Array.isArray(propValue) ? propValue : [];
      tlBuildDots();
      return;
    }
  });
});

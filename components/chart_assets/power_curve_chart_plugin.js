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

    .play_button {
      min-width: 85px;
    }

    /* ── Timeline scrubber (grows to fill the transport row) ─────────────── */
    .timeline {
      flex: 1;
      min-width: 300px;
      max-width: 900px;
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

    .canvas_wrapper {
      height: 75vh;
    }
    .power_curve_graph {
      display: block; 
      width: 100% !important; 
      height: 100% !important;     
      flex: 1; 
    }

    sl-range {
      --track-color-active: var(--sl-color-primary-500);
      --thumb-size: 30px;
    }
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
  btnPlay.className = "play_button";

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


  const canvas_wrapper = document.createElement("div");
  canvas_wrapper.className = "canvas_wrapper";
  ctx.domElement.appendChild(canvas_wrapper);

  // --- Canvas (below the scrubber) ---
  const canvas = document.createElement("canvas");
  canvas.className = "power_curve_graph"
  canvas_wrapper.appendChild(canvas);

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

  /** Tenths-of-a-second → "M:SS.t" (port of services/formatters.py:fmt_split). */
  function fmtSplit(tenths) {
    if (!tenths) return "—";
    const total = tenths / 10;
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
  }

  /**
   * Port of power_curve_animation.ol_event_line.
   * Builds the "Event  time-or-dist" string for tooltips and labels.
   */
  function eventLineFor(catKey, pace, distM) {
    const [etype, evalueStr] = catKey.split(":");
    const evalue = Number(evalueStr);
    if (etype === "dist") {
      const t = Math.round(pace * 10 * evalue / 500);
      const label = DIST_LABELS[evalue] || `${evalue.toLocaleString("en-US")}m`;
      return `${label}  ${fmtSplit(t)}`;
    } else {
      const mins = Math.floor(evalue / 600);
      return `${mins}min  ${distM.toLocaleString("en-US")}m`;
    }
  }

  // -----------------------------------------------------------------------
  // Timeline scrubber — helpers, event wiring, and seek handler
  // -----------------------------------------------------------------------

  let tlAnnotations = Array.isArray(ctx.initialProps.timeline_annotations)
    ? ctx.initialProps.timeline_annotations
    : [];
  let tlAnnHoverActive = false;


  // day to seek to when Play is pressed at end of timeline
  let rewindDay = 0;

  if (tlAnnotations.length > 0){
      minn      = Number(tlInput.min);
      maxx      = Number(tlInput.max);
      for (let index = tlAnnotations.length - 1; index >= 0; index--) {
        ann = tlAnnotations[index]
        if (ann.day < minn || ann.day > maxx) return;
        rewindDay = ann.day
        break
      }
  }

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
      if (cachedBundle && cachedBundle.snapshots_ready) startAnimation();
      updatePlayButton();
      ctx.updateProp("sim_playing_out", true);
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

  const _UI_FONT = "Nunito Sans, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";

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

  // -----------------------------------------------------------------------
  // Animation bundle — dataset builders
  // -----------------------------------------------------------------------

  /**
   * Find the snapshot entry for the current timeline position.
   *
   * `timeline_snapshots` is the two-level transport: {selection_key: {day: entry}}.
   * JS picks the inner dict via `selection_key` (current selection) and then
   * scans the numeric day keys to find the largest one ≤ currentDay.  Returns
   * the day-0 placeholder entry if no cached selection or no day fits — the
   * placeholder's `snapshot` has null fit params so pred dataset builders
   * degrade gracefully.
   */
  function findSnapshot(timelineSnapshots, selectionKey, currentDay) {
    const empty = {
      snapshot: {
        lb: {}, lb_anchor: {},
        cp_params: null, ll_slope: null, ll_intercept: null, pauls_k: 5.0,
      },
    };
    if (!timelineSnapshots || !selectionKey) return empty;
    const inner = timelineSnapshots[selectionKey];
    if (!inner) return empty;
    let bestDay = -1;
    let bestEntry = null;
    for (const k in inner) {
      const d = +k;
      if (d <= currentDay && d > bestDay) {
        bestDay = d;
        bestEntry = inner[k];
      }
    }
    return bestEntry || empty;
  }

  /**
   * Walk the workout manifest in day order and collect PB events — days on
   * which a new lifetime best was set for some (etype, evalue) category,
   * restricted to non-excluded workouts.  Used for the "New PB!" badge
   * during playback; replaces the previous Python-side ``new_pb_labels``
   * field on each snapshot so Python no longer owns any canvas-label state.
   *
   * Returns { byDay: {day: [label,...]}, sortedDays: [day,...] }.  Each
   * label carries both ``y_pace`` and ``y_watts`` so the tick loop picks
   * the right one for the current ``show_watts`` mode without a rebuild.
   */
  function computePBEvents(manifest, pbColor) {
    const sorted = (manifest || [])
      .filter(w => !w.excluded)
      .slice()
      .sort((a, b) => a.day - b.day);
    const bestPace = {};  // cat_key_str -> pace
    const byDay = {};
    const sortedDays = [];
    for (const w of sorted) {
      const prev = bestPace[w.cat_key_str];
      if (prev !== undefined && w.y_pace >= prev - 1e-9) continue;
      const pp = prev !== undefined && prev > w.y_pace
        ? (prev - w.y_pace) / prev * 100
        : 0;
      const pw = prev !== undefined
        ? (wattsFromPace(w.y_pace) - wattsFromPace(prev)) / wattsFromPace(prev) * 100
        : 0;
      const label = {
        x: w.x,
        y_pace: w.y_pace,
        y_watts: w.y_watts,
        line_event: w.event_line,
        pct_pace:  Math.round(pp * 10) / 10,
        pct_watts: Math.round(pw * 10) / 10,
        line_label: "\u2746 New PB!",
        color: pbColor,
        bold: true,
      };
      if (!byDay[w.day]) {
        byDay[w.day] = [];
        sortedDays.push(w.day);
      }
      byDay[w.day].push(label);
      bestPace[w.cat_key_str] = w.y_pace;
    }
    return { byDay, sortedDays };
  }

  /**
   * Newest PB-event day ≤ currentDay, or -1 if none.  ``sortedDays`` is
   * ascending so we walk until we pass ``currentDay``.
   */
  function newestPBEventDay(sortedDays, currentDay) {
    let found = -1;
    for (const d of sortedDays) {
      if (d > currentDay) break;
      found = d;
    }
    return found;
  }

  /**
   * Build per-season scatter + best-line datasets from the `workouts` prop.
   *
   * Visibility rules (mirror components/power_curve_workouts.py docstring):
   *   1. Hidden if w.day > cutoffDay.
   *   2. best_filter="All"    → all visible-by-day workouts shown.
   *      best_filter="PBs"    → only workouts currently holding the category PB.
   *      best_filter="SBs"    → only workouts currently holding the (season, cat) SB.
   *   3. Visible-but-event-filtered-out (cat_key ∉ selectedSet) → translucent;
   *      excluded from best-line membership.
   *   4. Visible + event-selected → full opacity; may contribute to best lines
   *      per overlay_bests.
   */
  function buildScatterFromWorkouts(workouts, cutoffDay, opts) {
    const { best_filter, overlay_bests, show_watts, x_mode,
            is_dark, season_meta, selected_dists, selected_times } = opts;
    if (!workouts || !workouts.length || !season_meta || !season_meta.length) {
      return [];
    }
    const useDuration = x_mode === "duration";
    const pbColor = is_dark ? "rgba(240,240,240,0.92)" : "rgba(40,40,40,0.88)";
    const selectedSet = new Set([
      ...(selected_dists || []).map(d => `dist:${d}`),
      ...(selected_times || []).map(t => `time:${t}`),
    ]);

    const visible = workouts.filter(w => w.day <= cutoffDay);
    if (!visible.length) return [];

    // Category PB (across all cats, regardless of selection) and per-season SB.
    const lbByCat = {};
    const sbByKey = {};
    for (const w of visible) {
      if (!(w.cat_key in lbByCat) || w.y_pace < lbByCat[w.cat_key].y_pace) {
        lbByCat[w.cat_key] = w;
      }
      const k = `${w.season_idx}|${w.cat_key}`;
      if (!(k in sbByKey) || w.y_pace < sbByKey[k].y_pace) sbByKey[k] = w;
    }

    function passesBestFilter(w) {
      if (best_filter === "PBs") return lbByCat[w.cat_key] === w;
      if (best_filter === "SBs") return sbByKey[`${w.season_idx}|${w.cat_key}`] === w;
      return true;  // "All"
    }
    function xOf(w) { return useDuration ? w.time_s : w.dist_m; }
    function yOf(w) { return show_watts ? w.y_watts : w.y_pace; }

    // Bucket per season; carry classification flags for drawing.
    const bySeason = new Map();
    for (const w of visible) {
      if (!passesBestFilter(w)) continue;
      const inSel = selectedSet.has(w.cat_key);
      const isLb = lbByCat[w.cat_key] === w;
      const isSb = sbByKey[`${w.season_idx}|${w.cat_key}`] === w;
      const entry = { w, translucent: !inSel, isLb, isSb };
      if (!bySeason.has(w.season_idx)) bySeason.set(w.season_idx, []);
      bySeason.get(w.season_idx).push(entry);
    }

    const datasets = [];

    // ── Scatter datasets (order=1) ──────────────────────────────────────────
    for (let si = 0; si < season_meta.length; si++) {
      const meta = season_meta[si];
      const entries = bySeason.get(si) || [];
      if (!entries.length) continue;
      const data = [], bg = [], border = [], bw = [], radii = [];
      for (const e of entries) {
        const { w, translucent, isLb, isSb } = e;
        const alpha = translucent ? 0.18 : ((isLb || isSb) ? 1.0 : 0.40);
        data.push({
          x: xOf(w),
          y: yOf(w),
          date: w.date_label,
          wtype: w.wtype,
          _event: eventLineFor(w.cat_key, w.y_pace, w.dist_m),
        });
        bg.push(colorWithAlpha(meta.color, alpha));
        if (isLb && !translucent) {
          border.push(pbColor);
          bw.push(2.5);
          radii.push(6);
        } else {
          const borderAlpha = translucent ? 0.25 : Math.min(alpha + 0.15, 1.0);
          border.push(colorWithAlpha(meta.border_color, borderAlpha));
          bw.push(translucent ? 0.5 : 1);
          radii.push(translucent ? 4 : 5);
        }
      }
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

    // ── Lifetime best line (order=3) ────────────────────────────────────────
    if (overlay_bests === "PBs") {
      const pts = [];
      const seen = new Set();
      for (const entries of bySeason.values()) {
        for (const { w, translucent, isLb } of entries) {
          if (translucent || !isLb || seen.has(w.cat_key)) continue;
          pts.push({ x: xOf(w), y: yOf(w) });
          seen.add(w.cat_key);
        }
      }
      pts.sort((a, b) => a.x - b.x);
      if (pts.length) {
        datasets.push({
          type: "line",
          label: "Lifetime Bests",
          data: pts,
          borderColor: pbColor,
          backgroundColor: "rgba(0,0,0,0)",
          borderWidth: 7,
          pointRadius: 0,
          tension: 0.15,
          order: 3,
        });
      }
    }

    // ── Season best lines (order=2) ─────────────────────────────────────────
    if (overlay_bests === "SBs") {
      for (let si = 0; si < season_meta.length; si++) {
        const meta = season_meta[si];
        const entries = bySeason.get(si) || [];
        if (!entries.length) continue;
        const pts = [];
        const seenCk = new Set();
        for (const { w, translucent, isSb } of entries) {
          if (translucent || !isSb || seenCk.has(w.cat_key)) continue;
          pts.push({ x: xOf(w), y: yOf(w) });
          seenCk.add(w.cat_key);
        }
        pts.sort((a, b) => a.x - b.x);
        if (!pts.length) continue;
        datasets.push({
          type: "line",
          label: `Season ${meta.label}`,
          data: pts,
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

  /** Build a scatter-opts dict from the current prop values. */
  function scatterOpts() {
    return {
      best_filter:   props.best_filter,
      overlay_bests: props.overlay_bests,
      show_watts:    props.show_watts,
      x_mode:        props.x_mode,
      is_dark:       props.is_dark,
      season_meta:   props.season_meta || [],
      selected_dists: props.selected_dists || [],
      selected_times: props.selected_times || [],
    };
  }

  /** Build a pred-opts dict from the current bundle + props. */
  function predOpts(bundle) {
    const xMode = bundle.x_mode || "distance";
    // x_mode-aware fallback: duration bounds must be in seconds, not meters,
    // otherwise the inRange filter in each pred builder rejects every point.
    const defaultXBounds = xMode === "duration" ? [10.0, 14400.0] : [100.0, 42195.0];
    const xb = (Array.isArray(bundle.x_bounds) && bundle.x_bounds.length === 2
                && Number.isFinite(bundle.x_bounds[0])
                && Number.isFinite(bundle.x_bounds[1]))
      ? bundle.x_bounds
      : defaultXBounds;
    const yb = (Array.isArray(bundle.y_bounds) && bundle.y_bounds.length === 2
                && Number.isFinite(bundle.y_bounds[0])
                && Number.isFinite(bundle.y_bounds[1]))
      ? bundle.y_bounds
      : [60.0, 250.0];
    return {
      predictor:       bundle.predictor || "none",
      show_components: bundle.show_components === true,
      show_watts:      props.show_watts,
      x_mode:          xMode,
      is_dark:         bundle.is_dark === true,
      x_bounds:        xb,
      y_bounds:        yb,
      rl_predictions:  bundle.rl_predictions || null,
      selected_dists:  props.selected_dists || [],
      selected_times:  props.selected_times || [],
    };
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

  /** Convert watts back to pace (sec/500m). Inverse of wattsFromPace. */
  function paceFromWattsVal(watts) {
    return 500.0 * Math.pow(2.80 / watts, 1.0 / 3.0);
  }

  // -----------------------------------------------------------------------
  // Prediction-curve math — ports of services/predictions.py +
  // services/critical_power_model.py.  All pure; consumed by the
  // snapshot-driven dataset builders below.
  // -----------------------------------------------------------------------

  const PACE_MIN = 60.0;
  const PACE_MAX = 400.0;

  const RANKED_DIST_VALUES = [100, 500, 1000, 2000, 5000, 6000, 10000, 21097, 42195];
  // Same source of truth as RANKED_DISTANCES in services/rowing_utils.py.
  const RANKED_DISTANCES_PAIRS = [
    [100, "100m"], [500, "500m"], [1000, "1k"], [2000, "2k"],
    [5000, "5k"], [6000, "6k"], [10000, "10k"],
    [21097, "½ Marathon"], [42195, "Marathon"],
  ];
  const RANKED_TIMES_PAIRS = [
    [600, "1 min"], [2400, "4 min"], [6000, "10 min"],
    [18000, "30 min"], [36000, "60 min"],
  ];

  // CP curve generation — matches services/critical_power_model.py constants.
  const CP_T_MIN = 10.0;
  const CP_T_MAX = 10_800.0;
  const CP_CURVE_N_PTS = 200;

  function paceValid(p) {
    return p !== null && p !== undefined && Number.isFinite(p) && p >= PACE_MIN && p <= PACE_MAX;
  }

  /** Critical Power model: P(t) = Pow1/(1+t/tau1) + Pow2/(1+t/tau2) */
  function criticalPowerModel(t, Pow1, tau1, Pow2, tau2) {
    return Pow1 / (1.0 + t / tau1) + Pow2 / (1.0 + t / tau2);
  }

  /**
   * Bisection root finder.  Returns null if f(lo)*f(hi) > 0 (no sign change)
   * or if f is non-finite anywhere on the bracket.  xtol sets convergence on
   * the x-axis (in whatever units lo/hi are — matches scipy.optimize.brentq's
   * xtol argument for our use cases).
   */
  function bisect(f, lo, hi, xtol = 0.5, maxIter = 100) {
    let flo = f(lo), fhi = f(hi);
    if (!Number.isFinite(flo) || !Number.isFinite(fhi)) return null;
    if (flo === 0) return lo;
    if (fhi === 0) return hi;
    if (flo * fhi > 0) return null;
    for (let i = 0; i < maxIter; i++) {
      const mid = 0.5 * (lo + hi);
      const fmid = f(mid);
      if (!Number.isFinite(fmid)) return null;
      if (Math.abs(fmid) < 1e-9 || (hi - lo) < xtol) return mid;
      if (flo * fmid < 0) { hi = mid; fhi = fmid; }
      else { lo = mid; flo = fmid; }
    }
    return 0.5 * (lo + hi);
  }

  // ── Samplers — (model_fit, dist_m) → pace or null ───────────────────────

  function paceFromLogLog(slope, intercept, distM) {
    if (slope == null || intercept == null) return null;
    const watts = Math.exp(intercept + slope * Math.log(distM));
    if (!Number.isFinite(watts) || watts <= 0) return null;
    const p = paceFromWattsVal(watts);
    return paceValid(p) ? p : null;
  }

  /** Paul's Law: p1 + k * log2(d2/d1) — predicts pace at d2 from anchor p1@d1. */
  function paulsLawPaceSingle(p1, d1, d2, k) {
    return p1 + k * (Math.log2(d2 / d1));
  }

  function paceFromPauls(lb, lba, distM, k) {
    if (!lb) return null;
    const paces = [];
    for (const cat of Object.keys(lb)) {
      const anchor = lba[cat];
      if (!anchor) continue;
      const p = paulsLawPaceSingle(lb[cat], anchor, distM, k);
      if (paceValid(p)) paces.push(p);
    }
    if (!paces.length) return null;
    return paces.reduce((a, b) => a + b, 0) / paces.length;
  }

  function cpUnpack(cpParams) {
    if (!cpParams) return null;
    const { Pow1, tau1, Pow2, tau2 } = cpParams;
    if (Pow1 == null || tau1 == null || Pow2 == null || tau2 == null) return null;
    return [Pow1, tau1, Pow2, tau2];
  }

  /**
   * Critical Power pace at a distance — invert the monotone P(t) model:
   * find t* such that (P(t*)/2.80)^(1/3) * t* = distM, then return
   * paceFromWatts(P(t*)).  Matches cp_pace_at in services/predictions.py.
   */
  function paceFromCP(cpParams, distM) {
    const p = cpUnpack(cpParams);
    if (!p) return null;
    const [Pow1, tau1, Pow2, tau2] = p;
    const resid = (t) => {
      const P = criticalPowerModel(t, Pow1, tau1, Pow2, tau2);
      return P > 0 ? Math.pow(P / 2.80, 1.0 / 3.0) * t - distM : -distM;
    };
    const tStar = bisect(resid, 10.0, 20_000.0, 0.5);
    if (tStar == null) return null;
    const watts = criticalPowerModel(tStar, Pow1, tau1, Pow2, tau2);
    if (watts <= 0) return null;
    const pace = paceFromWattsVal(watts);
    return paceValid(pace) ? pace : null;
  }

  // ── RowingLevel helpers ─────────────────────────────────────────────────

  /** Log-log interpolate within a single RL anchor's distance→pace table. */
  function rlInterpPace(preds, targetDist) {
    const known = [];
    for (const k of Object.keys(preds)) {
      const d = Number(k);
      const v = preds[k];
      if (Number.isFinite(d) && d > 0 && typeof v === "number" && paceValid(v)) {
        known.push([d, v]);
      }
    }
    if (known.length < 2) return null;
    known.sort((a, b) => a[0] - b[0]);
    let lo, hi;
    if (targetDist <= known[0][0]) { lo = known[0]; hi = known[1]; }
    else if (targetDist >= known[known.length - 1][0]) {
      lo = known[known.length - 2]; hi = known[known.length - 1];
    } else {
      for (let i = 0; i < known.length - 1; i++) {
        if (known[i][0] <= targetDist && targetDist <= known[i + 1][0]) {
          lo = known[i]; hi = known[i + 1]; break;
        }
      }
      if (!lo) { lo = known[known.length - 2]; hi = known[known.length - 1]; }
    }
    const logDlo = Math.log(lo[0]), logDhi = Math.log(hi[0]);
    if (logDhi === logDlo) return null;
    const t = (Math.log(targetDist) - logDlo) / (logDhi - logDlo);
    const logPace = Math.log(lo[1]) + t * (Math.log(hi[1]) - Math.log(lo[1]));
    return Math.exp(logPace);
  }

  function rlPaceFromPreds(preds, distM) {
    const dInt = Math.round(distM);
    const direct = preds[dInt] ?? preds[String(dInt)];
    if (paceValid(direct)) return direct;
    const pp = rlInterpPace(preds, distM);
    return paceValid(pp) ? pp : null;
  }

  /**
   * Distance-weighted RL average across all anchor PBs.  Weight per anchor =
   * 1 / (|log2(distM / anchor)| + 0.5) — anchors close in log-distance
   * dominate.  Ports rowinglevel_pace_at from services/predictions.py.
   */
  function paceFromRL(rlPreds, lba, distM) {
    if (!rlPreds) return null;
    const strLba = {};
    for (const k of Object.keys(lba || {})) strLba[String(k)] = lba[k];
    const paces = [], weights = [];
    for (const catKey of Object.keys(rlPreds)) {
      const p = rlPaceFromPreds(rlPreds[catKey], distM);
      if (p == null) continue;
      const anchor = strLba[catKey];
      const w = (anchor && distM > 0)
        ? 1.0 / (Math.abs(Math.log2(distM / anchor)) + 0.5)
        : 1.0;
      paces.push(p); weights.push(w);
    }
    if (!paces.length) return null;
    const totW = weights.reduce((a, b) => a + b, 0);
    return paces.reduce((a, p, i) => a + p * weights[i], 0) / totW;
  }

  // -----------------------------------------------------------------------
  // Prediction-dataset builders — driven by a per-keyframe "snapshot" dict
  // {lb, lb_anchor, cp_params, ll_slope, ll_intercept, pauls_k} plus
  // bundle-level opts {predictor, show_components, show_watts, x_mode,
  // is_dark, x_bounds, y_bounds, rl_predictions}.
  //
  // Each builder returns an array of Chart.js dataset dicts (may be empty).
  // Dispatcher: buildPredDatasetsFromSnapshot(snap, opts) →
  //   { datasets, canvasLabels } — datasets are dataset dicts; canvasLabels
  //   is the crossover annotation list (only populated for the CP predictor
  //   when show_components is on).
  // -----------------------------------------------------------------------

  // Colours derived in Python are warm amber; mirror here so JS can render
  // without a round-trip.  Single source of truth lives in chart_config.py's
  // pred_color — if that changes, update here too.
  function predColor(isDark) {
    return isDark ? "rgba(220,160,55,0.80)" : "rgba(185,120,20,0.80)";
  }

  function withAlpha(color, alpha) {
    return color.replace(
      /(hsla|rgba)\(([^,]+),([^,]+),([^,]+),[^)]+\)/,
      (_, fn, a, b, c) => `${fn}(${a},${b},${c},${alpha.toFixed(2)})`,
    );
  }

  /** Build a Chart.js prediction-line dataset dict. */
  function predDataset(label, points, color, pointRadius = 1.5, borderWidth = 1.5) {
    return {
      type: "line",
      label,
      data: points,
      borderColor: color,
      backgroundColor: "rgba(0,0,0,0)",
      borderWidth,
      borderDash: [5, 4],
      pointRadius,
      pointHoverRadius: pointRadius + 1.0,
      pointHitRadius: 8,
      pointBackgroundColor: color,
      tension: 0,
      order: 4,
      isPrediction: true,
    };
  }

  /** Y-value callback (pace → watts when show_watts). */
  function makeYFn(showWatts) {
    return showWatts
      ? (pace) => Math.round(wattsFromPace(pace) * 10) / 10
      : (pace) => Math.round(pace * 1000) / 1000;
  }

  /** X-value callback (dist, pace) → chart x for current x_mode. */
  function makeXFn(xMode) {
    return xMode === "duration"
      ? (dist, pace) => Math.round(dist * pace / 500.0 * 100) / 100
      : (dist, _p) => dist;
  }

  function inRange(pts, xMin, xMax) {
    return pts
      .filter(p => p.x >= xMin && p.x <= xMax)
      .sort((a, b) => a.x - b.x);
  }

  // ── Log-Log ─────────────────────────────────────────────────────────────
  function buildLogLogDataset(snap, opts) {
    const { ll_slope, ll_intercept } = snap;
    if (ll_slope == null || ll_intercept == null) return [];
    const yFn = makeYFn(opts.show_watts);
    const xFn = makeXFn(opts.x_mode);
    const pts = [];
    for (const d of RANKED_DIST_VALUES) {
      const p = paceFromLogLog(ll_slope, ll_intercept, d);
      if (paceValid(p)) pts.push({ x: xFn(d, p), y: yFn(p) });
    }
    const sorted = inRange(pts, opts.x_bounds[0], opts.x_bounds[1]);
    if (sorted.length < 2) return [];
    return [predDataset("_loglog_fit", sorted, opts.color, 3, 1.5)];
  }

  // ── Paul's Law ──────────────────────────────────────────────────────────
  function buildPaulsLawDatasets(snap, opts) {
    const { lb, lb_anchor, pauls_k } = snap;
    const yFn = makeYFn(opts.show_watts);
    const xFn = makeXFn(opts.x_mode);
    const [xMin, xMax] = opts.x_bounds;

    const byDist = {};
    const perAnchor = {};
    for (const cat of Object.keys(lb || {})) {
      const anchor = lb_anchor[cat];
      if (!anchor) continue;
      const catPts = [];
      for (const d of RANKED_DIST_VALUES) {
        const p = paulsLawPaceSingle(lb[cat], anchor, d, pauls_k);
        if (paceValid(p)) {
          if (!byDist[d]) byDist[d] = [];
          byDist[d].push(p);
          catPts.push([d, p]);
        }
      }
      if (catPts.length >= 2) perAnchor[cat] = catPts;
    }

    const out = [];
    const avgPts = [];
    for (const d of RANKED_DIST_VALUES) {
      const ps = byDist[d];
      if (ps && ps.length) {
        const avg = ps.reduce((a, b) => a + b, 0) / ps.length;
        avgPts.push({ x: xFn(d, avg), y: yFn(avg) });
      }
    }
    const sortedAvg = inRange(avgPts, xMin, xMax);
    if (sortedAvg.length >= 2) {
      out.push(predDataset("_pl_avg", sortedAvg, opts.color, 1.5, 2.0));
    }
    if (opts.show_components) {
      const dim = withAlpha(opts.color, 0.55);
      for (const cat of Object.keys(perAnchor)) {
        const pts = perAnchor[cat].map(([d, p]) => ({ x: xFn(d, p), y: yFn(p) }));
        const sorted = inRange(pts, xMin, xMax);
        if (sorted.length >= 2) {
          out.push(predDataset(`_pred_${cat}`, sorted, dim, 0, 1.0));
        }
      }
    }
    return out;
  }

  // ── RowingLevel ─────────────────────────────────────────────────────────
  function buildRowingLevelDatasets(snap, opts) {
    const rlPreds = opts.rl_predictions;
    if (!rlPreds) return [];
    const { lb_anchor } = snap;
    const yFn = makeYFn(opts.show_watts);
    const xFn = makeXFn(opts.x_mode);
    const [xMin, xMax] = opts.x_bounds;

    // Collect all unique distances across anchor curves (excluding 100m).
    const allDistsSet = new Set();
    for (const catKey of Object.keys(rlPreds)) {
      for (const dkey of Object.keys(rlPreds[catKey])) {
        const d = Math.round(Number(dkey));
        if (Number.isFinite(d) && d !== 100) allDistsSet.add(d);
      }
    }
    const allDists = Array.from(allDistsSet).sort((a, b) => a - b);

    const strLba = {};
    for (const k of Object.keys(lb_anchor || {})) strLba[String(k)] = lb_anchor[k];

    const avgPts = [];
    for (const d of allDists) {
      const paces = [], weights = [];
      for (const catKey of Object.keys(rlPreds)) {
        const preds = rlPreds[catKey];
        const p = preds[d] ?? preds[String(d)];
        if (!paceValid(p)) continue;
        const anchor = strLba[catKey];
        const w = anchor ? 1.0 / (Math.abs(Math.log2(d / anchor)) + 0.5) : 1.0;
        paces.push(p); weights.push(w);
      }
      if (paces.length) {
        const totW = weights.reduce((a, b) => a + b, 0);
        const avg = paces.reduce((a, p, i) => a + p * weights[i], 0) / totW;
        avgPts.push({ x: xFn(d, avg), y: yFn(avg) });
      }
    }

    const out = [];
    const sortedAvg = inRange(avgPts, xMin, xMax);
    if (sortedAvg.length >= 2) {
      out.push(predDataset("_rl_avg", sortedAvg, opts.color, 1.5, 2.0));
    }
    if (opts.show_components) {
      const dim = withAlpha(opts.color, 0.55);
      for (const catKey of Object.keys(rlPreds)) {
        const preds = rlPreds[catKey];
        const pts = [];
        for (const dkey of Object.keys(preds)) {
          const d = Math.round(Number(dkey));
          if (!Number.isFinite(d) || d === 100) continue;
          const p = preds[dkey];
          if (!paceValid(p)) continue;
          pts.push({ x: xFn(d, p), y: yFn(p) });
        }
        const sorted = inRange(pts, xMin, xMax);
        if (sorted.length < 2) continue;
        out.push(predDataset(`_rl_${catKey}`, sorted, dim, 0, 1.0));
      }
    }
    return out;
  }

  // ── Critical Power ──────────────────────────────────────────────────────
  // Returns { datasets, canvasLabels } — datasets include the curve, event
  // markers, optional fast/slow components and crossover vline; canvasLabels
  // carries the bottom-anchored crossover annotation when show_components.
  function buildCPDatasets(snap, opts) {
    const cp = snap.cp_params;
    if (!cp) return { datasets: [], canvasLabels: [] };
    const unpacked = cpUnpack(cp);
    if (!unpacked) return { datasets: [], canvasLabels: [] };
    const [Pow1, tau1, Pow2, tau2] = unpacked;
    const yFn = makeYFn(opts.show_watts);
    const xFn = makeXFn(opts.x_mode);
    const [xMin, xMax] = opts.x_bounds;
    const [yMin, yMax] = opts.y_bounds || [60.0, 250.0];
    const isDark = opts.is_dark;

    const datasets = [];

    // ── Smooth curve ────────────────────────────────────────────────────
    const cpPts = [];
    const logMin = Math.log10(CP_T_MIN);
    const logMax = Math.log10(CP_T_MAX);
    for (let i = 0; i < CP_CURVE_N_PTS; i++) {
      const lt = logMin + (logMax - logMin) * (i / (CP_CURVE_N_PTS - 1));
      const t = Math.pow(10, lt);
      const w = criticalPowerModel(t, Pow1, tau1, Pow2, tau2);
      if (w <= 0) continue;
      const pace = paceFromWattsVal(w);
      if (!paceValid(pace)) continue;
      const dist = t * (500.0 / pace);
      const xv = xFn(dist, pace);
      if (xv < xMin || xv > xMax) continue;
      datasets.length; // unused
      cpPts.push({ x: Math.round(xv * 10) / 10, y: yFn(pace) });
    }
    if (cpPts.length >= 2) {
      datasets.push(predDataset("_critical_power", cpPts, opts.color, 0, 1.5));
    }

    // ── Event markers (one per selected ranked event) ─────────────────
    const selDists = new Set(opts.selected_dists || []);
    const selTimes = new Set(opts.selected_times || []);
    const evPts = [];

    for (const [distM, label] of RANKED_DISTANCES_PAIRS) {
      if (!selDists.has(distM)) continue;
      const resid = (t) => {
        const P = criticalPowerModel(t, Pow1, tau1, Pow2, tau2);
        return P > 0 ? Math.pow(P / 2.80, 1.0 / 3.0) * t - distM : -distM;
      };
      const tStar = bisect(resid, CP_T_MIN, CP_T_MAX, 0.1);
      if (tStar == null) continue;
      const w = criticalPowerModel(tStar, Pow1, tau1, Pow2, tau2);
      if (w <= 0) continue;
      const pace = paceFromWattsVal(w);
      if (!paceValid(pace)) continue;
      const dist = tStar * (500.0 / pace);
      const xv = xFn(dist, pace);
      if (xv < xMin || xv > xMax) continue;
      evPts.push({ x: Math.round(xv * 10) / 10, y: yFn(pace), _event_label: label });
    }
    for (const [tenths, label] of RANKED_TIMES_PAIRS) {
      if (!selTimes.has(tenths)) continue;
      const t = tenths / 10.0;
      const w = criticalPowerModel(t, Pow1, tau1, Pow2, tau2);
      if (w <= 0) continue;
      const pace = paceFromWattsVal(w);
      if (!paceValid(pace)) continue;
      const dist = t * (500.0 / pace);
      const xv = xFn(dist, pace);
      if (xv < xMin || xv > xMax) continue;
      evPts.push({ x: Math.round(xv * 10) / 10, y: yFn(pace), _event_label: label });
    }
    if (evPts.length) {
      datasets.push({
        type: "scatter",
        label: "_cp_event_markers",
        data: evPts,
        backgroundColor: opts.color,
        borderColor: opts.color,
        borderWidth: 1,
        pointRadius: 4,
        pointHoverRadius: 7,
        pointHitRadius: 12,
        order: 4,
        isPrediction: true,
      });
    }

    // ── Crossover + fast/slow components (only when show_components) ─────
    const canvasLabels = [];
    if (opts.show_components) {
      const diff = (t) => Pow1 / (1.0 + t / tau1) - Pow2 / (1.0 + t / tau2);
      let tCross = null;
      if (diff(CP_T_MIN) * diff(CP_T_MAX) <= 0) {
        tCross = bisect(diff, CP_T_MIN, CP_T_MAX, 0.1);
      }
      if (tCross != null) {
        const wCross = criticalPowerModel(tCross, Pow1, tau1, Pow2, tau2);
        if (wCross > 0) {
          const paceCross = paceFromWattsVal(wCross);
          if (paceValid(paceCross)) {
            const distCross = tCross * (500.0 / paceCross);
            const xCross = xFn(distCross, paceCross);
            if (xCross >= xMin && xCross <= xMax) {
              const xoColor = isDark ? "rgba(20, 210, 190, 0.55)" : "rgba(0, 160, 145, 0.55)";
              const xoTextColor = isDark ? "rgba(20, 210, 190, 0.90)" : "rgba(0, 140, 128, 0.90)";
              datasets.push({
                type: "line",
                label: "_cp_crossover_vline",
                data: [{ x: xCross, y: yMin }, { x: xCross, y: yMax }],
                borderColor: xoColor,
                backgroundColor: "rgba(0,0,0,0)",
                borderWidth: 1.5,
                borderDash: [6, 4],
                pointRadius: 0,
                tension: 0,
                order: 4,
              });
              // Human-readable duration label ("1h 23m 05s" etc.)
              const total = Math.round(tCross);
              const mins = Math.floor(total / 60);
              const secs = total % 60;
              let tLabel;
              if (mins >= 60) {
                const hrs = Math.floor(mins / 60);
                const mm = mins % 60;
                tLabel = `${hrs}h ${mm}m ${String(secs).padStart(2, "0")}s`;
              } else if (mins > 0) {
                tLabel = `${mins}m ${String(secs).padStart(2, "0")}s`;
              } else {
                tLabel = `${secs}s`;
              }
              canvasLabels.push({
                x: xCross,
                y: null,
                _anchor: "bottom",
                lines: [`Crossover: ${tLabel}`, "<- sprint | aerobic ->"],
                color: xoTextColor,
              });
            }
          }
        }
      }

      // Fast/slow component curves.
      const dim = withAlpha(opts.color, 0.62);
      const fastPts = [], slowPts = [];
      for (let i = 0; i < CP_CURVE_N_PTS; i++) {
        const lt = logMin + (logMax - logMin) * (i / (CP_CURVE_N_PTS - 1));
        const t = Math.pow(10, lt);
        const wCombined = Pow1 / (1.0 + t / tau1) + Pow2 / (1.0 + t / tau2);
        if (wCombined <= 0) continue;
        const paceCombined = paceFromWattsVal(wCombined);
        if (!paceValid(paceCombined)) continue;
        const dist = t * (500.0 / paceCombined);
        const xv = xFn(dist, paceCombined);
        if (xv < xMin || xv > xMax) continue;
        const wFast = Pow1 / (1.0 + t / tau1);
        const wSlow = Pow2 / (1.0 + t / tau2);
        if (opts.show_watts) {
          fastPts.push({ x: Math.round(xv * 100) / 100, y: Math.round(wFast * 100) / 100 });
          slowPts.push({ x: Math.round(xv * 100) / 100, y: Math.round(wSlow * 100) / 100 });
        } else {
          const pf = paceFromWattsVal(wFast);
          const ps = paceFromWattsVal(wSlow);
          if (paceValid(pf)) fastPts.push({ x: Math.round(xv * 100) / 100, y: Math.round(pf * 10000) / 10000 });
          if (paceValid(ps)) slowPts.push({ x: Math.round(xv * 100) / 100, y: Math.round(ps * 10000) / 10000 });
        }
      }
      if (fastPts.length >= 2) datasets.push(predDataset("_cp_fast", fastPts, dim, 0, 1.0));
      if (slowPts.length >= 2) datasets.push(predDataset("_cp_slow", slowPts, dim, 0, 1.0));
    }

    return { datasets, canvasLabels };
  }

  // ── Average ensemble ────────────────────────────────────────────────────
  function buildAverageDatasets(snap, opts) {
    const { lb, lb_anchor, cp_params, ll_slope, ll_intercept, pauls_k } = snap;
    const yFn = makeYFn(opts.show_watts);
    const xFn = makeXFn(opts.x_mode);
    const [xMin, xMax] = opts.x_bounds;
    const N = 80;
    const logMin = Math.log10(100.0);
    const logMax = Math.log10(42195.0);

    const avgPts = [], llPts = [], plPts = [], cpPts = [], rlPts = [];

    for (let i = 0; i < N; i++) {
      const d = Math.pow(10, logMin + (logMax - logMin) * (i / (N - 1)));
      const paces = [];

      const ll = paceFromLogLog(ll_slope, ll_intercept, d);
      if (paceValid(ll)) { paces.push(ll); llPts.push({ x: xFn(d, ll), y: yFn(ll) }); }

      const pl = paceFromPauls(lb, lb_anchor, d, pauls_k);
      if (paceValid(pl)) { paces.push(pl); plPts.push({ x: xFn(d, pl), y: yFn(pl) }); }

      const cp = paceFromCP(cp_params, d);
      if (paceValid(cp)) { paces.push(cp); cpPts.push({ x: xFn(d, cp), y: yFn(cp) }); }

      const rl = paceFromRL(opts.rl_predictions, lb_anchor, d);
      if (paceValid(rl)) { paces.push(rl); rlPts.push({ x: xFn(d, rl), y: yFn(rl) }); }

      if (paces.length) {
        const avg = paces.reduce((a, b) => a + b, 0) / paces.length;
        avgPts.push({ x: xFn(d, avg), y: yFn(avg) });
      }
    }

    const out = [];
    const sortedAvg = inRange(avgPts, xMin, xMax);
    if (sortedAvg.length >= 2) {
      out.push(predDataset("_avg_ensemble", sortedAvg, opts.color, 1.5, 2.5));
    }
    if (opts.show_components) {
      const dim = withAlpha(opts.color, 0.55);
      const pairs = [["_avg_ll", llPts], ["_avg_pl", plPts], ["_avg_cp", cpPts], ["_avg_rl", rlPts]];
      for (const [label, pts] of pairs) {
        const sorted = inRange(pts, xMin, xMax);
        if (sorted.length >= 2) out.push(predDataset(label, sorted, dim, 0, 1.0));
      }
    }
    return out;
  }

  /**
   * Snapshot-driven prediction builder — owns prediction-curve dataset
   * construction on the JS side using fit parameters baked into each
   * timeline snapshot.
   *
   * snap = {lb, lb_anchor, cp_params, ll_slope, ll_intercept, pauls_k}
   * opts = {predictor, show_components, show_watts, x_mode, is_dark,
   *         x_bounds, y_bounds, rl_predictions, selected_dists,
   *         selected_times}
   *
   * Returns { datasets, canvasLabels }.  canvasLabels is populated only
   * when the CP predictor produces a crossover annotation.
   */
  function buildPredDatasetsFromSnapshot(snap, rawOpts) {
    if (!snap) return { datasets: [], canvasLabels: [] };
    const opts = { ...rawOpts, color: predColor(rawOpts.is_dark) };
    let p = opts.predictor;
    // CP → log-log fallback.  fit_critical_power() needs ≥5 PBs; early in the
    // timeline there aren't enough and cp_params is null.  Python's slow path
    // (power_curve_page:1266) falls back to log-log in that case; mirror it
    // here so the prediction curve doesn't vanish mid-animation.
    if (p === "critical_power" && !snap.cp_params) p = "loglog";
    if (p === "none") return { datasets: [], canvasLabels: [] };
    if (p === "loglog") return { datasets: buildLogLogDataset(snap, opts), canvasLabels: [] };
    if (p === "pauls_law") return { datasets: buildPaulsLawDatasets(snap, opts), canvasLabels: [] };
    if (p === "rowinglevel") return { datasets: buildRowingLevelDatasets(snap, opts), canvasLabels: [] };
    if (p === "critical_power") return buildCPDatasets(snap, opts);
    if (p === "average") return { datasets: buildAverageDatasets(snap, opts), canvasLabels: [] };
    return { datasets: [], canvasLabels: [] };
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
    const logY    = bundle.log_y === true;
    // Do NOT set reverse — let Chart.js default to false, matching the static chart.

    // Gridline colour — sourced from the bundle so it matches the static
    // chart's axis grid (Python is the single source of truth for colours).
    const gridColor = bundle.grid_color;

    const xScaleOpts = {
      type: logX ? "logarithmic" : "linear",
      ticks: { callback: (val) => xLabelFn(val) },
      grid: { color: gridColor },
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
      type: logY ? "logarithmic" : "linear",
      ticks: {
        callback: showWatts ? (v) => Math.round(v) + "W" : (v) => formatPace(v),
        stepSize: showWatts ? 50 : 5,
      },
      grid: { color: gridColor },
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
  let lastKfDay       = -1;     // tracks which PB-event day was last processed for PB badge
  let pbEventsByDay   = {};     // day -> [label,...] from computePBEvents
  let pbEventDays     = [];     // sorted ascending list of PB-event days

  const SPEED_DAYS = { "0.5x": 1, "1x": 7, "4x": 30, "16x": 91 };
  const TICK_MS = 350;

  function applyBundle(bundle) {
    cachedBundle     = bundle;
    currentDay       = props.timeline_max + 1;
    lastKfDay        = -1;
    pbBadgeCountdown = 0;
    pbBadgeLabels    = [];

    // Precompute PB events from the manifest (once per bundle).  Python no
    // longer ships per-snapshot PB labels; JS derives them here from the
    // same manifest used for scatter/overlay datasets.
    const pbEvents = computePBEvents(bundle.workout_manifest, bundle.pb_color);
    pbEventsByDay = pbEvents.byDay;
    pbEventDays   = pbEvents.sortedDays;

    // Sync scrubber range to the bundle timeline.
    if(tlInput.min != 0 || tlInput.max != bundle.total_days){
      tlInput.min = 0;
      tlInput.max = bundle.total_days;
      tlSetThumb(currentDay);
    }

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
    const kf       = findSnapshot(
      bundle.timeline_snapshots, bundle.selection_key, currentDay,
    );

    // ── Scatter + best lines ─────────────────────────────────────────────────
    const scatterDs = buildScatterFromWorkouts(
      props.workouts, currentDay, scatterOpts(),
    );

    // ── Prediction datasets + crossover labels (JS-sampled from snapshot) ────
    const { datasets: predDs, canvasLabels: predCanvasLabels } =
      buildPredDatasetsFromSnapshot(kf.snapshot, predOpts(bundle));

    // ── Overlay datasets ─────────────────────────────────────────────────────
    const { overlayDatasets, canvasLabels: overlayLabels } =
      buildOverlayDatasets(manifest, currentDay, currentStepDays, bundle, showW);

    // ── PB badge ─────────────────────────────────────────────────────────────
    // Trigger when we cross a new PB-event day.  We can't check
    // pbEventDay === currentDay because the tick step may skip over it.
    const pbEventDay = newestPBEventDay(pbEventDays, currentDay);
    if (pbEventDay > lastKfDay) {
      lastKfDay = pbEventDay;
      const dayLabels = pbEventsByDay[pbEventDay] || [];
      if (dayLabels.length) {
        pbBadgeLabels = dayLabels.map(lbl => ({
          x:          lbl.x,
          y:          showW ? lbl.y_watts : lbl.y_pace,
          line_event: lbl.line_event,
          pct_pace:   lbl.pct_pace,
          pct_watts:  lbl.pct_watts,
          line_label: lbl.line_label,
          color:      lbl.color,
          bold:       lbl.bold,
        }));
        pbBadgeCountdown = bundle.pb_badge_lifetime_steps;
      }
    }

    // Merge PB badge labels with upcoming-PB overlay labels and any
    // predictor canvas labels (e.g. CP fast/slow-twitch crossover annotation).
    let allCanvasLabels = [...overlayLabels];
    if (pbBadgeCountdown > 0) {
      allCanvasLabels = [...pbBadgeLabels, ...allCanvasLabels];
      pbBadgeCountdown--;
    }
    if (predCanvasLabels && predCanvasLabels.length) {
      allCanvasLabels = [...allCanvasLabels, ...predCanvasLabels];
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
      // Mirror the user-pause behavior so Python's state.sim_playing follows.
      // Without this, sim_playing_out stays true and a subsequent seek would
      // re-trigger play on the next render.
      ctx.updateProp("sim_playing_out", false);
    } else {
      const next = currentDay + currentStepDays;
      // If the next step would overshoot, clamp to exactly total_days so the
      // final frame is always rendered at the correct endpoint.
      currentDay = next >= bundle.total_days ? bundle.total_days : next;
    }
  }

  function startAnimation() {
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
    currentDay = props.timeline_max + 1;
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
    const kf       = findSnapshot(
      bundle.timeline_snapshots, bundle.selection_key, currentDay,
    );

    const scatterDs = buildScatterFromWorkouts(
      props.workouts, currentDay, scatterOpts(),
    );
    const { datasets: predDs, canvasLabels: predCanvasLabels } =
      buildPredDatasetsFromSnapshot(kf.snapshot, predOpts(bundle));
    const { overlayDatasets, canvasLabels: overlayLabels } =
      buildOverlayDatasets(manifest, currentDay, currentStepDays, bundle, showW);

    let allCanvasLabels = [...overlayLabels];
    if (pbBadgeCountdown > 0) {
      allCanvasLabels = [...pbBadgeLabels, ...allCanvasLabels];
    }
    if (predCanvasLabels && predCanvasLabels.length) {
      allCanvasLabels = [...allCanvasLabels, ...predCanvasLabels];
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
      // Render the end-of-timeline frame so the chart shows full data rather
      // than going blank — e.g. on initial mount after navigating back to the
      // page, when a cached bundle is applied with sim_command="stop".
      isPlaying = false;
      stopAnimation();
      if (cachedBundle) tick_noadvance();
    }
    // handleSimCommand does NOT send sim_playing_out — only user button clicks do.
    updatePlayButton();
  }

  // -----------------------------------------------------------------------
  // Initialise and respond to Python prop updates
  // -----------------------------------------------------------------------

  let props = {
    workouts:             ctx.initialProps.workouts             || [],
    season_meta:          ctx.initialProps.season_meta          || [],
    best_filter:          ctx.initialProps.best_filter          || "All",
    overlay_bests:        ctx.initialProps.overlay_bests        || "PBs",
    selected_dists:       ctx.initialProps.selected_dists       || [],
    selected_times:       ctx.initialProps.selected_times       || [],
    is_dark:              ctx.initialProps.is_dark              || false,
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

  // Props that affect scatter/best-line rendering.  When any of these change,
  // rebuild scatter in place (static path) or redraw the current frame (sim).
  const SCATTER_PROPS = new Set([
    "workouts", "season_meta", "best_filter", "overlay_bests",
    "selected_dists", "selected_times", "is_dark",
  ]);

  currentStepDays = SPEED_DAYS[props.sim_speed] || 7;

  // Apply initial state.  Python always pre-populates a fast sim_bundle before
  // the chart mounts, so the bundle branch is the only code path.
  if (props.sim_bundle) {
    if (!cachedBundle || cachedBundle.bundle_key !== props.sim_bundle.bundle_key) {
      applyBundle(props.sim_bundle);
    }
    handleSimCommand(props.sim_command);
  }

  ctx.onPropUpdate((propName, propValue) => {
    props[propName] = propValue;

    if (SCATTER_PROPS.has(propName)) {
      // Scatter-affecting props: redraw the current frame.  With the bundle
      // always present, a paused animation simply re-renders from the current
      // snapshot.
      if (cachedBundle && intervalId === null) {
        tick_noadvance();
      }
      return;
    }

    if (propName === "sim_bundle") {
      if (propValue) {
        // New bundle — apply only when the key changes (settings changed).
        if (!cachedBundle || cachedBundle.bundle_key !== propValue.bundle_key) {
          // Preserve the current animation position so that:
          //  a) settings changes (predictor, theme, etc.) resume from where we left off
          //  b) a user seek before the first Play press is honoured on bundle arrival
          // Only fall back to start_day when currentDay is still at the
          // default initial value (0) and no prior bundle has been loaded.
          const resumeDay = (cachedBundle || currentDay > 0) ? currentDay : null;
          pauseAnimation();
          applyBundle(propValue);
          // applyBundle() resets currentDay to start_day — restore it.
          if (resumeDay !== null && resumeDay <= propValue.total_days) {
            currentDay = resumeDay;
          }
        }
        // Always apply the current sim_command after receiving a bundle so that
        // "play" is honoured even if the command prop didn't change this cycle.
        handleSimCommand(props.sim_command);
      } else {
        // Bundle cleared (identity_key change in Python — e.g. user toggled
        // best_filter / excluded seasons / predictor).  Pause and drop the
        // cached bundle; Python will ship a fresh fast bundle within the same
        // render cycle, which will re-enter the applyBundle branch above.
        pauseAnimation();
        cachedBundle = null;
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

    if (propName === "show_watts" || propName === "x_mode") {
      // These change Chart.js axis options on the next applyBundle — settings
      // that mutate data_key trigger a bundle rebuild on the Python side, so
      // the incoming sim_bundle prop update drives the re-render.  No local
      // action needed here.
      return;
    }

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

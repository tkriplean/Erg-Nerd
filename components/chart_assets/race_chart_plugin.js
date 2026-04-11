/**
 * RaceChart — HyperDiv plugin for the regatta-style event race animation.
 *
 * Layout (flexbox column inside the shadow root):
 *   ┌─────────────────────────────────────────────────────┐  flex: 1
 *   │  Race canvas — header row + lane rows + finish line │
 *   ├─────────────────────────────────────────────────────┤  44px
 *   │  Controls: [▶] [time] [────seek────] [total] [Nx]  │
 *   └─────────────────────────────────────────────────────┘
 *
 * Props — Python → JS (Python-owned, never written by JS):
 *   races        [{id, label, color, strokes:[{t,d}], is_pb, season}, …]
 *   event_type   "dist" | "time"
 *   event_value  meters (dist) | tenths-of-sec (time)
 *   is_dark      bool
 *
 * Props — JS → Python (JS-owned):
 *   change_id        incremented on every user seek
 *   current_time_ms  race clock position when user sought
 *
 * Animation is entirely JS-internal via requestAnimationFrame.
 * Python does not drive ticks for this plugin.
 *
 * Lane layout (per boat, top-down):
 *   [LABEL ZONE 160px] [TRACK ZONE flex] [RESULT ZONE 88px]
 *
 * Boat: scull-shaped elongated hull (pointed bow at right, rounded stern).
 *       PB scull has a white stroke outline.
 * Wake: ring buffer of last 8 positions drawn with decreasing opacity + radius.
 *
 * Default playback speed: 8× real-time. Speed options: 1×–64×.
 */

window.hyperdiv.registerPlugin("RaceChart", (ctx) => {

  // ── Shadow DOM styles ──────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host {
      display: flex;
      flex-direction: column;
      width: 100%;
      height: 100%;
    }
    .canvas-wrap {
      flex: 1;
      min-height: 0;
      position: relative;
    }
    canvas {
      display: block;
      width: 100% !important;
      height: 100% !important;
    }
    .controls {
      flex-shrink: 0;
      height: 44px;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 0 10px;
      border-top: 1px solid rgba(128,128,128,0.20);
      font-family: sans-serif;
      font-size: 13px;
    }
    .play-btn {
      background: none;
      border: 1px solid rgba(128,128,128,0.45);
      border-radius: 4px;
      cursor: pointer;
      font-size: 14px;
      padding: 2px 9px;
      color: inherit;
      min-width: 38px;
    }
    .play-btn:hover { background: rgba(128,128,128,0.12); }
    .race-time-display {
      font-variant-numeric: tabular-nums;
      min-width: 48px;
      color: rgba(128,128,128,0.9);
    }
    .race-seek {
      flex: 1;
      cursor: pointer;
      accent-color: #4a9eff;
    }
    .race-time-total {
      font-variant-numeric: tabular-nums;
      min-width: 48px;
      color: rgba(128,128,128,0.6);
      font-size: 12px;
    }
    .race-speed {
      background: none;
      border: 1px solid rgba(128,128,128,0.35);
      border-radius: 4px;
      padding: 2px 4px;
      font-size: 12px;
      cursor: pointer;
      color: inherit;
    }
  `;
  ctx.domElement.appendChild(style);

  // ── DOM ────────────────────────────────────────────────────────────────────
  const canvasWrap = document.createElement("div");
  canvasWrap.className = "canvas-wrap";
  const canvas = document.createElement("canvas");
  canvasWrap.appendChild(canvas);
  ctx.domElement.appendChild(canvasWrap);

  const controls = document.createElement("div");
  controls.className = "controls";

  const playBtn = document.createElement("button");
  playBtn.className = "play-btn";
  playBtn.textContent = "▶";

  const timeDisplay = document.createElement("span");
  timeDisplay.className = "race-time-display";
  timeDisplay.textContent = "0:00.0";

  const seekInput = document.createElement("input");
  seekInput.type = "range";
  seekInput.className = "race-seek";
  seekInput.min = 0;
  seekInput.step = 100;
  seekInput.value = 0;

  const totalDisplay = document.createElement("span");
  totalDisplay.className = "race-time-total";

  const speedSelect = document.createElement("select");
  speedSelect.className = "race-speed";
  [1, 2, 4, 8, 16, 32, 64].forEach(v => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v + "×";
    if (v === 8) opt.selected = true;
    speedSelect.appendChild(opt);
  });

  controls.appendChild(playBtn);
  controls.appendChild(timeDisplay);
  controls.appendChild(seekInput);
  controls.appendChild(totalDisplay);
  controls.appendChild(speedSelect);
  ctx.domElement.appendChild(controls);

  // ── State ──────────────────────────────────────────────────────────────────
  let races       = ctx.initialProps.races || [];
  let eventType   = ctx.initialProps.event_type || "dist";
  let eventValue  = ctx.initialProps.event_value || 2000;
  let isDark      = !!(ctx.initialProps.is_dark);

  let playing        = false;
  let playbackSpeed  = 8;
  let currentTimeMs  = 0;
  let maxTimeMs      = 0;        // race duration (ms) — set by rebuildMaxTime()
  let lastTs         = null;
  let rafHandle      = null;
  let changeId       = 0;

  // Per-boat wake ring buffers: Map<id, {buf: [{x,y}], head: int}>
  const wakeBuffers = new Map();
  const WAKE_LEN = 8;

  // ── Time formatting ────────────────────────────────────────────────────────
  function fmtTime(ms) {
    const totalSec = ms / 1000;
    const m = Math.floor(totalSec / 60);
    const s = totalSec - m * 60;
    const sf = s.toFixed(1);
    const sStr = parseFloat(sf) < 10 ? "0" + sf : sf;
    return m + ":" + sStr;
  }

  // ── Compute race duration from stroke data ─────────────────────────────────
  function rebuildMaxTime() {
    if (!races || races.length === 0) { maxTimeMs = 0; return; }

    if (eventType === "dist") {
      // Use the official finish_time_s when available (Python-guaranteed); fall
      // back to last stroke time if missing.
      let maxSec = 0;
      for (const boat of races) {
        if (boat.finish_time_s != null) {
          maxSec = Math.max(maxSec, boat.finish_time_s);
        } else {
          const s = boat.strokes;
          if (!s || s.length === 0) continue;
          maxSec = Math.max(maxSec, s[s.length - 1].t);
        }
      }
      maxTimeMs = Math.ceil(maxSec * 1000) + 2000; // 2s buffer after finish
    } else {
      // Time event: event_value is in tenths of seconds
      maxTimeMs = Math.round(eventValue * 100); // tenths → ms
    }

    seekInput.max = maxTimeMs;
    totalDisplay.textContent = fmtTime(maxTimeMs);
  }

  // ── Boat distance at a given race time ─────────────────────────────────────
  function getBoatDistance(boat, timeMs) {
    const strokes = boat.strokes;
    if (!strokes || strokes.length === 0) return 0;

    const timeSec = timeMs / 1000;
    const last = strokes[strokes.length - 1];
    if (timeSec >= last.t) return last.d;
    if (timeSec <= 0) return 0;

    // Binary search for surrounding points
    let lo = 0, hi = strokes.length - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (strokes[mid].t <= timeSec) lo = mid; else hi = mid;
    }
    const a = strokes[lo];
    const b = strokes[Math.min(lo + 1, strokes.length - 1)];
    if (a.t === b.t) return a.d;
    const frac = Math.max(0, Math.min(1, (timeSec - a.t) / (b.t - a.t)));
    return a.d + frac * (b.d - a.d);
  }

  // ── Check if a distance-event boat has finished ────────────────────────────
  function hasFinished(boat, timeMs) {
    if (eventType !== "dist") return false;
    const targetDist = eventValue; // meters
    return getBoatDistance(boat, timeMs) >= targetDist;
  }

  function finishTimeMs(boat) {
    // Prefer the Python-supplied official finish time for accuracy.
    if (boat.finish_time_s != null) return boat.finish_time_s * 1000;

    // Fallback: binary search on stroke array to find when boat first hit targetDist
    const targetDist = eventValue;
    const strokes = boat.strokes;
    if (!strokes || strokes.length === 0) return maxTimeMs;
    const last = strokes[strokes.length - 1];
    if (last.d < targetDist) return maxTimeMs;

    let lo = 0, hi = strokes.length - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (strokes[mid].d < targetDist) lo = mid; else hi = mid;
    }
    const a = strokes[lo], b = strokes[Math.min(lo + 1, strokes.length - 1)];
    if (a.d === b.d) return a.t * 1000;
    const frac = Math.max(0, (targetDist - a.d) / (b.d - a.d));
    return (a.t + frac * (b.t - a.t)) * 1000;
  }

  // ── Compute leading distance for time events ───────────────────────────────
  function getLeadingDistance(timeMs) {
    let maxD = 0;
    for (const boat of races) {
      maxD = Math.max(maxD, getBoatDistance(boat, timeMs));
    }
    return maxD || 1;
  }

  // ── Colors ─────────────────────────────────────────────────────────────────
  function bgColor()        { return isDark ? "#1a1a2e" : "#f0f4ff"; }
  function laneLineColor()  { return isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.07)"; }
  function textColor()      { return isDark ? "rgba(255,255,255,0.82)" : "rgba(0,0,0,0.78)"; }
  function dimTextColor()   { return isDark ? "rgba(255,255,255,0.40)" : "rgba(0,0,0,0.38)"; }
  function finishLineColor(){ return isDark ? "rgba(255,255,255,0.30)" : "rgba(0,0,0,0.28)"; }
  function waterColor()     { return isDark ? "rgba(30,60,120,0.25)" : "rgba(180,210,255,0.30)"; }

  // ── Canvas DPR helper ──────────────────────────────────────────────────────
  let dpr = window.devicePixelRatio || 1;

  function resizeCanvas() {
    dpr = window.devicePixelRatio || 1;
    const w = canvasWrap.clientWidth;
    const h = canvasWrap.clientHeight;
    if (w <= 0 || h <= 0) return;
    canvas.width  = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }

  // ── Scull drawing helper ───────────────────────────────────────────────────
  // Draws a narrow, pointed-bow hull shape centred at (cx, cy).
  // hl = half-length (horizontal extent each side from centre)
  // hw = half-width  (vertical extent at the widest point)
  // Bow points to the right (direction of travel on the canvas).
  function drawScull(ctx2d, cx, cy, hl, hw, color, isPb, darkMode) {
    ctx2d.save();
    ctx2d.translate(cx, cy);

    ctx2d.beginPath();
    // Start at bow (sharp right tip)
    ctx2d.moveTo(hl, 0);
    // Upper edge: bow → widest point → stern (rounded)
    ctx2d.bezierCurveTo(hl * 0.5, -hw, -hl * 0.3, -hw, -hl, 0);
    // Lower edge: stern → widest point → bow (mirror)
    ctx2d.bezierCurveTo(-hl * 0.3, hw, hl * 0.5, hw, hl, 0);
    ctx2d.closePath();

    ctx2d.fillStyle = color;
    ctx2d.fill();

    if (isPb) {
      ctx2d.lineWidth = 2;
      ctx2d.strokeStyle = darkMode ? "rgba(255,255,255,0.85)" : "rgba(255,255,255,0.95)";
      ctx2d.stroke();
    }

    ctx2d.restore();
  }

  // ── Main render function ────────────────────────────────────────────────────
  function renderFrame(timeMs) {
    resizeCanvas();
    const ctx2d = canvas.getContext("2d");
    ctx2d.save();
    ctx2d.scale(dpr, dpr);

    const W = canvasWrap.clientWidth;
    const H = canvasWrap.clientHeight;
    if (W <= 0 || H <= 0) { ctx2d.restore(); return; }

    // ── Background ──
    ctx2d.fillStyle = bgColor();
    ctx2d.fillRect(0, 0, W, H);

    const numBoats = races.length;
    if (numBoats === 0) {
      ctx2d.fillStyle = dimTextColor();
      ctx2d.font = "14px sans-serif";
      ctx2d.textAlign = "center";
      ctx2d.fillText("No qualifying workouts for this event.", W / 2, H / 2);
      ctx2d.restore();
      return;
    }

    // ── Layout constants ──
    const HEADER_H = 26;
    const LABEL_W  = 160;  // wide enough for "Jan. 26th, 2019"
    const RESULT_W = 88;
    const TRACK_L  = LABEL_W;
    const TRACK_R  = W - RESULT_W;
    const TRACK_W  = TRACK_R - TRACK_L;
    const LANE_H   = Math.max(20, Math.min(44, (H - HEADER_H) / numBoats));
    const BOAT_R   = 6;   // half-width of scull hull
    const PB_R     = 8;   // half-width of PB scull hull

    // ── Header row ──
    const eventLabel = _fmtEventLabel();
    ctx2d.fillStyle = textColor();
    ctx2d.font = "bold 13px sans-serif";
    ctx2d.textAlign = "left";
    ctx2d.fillText(eventLabel, LABEL_W + 6, 17);

    ctx2d.font = "bold 13px monospace";
    ctx2d.textAlign = "right";
    ctx2d.fillStyle = dimTextColor();
    ctx2d.fillText(fmtTime(timeMs), W - RESULT_W - 6, 17);

    // ── Finish line (distance events) ──
    if (eventType === "dist") {
      ctx2d.save();
      ctx2d.setLineDash([5, 4]);
      ctx2d.strokeStyle = finishLineColor();
      ctx2d.lineWidth = 1.5;
      ctx2d.beginPath();
      ctx2d.moveTo(TRACK_R, HEADER_H);
      ctx2d.lineTo(TRACK_R, H);
      ctx2d.stroke();
      ctx2d.setLineDash([]);
      ctx2d.restore();

      ctx2d.fillStyle = dimTextColor();
      ctx2d.font = "10px sans-serif";
      ctx2d.textAlign = "center";
      ctx2d.fillText("FINISH", TRACK_R, HEADER_H + 10);
    }

    // ── Time event: progress ruler ──
    if (eventType === "time") {
      const leadDist = getLeadingDistance(timeMs);
      ctx2d.fillStyle = dimTextColor();
      ctx2d.font = "10px sans-serif";
      ctx2d.textAlign = "center";
      const kmLabel = leadDist >= 1000
        ? (leadDist / 1000).toFixed(2) + " km"
        : Math.round(leadDist) + " m";
      ctx2d.fillText(kmLabel + " (leader)", TRACK_R - 4, HEADER_H + 10);
    }

    // ── Lanes ──
    const leadDist = eventType === "time" ? getLeadingDistance(timeMs) : eventValue;

    for (let i = 0; i < numBoats; i++) {
      const boat = races[i];
      const laneY = HEADER_H + i * LANE_H;
      const midY  = laneY + LANE_H / 2;

      // Lane background (subtle water tint for alternating lanes)
      if (i % 2 === 0) {
        ctx2d.fillStyle = waterColor();
        ctx2d.fillRect(LABEL_W, laneY, TRACK_W, LANE_H);
      }

      // Lane separator
      ctx2d.strokeStyle = laneLineColor();
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.moveTo(0, laneY);
      ctx2d.lineTo(W, laneY);
      ctx2d.stroke();

      // ── Boat position ──
      const dist = getBoatDistance(boat, timeMs);
      const finished = eventType === "dist" && dist >= eventValue;
      let boatX;
      if (eventType === "dist") {
        const clampedDist = Math.min(dist, eventValue);
        boatX = TRACK_L + (clampedDist / eventValue) * TRACK_W;
      } else {
        boatX = TRACK_L + (dist / leadDist) * TRACK_W;
      }

      // ── Wake trail ──
      if (!wakeBuffers.has(boat.id)) {
        wakeBuffers.set(boat.id, { buf: [], head: 0 });
      }
      const wb = wakeBuffers.get(boat.id);

      // Only push new wake point if boat has moved (avoids static trail on pause)
      const prevWake = wb.buf[wb.buf.length - 1];
      if (!prevWake || Math.abs(prevWake.x - boatX) > 0.5) {
        wb.buf.push({ x: boatX, y: midY });
        if (wb.buf.length > WAKE_LEN) wb.buf.shift();
      }

      for (let w = 0; w < wb.buf.length - 1; w++) {
        const frac = (w + 1) / wb.buf.length;
        const wakeR = Math.max(1, BOAT_R * 0.7 * frac);
        const alpha = 0.45 * frac * frac;
        ctx2d.beginPath();
        ctx2d.arc(wb.buf[w].x, wb.buf[w].y, wakeR, 0, Math.PI * 2);
        ctx2d.fillStyle = hexWithAlpha(boat.color, alpha);
        ctx2d.fill();
      }

      // ── Scull (elongated hull, bow pointing right) ──
      const hullHW = boat.is_pb ? PB_R : BOAT_R; // half-width (vertical)
      const hullHL = hullHW * 3.2;               // half-length (horizontal)
      drawScull(ctx2d, boatX, midY, hullHL, hullHW, boat.color, boat.is_pb, isDark);

      // ── Label (left zone) ──
      ctx2d.fillStyle = boat.color;
      ctx2d.font = `${boat.is_pb ? "bold " : ""}11px sans-serif`;
      ctx2d.textAlign = "right";
      ctx2d.fillText(boat.label, LABEL_W - 8, midY + 4);

      // ── Result (right zone) ──
      ctx2d.textAlign = "left";
      if (eventType === "dist" && finished) {
        const fms = finishTimeMs(boat);
        ctx2d.fillStyle = boat.is_pb ? "#ffc107" : dimTextColor();
        ctx2d.font = `${boat.is_pb ? "bold " : ""}11px monospace`;
        ctx2d.fillText(fmtTime(fms), TRACK_R + 6, midY + 4);
      } else if (eventType === "time") {
        const dStr = dist >= 1000
          ? (dist / 1000).toFixed(2) + "k"
          : Math.round(dist) + "m";
        ctx2d.fillStyle = boat.is_pb ? "#ffc107" : dimTextColor();
        ctx2d.font = "11px monospace";
        ctx2d.fillText(dStr, TRACK_R + 6, midY + 4);
      }
    }

    // Final lane bottom border
    const lastLaneBottom = HEADER_H + numBoats * LANE_H;
    ctx2d.strokeStyle = laneLineColor();
    ctx2d.lineWidth = 1;
    ctx2d.beginPath();
    ctx2d.moveTo(0, lastLaneBottom);
    ctx2d.lineTo(W, lastLaneBottom);
    ctx2d.stroke();

    ctx2d.restore();
  }

  // ── Event label helper ─────────────────────────────────────────────────────
  function _fmtEventLabel() {
    if (eventType === "dist") {
      const m = eventValue;
      if (m >= 1000) return (m / 1000).toFixed(m % 1000 === 0 ? 0 : 3) + "k Race";
      return m + "m Race";
    } else {
      const secs = eventValue / 10;
      const mins = Math.round(secs / 60);
      return mins + "-Minute Race";
    }
  }

  // ── Hex color with alpha ───────────────────────────────────────────────────
  function hexWithAlpha(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha.toFixed(2)})`;
  }

  // ── rAF animation loop ─────────────────────────────────────────────────────
  function startRaf() {
    lastTs = null;
    if (rafHandle) cancelAnimationFrame(rafHandle);
    rafHandle = requestAnimationFrame(function tick(ts) {
      if (!playing) return;
      if (lastTs !== null) {
        currentTimeMs += (ts - lastTs) * playbackSpeed;
      }
      lastTs = ts;
      if (currentTimeMs >= maxTimeMs) {
        currentTimeMs = maxTimeMs;
        playing = false;
        updatePlayBtn();
      }
      renderFrame(currentTimeMs);
      updateSeekDisplay();
      if (playing) rafHandle = requestAnimationFrame(tick);
    });
  }

  function stopRaf() {
    playing = false;
    if (rafHandle) cancelAnimationFrame(rafHandle);
    rafHandle = null;
    lastTs = null;
  }

  function updatePlayBtn() {
    playBtn.textContent = playing ? "⏸" : "▶";
  }

  function updateSeekDisplay() {
    seekInput.value = currentTimeMs;
    timeDisplay.textContent = fmtTime(currentTimeMs);
  }

  // ── Reset race to start ────────────────────────────────────────────────────
  function resetRace() {
    stopRaf();
    currentTimeMs = 0;
    wakeBuffers.clear();
    updatePlayBtn();
    updateSeekDisplay();
  }

  // ── Controls event listeners ───────────────────────────────────────────────
  playBtn.addEventListener("click", () => {
    if (playing) {
      stopRaf();
      updatePlayBtn();
      renderFrame(currentTimeMs);
    } else {
      // If at end, restart from beginning
      if (currentTimeMs >= maxTimeMs) {
        currentTimeMs = 0;
        wakeBuffers.clear();
        updateSeekDisplay();
      }
      playing = true;
      updatePlayBtn();
      startRaf();
    }
  });

  seekInput.addEventListener("input", () => {
    stopRaf();
    currentTimeMs = parseFloat(seekInput.value);
    wakeBuffers.clear();
    renderFrame(currentTimeMs);
    timeDisplay.textContent = fmtTime(currentTimeMs);
    // Report back to Python
    changeId++;
    ctx.updateProp("change_id", changeId);
    ctx.updateProp("current_time_ms", Math.round(currentTimeMs));
  });

  speedSelect.addEventListener("change", () => {
    playbackSpeed = parseFloat(speedSelect.value);
    if (playing) {
      // Restart rAF with new speed (resets lastTs so no jump)
      lastTs = null;
    }
  });

  // ── Prop updates from Python ───────────────────────────────────────────────
  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "races") {
      races = propValue || [];
      wakeBuffers.clear();
      rebuildMaxTime();
      resetRace();
      renderFrame(0);
    } else if (propName === "event_type") {
      eventType = propValue || "dist";
      rebuildMaxTime();
      resetRace();
      renderFrame(0);
    } else if (propName === "event_value") {
      eventValue = propValue || 2000;
      rebuildMaxTime();
      resetRace();
      renderFrame(0);
    } else if (propName === "is_dark") {
      isDark = !!propValue;
      renderFrame(currentTimeMs);
    }
  });

  // ── Responsive resize ──────────────────────────────────────────────────────
  const ro = new ResizeObserver(() => {
    renderFrame(currentTimeMs);
  });
  ro.observe(canvasWrap);

  // ── Initial render ─────────────────────────────────────────────────────────
  rebuildMaxTime();
  renderFrame(0);
  updateSeekDisplay();

}); // end registerPlugin

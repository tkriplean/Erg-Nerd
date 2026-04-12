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
 * Playback speed presets: Slow (2 min), Normal (45 s), Fast (25 s), Very fast (10 s).
 * Each preset delivers a fixed real-time duration regardless of race length.
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

  // Speed presets: each delivers a fixed real-time playback duration regardless
  // of how long the race is, so a marathon and a sprint both feel engaging.
  const SPEED_PRESETS = [
    { label: "Slow",      targetMs: 120000 },
    { label: "Normal",    targetMs: 45000  },
    { label: "Fast",      targetMs: 25000  },
    { label: "Very fast", targetMs: 10000  },
  ];
  const speedSelect = document.createElement("select");
  speedSelect.className = "race-speed";
  SPEED_PRESETS.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p.label;
    opt.textContent = p.label;
    if (p.label === "Normal") opt.selected = true;
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

  let playing           = false;
  let selectedPreset    = "Normal";   // which SPEED_PRESETS entry is active
  let currentTimeMs     = 0;

  // Compute actual multiplier from selected preset + current race duration.
  function playbackSpeed() {
    const p = SPEED_PRESETS.find(x => x.label === selectedPreset) || SPEED_PRESETS[1];
    return maxTimeMs > 0 ? maxTimeMs / p.targetMs : 1;
  }
  let maxTimeMs           = 0;   // race duration (ms) — set by rebuildMaxTime()
  let maxDistForTimeEvent = 1;   // time events: furthest any boat rows (normalization)
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
      // Use the official finish_dist_m (Python-authoritative) when available,
      // otherwise fall back to the last stroke's recorded distance.
      // This is the fixed normalizer — computed once so boats don't snap to the
      // right edge at t=0 when dividing by a near-zero instantaneous leader dist.
      maxDistForTimeEvent = 1;
      for (const boat of races) {
        if (boat.finish_dist_m != null) {
          maxDistForTimeEvent = Math.max(maxDistForTimeEvent, boat.finish_dist_m);
        } else {
          const s = boat.strokes;
          if (s && s.length > 0) {
            maxDistForTimeEvent = Math.max(maxDistForTimeEvent, s[s.length - 1].d);
          }
        }
      }
    }

    seekInput.max = maxTimeMs;
    totalDisplay.textContent = fmtTime(maxTimeMs);
    // Speed is computed dynamically via playbackSpeed() — no static calibration needed.
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

  // ── Time (seconds) for a boat to reach a given distance ──────────────────
  // Returns null if the boat never reaches that distance.
  function timeToReachDist(boat, targetDist) {
    const strokes = boat.strokes;
    if (!strokes || strokes.length === 0) return null;
    const last = strokes[strokes.length - 1];
    if (last.d < targetDist) return null;
    let lo = 0, hi = strokes.length - 1;
    while (lo < hi - 1) {
      const mid = (lo + hi) >> 1;
      if (strokes[mid].d < targetDist) lo = mid; else hi = mid;
    }
    const a = strokes[lo], b = strokes[Math.min(lo + 1, strokes.length - 1)];
    if (a.d === b.d) return a.t;
    const frac = Math.max(0, (targetDist - a.d) / (b.d - a.d));
    return a.t + frac * (b.t - a.t);
  }

  // ── Colors ─────────────────────────────────────────────────────────────────
  function bgColor()        { return isDark ? "#1a1a2e" : "#f0f4ff"; }
  function laneLineColor()  { return isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.07)"; }
  function textColor()      { return isDark ? "rgba(255,255,255,1)" : "rgba(0,0,0,1)"; }
  function dimTextColor()   { return isDark ? "rgba(255,255,255,1)" : "rgba(0,0,0,1)"; }
  function finishLineColor(){ return isDark ? "rgba(255,255,255,1)" : "rgba(0,0,0,1)"; }
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

    // if (isPb) {
    //   ctx2d.lineWidth = 2;
    //   ctx2d.strokeStyle = darkMode ? "rgba(255,255,255,0.85)" : "rgba(255,255,255,0.95)";
    //   ctx2d.stroke();
    // }

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
    const PB_R     = BOAT_R;   // half-width of PB scull hull

    // ── Header row ──
    // const eventLabel = _fmtEventLabel();
    // ctx2d.fillStyle = textColor();
    // ctx2d.font = "bold 13px sans-serif";
    // ctx2d.textAlign = "left";
    // ctx2d.fillText(eventLabel, LABEL_W + 6, 17);

    ctx2d.font = "bold 13px monospace";
    ctx2d.textAlign = "right";
    ctx2d.fillStyle = dimTextColor();
    ctx2d.fillText(fmtTime(timeMs), W - RESULT_W - 6, 17);

    // ── Hull geometry constants (used throughout render) ──
    const BASE_HW = BOAT_R;
    const BASE_HL = BASE_HW * 3.2;
    const PB_HW   = BASE_HW;
    const PB_HL   = BASE_HL;
    // TRACK_INNER_BASE: the distance the BASE hull centre travels (stern→bow span)
    // Used to map "metres into race" → canvas X for split lines, consistent with
    // the per-boat position formula below.
    const TRACK_INNER_BASE = TRACK_W - 2 * BASE_HL;

    // ── Distance normalizer (how far "right edge" represents) ──
    const normDist = eventType === "dist" ? eventValue : maxDistForTimeEvent;

    // ── Helper: metres → canvas X (BOW position of BASE hull) ──
    // Consistent with the corrected per-boat boatCx formula:
    //   centre = TRACK_L + BASE_HL + frac * TRACK_INNER_BASE
    //   bow    = centre + BASE_HL = TRACK_L + 2*BASE_HL + frac * TRACK_INNER_BASE
    // At metres=0       → TRACK_L + 2*BASE_HL  (bow starts 2 hull-lengths from track edge)
    // At metres=normDist → TRACK_R              (bow exactly touches finish/right edge)
    function distToX(metres) {
      const frac = Math.min(1, metres / normDist);
      return TRACK_L + 2 * BASE_HL + frac * TRACK_INNER_BASE;
    }

    // ── Precompute finish ranks for medal display ──
    // Dist events: ranked by finish time as boats cross the line.
    // Time events: ranked by final distance, shown only at end of race.
    const finishRanks = new Map(); // boatId → rank (1 = gold, 2 = silver, 3 = bronze)
    const atEnd = timeMs >= maxTimeMs - 50;
    if (eventType === "dist") {
      const finishedNow = races
        .filter(b => getBoatDistance(b, timeMs) >= eventValue)
        .map(b => ({ id: b.id, fms: finishTimeMs(b) }))
        .sort((a, b) => a.fms - b.fms);
      finishedNow.forEach(({ id }, idx) => finishRanks.set(id, idx + 1));
    } else if (atEnd) {
      // Time event: rank by official final distance (highest = 1st)
      const ranked = races
        .map(b => ({
          id: b.id,
          d: b.finish_dist_m != null ? b.finish_dist_m : getBoatDistance(b, maxTimeMs),
        }))
        .sort((a, b) => b.d - a.d);
      ranked.forEach(({ id }, idx) => finishRanks.set(id, idx + 1));
    }

    // ── Split interval ──
    // Choose an interval that gives AT LEAST 3 interior checkpoints.
    function getSplitInterval(targetDist) {
      const candidates = [5000, 2000, 1000, 500, 250, 100];
      for (const c of candidates) {
        if (Math.floor((targetDist - 1) / c) >= 3) return c;
      }
      return Math.max(1, Math.floor(targetDist / 4));
    }
    const splitInterval = getSplitInterval(normDist);

    // ── Split lines + finish marker ──
    {
      ctx2d.save();
      ctx2d.setLineDash([3, 5]);
      ctx2d.lineWidth = 1;
      ctx2d.strokeStyle = finishLineColor();
      ctx2d.font = "11px sans-serif";
      ctx2d.textAlign = "center";
      ctx2d.fillStyle = dimTextColor();
      for (let sd = splitInterval; sd < normDist; sd += splitInterval) {
        const sx = distToX(sd);
        ctx2d.beginPath();
        ctx2d.moveTo(sx, HEADER_H);
        ctx2d.lineTo(sx, H);
        ctx2d.stroke();
        const splitLbl = sd >= 1000 ? (sd / 1000) + "k" : sd + "m";
        ctx2d.fillText(splitLbl, sx, HEADER_H - 4);
      }
      ctx2d.setLineDash([]);
      ctx2d.restore();

      if (eventType === "dist") {
        // Finish line: bow of BASE hull reaches TRACK_R at dist = normDist
        const finishX = TRACK_R;
        ctx2d.save();
        ctx2d.setLineDash([5, 4]);
        ctx2d.strokeStyle = finishLineColor();
        ctx2d.lineWidth = 1.5;
        ctx2d.beginPath();
        ctx2d.moveTo(finishX, HEADER_H);
        ctx2d.lineTo(finishX, H);
        ctx2d.stroke();
        ctx2d.setLineDash([]);
        ctx2d.restore();
        ctx2d.fillStyle = dimTextColor();
      }
    }

    // ── Compute per-boat gap labels at each split ──
    // A label appears only after the boat's stern has fully cleared the split line
    // plus a few pixels of padding so it never overlaps the hull in motion.
    // "Clearance metres" = hull full-length (2 × BASE_HL mapped back to metres) + 8px pad.
    const splitPositions = [];
    const numSplitLines = Math.floor((normDist - 1) / splitInterval);
    for (let si = 0; si < numSplitLines; si++) {
      splitPositions.push((si + 1) * splitInterval); // metres, not canvas X
    }

    // metres that correspond to one hull-length + 8px padding on canvas
    const clearanceMetres = (2 * BASE_HL + 8) / TRACK_INNER_BASE * normDist;

    const leaderTimes = splitPositions.map(sd => {
      let best = Infinity;
      for (const boat of races) {
        const t = timeToReachDist(boat, sd);
        if (t !== null && t < best) best = t;
      }
      return best === Infinity ? null : best;
    });

    const gapLabels = [];
    for (let i = 0; i < numBoats; i++) {
      const boatDist = getBoatDistance(races[i], timeMs);
      for (let si = 0; si < splitPositions.length; si++) {
        const sd = splitPositions[si];
        // Show only after stern has cleared: boat's bow must be > sd + hull + padding
        if (boatDist < sd + clearanceMetres) continue;
        const leaderT = leaderTimes[si];
        if (leaderT === null) continue;
        const boatT = timeToReachDist(races[i], sd);
        if (boatT === null) continue;
        gapLabels.push({ boatIdx: i, splitIdx: si, gapSec: boatT - leaderT });
      }
    }

    // ── Lanes ──

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

      // ── Boat geometry ──
      const hullHW = boat.is_pb ? PB_HW : BASE_HW;
      const hullHL = boat.is_pb ? PB_HL : BASE_HL;

      // ── Boat position ──
      // boatCx travels from (TRACK_L + hullHL) to (TRACK_R - hullHL).
      //   dist = 0         → boatCx = TRACK_L + hullHL  → stern = TRACK_L  (start line)
      //   dist = normDist  → boatCx = TRACK_R - hullHL  → bow   = TRACK_R  (finish line)
      // The label zone (left of TRACK_L) is never overlapped by the hull at any point.
      const dist = getBoatDistance(boat, timeMs);
      const finished = eventType === "dist" && dist >= eventValue;
      const clampedDist = Math.min(dist, normDist);
      const TRACK_INNER = TRACK_W - 2 * hullHL;
      const boatCx = TRACK_L + hullHL + (clampedDist / normDist) * TRACK_INNER;

      // ── Wake trail (follows hull centre) ──
      if (!wakeBuffers.has(boat.id)) {
        wakeBuffers.set(boat.id, { buf: [], head: 0 });
      }
      const wb = wakeBuffers.get(boat.id);

      const prevWake = wb.buf[wb.buf.length - 1];
      if (!prevWake || Math.abs(prevWake.x - boatCx) > 0.5) {
        wb.buf.push({ x: boatCx, y: midY });
        if (wb.buf.length > WAKE_LEN) wb.buf.shift();
      }

      for (let w = 0; w < wb.buf.length - 1; w++) {
        const frac = (w + 1) / wb.buf.length;
        const wakeR = Math.max(1, hullHW * 0.7 * frac);
        const alpha = 0.45 * frac * frac;
        ctx2d.beginPath();
        ctx2d.arc(wb.buf[w].x, wb.buf[w].y, wakeR, 0, Math.PI * 2);
        ctx2d.fillStyle = hexWithAlpha(boat.color, alpha);
        ctx2d.fill();
      }

      // ── Scull (elongated hull, bow pointing right) ──
      drawScull(ctx2d, boatCx, midY, hullHL, hullHW, boat.color, boat.is_pb, isDark);

      // ── Label (left zone) — always shows date ──
      ctx2d.fillStyle = boat.color;
      ctx2d.font = `${boat.is_pb ? "bold " : ""}13px sans-serif`;
      ctx2d.textAlign = "right";
      ctx2d.fillText(boat.label, LABEL_W - 8, midY + 4);

      // ── Result zone (right of finish line) ──
      // rank is defined for: all finished dist boats, and top-3 time boats at end of race.
      const rank = finishRanks.get(boat.id); // undefined if not yet ranked
      const medal = rank === 1 ? " 🥇" : rank === 2 ? " 🥈" : rank === 3 ? " 🥉" : "";

      if (eventType === "dist" && finished) {
        let textX = TRACK_R + 5;
        const fms = finishTimeMs(boat);
        ctx2d.fillStyle = boat.is_pb ? "#ffc107" : (rank && rank <= 3 ? textColor() : dimTextColor());
        ctx2d.font = `${boat.is_pb || (rank && rank <= 3) ? "bold " : ""}13px monospace`;
        ctx2d.textAlign = "left";
        ctx2d.fillText(fmtTime(fms)+medal, textX, midY + 4);
      } else if (eventType === "time") {
        // Show running distance; at end snap to official value and show medals.
        const displayDist = (atEnd && boat.finish_dist_m != null) ? boat.finish_dist_m : dist;
        const dStr = displayDist >= 1000
          ? (displayDist / 1000).toFixed(2) + "k"
          : Math.round(displayDist) + "m";
        let textX = TRACK_R + 5;
        ctx2d.fillStyle = boat.is_pb ? "#ffc107" : (rank && rank <= 3 ? textColor() : dimTextColor());
        ctx2d.font = `${boat.is_pb || (rank && rank <= 3) ? "bold " : ""}13px monospace`;
        ctx2d.textAlign = "left";
        ctx2d.fillText(dStr+medal, textX, midY + 4);
        console.log(textX)
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

    // ── Gap-to-leader labels at split lines ──
    // Render after all boats so they sit on top of the lanes.
    // Leader shows no label (gap = 0); others show "+X.Xs" in their boat colour.
    for (const { boatIdx, splitIdx, gapSec } of gapLabels) {
      if (gapSec <= 0.05) continue; // leader — skip
      const boat = races[boatIdx];
      const laneY = HEADER_H + boatIdx * LANE_H;
      const midY  = laneY + LANE_H / 2;
      const sx    = distToX(splitPositions[splitIdx]); // metres → canvas X
      const label = "+" + gapSec.toFixed(1) + "s";

      // Tiny pill background so label is readable over the split line
      ctx2d.font = "9px sans-serif";
      const tw = ctx2d.measureText(label).width;
      const px = 3, py = 2;
      ctx2d.fillStyle = isDark ? "rgba(20,20,40,1)" : "rgba(240,244,255,1)";
      ctx2d.beginPath();
      ctx2d.roundRect(sx - tw / 2 - px, midY - 7 - py, tw + px * 2, 10 + py * 2, 3);
      ctx2d.fill();

      ctx2d.fillStyle = hexWithAlpha(boat.color, 0.9);
      ctx2d.textAlign = "center";
      ctx2d.font = "13px monospace";
      ctx2d.fillText(label, sx, midY + 3);
    }

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
        currentTimeMs += (ts - lastTs) * playbackSpeed();
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
    selectedPreset = speedSelect.value;
    if (playing) lastTs = null; // reset timing so the new speed takes effect cleanly
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

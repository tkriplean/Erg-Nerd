window.hyperdiv.registerPlugin("DateSlider", (ctx) => {
  // -------------------------------------------------------------------------
  // Shadow-DOM styles
  // -------------------------------------------------------------------------
  const style = document.createElement("style");
  style.textContent = `
    :host {
      display: block;
      width: 100%;
      overflow: visible;
      --thumb-size: 22px;
      --tip-bg:      var(--sl-tooltip-background-color, #1e293b);
      --tip-fg:      var(--sl-tooltip-color, #fff);
    }

    .wrap {
      position: relative;
      padding: 36px 18px 36px 18px;
    }

    /* Tooltip — floats above the thumb or above a hovered dot */
    .tip {
      position: absolute;
      bottom: calc(62% + 8px);
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

    /* Row of SB annotation dots below the slider track */
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
    .dot:hover {
      transform: translateX(-50%) scale(1.45);
    }
  `;
  ctx.domElement.appendChild(style);

  // -------------------------------------------------------------------------
  // Date formatter
  // -------------------------------------------------------------------------
  let startDate = ctx.initialProps.start_date
    ? new Date(ctx.initialProps.start_date + "T00:00:00")
    : new Date();

  function formatDate(dayOffset) {
    const d = new Date(startDate);
    d.setDate(d.getDate() + Number(dayOffset));
    return d.toLocaleDateString("en-US", {
      month: "short",
      day:   "numeric",
      year:  "numeric",
    });
  }

  // -------------------------------------------------------------------------
  // DOM
  // -------------------------------------------------------------------------
  const wrap = document.createElement("div");
  wrap.className = "wrap";
  ctx.domElement.appendChild(wrap);

  const input = document.createElement("sl-range");

  input.min   = ctx.initialProps.min_value ?? 0;
  input.max   = ctx.initialProps.max_value ?? 100;
  input.step  = ctx.initialProps.step ?? 1;
  // target_value is the Python-owned position; value is the JS-reported position.
  input.value = ctx.initialProps.target_value ?? ctx.initialProps.value ?? 0;
  input.tooltipFormatter = formatDate
  wrap.appendChild(input);

  const tip = document.createElement("div");
  tip.className = "tip";
  wrap.appendChild(tip);

  const annRow = document.createElement("div");
  annRow.className = "ann-row";
  wrap.appendChild(annRow);


  // -------------------------------------------------------------------------
  // Track fill — gradient left of thumb shows elapsed progress
  // -------------------------------------------------------------------------
  function updateFill() {
    const min = Number(input.min);
    const max = Number(input.max);
    const val = Number(input.value);
    const pct = max > min ? (val - min) / (max - min) * 100 : 0;
    input.style.background =
      `linear-gradient(to right, var(--fill-color) ${pct}%, var(--track-color) ${pct}%)`;
  }

  // -------------------------------------------------------------------------
  // SB annotation dots
  // -------------------------------------------------------------------------
  let annotations = Array.isArray(ctx.initialProps.annotations)
    ? ctx.initialProps.annotations
    : [];

  // Track which dot is hovered so we can hide the tip on mouseleave
  let annHoverActive = false;

  function buildDots() {
    annRow.innerHTML = "";
    annHoverActive = false;

    const min      = Number(input.min);
    const max      = Number(input.max);
    const halfThumb = 11;
    const trackW   = input.offsetWidth || 200;

    annotations.forEach((ann) => {
      // Skip dots outside the current slider range
      if (ann.day < min || ann.day > max) return;

      const pct  = max > min ? (ann.day - min) / (max - min) : 0;
      const left = halfThumb + pct * (trackW - 2 * halfThumb);

      const dot = document.createElement("div");
      dot.className = "dot";
      dot.style.left       = left + "px";
      dot.style.background = ann.color;

      dot.addEventListener("mouseenter", () => {
        annHoverActive = true;
        tip.textContent = ann.label;
        tip.style.left  = left + "px";
        tip.classList.add("show");
      });

      dot.addEventListener("mouseleave", () => {
        annHoverActive = false;
        tip.classList.remove("show");
      });

      dot.addEventListener("click", (e) => {
        e.stopPropagation();
        // Seek to one day before the SB so the SB appears on the next step.
        const seekDay = Math.max(Number(input.min), ann.day - 1);
        input.value = seekDay;
        updateFill();
        changeId += 1;
        ctx.updateProp("change_id", changeId);
        ctx.updateProp("value", seekDay);
      });

      annRow.appendChild(dot);
    });
  }

  updateFill();  // set initial fill
  buildDots();

  // Reposition dots (and slider tip) when the track width changes
  const resizeObserver = new ResizeObserver(() => {
    buildDots();
    updateFill();
  });
  resizeObserver.observe(input);

  // -------------------------------------------------------------------------
  // Debounced server update + change_id tracking
  // -------------------------------------------------------------------------
  let changeId     = ctx.initialProps.change_id ?? 0;
  let debounceTimer = null;

  function sendToServer() {
    changeId += 1;
    ctx.updateProp("change_id", changeId);
    ctx.updateProp("value", Number(input.value));
  }

  // Show tooltip and start debounce on every drag movement
  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(sendToServer, 250);
  });

  
  // Flush immediately on release (change fires after mouseup/touchend)
  input.addEventListener("change", () => {
    clearTimeout(debounceTimer);
    sendToServer();
  });

  // Hide drag tooltip when drag ends (but not if a dot tooltip is active)
  document.addEventListener("mouseup", () => {
    if (!annHoverActive) tip.classList.remove("show");
  });
  document.addEventListener("touchend", () => {
    if (!annHoverActive) tip.classList.remove("show");
  });

  // -------------------------------------------------------------------------
  // Receive prop updates from Python
  // change_id is never sent by Python — it flows JS→Python only.
  // -------------------------------------------------------------------------
  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "target_value") {
      // Python drives the thumb position (animation ticks, button seeks).
      // This prop is never written by JS, so HyperDiv never marks it mutated,
      // meaning Python updates are never silently dropped.
      input.value = propValue;
    }
    else if (propName === "max_value")  { input.max  = propValue; buildDots(); updateFill(); }
    else if (propName === "min_value")  { input.min  = propValue; buildDots(); updateFill(); }
    else if (propName === "step")       { input.step = propValue; }
    else if (propName === "start_date") {
      startDate = new Date(propValue + "T00:00:00");
    }
    else if (propName === "annotations") {
      annotations = Array.isArray(propValue) ? propValue : [];
      buildDots();
    }
    // value: JS-owned, Python reads it — no handling here.
    // change_id: JS-owned, Python reads it — no handling here.
  });
});

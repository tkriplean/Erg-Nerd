/**
 * VolumeChart — HyperDiv plugin wrapping Chart.js for the stacked-bar
 * volume (meters × pace zone) chart on the Sessions tab.
 *
 * Props received from Python:
 *   config  — full Chart.js config dict (type, data, options)
 *
 * JS-injected behaviour:
 *   - Y-axis ticks formatted as meters ("10.5k", "500m", …) or "%"
 *   - Tooltip shows each non-zero bin + footer total, in the active unit
 */



window.hyperdiv.registerPlugin("VolumeChart", (ctx) => {
  // ── Shadow DOM setup ────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; height: 100%; }
    canvas { display: block; width: 100% !important; height: 100% !important; }
  `;
  ctx.domElement.appendChild(style);

  const canvas = document.createElement("canvas");
  ctx.domElement.appendChild(canvas);

  let chartInstance = null;

  // ── Formatters ──────────────────────────────────────────────────────────

  /** Format a raw meter value for display: ≥1000 → "10.5k", else "500m". */
  function fmtMeters(m) {
    const v = Math.round(m);
    if (v >= 1000) {
      const k = v / 1000;
      return (Number.isInteger(k) ? k : k.toFixed(1)) + "k";
    }
    return v + "m";
  }

  // ── Options post-processing: attach JS callbacks ─────────────────────────

  function buildOptions(options) {
    // Deep-clone so we never mutate the prop value.
    const opts = JSON.parse(JSON.stringify(options));
    const isPercent = opts.value_mode === "percent";
    const periodTotals = opts.period_totals || [];

    // Y-axis: format tick values per the active mode.
    if (opts.scales && opts.scales.y) {
      opts.scales.y.ticks = opts.scales.y.ticks || {};
      opts.scales.y.ticks.callback = isPercent
        ? (val) => `${Math.round(val)}%`
        : (val) => fmtMeters(val);
    }

    // Custom tooltip: index mode (shows all datasets for a bar on hover).
    // In percent mode values display as % only; in meters mode as meters.
    opts.plugins = opts.plugins || {};
    opts.plugins.tooltip = {
      mode: "index",
      intersect: false,
      callbacks: {
        title(items) {
          return items.length ? items[0].label : "";
        },
        label(context) {
          const ds = context.dataset || {};
          const val = context.raw || 0;
          if (val === 0) return null;
          if (isPercent) {
            return `${ds.label}:  ${val.toFixed(1)}%`;
          }
          return `${ds.label}:  ${fmtMeters(val)}`;
        },
        footer(items) {
          if (!items.length) return "";
          const total = items.reduce((s, it) => s + (it.raw || 0), 0);
          if (!total) return "";
          if (isPercent) {
            return `Total:  ${total.toFixed(1)}%`;
          }
          return `Total:  ${fmtMeters(total)}`;
        },
      },
    };

    return opts;
  }

  // ── Chart lifecycle ──────────────────────────────────────────────────────

  function applyConfig(config) {
    if (!config) return;
    const processedOpts = buildOptions(config.options);

    if (chartInstance) {
      // Update in place — avoids flash of empty canvas on re-render.
      chartInstance.data = config.data;
      chartInstance.options = processedOpts;
      chartInstance.update("none");
    } else {
      chartInstance = new Chart(canvas, {
        type: config.type,
        data: config.data,
        options: processedOpts,
      });
    }
  }

  // ── Initialise and respond to Python prop updates ─────────────────────────

  let props = { config: ctx.initialProps.config || null };
  applyConfig(props.config);

  ctx.onPropUpdate((propName, propValue) => {
    props[propName] = propValue;
    applyConfig(props.config);
  });
});

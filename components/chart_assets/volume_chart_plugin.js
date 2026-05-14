/**
 * VolumeChart — HyperDiv plugin wrapping Chart.js for the stacked-bar
 * volume (meters × pace zone) chart on the Workouts page.
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

  /**
   * In percent mode, re-normalise each period so the visible datasets sum
   * to 100 % — `raw_m` is the source of truth, the displayed `data` array
   * gets recomputed against the visible-only per-period total.  No-op when
   * not in percent mode.
   */
  function recomputePercents(chart) {
    if (!chart || !chart.options || chart.options.value_mode !== "percent") return;

    const datasets = chart.data.datasets || [];
    const numLabels = (chart.data.labels || []).length;

    const visibleTotals = new Array(numLabels).fill(0);
    for (let di = 0; di < datasets.length; di++) {
      if (!chart.isDatasetVisible(di)) continue;
      const rawM = datasets[di].raw_m || [];
      for (let i = 0; i < numLabels; i++) {
        visibleTotals[i] += rawM[i] || 0;
      }
    }

    for (let di = 0; di < datasets.length; di++) {
      const rawM = datasets[di].raw_m || [];
      datasets[di].data = rawM.map((m, i) =>
        visibleTotals[i] > 0
          ? Math.round((m / visibleTotals[i]) * 10000) / 100
          : 0
      );
    }

    chart.update("none");
  }

  // ── Options post-processing: attach JS callbacks ─────────────────────────

  function buildOptions(options) {
    // Deep-clone so we never mutate the prop value.
    const opts = JSON.parse(JSON.stringify(options));
    const isPercent = opts.value_mode === "percent";
    const periodTotals = opts.period_totals || [];
    const tooltipTitles = opts.tooltip_titles || [];

    // Y-axis: format tick values per the active mode.
    if (opts.scales && opts.scales.y) {
      opts.scales.y.ticks = opts.scales.y.ticks || {};
      opts.scales.y.ticks.callback = isPercent
        ? (val) => `${Math.round(val)}%`
        : (val) => fmtMeters(val);
    }

    // Override the legend's default onClick so that toggling a series in
    // percent mode re-normalises the remaining (visible) series back to 100 %
    // instead of leaving short bars.  In meters mode the default behaviour
    // (just hide the dataset) is fine.
    opts.plugins = opts.plugins || {};
    opts.plugins.legend = opts.plugins.legend || {};
    opts.plugins.legend.onClick = function (e, legendItem, legend) {
      const index = legendItem.datasetIndex;
      const ci = legend.chart;
      const meta = ci.getDatasetMeta(index);
      const currentlyVisible = ci.isDatasetVisible(index);
      meta.hidden = currentlyVisible ? true : null;
      legendItem.hidden = currentlyVisible;

      if (ci.options.value_mode === "percent") {
        recomputePercents(ci);
      } else {
        ci.update();
      }
    };

    // Custom tooltip: index mode (shows all datasets for a bar on hover).
    // In percent mode values display as % only; in meters mode as meters.
    opts.plugins.tooltip = {
      mode: "index",
      intersect: false,
      callbacks: {
        title(items) {
          if (!items.length) return "";
          const idx = items[0].dataIndex;

          if (idx != null && tooltipTitles[idx] != null) {
            return tooltipTitles[idx];
          }
          return items[0].label;
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

    // Visibility state on chart meta survives prop updates.  If the user
    // had hidden a series in percent mode, Python's freshly-computed data
    // still assumes everything visible, so re-normalise once here.
    recomputePercents(chartInstance);
  }

  // ── Initialise and respond to Python prop updates ─────────────────────────

  let props = { config: ctx.initialProps.config || null };
  applyConfig(props.config);

  ctx.onPropUpdate((propName, propValue) => {
    props[propName] = propValue;
    applyConfig(props.config);
  });
});

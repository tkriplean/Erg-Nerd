// race_scatter_plugin.js — pace-vs-date scatter for the Race Page.
//
// Receives a Chart.js config dict from Python.  String-typed callback fields
// (ticks.callback, tooltip.callbacks.label) are evaluated as JS functions
// here — HyperDiv's built-in hd.chart strips function values during JSON
// transport, so this thin wrapper exists purely to restore them.

window.hyperdiv.registerPlugin("RaceScatterChart", (ctx) => {
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; }
    .wrap { position: relative; width: 100%; height: 100%; }
    canvas { display: block; width: 100% !important; }
  `;
  ctx.domElement.appendChild(style);

  const wrap = document.createElement("div");
  wrap.className = "wrap";
  const canvas = document.createElement("canvas");
  wrap.appendChild(canvas);
  ctx.domElement.appendChild(wrap);

  let chartInstance = null;

  // Walk the config tree and replace any string starting with "function" with
  // the eval'd function.  Mutates in place.  The set of fields we evaluate is
  // intentionally narrow — only known callback positions in Chart.js configs.
  function reviveCallbacks(cfg) {
    if (!cfg || typeof cfg !== "object") return;
    const opts = cfg.options || {};
    const scales = opts.scales || {};
    for (const axis of ["x", "y"]) {
      const ticks = scales[axis] && scales[axis].ticks;
      if (ticks && typeof ticks.callback === "string") {
        // eslint-disable-next-line no-eval
        ticks.callback = eval("(" + ticks.callback + ")");
      }
    }
    const tt = opts.plugins && opts.plugins.tooltip;
    const cbs = tt && tt.callbacks;
    if (cbs) {
      for (const k of ["label", "title", "afterLabel", "beforeLabel"]) {
        if (typeof cbs[k] === "string") {
          // eslint-disable-next-line no-eval
          cbs[k] = eval("(" + cbs[k] + ")");
        }
      }
    }
  }

  function buildChart(config) {
    if (!config) return;
    if (chartInstance) {
      chartInstance.destroy();
      chartInstance = null;
    }
    // Deep-clone so successive prop updates don't compound mutations.
    const cfg = JSON.parse(JSON.stringify(config));
    reviveCallbacks(cfg);
    chartInstance = new Chart(canvas.getContext("2d"), cfg);
  }

  const _h = ctx.initialProps.height;
  if (_h) canvas.style.height = typeof _h === "number" ? _h + "px" : _h;

  buildChart(ctx.initialProps.config);

  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "height") {
      canvas.style.height = typeof propValue === "number" ? propValue + "px" : propValue;
    }
    if (propName === "config") {
      buildChart(propValue);
    }
  });
});

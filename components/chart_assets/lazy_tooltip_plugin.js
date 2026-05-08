/**
 * LazyTooltip — HyperDiv plugin that defers tooltip body construction until
 * the user first hovers (or tabs to) the trigger.  See the docstring of
 * components/lazy_tooltip_plugin.py for config shapes.
 *
 * Three trigger kinds — all rendered from the `config` prop:
 *   "spread"  — bold score on top of a small zone bar (image)
 *   "quality" — rounded coloured pill with a label
 *   "info"    — Shoelace question-circle icon
 *
 * The first mouseenter on the trigger lazily creates an <sl-tooltip>, moves
 * the trigger inside it, builds the rich body, and forces it open for that
 * hover.  Subsequent shows are handled natively by Shoelace.
 */

window.hyperdiv.registerPlugin("LazyTooltip", (ctx) => {
  const root = ctx.domElement; // shadowRoot

  // Style is intentionally tiny — we lean on the document's Shoelace CSS
  // custom properties (which inherit through shadow DOM) for fonts/colours.
  const style = document.createElement("style");
  style.textContent = `
    :host { display: inline-block; }
    .trigger {
      display: inline-flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 0.2rem;
      cursor: default;
    }
    .score { font-weight: bold; font-size: var(--sl-font-size-medium, 1rem); line-height: 1.2; }
    .bar { display: block; }
    .pill {
      display: inline-flex;
      padding: 0.15rem 0.5rem;
      border-radius: var(--sl-border-radius-medium, 0.25rem);
      align-items: center;
      justify-content: center;
    }
    .pill-label {
      font-size: var(--sl-font-size-x-small, 0.75rem);
      font-weight: bold;
      color: #fff;
      line-height: 1.2;
    }
    .info-wrap { display: inline-flex; padding: 0 0.3rem 0.3rem 0; }
    sl-icon { font-size: 0.95rem; }
    .tt-body {
      padding: 0.4rem;
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
      min-width: 12rem;
    }
    .tt-body.quality, .tt-body.info { min-width: 0; max-width: 24rem; gap: 0.3rem; }
    .row { display: flex; align-items: center; gap: 0.4rem; }
    .row img { width: 0.6rem; height: 0.6rem; display: block; }
    .row .zname {
      font-size: var(--sl-font-size-x-small, 0.75rem);
      flex: 1;
    }
    .row .pct {
      font-size: var(--sl-font-size-x-small, 0.75rem);
      font-weight: bold;
    }
    .row .meters {
      font-size: var(--sl-font-size-x-small, 0.75rem);
      color: var(--sl-color-neutral-500);
    }
    .tt-title { font-size: var(--sl-font-size-small, 0.875rem); font-weight: bold; }
    .tt-headline { font-size: var(--sl-font-size-x-small, 0.75rem); }
    .tt-label {
      font-size: var(--sl-font-size-x-small, 0.75rem);
      color: var(--sl-color-neutral-500);
    }
    .tt-item { font-size: var(--sl-font-size-x-small, 0.75rem); }
  `;
  root.appendChild(style);

  let trigger = null;
  let slTooltip = null;
  let armed = false;

  // Stacked-bar builder — same shape as workout_table_plugin's
  // _buildSpreadBar.  Skips bin 0 (Rest), drops segments smaller than 2 % of
  // work total, falls back to a grey rect when no work is logged.  Inline
  // SVG so we don't have to ship a base64 data URI through Python on every
  // render.
  function buildSpreadBar(binMeters, colors, widthRem, heightRem) {
    if (!binMeters) return null;
    const SVG_NS = "http://www.w3.org/2000/svg";
    const W = 160;
    const H = 8;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("xmlns", SVG_NS);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    // preserveAspectRatio="none" matches what the original `<img src=data:…>`
    // bar did via the <img> box: stretch the rects to fill the CSS dimensions
    // exactly.  Without it the default `xMidYMid meet` would letterbox the
    // 20:1 viewBox inside a 10:1 (5rem × 0.5rem) CSS box and clip visible
    // bar height to a hairline.
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "bar");
    svg.style.width = widthRem + "rem";
    svg.style.height = heightRem + "rem";
    svg.style.display = "block";

    let total = 0;
    for (let i = 1; i < binMeters.length; i++) {
      const m = binMeters[i];
      if (m > 0) total += m;
    }

    let x = 0;
    let drew = false;
    if (total > 0) {
      for (let i = 1; i < binMeters.length; i++) {
        const m = binMeters[i];
        if (m <= 0) continue;
        const f = m / total;
        if (f < 0.02) continue;
        const w = Math.round(f * W);
        if (w <= 0) continue;
        const rect = document.createElementNS(SVG_NS, "rect");
        rect.setAttribute("x", x);
        rect.setAttribute("y", 0);
        rect.setAttribute("width", w);
        rect.setAttribute("height", H);
        rect.setAttribute("fill", (colors && colors[i]) || "#d1d5db");
        svg.appendChild(rect);
        x += w;
        drew = true;
      }
    }
    if (!drew) {
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", 0);
      rect.setAttribute("y", 0);
      rect.setAttribute("width", W);
      rect.setAttribute("height", H);
      rect.setAttribute("fill", "#d1d5db");
      svg.appendChild(rect);
    }
    return svg;
  }

  function buildTrigger(cfg) {
    const el = document.createElement("div");
    el.className = "trigger";

    if (cfg.kind === "spread") {
      const score = document.createElement("div");
      score.className = "score";
      score.textContent = cfg.score;
      el.appendChild(score);
      const bar = buildSpreadBar(
        cfg.bin_meters, cfg.bar_colors, cfg.bar_w || 5, cfg.bar_h || 0.5);
      if (bar) el.appendChild(bar);
    } else if (cfg.kind === "quality") {
      const pill = document.createElement("div");
      pill.className = "pill";
      pill.style.background = cfg.bg;
      const lbl = document.createElement("span");
      lbl.className = "pill-label";
      lbl.textContent = cfg.label;
      pill.appendChild(lbl);
      el.appendChild(pill);
    } else if (cfg.kind === "info") {
      const wrap = document.createElement("span");
      wrap.className = "info-wrap";
      const icon = document.createElement("sl-icon");
      icon.setAttribute("name", "question-circle");
      if (cfg.color) icon.style.color = cfg.color;
      wrap.appendChild(icon);
      el.appendChild(wrap);
    }
    return el;
  }

  function buildBody(cfg) {
    const body = document.createElement("div");
    body.className = "tt-body";

    if (cfg.kind === "spread") {
      for (const it of cfg.items || []) {
        const row = document.createElement("div");
        row.className = "row";
        const img = document.createElement("img");
        img.src = it.swatch_uri;
        row.appendChild(img);
        if (it.name) {
          const nm = document.createElement("span");
          nm.className = "zname";
          nm.textContent = it.name;
          row.appendChild(nm);
        }
        const pct = document.createElement("span");
        pct.className = "pct";
        pct.textContent = it.pct_text;
        row.appendChild(pct);
        if (it.meters_text) {
          const m = document.createElement("span");
          m.className = "meters";
          m.textContent = it.meters_text;
          row.appendChild(m);
        }
        body.appendChild(row);
      }
    } else if (cfg.kind === "quality") {
      body.classList.add("quality");
      if (cfg.tt_title) {
        const t = document.createElement("div");
        t.className = "tt-title";
        t.textContent = cfg.tt_title;
        body.appendChild(t);
      }
      if (cfg.headline) {
        const h = document.createElement("div");
        h.className = "tt-headline";
        h.textContent = cfg.headline;
        body.appendChild(h);
      }
      if (cfg.top && cfg.top.length) {
        const lbl = document.createElement("div");
        lbl.className = "tt-label";
        lbl.textContent = "Top contributions:";
        body.appendChild(lbl);
        for (const c of cfg.top) {
          const row = document.createElement("div");
          row.className = "tt-item";
          row.textContent = `  • ${c.name}: ${c.pct_text}`;
          body.appendChild(row);
        }
      }
    } else if (cfg.kind === "info") {
      body.classList.add("info");
      body.style.maxWidth = (cfg.max_width || 24) + "rem";
      if (cfg.title || cfg.subtitle) {
        const head = document.createElement("div");
        head.style.display = "flex";
        head.style.flexWrap = "wrap";
        head.style.alignItems = "center";
        head.style.gap = "0.5rem";
        if (cfg.title) {
          const t = document.createElement("span");
          t.style.fontWeight = "bold";
          t.style.fontSize = "var(--sl-font-size-medium, 1rem)";
          t.textContent = cfg.title;
          head.appendChild(t);
        }
        if (cfg.subtitle) {
          const s = document.createElement("span");
          s.style.fontSize = "var(--sl-font-size-small, 0.875rem)";
          s.style.color = "var(--sl-color-neutral-300)";
          s.textContent = cfg.subtitle;
          head.appendChild(s);
        }
        body.appendChild(head);
      }
      if (cfg.body) {
        const b = document.createElement("div");
        b.style.fontSize = "var(--sl-font-size-small, 0.875rem)";
        if (cfg.body_muted) b.style.color = "var(--sl-color-neutral-300)";
        b.textContent = cfg.body;
        body.appendChild(b);
      }
      if (cfg.example) {
        const e = document.createElement("div");
        e.style.fontSize = "var(--sl-font-size-small, 0.875rem)";
        e.style.color = "var(--sl-color-neutral-300)";
        e.style.fontStyle = "italic";
        e.textContent = cfg.example;
        body.appendChild(e);
      }
    }
    return body;
  }

  function teardown() {
    if (slTooltip) {
      slTooltip.remove();
      slTooltip = null;
    }
    if (trigger) {
      trigger.remove();
      trigger = null;
    }
    armed = false;
  }

  function render(cfg, placement) {
    teardown();
    if (!cfg) return;
    trigger = buildTrigger(cfg);
    root.appendChild(trigger);

    const arm = () => {
      if (armed || !trigger) return;
      armed = true;
      slTooltip = document.createElement("sl-tooltip");
      slTooltip.setAttribute("placement", placement || "top");
      slTooltip.setAttribute("hoist", "");
      // Reparent the trigger inside the tooltip so it acts as the anchor.
      slTooltip.appendChild(trigger);
      const body = buildBody(cfg);
      body.setAttribute("slot", "content");
      slTooltip.appendChild(body);
      root.appendChild(slTooltip);
      // The mouseenter that armed us has already fired; show explicitly so
      // there's no perceptible delay before the tooltip appears.
      requestAnimationFrame(() => {
        // Enforce a single visible tooltip at a time: register this one and
        // dismiss any others before showing.  The registry self-cleans via
        // sl-after-hide, so it stays accurate however a tooltip gets closed.
        const reg = (window._wtTooltipRegistry = window._wtTooltipRegistry || {
          active: new Set(),
          register(t) {
            if (this.active.has(t)) return;
            this.active.add(t);
            t.addEventListener("sl-after-hide", () => this.active.delete(t));
          },
          hideOthers(keep) {
            for (const t of this.active) {
              if (t !== keep) { try { t.hide(); } catch (e) {} }
            }
          },
        });
        reg.register(slTooltip);
        reg.hideOthers(slTooltip);
        try {
          slTooltip.show();
        } catch (e) {
          /* ignore */
        }
      });
    };

    trigger.addEventListener("mouseenter", arm, { once: true });
    trigger.addEventListener("focusin", arm, { once: true });
  }

  let cfg = ctx.initialProps.config || null;
  let placement = ctx.initialProps.placement || "top";
  render(cfg, placement);

  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "config") cfg = propValue;
    else if (propName === "placement") placement = propValue;
    render(cfg, placement);
  });
});

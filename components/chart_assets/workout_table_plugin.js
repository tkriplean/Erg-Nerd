/**
 * WorkoutTable — HyperDiv plugin that renders a sortable, paginated CSS-grid
 * table entirely in JS.  Replaces the Python WorkoutTable component.
 *
 * Props (Python → JS):
 *   rows            list[dict]    Full dataset (workout / rank rows). Stable.
 *   column_configs  list[dict]    Compiled column specs (one per visible col).
 *   visible_ids     list | null   When set, only rows whose id is in the list
 *                                 are displayed. null = show all.
 *   highlight_ids   list          Row ids to highlight (primary-50 background).
 *   default_sort_col str
 *   default_sort_asc bool
 *   paginate        bool
 *   rows_per_page   int
 *   reset_token     str           When this changes, sort + page reset.
 *
 * Props (JS → Python):
 *   event_out       {name, payload, seq} | null
 *
 * Column config shape:
 *   {
 *     key:          "watts",
 *     header:       "Watts",
 *     width:        "5rem",            // CSS grid track size
 *     align:        "center" | "start" | "end",
 *     sortable:     true,
 *     default_asc:  false,
 *     renderer:     "text",            // dispatch key into RENDERERS
 *     format:       "watts",           // for renderer="text", dispatch key into FORMATS
 *     sort_key:     "watts",           // dispatch key into SORT_KEYS (default = key)
 *     opts:         {…}                // per-instance opts (e.g. compared_ids)
 *   }
 */

window.hyperdiv.registerPlugin("WorkoutTable", (ctx) => {
  const root = ctx.domElement;

  // ── Style ────────────────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = `
    :host { display: block; width: 100%; }
    .grid {
      display: grid;
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium, 0.25rem);
      overflow-x: auto;

    }
    .cell {
      padding: 0.4rem 0.75rem;
      font-size: var(--sl-font-size-small);
      display: flex;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
    }
    .cell.align-start  { justify-content: flex-start; text-align: left; }
    .cell.align-end    { justify-content: flex-end; text-align: right; }
    .cell.align-center { justify-content: center; text-align: center; }
    .cell.end { padding-right: 24px; }
    .hdr {
      background: var(--sl-color-neutral-50);
      border-bottom: 1px solid var(--sl-color-neutral-200);
      color: var(--sl-color-neutral-500);
      padding-top: 0.4rem;
      padding-bottom: 0.4rem;
    }
    .row-cell {
      padding-top: 0.5rem;
      padding-bottom: 0.5rem;
      border-bottom: 1px solid var(--sl-color-neutral-100);
      color: var(--sl-color-neutral-700);
    }
    .row-cell.alt { background: var(--sl-color-neutral-50); }
    .row-cell.hl  { background: var(--sl-color-primary-50); color: var(--sl-color-primary-700); font-weight: 600; }
    .sort-btn {
      background: none; border: none; cursor: pointer;
      font: inherit; color: inherit;
      padding: 0; margin: 0;
      font-size: var(--sl-font-size-small);
    }
    .sort-btn:hover { color: var(--sl-color-neutral-700); }
    .sort-btn.active { font-weight: bold; color: var(--sl-color-neutral-600); }
    .empty {
      padding: 1rem;
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-small);
    }
    .pagination {
      display: flex; align-items: center; justify-content: center;
      gap: 1rem; padding: 0.75rem 0;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-500);
    }
    .pagination sl-button { font-size: inherit; }

    /* Cell-internal styles */
    a.link { font-size: var(--sl-font-size-small); text-decoration: none; color: var(--sl-color-primary-600); }
    a.link:hover { text-decoration: underline; }

    .spread { display: flex; flex-direction: column; align-items: center; gap: 0.2rem; cursor: default; }
    .spread .score { font-weight: bold; font-size: var(--sl-font-size-medium); line-height: 1.1; }
    .spread img.bar { display: block; }
    .em-dash { color: var(--sl-color-neutral-400); font-size: var(--sl-font-size-medium); }

    .quality-pill {
      display: inline-flex;
      padding: 0.15rem 0.5rem;
      border-radius: var(--sl-border-radius-medium, 0.25rem);
      align-items: center; justify-content: center;
      cursor: default;
    }
    .quality-pill .label {
      font-size: var(--sl-font-size-x-small);
      font-weight: bold;
      color: #fff;
      line-height: 1.2;
    }

    .stimulus { font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-500); font-style: italic; }

    .struct-btn {
      background: none; border: none; cursor: pointer;
      font-size: var(--sl-font-size-medium);
      color: var(--sl-color-neutral-700);
      padding: 0;
    }
    .struct-btn.active { color: var(--sl-color-primary-500); font-weight: 600; }

    .rank-btn { background: none; border: none; cursor: pointer; padding: 0 0.3rem; font: inherit; color: var(--sl-color-primary-600); }
    .rank-btn:hover { background: var(--sl-color-neutral-100); border-radius: 4px; }
    .rank-btn .row { display: inline-flex; gap: 0.3rem; align-items: center; justify-content: center; }
    .rank-num { font-family: var(--sl-font-mono, monospace); text-align: right; font-size: var(--sl-font-size-small); }
    .rank-of  { font-size: var(--sl-font-size-x-small); text-align: center; }

    .pct-whole { font-size: var(--sl-font-size-large); font-weight: 600; }
    .pct-tenth { font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-500); padding-top: 0.1rem; }

    .dist-img { width: 100%; height: 32px; }

    /* Tooltip body content (used inside <sl-tooltip slot="content">) */
    .tt-body { padding: 0.4rem; display: flex; flex-direction: column; gap: 0.2rem; min-width: 12rem; }
    .tt-body.quality { min-width: 0; max-width: 24rem; gap: 0.3rem; }
    .tt-body .row { display: flex; align-items: center; gap: 0.4rem; }
    .tt-body .row img { width: 0.6rem; height: 0.6rem; display: block; }
    .tt-body .row .zname { font-size: var(--sl-font-size-x-small); flex: 1; }
    .tt-body .row .pct { font-size: var(--sl-font-size-x-small); font-weight: bold; min-width: 2.4rem; text-align: right; }
    .tt-body .row .meters { font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-500); }
    .tt-title { font-size: var(--sl-font-size-small); font-weight: bold; }
    .tt-headline { font-size: var(--sl-font-size-x-small); }
    .tt-label { font-size: var(--sl-font-size-x-small); color: var(--sl-color-neutral-500); }
    .tt-item { font-size: var(--sl-font-size-x-small); }
  `;
  root.appendChild(style);

  // ── State ────────────────────────────────────────────────────────────────
  const state = {
    rows: ctx.initialProps.rows || [],
    cols: ctx.initialProps.column_configs || [],
    visibleIds: ctx.initialProps.visible_ids,
    highlightIds: new Set(ctx.initialProps.highlight_ids || []),
    sortCol: ctx.initialProps.default_sort_col || "date",
    sortAsc: !!ctx.initialProps.default_sort_asc,
    page: 0,
    perPage: ctx.initialProps.rows_per_page || 25,
    paginate: ctx.initialProps.paginate !== false,
    resetToken: ctx.initialProps.reset_token || "",
    defaultSortCol: ctx.initialProps.default_sort_col || "date",
    defaultSortAsc: !!ctx.initialProps.default_sort_asc,
  };

  let eventSeq = 0;
  function emit(name, payload) {
    eventSeq += 1;
    ctx.updateProp("event_out", { name, payload, seq: eventSeq });
  }

  // ── Format helpers (port of services/formatters.py) ──────────────────────
  const MACHINE_LABELS = {
    rower: "Rower", skierg: "SkiErg", bike: "BikeErg", dynamic: "Dynamic",
    slides: "Slides", paddle: "Paddle", water: "Water", snow: "Snow",
    rollerski: "Roller Ski", multierg: "MultiErg",
  };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function fmtDate(s) {
    if (!s) return "—";
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (!m) return s.slice(0, 10);
    return `${MONTHS[+m[2] - 1]} ${+m[3]}, ${m[1]}`;
  }
  function formatTime(tenths) {
    const t = Math.trunc(tenths);
    const frac = ((t % 10) + 10) % 10;
    const totalS = Math.trunc(t / 10);
    const secs = totalS % 60;
    const totalM = Math.trunc(totalS / 60);
    const mins = totalM % 60;
    const hours = Math.trunc(totalM / 60);
    if (hours) return `${hours}:${pad2(mins)}:${pad2(secs)}.${frac}`;
    return `${mins}:${pad2(secs)}.${frac}`;
  }
  function fmtSplit(tenths) {
    if (!tenths) return "—";
    const total = tenths / 10;
    const m = Math.floor(total / 60);
    const s = total - m * 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
  }
  function paceTenths(r) {
    const t = r.time, d = r.distance;
    if (!t || !d) return null;
    return (t * 500) / d;
  }
  function fmtDistance(m) {
    if (!m) return "—";
    return m.toLocaleString("en-US") + "m";
  }
  function fmtHr(hr) {
    if (!hr || typeof hr !== "object") return "—";
    return hr.average ? `${hr.average} bpm` : "—";
  }
  function machineLabel(t) {
    if (!t) return "—";
    return MACHINE_LABELS[t.toLowerCase()] || (t[0].toUpperCase() + t.slice(1));
  }
  function fmtWatts(r) {
    if (r.watts == null) return "—";
    return String(Math.round(r.watts));
  }
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }

  // ── DOM helpers ───────────────────────────────────────────────────────────
  function el(tag, props, children) {
    const e = document.createElement(tag);
    if (props) for (const k in props) {
      if (k === "class") e.className = props[k];
      else if (k === "style") Object.assign(e.style, props[k]);
      else if (k.startsWith("on") && typeof props[k] === "function") {
        e.addEventListener(k.slice(2).toLowerCase(), props[k]);
      } else if (k in e) e[k] = props[k];
      else e.setAttribute(k, props[k]);
    }
    if (children != null) {
      const arr = Array.isArray(children) ? children : [children];
      for (const c of arr) {
        if (c == null) continue;
        if (typeof c === "string") e.appendChild(document.createTextNode(c));
        else e.appendChild(c);
      }
    }
    return e;
  }
  function text(s) { return document.createTextNode(s ?? ""); }
  function emDash() { return el("span", { class: "em-dash" }, "—"); }

  // ── Format dispatch (text-only renderers) ────────────────────────────────
  const FORMATS = {
    date:              (r) => fmtDate(r.date),
    type:              (r) => machineLabel(r.type || ""),
    distance:          (r) => fmtDistance(r.distance),
    time:              (r) => r.time_formatted || (r.time ? formatTime(r.time) : "—"),
    pace:              (r) => fmtSplit(paceTenths(r)),
    watts:             (r) => fmtWatts(r),
    drag:              (r) => r.drag_factor ? String(r.drag_factor) : "—",
    spm:               (r) => r.stroke_rate ? String(r.stroke_rate) : "—",
    hr:                (r) => fmtHr(r.heart_rate),
    season:            (r) => r.season || "",
    structure:         (r) => r.is_interval ? (r.intervals_label || "") : "",
    reps:              (r) => r.reps ? String(r.reps) : "—",
    work_pace:         (r) => r.work_pace ? fmtSplit(r.work_pace) : "—",
    work_spm:          (r) => r.work_spm ? Math.round(r.work_spm).toString() : "—",
    workout_structure: (r) => r.is_interval ? (r.intervals_label || "") : "",
    similarity:        (r) => r._similarity != null ? r._similarity.toFixed(0) : "—",
    rank_event:        (r) => r.event_label || "",
    rank_date:         (r) => r.date_label || "",
    rank_age:          (r) => String(r.age),
    rank_age_group:    (r) => r.age_band_rankings || "",
    rank_result:       (r) => r.event_kind === "dist"
                                ? formatTime(r.value_tenths || 0)
                                : fmtDistance(r.value_tenths || 0),
    rank_pace:         (r) => r.pace_tenths ? fmtSplit(r.pace_tenths) : "—",
    rank_watts:        (r) => r.watts ? Math.round(r.watts).toString() : "—",
    rank_wr_pct_pace:  (r) => "wr_pct_pace"  in r ? `${r.wr_pct_pace.toFixed(1)}%`  : "—",
    rank_wr_pct_watts: (r) => "wr_pct_watts" in r ? `${r.wr_pct_watts.toFixed(1)}%` : "—",
    rank_wr_pace:      (r) => r.wr_pace ? fmtSplit(Math.round(r.wr_pace * 10)) : "—",
    // ── ESS family ─────────────────────────────────────────────────────────
    ess:               (r) => r._ess != null ? r._ess.toFixed(1) : "—",
    if_eff:            (r) => r._if_eff != null ? r._if_eff.toFixed(2) : "—",
    anaerobic_strain:  (r) => r._anaerobic_strain != null
                                ? `${Math.round(r._anaerobic_strain * 100)}%`
                                : "—",
  };

  // ── Sort-key dispatch ────────────────────────────────────────────────────
  const POS_INF = Number.POSITIVE_INFINITY;
  const SORT_KEYS = {
    date:              (r) => r.date || "",
    type:              (r) => machineLabel(r.type || ""),
    distance:          (r) => r.distance || 0,
    time:              (r) => r.time || 0,
    pace:              (r) => paceTenths(r) || POS_INF,
    watts:             (r) => r.watts ?? 0,
    drag:              (r) => r.drag_factor || 0,
    spm:               (r) => r.stroke_rate || 0,
    //hr:                (r) => (r.heart_rate && r.heart_rate.average) || 0,
    season:            (r) => r.date || "",
    structure:         (r) => r.is_interval ? (r.structure_key || "") : "",
    reps:              (r) => r.reps || 0,
    work_pace:         (r) => r.work_pace || POS_INF,
    work_spm:          (r) => r.work_spm || 0,
    workout_structure: (r) => r.is_interval ? (r.structure_key || "") : "",
    similarity:        (r) => r._similarity != null ? r._similarity : -1,
    power_spread:      (r) => r._power_spread_score != null ? r._power_spread_score : -1,
    hr:                (r) => r._hr_spread_score != null ? ((r.heart_rate && r.heart_rate.average) || 0) + r._hr_spread_score : -1,
    quality:           (r) => r._quality_score != null ? r._quality_score : -1,
    ess:               (r) => r._ess != null ? r._ess : -1,
    if_eff:            (r) => r._if_eff != null ? r._if_eff : -1,
    severity:          (r) => r._severity_score != null ? r._severity_score : -1,
    anaerobic_strain:  (r) => r._anaerobic_strain != null ? r._anaerobic_strain : -1,
    rank_event:        (r, opts) => (opts && opts.event_order && opts.event_order[r.event_key]) ?? 99,
    rank_date:         (r) => r.date_iso || "",
    rank_age:          (r) => r.age || 0,
    rank_age_group:    (r) => r.age_band_rankings || "",
    rank_result:       (r) => r.value_tenths || 0,
    rank_pace:         (r) => r.pace_tenths || POS_INF,
    rank_watts:        (r) => r.watts || 0,
    rank_wr_pct_pace:  (r) => r.wr_pct_pace  || 0,
    rank_wr_pct_watts: (r) => r.wr_pct_watts || 0,
    rank_wr_pace:      (r) => r.wr_pace || POS_INF,
    rank:              (r) => r.rank || 1e9,
    rank_percentile:   (r) => r.percentile || 0,
  };

  // ── Tooltip: lazy-arming Shoelace tooltip on first hover ─────────────────
  function lazyTooltipWrap(triggerNode, buildBody, placement) {
    let armed = false;
    const arm = () => {
      if (armed) return;
      armed = true;
      const tt = document.createElement("sl-tooltip");
      tt.setAttribute("placement", placement || "top");
      tt.setAttribute("hoist", "");
      // Reparent the trigger into the tooltip (preserves its position in flow).
      const slot = triggerNode.parentNode;
      const idx = Array.from(slot.children).indexOf(triggerNode);
      slot.insertBefore(tt, triggerNode);
      tt.appendChild(triggerNode);
      const body = buildBody();
      body.setAttribute("slot", "content");
      tt.appendChild(body);
      requestAnimationFrame(() => { try { tt.show(); } catch (e) {} });
    };
    triggerNode.addEventListener("mouseenter", arm, { once: true });
    triggerNode.addEventListener("focusin", arm, { once: true });
  }

  // ── Cell renderers ────────────────────────────────────────────────────────
  const RENDERERS = {
    text(r, col) {
      const fn = FORMATS[col.format];
      const v = fn ? fn(r) : "";
      return text(v);
    },

    link(r, col) {
      // Suppress the "view" link for the row that matches the column's
      // current_id option — used by the "all workouts done on this day"
      // table on the workout page so the page doesn't link back to itself.
      const cur = col && col.opts && col.opts.current_id;
      if (cur != null && String(cur) === String(r.id)) {
        return document.createDocumentFragment();
      }
      return el("a", { class: "link", href: `/session/${r.id}` }, "view");
    },

    structure_filter(r, col) {
      const sk = r.structure_key || "";
      if (!sk) return text("");
      const label = r.intervals_label || sk;
      const active = (col.opts && col.opts.active_key) === sk;
      const btn = el("button", {
        class: "struct-btn" + (active ? " active" : ""),
        onClick: () => emit("structure_click", { structure_key: sk }),
      }, label);
      return btn;
    },

    stimulus(r) {
      const s = r._stimulus || "";
      if (!s || s === "—") return document.createDocumentFragment();
      return el("span", { class: "stimulus" }, s);
    },

    compare(r, col) {
      if (!r.stroke_data) return emDash();
      if (r.id == null) return emDash();
      const opts = col.opts || {};
      const compared = new Set(opts.compared_ids || []);
      const checked = compared.has(r.id);
      const stack = !!opts.stack_active;
      const cb = el("sl-checkbox", { size: "small" });
      if (checked) cb.setAttribute("checked", "");
      if (stack) cb.setAttribute("disabled", "");
      cb.addEventListener("sl-change", () => {
        emit("compare_toggle", { workout_id: r.id, checked: !!cb.checked });
      });
      return cb;
    },

    power_spread(r, col) {
      const score = r._power_spread_score;
      const bins = r._bin_meters;
      if (score == null || bins == null) return emDash();
      return _spreadCell(score, r._bar_uri, bins, col);
    },

    hr_spread(r, col) {
      const hr = (r.heart_rate && r.heart_rate.average) || null
      const bins = r._hr_bin_meters;
      console.log("HI", hr, bins)

      if (hr == null || bins == null) return emDash();
      return _spreadCell(hr, r._hr_bar_uri, bins, col);
    },

    quality(r, col) {
      const q = r._quality;
      if (q == null) return emDash();
      const styles = (col.opts && col.opts.quality_styles) || {};
      const style = styles[q];
      if (!style) return text(q);
      const pill = el("div", { class: "quality-pill", style: { background: style.bg } },
        el("span", { class: "label" }, style.label || q));
      const score = r._quality_score || 0;
      const energy = r._quality_energy || {};
      lazyTooltipWrap(pill, () => _qualityTooltipBody(q, score, energy), "top");
      return pill;
    },

    severity(r, col) {
      const sev = r._severity;
      if (sev == null) return emDash();
      const styles = (col.opts && col.opts.severity_styles) || {};
      const style = styles[sev];
      if (!style) return text(sev);
      const pill = el("div", { class: "quality-pill", style: { background: style.bg } },
        el("span", { class: "label" }, style.label || sev));
      const score = r._severity_score || 0;
      const strain = r._anaerobic_strain || 0;
      lazyTooltipWrap(pill, () => _severityTooltipBody(sev, score, strain), "top");
      return pill;
    },

    rank(r) {
      if (!r.rank_total) return emDash();
      const rankChars = Math.max(1, r._rank_chars || 1);
      const totalChars = Math.max(1, r._total_chars || 1);
      const rankW = (7 * rankChars) + "px";
      const totalW = (7 * totalChars) + "px";
      const rankS = r.rank.toLocaleString("en-US");
      const totalS = r.rank_total.toLocaleString("en-US");
      const btn = el("button", {
        class: "rank-btn",
        onClick: () => emit("rank_click", { row_id: r.id }),
      },
        el("span", { class: "row" }, [
          el("span", { class: "rank-num", style: { width: rankW } }, rankS),
          el("span", { class: "rank-of" }, "of"),
          el("span", { class: "rank-num", style: { width: totalW } }, totalS),
        ])
      );
      return btn;
    },

    rank_percentile(r) {
      if (!r.rank_total) return emDash();
      const pct = r.percentile;
      let whole = Math.floor(pct);
      let tenth = Math.round((pct - whole) * 10);
      if (tenth >= 10) { whole += 1; tenth = 0; }
      return el("span", { style: { display: "inline-flex", alignItems: "flex-start" } }, [
        el("span", { class: "pct-whole" }, String(whole)),
        el("span", { class: "pct-tenth" }, "." + tenth),
      ]);
    },

    rank_distribution(r) {
      if (!r._dist_uri) return emDash();
      return el("div", { style: { width: "100%" } },
        el("img", { class: "dist-img", src: r._dist_uri }));
    },
  };

  // ── Spread cell (score + bar img + lazy tooltip with zone breakdown) ─────
  function _spreadCell(score, barUri, binMeters, col) {
    const opts = col.opts || {};
    const trigger = el("div", { class: "spread" });
    const scoreEl = el("div", { class: "score" }, score.toFixed(0));
    trigger.appendChild(scoreEl);
    if (barUri) {
      const img = el("img", { class: "bar", src: barUri });
      img.style.width = (opts.bar_w || 5) + "rem";
      img.style.height = (opts.bar_h || 0.5) + "rem";
      trigger.appendChild(img);
    }

    const skip = new Set(opts.skip_indices || [0]);
    const swatches = opts.swatch_uris || [];
    const zoneNames = opts.zone_names || [];
    const fmtMeters = (m) => {
      const v = Math.round(m);
      if (v >= 1000) {
        const k = v / 1000;
        return (Number.isInteger(k) ? k : k.toFixed(1)) + "k";
      }
      return v + "m";
    };
    const buildBody = () => {
      const total = binMeters.reduce(
        (acc, m, idx) => skip.has(idx) ? acc : acc + (m > 0 ? m : 0), 0);
      const items = [];
      for (let idx = 0; idx < swatches.length; idx++) {
        if (skip.has(idx)) continue;
        const meters = binMeters[idx] || 0;
        if (meters <= 0) continue;
        const pct = total > 0 ? meters / total : 0;
        if (pct < 0.005) continue;
        items.push({
          uri: swatches[idx],
          name: zoneNames[idx] || "",
          pctText: Math.round(pct * 100) + "%",
          metersText: fmtMeters(meters),
        });
      }
      const body = el("div", { class: "tt-body" });
      for (const it of items) {
        const row = el("div", { class: "row" }, [
          el("img", { src: it.uri }),
        ]);
        if (it.name) {
          row.appendChild(el("span", { class: "zname" }, it.name));
        }
        row.appendChild(el("span", { class: "pct" }, it.pctText));
        row.appendChild(el("span", { class: "meters" }, it.metersText));
        body.appendChild(row);
      }
      return body;
    };
    lazyTooltipWrap(trigger, buildBody, "top");
    return trigger;
  }

  function _severityTooltipBody(sev, score, strain) {
    const body = el("div", { class: "tt-body quality" });
    body.appendChild(el("div", { class: "tt-title" }, `${sev} severity`));
    let headline;
    if (sev === "Low") headline = `Severity ${score.toFixed(2)} — easy / recovery / base session.`;
    else if (sev === "Moderate") headline = `Severity ${score.toFixed(2)} — solid moderate session.`;
    else if (sev === "High") headline = `Severity ${score.toFixed(2)} — sharp threshold / VO2 / intervals.`;
    else headline = `Severity ${score.toFixed(2)} — race-pace or max effort; high recovery demand.`;
    body.appendChild(el("div", { class: "tt-headline" }, headline));
    body.appendChild(el("div", { class: "tt-label" },
      `W' depleted: ${Math.round(strain * 100)}%`));
    return body;
  }

  function _qualityTooltipBody(q, score, energy) {
    const body = el("div", { class: "tt-body quality" });
    body.appendChild(el("div", { class: "tt-title" }, `${q} quality`));
    let headline;
    if (q === "Low") headline = `Quality score ${score.toFixed(2)} — below the 0.50 threshold for a Medium session.`;
    else if (q === "Medium") headline = `Quality score ${score.toFixed(2)} — clears the 0.50 Medium threshold, below the 0.75 cutoff for High.`;
    else if (q === "High") headline = `Quality score ${score.toFixed(2)} — clears the 0.75 High threshold.`;
    else headline = `Quality score ${score.toFixed(2)} — beyond reference power.`;
    body.appendChild(el("div", { class: "tt-headline" }, headline));
    // Top-3 categories
    const cats = Object.entries(energy || {}).filter(([_, e]) => e > 0);
    cats.sort((a, b) => b[1] - a[1]);
    const top = cats.slice(0, 3);
    if (top.length) {
      const totalE = top.reduce((acc, [_, e]) => acc + e, 0) || 1;
      body.appendChild(el("div", { class: "tt-label" }, "Top contributions:"));
      for (const [name, e] of top) {
        body.appendChild(el("div", { class: "tt-item" },
          `  • ${name}: ${Math.round((100 * e) / totalE)}%`));
      }
    }
    return body;
  }

  // ── Sort + visible-rows pipeline ─────────────────────────────────────────
  function visibleAndSorted() {
    let rs = state.rows;
    if (state.visibleIds != null) {
      const allow = new Set(state.visibleIds);
      rs = rs.filter((r) => allow.has(r.id));
    }
    const col = state.cols.find((c) => c.key === state.sortCol);
    if (col) {
      const keyName = col.sort_key || col.key;
      const fn = SORT_KEYS[keyName];
      if (fn) {
        const opts = col.opts || {};
        const dir = state.sortAsc ? 1 : -1;
        rs = rs.slice().sort((a, b) => {
          const ka = fn(a, opts);
          const kb = fn(b, opts);
          if (ka < kb) return -1 * dir;
          if (ka > kb) return  1 * dir;
          return 0;
        });
      }
    }
    return rs;
  }

  // ── Render ────────────────────────────────────────────────────────────────
  let container = null;
  function render() {
    if (container) container.remove();
    container = document.createElement("div");
    root.appendChild(container);

    if (!state.rows.length) {
      container.appendChild(el("div", { class: "empty" }, "No results."));
      return;
    }

    const sorted = visibleAndSorted();
    const total = sorted.length;
    if (!total) {
      container.appendChild(el("div", { class: "empty" }, "No results."));
      return;
    }

    const perPage = state.paginate ? state.perPage : total;
    const totalPages = Math.max(1, Math.ceil(total / perPage));
    if (state.page >= totalPages) state.page = totalPages - 1;
    if (state.page < 0) state.page = 0;
    const pageRows = sorted.slice(state.page * perPage, state.page * perPage + perPage);

    // Grid
    const grid = el("div", { class: "grid" });
    grid.style.gridTemplateColumns = state.cols.map((c) => c.width).join(" ");

    // Header
    for (const col of state.cols) {
      const align = "align-" + (col.align || "center");
      const cell = el("div", { class: `cell hdr ${align}` });
      if (col.sortable && col.header) {
        const isActive = state.sortCol === col.key;
        const indicator = isActive ? (state.sortAsc ? " ▲" : " ▼") : "";
        const btn = el("button", {
          class: "sort-btn" + (isActive ? " active" : ""),
          onClick: () => onSortClick(col),
        }, col.header + indicator);
        cell.appendChild(btn);
      } else if (col.header) {
        cell.appendChild(text(col.header));
        cell.style.fontWeight = "600";
      }
      grid.appendChild(cell);
    }

    // Rows
    pageRows.forEach((row, i) => {
      const isAlt = i % 2 === 1;
      const isHl = state.highlightIds.has(row.id);
      idx = 0;
      col_count = state.cols.length
      for (const col of state.cols) {
        const align = "align-" + (col.align || "center");
        isEnd = idx == col_count - 1;
        const cls = `cell row-cell ${align}` + (isHl ? " hl" : (isAlt ? " alt" : "")) + (isEnd ? " end" : "");
        const cell = el("div", { class: cls });
        const renderer = RENDERERS[col.renderer] || RENDERERS.text;
        const node = renderer(row, col);
        if (node != null) cell.appendChild(node);
        grid.appendChild(cell);
        idx += 1;
      }
    });

    container.appendChild(grid);

    // Pagination
    if (state.paginate && totalPages > 1) {
      const bar = el("div", { class: "pagination" });
      if (state.page > 0) {
        const prev = el("sl-button", { size: "small", variant: "neutral" }, "← Prev");
        prev.addEventListener("click", () => { state.page -= 1; render(); });
        bar.appendChild(prev);
      }
      bar.appendChild(text(`Page ${state.page + 1} of ${totalPages}  (${total} workouts)`));
      if (state.page < totalPages - 1) {
        const next = el("sl-button", { size: "small", variant: "neutral" }, "Next →");
        next.addEventListener("click", () => { state.page += 1; render(); });
        bar.appendChild(next);
      }
      container.appendChild(bar);
    }
  }

  function onSortClick(col) {
    if (state.sortCol === col.key) {
      state.sortAsc = !state.sortAsc;
    } else {
      state.sortCol = col.key;
      state.sortAsc = !!col.default_asc;
    }
    state.page = 0;
    render();
  }

  // ── Initial render ────────────────────────────────────────────────────────
  render();

  // ── Prop updates ──────────────────────────────────────────────────────────
  ctx.onPropUpdate((name, value) => {
    switch (name) {
      case "rows":            state.rows = value || []; break;
      case "column_configs":  state.cols = value || []; break;
      case "visible_ids":     state.visibleIds = value; break;
      case "highlight_ids":   state.highlightIds = new Set(value || []); break;
      case "default_sort_col":
        state.defaultSortCol = value || "date";
        // Don't override user-chosen sort; only used on reset.
        break;
      case "default_sort_asc":
        state.defaultSortAsc = !!value;
        break;
      case "paginate":        state.paginate = value !== false; break;
      case "rows_per_page":   state.perPage = value || 25; break;
      case "reset_token":
        if (value !== state.resetToken) {
          state.resetToken = value;
          state.page = 0;
          state.sortCol = state.defaultSortCol;
          state.sortAsc = state.defaultSortAsc;
        }
        break;
      default: return;
    }
    render();
  });
});

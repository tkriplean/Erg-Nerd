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
 *   searchable      bool          Render a search bar above the grid.
 *                                 Filter is text + fuzzy number/duration/
 *                                 distance + "NxM" interval pattern; in
 *                                 tree mode it's session-aware.
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

    .grid {
      display: grid;
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium, 0.25rem);
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
      background: var(--sl-color-neutral-100);
      border-bottom: 1px solid var(--sl-color-neutral-200);
      color: var(--sl-color-neutral-900);
      padding-top: 0.4rem;
      padding-bottom: 0.4rem;
      line-height: 1.15;
      box-shadow: 0 1px 0 var(--sl-color-neutral-200);
    }
    .row-cell {
      padding-top: 0.5rem;
      padding-bottom: 0.5rem;
      border-bottom: 1px solid var(--sl-color-neutral-100);
      color: var(--sl-color-neutral-700);
    }
    .row-cell.alt { background: var(--sl-color-neutral-50); }
    .row-cell.hl  { background: var(--sl-color-primary-50); color: var(--sl-color-primary-700); font-weight: 600; }

    /* Tree mode: alternating session-block tint replaces zebra. */
    .row-cell.session-tint-a { background: var(--sl-color-neutral-50); }
    .row-cell.session-tint-b { background: transparent; }
    /* Suppress the per-row bottom border between a parent and its children
       and between consecutive children of the same session, so the block
       reads as one visual unit. */
    .row-cell.session-internal { border-bottom: 1px solid transparent; }

    /* Date cell: chevron, child indent. */
    .tree-date { display: flex; align-items: center; gap: 0; justify-content: flex-start; }
    .tree-date .tree-date-text { display: flex; flex-direction: column; }
    .tree-chevron {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1rem;
      margin-right: 0.5rem;
      cursor: pointer;
      user-select: none;
      color: var(--sl-color-neutral-500);
      transition: transform 120ms ease;
      font-size: 0.7rem;
      line-height: 1;
      flex-shrink: 0;
    }
    .tree-chevron.open { transform: rotate(90deg); }
    .tree-chevron-spacer {
      display: inline-block;
      width: 1rem;
      margin-right: 0.5rem;
      flex-shrink: 0;
    }
    .row-cell.is-child .tree-date-text { padding-left: 1.4rem; }
    .tree-date .date-main {
      font-size: var(--sl-font-size-medium);
      font-family: var(--sl-font-mono, ui-monospace, monospace);
    }

    /* Time-of-day column — colloquial period above session duration. */
    .tod-cell { display: flex; flex-direction: column; align-items: center; gap: 0.05rem; line-height: 1.1; }
    .tod-cell .tod-main { font-size: var(--sl-font-size-small); color: var(--sl-color-neutral-700); }
    .tod-cell .tod-sub  {
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-500);
      font-family: var(--sl-font-mono, ui-monospace, monospace);
    }
    .tod-cell.child .tod-main {
      font-family: var(--sl-font-mono, ui-monospace, monospace);
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-x-small);
    }

    /* Manually-added workouts — Concept2 stores them with date suffix
       " 00:00:00" (no real time-of-day).  Flag the relevant cell so the
       user can spot them and override the time on the WorkoutPage. */
    .manually-added,
    .manually-added .tod-main,
    .manually-added .date-main {
      color: var(--sl-color-warning-700, #b8651e);
    }
    .manually-added .manual-icon {
      display: inline-block;
      margin-right: 0.2rem;
      font-size: 0.7rem;
    }

    .cell .lines .distance-sub {
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-500);    
    }

    /* Role labels on child rows (warmup/main/recovery/cooldown).  Main
       gets a filled pill; the rest get subtle tinted badges so the
       visual hierarchy reads as "main = the work, others = context". */
    .role-label {
      display: inline-block;
      font-size: 0.62rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-weight: 600;
      padding: 0.05rem 0.4rem;
      border-radius: 999px;
      line-height: 1.4;
    }
    .role-label.role-main {
      background: var(--sl-color-primary-600);
      color: #fff;
      box-shadow: 0 1px 2px rgba(0,0,0,0.15);
      letter-spacing: 0.08em;
    }
    .role-label.role-warmup {
      background: var(--sl-color-warning-100, rgba(255, 200, 100, 0.25));
      color: var(--sl-color-warning-700, #b8651e);
    }
    .role-label.role-cooldown {
      background: rgba(120, 180, 220, 0.22);
      color: #2c6a92;
    }
    .role-label.role-recovery {
      background: var(--sl-color-success-100, rgba(120, 200, 130, 0.2));
      color: var(--sl-color-success-700, #2f7a3a);
    }

    /* Gap rows — the slim italic spacer between consecutive workouts in
       an expanded session, showing how much wall-clock time elapsed. */
    .row-cell.gap-cell {
      padding-top: 0.15rem;
      padding-bottom: 0.15rem;
      border-bottom: 1px solid transparent;
    }
    .gap-text {
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-500);
    }

    /* Multi-line cell content (e.g. Main Work). */
    .cell .lines { display: flex; flex-direction: column; flex-align: center; gap: 0.1rem; width: 100%; }
    .cell .lines > div { font-size: var(--sl-font-size-small); }
    /* Sort header — always reserve space for the arrow so toggling sort
       direction (or moving the active sort to a different column) never
       changes line count or column width.  Active state is signalled via
       color contrast only — switching to a bold weight would change the
       label's metrics and ripple a width change through the grid. */
    .sort-btn {
      display: inline-flex;
      align-items: baseline;
      gap: 0.2rem;
      flex-wrap: nowrap;
      background: none; border: none; cursor: pointer;
      font: inherit;
      color: var(--sl-color-neutral-900);
      padding: 0; margin: 0;
      font-size: var(--sl-font-size-small);
      font-weight: 500;
      text-align: inherit;
      line-height: inherit;
    }
    .sort-btn .sort-label { white-space: normal; }
    .sort-btn .sort-arrow {
      font-size: 0.6rem;
      flex-shrink: 0;
      padding-top: 0.3rem;
      align-self: center;
    }
    .sort-btn .sort-arrow.hidden { visibility: hidden; }
    .sort-btn:hover { color: var(--sl-color-neutral-900); }
    .sort-btn.active { color: var(--sl-color-neutral-900); }
    .sort-btn.active .sort-arrow { color: var(--sl-color-neutral-600); }
    .empty {
      padding: 1rem;
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-small);
    }
    .pagination {
      display: flex; align-items: center; justify-content: center;
      gap: 0.25rem; padding: 0.5rem 0;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-500);
    }
    .pagination sl-icon-button { font-size: 1.1rem; }
    .pagination sl-icon-button[disabled] { opacity: 0.3; }
    .pagination .pagination-count {
      margin-left: 0.5rem;
      color: var(--sl-color-neutral-400);
      font-size: var(--sl-font-size-x-small);
    }

    /* Top-of-table toolbar — search on the left, pagination on the right.
       The toolbar (and the search-bar inside it) is persistent across
       renders so the input keeps focus / selection while typing; only the
       pagination-slot's children are swapped on each render. */
    .table-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      padding: 0.4rem 0;
    }
    .table-toolbar .pagination-slot {
      display: flex;
      align-items: center;
      flex: 0 0 auto;
    }
    .table-toolbar .pagination-slot .pagination {
      padding: 0;
    }
    .search-bar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex: 1 1 auto;
      min-width: 0;
    }
    .search-input-wrap {
      position: relative;
      flex: 0 1 24rem;
      display: flex;
      align-items: center;
      min-width: 0;
    }
    .search-input-wrap .search-icon {
      position: absolute;
      left: 0.55rem;
      top: 50%;
      transform: translateY(-50%);
      color: var(--sl-color-neutral-600);
      font-size: 0.95rem;
      pointer-events: none;
      display: inline-flex;
      align-items: center;
    }
    .search-bar .search-input {
      width: 100%;
      padding: 0.35rem 0.65rem 0.35rem 2rem;
      font: inherit;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-900);
      background: var(--sl-color-neutral-0);
      border: 1px solid var(--sl-color-neutral-200);
      border-radius: var(--sl-border-radius-medium, 0.25rem);
      box-sizing: border-box;
      outline: none;
    }
    .search-bar .search-input:focus {
      border-color: var(--sl-color-primary-500);
      box-shadow: 0 0 0 2px var(--sl-color-primary-100, rgba(59,130,246,0.18));
    }
    .search-bar .search-input::placeholder {
      color: var(--sl-color-neutral-400);
    }
    .search-bar .search-count {
      font-size: var(--sl-font-size-x-small);
      color: var(--sl-color-neutral-500);
      white-space: nowrap;
    }

    /* Cell-internal styles */
    a.link { font-size: var(--sl-font-size-small); text-decoration: none; color: var(--sl-color-primary-600); }
    a.link:hover { text-decoration: underline; }

    .spread { display: flex; flex-direction: column; align-items: center; gap: 0.2rem; cursor: default; }
    .spread .score { font-weight: bold; font-size: var(--sl-font-size-medium); line-height: 1.1; }
    .spread img.bar { display: block; }
    .watts-cell { display: flex; flex-direction: column; align-items: center; gap: 0.15rem; cursor: default; }
    .watts-cell .watts-num { font-size: var(--sl-font-size-medium); line-height: 1.1; }
    .watts-cell img.bar { display: block; }
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
    perPage: ctx.initialProps.rows_per_page || 50,
    paginate: ctx.initialProps.paginate !== false,
    resetToken: ctx.initialProps.reset_token || "",
    defaultSortCol: ctx.initialProps.default_sort_col || "date",
    defaultSortAsc: !!ctx.initialProps.default_sort_asc,
    treeMode: !!ctx.initialProps.tree_mode,
    expanded: new Set(),
    linkPrefix: ctx.initialProps.link_prefix || "",
    // Search state.  searchable is locked at mount time; searchQuery is
    // updated by the input element directly (no Python round-trip) and
    // persisted alongside sort / page / expanded.
    searchable: !!ctx.initialProps.searchable,
    searchQuery: "",
  };

  // ── State persistence across remount ─────────────────────────────────────
  // When the user clicks "view" → /workout/<id> and then comes back via the
  // browser back button, the plugin instance is destroyed and recreated.
  // Mirror sortCol/sortAsc/page/expanded into sessionStorage keyed by the
  // plugin's stable component id so the table lands in the same state.
  const STATE_STORAGE_KEY = "wtbl:" + ctx.key;
  try {
    const _raw = sessionStorage.getItem(STATE_STORAGE_KEY);
    if (_raw) {
      const _saved = JSON.parse(_raw);
      if (_saved && typeof _saved === "object") {
        if (typeof _saved.sortCol === "string") state.sortCol = _saved.sortCol;
        if (typeof _saved.sortAsc === "boolean") state.sortAsc = _saved.sortAsc;
        if (typeof _saved.page === "number") state.page = _saved.page;
        if (Array.isArray(_saved.expanded)) state.expanded = new Set(_saved.expanded);
        if (typeof _saved.searchQuery === "string") state.searchQuery = _saved.searchQuery;
      }
    }
  } catch (e) { /* sessionStorage unavailable — fall back to defaults */ }

  function persistState() {
    try {
      sessionStorage.setItem(STATE_STORAGE_KEY, JSON.stringify({
        sortCol: state.sortCol,
        sortAsc: state.sortAsc,
        page: state.page,
        expanded: Array.from(state.expanded),
        searchQuery: state.searchQuery,
      }));
    } catch (e) { /* quota / disabled — ignore */ }
  }

  // ── SPA navigation ───────────────────────────────────────────────────────
  // Plain <a href> would trigger a full page reload, dropping the websocket
  // and every server-side hd.state.  Reuse the browser's pushState +
  // popstate machinery (HyperDiv's location-singleton listens on popstate)
  // so the table view links keep us inside the running app.  Cmd/ctrl/shift/
  // middle clicks fall through to the browser so "open in new tab" still
  // works.
  //
  // We set ``window.__ergNerdSyntheticPopstate`` while we dispatch the
  // popstate so the scroll-restore listener in app.py's _SCROLL_NAV_JS can
  // tell forward-clicks apart from real browser back/forward — only the
  // latter should restore the previous scroll position.
  function spaNavigate(href) {
    history.pushState(null, "", href);
    window.__ergNerdSyntheticPopstate = true;
    try {
      window.dispatchEvent(new PopStateEvent("popstate"));
    } finally {
      window.__ergNerdSyntheticPopstate = false;
    }
  }
  function isPlainClick(ev) {
    return !ev.metaKey && !ev.ctrlKey && !ev.shiftKey && !ev.altKey && ev.button === 0;
  }

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

  // Readable duration: "1hr30min37s", "30min", "47s".  Used for the
  // session date sub-line and the Work Duration column in tree mode.
  function fmtDurationReadable(secs, drop_seconds_on_long) {
    if (!secs || secs <= 0) return "—";
    const total = Math.round(secs);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const parts = [];
    if (h) parts.push(`${h}hr`);
    if (m) parts.push(`${m}min`);
    if (s && (!drop_seconds_on_long || (m < 20 && h < 1))) parts.push(`${s}s`);
    return parts.join(" ");
  }

  // Short slash date: "9/24/26".  Used for the session date main line
  // (parent rows in tree mode).
  function fmtShortDate(s) {
    if (!s) return "—";
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
    if (!m) return s.slice(0, 10);
    const yy = m[1].slice(2);
    return `${m[2]}/${m[3]}/${yy}`;
  }
  function capitalize(s) {
    return s ? s[0].toUpperCase() + s.slice(1) : "";
  }

  // True when this row represents a manually-added workout (or a
  // session whose only member is one) — Concept2 stores these with a
  // date suffix " 00:00:00" since there's no recorded time-of-day.
  // The owner can override the time on the WorkoutPage; until then we
  // flag the cell visually.
  function _isManuallyAdded(r) {
    if (!r) return false;
    if (r._row_kind === "session") {
      const sd = r._session_start_dt || r.date || "";
      return typeof sd === "string" && sd.endsWith(" 00:00:00");
    }
    const d = r.date || "";
    return typeof d === "string" && d.endsWith(" 00:00:00");
  }

  // Workout start time as "h:mm am/pm".  ``r.date`` is the workout end
  // (Concept2 convention); ``r.time`` is duration in tenths of a second.
  // Returns "" when either is missing or unparseable.
  function workoutStartHHMM(r) {
    const dateStr = r.date;
    if (!dateStr || dateStr.length < 16) return "";
    const tenths = r.time || 0;
    // Tolerate either "YYYY-MM-DD HH:MM:SS" or ISO "YYYY-MM-DDTHH:MM:SS".
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(dateStr);
    if (!m) return "";
    const end = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
    if (isNaN(end.getTime())) return "";
    const start = new Date(end.getTime() - Math.round(tenths * 100));
    let h = start.getHours();
    const mm = pad2(start.getMinutes());
    const ampm = h >= 12 ? "pm" : "am";
    h = h % 12 || 12;
    return `${h}:${mm}${ampm}`;
  }

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

  // "View" anchor: real <a href> so cmd/ctrl/middle-click open a new tab,
  // but a plain click is intercepted and routed through ``spaNavigate`` so
  // the running app keeps its websocket and server-side state.  Persists
  // the table state right before navigating so the back-button restoration
  // sees the freshest sort/page/expanded values.
  function _viewLink(id) {
    const href = state.linkPrefix + "/workout/" + id;
    return el(
      "a",
      {
        class: "link",
        href,
        onClick: (ev) => {
          if (!isPlainClick(ev)) return;
          ev.preventDefault();
          persistState();
          spaNavigate(href);
        },
      },
      "view",
    );
  }

  // Tree-mode role classification: a child is "non-main" when it's a
  // warmup, cooldown, or recovery piece — its meters belong in Other
  // Distance rather than Work Distance.  ``main`` and ``single`` are
  // the only roles whose distance counts as work.
  function _isNonMainRole(role) {
    return role === "warmup" || role === "cooldown" || role === "recovery";
  }

  // ── Format dispatch (text-only renderers) ────────────────────────────────
  const FORMATS = {
    date:              (r) => fmtDate(r.date),
    type:              (r) => machineLabel(r.type || ""),
    distance:          (r) => fmtDistance(r.distance),
    time:              (r) => r.time_formatted || (r.time ? formatTime(r.time) : "—"),
    pace:              (r) => fmtSplit(r._row_kind === "session" ? r._pace_tenths : paceTenths(r)),
    watts:             (r) => fmtWatts(r),
    drag:              (r) => {
                          const v = r._row_kind === "session" ? r._drag : r.drag_factor;
                          return v ? String(Math.round(v)) : "—";
                       },
    spm:               (r) => {
                          const v = r._row_kind === "session" ? r._spm : r.stroke_rate;
                          return v ? String(Math.round(v)) : "—";
                       },
    hr:                (r) => fmtHr(r.heart_rate),
    work_duration:     (r) => fmtDurationReadable(r._row_kind === "session"
                                ? r._work_duration_s
                                : (r.time || 0) / 10),
    work_distance:     (r) => {
                          if (_isNonMainRole(r._role)) return "—";
                          return fmtDistance(r.distance || 0);
                       },
    other_distance:    (r) => {
                          let m;
                          if (r._row_kind === "session") {
                            m = r._other_distance_m;
                          } else if (_isNonMainRole(r._role)) {
                            // Non-main child: every meter (work + rest)
                            // counts as Other.
                            m = (r.distance || 0) + (r.rest_distance || 0);
                          } else {
                            // Main / singleton child: work distance is
                            // displayed in Work Distance; only the rest
                            // distance lands here.
                            m = r.rest_distance || 0;
                          }
                          return m ? fmtDistance(m) : "—";
                       },
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
    glycogen_used:     (r) => r._glycogen_used != null
                                ? `${Math.round(r._glycogen_used * 100)}%${r._glycogen_used > 1 ? " ⚠" : ""}`
                                : "—",
  };

  // ── Sort-key dispatch ────────────────────────────────────────────────────
  const POS_INF = Number.POSITIVE_INFINITY;
  const SORT_KEYS = {
    date:              (r) => r.date || "",
    type:              (r) => machineLabel(r.type || ""),
    distance:          (r) => r.distance || 0,
    time:              (r) => r.time || 0,
    pace:              (r) => (r._row_kind === "session" ? r._pace_tenths : paceTenths(r)) || POS_INF,
    watts:             (r) => r.watts ?? 0,
    drag:              (r) => (r._row_kind === "session" ? r._drag : r.drag_factor) || 0,
    spm:               (r) => (r._row_kind === "session" ? r._spm : r.stroke_rate) || 0,
    work_duration:     (r) => r._row_kind === "session" ? (r._work_duration_s || 0) : (r.time || 0) / 10,
    work_distance:     (r) => r._row_kind === "session" ? (r._work_distance_m || 0) : (r.distance || 0),
    other_distance:    (r) => r._row_kind === "session" ? (r._other_distance_m || 0) : 0,
    //hr:                (r) => (r.heart_rate && r.heart_rate.average) || 0,
    season:            (r) => r.date || "",
    structure:         (r) => r.is_interval ? (r.structure_key || "") : "",
    reps:              (r) => r.reps || 0,
    work_pace:         (r) => r.work_pace || POS_INF,
    work_spm:          (r) => r.work_spm || 0,
    workout_structure: (r) => r.is_interval ? (r.structure_key || "") : "",
    similarity:        (r) => r._similarity != null ? r._similarity : -1,
    hr:                (r) => r._hr_spread_score != null ? ((r.heart_rate && r.heart_rate.average) || 0) + r._hr_spread_score : -1,
    ess:               (r) => r._ess != null ? r._ess : -1,
    if_eff:            (r) => r._if_eff != null ? r._if_eff : -1,
    severity:          (r) => r._severity_score != null ? r._severity_score : -1,
    anaerobic_strain:  (r) => r._anaerobic_strain != null ? r._anaerobic_strain : -1,
    glycogen_used:     (r) => r._glycogen_used != null ? r._glycogen_used : -1,
    stimulus:          (r) => {
      // Sort by count of fully-stimulated systems, then by max dose as tiebreak.
      const doses = r._stimulus_doses;
      if (!doses) return -1;
      let count = 0;
      let maxDose = 0;
      for (const k in doses) {
        const v = doses[k] || 0;
        if (v >= 1.0) count++;
        if (v > maxDose) maxDose = v;
      }
      return count + maxDose / 1000;
    },
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
  // We use ``trigger="manual"`` and drive show/hide ourselves from the
  // trigger's mouseenter / mouseleave events.  Shoelace's built-in hover
  // tracking misses our reparented trigger when the cursor moves quickly
  // (the original mouseenter fires before the sl-tooltip exists), which
  // leaves the tooltip stuck visible.  Manual mode + our own state flag
  // is reliable: we *know* when the cursor is over the trigger, so we
  // *know* when the tooltip should hide.
  function lazyTooltipWrap(triggerNode, buildBody, placement) {
    // Single-tooltip rule: register every tooltip we show in a global
    // registry, then dismiss the others before showing this one.  Shared
    // with the LazyTooltip plugin via the same window key, so the rule
    // covers both implementations cohesively.
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
    let tt = null;
    let isOver = false;
    const ensureTooltip = () => {
      if (tt) return;
      tt = document.createElement("sl-tooltip");
      tt.setAttribute("placement", placement || "top");
      tt.setAttribute("hoist", "");
      tt.setAttribute("trigger", "manual");
      const slot = triggerNode.parentNode;
      slot.insertBefore(tt, triggerNode);
      tt.appendChild(triggerNode);
      const body = buildBody();
      body.setAttribute("slot", "content");
      tt.appendChild(body);
    };
    triggerNode.addEventListener("mouseenter", () => {
      isOver = true;
      ensureTooltip();
      // Defer to next frame so the tooltip is in the DOM before we show.
      requestAnimationFrame(() => {
        if (isOver && tt) {
          reg.register(tt);
          reg.hideOthers(tt);
          try { tt.show(); } catch (e) {}
        }
      });
    });
    triggerNode.addEventListener("mouseleave", () => {
      isOver = false;
      if (tt) { try { tt.hide(); } catch (e) {} }
    });
    triggerNode.addEventListener("focusin", () => {
      isOver = true;
      ensureTooltip();
      if (tt) {
        reg.register(tt);
        reg.hideOthers(tt);
        try { tt.show(); } catch (e) {}
      }
    });
    triggerNode.addEventListener("focusout", () => {
      isOver = false;
      if (tt) { try { tt.hide(); } catch (e) {} }
    });
  }

  // ── Cell renderers ────────────────────────────────────────────────────────
  const RENDERERS = {
    text(r, col) {
      const fn = FORMATS[col.format];
      const v = fn ? fn(r) : "";
      return text(v);
    },

    link(r, col) {
      // Tree mode: parent rows link to the most-severe main's workout
      // page; suppress the link entirely when the session is expanded
      // (the children's own View links carry the user to the right
      // place).
      if (r._row_kind === "session") {
        if (state.expanded.has(r.session_id)) {
          return document.createDocumentFragment();
        }
        const target = r._view_target_id;
        if (!target) return document.createDocumentFragment();
        return _viewLink(target);
      }
      // Suppress the "view" link for the row that matches the column's
      // current_id option — used by the "all workouts done on this day"
      // table on the workout page so the page doesn't link back to itself.
      const cur = col && col.opts && col.opts.current_id;
      if (cur != null && String(cur) === String(r.id)) {
        return document.createDocumentFragment();
      }
      return _viewLink(r.id);
    },

    tree_date(r) {
      const manual = _isManuallyAdded(r);
      const wrap = el("div", {
        class: "tree-date" + (manual ? " manually-added" : ""),
      });
      if (r._row_kind === "session" && (r._member_count || 1) > 1) {
        const open = state.expanded.has(r.session_id);
        const chev = el("span",
          { class: "tree-chevron" + (open ? " open" : ""),
            onClick: (ev) => {
              ev.stopPropagation();
              toggleExpand(r.session_id);
            } },
          "▶");
        wrap.appendChild(chev);
      } else {
        // Singleton parent OR child row — reserve the chevron's width so
        // every date cell aligns to the same x-position.
        wrap.appendChild(el("span", { class: "tree-chevron-spacer" }));
      }
      const textWrap = el("div", { class: "tree-date-text" });
      if (r._row_kind === "session") {
        const dateMain = el("div", { class: "date-main" });
        if (manual) {
          dateMain.appendChild(el("span", { class: "manual-icon" }, "⚠ "));
        }
        dateMain.appendChild(document.createTextNode(
          fmtShortDate(r._session_start_dt || r.date)));
        textWrap.appendChild(dateMain);
      } else {
        // Child workout row — role badge.  Singletons stay parents and
        // never reach here, so "single" is a defensive skip.
        const role = r._role || "";
        if (role && role !== "single") {
          textWrap.appendChild(el("span",
            { class: "role-label role-" + role },
            capitalize(role)));
        }
      }
      wrap.appendChild(textWrap);
      return wrap;
    },
    combined_distance(r) {
      // if (r._row_kind === "session") {
      //   return text(fmtDistance(r._work_distance_m));
      // }
      // Tree-mode child: warmup/cooldown/recovery
      // workouts contribute their distance to Other,
      // not Work — even if they're themselves
      // intervals.  The parent already accounts for
      // this in its rollup.

      //if (_isNonMainRole(r._role)) return "—";
      if (r._row_kind === "session") {
        wd = r._work_distance_m
        rd = r._other_distance_m
      } else {
        wd = _isNonMainRole(r._role) ? 0 : r.distance
        rd = (r.rest_distance || 0) + (_isNonMainRole(r._role) ? r.distance : 0)
      }

      if (wd > 0)
        wd = fmtDistance(wd)
      else
        wd = ""
      if (rd > 0)
        rd = fmtDistance(rd)
      else
        rd = ""

      wrap = el("div", { class: "lines" }, [
        el("div", null, wd), 
        el("div", {class: "distance-sub"}, rd)
      ]);

      return wrap;
    },
    time_of_day(r) {
      const manual = _isManuallyAdded(r);
      // Parent (session) row: colloquial period above total session duration.
      if (r._row_kind === "session") {
        const cls = "tod-cell" + (manual ? " manually-added" : "");
        const wrap = el("div", { class: cls });
        if (manual) {
          // Singleton session of a manually-added workout — no real
          // time-of-day.  Flag with "Manual" instead of "Night".
          wrap.appendChild(el("div", { class: "tod-main" }, [
            el("span", { class: "manual-icon" }, "⚠"),
            "Manual",
          ]));
          if (r._session_total_duration_s) {
            wrap.appendChild(el("div", { class: "tod-sub" },
              fmtDurationReadable(r._session_total_duration_s, true)));
          }
          return wrap;
        }
        if (r._session_tod) {
          wrap.appendChild(el("div", { class: "tod-main" }, r._session_tod));
        }
        if (r._session_total_duration_s) {
          wrap.appendChild(el("div", { class: "tod-sub" },
            fmtDurationReadable(r._session_total_duration_s, true)));
        }
        if (!wrap.children.length) return emDash();
        return wrap;
      }
      // Child workout row — its precise start time so the user can see
      // the rhythm of the session.
      if (manual) {
        return el("div", { class: "tod-cell child manually-added" },
          el("div", { class: "tod-main" }, [
            el("span", { class: "manual-icon" }, "⚠"),
            "Manual",
          ]));
      }
      const start = workoutStartHHMM(r);
      if (!start) return document.createDocumentFragment();
      return el("div", { class: "tod-cell child" },
        el("div", { class: "tod-main" }, start));
    },

    gap(r, col) {
      // Render content only in the Main Work column; every other cell
      // for a gap row is empty (cells still exist so the grid lines up).
      if (col.key !== "main_work") return null;
      return el("span", { class: "gap-text" },
        `${fmtDurationReadable(r._gap_seconds, false)} gap`);
    },

    main_work_lines(r) {
      if (r._row_kind === "session") {
        // When the session is expanded, the children (with gap rows in
        // between) tell the full story — the parent's main-work summary
        // becomes redundant noise, so hide it.
        if (state.expanded.has(r.session_id)) return document.createDocumentFragment();
        const lines = r._main_work_lines || [];
        if (!lines.length) return text("");
        const wrap = el("div", { class: "lines" });
        for (const ln of lines) wrap.appendChild(el("div", null, ln));
        return wrap;
      }
      // Child row — show this workout's own one-line description so the
      // expanded view tells you what each piece was.  For non-interval
      // rows the role label in the date cell already conveys it; only
      // interval rows benefit from the explicit label here.
      if (r.is_interval) return text(r.intervals_label || "");
      // Non-interval child: show "<dist or time> @ <pace>".
      const pTen = paceTenths(r);
      const pace = pTen ? fmtSplit(pTen) : "—";
      const wt = (r.workout_type || "");
      const head = wt.indexOf("Time") >= 0
        ? (r.time ? formatTime(r.time) : "—")
        : (r.distance ? fmtDistance(r.distance) : "—");
      return text(`${head} @ ${pace}`);
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

    // Watts cell: watts number on top, small Zone-Spread stacked bar
    // below.  Bar widths come from ``_zone_bin_fractions`` (a 7-element
    // list aligned to BIN_NAMES — index 0 is Rest, indices 1-6 are
    // Sprint/Anaerobic/VO2max/Threshold/Tempo/Endurance time fractions).
    // Tooltip on hover shows the per-band breakdown.
    watts_zones(r, col) {
      const wattsText = fmtWatts(r);
      const fractions = r._zone_bin_fractions;
      const wrap = el("div", { class: "watts-cell" });
      wrap.appendChild(el("div", { class: "watts-num" }, wattsText));
      if (Array.isArray(fractions) && fractions.length) {
        // Render the stacked bar inline; reuse _spreadCell tooltip body
        // but skip its score row (the watts number plays that role here).
        const opts = col.opts || {};
        const bar = _buildSpreadBar(
          fractions, opts.bar_colors || [], opts.bar_w || 5, opts.bar_h || 0.5);
        if (bar) {
          wrap.appendChild(bar);
          // Lazy tooltip: per-band fraction breakdown.
          const skip = new Set(opts.skip_indices || [0]);
          const swatches = opts.swatch_uris || [];
          const zoneNames = opts.zone_names || [];
          const buildBody = () => {
            const total = fractions.reduce(
              (acc, m, idx) => skip.has(idx) ? acc : acc + (m > 0 ? m : 0), 0);
            const body = el("div", { class: "tt-body" });
            body.appendChild(el("div", { class: "tt-title" }, "Approx. Spread of Time in Zone"));
            for (let idx = 0; idx < swatches.length; idx++) {
              if (skip.has(idx)) continue;
              const v = fractions[idx] || 0;
              if (v <= 0) continue;
              const pct = total > 0 ? v / total : 0;
              if (pct < 0.005) continue;
              const row = el("div", { class: "row" }, [
                el("img", { src: swatches[idx] }),
              ]);
              row.appendChild(el("span", { class: "zname" }, zoneNames[idx] || ""));
              row.appendChild(el("span", { class: "pct" }, Math.round(pct * 100) + "%"));
              body.appendChild(row);
            }
            return body;
          };
          lazyTooltipWrap(wrap, buildBody, "top");
        }
      }
      return wrap;
    },

    hr_spread(r, col) {
      const hr = (r.heart_rate && r.heart_rate.average) || null
      const bins = r._hr_bin_meters;
      if (hr == null || bins == null) return emDash();
      return _spreadCell(hr, bins, col);
    },

    severity(r, col) {
      const sev = r._severity;
      if (sev == null) return emDash();
      const styles = (col.opts && col.opts.severity_styles) || {};
      const style = styles[sev];
      if (!style) return text(sev);
      const pill = el("div", { class: "quality-pill", style: { background: style.bg } },
        el("span", { class: "label" }, style.label || sev));
      lazyTooltipWrap(pill, () => _severityTooltipBody(r, col), "top");
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

    stimulus(r, col) {
      const doses = r._stimulus_doses;
      if (!doses) return emDash();
      return _stimulusStripCell(doses, col);
    },
  };

  // ── Stacked-bar builder (work-meter fraction per zone, bin 0 = Rest skipped) ──
  // Skips bin 0, drops segments smaller than 2 % of work total, and falls
  // back to a single grey rect when no work is logged.  Inline SVG instead
  // of an `<img src=data-uri>` so the bar doesn't have to round-trip through
  // Python on every render.
  function _buildSpreadBar(binMeters, colors, widthRem, heightRem) {
    if (!binMeters) return null;
    const SVG_NS = "http://www.w3.org/2000/svg";
    const W = 160;
    const H = 8;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("xmlns", SVG_NS);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    // preserveAspectRatio="none" stretches the rects to fill the CSS box
    // exactly — same effect the original `<img src=data:…>` bar got from
    // the <img> element's box.  Without it the default `xMidYMid meet`
    // letterboxes the 20:1 viewBox inside the 10:1 CSS box and the bar
    // collapses to a hairline.
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
        rect.setAttribute("fill", colors[i] || "#d1d5db");
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

  // ── Stimulus strip builder (six per-band cells, proportional fill) ───
  // Renders a row of six SVG cells, one per duration band.  Each cell's
  // fill height is proportional to the workout's stimulus dose for that
  // band, clamped at 1.0 (anything beyond fills the cell completely):
  //
  //   dose = 0      empty cell (no rect, blank space)
  //   dose ∈ (0,1]  filled rect with height = dose × cell_height
  //   dose > 1.0    full cell (clamped)
  //
  // The 6 bands are passed in via `opts.bands` (ordered list of band-second
  // keys: [20, 90, 300, 1200, 3600, 7200]) plus `opts.colors` (one rgba per
  // band).  When *all* doses are zero, the caller renders an em-dash
  // instead of this strip.
  function _buildStimulusStrip(doses, opts, widthRem, heightRem) {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const bands = opts.bands || [20, 90, 300, 1200, 3600, 7200];
    const colors = opts.colors || [];
    const N = bands.length;
    const cellW = 12;
    const gap = 1;
    const W = N * cellW + (N - 1) * gap;
    const H = 12;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("xmlns", SVG_NS);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("class", "stim-strip");
    svg.style.width = (widthRem || 4.5) + "rem";
    svg.style.height = (heightRem || 0.75) + "rem";
    svg.style.display = "block";
    for (let i = 0; i < N; i++) {
      const band = bands[i];
      const dose = (doses && doses[band]) || (doses && doses[String(band)]) || 0;

      // make a gray shape in each cell
      x = i * (cellW + gap);
      color = "rgba(160,160,160,0.25)";
      fillFrac = 1.0
      fillH = (H - 2) * fillFrac;
      fillY = H - 1 - fillH;
      fill = document.createElementNS(SVG_NS, "rect");
      fill.setAttribute("x", x + 1);
      fill.setAttribute("y", fillY);
      fill.setAttribute("width", cellW - 2);
      fill.setAttribute("height", fillH);
      fill.setAttribute("fill", color);
      svg.appendChild(fill);

      if (dose <= 0) continue;  // no stimulus
      color = colors[i] || "rgba(160,160,160,0.85)";
      fillFrac = Math.min(1.0, dose);  // clamp at 1.0 visually
      fillH = (H - 2) * fillFrac;
      fillY = H - 1 - fillH;
      fill = document.createElementNS(SVG_NS, "rect");
      fill.setAttribute("x", x + 1);
      fill.setAttribute("y", fillY);
      fill.setAttribute("width", cellW - 2);
      fill.setAttribute("height", fillH);
      fill.setAttribute("fill", color);
      svg.appendChild(fill);
    }
    return svg;
  }

  function _stimulusStripCell(doses, col) {
    const opts = col.opts || {};
    const bands = opts.bands || [20, 90, 300, 1200, 3600, 7200];

    // Detect the "recovery row" case: every band's dose is zero.  Render
    // an em-dash instead of the strip — visually quieter and signals
    // "this workout was a recovery row" rather than ambiguously empty.
    let anyDose = false;
    for (const b of bands) {
      const v = (doses && doses[b]) || (doses && doses[String(b)]) || 0;
      if (v > 0) { anyDose = true; break; }
    }

    const trigger = el("div", { class: "stim" });
    if (!anyDose) {
      trigger.appendChild(el("span", { class: "muted" }, "—"));
    } else {
      trigger.appendChild(
        _buildStimulusStrip(doses, opts, opts.strip_w || 4.5, opts.strip_h || 0.75)
      );
    }

    const buildBody = () => {
      const body = el("div", { class: "tt-body" });
      const names = opts.zone_names || [];
      const swatches = opts.swatch_uris || [];
      let stimulated = [];
      for (let i = 0; i < bands.length; i++) {
        const band = bands[i];
        const dose = (doses && doses[band]) || (doses && doses[String(band)]) || 0;
        const name = names[i] || String(band) + "s";
        const state = dose >= 2.0 ? "overdose"
                    : dose >= 1.0 ? "full"
                    : dose >= 0.5 ? "partial"
                    : dose > 0    ? "minimal"
                    : "none";
        if (dose >= 1.0) stimulated.push(name);
        const row = el("div", { class: "row" });
        if (swatches[i]) row.appendChild(el("img", { src: swatches[i] }));
        row.appendChild(el("span", { class: "zname" }, name));
        if (dose > 0) {
          row.appendChild(el("span", { class: "pct" },
            //`${dose.toFixed(2)}× (${state})`));
            `${10 * Math.round(10 * dose)}%`));

        } else {
          row.appendChild(el("span", { class: "pct muted" }, "—"));
        }
        body.appendChild(row);
      }
      const headline = stimulated.length
        ? `Stimulated: ${stimulated.join(" + ")}`
        : (anyDose ? "Sub-threshold stimulus only" : "Recovery row");
      // body.insertBefore(
      //   el("div", { class: "tt-headline" }, headline), body.firstChild);
      body.insertBefore(
        el("div", { class: "tt-headline" }, "very approx. % of full dose"), body.firstChild);

      body.insertBefore(el("div", { class: "tt-title" }, "Training Stimulus"), body.firstChild);

      return body;
    };
    lazyTooltipWrap(trigger, buildBody, "top");
    return trigger;
  }

  // ── Spread cell (score + bar + lazy tooltip with zone breakdown) ─────
  // ``score`` is a number rendered as the headline; ``binMeters`` is the
  // per-bin numeric weights driving the stacked bar.  When opts.show_meters
  // is false, the tooltip drops the meters column (Zone-Spread case where
  // binMeters are time fractions, not actual meter counts).
  function _spreadCell(score, binMeters, col) {
    const opts = col.opts || {};
    const showMeters = opts.show_meters !== false;
    const trigger = el("div", { class: "spread" });
    const scoreEl = el("div", { class: "score" }, score.toFixed(0));
    trigger.appendChild(scoreEl);
    const bar = _buildSpreadBar(
      binMeters, opts.bar_colors || [], opts.bar_w || 5, opts.bar_h || 0.5);
    if (bar) trigger.appendChild(bar);

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
        const item = {
          uri: swatches[idx],
          name: zoneNames[idx] || "",
          pctText: Math.round(pct * 100) + "%",
        };
        if (showMeters) item.metersText = fmtMeters(meters);
        items.push(item);
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
        if (showMeters) {
          row.appendChild(el("span", { class: "meters" }, it.metersText));
        }
        body.appendChild(row);
      }
      return body;
    };
    lazyTooltipWrap(trigger, buildBody, "top");
    return trigger;
  }

  // Severity = max(pk5, pk60, pk20) + 0.50·W'_used + 0.40·glycogen_used.
  // The tooltip decomposes the score into those three contributors, names
  // the dominant one, and lists which physiological systems received a
  // full adaptation-grade dose — so the description reflects what the
  // workout actually was, not the bucket's generic archetype.
  function _severityTooltipBody(r, col) {
    const sev = r._severity;
    const score = r._severity_score || 0;
    const strain = r._anaerobic_strain || 0;
    const glycogen = r._glycogen_used; // may be null when no profile mass
    const stimSystems = r._stimulus_systems || [];

    const strainTerm = 0.5 * strain;
    const glyTerm = 0.4 * (glycogen || 0);
    const peakTerm = Math.max(0, score - strainTerm - glyTerm);

    const contribs = [
      { label: "peak rolling intensity", value: peakTerm },
      { label: "W' depletion", value: strainTerm },
    ];
    if (glycogen != null) {
      contribs.push({ label: "glycogen drain", value: glyTerm });
    }
    const dominant = contribs.reduce((a, b) => (a.value >= b.value ? a : b));

    const body = el("div", { class: "tt-body quality" });
    body.appendChild(el("div", { class: "tt-title" },
      `${sev} severity — ${score.toFixed(2)}`));
    body.appendChild(el("div", { class: "tt-headline" },
      `Driven by ${dominant.label}.`));

    body.appendChild(el("div", { class: "tt-label" },
      `Peak intensity: +${peakTerm.toFixed(2)}`));
    body.appendChild(el("div", { class: "tt-label" },
      `W' depleted ${Math.round(strain * 100)}%: +${strainTerm.toFixed(2)}`));
    if (glycogen != null) {
      body.appendChild(el("div", { class: "tt-label" },
        `Glycogen used ${Math.round(glycogen * 100)}%: +${glyTerm.toFixed(2)}`));
    }

    const bandNames = (col && col.opts && col.opts.band_names) || {};
    const stimText = stimSystems.length
      ? stimSystems.map(b => bandNames[b] || bandNames[String(b)] || `${b}s`).join(" + ")
      : "no system reached full dose";
    body.appendChild(el("div", { class: "tt-label" }, `Stimulus: ${stimText}`));

    return body;
  }

  // ── Search ───────────────────────────────────────────────────────────────
  // The search bar above the grid filters rows on the fly.  Matching rules
  // are applied per-term (whitespace-separated, AND across terms):
  //
  //   • Substring match against a per-row haystack — comments, machine,
  //     workout_type, intervals_label / structure_key, severity bucket,
  //     season, main-work descriptions, and stimulus-system synonyms
  //     (e.g. "vo2" matches a workout that fully stimulates the 300s band).
  //   • Numeric / duration / distance: "1hr", "60min", "5k", "2000m", or
  //     bare numbers like "60" — exact-matched against the workout's
  //     duration or distance and (if interval) each interval's work and
  //     rest leg.  "Exact" allows ±0.5s on durations to absorb fractional-
  //     second rounding; distances are integer-equal.
  //   • "NxM[unit]" patterns ("4x4", "5×500m") — rep count must match
  //     exactly; the work amount exact-matches against any interval leg.
  //   • Leading "~" on any numeric / interval term flips it to fuzzy
  //     (±10 %, ±15 s / ±50 m floor; intervals get ±15 %).  E.g. "5k"
  //     hits only true 5000m efforts, "~5k" hits anything 4500–5500m.
  //
  // In tree mode, a session row matches when any term hits either the
  // parent or any of its workouts.  Sessions matched only via children
  // are auto-expanded so the user can see what triggered the match.

  // Stimulus band → searchable synonyms.  Indexed by the band-seconds
  // key under ``_stimulus_doses``; each entry expands into the haystack
  // when the workout's dose for that band ≥ 1.0.
  const _STIM_SYNONYMS = {
    20:   ["sprint"],
    90:   ["anaerobic"],
    300:  ["vo2", "vo2max"],
    1200: ["threshold", "ftp"],
    3600: ["tempo"],
    7200: ["endurance", "aerobic"],
  };

  function _buildHaystack(row) {
    const parts = [];
    const push = (v) => { if (v != null && v !== "") parts.push(String(v)); };
    push(row.comments);
    push(row.type);
    push(MACHINE_LABELS[(row.type || "").toLowerCase()]);
    push(row.intervals_label);
    push(row.structure_key);
    push(row.workout_type);
    push(row._severity);
    push(row.season);
    if (Array.isArray(row._main_work_lines)) {
      push(row._main_work_lines.join(" "));
    }
    if (row.is_interval) push("interval");
    if (row._stimulus_doses && typeof row._stimulus_doses === "object") {
      for (const k in row._stimulus_doses) {
        const dose = +row._stimulus_doses[k] || 0;
        if (dose >= 1.0) {
          const syns = _STIM_SYNONYMS[parseInt(k, 10)];
          if (syns) for (const s of syns) push(s);
        }
      }
    }
    return parts.join("").toLowerCase();
  }

  function _hayFor(row) {
    if (row._wt_search_hay == null) row._wt_search_hay = _buildHaystack(row);
    return row._wt_search_hay;
  }

  // Convert num+unit to (sec, m) target candidates.  No unit ⇒ try
  // multiple interpretations so a bare "60" hits 60s, 60min, 60m, 60km.
  function _numTargets(num, unit) {
    if (unit === "hr" || unit === "h") return [{sec: num * 3600}];
    if (unit === "min") return [{sec: num * 60}];
    if (unit === "s" || unit === "sec" || unit === '"') return [{sec: num}];
    if (unit === "km" || unit === "k") return [{m: num * 1000}];
    if (unit === "m") return [{m: num}];
    if (unit === "'") return [{sec: num * 60}];
    return [
      {sec: num},
      {sec: num * 60},
      {sec: num * 3600},
      {m: num},
      {m: num * 1000},
    ];
  }

  // Tolerance helpers.  ``fuzzy`` widens the window to ±10 %; otherwise
  // we hold the bar tight: integer-equal for distance, ±0.5 s for
  // duration so a 1hr workout recorded as 60:00.3 still hits "1hr".
  function _durTol(target, fuzzy) {
    return fuzzy ? Math.max(15, target * 0.10) : 0.5;
  }
  function _distTol(target, fuzzy) {
    return fuzzy ? Math.max(50, target * 0.10) : 0;
  }
  // Interval-leg tolerances are slightly looser in fuzzy mode (the user
  // is targeting a specific shape rather than a rough total) but the
  // exact tolerance is the same as whole-row.
  function _ivlDurTol(target, fuzzy) {
    return fuzzy ? Math.max(2, target * 0.15) : 0.5;
  }
  function _ivlDistTol(target, fuzzy) {
    return fuzzy ? Math.max(50, target * 0.10) : 0;
  }

  function _matchDuration(row, target, fuzzy) {
    const tol = _durTol(target, fuzzy);
    const sec = (row.time || 0) / 10;
    if (sec > 0 && Math.abs(sec - target) <= tol) return true;
    const ivs = (row.workout && row.workout.intervals) || [];
    for (const iv of ivs) {
      const s = (iv.time || 0) / 10;
      if (s > 0 && Math.abs(s - target) <= tol) return true;
      const rs = (iv.rest_time || 0) / 10;
      if (rs > 0 && Math.abs(rs - target) <= tol) return true;
    }
    // Tree-mode parent: total work duration is on _work_duration_s.
    if (row._row_kind === "session") {
      const wsec = row._work_duration_s || 0;
      if (wsec > 0 && Math.abs(wsec - target) <= tol) return true;
      const tsec = row._session_total_duration_s || 0;
      if (tsec > 0 && Math.abs(tsec - target) <= tol) return true;
    }
    return false;
  }

  function _matchDistance(row, target, fuzzy) {
    const tol = _distTol(target, fuzzy);
    const m = row.distance || 0;
    if (m > 0 && Math.abs(m - target) <= tol) return true;
    const ivs = (row.workout && row.workout.intervals) || [];
    for (const iv of ivs) {
      if (iv.distance && Math.abs(iv.distance - target) <= tol) return true;
      if (iv.rest_distance && Math.abs(iv.rest_distance - target) <= tol) return true;
    }
    if (row._row_kind === "session") {
      const wm = row._work_distance_m || 0;
      if (wm > 0 && Math.abs(wm - target) <= tol) return true;
    }
    return false;
  }

  // True when at least one interval leg matches the parsed (num, unit).
  function _matchIvlAmount(row, num, unit, fuzzy) {
    const ivs = (row.workout && row.workout.intervals) || [];
    if (!ivs.length) return false;
    const targets = _numTargets(num, unit);
    for (const t of targets) {
      if (t.sec != null) {
        const tol = _ivlDurTol(t.sec, fuzzy);
        for (const iv of ivs) {
          const s = (iv.time || 0) / 10;
          if (s > 0 && Math.abs(s - t.sec) <= tol) return true;
        }
      }
      if (t.m != null) {
        const tol = _ivlDistTol(t.m, fuzzy);
        for (const iv of ivs) {
          if (iv.distance && Math.abs(iv.distance - t.m) <= tol) return true;
        }
      }
    }
    return false;
  }

  // Parse a single search term:
  //   "4x4", "5×500m"      → {kind:"interval", reps, num, unit, fuzzy}
  //   "1hr", "60min", "5k" → {kind:"number",   num, unit, fuzzy}
  //   "60", "1.5"          → {kind:"number",   num, unit:"", fuzzy}
  //   anything else        → {kind:"text"}
  //
  // A leading "~" turns numeric / interval matching fuzzy.  It's
  // ignored for plain text terms (the substring matcher strips it
  // separately so haystacks don't need to contain a literal tilde).
  function _parseTerm(term) {
    let fuzzy = false;
    let t = term;
    if (t.length > 1 && t[0] === "~") {
      fuzzy = true;
      t = t.slice(1);
    }
    let m = /^(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*([a-z'"]*)$/i.exec(t);
    if (m) {
      return {
        kind: "interval",
        reps: parseInt(m[1], 10),
        num: parseFloat(m[2]),
        unit: m[3].toLowerCase(),
        fuzzy,
      };
    }
    m = /^(\d+(?:\.\d+)?)\s*([a-z'"]*)$/i.exec(t);
    if (m) {
      return {
        kind: "number",
        num: parseFloat(m[1]),
        unit: m[2].toLowerCase(),
        fuzzy,
      };
    }
    return {kind: "text"};
  }

  // True iff `term` matches `row` directly (without descending into
  // children — caller handles tree-mode parent/child fallthrough).
  function _termMatchesNode(term, row) {
    const lower = term.toLowerCase();
    // Substring match: strip a leading "~" so the numeric-only modifier
    // doesn't have to appear in the haystack to land a hit.
    const textTerm = lower.length > 1 && lower[0] === "~" ? lower.slice(1) : lower;
    if (textTerm && _hayFor(row).indexOf(textTerm) !== -1) return true;
    const parsed = _parseTerm(lower);
    if (parsed.kind === "number") {
      const targets = _numTargets(parsed.num, parsed.unit);
      for (const t of targets) {
        if (t.sec != null && _matchDuration(row, t.sec, parsed.fuzzy)) return true;
        if (t.m != null && _matchDistance(row, t.m, parsed.fuzzy)) return true;
      }
      return false;
    }
    if (parsed.kind === "interval") {
      const reps = row.reps;
      if (reps != null && reps === parsed.reps) {
        if (_matchIvlAmount(row, parsed.num, parsed.unit, parsed.fuzzy)) return true;
      }
      return false;
    }
    return false;
  }

  function _tokenizeQuery(q) {
    return (q || "").trim().split(/\s+/).filter(Boolean);
  }

  // Decide if a row matches the active query.  Returns
  // {matched, viaChild}; viaChild is true when at least one term only
  // matched through a child workout (used to drive auto-expand).
  function _rowMatchesQuery(row, terms) {
    if (!terms.length) return {matched: true, viaChild: false};
    const isParent = row._row_kind === "session" && Array.isArray(row._children);
    let viaChild = false;
    for (const t of terms) {
      if (_termMatchesNode(t, row)) continue;
      if (isParent) {
        let found = false;
        for (const c of row._children) {
          if (c._row_kind !== "workout") continue;
          if (_termMatchesNode(t, c)) { found = true; break; }
        }
        if (found) { viaChild = true; continue; }
      }
      return {matched: false, viaChild: false};
    }
    return {matched: true, viaChild};
  }

  // Auto-expand set rebuilt on each filter pass.  Used by render() to
  // open sessions whose match was driven only by their children.
  let _autoExpandSessions = new Set();

  // ── Sort + visible-rows pipeline ─────────────────────────────────────────
  function _sortRows(rs) {
    const col = state.cols.find((c) => c.key === state.sortCol);
    if (!col) return rs;
    const keyName = col.sort_key || col.key;
    const fn = SORT_KEYS[keyName];
    if (!fn) return rs;
    const opts = col.opts || {};
    const dir = state.sortAsc ? 1 : -1;
    return rs.slice().sort((a, b) => {
      const ka = fn(a, opts);
      const kb = fn(b, opts);
      if (ka < kb) return -1 * dir;
      if (ka > kb) return  1 * dir;
      return 0;
    });
  }

  function _afterIdFilter() {
    if (state.visibleIds == null) return state.rows;
    const allow = new Set(state.visibleIds);
    return state.rows.filter((r) => allow.has(r.id));
  }

  // Flat mode: filter by visibleIds, search, sort.  Returns
  // ``{list, preSearchTotal}`` so render() can populate the search-count
  // label without re-filtering.
  function visibleAndSorted() {
    const after = _afterIdFilter();
    const preSearchTotal = after.length;
    let rs = after;
    if (state.searchable && state.searchQuery) {
      const terms = _tokenizeQuery(state.searchQuery);
      if (terms.length) {
        rs = rs.filter((r) => _rowMatchesQuery(r, terms).matched);
      }
    }
    return { list: _sortRows(rs), preSearchTotal };
  }

  // Tree mode: sort parents only; expanded children are spliced in
  // immediately below their parent.  Children are pre-sorted by start
  // time and stay in that order regardless of the active column sort.
  // Sessions matched only via children are tracked in
  // ``_autoExpandSessions`` so render() can open them.
  function visibleAndSortedTree() {
    const after = _afterIdFilter();
    const preSearchTotal = after.length;
    let parents = after;
    _autoExpandSessions = new Set();
    if (state.searchable && state.searchQuery) {
      const terms = _tokenizeQuery(state.searchQuery);
      if (terms.length) {
        parents = parents.filter((p) => {
          const m = _rowMatchesQuery(p, terms);
          if (m.matched && m.viaChild && (p._member_count || 1) > 1) {
            _autoExpandSessions.add(p.session_id);
          }
          return m.matched;
        });
      }
    }
    return { list: _sortRows(parents), preSearchTotal };
  }

  function toggleExpand(sid) {
    if (state.expanded.has(sid)) state.expanded.delete(sid);
    else state.expanded.add(sid);
    persistState();
    render();
  }

  // ── Toolbar + search bar (persistent across renders so focus is preserved) ──
  // The toolbar lives directly under ``root``, above the rebuilt-each-
  // render ``container``.  It holds the search bar on the left and a
  // ``paginationSlot`` on the right whose children are swapped each
  // render — neither the input nor the toolbar itself is ever detached,
  // so cursor / focus / selection survive every keystroke.
  let toolbar = null;
  let paginationSlot = null;
  let searchBar = null;
  let searchInput = null;
  let searchCount = null;

  function ensureSearchBar() {
    if (searchBar) return;
    searchBar = el("div", { class: "search-bar" });
    const wrap = el("div", { class: "search-input-wrap" });
    const icon = document.createElement("sl-icon");
    icon.setAttribute("name", "search");
    icon.setAttribute("class", "search-icon");
    wrap.appendChild(icon);
    searchInput = el("input", {
      type: "search",
      class: "search-input",
      placeholder: "Try 5k or ~5k or 1hr or vo2 or 4x4",
      value: state.searchQuery,
      autocomplete: "off",
      spellcheck: "false",
    });
    searchInput.addEventListener("input", (ev) => {
      state.searchQuery = ev.target.value || "";
      state.page = 0;
      persistState();
      render();
    });
    wrap.appendChild(searchInput);
    searchBar.appendChild(wrap);
    searchCount = el("span", { class: "search-count" });
    searchBar.appendChild(searchCount);
  }

  function ensureToolbar() {
    if (!state.searchable) {
      if (toolbar) {
        toolbar.remove();
        toolbar = paginationSlot = null;
      }
      // Throw the searchBar away too — re-created on demand if searchable
      // flips back on.
      searchBar = searchInput = searchCount = null;
      return;
    }
    if (toolbar) return;
    ensureSearchBar();
    toolbar = el("div", { class: "table-toolbar" });
    toolbar.appendChild(searchBar);
    paginationSlot = el("div", { class: "pagination-slot" });
    toolbar.appendChild(paginationSlot);
    // Append after the <style> so it lands at the top of the visible
    // tree but doesn't get reordered relative to ``container`` on
    // subsequent renders.
    root.appendChild(toolbar);
  }

  function updateSearchCount(visibleN, totalN, kindLabel) {
    if (!searchCount) return;
    if (state.searchQuery && visibleN !== totalN) {
      searchCount.textContent =
        `${visibleN} of ${totalN} ${kindLabel}${totalN === 1 ? "" : "s"}`;
    } else {
      searchCount.textContent = "";
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────
  let container = null;
  function render() {
    ensureToolbar();
    if (paginationSlot) paginationSlot.replaceChildren();
    if (container) container.remove();
    container = document.createElement("div");
    root.appendChild(container);

    if (!state.rows.length) {
      container.appendChild(el("div", { class: "empty" }, "No results."));
      updateSearchCount(0, 0, "workout");
      return;
    }

    // Tree mode paginates parents (sessions) and expands children inline
    // under each visible parent on the current page.  Flat mode is the
    // existing behavior — paginate the row list directly.
    let pageRows;            // the rows actually rendered this turn
    let total;               // page count denominator
    let totalPages;
    let countLabel;          // tail of the pagination bar text

    if (state.treeMode) {
      const { list: parents, preSearchTotal } = visibleAndSortedTree();
      total = parents.length;
      updateSearchCount(total, preSearchTotal, "session");
      if (!total) {
        const msg = state.searchQuery
          ? "No sessions match the search."
          : "No results.";
        container.appendChild(el("div", { class: "empty" }, msg));
        return;
      }
      const perPage = state.paginate ? state.perPage : total;
      totalPages = Math.max(1, Math.ceil(total / perPage));
      if (state.page >= totalPages) state.page = totalPages - 1;
      if (state.page < 0) state.page = 0;
      const pageParents = parents.slice(
        state.page * perPage, state.page * perPage + perPage);
      pageRows = [];
      for (const p of pageParents) {
        pageRows.push(p);
        const expanded = state.expanded.has(p.session_id)
          || _autoExpandSessions.has(p.session_id);
        if (expanded && Array.isArray(p._children)) {
          for (const c of p._children) pageRows.push(c);
        }
      }
      countLabel = `${total} session${total === 1 ? "" : "s"}`;
    } else {
      const { list: sorted, preSearchTotal } = visibleAndSorted();
      total = sorted.length;
      updateSearchCount(total, preSearchTotal, "workout");
      if (!total) {
        const msg = state.searchQuery
          ? "No workouts match the search."
          : "No results.";
        container.appendChild(el("div", { class: "empty" }, msg));
        return;
      }
      const perPage = state.paginate ? state.perPage : total;
      totalPages = Math.max(1, Math.ceil(total / perPage));
      if (state.page >= totalPages) state.page = totalPages - 1;
      if (state.page < 0) state.page = 0;
      pageRows = sorted.slice(state.page * perPage, state.page * perPage + perPage);
      countLabel = `${total} workouts`;
    }

    // Grid
    const grid = el("div", { class: "grid" });
    grid.style.gridTemplateColumns = state.cols.map((c) => c.width).join(" ");

    // Header
    for (const col of state.cols) {
      const align = "align-" + (col.align || "center");
      const cell = el("div", { class: `cell hdr ${align}` });

      // In tree mode, the date column needs a chevron-spacer prefix in its
      // header so the "Date" label aligns with the chevroned content below.
      if (state.treeMode && col.key === "date") {
        cell.appendChild(el("span", { class: "tree-chevron-spacer" }));
      }

      if (col.sortable && col.header) {
        const isActive = state.sortCol === col.key;
        const arrow = state.sortAsc ? "▲" : "▼";
        const btn = el("button", {
          class: "sort-btn" + (isActive ? " active" : ""),
          onClick: () => onSortClick(col),
        }, [
          el("span", { class: "sort-arrow hidden" }, arrow), // so header remains aligned over data          
          el("span", { class: "sort-label" }, col.header),
          el("span", { class: "sort-arrow" + (isActive ? "" : " hidden") }, arrow),
        ]);
        cell.appendChild(btn);
      } else if (col.header) {
        cell.appendChild(text(col.header));
        cell.style.fontWeight = "600";
      }
      grid.appendChild(cell);
    }

    // Rows
    let sessionTintIdx = 0;
    let currentTint = "session-tint-b";
    pageRows.forEach((row, i) => {
      const isAlt = i % 2 === 1;
      const isHl = state.highlightIds.has(row.id);
      const isChild = state.treeMode && row._row_kind === "workout";
      const isParent = state.treeMode && row._row_kind === "session";
      const isGap    = state.treeMode && row._row_kind === "gap";

      // Tree-mode session block tinting: flip on each parent so a parent
      // and its expanded children share a single tint distinct from
      // neighboring sessions.
      if (isParent) {
        sessionTintIdx += 1;
        currentTint = (sessionTintIdx % 2 === 0)
          ? "session-tint-a" : "session-tint-b";
      }

      // Whether this row's bottom border should be suppressed because
      // the next row is part of the same session block.
      const next = pageRows[i + 1];
      const nextSid = next && (next._row_kind === "session"
        ? next.session_id : next._session_id);
      const thisSid = row._row_kind === "session" ? row.session_id : row._session_id;
      const sameBlockNext = state.treeMode && next != null
        && next._row_kind !== "session" && nextSid === thisSid;

      const rowManual = !isGap && _isManuallyAdded(row);
      let idx = 0;
      const colCount = state.cols.length;
      for (const col of state.cols) {
        const align = "align-" + (col.align || "center");
        const isEnd = idx === colCount - 1;
        let bgCls;
        if (isHl) bgCls = " hl";
        else if (state.treeMode) bgCls = " " + currentTint;
        else bgCls = isAlt ? " alt" : "";
        const childCls = isChild ? " is-child" : "";
        const gapCls = isGap ? " gap-cell session-internal" : "";
        const internalCls = (!isGap && sameBlockNext) ? " session-internal" : "";
        // Flag the date cell on manually-added rows.  Tree mode: only the
        // session row's date cell flags (the child rows show the "Manual"
        // marker in the time-of-day column instead, and the tree_date
        // renderer doesn't print a date for child rows anyway).  Non-tree
        // mode: every manually-added row's date cell flags since there's
        // no time-of-day column to surface the warning.
        const flagDateCell = rowManual && col.key === "date" && (
          !state.treeMode || row._row_kind === "session"
        );
        const manualCls = flagDateCell ? " manually-added" : "";
        const cls = `cell row-cell ${align}${bgCls}${childCls}${gapCls}${internalCls}${manualCls}`
          + (isEnd ? " end" : "");
        const cell = el("div", { class: cls });
        const renderer = isGap
          ? RENDERERS.gap
          : (RENDERERS[col.renderer] || RENDERERS.text);
        const node = renderer(row, col);
        if (node != null) cell.appendChild(node);
        // Non-tree mode: the date renderer is plain text, so prepend a
        // glyph at the cell level.  Tree mode: the tree_date renderer
        // already injects the glyph inside its .date-main wrapper.
        if (flagDateCell && !state.treeMode) {
          cell.insertBefore(
            el("span", { class: "manual-icon" }, "⚠ "), cell.firstChild);
        }
        grid.appendChild(cell);
        idx += 1;
      }
    });


    function buildPaginationBar() {
      if (!(state.paginate && totalPages > 1)) return null;
      const bar = el("div", { class: "pagination" });
      const prev = el("sl-icon-button", {
        name: "chevron-left", label: "Previous page",
        ...(state.page === 0 ? { disabled: true } : {}),
      });
      prev.addEventListener("click", () => {
        if (state.page > 0) { state.page -= 1; persistState(); render(); }
      });
      bar.appendChild(prev);
      bar.appendChild(text(`Page ${state.page + 1} of ${totalPages}`));
      const next = el("sl-icon-button", {
        name: "chevron-right", label: "Next page",
        ...(state.page >= totalPages - 1 ? { disabled: true } : {}),
      });
      next.addEventListener("click", () => {
        if (state.page < totalPages - 1) { state.page += 1; persistState(); render(); }
      });
      bar.appendChild(next);
      return bar;
    }

    // Top pagination: lives in the toolbar's slot when one exists (so it
    // sits on the same line as the search bar), otherwise it goes at the
    // top of the rebuilt container — same place the un-toolbar'd flow
    // had it.
    const topPag = buildPaginationBar();
    if (topPag) {
      if (paginationSlot) paginationSlot.appendChild(topPag);
      else container.appendChild(topPag);
    }

    container.appendChild(grid);

    // Bottom pagination always lives inside the container, centered.
    const bottomPag = buildPaginationBar();
    if (bottomPag) container.appendChild(bottomPag);
  }

  function onSortClick(col) {
    if (state.sortCol === col.key) {
      state.sortAsc = !state.sortAsc;
    } else {
      state.sortCol = col.key;
      state.sortAsc = !!col.default_asc;
    }
    state.page = 0;
    persistState();
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
      case "tree_mode":       state.treeMode = !!value; break;
      case "searchable":      state.searchable = !!value; break;
      case "link_prefix":     state.linkPrefix = value || ""; break;
      case "reset_token":
        if (value !== state.resetToken) {
          state.resetToken = value;
          state.page = 0;
          state.sortCol = state.defaultSortCol;
          state.sortAsc = state.defaultSortAsc;
          // Filter changes that fire reset_token also collapse all
          // expanded sessions, so children aren't stranded under a
          // since-filtered-out parent.
          state.expanded.clear();
          persistState();
        }
        break;
      default: return;
    }
    render();
  });
});

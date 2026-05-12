/**
 * DataTable — Generic JS-backed sortable table.
 *
 * Sibling to WorkoutTable, sharing CSS via window.ErgNerdTable.gridCSS.
 * No workout-specific renderers, no tree mode, no search bar — every
 * cell is plain text built from row[col.value_key] plus an optional
 * " (row[col.secondary_key])" suffix.
 *
 * Props (Python → JS):
 *   rows              list[dict]   Row data.  Each row has an injected
 *                                  ``_idx`` field so sort_key="_idx"
 *                                  yields insertion order.
 *   columns           list[dict]   Column specs (see below).
 *   default_sort_col  str
 *   default_sort_asc  bool
 *   paginate          bool
 *   rows_per_page     int
 *   initial_rows      int          0 = disabled, >0 = show-all overlay
 *                                  reveals rows past Nth on this page.
 *   reset_token       str          When changed, resets sort/page/show-all.
 *
 * Props (JS → Python):
 *   event_out         {name, payload, seq} | null
 *
 * Column spec shape:
 *   {
 *     key:           "z1",                  // unique id (state/sort)
 *     header:        "Easy",
 *     width:         "minmax(9rem,1fr)",
 *     align:         "center" | "start" | "end",
 *     sortable:      true,
 *     default_asc:   false,
 *     renderer:      "text",                // forward-compat hook
 *     value_key:     "z1_m",                // row field → cell text
 *     secondary_key: "z1_pct",              // optional, shown as " (val)"
 *     sort_key:      "z1_raw",              // optional; defaults to value_key
 *   }
 */

// table_shared.js is declared first in the plugin's _assets, but HyperDiv
// loads scripts via Promise.all (parallel) — so we can't rely on
// table_shared.js having executed at *this* file's top level.  All access
// to ``window.ErgNerdTable`` MUST happen inside the registerPlugin
// callback, which fires only after ALL assets have finished loading.
window.hyperdiv.registerPlugin("DataTable", (ctx) => {
  const root = ctx.domElement;

  // ── Style ────────────────────────────────────────────────────────────────
  const style = document.createElement("style");
  style.textContent = window.ErgNerdTable.gridCSS;
  root.appendChild(style);

  // ── DOM helpers ──────────────────────────────────────────────────────────
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


  // ── State ────────────────────────────────────────────────────────────────
  const state = {
    rows: ctx.initialProps.rows || [],
    cols: ctx.initialProps.columns || [],
    sortCol: ctx.initialProps.default_sort_col || "",
    sortAsc: !!ctx.initialProps.default_sort_asc,
    page: 0,
    perPage: ctx.initialProps.rows_per_page || 50,
    paginate: !!ctx.initialProps.paginate,
    initialRows: ctx.initialProps.initial_rows || 0,
    showAllClicked: false,
    resetToken: ctx.initialProps.reset_token || "",
    defaultSortCol: ctx.initialProps.default_sort_col || "",
    defaultSortAsc: !!ctx.initialProps.default_sort_asc,
  };
  console.log("LDSDS", state.showAllClicked)
  console.log(state)

  // ── State persistence across remount ─────────────────────────────────────
  // Mirror sort/page/show-all to sessionStorage so a back-button revisit
  // lands the table in the same state.  Tolerant reader — additive fields
  // are safe; no version field yet.
  const STATE_STORAGE_KEY = "dtbl:" + ctx.key;
  try {
    const _raw = sessionStorage.getItem(STATE_STORAGE_KEY);
    if (_raw) {
      const _saved = JSON.parse(_raw);
      if (_saved && typeof _saved === "object") {
        if (typeof _saved.sortCol === "string") state.sortCol = _saved.sortCol;
        if (typeof _saved.sortAsc === "boolean") state.sortAsc = _saved.sortAsc;
        // if (typeof _saved.page === "number") state.page = _saved.page;
        // if (typeof _saved.showAllClicked === "boolean") {
        //   state.showAllClicked = _saved.showAllClicked;
        // }
      }
    }
  } catch (e) { /* sessionStorage unavailable — fall back to defaults */ }

  function persistState() {
    try {
      sessionStorage.setItem(STATE_STORAGE_KEY, JSON.stringify({
        sortCol: state.sortCol,
        sortAsc: state.sortAsc,
        page: state.page,
        showAllClicked: state.showAllClicked,
      }));
    } catch (e) { /* quota / disabled — ignore */ }
  }

  let eventSeq = 0;
  function emit(name, payload) {
    eventSeq += 1;
    ctx.updateProp("event_out", { name, payload, seq: eventSeq });
  }

  // ── Sort ─────────────────────────────────────────────────────────────────
  function _sortRows(rs) {
    if (!state.sortCol) return rs;
    const col = state.cols.find((c) => c.key === state.sortCol);
    if (!col) return rs;
    const keyName = col.sort_key || col.value_key || col.key;
    const dir = state.sortAsc ? 1 : -1;
    return rs.slice().sort((a, b) => {
      const ka = a[keyName];
      const kb = b[keyName];
      if (ka == null && kb == null) return 0;
      if (ka == null) return 1;   // nulls last regardless of direction
      if (kb == null) return -1;
      if (ka < kb) return -1 * dir;
      if (ka > kb) return  1 * dir;
      return 0;
    });
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

  // ── Cell text builder ────────────────────────────────────────────────────
  function _cellText(row, col) {
    const v = row[col.value_key];
    const main = (v == null) ? "" : String(v);
    if (col.secondary_key) {
      const s = row[col.secondary_key];
      if (s != null && s !== "") return `${main}  (${s})`;
    }
    return main;
  }

  // ── Render ───────────────────────────────────────────────────────────────
  let container = null;
  function render() {
    if (container) container.remove();
    container = document.createElement("div");
    root.appendChild(container);

    if (!state.rows.length) {
      container.appendChild(el("div", { class: "empty" }, "No results."));
      return;
    }

    const sorted = _sortRows(state.rows);
    const total = sorted.length;
    const perPage = state.paginate ? state.perPage : total;
    const totalPages = Math.max(1, Math.ceil(total / perPage));
    if (state.page >= totalPages) state.page = totalPages - 1;
    if (state.page < 0) state.page = 0;
    let pageRows = sorted.slice(
      state.page * perPage, state.page * perPage + perPage);

    // Show-all clip — applied AFTER pagination, on the current page only.
    const overlayVisible = state.initialRows > 0
      && pageRows.length > state.initialRows
      && !state.showAllClicked;
    if (overlayVisible) {
      pageRows = pageRows.slice(0, state.initialRows);
    }

    // Top pagination — always shown when paginate=true && >1 page.
    const topPag = buildPaginationBar(totalPages);
    if (topPag) container.appendChild(topPag);

    // Grid (wrapped so the show-all overlay can position over it).
    const wrap = el("div", { class: "table-wrap" });
    container.appendChild(wrap);

    const grid = el("div", { class: "grid" });
    grid.style.gridTemplateColumns = state.cols.map((c) => c.width).join(" ");

    // Header
    for (const col of state.cols) {
      const align = "align-" + (col.align || "center");
      const cell = el("div", { class: `cell hdr ${align}` });
      if (col.sortable && col.header) {
        const isActive = state.sortCol === col.key;
        const arrow = state.sortAsc ? "▲" : "▼";
        const btn = el("button", {
          class: "sort-btn" + (isActive ? " active" : ""),
          onClick: () => onSortClick(col),
        }, [
          // Hidden leading arrow so toggling sort never shifts the label
          // horizontally (mirrors WorkoutTable's pattern).
          el("span", { class: "sort-arrow hidden" }, arrow),
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

    // Data rows
    pageRows.forEach((row, i) => {
      const isAlt = i % 2 === 1;
      const colCount = state.cols.length;
      let idx = 0;
      for (const col of state.cols) {
        const align = "align-" + (col.align || "center");
        const isEnd = idx === colCount - 1;
        const cls = `cell row-cell ${align}${isAlt ? " alt" : ""}`
          + (isEnd ? " end" : "");
        const cell = el("div", { class: cls });
        cell.appendChild(text(_cellText(row, col)));
        grid.appendChild(cell);
        idx += 1;
      }
    });

    wrap.appendChild(grid);

    // Show-all overlay — sits inside the wrap, absolutely positioned.
    if (overlayVisible) {
      const overlay = window.ErgNerdTable.buildShowAllOverlay({
        onClick: () => {
          state.showAllClicked = true;
          persistState();
          render();
        },
      });
      wrap.appendChild(overlay);
    }

    // Bottom pagination — suppressed while the overlay is visible.  Note
    // the rule is "overlay visible", NOT "show-all not clicked": when
    // initial_rows >= total, the overlay is hidden and bottom pagination
    // should still appear normally.
    if (!overlayVisible) {
      const bottomPag = buildPaginationBar(totalPages);
      if (bottomPag) container.appendChild(bottomPag);
    }
  }

  function buildPaginationBar(totalPages) {
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

  // ── Initial render ───────────────────────────────────────────────────────
  render();

  // ── Prop updates ─────────────────────────────────────────────────────────
  ctx.onPropUpdate((name, value) => {
    switch (name) {
      case "rows":             state.rows = value || []; break;
      case "columns":          state.cols = value || []; break;
      case "default_sort_col":
        state.defaultSortCol = value || "";
        break;
      case "default_sort_asc":
        state.defaultSortAsc = !!value;
        break;
      case "paginate":         state.paginate = !!value; break;
      case "rows_per_page":    state.perPage = value || 50; break;
      case "initial_rows":
        state.initialRows = value || 0;
        // Toggling the cap counts as a "fresh" presentation — reset the
        // overlay state so newly-capped tables show the button again.
        state.showAllClicked = false;
        persistState();
        break;
      case "reset_token":
        if (value !== state.resetToken) {
          state.resetToken = value;
          state.page = 0;
          state.sortCol = state.defaultSortCol;
          state.sortAsc = state.defaultSortAsc;
          state.showAllClicked = false;
          persistState();
        }
        break;
      default: return;
    }
    render();
  });
});

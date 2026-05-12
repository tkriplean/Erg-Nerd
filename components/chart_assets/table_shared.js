/**
 * table_shared.js — Shared CSS + helpers for WorkoutTable and DataTable.
 *
 * Pure synchronous module — sets `window.ErgNerdTable` at top-level and
 * returns.  Each table plugin's main JS file is loaded *after* this one
 * (see the `_assets` ordering in workout_table.py / data_table.py) and
 * relies on `window.ErgNerdTable` being available at registration time.
 *
 * Exports:
 *   window.ErgNerdTable.gridCSS           Shared CSS string (grid, cells,
 *                                         header, sort button, pagination,
 *                                         show-all overlay).
 *   window.ErgNerdTable.buildShowAllOverlay({onClick, label?})
 *                                         Returns a DOM node that overlays
 *                                         the bottom of a table-wrap and
 *                                         fires onClick when clicked or
 *                                         Enter/Space pressed.
 */

(function () {
  if (window.ErgNerdTable) return; // dedup guard

  const gridCSS = `
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
    .row-cell.hl  {
      background: var(--sl-color-primary-50);
      color: var(--sl-color-primary-700);
      font-weight: 600;
    }

    /* Sort header — always reserve space for the arrow so toggling sort
       direction (or moving the active sort to a different column) never
       changes line count or column width. */
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
    .sort-btn:hover  { color: var(--sl-color-neutral-900); }
    .sort-btn.active { color: var(--sl-color-neutral-900); }
    .sort-btn.active .sort-arrow { color: var(--sl-color-neutral-600); }

    .pagination {
      display: flex; align-items: center; justify-content: center;
      gap: 0.25rem; padding: 0.5rem 0;
      font-size: var(--sl-font-size-small);
      color: var(--sl-color-neutral-500);
    }
    .pagination sl-icon-button { font-size: 1.1rem; }
    .pagination sl-icon-button[disabled] { opacity: 0.3; }

    .empty {
      padding: 1rem;
      color: var(--sl-color-neutral-500);
      font-size: var(--sl-font-size-small);
    }

    /* Show-all overlay — sits at the bottom of a .table-wrap (which must
       be position:relative) so it visually masks the last row(s) of the
       grid.  Gradient fades from transparent at the top to the page
       background at the bottom so the boundary is soft.  The wrap inherits
       the grid's rounded corners via matching border-radius. */
    .table-wrap { position: relative; }
    .show-all-overlay {
      position: absolute;
      left: 0; right: 0; bottom: 0;
      height: 5.5rem;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      border-bottom-left-radius: var(--sl-border-radius-medium, 0.25rem);
      border-bottom-right-radius: var(--sl-border-radius-medium, 0.25rem);
      background: linear-gradient(
        to bottom,
        color-mix(in srgb, var(--sl-color-neutral-200) 0%, transparent) 0%,
        var(--sl-color-neutral-200) 55%,
        var(--sl-color-neutral-200) 100%);
    }
    .show-all-overlay:focus {
      outline: 2px solid var(--sl-color-primary-400);
      outline-offset: -2px;
    }
    .show-all-overlay .show-all-text {
      font-size: var(--sl-font-size-large);
      font-weight: 700;
      color: var(--sl-color-primary-600);
      margin-top: 42px;
    }
    .show-all-overlay:hover .show-all-text {
      color: var(--sl-color-primary-700);
      text-decoration: underline;
    }
  `;

  function buildShowAllOverlay({ onClick, label }) {
    const txt = label || "Show all";
    const div = document.createElement("div");
    div.className = "show-all-overlay";
    div.setAttribute("role", "button");
    div.setAttribute("tabindex", "0");
    div.setAttribute("aria-label", txt);
    const span = document.createElement("span");
    span.className = "show-all-text";
    span.textContent = txt;
    div.appendChild(span);
    div.addEventListener("click", onClick);
    div.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        onClick();
      }
    });
    return div;
  }

  window.ErgNerdTable = { gridCSS, buildShowAllOverlay };
})();

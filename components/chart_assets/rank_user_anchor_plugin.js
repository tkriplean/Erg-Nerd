/**
 * RankUserAnchor — tiny invisible HyperDiv plugin embedded in the user's row
 * inside the Rank Page rankings modal. On register and on every `signature`
 * prop change, scrolls its host element into the center of the nearest
 * scrollable ancestor (the rows container with max-height: 60vh).
 *
 * Double-RAF defers the scroll until after the <sl-dialog> open transition
 * has laid out, so the scroll lands on a measurable, visible container.
 */

window.hyperdiv.registerPlugin("RankUserAnchor", (ctx) => {
  const host = ctx.domElement.host;

  function scrollIntoCenter() {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        try {
          host.scrollIntoView({ block: "center", behavior: "auto" });
        } catch (e) {
          /* ignore */
        }
      });
    });
  }

  scrollIntoCenter();

  ctx.onPropUpdate((propName) => {
    if (propName === "signature") scrollIntoCenter();
  });
});

/* Client directory: keep the bulk "target" dropdown in sync with the chosen action. */
(function () {
  "use strict";
  const action = document.getElementById("bulk-action");
  const target = document.getElementById("bulk-target");
  if (!action || !target) return;

  const sync = () => {
    const kind = action.value.endsWith("_tag") ? "tag" : action.value.endsWith("_category") ? "category" : null;
    target.hidden = !kind;
    let firstVisible = null;
    target.querySelectorAll("option").forEach((opt) => {
      const show = opt.dataset.kind === kind;
      opt.hidden = !show;
      opt.disabled = !show;
      if (show && !firstVisible) firstVisible = opt;
    });
    target.querySelectorAll("optgroup").forEach((g) => {
      g.hidden = ![...g.querySelectorAll("option")].some((o) => !o.hidden);
    });
    const current = target.selectedOptions[0];
    if (firstVisible && (!current || current.hidden)) firstVisible.selected = true;
  };
  action.addEventListener("change", sync);
  sync();
})();

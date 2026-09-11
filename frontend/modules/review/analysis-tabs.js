// Page-session state: switching sources only changes visibility, never board or request state.
export function createAnalysisTabs($) {
  const names = ["engine", "coach"];
  let selected = "engine";
  function select(name) {
    selected = name;
    for (const item of names) {
      const active = item === selected;
      const button = $(`analysis-tab-${item}`);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      $(`analysis-${item}`).hidden = !active;
    }
  }
  return {
    mount() {
      select(selected);
      for (const name of names) {
        const button = $(`analysis-tab-${name}`);
        button.addEventListener("click", () => select(name));
        button.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          event.stopPropagation();
          const next = event.key === "Home" ? "engine" : event.key === "End" ? "coach"
            : names[(names.indexOf(name) + 1) % names.length];
          select(next);
          $(`analysis-tab-${next}`).focus();
        });
      }
    },
  };
}

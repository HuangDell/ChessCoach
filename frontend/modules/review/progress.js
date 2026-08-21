export function createAnalysisProgress({ $, setWorkflowState }) {
  function render(status) {
    const fill = $("analysis-progress-fill");
    const label = $("analysis-progress-label");
    if (!fill || !label) return;
    const multipleGames = status && (status.total_games || 1) > 1;
    const prefix = multipleGames ? `Game ${status.current_game} of ${status.total_games} · ` : "";
    const done = status && status.total ? status.done : 0;
    const total = status && status.total ? status.total : 0;
    const eta = status ? status.eta_seconds : null;
    const phase = status && status.phase ? status.phase : "scanning";
    const phaseLabels = {
      queued: "Queued",
      scanning: "Scanning every position",
      selecting_critical: "Selecting key positions",
      deep_analysis: "Deep-analyzing key positions",
      extracting_facts: "Preparing engine facts",
    };
    const phaseLabel = phaseLabels[phase] || "Analyzing";
    const critical = phase === "deep_analysis" && status && status.critical_total
      ? ` ${status.critical_done || 0}/${status.critical_total}`
      : "";
    const workflowState = {
      queued: "ready_to_analyze",
      scanning: "analyzing_scan",
      selecting_critical: "analyzing_scan",
      deep_analysis: "analyzing_deep",
      extracting_facts: "extracting_facts",
    }[phase] || "analyzing_scan";
    const workflowLabel = phase === "scanning"
      ? `Scanning game: ${done} / ${total || "—"} positions`
      : phase === "deep_analysis"
      ? `Deep analysis: ${(status && status.critical_done) || 0} / ${(status && status.critical_total) || "—"} critical positions`
      : phase === "extracting_facts"
      ? `Preparing facts: ${(status && status.critical_done) || 0} / ${(status && status.critical_total) || "—"}`
      : `${phaseLabel}…`;
    setWorkflowState(
      workflowState,
      workflowLabel,
      "The main line remains navigable during analysis.",
      total ? `${done} / ${total}` : ""
    );
    if (!total || eta == null) {
      fill.classList.add("indeterminate");
      label.textContent = `${prefix}${phaseLabel}${critical}…`;
      return;
    }
    fill.classList.remove("indeterminate");
    const percentage = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
    fill.style.width = percentage + "%";
    label.textContent = `${prefix}${phaseLabel}${critical} · ${percentage}% · ~${Math.max(1, Math.round(eta))}s left`;
  }

  function setVisible(visible) {
    const container = $("analysis-progress");
    if (container) container.hidden = !visible;
    if (visible) render(null);
  }

  return { render, setVisible };
}

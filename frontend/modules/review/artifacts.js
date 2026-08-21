import { apiErrorMessage } from "./helpers.js";

export function createReviewArtifacts({
  $,
  api,
  getSnapshot,
  setState,
  setWorkflowState,
  renderList,
  refreshView,
  renderGraph,
  renderCritical,
}) {
  let generation = 0;
  let busy = false;

  function explanationFor(snapshot, criticalId) {
    return ((snapshot.explanationArtifact && snapshot.explanationArtifact.positions) || []).find(
      (item) => item.critical_id === criticalId
    ) || null;
  }

  function reset(gameId = null) {
    generation += 1;
    busy = false;
    $("generate-explanation").disabled = false;
    setState({
      currentGameId: gameId,
      engineReview: null,
      criticalPositions: [],
      explanationArtifact: null,
      activeCriticalId: null,
    });
  }

  async function generateExplanations() {
    let snapshot = getSnapshot();
    const critical = snapshot.activeCritical;
    if (!snapshot.currentGameId || !critical || busy) return;
    const currentExists = !!explanationFor(snapshot, critical.critical_id);
    const targets = currentExists
      ? [critical]
      : snapshot.criticalPositions.filter(
          (item) => !explanationFor(snapshot, item.critical_id)
        );
    if (!targets.length) return;
    const token = ++generation;
    busy = true;
    const button = $("generate-explanation");
    button.disabled = true;
    let failures = 0;
    for (let index = 0; index < targets.length; index += 1) {
      if (token !== generation) return;
      setWorkflowState(
        "generating_explanations",
        `Preparing explanations: ${index} / ${targets.length}`,
        "The Engine review and board remain available.",
        `${index} / ${targets.length}`
      );
      $("explanation-status").textContent = `Generating ${targets[index].critical_id}…`;
      try {
        const data = await api.generateExplanations(snapshot.currentGameId, {
          review_side: snapshot.player,
          critical_id: targets[index].critical_id,
          force: currentExists,
        });
        if (token !== generation) return;
        if (data.error) throw new Error(apiErrorMessage(data.error, "Explanation failed."));
        setState({ explanationArtifact: data.artifact || getSnapshot().explanationArtifact });
      } catch (error) {
        failures += 1;
        $("explanation-status").textContent = error.message || "Explanation failed.";
      }
      snapshot = getSnapshot();
    }
    if (token !== generation) return;
    busy = false;
    button.disabled = false;
    snapshot = getSnapshot();
    const ready = ((snapshot.explanationArtifact && snapshot.explanationArtifact.positions) || []).length;
    const total = snapshot.criticalPositions.length;
    setWorkflowState(
      failures || ready < total ? "partial_ready" : "review_ready",
      failures ? "Engine review ready · some explanations failed" : "Review ready",
      failures
        ? "Retry from any key position; Engine facts are unaffected."
        : `${ready} grounded explanations available.`,
      `${ready} / ${total}`
    );
    renderCritical(getSnapshot().activeCritical);
  }

  async function load(gameId, side) {
    const currentGameId = gameId || getSnapshot().currentGameId;
    setState({
      currentGameId,
      engineReview: null,
      criticalPositions: [],
      explanationArtifact: null,
      activeCriticalId: null,
    });
    if (!currentGameId) {
      setWorkflowState(
        "partial_ready",
        "Engine review ready",
        "This legacy game has no stored Phase 6 artifact."
      );
      renderList();
      return;
    }
    let engineReview;
    try {
      engineReview = await api.analysis(currentGameId, side);
    } catch (_) {
      setWorkflowState(
        "partial_ready",
        "Engine review ready",
        "Structured artifact is unavailable; timeline navigation still works."
      );
      renderList();
      return;
    }
    setState({
      engineReview,
      criticalPositions: (engineReview.critical_positions || []).slice(),
    });
    try {
      const explanationArtifact = await api.explanations(currentGameId, { review_side: side });
      setState({ explanationArtifact });
    } catch (_) {}
    const snapshot = getSnapshot();
    const ready = ((snapshot.explanationArtifact && snapshot.explanationArtifact.positions) || []).length;
    const total = snapshot.criticalPositions.length;
    setWorkflowState(
      ready === total && total ? "review_ready" : "partial_ready",
      ready === total && total ? "Review ready" : "Engine review ready",
      total
        ? `${total} key positions · AI explanations are optional.`
        : "No critical positions were selected.",
      total ? `${ready} / ${total}` : ""
    );
    $("review-empty").hidden = !!snapshot.criticalPositions.length;
    $("critical-review").hidden = !snapshot.criticalPositions.length;
    refreshView();
    renderGraph();
  }

  return { reset, generateExplanations, load };
}

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
    $("generate-explanations-all").disabled = false;
    setState({
      currentGameId: gameId,
      engineReview: null,
      criticalPositions: [],
      explanationArtifact: null,
      explanationStatuses: {},
      activeCriticalId: null,
    });
  }

  async function generateExplanations({ all = false } = {}) {
    let snapshot = getSnapshot();
    const critical = snapshot.activeCritical;
    if (!snapshot.currentGameId || !critical || busy) return;
    const currentExists = !!explanationFor(snapshot, critical.critical_id);
    const targets = all
      ? snapshot.criticalPositions.filter(
          (item) => !explanationFor(snapshot, item.critical_id)
        )
      : [critical];
    if (!targets.length) return;
    const token = ++generation;
    busy = true;
    const button = $("generate-explanation");
    button.disabled = true;
    $("generate-explanations-all").disabled = true;
    let failures = 0;
    let stopped = false;
    let lastFailure = "";
    for (let index = 0; index < targets.length; index += 1) {
      if (token !== generation) return;
      setWorkflowState(
        "generating_explanations",
        `Preparing explanations: ${index} / ${targets.length}`,
        "The Engine review and board remain available.",
        `${index} / ${targets.length}`
      );
      const id = targets[index].critical_id;
      setState({ explanationStatuses: { ...getSnapshot().explanationStatuses, [id]: "Generating…" } });
      renderCritical(getSnapshot().activeCritical);
      try {
        const data = await api.generateExplanations(snapshot.currentGameId, {
          review_side: snapshot.player,
          critical_id: targets[index].critical_id,
          force: !all && currentExists,
        });
        if (token !== generation) return;
        if (data.error) {
          const error = new Error(apiErrorMessage(data.error, "Explanation failed."));
          error.payload = data;
          throw error;
        }
        setState({
          explanationArtifact: data.artifact || getSnapshot().explanationArtifact,
          explanationStatuses: { ...getSnapshot().explanationStatuses, [id]: "" },
        });
      } catch (error) {
        if (token !== generation) return;
        failures += 1;
        lastFailure = error.message || "Explanation failed.";
        stopped = error.payload?.error?.reason === "authentication_failed";
        if (stopped && all) lastFailure += ` Batch stopped; ${targets.length - index} positions remain unexplained.`;
        setState({ explanationStatuses: { ...getSnapshot().explanationStatuses, [id]: lastFailure } });
      }
      snapshot = getSnapshot();
      if (stopped) break;
    }
    if (token !== generation) return;
    busy = false;
    button.disabled = false;
    $("generate-explanations-all").disabled = false;
    snapshot = getSnapshot();
    const ready = ((snapshot.explanationArtifact && snapshot.explanationArtifact.positions) || []).length;
    const total = snapshot.criticalPositions.length;
    setWorkflowState(
      failures || ready < total ? "partial_ready" : "review_ready",
      failures ? "Engine review ready · some explanations failed" : "Review ready",
      failures
        ? lastFailure
        : `${ready} grounded explanations available.`,
      `Explanations ${ready} / ${total}`
    );
    renderCritical(getSnapshot().activeCritical);
  }

  async function load(gameId, side, options = {}) {
    const token = ++generation;
    const currentGameId = gameId || getSnapshot().currentGameId;
    const isCurrent = () => (
      token === generation && getSnapshot().currentGameId === currentGameId
    );
    setState({
      currentGameId,
      engineReview: null,
      criticalPositions: [],
      explanationArtifact: null,
      explanationStatuses: {},
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
      engineReview = await api.analysis(currentGameId, side, options);
    } catch (error) {
      if (!isCurrent() || (error && error.name === "AbortError")) return false;
      setWorkflowState(
        "partial_ready",
        "Engine review ready",
        "Structured artifact is unavailable; timeline navigation still works."
      );
      renderList();
      return true;
    }
    if (!isCurrent()) return false;
    setState({
      engineReview,
      criticalPositions: (engineReview.critical_positions || []).slice(),
    });
    try {
      const explanationArtifact = await api.explanations(
        currentGameId,
        { review_side: side },
        options
      );
      if (!isCurrent()) return false;
      setState({ explanationArtifact });
    } catch (error) {
      if (!isCurrent() || (error && error.name === "AbortError")) return false;
    }
    if (!isCurrent()) return false;
    const snapshot = getSnapshot();
    const ready = ((snapshot.explanationArtifact && snapshot.explanationArtifact.positions) || []).length;
    const total = snapshot.criticalPositions.length;
    setWorkflowState(
      ready === total && total ? "review_ready" : "partial_ready",
      ready === total && total ? "Review ready" : "Engine review ready",
      total
        ? `${total} key positions · AI explanations are optional.`
        : "No critical positions were selected.",
      ""
    );
    $("review-empty").hidden = !!snapshot.criticalPositions.length;
    $("critical-review").hidden = !snapshot.criticalPositions.length;
    refreshView();
    renderGraph();
    return true;
  }

  return { reset, generateExplanations, load };
}

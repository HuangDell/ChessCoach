export function createReviewCoach({ $ }) {
  function renderQuickSummary(session) {
    const summary = $("coach");
    if (!summary) return;
    const text = (session && session.coach_summary) || "";
    summary.textContent = text;
    summary.hidden = !text;
  }

  function reset() {
    $("coach").hidden = true;
    $("coach").textContent = "";
  }

  return {
    reset,
    renderQuickSummary,
  };
}

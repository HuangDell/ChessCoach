import { renderMarkdown } from "../core/format.js";

export function createReviewCoach({ $, api, hasTimeline }) {
  let autoGenerate = false;
  let requestToken = 0;
  let quickSummaryHasText = false;
  let quickSummaryExpanded = false;
  let aiSummaryReady = false;

  function syncQuickSummary() {
    const summary = $("coach");
    const toggle = $("coach-toggle");
    if (!summary || !toggle) return;
    if (!quickSummaryHasText) {
      summary.hidden = true;
      toggle.hidden = true;
    } else if (!aiSummaryReady) {
      summary.hidden = false;
      toggle.hidden = true;
    } else {
      toggle.hidden = false;
      summary.hidden = !quickSummaryExpanded;
      toggle.textContent = quickSummaryExpanded ? "▾ Hide quick summary" : "▸ Show quick summary";
    }
  }

  function renderQuickSummary(session) {
    const summary = $("coach");
    if (!summary) return;
    const text = (session && session.coach_summary) || "";
    summary.textContent = text;
    quickSummaryHasText = !!text;
    quickSummaryExpanded = false;
    syncQuickSummary();
  }

  function showButton(show) {
    const button = $("coach-ai-btn");
    if (button) button.hidden = !show;
  }

  function setAiSummary(state, text) {
    const summary = $("coach-ai");
    if (!summary) return;
    if (state === "hidden") {
      summary.hidden = true;
      summary.className = "coach-ai";
      summary.textContent = "";
    } else if (state === "pending") {
      summary.hidden = false;
      summary.className = "coach-ai pending";
      summary.textContent = "Snowie is writing up a full game summary…";
    } else if (state === "error") {
      summary.hidden = false;
      summary.className = "coach-ai err";
      summary.textContent = text || "Couldn't generate the summary.";
    } else {
      summary.hidden = false;
      summary.className = "coach-ai";
      summary.innerHTML =
        `<span class="coach-ai-tag">AI coach (Snowie)` +
        `<button id="coach-ai-refresh" class="coach-ai-refresh" type="button" ` +
        `title="Regenerate this summary (uses your Claude subscription)" aria-label="Regenerate summary">⟳</button>` +
        `</span>${renderMarkdown(text)}`;
      $("coach-ai-refresh")?.addEventListener("click", () => fetchSummary(true));
    }
    aiSummaryReady = state === "ready";
    syncQuickSummary();
  }

  async function fetchSummary(force = false) {
    const token = ++requestToken;
    showButton(false);
    setAiSummary("pending");
    let result;
    try {
      result = await api.coach({ force: !!force });
    } catch (_) {
      if (token === requestToken) {
        setAiSummary("error", "Couldn't reach the summary service.");
        showButton(true);
      }
      return;
    }
    if (token !== requestToken) return;
    if (result && result.summary) setAiSummary("ready", result.summary);
    else {
      setAiSummary(result && result.error ? "error" : "hidden", result && result.error);
      showButton(true);
    }
  }

  function prepare(session) {
    requestToken += 1;
    setAiSummary("hidden");
    if (!hasTimeline()) return showButton(false);
    if (session && session.coach_ai_text) {
      setAiSummary("ready", session.coach_ai_text);
      return showButton(false);
    }
    if (autoGenerate) fetchSummary();
    else showButton(true);
  }

  function reset() {
    requestToken += 1;
    quickSummaryHasText = false;
    quickSummaryExpanded = false;
    aiSummaryReady = false;
    $("coach").hidden = true;
    $("coach-toggle").hidden = true;
    setAiSummary("hidden");
    showButton(false);
  }

  function refreshAfterSettings() {
    const card = $("coach-ai");
    const busy = card && !card.hidden && !card.classList.contains("err");
    if (hasTimeline() && !busy) {
      if (autoGenerate) fetchSummary();
      else {
        setAiSummary("hidden");
        showButton(true);
      }
    } else if (!hasTimeline()) {
      setAiSummary("hidden");
      showButton(false);
    }
  }

  function mount() {
    $("coach-ai-btn").addEventListener("click", () => fetchSummary());
    $("coach-toggle").addEventListener("click", () => {
      quickSummaryExpanded = !quickSummaryExpanded;
      syncQuickSummary();
    });
  }

  return {
    mount,
    prepare,
    reset,
    renderQuickSummary,
    refreshAfterSettings,
    setAutoGenerate(value) { autoGenerate = !!value; },
  };
}

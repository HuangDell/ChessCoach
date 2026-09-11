import { sleep } from "../core/async.js";
import { movesToArrows } from "./helpers.js";

const THREAT_DEPTH = 16;
const SEARCH_DEPTHS = [14, 18, 22];
const SEARCH_MAX_MS = 5000;
const SEARCH_DEBOUNCE_MS = 120;

export function createEngineArrowSearch({
  api,
  getFen,
  isBestEnabled,
  isThreatEnabled,
  setBestArrows,
  setThreatArrows,
  setAnalysisRef = () => {},
  onUpdate,
}) {
  let abortController = null;
  let generation = 0;

  function refresh() {
    cancel();
    abortController = new AbortController();
    setBestArrows([]);
    setThreatArrows([]);
    setAnalysisRef(null);
    onUpdate();
    const currentGeneration = generation;
    const fen = getFen();
    if (isBestEnabled()) deepenBestMoves(fen, currentGeneration, abortController.signal);
    if (isThreatEnabled()) fetchThreats(fen, currentGeneration, abortController.signal);
  }

  function cancel() {
    generation += 1;
    if (abortController) abortController.abort();
    abortController = null;
  }

  async function deepenBestMoves(fen, currentGeneration, signal) {
    await sleep(SEARCH_DEBOUNCE_MS);
    if (currentGeneration !== generation) return;
    const startedAt = performance.now();
    for (const depth of SEARCH_DEPTHS) {
      if (currentGeneration !== generation) return;
      let result;
      try {
        result = await api.bestMoves({ fen, depth, multipv: 3 }, { signal });
      } catch (_) {
        return;
      }
      if (currentGeneration !== generation) return;
      if (result && result.moves && result.moves.length) {
        setBestArrows(movesToArrows(result.moves));
        setAnalysisRef(result.analysis_ref || null);
        onUpdate();
      }
      if (performance.now() - startedAt > SEARCH_MAX_MS) break;
    }
  }

  async function fetchThreats(fen, currentGeneration, signal) {
    await sleep(SEARCH_DEBOUNCE_MS);
    if (currentGeneration !== generation) return;
    let result;
    try {
      result = await api.threats({ fen, depth: THREAT_DEPTH, multipv: 3 }, { signal });
    } catch (_) {
      return;
    }
    if (currentGeneration !== generation) return;
    if (result && result.moves && result.moves.length) {
      setThreatArrows(movesToArrows(result.moves, "yellow", 11));
      onUpdate();
    }
  }

  return { refresh, cancel };
}

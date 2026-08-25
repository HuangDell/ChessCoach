import { createLatestRequestScope } from "../core/async.js";
import { errorMessage } from "../core/errors.js";
import { renderMarkdown } from "../core/format.js";
import { storageGet, storageSet } from "../core/storage.js";

const AGENT_SESSION_KEY = "chessAgentSessionId";

function unavailable(capability) {
  return capability && (
    capability.enabled === false ||
    capability.available === false
  );
}

function referenceLabel(reference = {}) {
  if (reference.kind === "critical_position") {
    return reference.critical_id || "Key position";
  }
  if (reference.kind === "game") return "Open game";
  if (reference.kind === "skill") return reference.skill_id || "Review skill";
  if (reference.ply != null) return `Position ${Number(reference.ply) + 1}`;
  return "Open position";
}

function toolSummary(toolCalls = []) {
  if (!toolCalls.length) return "";
  return toolCalls.map((call) => {
    const status = call.status === "ok"
      ? (call.cache_hit ? "cached" : "ok")
      : String(call.status || "error").replaceAll("_", " ");
    return `${call.name || "tool"}: ${status}`;
  }).join(" · ");
}

export function buildReviewAgentContext(snapshot = {}, details = {}) {
  const {
    currentGameId,
    player,
    timeline = [],
    criticalPositions = [],
    activeCriticalId,
    retryActive,
    navigation = {},
  } = snapshot;
  const suppliedFen = details.fen || snapshot.fen;
  const recentMainline = (ply) => timeline
    .slice(Math.max(0, Number(ply) - 8), Number(ply))
    .filter((node) => node.move_uci && node.move_san);
  const criticalAtBasePly = (ply, requestedId = null) => {
    if (requestedId) {
      const requested = criticalPositions.find((item) => item.critical_id === requestedId);
      if (requested && Number(requested.ply) - 1 === Number(ply)) return requested;
    }
    return criticalPositions.find((item) => Number(item.ply) - 1 === Number(ply)) || null;
  };
  const reference = (basePly, baseFen, critical) => ({
    game_id: currentGameId,
    review_side: player,
    critical_id: critical ? critical.critical_id : null,
    ply: critical ? Number(critical.ply) : Number(basePly),
    fen: baseFen,
  });

  if (!currentGameId || !timeline.length) {
    return {
      game_id: null,
      review_side: null,
      active_ply: null,
      active_critical_id: null,
      activity: "position_analysis",
      focus_ref: null,
      position: suppliedFen ? {
        fen: suppliedFen,
        recent_moves_uci: [],
        recent_moves_san: [],
        reference: { fen: suppliedFen },
      } : null,
    };
  }

  if (retryActive) {
    const critical = criticalPositions.find((item) => item.critical_id === activeCriticalId) || null;
    if (critical) {
      const basePly = Math.max(0, Number(critical.ply) - 1);
      const recent = recentMainline(basePly);
      const explorationMovesUci = details.explorationMovesUci || [];
      const explorationMovesSan = details.explorationMovesSan || [];
      return {
        game_id: currentGameId,
        review_side: player,
        active_ply: basePly,
        active_critical_id: critical.critical_id,
        activity: "retry",
        focus_ref: `retry:${critical.critical_id}`,
        position: {
          fen: suppliedFen || critical.fen_before,
          recent_moves_uci: recent.map((node) => node.move_uci),
          recent_moves_san: recent.map((node) => node.move_san),
          ...(explorationMovesUci.length ? {
            exploration_moves_uci: explorationMovesUci,
            exploration_moves_san: explorationMovesSan,
          } : {}),
          reference: reference(basePly, critical.fen_before, critical),
        },
      };
    }
  }

  const exploring = details.mode === "variation" || details.mode === "exploration" ||
    navigation.exploring;
  if (exploring) {
    const basePly = Number(details.basePly ?? navigation.exploreBaseNode ?? navigation.cur);
    const baseFen = details.baseFen || (timeline[basePly] && timeline[basePly].fen) || suppliedFen;
    const critical = criticalAtBasePly(basePly, details.criticalId || activeCriticalId);
    const recent = recentMainline(basePly);
    return {
      game_id: currentGameId,
      review_side: player,
      active_ply: basePly,
      active_critical_id: critical ? critical.critical_id : null,
      activity: "position_analysis",
      focus_ref: details.mode === "variation"
        ? `variation:${critical ? critical.critical_id : basePly}`
        : `exploration:${basePly}`,
      position: {
        fen: suppliedFen,
        recent_moves_uci: recent.map((node) => node.move_uci),
        recent_moves_san: recent.map((node) => node.move_san),
        exploration_moves_uci: details.explorationMovesUci || [],
        exploration_moves_san: details.explorationMovesSan || [],
        reference: reference(basePly, baseFen, critical),
      },
    };
  }

  const activePly = Number(details.ply ?? navigation.cur);
  const fen = suppliedFen || (timeline[activePly] && timeline[activePly].fen);
  const critical = criticalAtBasePly(activePly, activeCriticalId);
  const recent = recentMainline(activePly);
  const position = {
    fen,
    recent_moves_uci: recent.map((node) => node.move_uci),
    recent_moves_san: recent.map((node) => node.move_san),
    reference: reference(activePly, fen, critical),
  };
  if (details.selectedFen === fen && details.selectedMoveSan && details.selectedMoveUci) {
    position.selected_move_san = details.selectedMoveSan;
    position.selected_move_uci = details.selectedMoveUci;
  }
  return {
    game_id: currentGameId,
    review_side: player,
    active_ply: activePly,
    active_critical_id: critical ? critical.critical_id : null,
    activity: "game_review",
    focus_ref: critical ? `critical:${critical.critical_id}` : null,
    position,
  };
}

export function buildTrainingAgentContext(fen) {
  return {
    game_id: null,
    review_side: null,
    active_ply: null,
    active_critical_id: null,
    activity: "training",
    focus_ref: null,
    position: fen ? {
      fen,
      recent_moves_uci: [],
      recent_moves_san: [],
      reference: { fen },
    } : null,
  };
}

export function createReviewChat({
  $,
  agentApi,
  getAgentContext = () => null,
  onReference = () => {},
  onAction = () => {},
  sessionStore = globalThis.sessionStorage,
}) {
  let agentSessionId = storageGet(sessionStore, AGENT_SESSION_KEY, "") || null;
  let agentGeneration = null;
  let capability = null;
  let currentContext = null;
  let currentContextSignature = "";
  let syncedContextSignature = "";
  let queuedContextSignature = "";
  let generation = 0;
  let contextQueue = Promise.resolve();
  let sessionPromise = null;
  let sessionEpoch = 0;
  let pendingMessage = null;
  let restoredSummary = "";
  const messageScope = createLatestRequestScope();
  const actionScope = createLatestRequestScope();

  function addMessage(className, text) {
    const message = document.createElement("div");
    message.className = `chat-msg ${className}`;
    if (className.split(" ").includes("bot")) message.innerHTML = renderMarkdown(text);
    else message.textContent = text;
    const messages = $("chat-messages");
    messages.appendChild(message);
    messages.scrollTop = messages.scrollHeight;
    return message;
  }

  function addInteractiveButton(parent, label, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-response-action";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await handler();
      } catch (error) {
        if (!error || error.name !== "AbortError") {
          addMessage("bot err", errorMessage(error, "That coach action is no longer available."));
        }
      } finally {
        button.disabled = false;
      }
    });
    parent.appendChild(button);
  }

  function addAgentResponse(response = {}, calls = [], actionContext = {}) {
    const message = addMessage("bot", response.text || "(no answer)");
    const references = response.references || [];
    const actions = response.suggested_actions || [];
    if (references.length || actions.length) {
      const controls = document.createElement("div");
      controls.className = "chat-response-actions";
      for (const reference of references) {
        addInteractiveButton(controls, referenceLabel(reference), () => onReference(reference));
      }
      for (const action of actions) {
        addInteractiveButton(controls, action.label || "Open", async () => {
          const request = actionScope.begin();
          if (
            actionContext.uiGeneration !== generation ||
            actionContext.sessionId !== agentSessionId ||
            actionContext.agentGeneration !== agentGeneration
          ) {
            throw new Error("That coach action belongs to an older board context.");
          }
          const result = await onAction(action, {
            sessionId: actionContext.sessionId,
            expectedGeneration: actionContext.agentGeneration,
            signal: request.signal,
            isCurrent: request.isCurrent,
          });
          if (!request.isCurrent() || actionContext.uiGeneration !== generation) {
            throw new DOMException("Superseded", "AbortError");
          }
          return result;
        });
      }
      message.appendChild(controls);
    }
    const summary = toolSummary(calls);
    if (summary) {
      const details = document.createElement("details");
      details.className = "chat-tool-summary";
      const heading = document.createElement("summary");
      heading.textContent = `${calls.length} coach tool${calls.length === 1 ? "" : "s"}`;
      const content = document.createElement("div");
      content.textContent = summary;
      details.appendChild(heading);
      details.appendChild(content);
      message.appendChild(details);
    }
    return message;
  }

  function renderConversationSummary(session) {
    const summary = String((session && session.conversation_summary) || "").trim();
    if (!summary || summary === restoredSummary) return;
    restoredSummary = summary;
    addMessage("bot summary", `**Earlier coaching context:** ${summary}`);
  }

  function invalidatePending() {
    generation += 1;
    messageScope.cancel();
    actionScope.cancel();
    if (pendingMessage) pendingMessage.remove();
    pendingMessage = null;
    $("chat-send").disabled = false;
  }

  function semanticSignature(context) {
    try {
      return JSON.stringify(context || null);
    } catch (_) {
      return "";
    }
  }

  function setContext(context) {
    const signature = semanticSignature(context);
    if (signature && signature === currentContextSignature) return contextQueue;
    currentContext = context || null;
    currentContextSignature = signature;
    invalidatePending();
    const sync = enqueueContextSync(currentContext);
    void sync.catch(() => {});
    return sync;
  }

  // Narrow compatibility adapter for callers that focus one legal move.
  function setMoveContext(fen, san = null, uci = null) {
    const base = currentContext || getAgentContext() || {};
    const position = {
      ...(base.position || {}),
      fen,
      reference: (base.position && base.position.reference) || { fen },
    };
    if (san && uci) {
      position.selected_move_san = san;
      position.selected_move_uci = uci;
    } else {
      delete position.selected_move_san;
      delete position.selected_move_uci;
    }
    return setContext({ ...base, position });
  }

  function contextChanged() {
    currentContext = null;
    currentContextSignature = "";
    syncedContextSignature = "";
    queuedContextSignature = "";
    invalidatePending();
  }

  function reset() {
    invalidatePending();
    const obsoleteSessionId = agentSessionId;
    currentContext = null;
    currentContextSignature = "";
    syncedContextSignature = "";
    queuedContextSignature = "";
    restoredSummary = "";
    $("chat-messages").innerHTML = "";
    forgetAgentSession();
    sessionPromise = null;
    contextQueue = Promise.resolve();
    if (obsoleteSessionId) void deleteAgentSession(obsoleteSessionId);
  }

  function setAgentCapability(value = {}) {
    capability = value;
  }

  function sessionFields(context) {
    const body = {};
    if (context && context.game_id) body.game_id = context.game_id;
    if (context && context.review_side) body.review_side = context.review_side;
    if (context && context.active_ply != null) body.active_ply = context.active_ply;
    if (context && context.active_critical_id) {
      body.active_critical_id = context.active_critical_id;
    }
    return body;
  }

  function adoptAgentSession(payload) {
    const session = payload && payload.session;
    if (!session || !session.session_id || !Number.isInteger(session.generation)) {
      throw new Error("The Agent service returned an invalid session.");
    }
    if (agentSessionId && agentSessionId !== session.session_id) {
      syncedContextSignature = "";
      queuedContextSignature = "";
    }
    agentSessionId = session.session_id;
    agentGeneration = session.generation;
    storageSet(sessionStore, AGENT_SESSION_KEY, agentSessionId);
    return session;
  }

  function forgetAgentSession() {
    sessionEpoch += 1;
    agentSessionId = null;
    agentGeneration = null;
    syncedContextSignature = "";
    queuedContextSignature = "";
    storageSet(sessionStore, AGENT_SESSION_KEY, "");
  }

  async function deleteAgentSession(sessionId) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await agentApi.deleteSession(sessionId);
        return;
      } catch (error) {
        if (!error || error.status !== 409 || attempt > 0) return;
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
    }
  }

  async function createOrRestoreAgentSession() {
    const expectedEpoch = sessionEpoch;
    if (agentSessionId) {
      try {
        const payload = await agentApi.getSession(agentSessionId);
        if (expectedEpoch !== sessionEpoch) throw new DOMException("Superseded", "AbortError");
        return adoptAgentSession(payload);
      } catch (error) {
        if (!error || error.status !== 404) throw error;
        forgetAgentSession();
      }
    }
    const createEpoch = sessionEpoch;
    const context = currentContext || getAgentContext();
    const payload = await agentApi.createSession(sessionFields(context));
    if (createEpoch !== sessionEpoch) {
      const createdId = payload && payload.session && payload.session.session_id;
      if (createdId) void deleteAgentSession(createdId);
      throw new DOMException("Superseded", "AbortError");
    }
    return adoptAgentSession(payload);
  }

  function ensureAgentSession() {
    if (sessionPromise) return sessionPromise;
    const pending = createOrRestoreAgentSession();
    sessionPromise = pending;
    void pending.finally(() => {
      if (sessionPromise === pending) sessionPromise = null;
    }).catch(() => {});
    return pending;
  }

  async function updateAgentContext(context) {
    const updateEpoch = sessionEpoch;
    const update = async (expectedEpoch = updateEpoch) => {
      if (expectedEpoch !== sessionEpoch) throw new DOMException("Superseded", "AbortError");
      const requestedSessionId = agentSessionId;
      const payload = await agentApi.updateContext(requestedSessionId, {
        ...context,
        expected_generation: agentGeneration,
      });
      if (expectedEpoch !== sessionEpoch || requestedSessionId !== agentSessionId) {
        throw new DOMException("Superseded", "AbortError");
      }
      return adoptAgentSession(payload);
    };
    try {
      return await update();
    } catch (error) {
      if (!error || ![404, 409].includes(error.status)) throw error;
      if (error.status === 404) {
        forgetAgentSession();
        await ensureAgentSession();
        return update(sessionEpoch);
      }
      await ensureAgentSession();
      return update();
    }
  }

  function enqueueContextSync(context = null) {
    const desiredContext = context || currentContext || getAgentContext();
    const signature = semanticSignature(desiredContext);
    if (signature && (
      signature === queuedContextSignature ||
      (signature === syncedContextSignature && !queuedContextSignature)
    )) return contextQueue;
    const requestedGeneration = generation;
    queuedContextSignature = signature;
    contextQueue = contextQueue.catch(() => {}).then(async () => {
      if (requestedGeneration !== generation) return null;
      if (!agentSessionId || agentGeneration == null) await ensureAgentSession();
      if (requestedGeneration !== generation) return null;
      const result = await updateAgentContext(desiredContext);
      syncedContextSignature = signature;
      return result;
    }).finally(() => {
      if (queuedContextSignature === signature) queuedContextSignature = "";
    });
    return contextQueue;
  }

  async function restore() {
    const expectedGeneration = generation;
    try {
      const session = await ensureAgentSession();
      if (expectedGeneration !== generation) return;
      renderConversationSummary(session);
      const context = currentContext || getAgentContext();
      if (context) await enqueueContextSync(context);
    } catch (_) {
      // Session restore is optional UI state; Engine Review remains independent.
    }
  }

  async function sendAgent(question, request, expectedGeneration) {
    if (unavailable(capability)) {
      throw new Error(
        capability.reason || "The Agent coach is unavailable. Engine Review is still available."
      );
    }
    await ensureAgentSession();
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    const context = currentContext || getAgentContext();
    if (context) await enqueueContextSync(context);
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    const result = await agentApi.sendMessage(agentSessionId, {
      message: question,
      expected_generation: agentGeneration,
    }, { signal: request.signal });
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    const session = adoptAgentSession(result);
    if (result.error) addMessage("bot err", errorMessage(result.error));
    else addAgentResponse(result.response || {}, result.tool_calls || [], {
      sessionId: agentSessionId,
      agentGeneration: session.generation,
      uiGeneration: generation,
    });
    renderConversationSummary(session);
  }

  async function send(event) {
    event.preventDefault();
    const input = $("chat-input");
    const question = input.value.trim() || "What's the best move in this position, and why?";
    input.value = "";
    addMessage("user", question);
    const request = messageScope.begin();
    const expectedGeneration = generation;
    $("chat-send").disabled = true;
    pendingMessage = addMessage("bot pending", "Snowie is thinking... (a few seconds)");
    try {
      await sendAgent(question, request, expectedGeneration);
    } catch (error) {
      if (request.isCurrent() && error && error.name !== "AbortError") {
        addMessage("bot err", errorMessage(error, "The coach request failed."));
      }
    } finally {
      if (request.isCurrent()) {
        if (pendingMessage) pendingMessage.remove();
        pendingMessage = null;
        $("chat-send").disabled = false;
        input.focus();
      }
    }
  }

  function mount() {
    $("chat-form").addEventListener("submit", send);
  }

  return {
    mount,
    reset,
    restore,
    setAgentCapability,
    setContext,
    setMoveContext,
    contextChanged,
    get generation() { return generation; },
    get sessionId() { return agentSessionId; },
  };
}

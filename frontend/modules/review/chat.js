import { createLatestRequestScope } from "../core/async.js";
import { errorMessage } from "../core/errors.js";
import { renderMarkdown } from "../core/format.js";
import { storageGet, storageSet } from "../core/storage.js";

const AGENT_SESSION_KEY = "chessAgentSessionId";

export function createReviewChat({
  $,
  api,
  agentApi,
  getBoardFen,
  getAgentContext,
  usePersonalHistory,
  sessionStore = globalThis.sessionStorage,
}) {
  let moveFen = null;
  let moveSan = null;
  let moveUci = null;
  let legacySessionId = null;
  let agentSessionId = storageGet(sessionStore, AGENT_SESSION_KEY, "") || null;
  let agentGeneration = null;
  let agentEnabled = false;
  let generation = 0;
  let contextQueue = Promise.resolve();
  let sessionPromise = null;
  let sessionEpoch = 0;
  let pendingMessage = null;
  const messageScope = createLatestRequestScope();

  function addMessage(className, text) {
    const message = document.createElement("div");
    message.className = `chat-msg ${className}`;
    if (className === "bot") message.innerHTML = renderMarkdown(text);
    else message.textContent = text;
    const messages = $("chat-messages");
    messages.appendChild(message);
    messages.scrollTop = messages.scrollHeight;
    return message;
  }

  function setMoveContext(fen, san = null, uci = null) {
    moveFen = fen;
    moveSan = san;
    moveUci = uci;
    invalidatePending();
    if (agentEnabled && agentSessionId) void enqueueContextSync().catch(() => {});
  }

  function reset() {
    invalidatePending();
    const obsoleteSessionId = agentSessionId;
    moveFen = null;
    moveSan = null;
    moveUci = null;
    legacySessionId = null;
    $("chat-messages").innerHTML = "";
    forgetAgentSession();
    sessionPromise = null;
    contextQueue = Promise.resolve();
    if (obsoleteSessionId) void deleteAgentSession(obsoleteSessionId);
  }

  function invalidatePending() {
    generation += 1;
    messageScope.cancel();
    if (pendingMessage) pendingMessage.remove();
    pendingMessage = null;
    $("chat-send").disabled = false;
  }

  function setAgentCapability(capability = {}) {
    const features = capability.features || {};
    const enabled = capability.enabled === true &&
      capability.available === true &&
      features.review_chat === true;
    if (enabled === agentEnabled) return;
    invalidatePending();
    agentEnabled = enabled;
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
    agentSessionId = session.session_id;
    agentGeneration = session.generation;
    storageSet(sessionStore, AGENT_SESSION_KEY, agentSessionId);
    return session;
  }

  function forgetAgentSession() {
    sessionEpoch += 1;
    agentSessionId = null;
    agentGeneration = null;
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
    const payload = await agentApi.createSession(sessionFields(getAgentContext()));
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
    const update = async () => {
      if (updateEpoch !== sessionEpoch) throw new DOMException("Superseded", "AbortError");
      const requestedSessionId = agentSessionId;
      const payload = await agentApi.updateContext(requestedSessionId, {
        ...context,
        expected_generation: agentGeneration,
      });
      if (updateEpoch !== sessionEpoch || requestedSessionId !== agentSessionId) {
        throw new DOMException("Superseded", "AbortError");
      }
      return adoptAgentSession(payload);
    };
    try {
      return await update();
    } catch (error) {
      if (!error || error.status !== 409) throw error;
      await ensureAgentSession();
      return update();
    }
  }

  function enqueueContextSync(context = null) {
    const requestedGeneration = generation;
    contextQueue = contextQueue.catch(() => {}).then(async () => {
      if (!agentEnabled || requestedGeneration !== generation) return null;
      if (!agentSessionId || agentGeneration == null) await ensureAgentSession();
      if (requestedGeneration !== generation) return null;
      return updateAgentContext(context || getAgentContext());
    });
    return contextQueue;
  }

  async function restore() {
    const expectedGeneration = generation;
    if (agentEnabled) {
      try {
        await ensureAgentSession();
        if (expectedGeneration === generation) await enqueueContextSync();
      } catch (_) {}
      return;
    }
    let history;
    try {
      history = await api.chatHistory();
    } catch (_) {
      return;
    }
    if (expectedGeneration !== generation) return;
    $("chat-messages").innerHTML = "";
    for (const message of (history && history.messages) || []) {
      addMessage(message.role === "bot" ? "bot" : "user", message.text);
    }
    legacySessionId = (history && history.session_id) || null;
  }

  async function sendLegacy(question, request, expectedGeneration) {
    const result = await api.chat({
      question,
      fen: getBoardFen(),
      last_move: moveSan,
      move_fen: moveFen,
      session_id: legacySessionId,
      use_profile: usePersonalHistory(),
    }, { signal: request.signal });
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    if (result.error) addMessage("bot err", result.error);
    else {
      addMessage("bot", result.answer || "(no answer)");
      if (result.session_id) legacySessionId = result.session_id;
    }
  }

  async function sendAgent(question, request, expectedGeneration) {
    await ensureAgentSession();
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    await enqueueContextSync(agentContextForMessage());
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    const result = await agentApi.sendMessage(agentSessionId, {
      message: question,
      expected_generation: agentGeneration,
    }, { signal: request.signal });
    if (!request.isCurrent() || expectedGeneration !== generation) return;
    adoptAgentSession(result);
    if (result.error) addMessage("bot err", errorMessage(result.error));
    else addMessage("bot", (result.response && result.response.text) || "(no answer)");
  }

  function agentContextForMessage() {
    const context = getAgentContext();
    if (!moveFen || !moveUci || !moveSan) return context;
    const selectedPosition = {
      ...(context.position || {}),
      fen: moveFen,
      selected_move_uci: moveUci,
      selected_move_san: moveSan,
    };
    if (!context.position || context.position.fen !== moveFen) {
      selectedPosition.recent_moves_uci = [];
      selectedPosition.recent_moves_san = [];
      selectedPosition.reference = { fen: moveFen };
      return {
        game_id: null,
        review_side: null,
        active_ply: null,
        active_critical_id: null,
        activity: "position_analysis",
        position: selectedPosition,
      };
    }
    return { ...context, position: selectedPosition };
  }

  async function send(event) {
    event.preventDefault();
    const input = $("chat-input");
    const question = input.value.trim() || (moveSan
      ? `Why is ${moveSan} bad here?`
      : "What's the best move in this position, and why?");
    input.value = "";
    addMessage("user", question);
    const request = messageScope.begin();
    const expectedGeneration = generation;
    $("chat-send").disabled = true;
    pendingMessage = addMessage("bot pending", "Snowie is thinking… (a few seconds)");
    try {
      if (agentEnabled) await sendAgent(question, request, expectedGeneration);
      else await sendLegacy(question, request, expectedGeneration);
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
    setMoveContext,
    get generation() { return generation; },
  };
}

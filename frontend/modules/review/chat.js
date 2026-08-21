import { renderMarkdown } from "../core/format.js";

export function createReviewChat({ $, api, getBoardFen, usePersonalHistory }) {
  let moveFen = null;
  let moveSan = null;
  let sessionId = null;
  let generation = 0;

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

  function setMoveContext(fen, san = null) {
    moveFen = fen;
    moveSan = san;
  }

  function reset() {
    generation += 1;
    moveFen = null;
    moveSan = null;
    sessionId = null;
    $("chat-messages").innerHTML = "";
  }

  async function restore() {
    const expectedGeneration = generation;
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
    sessionId = (history && history.session_id) || null;
  }

  async function send(event) {
    event.preventDefault();
    const input = $("chat-input");
    const question = input.value.trim() || (moveSan
      ? `Why is ${moveSan} bad here?`
      : "What's the best move in this position, and why?");
    input.value = "";
    addMessage("user", question);
    $("chat-send").disabled = true;
    const pending = addMessage("bot pending", "Snowie is thinking… (a few seconds)");
    try {
      const result = await api.chat({
        question,
        fen: getBoardFen(),
        last_move: moveSan,
        move_fen: moveFen,
        session_id: sessionId,
        use_profile: usePersonalHistory(),
      });
      pending.remove();
      if (result.error) addMessage("bot err", result.error);
      else {
        addMessage("bot", result.answer || "(no answer)");
        if (result.session_id) sessionId = result.session_id;
      }
    } catch (error) {
      pending.remove();
      addMessage("bot err", "Request failed: " + error);
    } finally {
      $("chat-send").disabled = false;
      input.focus();
    }
  }

  function mount() {
    $("chat-form").addEventListener("submit", send);
  }

  return {
    mount,
    reset,
    restore,
    setMoveContext,
    get generation() { return generation; },
  };
}

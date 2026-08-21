import { chatApi } from "../api/chat.js";
import { createLatestRequestScope } from "../core/async.js";
import { errorMessage } from "../core/errors.js";
import { renderMarkdown } from "../core/format.js";

export function createPuzzleChat({ $, getPuzzle, usePersonalHistory, onEngage }) {
  const requests = createLatestRequestScope();
  let sessionId = null;
  let fen = null;
  let busy = false;

  function addMessage(className, text) {
    const message = document.createElement("div");
    message.className = `chat-msg ${className}`;
    if (className === "bot") message.innerHTML = renderMarkdown(text);
    else message.textContent = text;
    const messages = $("pz-chat-messages");
    messages.appendChild(message);
    messages.scrollTop = messages.scrollHeight;
    return message;
  }

  function reset() {
    requests.cancel();
    sessionId = null;
    fen = null;
    busy = false;
    const messages = $("pz-chat-messages");
    if (messages) messages.innerHTML = "";
    const panel = $("pz-chat");
    if (panel) panel.hidden = true;
    const input = $("pz-chat-input");
    if (input) input.value = "";
    const sendButton = $("pz-chat-send");
    if (sendButton) sendButton.disabled = false;
  }

  async function send(event) {
    event.preventDefault();
    const puzzle = getPuzzle();
    if (busy || !puzzle) return;
    onEngage();
    const input = $("pz-chat-input");
    const question = input.value.trim() || "Can you explain that a bit more?";
    input.value = "";
    addMessage("user", question);
    busy = true;
    $("pz-chat-send").disabled = true;
    const pending = addMessage("bot pending", "Snowie is sniffing around (thinking)");
    const request = requests.begin();
    const move = puzzle.source === "your_games" ? puzzle.played_uci : puzzle._yourMove || null;
    try {
      const response = await chatApi.send({
        question,
        fen: fen || puzzle.solve_fen || puzzle.fen || null,
        last_move: move,
        session_id: sessionId,
        use_profile: usePersonalHistory(),
      }, { signal: request.signal });
      if (!request.isCurrent()) return;
      pending.remove();
      if (response.error) addMessage("bot err", errorMessage(response.error));
      else {
        addMessage("bot", response.answer || "(no answer)");
        if (response.session_id) sessionId = response.session_id;
      }
    } catch (error) {
      if (!request.isCurrent()) return;
      pending.remove();
      addMessage("bot err", `Request failed: ${errorMessage(error)}`);
    } finally {
      if (request.isCurrent()) {
        busy = false;
        $("pz-chat-send").disabled = false;
        input.focus();
      }
    }
  }

  return {
    reset,
    send,
    setContext(nextSessionId, nextFen) {
      sessionId = nextSessionId || null;
      fen = nextFen || null;
      $("pz-chat").hidden = false;
    },
    snapshot: () => ({ sessionId, fen }),
    restore(context = {}) {
      sessionId = context.sessionId || null;
      fen = context.fen || null;
    },
  };
}

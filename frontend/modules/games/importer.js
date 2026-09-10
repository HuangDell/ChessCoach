import { gamesApi } from "../api/games.js";
import { createLatestRequestScope } from "../core/async.js";
import { errorMessage } from "../core/errors.js";
import { countPgnGames, dragHasFiles, firstPgnFile } from "./helpers.js";

export function createGamesImporter({ $, bridge, setMode, isPasteMode }) {
  const requests = createLatestRequestScope();
  let sourceType = "pgn_text";

  function updateHint() {
    if (!isPasteMode()) return;
    const pgn = $("paste-pgn").value.trim();
    const status = $("history-status");
    status.classList.remove("import-error", "import-success", "import-action");
    if (!pgn) {
      status.textContent = "Paste or upload a PGN (one or many games).";
    } else if (/^https?:\/\//i.test(pgn)) {
      status.classList.add("import-action");
      status.textContent = "Download or copy the game's PGN, then paste it here.";
    } else {
      const count = countPgnGames(pgn);
      status.textContent = count > 1
        ? `${count} games detected — all will be imported.`
        : "1 game ready to import.";
    }
  }

  async function analyze(pgn, side, username, importSource = sourceType) {
    const request = requests.begin();
    $("firstrun").hidden = true;
    const status = $("history-status");
    const submit = $("paste-submit");
    status.classList.remove("import-error", "import-success", "import-action");
    if (/^https?:\/\//i.test(pgn.trim())) {
      status.classList.add("import-action");
      status.textContent = "Single-game URLs aren't supported. Download or copy the PGN first.";
      return;
    }
    bridge.review.exitFreeAnalysis();

    status.textContent = "Importing PGN…";
    bridge.review.setWorkflowState(
      "importing",
      "Importing PGN",
      "Normalizing headers and replaying legal moves."
    );
    submit.disabled = true;
    let data;
    try {
      data = await gamesApi.importPgn(
        {
          pgn,
          source_type: importSource || "pgn_text",
          review_side: side || "auto",
          username: username || "",
        },
        { signal: request.signal }
      );
    } catch (error) {
      if (!request.isCurrent()) return;
      data = error && error.payload;
      if (!data) {
        status.classList.add("import-error");
        status.textContent = "Could not reach the import service.";
        bridge.review.setWorkflowState(
          "failed",
          "Import failed",
          "Could not reach the local import service."
        );
        submit.disabled = false;
        return;
      }
    }
    if (!request.isCurrent()) return;
    submit.disabled = false;
    if (data.error) {
      const urlHint = data.error.code === "url_not_supported";
      status.classList.add(urlHint ? "import-action" : "import-error");
      status.textContent = errorMessage(data.error, "PGN import failed.");
      bridge.review.setWorkflowState("failed", "Import failed", status.textContent);
      return;
    }

    const imported = data.games || (data.game_id ? [data] : []);
    if (!imported.length) {
      status.classList.add("import-error");
      status.textContent = "The importer returned no games.";
      bridge.review.setWorkflowState("failed", "Import failed", status.textContent);
      return;
    }
    if (side === "auto" && imported.some((game) => !game.review_side)) {
      status.classList.add("import-action");
      status.textContent = imported.length === 1
        ? "Imported. Choose White or Black, then continue."
        : "Imported. Enter your username or choose a review side, then continue.";
      $("paste-side").focus();
      bridge.review.setWorkflowState(
        "ready_to_analyze",
        "Game imported",
        "Choose a review side to continue."
      );
      return;
    }

    const normalized = imported.map((game) => game.pgn).join("\n");
    if (imported.length > 1) {
      status.classList.add("import-success");
      status.textContent = `Imported ${imported.length} games. Starting analysis…`;
      bridge.review.openBatch(normalized, side, username);
      return;
    }

    const game = imported[0];
    const selected = game.review_side || side;
    bridge.review.setWorkflowState(
      "ready_to_analyze",
      "Game ready to analyze",
      `Reviewing ${selected}.`
    );
    status.classList.add("import-success");
    status.textContent = game.analysis_cached
      ? "Imported. Cached analysis found — opening it now."
      : game.already_imported
      ? "Game already imported. Starting analysis…"
      : "Imported and ready. Starting analysis…";
    bridge.review.openGame(game.pgn, selected, game.game_id);
  }

  function loadFile(file, analyzeImmediately) {
    if (!file) return;
    sourceType = "pgn_file";
    const reader = new FileReader();
    reader.onload = () => {
      setMode("paste");
      $("paste-pgn").value = reader.result || "";
      updateHint();
      if (!analyzeImmediately) return;
      const pgn = $("paste-pgn").value.trim();
      if (!pgn) {
        $("history-status").textContent = "That file had no PGN text.";
        return;
      }
      analyze(
        pgn,
        $("paste-side").value || "auto",
        $("paste-username").value.trim(),
        "pgn_file"
      );
    };
    reader.onerror = () => {
      $("history-status").textContent = "Could not read that file.";
    };
    reader.readAsText(file);
  }

  function initDrop() {
    const column = $("history-col");
    if (!column) return;
    let depth = 0;
    const clear = () => {
      depth = 0;
      column.classList.remove("drag-over");
    };
    column.addEventListener("dragenter", (event) => {
      if (!dragHasFiles(event)) return;
      event.preventDefault();
      depth += 1;
      column.classList.add("drag-over");
    });
    column.addEventListener("dragover", (event) => {
      if (!dragHasFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });
    column.addEventListener("dragleave", (event) => {
      if (dragHasFiles(event) && --depth <= 0) clear();
    });
    column.addEventListener("drop", (event) => {
      if (!dragHasFiles(event)) return;
      event.preventDefault();
      clear();
      const file = firstPgnFile(event.dataTransfer);
      if (!file) {
        setMode("paste");
        $("history-status").textContent = "Drop a .pgn file to analyze it.";
        return;
      }
      loadFile(file, true);
    });
    for (const eventName of ["dragover", "drop"]) {
      document.addEventListener(eventName, (event) => {
        if (dragHasFiles(event)) event.preventDefault();
      });
    }
  }

  function mount() {
    $("paste-upload").addEventListener("click", () => $("paste-file").click());
    $("paste-file").addEventListener("change", (event) => {
      loadFile(event.target.files && event.target.files[0], false);
      event.target.value = "";
    });
    $("paste-pgn").addEventListener("input", () => {
      sourceType = "pgn_text";
      updateHint();
    });
    $("paste-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const pgn = $("paste-pgn").value.trim();
      if (!pgn) {
        $("history-status").textContent = "Paste or upload a PGN first.";
        return;
      }
      analyze(
        pgn,
        $("paste-side").value || "auto",
        $("paste-username").value.trim(),
        sourceType
      );
    });
    initDrop();
  }

  return { mount, updateHint };
}

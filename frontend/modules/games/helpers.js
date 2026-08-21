export const resultClass = (result) =>
  result === "win" ? "win" : result === "loss" ? "loss" : result === "draw" ? "draw" : "";

export const resultWord = (result) => ({ win: "Won", loss: "Lost", draw: "Drew" })[result] || "";

export function sideForUser(game, username) {
  const white = String((game && game.white) || "").toLowerCase();
  const black = String((game && game.black) || "").toLowerCase();
  const player = String(username || "").toLowerCase();
  return player && white === player ? "white" : player && black === player ? "black" : "auto";
}

export function historyRows(response) {
  return response && Array.isArray(response.games) ? response.games : [];
}

export function countPgnGames(pgn) {
  return Math.max(1, (String(pgn || "").match(/^\s*\[Event\b/gm) || []).length);
}

export function dragHasFiles(event) {
  return !!event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files");
}

export function firstPgnFile(dataTransfer) {
  const files = dataTransfer && dataTransfer.files ? Array.from(dataTransfer.files) : [];
  return files.find((file) => /\.(pgn|txt)$/i.test(file.name || "")) || null;
}

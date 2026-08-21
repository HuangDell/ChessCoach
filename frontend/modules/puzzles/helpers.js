const NON_MOTIF_THEMES = new Set([
  "oneMove", "short", "long", "veryLong",
  "master", "masterVsMaster", "superGM",
  "opening", "middlegame", "endgame",
  "crushing", "advantage", "equality", "mate",
]);

export function motifThemes(themes) {
  return (Array.isArray(themes) ? themes : []).filter(
    (theme) => theme && !NON_MOTIF_THEMES.has(theme) && !/^mateIn\d+$/.test(theme)
  );
}

export function squarePosition(square, orientation = "white") {
  if (!/^[a-h][1-8]$/.test(String(square || ""))) return null;
  let file = square.charCodeAt(0) - 97;
  let rank = 8 - Number(square[1]);
  if (orientation === "black") {
    file = 7 - file;
    rank = 7 - rank;
  }
  return { left: `${file * 12.5}%`, top: `${rank * 12.5}%` };
}

export function formatClock(seconds) {
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

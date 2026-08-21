"""Stable identity for a chess game, independent of mutable PGN metadata."""
from __future__ import annotations

import hashlib
import io

import chess
import chess.pgn

GAME_ID_LENGTH = 20


def game_id_for_moves(uci_moves: list[str], initial_fen: str | None = None) -> str:
    """Return a SHA-256 prefix for a legal mainline and its optional setup position."""
    position = initial_fen.strip() if initial_fen else "startpos"
    payload = f"{position}\n{' '.join(uci_moves)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:GAME_ID_LENGTH]


def setup_fen(headers: dict[str, str], initial_fen: str) -> str | None:
    """Include FEN in identity only when the game does not start from the standard position."""
    if headers.get("SetUp") == "1" or initial_fen != chess.STARTING_FEN:
        return initial_fen
    return None


def setup_fen_from_headers(headers: dict[str, str]) -> str | None:
    raw = (headers.get("FEN") or "").strip()
    if headers.get("SetUp") != "1" or not raw:
        return None
    try:
        return chess.Board(raw).fen()
    except ValueError:
        return raw


def game_id_from_game(game: chess.pgn.Game) -> str:
    board = game.board()
    initial_fen = board.fen()
    moves = [move.uci() for move in game.mainline_moves()]
    return game_id_for_moves(moves, setup_fen(dict(game.headers), initial_fen))


def game_id_from_pgn(pgn: str) -> str | None:
    try:
        game = chess.pgn.read_game(io.StringIO(pgn or ""))
    except Exception:
        return None
    if game is None or game.errors:
        return None
    moves = list(game.mainline_moves())
    if not moves:
        return None
    board = game.board()
    return game_id_for_moves(
        [move.uci() for move in moves],
        setup_fen(dict(game.headers), board.fen()),
    )

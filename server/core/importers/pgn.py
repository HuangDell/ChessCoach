"""PGN text/file importer.

The importer is engine-free. It parses and legally replays each mainline with python-chess,
produces a deterministic PGN containing only that mainline, and keeps the untouched source text
separately for provenance.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import chess
import chess.pgn

from server import config
from server.core import game_identity

_EVENT_BOUNDARY = re.compile(r"(?m)^(?=\[Event\b)")
_SOURCE_TYPES = {"pgn_text", "pgn_file", "chesscom_sync", "lichess"}
_MAX_PGN_BYTES = 10 * 1024 * 1024


class PgnImportError(ValueError):
    def __init__(self, code: str, message: str, *, game_index: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.game_index = game_index


class _QuietGameBuilder(chess.pgn.GameBuilder):
    """Collect parser errors for the API response without logging user input to stderr."""

    def handle_error(self, error: Exception) -> None:
        self.game.errors.append(error)


@dataclass
class ImportedGame:
    game_id: str
    source_type: str
    source_url: str | None
    pgn: str
    original_pgn: str
    headers: dict[str, str]
    ply_count: int
    review_side: str | None
    moves: list[dict]
    already_imported: bool = False
    analysis_cached: bool = False
    cached_sides: list[str] = field(default_factory=list)

    def to_dict(self, *, include_moves: bool = False) -> dict:
        data = {
            "game_id": self.game_id,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "pgn": self.pgn,
            "headers": self.headers,
            "ply_count": self.ply_count,
            "review_side": self.review_side,
            "review_side_required": self.review_side is None,
            "already_imported": self.already_imported,
            "analysis_cached": self.analysis_cached,
            "cached_sides": self.cached_sides,
        }
        if include_moves:
            data["moves"] = self.moves
        return data

    def metadata(self) -> dict:
        return {
            "artifact_version": 1,
            "game_id": self.game_id,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "headers": self.headers,
            "ply_count": self.ply_count,
            "review_side": self.review_side,
            "moves": self.moves,
        }


def _looks_like_url(value: str) -> bool:
    first = value.strip().splitlines()[0].strip() if value.strip() else ""
    try:
        parsed = urlparse(first)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _chunks(text: str) -> list[str]:
    chunks = [part.strip() for part in _EVENT_BOUNDARY.split(text) if part.strip()]
    return chunks or ([text.strip()] if text.strip() else [])


def _configured_handles(extra_handle: str = "") -> set[str]:
    handles = {
        config.USERNAME,
        config.LICHESS_USERNAME,
        config.CHESSCOM_USERNAME,
        extra_handle,
    }
    handles.update(alias for _, alias in config.USERNAME_ALIASES)
    return {handle.strip().casefold() for handle in handles if handle and handle.strip()}


def resolve_review_side(
    headers: dict[str, str], requested: str = "auto", *, username: str = ""
) -> str | None:
    side = (requested or "auto").strip().lower()
    if side in {"white", "black"}:
        return side
    if side != "auto":
        raise PgnImportError(
            "invalid_review_side", "Review side must be 'white', 'black', or 'auto'."
        )
    mine = _configured_handles(username)
    white = (headers.get("White") or "").strip().casefold()
    black = (headers.get("Black") or "").strip().casefold()
    white_match = bool(white and white in mine)
    black_match = bool(black and black in mine)
    if white_match != black_match:
        return "white" if white_match else "black"
    return None


def _source_url(headers: dict[str, str], explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    for name in ("Link", "Site"):
        value = (headers.get(name) or "").strip()
        if value.startswith(("https://", "http://")):
            return value
    return None


def _parse_one(
    source: str,
    *,
    source_type: str,
    source_url: str | None,
    review_side: str,
    username: str,
    game_index: int,
) -> ImportedGame:
    try:
        game = chess.pgn.read_game(io.StringIO(source), Visitor=_QuietGameBuilder)
    except Exception as exc:
        raise PgnImportError(
            "invalid_pgn", f"Could not parse game {game_index}: {exc}", game_index=game_index
        ) from exc
    if game is None:
        raise PgnImportError(
            "invalid_pgn", f"No chess game found at game {game_index}.", game_index=game_index
        )
    if game.errors:
        detail = str(game.errors[0])
        raise PgnImportError(
            "invalid_pgn",
            f"Game {game_index} contains an illegal or unreadable move: {detail}",
            game_index=game_index,
        )

    headers = {str(key): str(value) for key, value in game.headers.items()}
    variant = (headers.get("Variant") or "").strip().lower()
    if variant and variant not in {"standard", "chess"}:
        raise PgnImportError(
            "unsupported_variant",
            f"Game {game_index} uses unsupported variant '{headers['Variant']}'.",
            game_index=game_index,
        )

    board = game.board()
    initial_fen = board.fen()
    normalized = chess.pgn.Game()
    normalized.headers.clear()
    normalized.headers.update(headers)
    target_node: chess.pgn.GameNode = normalized
    moves: list[dict] = []
    source_node: chess.pgn.GameNode = game

    while source_node.variations:
        source_node = source_node.variations[0]
        move = source_node.move
        if move not in board.legal_moves:
            raise PgnImportError(
                "invalid_pgn",
                f"Game {game_index} contains illegal move {move.uci()} at ply {len(moves) + 1}.",
                game_index=game_index,
            )
        fen_before = board.fen()
        san = board.san(move)
        mover = "white" if board.turn == chess.WHITE else "black"
        move_number = board.fullmove_number
        clock = source_node.clock()
        target_node = target_node.add_main_variation(move)
        if clock is not None:
            target_node.set_clock(clock)
        board.push(move)
        moves.append(
            {
                "ply": len(moves) + 1,
                "move_number": move_number,
                "side": mover,
                "fen_before": fen_before,
                "fen_after": board.fen(),
                "uci": move.uci(),
                "san": san,
                "clock_seconds": clock,
            }
        )

    if not moves:
        raise PgnImportError(
            "invalid_pgn", f"Game {game_index} has no mainline moves.", game_index=game_index
        )

    exporter = chess.pgn.StringExporter(
        headers=True, variations=False, comments=True, columns=None
    )
    normalized_pgn = normalized.accept(exporter).strip() + "\n"
    gid = game_identity.game_id_for_moves(
        [move["uci"] for move in moves],
        game_identity.setup_fen(headers, initial_fen),
    )
    return ImportedGame(
        game_id=gid,
        source_type=source_type,
        source_url=_source_url(headers, source_url),
        pgn=normalized_pgn,
        original_pgn=source.strip() + "\n",
        headers=headers,
        ply_count=len(moves),
        review_side=resolve_review_side(headers, review_side, username=username),
        moves=moves,
    )


def import_pgn(
    text: str,
    *,
    source_type: str = "pgn_text",
    source_url: str | None = None,
    review_side: str = "auto",
    username: str = "",
) -> list[ImportedGame]:
    """Parse one or more PGNs into deterministic, legally validated mainline artifacts."""
    raw = (text or "").lstrip("\ufeff").strip()
    if not raw:
        raise PgnImportError("empty_input", "Paste PGN text or choose a PGN file first.")
    if len(raw.encode("utf-8")) > _MAX_PGN_BYTES:
        raise PgnImportError("input_too_large", "PGN input is larger than the 10 MB limit.")
    if _looks_like_url(raw):
        raise PgnImportError(
            "url_not_supported",
            "Single-game URLs are not imported yet. Download or copy the PGN from Chess.com, "
            "then paste or upload it here.",
        )
    normalized_source_type = source_type if source_type in _SOURCE_TYPES else "pgn_text"
    chunks = _chunks(raw)
    games = [
        _parse_one(
            chunk,
            source_type=normalized_source_type,
            source_url=source_url,
            review_side=review_side,
            username=username,
            game_index=index,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    if not games:
        raise PgnImportError("invalid_pgn", "No valid chess game was found in the PGN.")
    return games

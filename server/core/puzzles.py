"""Puzzle loading, selection, and move validation for the tactical trainer.

Engine-free and best-effort, like `openings.py`: the vendored baseline shard
(`server/data/puzzles/baseline.jsonl.gz`, gzip JSONL) is loaded once and filtered in memory
(a few thousand dicts is trivial; no SQLite). Per-user variety comes from a seeded shuffle of the
candidate pool plus a served-`seen_ids` exclusion, so same-rating users get different,
non-repeating streams from identical static files.

Each puzzle dict (one JSONL line):
    {id, fen, moves:[uci...], rating, rd, themes:[...], popularity, nbplays, game_url}
`moves[0]` is the auto-played setup move (it creates the puzzle); the solver finds `moves[1:]`,
with opponent replies forced at the even indices.
"""

from __future__ import annotations

import functools
import glob
import gzip
import json
import os
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable, Optional

import chess

from .. import config
from . import puzzle_shards
from .agent.models import MemoryQuery
from .learning import memory as learning_memory

_BASELINE = Path(__file__).resolve().parent.parent / "data" / "puzzles" / "baseline.jsonl.gz"

# Selection: start with a tight band around the user's rating and widen until we have enough.
_BAND = 100
_MIN_POOL = 12
# Lichess tags every puzzle with THEME tags, but many are metadata, not trainable skills: the
# puzzle's length ("oneMove"), where it came from ("master"), the game phase, or the resulting eval
# ("crushing"). Surfacing "master 0%" or "oneMove 0%" in the "Work on" card is noise — only real
# tactical/mate motifs are actionable. Everything here is excluded from the weakness card + bias.
_NON_MOTIF_THEMES: frozenset[str] = frozenset({
    "oneMove", "short", "long", "veryLong",          # puzzle length
    "master", "masterVsMaster", "superGM",           # puzzle origin
    "opening", "middlegame", "endgame",              # game phase
    "crushing", "advantage", "equality", "mate",     # resulting eval / generic goal
})


def validate_curated_puzzle(puzzle: Mapping[str, object]) -> dict:
    """Validate and normalize one externally sourced curated puzzle.

    The setup move owns the position presented to the solver, so a supplied
    ``solve_fen`` must match the position obtained by legally replaying that
    move.  The complete solution line is replayed as well; malformed shard
    rows must never reach a session or become durable learning sources.
    """
    if not isinstance(puzzle, Mapping):
        raise ValueError("Curated puzzle must be an object.")

    puzzle_id = puzzle.get("id")
    if not isinstance(puzzle_id, str) or not puzzle_id.strip():
        raise ValueError("Curated puzzle requires a stable id.")

    fen = puzzle.get("fen")
    if not isinstance(fen, str) or not fen.strip():
        raise ValueError("Curated puzzle requires an owning FEN.")
    try:
        board = chess.Board(fen.strip())
    except ValueError as exc:
        raise ValueError("Curated puzzle has an invalid owning FEN.") from exc
    if not board.is_valid():
        raise ValueError("Curated puzzle has an invalid owning FEN.")

    moves = puzzle.get("moves")
    if not isinstance(moves, list) or len(moves) < 2:
        raise ValueError("Curated puzzle requires a setup move and solver line.")
    normalized_moves: list[str] = []
    solve_fen: str | None = None
    side_to_move: str | None = None
    for index, raw_move in enumerate(moves):
        if not isinstance(raw_move, str) or not raw_move.strip():
            raise ValueError("Curated puzzle solution moves must be non-empty UCI strings.")
        try:
            move = chess.Move.from_uci(raw_move.strip())
        except ValueError as exc:
            raise ValueError("Curated puzzle contains an invalid UCI move.") from exc
        if move not in board.legal_moves:
            raise ValueError("Curated puzzle contains a move that is not legal in its owning FEN.")
        board.push(move)
        normalized_moves.append(move.uci())
        if index == 0:
            solve_fen = board.fen()
            side_to_move = "white" if board.turn == chess.WHITE else "black"

    supplied_solve_fen = puzzle.get("solve_fen")
    if supplied_solve_fen is not None:
        if not isinstance(supplied_solve_fen, str) or not supplied_solve_fen.strip():
            raise ValueError("Curated puzzle solve FEN must be a non-empty string.")
        try:
            supplied_board = chess.Board(supplied_solve_fen.strip())
        except ValueError as exc:
            raise ValueError("Curated puzzle has an invalid solve FEN.") from exc
        if not supplied_board.is_valid() or supplied_board.fen() != solve_fen:
            raise ValueError("Curated puzzle solve FEN does not belong to its setup move.")

    themes = puzzle.get("themes")
    if (
        not isinstance(themes, list)
        or not themes
        or any(not isinstance(theme, str) or not theme.strip() for theme in themes)
    ):
        raise ValueError("Curated puzzle requires verified non-empty theme strings.")
    normalized_themes = [theme.strip() for theme in themes]
    if len(set(normalized_themes)) != len(normalized_themes):
        raise ValueError("Curated puzzle themes must be unique.")

    normalized = dict(puzzle)
    normalized.update(
        id=puzzle_id.strip(),
        fen=chess.Board(fen.strip()).fen(),
        moves=normalized_moves,
        themes=normalized_themes,
        solve_fen=solve_fen,
        side_to_move=side_to_move,
    )
    return normalized


def is_trainable_theme(theme: str) -> bool:
    """Is this a real tactical/mate motif a player can drill, vs. a Lichess metadata tag?"""
    return bool(theme) and theme not in _NON_MOTIF_THEMES


def weak_theme_stats(by_theme: dict, *, min_seen: int = 4, max_rate: float = 0.7,
                     limit: int = 3) -> list[dict]:
    """Adapt canonical puzzle-skill weaknesses to the legacy Work On card shape.

    ``by_theme`` and the legacy thresholds remain signature-compatible but no longer promote a
    second weakness system.
    """
    del by_theme, min_seen, max_rate
    if not config.PERSONALIZE_HISTORY:
        return []
    try:
        estimates = learning_memory.retrieve_estimates(
            MemoryQuery(activity="training", window="recent", limit=5),
            personalization_enabled=True,
        )
    except Exception:  # noqa: BLE001 - puzzle stats remain available without learning memory
        return []
    output = []
    for estimate in estimates:
        theme = _SKILL_TO_THEME.get(estimate.skill_id)
        if estimate.status != "weakness" or theme is None:
            continue
        output.append(
            {
                "theme": theme,
                "seen": estimate.evidence_count,
                "solved": estimate.success_count,
                "rate": estimate.success_count / estimate.evidence_count,
            }
        )
    return output[:limit]


_SKILL_TO_THEME: dict[str, str] = {
    "tactics.fork_detection": "fork",
    "tactics.mating_threat_detection": "mateIn2",
    "tactics.loose_piece_awareness": "hangingPiece",
}


def _load_jsonl_gz(path: str) -> list[dict]:
    """Parse and validate a gzip-JSONL puzzle shard; bad rows are never served."""
    out: list[dict] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(validate_curated_puzzle(json.loads(line)))
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return []
    return out


@functools.lru_cache(maxsize=1)
def _baseline() -> list[dict]:
    """All vendored baseline puzzles. Built once, lazily. Missing/corrupt -> [] (degrade)."""
    if not _BASELINE.is_file():
        return []
    return _load_jsonl_gz(str(_BASELINE))


@functools.lru_cache(maxsize=1)
def _downloaded_pool() -> list[dict]:
    """All puzzles from downloaded dense band shards under <DATA_DIR>/puzzles (P3).

    Cached; `puzzle_shards.ensure_band` clears this cache (via `_invalidate_pool`) when a new shard
    lands, so the next selection sees it. Empty when nothing has been downloaded yet."""
    out: list[dict] = []
    try:
        pattern = os.path.join(config._puzzle_dir(), "band_*.jsonl.gz")
        for path in glob.glob(pattern):
            out.extend(_load_jsonl_gz(path))
    except OSError:
        return []
    return out


def _merged_pool() -> list[dict]:
    """Baseline + downloaded shards, de-duplicated by id (a downloaded copy wins). The baseline is
    always the guaranteed floor; downloaded bands just deepen it around the user's rating."""
    dl = _downloaded_pool()
    base = _baseline()
    if not dl:
        return base
    seen: set[str] = set()
    merged: list[dict] = []
    for p in (*dl, *base):
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        merged.append(p)
    return merged


def available_themes() -> list[str]:
    """Sorted distinct theme tags present in the loaded pool (for the frontend theme picker)."""
    seen: set[str] = set()
    for p in _merged_pool():
        for t in p.get("themes", []):
            seen.add(t)
    return sorted(seen)


def weakness_themes(state: dict) -> list[str]:
    """Return Lichess themes for canonical established/emerging weaknesses only."""
    del state
    if not config.PERSONALIZE_HISTORY:
        return []
    items = learning_memory.retrieve_memory(
        MemoryQuery(activity="training", window="recent", limit=5),
        personalization_enabled=True,
    )
    return [
        theme
        for item in items
        if item.status == "weakness"
        if (theme := _SKILL_TO_THEME.get(item.skill_id)) is not None
    ][:4]


def _candidates(rating: float, themes: Optional[Iterable[str]]) -> list[dict]:
    """Puzzles within a rating band around `rating`, widened until the pool is usable."""
    pool = _merged_pool()
    if not pool:
        return []
    want = set(themes) if themes else None
    if want:
        pool = [p for p in pool if want & set(p.get("themes", []))]
    if not pool:
        return []
    # Only serve well-established puzzles that will actually move the Glicko rating. The ~5% with a
    # high RatingDeviation play "unrated", which confuses users ("why didn't my rating change?"), so
    # drop them from selection — falling back to the unfiltered pool only if that empties it.
    rated_pool = [p for p in pool if float(p.get("rd", 999)) < config.PUZZLE_MAX_RD]
    pool = rated_pool or pool
    band = _BAND
    while band <= 1200:
        near = [p for p in pool if abs(float(p.get("rating", 1500)) - rating) <= band]
        if len(near) >= _MIN_POOL:
            return near
        band += _BAND
    return pool  # whole (theme-filtered) pool as a last resort


def next_puzzle(
    rating: float,
    *,
    themes: Optional[Iterable[str]] = None,
    exclude: Optional[set[str]] = None,
    seed: Optional[int] = None,
    difficulty: Optional[str] = None,
    rd: Optional[float] = None,
) -> Optional[dict]:
    """Pick a puzzle near `rating`, theme-filtered + not in `exclude`, via a per-user seeded shuffle.

    `difficulty` of "easier"/"harder" shifts the target rating by one band. `rd` is accepted for
    signature stability but no longer affects selection (the full shard set is downloaded in the
    background regardless). Returns the parsed puzzle (with a derived `side_to_move`) or None when
    nothing is available.
    """
    target = float(rating)
    if difficulty == "easier":
        target -= _BAND
    elif difficulty == "harder":
        target += _BAND

    # Fire-and-forget: pull the whole shard set (~16 MB) in the background on first use so the pool
    # deepens over time. This call serves from whatever is cached now. Best-effort + non-blocking.
    puzzle_shards.ensure_all_bands()

    candidates = _candidates(target, themes)
    exclude = exclude or set()
    fresh = [p for p in candidates if p.get("id") not in exclude]
    pool = fresh or candidates  # everything seen? fall back to the full band rather than nothing
    if not pool:
        return None

    rng = random.Random(seed if seed is not None else 0)
    order = pool[:]
    rng.shuffle(order)
    chosen = order[0]
    return _with_side(chosen)


def get_puzzle(puzzle_id: str) -> Optional[dict]:
    """Look up a loaded puzzle by id (for /move and /explain after selection)."""
    for p in _merged_pool():
        if p.get("id") == puzzle_id:
            return _with_side(p)
    return None


def _with_side(puzzle: dict) -> dict:
    """Return a normalized puzzle whose setup-owned solver position is verified."""
    return validate_curated_puzzle(puzzle)


def validate_step(puzzle: dict, ply_index: int, uci: str) -> dict:
    """Is `uci` the expected solution move at `ply_index` (into `puzzle['moves']`)?

    Replays the forced line up to `ply_index` from the puzzle FEN, so it's stateless. At the
    final solver move, any legal move that delivers checkmate is accepted (mate-leaf tolerance).
    Returns `{correct, is_complete, expected_uci, opponent_reply_uci}`.
    """
    moves = puzzle.get("moves", [])
    if ply_index < 0 or ply_index >= len(moves):
        return {"correct": False, "is_complete": False, "expected_uci": None, "opponent_reply_uci": None}

    board = chess.Board(puzzle["fen"])
    try:
        for m in moves[:ply_index]:
            board.push_uci(m)
    except ValueError:
        return {"correct": False, "is_complete": False, "expected_uci": None, "opponent_reply_uci": None}

    expected = moves[ply_index]
    is_last = ply_index == len(moves) - 1
    correct = uci == expected

    if not correct and is_last:
        # Branch tolerance: at a mate leaf any legal mating move counts.
        try:
            mv = chess.Move.from_uci(uci)
            if mv in board.legal_moves:
                test = board.copy(stack=False)
                test.push(mv)
                if test.is_checkmate():
                    correct = True
        except ValueError:
            pass

    if not correct:
        return {"correct": False, "is_complete": False, "expected_uci": expected, "opponent_reply_uci": None}

    next_index = ply_index + 1
    if next_index >= len(moves):
        return {"correct": True, "is_complete": True, "expected_uci": expected, "opponent_reply_uci": None}
    return {
        "correct": True,
        "is_complete": False,
        "expected_uci": expected,
        "opponent_reply_uci": moves[next_index],
    }


def position_fen(puzzle: dict, ply_index: int) -> str:
    """The FEN the solver moves from at `ply_index` (after replaying the forced prefix).

    Used to engine-ground the moves the user actually tried, so the coach can say concretely why
    each one works or fails. Best-effort -> the puzzle FEN on any error.
    """
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", [])[:ply_index]:
            board.push_uci(m)
        return board.fen()
    except (ValueError, KeyError):
        return puzzle.get("fen", "")


def solution_san(puzzle: dict) -> list[str]:
    """The full solution line (including the setup move) in SAN, for the coach facts."""
    out: list[str] = []
    try:
        board = chess.Board(puzzle["fen"])
        for m in puzzle.get("moves", []):
            mv = chess.Move.from_uci(m)
            out.append(board.san(mv))
            board.push(mv)
    except (ValueError, KeyError):
        return out
    return out

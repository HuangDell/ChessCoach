"""Disk cache of fully-analysed games so reopening a past game is instant.

A full :class:`ReviewSession` (timeline + per-mistake comments + mistakes) is expensive to
compute — a ~20-45s Stockfish sweep — but cheap to store (~tens of KB of JSON). We persist
each analysed session to ``<DATA_DIR>/analysis-cache/<game_id>_<side>_<profile>.json`` keyed by
game identity, reviewed side and the complete analysis profile, so reopening any game already
analysed on this machine — *even in a previous app session* — loads from disk instead of
re-running the engine.

The cache key uses the shared imported-game identity (SHA-256 of the UCI mainline plus a custom
initial FEN), so ``load`` can compute it straight from PGN before analysis.

Everything here is best-effort and engine-free: any failure (corrupt file, schema bump, disk
error) is swallowed and the caller falls back to a fresh sweep. ``CHESS_ANALYSIS_CACHE=0``
disables it; the entry count is bounded (oldest-by-access pruned) so disk use stays in check.
"""
from __future__ import annotations

import io
import json
import os
import re
from typing import Optional

import chess.pgn

from server import config
from server.core import game_identity
from server.core.game_analysis import analysis_profile_for_headers, resolve_player
from server.core.session import ReviewSession

# Bump when the on-disk payload shape (or ReviewSession schema) changes incompatibly, so stale
# files are ignored rather than mis-parsed.
CACHE_VERSION = 4


# --------------------------------------------------------------------------------------
# Paths / keys
# --------------------------------------------------------------------------------------
def _cache_dir() -> str:
    return os.path.join(config.DATA_DIR, "analysis-cache")


def _safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", part or "").strip("_") or "x"


def _path(game_id: str, side: str, profile_id: str) -> str:
    return os.path.join(
        _cache_dir(), f"{_safe(game_id)}_{_safe(side)}_{_safe(profile_id)}.json"
    )


def _game_id(ucis: list[str], initial_fen: str | None = None) -> str:
    return game_identity.game_id_for_moves(ucis, initial_fen)


def _sess_ucis(sess: ReviewSession) -> list[str]:
    """Every move (both sides) of the session, mirroring ``history._full_move_ucis`` so the
    game_id computed here matches the one history records under."""
    ucis = [n["move_uci"] for n in sess.timeline if n.get("move_uci")]
    return ucis or [m.move_uci for m in sess.all_moves]


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
def store(sess: ReviewSession) -> None:
    """Persist a fully-analysed session to disk. Best-effort: never raises."""
    if not config.ANALYSIS_CACHE_ENABLED:
        return
    try:
        ucis = _sess_ucis(sess)
        if not ucis:  # nothing to key on (empty/illegal game)
            return
        initial_fen = game_identity.setup_fen_from_headers(sess.headers)
        profile = sess.engine_analysis.get("profile") or {}
        profile_id = str(profile.get("id") or "")
        if not profile_id:
            return
        path = _path(_game_id(ucis, initial_fen), sess.player, profile_id)
        os.makedirs(_cache_dir(), exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "profile_version": config.ANALYSIS_PROFILE_VERSION,
            "profile_id": profile_id,
            "side": sess.player,
            "sweep_depth": sess.sweep_depth,
            "session": json.loads(sess.model_dump_json()),
        }
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)  # atomic
        _prune()
    except Exception:  # pragma: no cover - caching must never break a review
        pass


def load(pgn: str, player: str = "auto") -> Optional[ReviewSession]:
    """Return a cached session for this PGN+side, or None if not cached / unreadable.

    The reviewed side is resolved the same way the analysis path resolves it (``resolve_player``),
    so ``player="auto"`` finds the entry stored under the auto-detected colour.
    """
    if not config.ANALYSIS_CACHE_ENABLED:
        return None
    try:
        game = chess.pgn.read_game(io.StringIO(pgn or ""))
        if game is None:
            return None
        side = resolve_player(dict(game.headers), player)
        ucis = [m.uci() for m in game.mainline_moves()]
        if not ucis:
            return None
        initial_fen = game.board().fen()
        profile_id = analysis_profile_for_headers(dict(game.headers), side)["id"]
        path = _path(
            _game_id(ucis, game_identity.setup_fen(dict(game.headers), initial_fen)),
            side,
            profile_id,
        )
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("version") != CACHE_VERSION:
            return None
        if payload.get("profile_version") != config.ANALYSIS_PROFILE_VERSION:
            return None
        if payload.get("profile_id") != profile_id:
            return None
        sess = ReviewSession.model_validate(payload["session"])
        # Fresh open: drop any saved navigation state.
        sess.current_index = 0
        sess.explore_fen = None
        # Older cache entries can contain coach_ai_text. The removed legacy summary has no consumer.
        sess.coach_ai_text = None
        os.utime(path, None)  # mark as recently used for LRU pruning
        return sess
    except Exception:  # pragma: no cover - a bad cache file just means "miss"
        return None


def _prune() -> None:
    """Keep at most ``config.ANALYSIS_CACHE_MAX`` entries, dropping least-recently-used first."""
    cap = config.ANALYSIS_CACHE_MAX
    if cap <= 0:
        return
    try:
        entries = [
            os.path.join(_cache_dir(), n)
            for n in os.listdir(_cache_dir())
            if n.endswith(".json")
        ]
        if len(entries) <= cap:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))  # oldest access first
        for p in entries[: len(entries) - cap]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass

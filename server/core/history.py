"""Persistent game history -> personalised coaching.

Each analysed game is turned into one compact JSON record (`build_game_record`) and appended
to `<DATA_DIR>/history/games.jsonl` (`record_game`). Records carry both the raw provenance
(`platform`, `player_name`) and a resolved canonical `player_id`, so one person's several
lichess/chess.com accounts fold into a single coaching profile via `<DATA_DIR>/identities.json`.

The JSONL is atomically upserted by `(game_id, reviewed_side)`; readers also dedupe legacy files
that may still contain repeated rows. From those records we aggregate a small,
prompt-ready `profile` (`build_profile`) cached at `<DATA_DIR>/profiles/<player_id>.json`.

Everything here is engine-free and deterministic: motif tags (`tag_motifs`) and phase
detection (`_phase`) are cheap static heuristics over the FENs/moves we already computed,
so history is essentially free to record and trivial to backfill when the heuristics improve.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import chess

from server import config
from server.core.agent.models import MemoryQuery
from server.core import game_identity
from server.core import openings
from server.core import session as session_mod
from server.core.evaluation import classify_speed
from server.core.learning.estimates import EstimateStore, rank_estimates
from server.core.learning import memory as learning_memory
from server.core.learning import taxonomy as learning_taxonomy
from server.core.session import ReviewSession

SCHEMA_VERSION = 1
_HISTORY_LOCK = threading.RLock()

# Static piece values for the "is this piece hanging" heuristic (king effectively infinite).
_PIECE_VALUE = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 100,
}


class GameDeletionError(RuntimeError):
    """A game could not be deleted without risking inconsistent persistent data."""


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
def _data_dir(data_dir: Optional[str]) -> str:
    return data_dir if data_dir is not None else config.DATA_DIR


def _history_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "history", "games.jsonl")


def _identities_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "identities.json")


def _profile_path(player_id: str, data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "profiles", f"{_safe(player_id)}.json")


def _attempts_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(_data_dir(data_dir), "history", "attempts.jsonl")


def _safe(name: str) -> str:
    """Filesystem-safe slug for a player_id."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "unknown").strip("_") or "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fsync_directory(path: str) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: str, content: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_jsonl(path: str, records: list[dict]) -> None:
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
    ).encode("utf-8")
    _atomic_write_bytes(path, content)


def _snapshot_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _restore_file_snapshot(path: str, content: bytes | None) -> None:
    """Restore bytes without going through a possibly failing normal transaction writer."""

    directory = os.path.dirname(path)
    if content is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        _fsync_directory(directory)
        return

    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.rollback.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _profile_snapshots(profile_dir: str) -> tuple[bool, dict[str, bytes]]:
    existed = os.path.isdir(profile_dir)
    if not existed:
        return False, {}
    snapshots: dict[str, bytes] = {}
    for name in os.listdir(profile_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(profile_dir, name)
        if os.path.isfile(path):
            content = _snapshot_file(path)
            if content is not None:
                snapshots[path] = content
    return True, snapshots


def _restore_deletion_snapshots(
    file_snapshots: dict[str, bytes | None],
    *,
    profile_dir: str,
    profile_dir_existed: bool,
    profiles: dict[str, bytes],
) -> None:
    try:
        current_profiles = {
            os.path.join(profile_dir, name)
            for name in os.listdir(profile_dir)
            if name.endswith(".json") and os.path.isfile(os.path.join(profile_dir, name))
        }
    except FileNotFoundError:
        current_profiles = set()
    for path in sorted(current_profiles - set(profiles)):
        _restore_file_snapshot(path, None)
    for path, content in sorted(profiles.items()):
        _restore_file_snapshot(path, content)
    for path, content in file_snapshots.items():
        _restore_file_snapshot(path, content)
    if not profile_dir_existed:
        try:
            os.rmdir(profile_dir)
        except (FileNotFoundError, OSError):
            pass


def _stage_game_artifact(directory: str, base: str) -> str | None:
    """Atomically hide an artifact; irreversible cleanup happens only after commit."""

    if not os.path.isdir(directory):
        return None
    staging_root = os.path.join(base, ".deleted-games")
    os.makedirs(staging_root, exist_ok=True)
    descriptor, staged_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(directory)}.", suffix=".deleting", dir=staging_root
    )
    os.close(descriptor)
    os.unlink(staged_path)
    try:
        os.replace(directory, staged_path)
        _fsync_directory(os.path.dirname(directory))
        _fsync_directory(staging_root)
    except OSError:
        if os.path.isdir(staged_path) and not os.path.exists(directory):
            os.replace(staged_path, directory)
        raise
    return staged_path


def _read_attempt_rows_for_deletion(path: str) -> list[dict] | None:
    """Strictly preflight an attempt log before any owning artifact is removed."""

    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows: list[dict] = []
            for line_number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise GameDeletionError(
                        f"Could not safely delete game: invalid attempt data in {path} "
                        f"at line {line_number}."
                    ) from exc
                if not isinstance(row, dict):
                    raise GameDeletionError(
                        f"Could not safely delete game: invalid attempt row in {path} "
                        f"at line {line_number}."
                    )
                rows.append(row)
            return rows
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GameDeletionError(f"Could not read attempt data before deleting game: {exc}") from exc


# --------------------------------------------------------------------------------------
# Identity resolution
# --------------------------------------------------------------------------------------
def _norm_platform(raw: str) -> str:
    """Normalise any platform spelling (Site URL, 'lichess.org', 'chesscom', ...) to a token."""
    s = (raw or "").lower()
    if "lichess" in s:
        return "lichess"
    if "chess.com" in s or "chesscom" in s:
        return "chesscom"
    return s.strip() or "unknown"


def _platform_from_headers(headers: dict) -> str:
    blob = " ".join(headers.get(k, "") for k in ("Site", "Link", "Event"))
    return _norm_platform(blob)


def load_identities(data_dir: Optional[str] = None) -> dict:
    """Read the alias map; missing/garbled file -> {} (history still works, just unmapped)."""
    path = _identities_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_identity(
    headers: dict, reviewed_side: str, data_dir: Optional[str] = None
) -> tuple[str, str, str]:
    """Resolve (player_id, platform, player_name) for the reviewed side.

    `player_id` is the canonical id from identities.json if an alias matches; otherwise it
    falls back to the raw handle (so unmapped accounts are still recorded, never merged by
    accident). An alias may omit `platform` to match a handle across every platform.
    """
    name = headers.get("White" if reviewed_side == "white" else "Black", "").strip()
    platform = _platform_from_headers(headers)
    name_lc = name.lower()

    # 1. Explicit identities.json (most specific; supports multiple people).
    for pid, info in load_identities(data_dir).items():
        for alias in (info or {}).get("aliases", []):
            a_name = str(alias.get("name", "")).strip().lower()
            if not a_name or a_name != name_lc:
                continue
            a_plat = alias.get("platform")
            if a_plat is None or _norm_platform(str(a_plat)) == platform:
                return pid, platform, name

    # 2. CHESS_USERNAME + CHESS_ALIASES from the environment: every listed
    #    handle folds into CHESS_USERNAME as the canonical player_id.
    if name_lc and config.USERNAME:
        if name_lc == config.USERNAME.lower():
            return config.USERNAME, platform, name
        for a_plat, a_name in config.USERNAME_ALIASES:
            if a_name == name_lc and (a_plat is None or _norm_platform(a_plat) == platform):
                return config.USERNAME, platform, name

    # 3. Unmapped: key by the raw handle so the game is still recorded (never merged blindly).
    fallback = name_lc or (config.USERNAME or "").lower() or "me"
    return fallback, platform, name


def _display_name(player_id: str, data_dir: Optional[str] = None) -> str:
    info = load_identities(data_dir).get(player_id) or {}
    return info.get("display_name") or player_id


def _resolves_to_me(handle: str, platform: Optional[str], data_dir: Optional[str]) -> bool:
    """True if `handle` already resolves to the canonical "me" (env or identities.json)."""
    handle_lc = (handle or "").strip().lower()
    if not handle_lc:
        return False
    if config.USERNAME and handle_lc == config.USERNAME.lower():
        return True
    plat = _norm_platform(platform) if platform else None
    for a_plat, a_name in config.USERNAME_ALIASES:
        if a_name == handle_lc and (a_plat is None or _norm_platform(a_plat) == plat):
            return True
    me = my_player_id(data_dir)
    for alias in (load_identities(data_dir).get(me) or {}).get("aliases", []):
        if str(alias.get("name", "")).strip().lower() == handle_lc:
            a_plat = alias.get("platform")
            if a_plat is None or plat is None or _norm_platform(str(a_plat)) == plat:
                return True
    return False


def ensure_self_alias(
    handle: str, platform: Optional[str] = None, data_dir: Optional[str] = None
) -> str:
    """Fold `handle` into the canonical "me" so uploaded games show up in "My games".

    When you upload your own (e.g. Chess.com) games, your handle there may differ from
    CHESS_USERNAME. This persists `handle` as an alias of `my_player_id()` in identities.json so
    `resolve_identity` maps those games — and any future ones from that account — to you. Idempotent
    and best-effort; returns the canonical player_id the handle now resolves to.
    """
    canonical = my_player_id(data_dir)
    handle = (handle or "").strip()
    if not handle or _resolves_to_me(handle, platform, data_dir):
        return canonical

    ids = load_identities(data_dir)
    entry = ids.get(canonical) or {}
    aliases = list(entry.get("aliases") or [])
    aliases.append({"name": handle, "platform": _norm_platform(platform) if platform else None})
    entry["aliases"] = aliases
    entry.setdefault("display_name", canonical)
    ids[canonical] = entry

    path = _identities_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ids, fh, ensure_ascii=False, indent=2)
    return canonical


# --------------------------------------------------------------------------------------
# Motif tagging + phase (cheap static heuristics, no engine)
# --------------------------------------------------------------------------------------
# Human-readable labels for the coaching profile / chat injection.
_MOTIF_LABELS = {
    "hung_piece": "hanging pieces (leaving a piece en prise)",
    "pawn_grab": "greedy pawn-grabbing",
    "missed_capture": "missing free material",
    "missed_fork": "missing forks",
    "allowed_fork": "walking into forks",
    "allowed_mate": "allowing forced mate",
    "back_rank": "back-rank weaknesses",
    "missed_mate": "missing forced mates",
    "time_trouble": "blundering in time pressure (low clock)",
}


def _time_control_base(time_control: str) -> Optional[float]:
    """Base seconds from a PGN TimeControl ("600+0", "300+5", "600"); None if unknown/correspondence."""
    tc = (time_control or "").strip()
    if not tc or tc in ("-", "?"):
        return None
    head = tc.split("+", 1)[0]
    if "/" in head:  # correspondence ("1/259200" = days), not a sudden-death clock
        return None
    try:
        base = float(head)
    except ValueError:
        return None
    return base if base > 0 else None


def time_motifs(
    clock_after: Optional[float], opp_clock: Optional[float], base: Optional[float]
) -> list[str]:
    """`time_trouble` when the move was made on a low clock, or far behind the opponent.

    Needs PGN [%clk] data; returns [] when clocks are absent (graceful on PGNs without timing).
    """
    if clock_after is None:
        return []
    low_absolute = clock_after <= 30 or (base is not None and clock_after <= 0.10 * base)
    much_less_than_opp = (
        opp_clock is not None
        and opp_clock > 0
        and clock_after <= 0.5 * opp_clock
        and clock_after <= (0.20 * base if base else 60)
    )
    return ["time_trouble"] if (low_absolute or much_less_than_opp) else []


def _val(piece: Optional[chess.Piece]) -> int:
    return _PIECE_VALUE.get(piece.piece_type, 0) if piece else 0


def _is_hanging(board: chess.Board, square: int) -> bool:
    """Static SEE-lite: is the piece on `square` left en prise (undefended, or won by a
    cheaper attacker)? `board` is the position with that piece already on the board."""
    piece = board.piece_at(square)
    if piece is None:
        return False
    attackers = board.attackers(not piece.color, square)
    if not attackers:
        return False
    defenders = board.attackers(piece.color, square)
    cheapest = min(_val(board.piece_at(sq)) for sq in attackers)
    return (not defenders) or cheapest < _val(piece)


def _is_fork(board: chess.Board, move: chess.Move) -> bool:
    """Does `move` (by board.turn) land a piece that forks >= 2 valuable enemy targets?

    A "valuable" target is the enemy king (check) or a piece worth at least as much as the
    forking piece. We require either a check or an undefended target (so it actually wins),
    and that the forking piece isn't itself simply hanging to a cheaper piece.
    """
    forker = board.turn
    b = board.copy(stack=False)
    b.push(move)
    pf = b.piece_at(move.to_square)
    if pf is None or pf.color != forker:
        return False
    a_val = _val(pf)
    targets = [
        (sq, b.piece_at(sq))
        for sq in b.attacks(move.to_square)
        if b.piece_at(sq)
        and b.piece_at(sq).color != forker
        and (b.piece_at(sq).piece_type == chess.KING or _val(b.piece_at(sq)) >= a_val)
    ]
    if len(targets) < 2:
        return False
    gives_check = any(p.piece_type == chess.KING for _, p in targets)
    undefended = any(
        p.piece_type != chess.KING and not b.attackers(not forker, sq)
        for sq, p in targets
    )
    if not (gives_check or undefended):
        return False
    # The forking piece must not just hang for free (then the opponent escapes by taking it).
    enemy = b.attackers(not forker, move.to_square)
    if enemy:
        own = b.attackers(forker, move.to_square)
        if not own and min(_val(b.piece_at(s)) for s in enemy) < a_val:
            return False
    return True


def _allowed_opponent_fork(board: chess.Board) -> bool:
    """In the position after our move (board.turn = opponent), can the opponent fork us?"""
    return any(_is_fork(board, mv) for mv in board.legal_moves)


def _allowed_mate_in_1(board: chess.Board) -> Optional[chess.Move]:
    """The opponent's mate-in-1 in this position, if any (board.turn = opponent)."""
    for mv in board.legal_moves:
        board.push(mv)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return mv
    return None


def _is_back_rank_mate(board: chess.Board, mate_move: chess.Move, victim: chess.Color) -> bool:
    """Is `mate_move` a rook/queen mate delivered on `victim`'s back rank?"""
    piece = board.piece_at(mate_move.from_square)
    if piece is None or piece.piece_type not in (chess.ROOK, chess.QUEEN):
        return False
    back = 0 if victim == chess.WHITE else 7
    return chess.square_rank(mate_move.to_square) == back


def _back_rank_weak(board: chess.Board, color: chess.Color) -> bool:
    """Structural back-rank weakness for `color`: king boxed on its back rank (no luft) while
    the opponent has a rook/queen on a file with no friendly pawn (i.e. it can reach the rank)."""
    king_sq = board.king(color)
    if king_sq is None:
        return False
    back = 0 if color == chess.WHITE else 7
    if chess.square_rank(king_sq) != back:
        return False
    forward = back + (1 if color == chess.WHITE else -1)
    king_file = chess.square_file(king_sq)
    # No luft: every square in front of the king is occupied by one of the king's own pieces.
    for df in (-1, 0, 1):
        f = king_file + df
        if 0 <= f <= 7:
            occ = board.piece_at(chess.square(f, forward))
            if occ is None or occ.color != color:
                return False  # an escape square exists
    opp = not color
    for sq, piece in board.piece_map().items():
        if piece.color == opp and piece.piece_type in (chess.ROOK, chess.QUEEN):
            f = chess.square_file(sq)
            file_pawns = any(
                board.piece_at(chess.square(f, r)) == chess.Piece(chess.PAWN, color)
                for r in range(8)
            )
            if not file_pawns:
                return True
    return False


def tag_motifs(
    fen_before: str,
    move_uci: str,
    best_uci: Optional[str],
    win_swing: float,
    eval_before: float,
) -> list[str]:
    """Best-effort motif tags for one flagged move, from data we already have (no engine).

    Tags fall into three buckets: what we did wrong with our move (`pawn_grab`, `hung_piece`),
    what we missed (`missed_capture`, `missed_fork`, `missed_mate`), and what we let the
    opponent do (`allowed_fork`, `allowed_mate`, `back_rank`). All are static (<= 2 ply of
    pure python-chess) and deterministic. Conservative on purpose — they run only on
    already-flagged mistakes, so a true-positive bias is fine. The schema reserves `motifs`
    for exactly this, so records can be re-tagged offline with no re-analysis.
    """
    motifs: list[str] = []
    try:
        board = chess.Board(fen_before)
        move = chess.Move.from_uci(move_uci)
    except (ValueError, AssertionError):
        return motifs
    if move not in board.legal_moves:
        return motifs

    mover = board.turn

    # --- what we did with our move ---
    if board.is_capture(move):
        if board.is_en_passant(move) or _val(board.piece_at(move.to_square)) == 1:
            motifs.append("pawn_grab")

    # --- what we missed (the engine's best move) ---
    if best_uci:
        try:
            best = chess.Move.from_uci(best_uci)
        except (ValueError, AssertionError):
            best = None
        if best is not None and best != move and best in board.legal_moves:
            if board.is_capture(best) and not board.is_en_passant(best):
                if _val(board.piece_at(best.to_square)) >= 3:
                    motifs.append("missed_capture")
            if _is_fork(board, best):
                motifs.append("missed_fork")

    # --- the position after our move (opponent to move) ---
    after = board.copy(stack=False)
    after.push(move)

    if _is_hanging(after, move.to_square):
        motifs.append("hung_piece")

    if not after.is_game_over():
        if _allowed_opponent_fork(after):
            motifs.append("allowed_fork")
        mate_move = _allowed_mate_in_1(after)
        if mate_move is not None:
            motifs.append("allowed_mate")
            if _is_back_rank_mate(after, mate_move, mover):
                motifs.append("back_rank")
        if "back_rank" not in motifs and _back_rank_weak(after, mover):
            motifs.append("back_rank")

    # missed_mate: a forced mate was available for the mover and we didn't play it.
    if eval_before >= config.MATE_SCORE_CP - 1000:
        motifs.append("missed_mate")

    return motifs


def _view_summary(agg: dict) -> str:
    """One-line summary of an aggregate view (accuracy, top motifs, weakest phase)."""
    bits = []
    if agg.get("avg_accuracy") is not None:
        r = agg.get("results", {})
        bits.append(
            f"accuracy {agg['avg_accuracy']}% "
            f"({r.get('win', 0)}W-{r.get('loss', 0)}L-{r.get('draw', 0)}D)"
        )
    weaknesses = agg.get("weaknesses", [])
    if weaknesses:
        named = ", ".join(
            f"{str(item['category']).replace('_', ' ')} (x{item['count']})"
            for item in weaknesses[:3]
        )
        bits.append(f"established weaknesses: {named}")
    if agg.get("weakest_phase"):
        bits.append(f"weakest phase {agg['weakest_phase']}")
    return "; ".join(bits)


def format_profile_for_prompt(profile: dict) -> Optional[str]:
    """Render the hybrid profile as a compact coaching block for the chat prompt (None if empty)."""
    recent = profile.get("recent") or {}
    if not recent.get("games"):
        return None
    out = [
        "The user's play profile — use it to personalise advice and point out recurring patterns "
        "when relevant (don't force it if it doesn't apply):"
    ]
    window = recent.get("window")
    scope = f"last {window} games" if window else f"all {recent['games']} games"
    out.append(f"- Recent form ({scope}): {_view_summary(recent)}.")

    # Per-mode breakdown — only worth stating when the player has played more than one mode,
    # so coaching can weigh the CURRENT game's mode and note where patterns diverge by speed.
    by_speed = [s for s in recent.get("by_speed", []) if s.get("speed") != "unknown"]
    if len(by_speed) >= 2:
        parts = []
        for s in by_speed:
            acc = f"{s['avg_accuracy']}% acc" if s.get("avg_accuracy") is not None else "acc n/a"
            parts.append(f"{s['speed']} ×{s['games']} ({acc}, {s['blunders_per_game']} blunders/game)")
        out.append(
            "- By mode: " + "; ".join(parts) + ". Mistake tolerance differs by mode — judge the "
            "current game against its own mode (faster time controls warrant more lenient "
            "expectations)."
        )

    lifetime = profile.get("lifetime") or {}
    # Only show lifetime if it covers a different (larger) set than the recent window.
    if lifetime.get("games") and lifetime["games"] != recent["games"]:
        out.append(f"- Lifetime ({lifetime['games']} games): {_view_summary(lifetime)}.")
        ra, la = recent.get("avg_accuracy"), lifetime.get("avg_accuracy")
        if ra is not None and la is not None and abs(ra - la) >= 2:
            trend = "improving" if ra > la else "slipping"
            out.append(
                f"- Trend: {trend} — recent accuracy {ra}% vs lifetime {la}%. "
                "Weight the recent form more heavily."
            )
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# End-of-game coaching blurb
# --------------------------------------------------------------------------------------
def _grade_phrase(acc: float) -> str:
    if acc >= 90:
        return "Excellent game"
    if acc >= 80:
        return "Solid game"
    if acc >= 70:
        return "A mixed game"
    return "A rough game"


def _tally_phrase(blunders: int, mistakes: int, inaccuracies: int) -> str:
    """e.g. '1 blunder, 3 mistakes and 2 inaccuracies' — zeros dropped, plurals handled."""
    parts = []
    for n, noun in ((blunders, "blunder"), (mistakes, "mistake"), (inaccuracies, "inaccuracy")):
        if n:
            word = noun if n == 1 else (noun[:-1] + "ies" if noun.endswith("y") else noun + "s")
            parts.append(f"{n} {word}")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _dominant_motif(mistakes: list) -> Optional[str]:
    """The most common motif across this game's flagged moves (engine-free; reuses tag_motifs)."""
    counts: Counter = Counter()
    for m in mistakes:
        best_uci = m.best_line_uci[0] if m.best_line_uci else None
        for motif in tag_motifs(m.fen_before, m.move_uci, best_uci, m.win_swing, m.eval_before):
            counts[motif] += 1
    return counts.most_common(1)[0][0] if counts else None


def _is_recurring(motif: str, data_dir: Optional[str]) -> bool:
    """True only when canonical recent memory promotes the motif to weakness."""

    aliases = {
        "missed_fork": "fork",
        "allowed_fork": "fork",
        "hung_piece": "hanging_piece",
        "back_rank": "allowed_mate",
    }
    focus = aliases.get(motif, motif)
    items = learning_memory.retrieve_memory(
        MemoryQuery(activity="game_review", focus_category=focus, window="recent", limit=1),
        data_dir=_data_dir(data_dir),
        personalization_enabled=config.PERSONALIZE_HISTORY,
    )
    return bool(items and items[0].status == "weakness")


def coach_summary(sess: ReviewSession, data_dir: Optional[str] = None) -> Optional[str]:
    """A short, engine-free end-of-game coaching blurb grounded in this game's flagged moves.

    Same philosophy as MoveReview.comment: templated and deterministic, with no Engine or model
    call, so it's free and always available. Three beats — overall accuracy/tally, the single
    costliest moment (with the better move), and the recurring thread to watch (tied to the
    player's profile when the same theme shows up across games). Returns None for a clean game.
    """
    side = "White" if sess.player == "white" else "Black"
    acc = sess.accuracy_white if sess.player == "white" else sess.accuracy_black
    mistakes = sess.mistakes
    if not mistakes:
        return f"Clean game — {acc}% accuracy as {side}, no inaccuracies, mistakes or blunders flagged."

    counts = Counter(m.classification for m in mistakes)
    tally = _tally_phrase(
        counts.get("blunder", 0), counts.get("mistake", 0), counts.get("inaccuracy", 0)
    )
    parts = [f"{_grade_phrase(acc)} — {acc}% accuracy as {side}, with {tally}."]

    worst = max(mistakes, key=lambda m: m.win_swing)
    num = f"{worst.move_number}{'.' if worst.color == 'white' else '...'}"
    better = f" {worst.best_move_san} was stronger." if worst.best_move_san else ""
    parts.append(
        f"Your costliest moment was {num}{worst.move_san} "
        f"({worst.classification}, −{round(worst.win_swing)}%).{better}"
    )

    motif = _dominant_motif(mistakes)
    if motif:
        label = _MOTIF_LABELS.get(motif, motif)
        tie = " — also a recurring theme across your recent games" if _is_recurring(motif, data_dir) else ""
        parts.append(f"The thread to watch: {label}{tie}.")

    return " ".join(parts)


def _phase(fen: str, move_number: int) -> str:
    """opening / middlegame / endgame from material + move number (heuristic)."""
    try:
        board = chess.Board(fen)
    except (ValueError, AssertionError):
        return "middlegame"
    pieces = [p for p in board.piece_map().values() if p.piece_type not in (chess.KING, chess.PAWN)]
    queens = sum(1 for p in pieces if p.piece_type == chess.QUEEN)
    if len(pieces) <= 6 or (queens == 0 and len(pieces) <= 8):
        return "endgame"
    if move_number <= 12:
        return "opening"
    return "middlegame"


# --------------------------------------------------------------------------------------
# Record building
# --------------------------------------------------------------------------------------
def _int_or_none(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _clean_date(headers: dict) -> Optional[str]:
    raw = (headers.get("UTCDate") or headers.get("Date") or "").strip()
    if not raw or "?" in raw:
        return None
    return raw.replace(".", "-")


def _player_result(result: str, side: str) -> Optional[str]:
    if result == "1-0":
        return "win" if side == "white" else "loss"
    if result == "0-1":
        return "win" if side == "black" else "loss"
    if result == "1/2-1/2":
        return "draw"
    return None


def game_url_from_headers(headers: dict) -> Optional[str]:
    """The original game's URL (Lichess/Chess.com) from the PGN's Site/Link header, if any."""
    for key in ("Site", "Link"):
        val = headers.get(key, "").strip()
        if val.startswith("http"):
            return val
    return None


# Back-compat alias (kept: used across this module).
_game_url = game_url_from_headers


def _full_move_ucis(sess: ReviewSession) -> list[str]:
    ucis = [n["move_uci"] for n in sess.timeline if n.get("move_uci")]
    if ucis:
        return ucis
    return [m.move_uci for m in sess.all_moves]  # fallback (reviewed side only)


def _game_id(sess: ReviewSession) -> str:
    initial_fen = game_identity.setup_fen_from_headers(sess.headers)
    return game_identity.game_id_for_moves(_full_move_ucis(sess), initial_fen)


def build_game_record(sess: ReviewSession, data_dir: Optional[str] = None) -> dict:
    """Turn a ReviewSession into one JSONL-ready coaching record."""
    headers = sess.headers
    side = sess.player
    player_id, platform, player_name = resolve_identity(headers, side, data_dir)
    game_id = _game_id(sess)

    base = _time_control_base(headers.get("TimeControl", ""))
    counts: Counter = Counter()
    phase_loss = {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0}
    mistakes = []
    for m in sess.mistakes:
        best_uci = m.best_line_uci[0] if m.best_line_uci else None
        phase = _phase(m.fen_before, m.move_number)
        counts[m.classification] += 1
        phase_loss[phase] = phase_loss.get(phase, 0.0) + m.win_swing
        motifs = tag_motifs(m.fen_before, m.move_uci, best_uci, m.win_swing, m.eval_before)
        motifs += time_motifs(m.clock_after, m.opp_clock, base)
        mistakes.append(
            {
                "ply": m.ply,
                "move_number": m.move_number,
                "color": m.color,
                "san": m.move_san,
                "uci": m.move_uci,
                "best_san": m.best_move_san,
                "best_uci": best_uci,
                "classification": m.classification,
                "win_before": round(m.win_before, 1),
                "win_after": round(m.win_after, 1),
                "win_drop": round(m.win_swing, 1),
                "phase": phase,
                "fen_before": m.fen_before,
                "clock_after": m.clock_after,
                "opp_clock": m.opp_clock,
                "motifs": motifs,
            }
        )

    # Stage 2 facts are the Phase 8 coaching vocabulary. The index keeps only compact links; FENs,
    # legal lines and detailed facts remain in the per-game analysis artifact.
    categories: Counter = Counter()
    category_loss: Counter = Counter()
    critical_summaries: list[dict] = []
    move_by_ply = {int(m.ply): m for m in sess.mistakes}
    for position in (sess.engine_analysis or {}).get("critical_positions", []) or []:
        facts = position.get("facts") or {}
        category = str(facts.get("primary_category") or "uncategorized")
        phase = str(
            (((facts.get("snapshots") or {}).get("before") or {}).get("phase") or {}).get(
                "name", "middlegame"
            )
        )
        loss = round(float(position.get("win_loss") or 0.0), 1)
        categories[category] += 1
        category_loss[category] += loss
        move = move_by_ply.get(int(position.get("ply") or 0))
        critical_summaries.append(
            {
                "critical_id": position.get("critical_id"),
                "ply": position.get("ply"),
                "classification": position.get("classification"),
                "category": category,
                "phase": phase,
                "win_loss": loss,
                "seconds_spent": move.seconds_spent if move else None,
            }
        )

    plies = max(len(sess.timeline) - 1, 0) or len(sess.all_moves)
    accuracy = sess.accuracy_white if side == "white" else sess.accuracy_black

    # Opening/ECO: trust the PGN headers (Lichess ships them) but fall back to a local lookup
    # when absent (Chess.com bulk exports often omit them) so those games still get named.
    # Engine-free; reuses the timeline FENs we already have.
    eco = headers.get("ECO") or None
    opening = headers.get("Opening") or None
    if not opening:
        fens = [n.get("fen") for n in sess.timeline if n.get("fen")]
        eco2, name2 = openings.classify_from_fens(fens)
        eco = eco or eco2
        opening = name2

    metadata = {}
    try:
        metadata_path = os.path.join(_data_dir(data_dir), "games", game_id, "metadata.json")
        with open(metadata_path, "r", encoding="utf-8") as fh:
            metadata = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "schema_version": SCHEMA_VERSION,
        "game_id": game_id,
        "reviewed_side": side,
        "analyzed_at": _now_iso(),
        "player_id": player_id,
        "platform": platform,
        "source": metadata.get("source_type") or platform,
        "source_url": metadata.get("source_url") or _game_url(headers),
        "player_name": player_name,
        "date": _clean_date(headers),
        "white": headers.get("White", "?"),
        "black": headers.get("Black", "?"),
        "result": sess.result,
        "player_result": _player_result(sess.result, side),
        "eco": eco,
        "opening": opening,
        "time_control": headers.get("TimeControl") or None,
        "speed": classify_speed(headers.get("TimeControl"), headers.get("Event")),
        "player_elo": _int_or_none(headers.get("WhiteElo" if side == "white" else "BlackElo", "")),
        "opponent_elo": _int_or_none(headers.get("BlackElo" if side == "white" else "WhiteElo", "")),
        "game_url": metadata.get("source_url") or _game_url(headers),
        "sweep_depth": sess.sweep_depth,
        "review_elo": sess.review_elo,
        "thresholds": sess.thresholds,
        "ply_count": plies,
        "accuracy": round(accuracy, 1),
        "counts": {k: counts.get(k, 0) for k in ("inaccuracy", "mistake", "blunder")},
        "mistake_counts": {k: counts.get(k, 0) for k in ("inaccuracy", "mistake", "blunder")},
        "critical_count": len(critical_summaries),
        "categories": dict(categories),
        "category_loss": {key: round(value, 1) for key, value in category_loss.items()},
        "critical_positions": critical_summaries,
        "artifact_version": {
            "analysis_schema": (sess.engine_analysis or {}).get("schema_version"),
            "analysis_profile": ((sess.engine_analysis or {}).get("profile") or {}).get("id"),
        },
        "phase_loss": {k: round(v, 1) for k, v in phase_loss.items()},
        "mistakes": mistakes if not critical_summaries else [],
    }


# --------------------------------------------------------------------------------------
# Storage (atomic JSONL upsert; legacy readers still dedupe)
# --------------------------------------------------------------------------------------
def append_record(record: dict, data_dir: Optional[str] = None) -> None:
    """Atomically insert or replace one game/side record."""
    path = _history_path(data_dir)
    key = (record.get("game_id"), record.get("reviewed_side"))
    with _HISTORY_LOCK:
        records = load_records(data_dir=data_dir)
        records = [r for r in records if (r.get("game_id"), r.get("reviewed_side")) != key]
        records.append(record)
        _atomic_jsonl(path, records)


def load_records(
    player_id: Optional[str] = None, data_dir: Optional[str] = None
) -> list[dict]:
    """All games, deduped to the latest record per (game_id, reviewed_side).

    Optionally filtered to one `player_id`. Bad/blank lines are skipped, not fatal.
    """
    path = _history_path(data_dir)
    latest: dict[tuple, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (rec.get("game_id"), rec.get("reviewed_side"))
                prev = latest.get(key)
                if prev is None or rec.get("analyzed_at", "") >= prev.get("analyzed_at", ""):
                    latest[key] = rec
    except FileNotFoundError:
        return []
    records = list(latest.values())
    if player_id is not None:
        records = [r for r in records if r.get("player_id") == player_id]
    return records


def list_players(data_dir: Optional[str] = None) -> list[str]:
    return sorted({r.get("player_id") for r in load_records(data_dir=data_dir) if r.get("player_id")})


def my_player_id(data_dir: Optional[str] = None) -> str:
    """Canonical player_id for the configured user (CHESS_USERNAME, remapped via identities.json).

    The env path in `resolve_identity` folds CHESS_USERNAME + aliases onto config.USERNAME, so that
    is the join key for "my games". If identities.json maps that handle to another canonical id,
    honour it. Lowercased to match how handles are stored. Used to filter the web history list.
    """
    handle = (config.USERNAME or "").strip()
    handle_lc = handle.lower()
    for pid, info in load_identities(data_dir).items():
        for alias in (info or {}).get("aliases", []):
            if str(alias.get("name", "")).strip().lower() == handle_lc:
                return pid
    return handle_lc or "me"


def history_rows(player_id: Optional[str] = None, data_dir: Optional[str] = None) -> list[dict]:
    """Compact, newest-first list of analysed games for the web history panel.

    Filtered to `player_id` when given (the panel passes `my_player_id()` for "just my games").
    Each row reuses fields already on the record — no recompute — plus `has_pgn` so the frontend
    knows whether the game can be reopened (records written before PGNs were stored can't be).
    """
    records = sorted(
        load_records(player_id=player_id, data_dir=data_dir),
        key=lambda r: r.get("analyzed_at", ""),
        reverse=True,
    )
    # "Is this me?" is computed at READ time against the CURRENT identity config, not from the
    # record's frozen player_id — so a game keyed by a raw handle (e.g. a chess.com game recorded
    # before that handle was folded into "me") still tints once the handle is added in Settings.
    me = my_player_id(data_dir)
    rows = []
    for r in records:
        pgn = r.get("pgn")
        if not pgn and r.get("game_id"):
            try:
                with open(
                    os.path.join(_data_dir(data_dir), "games", str(r["game_id"]), "source.pgn"),
                    "r",
                    encoding="utf-8",
                ) as fh:
                    pgn = fh.read()
            except OSError:
                pgn = None
        rows.append(
            {
                "game_id": r.get("game_id"),
                "player_id": r.get("player_id"),  # lets the panel tint games that are "you"
                "is_me": (
                    r.get("player_id") == me
                    or _resolves_to_me(
                        r.get("player_name") or r.get("player_id") or "",
                        r.get("platform"),
                        data_dir,
                    )
                ),
                "reviewed_side": r.get("reviewed_side"),
                "white": r.get("white"),
                "black": r.get("black"),
                "player_result": r.get("player_result"),
                "accuracy": r.get("accuracy"),
                "speed": r.get("speed") or "unknown",
                "opening": r.get("opening") or r.get("eco"),
                "date": r.get("date"),
                "counts": r.get("counts") or {},
                "game_url": r.get("game_url"),
                "has_pgn": bool(pgn),
                "pgn": pgn,
            }
        )
    return rows


# --------------------------------------------------------------------------------------
# Insights (time-windowed aggregate for the board's Insights panel)
# --------------------------------------------------------------------------------------
def _record_day(r: dict) -> str:
    """The day a game belongs to, as YYYY-MM-DD: when it was PLAYED (the PGN date) if known,
    else when it was analysed. Lexicographic compare works for cutoffs."""
    d = (r.get("date") or "").strip()
    return d if d else (r.get("analyzed_at") or "")[:10]


def load_attempt_records(data_dir: Optional[str] = None) -> list[dict]:
    """Load unified Retry/personal-puzzle history, tolerating old paths and damaged lines."""
    paths = [
        _attempts_path(data_dir),
        os.path.join(_data_dir(data_dir), "training", "attempts.jsonl"),
    ]
    found: dict[str, dict] = {}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        attempt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(attempt, dict):
                        continue
                    key = str(attempt.get("attempt_id") or f"{path}:{len(found)}")
                    found[key] = attempt
        except OSError:
            continue
    return sorted(found.values(), key=lambda item: item.get("attempted_at", ""))


def _attempt_summary(attempts: list[dict]) -> dict:
    sources: dict[str, Counter] = {}
    categories: dict[str, Counter] = {}
    for attempt in attempts:
        source = str(attempt.get("source") or "training")
        category = str(attempt.get("category") or "uncategorized")
        sources.setdefault(source, Counter()).update(total=1, solved=int(bool(attempt.get("solved"))))
        categories.setdefault(category, Counter()).update(
            total=1, solved=int(bool(attempt.get("solved")))
        )

    def row(name: str, counts: Counter) -> dict:
        total = int(counts.get("total", 0))
        solved = int(counts.get("solved", 0))
        return {
            "name": name,
            "total": total,
            "solved": solved,
            "solve_rate": round(solved * 100.0 / total, 1) if total else None,
        }

    total = len(attempts)
    solved = sum(1 for item in attempts if item.get("solved"))
    return {
        "total": total,
        "solved": solved,
        "solve_rate": round(solved * 100.0 / total, 1) if total else None,
        "by_source": [row(name, value) for name, value in sorted(sources.items())],
        "by_category": sorted(
            (row(name, value) for name, value in categories.items()),
            key=lambda item: (-item["total"], item["name"]),
        ),
    }


def _trend(records: list[dict]) -> dict | None:
    """Compare two adjacent recent samples; avoid calling two games a lasting trend."""
    if len(records) < 4:
        return None
    ordered = sorted(records, key=lambda item: (item.get("date") or "", item.get("analyzed_at") or ""))
    sample = min(10, len(ordered) // 2)
    previous, recent = ordered[-sample * 2 : -sample], ordered[-sample:]

    def avg(items: list[dict]) -> float | None:
        values = [float(item["accuracy"]) for item in items if item.get("accuracy") is not None]
        return sum(values) / len(values) if values else None

    before, after = avg(previous), avg(recent)
    if before is None or after is None:
        return None
    delta = round(after - before, 1)
    return {
        "sample_games": sample,
        "previous_accuracy": round(before, 1),
        "recent_accuracy": round(after, 1),
        "accuracy_delta": delta,
        "direction": "improving" if delta >= 1.0 else "declining" if delta <= -1.0 else "steady",
    }


def insights(days: Optional[int] = None, data_dir: Optional[str] = None) -> dict:
    """Aggregate stats + recurring themes for the configured user's games in a time window.

    `days` limits to games from the last N days (by played date, falling back to analysed
    date); None/0 = all history. Reuses `_aggregate`, plus human labels on the motifs so the
    frontend can render them directly.
    """
    me = my_player_id(data_dir)
    records = load_records(data_dir=data_dir)
    # With an identity configured, insights are about "you". Without one (e.g. a paste-only user
    # who never set a username), aggregate everything — same philosophy as the "My games" list.
    if (config.USERNAME or "").strip():
        records = [
            r
            for r in records
            if r.get("player_id") == me
            or _resolves_to_me(
                r.get("player_name") or r.get("player_id") or "", r.get("platform"), data_dir
            )
        ]
    profile_game_ids = {str(r.get("game_id")) for r in records if r.get("game_id")}
    cutoff = None
    if days and days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        records = [r for r in records if _record_day(r) >= cutoff]

    attempts = [
        item
        for item in load_attempt_records(data_dir)
        if (not profile_game_ids or str(item.get("game_id")) in profile_game_ids)
        and (not cutoff or str(item.get("attempted_at") or "")[:10] >= cutoff)
    ]
    agg = _aggregate(records, attempts=attempts)
    for m in agg.get("top_motifs", []):
        m["label"] = _MOTIF_LABELS.get(m["motif"], m["motif"])
    agg["trend"] = _trend(records)
    _apply_canonical_learning(
        agg,
        data_dir=data_dir,
        window="lifetime" if not days else "recent",
    )
    return {"schema_version": SCHEMA_VERSION, "player_id": me, "days": days or 0, **agg}


# --------------------------------------------------------------------------------------
# Derived profile (rebuildable cache)
# --------------------------------------------------------------------------------------
_CHECKLISTS = {
    "allowed_mate": "Before moving, calculate every forcing check available to the opponent.",
    "missed_mate": "When a king is exposed, calculate checks before quiet moves.",
    "wrong_exchange_sequence": "Calculate exchanges through the final recapture.",
    "missed_opponent_threat": "Name the opponent's checks, captures, and threats first.",
    "hanging_piece": "Check that the destination square and every loose piece stay defended.",
    "missed_capture": "Scan all legal captures before choosing a positional move.",
    "fork": "Look for forcing moves that attack two valuable targets.",
}


def _fast_limit(speed: str) -> float:
    return {"bullet": 1.5, "blitz": 3.0, "rapid": 6.0, "classical": 12.0}.get(speed, 4.0)


_PREFERRED_LEGACY_CATEGORY = {
    "tactics.fork_detection": "fork",
    "tactics.mating_threat_detection": "allowed_mate",
    "tactics.loose_piece_awareness": "hanging_piece",
    "calculation.opponent_forcing_moves": "missed_opponent_threat",
    "calculation.exchange_sequence": "wrong_exchange_sequence",
    "calculation.candidate_moves": "missed_capture",
    "strategy.king_safety": "king_safety",
    "strategy.piece_activity": "piece_activity",
    "opening.development": "development",
    "endgame.conversion": "conversion",
    "practical.blunder_check": "blunder_check",
    "practical.time_management": "time_management",
}


def _legacy_learning_example(reference: object) -> dict:
    return {
        key: value
        for key, value in {
            "game_id": getattr(reference, "game_id", None),
            "reviewed_side": getattr(reference, "review_side", None),
            "critical_id": getattr(reference, "critical_id", None),
            "ply": getattr(reference, "ply", None),
            "fen": getattr(reference, "fen", None),
            "puzzle_id": getattr(reference, "puzzle_id", None),
        }.items()
        if value is not None
    }


def _is_legacy_training_reference(reference: object) -> bool:
    """Whether the legacy profile can open this example as a game retry."""

    return bool(
        getattr(reference, "game_id", None)
        and getattr(reference, "review_side", None)
        and getattr(reference, "critical_id", None)
    )


def _canonical_weakness_rows(
    aggregate: dict,
    *,
    data_dir: Optional[str],
    window: str,
) -> list[dict]:
    """Adapt canonical weaknesses to the legacy frontend row shape."""

    if not config.PERSONALIZE_HISTORY or not learning_memory.is_available():
        return []
    try:
        estimates = EstimateStore(_data_dir(data_dir)).ensure_current(window=window)
    except Exception:  # noqa: BLE001 - profile display degrades without affecting history
        return []
    weaknesses = [
        estimate
        for estimate in rank_estimates(estimates)
        if estimate.status == "weakness"
        and any(_is_legacy_training_reference(item) for item in estimate.examples)
    ][:3]
    category_rows = [
        row for row in aggregate.get("categories", []) if isinstance(row, dict)
    ]
    total_failures = sum(estimate.failure_count for estimate in weaknesses) or 1
    total_loss = sum(estimate.cumulative_loss for estimate in weaknesses) or 1.0
    output: list[dict] = []
    for estimate in weaknesses:
        matching = [
            row
            for row in category_rows
            if learning_taxonomy.resolve_skill_id(str(row.get("category") or ""))
            == estimate.skill_id
        ]
        base = max(
            matching,
            key=lambda row: (
                int(row.get("count") or 0),
                float(row.get("cumulative_win_loss") or 0.0),
            ),
            default={},
        )
        examples = [
            _legacy_learning_example(item)
            for item in estimate.examples
            if _is_legacy_training_reference(item)
        ]
        count = estimate.failure_count
        cumulative_loss = estimate.cumulative_loss
        category = str(
            base.get("category")
            or _PREFERRED_LEGACY_CATEGORY.get(estimate.skill_id)
            or estimate.skill_id
        )
        output.append(
            {
                **base,
                "skill_id": estimate.skill_id,
                "category": category,
                "count": count,
                "game_count": estimate.distinct_games,
                "cumulative_win_loss": round(cumulative_loss, 1),
                "average_severity": round(cumulative_loss / count, 1) if count else 0.0,
                "primary_phase": str(base.get("primary_phase") or "training"),
                "examples": examples,
                "share": round(count / total_failures * 100.0, 1),
                "impact_share": round(cumulative_loss / total_loss * 100.0, 1),
                "data_window_games": int(aggregate.get("games") or 0),
                "typical_position": examples[0],
                "confidence_level": estimate.confidence_level,
            }
        )
    return output


def _apply_canonical_learning(
    aggregate: dict,
    *,
    data_dir: Optional[str],
    window: str,
) -> dict:
    aggregate["weaknesses"] = _canonical_weakness_rows(
        aggregate,
        data_dir=data_dir,
        window=window,
    )
    aggregate["coach_summary"] = _personal_coach_summary(aggregate)
    return aggregate


def _personal_coach_summary(aggregate: dict) -> dict:
    weaknesses = aggregate.get("weaknesses") or []
    games = int(aggregate.get("games") or 0)
    if not weaknesses:
        return {
            "ready": False,
            "headline": (
                "Analyze at least 3 games with a repeated error category to establish a weakness."
                if games
                else "Analyze games to start building your local coaching profile."
            ),
            "problems": [],
            "checklist": [],
            "recommended_training": [],
        }

    top = weaknesses[0]
    phase = str(top.get("primary_phase") or "middlegame")
    fast = int(top.get("fast_count") or 0)
    count = int(top.get("count") or 0)
    fast_note = (
        f" {fast} of those errors were played unusually quickly for the time control."
        if count and fast / count >= 0.4
        else ""
    )
    trend = aggregate.get("trend") or {}
    trend_note = ""
    if trend:
        trend_note = (
            f" Recent accuracy is {trend['direction']} ({trend['recent_accuracy']}% vs "
            f"{trend['previous_accuracy']}%)."
        )
    checklist = []
    for item in weaknesses:
        advice = _CHECKLISTS.get(item["category"])
        if advice and advice not in checklist:
            checklist.append(advice)
    return {
        "ready": True,
        "headline": (
            f"Across {games} games, {count} key losses came from "
            f"{str(top['category']).replace('_', ' ')}, mainly in the {phase}."
            f"{fast_note}{trend_note}"
        ),
        "problems": weaknesses,
        "checklist": checklist[:3],
        "recommended_training": [
            {
                "category": item["category"],
                "count": item["count"],
                "game_id": (item.get("typical_position") or {}).get("game_id"),
                "critical_id": (item.get("typical_position") or {}).get("critical_id"),
                "reviewed_side": (item.get("typical_position") or {}).get("reviewed_side"),
            }
            for item in weaknesses
        ],
    }


def _aggregate(records: list[dict], attempts: Optional[list[dict]] = None) -> dict:
    """Aggregate a list of game records into one stats view (accuracy, motifs, phases, openings)."""
    agg: dict = {"games": len(records)}
    if not records:
        agg.update(
            {
                "avg_accuracy": None,
                "results": {key: 0 for key in ("win", "loss", "draw")},
                "mistake_totals": {key: 0 for key in ("inaccuracy", "mistake", "blunder")},
                "mistakes_per_game": {key: 0.0 for key in ("inaccuracy", "mistake", "blunder")},
                "top_motifs": [],
                "phase_loss_total": {key: 0.0 for key in ("opening", "middlegame", "endgame")},
                "phase_error_counts": {key: 0 for key in ("opening", "middlegame", "endgame")},
                "weakest_phase": None,
                "categories": [],
                "weaknesses": [],
                "training": _attempt_summary(attempts or []),
                "by_speed": [],
                "openings": [],
            }
        )
        return agg

    accs = [r["accuracy"] for r in records if r.get("accuracy") is not None]
    results: Counter = Counter(r.get("player_result") for r in records if r.get("player_result"))
    counts: Counter = Counter()
    motifs: Counter = Counter()
    phase_loss = {"opening": 0.0, "middlegame": 0.0, "endgame": 0.0}
    openings: dict[str, dict] = {}
    by_speed: dict[str, dict] = {}
    category_stats: dict[str, dict] = {}
    phase_errors: Counter = Counter()

    for r in records:
        for k, v in (r.get("counts") or {}).items():
            counts[k] += v
        for k, v in (r.get("phase_loss") or {}).items():
            phase_loss[k] = phase_loss.get(k, 0.0) + v
        for m in r.get("mistakes", []):
            motifs.update(m.get("motifs", []))
        compact = list(r.get("critical_positions") or [])
        if not compact:
            compact = [
                {
                    "critical_id": None,
                    "ply": item.get("ply"),
                    "classification": item.get("classification"),
                    "category": (item.get("motifs") or ["uncategorized"])[0],
                    "phase": item.get("phase") or "middlegame",
                    "win_loss": item.get("win_drop") or 0.0,
                    "seconds_spent": item.get("seconds_spent"),
                }
                for item in r.get("mistakes", []) or []
            ]
        for item in compact:
            category = str(item.get("category") or "uncategorized")
            phase = str(item.get("phase") or "middlegame")
            loss = float(item.get("win_loss") or 0.0)
            stat = category_stats.setdefault(
                category,
                {
                    "count": 0,
                    "cumulative_win_loss": 0.0,
                    "phases": Counter(),
                    "classifications": Counter(),
                    "fast_count": 0,
                    "examples": [],
                    "games": set(),
                },
            )
            stat["count"] += 1
            stat["cumulative_win_loss"] += loss
            stat["phases"][phase] += 1
            stat["classifications"][str(item.get("classification") or "critical")] += 1
            stat["games"].add(str(r.get("game_id") or ""))
            phase_errors[phase] += 1
            spent = item.get("seconds_spent")
            if spent is not None and float(spent) <= _fast_limit(str(r.get("speed") or "unknown")):
                stat["fast_count"] += 1
            stat["examples"].append(
                {
                    "game_id": r.get("game_id"),
                    "reviewed_side": r.get("reviewed_side"),
                    "critical_id": item.get("critical_id"),
                    "ply": item.get("ply"),
                    "date": r.get("date"),
                    "opponent": r.get("black") if r.get("reviewed_side") == "white" else r.get("white"),
                    "win_loss": round(loss, 1),
                }
            )
        op = r.get("opening") or r.get("eco") or "Unknown"
        st = openings.setdefault(op, {"games": 0, "acc_sum": 0.0})
        st["games"] += 1
        if r.get("accuracy") is not None:
            st["acc_sum"] += r["accuracy"]
        # Per-mode (bullet/blitz/rapid/...) breakdown, so coaching can apply mode-appropriate
        # expectations and call out where a player's patterns differ by speed.
        sp = r.get("speed") or "unknown"
        sps = by_speed.setdefault(sp, {"games": 0, "acc_sum": 0.0, "acc_n": 0, "blunders": 0})
        sps["games"] += 1
        if r.get("accuracy") is not None:
            sps["acc_sum"] += r["accuracy"]
            sps["acc_n"] += 1
        sps["blunders"] += (r.get("counts") or {}).get("blunder", 0)

    games = len(records)
    category_rows = []
    for category, value in category_stats.items():
        count = int(value["count"])
        category_rows.append(
            {
                "category": category,
                "count": count,
                "game_count": len(value["games"] - {""}),
                "cumulative_win_loss": round(float(value["cumulative_win_loss"]), 1),
                "average_severity": round(float(value["cumulative_win_loss"]) / count, 1),
                "primary_phase": value["phases"].most_common(1)[0][0],
                "phases": dict(value["phases"]),
                "classifications": dict(value["classifications"]),
                "fast_count": int(value["fast_count"]),
                "examples": sorted(
                    value["examples"], key=lambda item: -float(item.get("win_loss") or 0.0)
                )[:3],
            }
        )
    category_rows.sort(key=lambda item: (-item["count"], -item["cumulative_win_loss"]))

    agg.update(
        {
            "avg_accuracy": round(sum(accs) / len(accs), 1) if accs else None,
            "results": {k: results.get(k, 0) for k in ("win", "loss", "draw")},
            "mistake_totals": {k: counts.get(k, 0) for k in ("inaccuracy", "mistake", "blunder")},
            "mistakes_per_game": {
                k: round(counts.get(k, 0) / games, 2) for k in ("inaccuracy", "mistake", "blunder")
            },
            "top_motifs": [{"motif": k, "count": v} for k, v in motifs.most_common(8)],
            "phase_loss_total": {k: round(v, 1) for k, v in phase_loss.items()},
            "phase_error_counts": {
                key: int(phase_errors.get(key, 0)) for key in ("opening", "middlegame", "endgame")
            },
            "weakest_phase": max(phase_loss, key=phase_loss.get) if any(phase_loss.values()) else None,
            "categories": category_rows,
            "weaknesses": [],
            "training": _attempt_summary(attempts or []),
            "by_speed": [
                {
                    "speed": k,
                    "games": v["games"],
                    "avg_accuracy": round(v["acc_sum"] / v["acc_n"], 1) if v["acc_n"] else None,
                    "blunders_per_game": round(v["blunders"] / v["games"], 2) if v["games"] else None,
                }
                for k, v in sorted(by_speed.items(), key=lambda kv: -kv[1]["games"])
            ],
            "openings": sorted(
                (
                    {
                        "opening": k,
                        "games": v["games"],
                        "avg_accuracy": round(v["acc_sum"] / v["games"], 1) if v["games"] else None,
                    }
                    for k, v in openings.items()
                ),
                key=lambda o: -o["games"],
            )[:10],
        }
    )
    return agg


def build_profile(player_id: str, data_dir: Optional[str] = None) -> dict:
    """Build a hybrid coaching profile: a "recent form" sliding window + a "lifetime" view.

    The split lets coaching adapt as a player improves (recent weaknesses surface; old, fixed ones
    fade out of the window). Window sizes come from config: `PROFILE_RECENT_WINDOW` (last N games;
    <=0 = all) and `PROFILE_LIFETIME` (None = all history, positive N = last N, 0 = omit the
    lifetime view so the profile is a pure sliding window). Both recompute from the full history.
    """
    records = load_records(data_dir=data_dir)
    if player_id == my_player_id(data_dir):
        records = [
            item
            for item in records
            if item.get("player_id") == player_id
            or _resolves_to_me(
                item.get("player_name") or item.get("player_id") or "",
                item.get("platform"),
                data_dir,
            )
        ]
    else:
        records = [item for item in records if item.get("player_id") == player_id]
    records.sort(key=lambda item: item.get("analyzed_at", ""))
    profile: dict = {
        "schema_version": SCHEMA_VERSION,
        "player_id": player_id,
        "display_name": _display_name(player_id, data_dir),
        "games_analyzed": len(records),
        "generated_at": _now_iso(),
    }
    if not records:
        return profile

    recent_n = config.PROFILE_RECENT_WINDOW
    recent_records = records if recent_n <= 0 else records[-recent_n:]
    attempts = load_attempt_records(data_dir)

    def attempts_for(items: list[dict]) -> list[dict]:
        ids = {str(item.get("game_id")) for item in items if item.get("game_id")}
        return [attempt for attempt in attempts if str(attempt.get("game_id")) in ids]

    recent_aggregate = _aggregate(recent_records, attempts=attempts_for(recent_records))
    recent_aggregate["trend"] = _trend(recent_records)
    _apply_canonical_learning(recent_aggregate, data_dir=data_dir, window="recent")
    profile["recent"] = {"window": recent_n if recent_n > 0 else None, **recent_aggregate}

    lifetime_n = config.PROFILE_LIFETIME
    if lifetime_n != 0:  # 0 disables the lifetime view (pure sliding window)
        lifetime_records = records if lifetime_n is None else records[-lifetime_n:]
        lifetime_aggregate = _aggregate(lifetime_records, attempts=attempts_for(lifetime_records))
        lifetime_aggregate["trend"] = _trend(lifetime_records)
        _apply_canonical_learning(lifetime_aggregate, data_dir=data_dir, window="lifetime")
        profile["lifetime"] = lifetime_aggregate

    profile["recent_games"] = [
        {
            "date": r.get("date"),
            "opening": r.get("opening") or r.get("eco"),
            "accuracy": r.get("accuracy"),
            "result": r.get("player_result"),
            "blunders": (r.get("counts") or {}).get("blunder", 0),
        }
        for r in records[-8:]
    ]
    return profile


def write_profile(player_id: str, data_dir: Optional[str] = None) -> dict:
    profile = build_profile(player_id, data_dir)
    path = _profile_path(player_id, data_dir)
    content = (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, content)
    return profile


# --------------------------------------------------------------------------------------
# Public entry points used by Web jobs and tests.
# --------------------------------------------------------------------------------------
def record_game(sess: ReviewSession, data_dir: Optional[str] = None) -> dict:
    """Append the game to history and refresh the player's profile cache. Returns the record."""
    # Compatibility callers may start analysis without going through POST /games/import. Materialize
    # the same stable source artifact here so every new history row remains reopenable after restart.
    game_id = _game_id(sess)
    source_path = os.path.join(_data_dir(data_dir), "games", game_id, "source.pgn")
    if not os.path.isfile(source_path):
        try:
            from server.core.importers import import_pgn
            from server.core.storage import store_game

            imported = import_pgn(
                sess.pgn,
                source_type=(
                    "chesscom_sync"
                    if _platform_from_headers(sess.headers) == "chesscom"
                    else "lichess"
                    if _platform_from_headers(sess.headers) == "lichess"
                    else "pgn_text"
                ),
                review_side=sess.player,
            )[0]
            if data_dir is None or os.path.abspath(_data_dir(data_dir)) == os.path.abspath(config.DATA_DIR):
                store_game(imported)
            else:
                directory = os.path.dirname(source_path)
                os.makedirs(directory, exist_ok=True)
                with open(source_path, "w", encoding="utf-8") as fh:
                    fh.write(imported.pgn)
                with open(os.path.join(directory, "metadata.json"), "w", encoding="utf-8") as fh:
                    json.dump(imported.metadata(), fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
    record = build_game_record(sess, data_dir)
    append_record(record, data_dir)
    write_profile(record["player_id"], data_dir)
    return record


def get_profile(player_id: Optional[str] = None, data_dir: Optional[str] = None) -> dict:
    """Profile for `player_id`, or for the current session's player when omitted."""
    if player_id is None:
        sess = session_mod.get_session()
        if sess is None:
            return {
                "error": "No player_id given and no game analysed yet.",
                "known_players": list_players(data_dir),
            }
        player_id, _, _ = resolve_identity(sess.headers, sess.player, data_dir)
    return build_profile(player_id, data_dir)


def delete_game_data(game_id: str, data_dir: Optional[str] = None) -> dict:
    """Remove one game, every indexed side, linked attempts, and rebuild derived profiles."""
    from server.core.storage import (
        coordinated_attempt_log_mutation,
        coordinated_game_mutation,
        coordinated_learning_source_mutation,
    )

    base = _data_dir(data_dir)
    if not re.fullmatch(r"[0-9a-f]{20}", game_id or ""):
        raise ValueError("Unknown game.")
    directory = os.path.join(base, "games", game_id)
    staged_artifact: str | None = None

    with coordinated_game_mutation(game_id):
        with coordinated_learning_source_mutation():
            with coordinated_attempt_log_mutation(), _HISTORY_LOCK:
                file_snapshots: dict[str, bytes | None] = {}
                profile_dir = os.path.join(base, "profiles")
                profile_dir_existed = False
                profiles: dict[str, bytes] = {}
                mutations_started = False
                try:
                    records = load_records(data_dir=data_dir)
                    kept = [item for item in records if item.get("game_id") != game_id]
                    removed_records = len(records) - len(kept)

                    attempt_updates: list[tuple[str, list[dict], int]] = []
                    attempt_paths = dict.fromkeys(
                        (
                            _attempts_path(data_dir),
                            os.path.join(base, "training", "attempts.jsonl"),
                        )
                    )
                    for path in attempt_paths:
                        rows = _read_attempt_rows_for_deletion(path)
                        if rows is None:
                            continue
                        remaining = [row for row in rows if row.get("game_id") != game_id]
                        attempt_updates.append((path, remaining, len(rows) - len(remaining)))

                    mutable_paths = [
                        path for path, _remaining, removed in attempt_updates if removed
                    ]
                    if removed_records:
                        mutable_paths.append(_history_path(data_dir))
                    file_snapshots = {path: _snapshot_file(path) for path in mutable_paths}
                    profile_dir_existed, profiles = _profile_snapshots(profile_dir)

                    removed_attempts = sum(update[2] for update in attempt_updates)
                    mutations_started = True
                    for path, remaining, removed in attempt_updates:
                        if removed:
                            _atomic_jsonl(path, remaining)
                    if removed_records:
                        _atomic_jsonl(_history_path(data_dir), kept)

                    try:
                        for name in os.listdir(profile_dir):
                            if name.endswith(".json"):
                                os.unlink(os.path.join(profile_dir, name))
                    except FileNotFoundError:
                        pass
                    for player_id in sorted(
                        {item.get("player_id") for item in kept if item.get("player_id")}
                    ):
                        write_profile(str(player_id), data_dir)

                    staged_artifact = _stage_game_artifact(directory, base)
                    artifact_deleted = staged_artifact is not None
                except Exception as exc:
                    if mutations_started:
                        try:
                            _restore_deletion_snapshots(
                                file_snapshots,
                                profile_dir=profile_dir,
                                profile_dir_existed=profile_dir_existed,
                                profiles=profiles,
                            )
                        except OSError as rollback_exc:
                            raise GameDeletionError(
                                f"Could not safely delete game {game_id}; deletion failed ({exc}) "
                                f"and index rollback also failed: {rollback_exc}"
                            ) from rollback_exc
                    if isinstance(exc, GameDeletionError):
                        raise
                    raise GameDeletionError(
                        f"Could not safely delete game {game_id}; deletion was rolled back: {exc}"
                    ) from exc

    if staged_artifact is not None:
        try:
            shutil.rmtree(staged_artifact)
        except Exception:  # noqa: BLE001 - committed hidden tombstones are safe to clean later
            pass

    return {
        "game_id": game_id,
        "artifact_deleted": artifact_deleted,
        "history_records_removed": removed_records,
        "attempts_removed": removed_attempts,
    }

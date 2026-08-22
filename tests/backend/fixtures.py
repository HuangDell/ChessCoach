"""Small deterministic artifacts shared by backend contract tests."""
from __future__ import annotations

import json
from pathlib import Path

import chess

from server.core import fact_extraction


GAME_ID = "0123456789abcdefabcd"
CRITICAL_ID = "ply-1"
TACTICAL_FEN = "4k3/8/8/8/8/3q4/8/3QK2R w K - 0 1"


def _score_povs(value: int) -> dict:
    # Production score aliases identify the semantic consumer; each score states the actual color POV.
    return {
        name: {"type": "cp", "value": value, "pov": "white"}
        for name in ("white", "mover", "review_side")
    }


def _win_povs(white: float) -> dict:
    return {
        "white": white,
        "black": round(100.0 - white, 1),
        "mover": white,
        "review_side": white,
    }


def _candidate(
    *,
    rank: int,
    uci: str,
    san: str,
    value: int,
    win: float,
    line_uci: list[str],
    line_san: list[str],
) -> dict:
    return {
        "rank": rank,
        "move": {"uci": uci, "san": san},
        "eval": {"type": "cp", "value": value, "pov": "white"},
        "scores": _score_povs(value),
        "win_percent": _win_povs(win),
        "win_gap_from_best": round(92.0 - win, 1),
        "centipawn_gap_from_best": max(0, 900 - value),
        "line": {"uci": line_uci, "san": line_san},
    }


def analysis_artifact() -> dict:
    """Return a synthetic artifact with the same shapes as ``game_analysis`` output."""
    before = chess.Board(TACTICAL_FEN)
    played = chess.Move.from_uci("h1h2")
    after = before.copy(stack=False)
    after.push(played)
    scores_before = _score_povs(900)
    scores_after = _score_povs(-900)
    wins_before = _win_povs(92.0)
    wins_after = _win_povs(8.0)
    candidates = [
        _candidate(
            rank=1,
            uci="d1d3",
            san="Qxd3",
            value=900,
            win=92.0,
            line_uci=["d1d3", "e8f7"],
            line_san=["Qxd3", "Kf7"],
        ),
        _candidate(
            rank=2,
            uci="d1a4",
            san="Qa4+",
            value=650,
            win=82.0,
            line_uci=["d1a4", "e8f8"],
            line_san=["Qa4+", "Kf8"],
        ),
        _candidate(
            rank=3,
            uci="h1h2",
            san="Rh2",
            value=-900,
            win=8.0,
            line_uci=["h1h2", "d3d1"],
            line_san=["Rh2", "Qxd1+"],
        ),
    ]
    critical = {
        "critical_id": CRITICAL_ID,
        "priority": 1,
        "critical_score": 880.0,
        "ply": 1,
        "move_number": 1,
        "side": "white",
        "fen_before": before.fen(),
        "fen_after": after.fen(),
        "played_move": {"uci": played.uci(), "san": before.san(played)},
        "classification": "blunder",
        "eval_before": scores_before["white"],
        "eval_after": scores_after["white"],
        "scores": {"before": scores_before, "after": scores_after},
        "win_loss": 84.0,
        "centipawn_loss": 1800.0,
        "signals": ["missed_win"],
        "played_move_rank": 3,
        "played_move_in_multipv": True,
        "played_move_eval": scores_after["white"],
        "played_move_scores": scores_after,
        "opponent_best_reply": {"uci": "d3d1", "san": "Qxd1+"},
        "played_line": {"uci": ["h1h2", "d3d1"], "san": ["Rh2", "Qxd1+"]},
        "best_line": candidates[0]["line"],
        "candidates": candidates,
        "criticality": "critical",
        "forced_mate": False,
        "deep_depth": 18,
        "multipv": 3,
    }
    critical["facts"] = fact_extraction.extract_facts(critical, line_plies=8)
    stage_move = {
        "ply": 1,
        "move_number": 1,
        "side": "white",
        "fen_before": before.fen(),
        "fen_after": after.fen(),
        "played_move": critical["played_move"],
        "eval_before": critical["eval_before"],
        "eval_after": critical["eval_after"],
        "scores": critical["scores"],
        "win_percent_before": wins_before,
        "win_percent_after": wins_after,
        "win_percent_loss": critical["win_loss"],
        "centipawn_loss": critical["centipawn_loss"],
        "classification": critical["classification"],
        "best_move": candidates[0]["move"],
        "best_pv": candidates[0]["line"],
        "signals": critical["signals"],
        "terminal_after": False,
        "clock_seconds": None,
    }
    return {
        "schema_version": 2,
        "game_id": GAME_ID,
        "review_side": "white",
        "cache_key": f"{GAME_ID}:white:test-profile",
        "headers": {
            "White": "Student",
            "Black": "Opponent",
            "Result": "0-1",
            "TimeControl": "600+0",
        },
        "result": "0-1",
        "profile": {
            "version": "engine-analysis-v1",
            "preset": "balanced",
            "scan": {"depth": 12, "multipv": 1},
            "deep": {"depth": 18, "multipv": 3},
            "id": "test-profile",
            "review": {"elo": 1400, "speed": "rapid"},
            "critical": {
                "minimum_target": 1,
                "maximum": 3,
                "thresholds": [5.0, 10.0, 20.0],
            },
            "facts": {"version": 1, "line_plies": 8},
        },
        "engine": {"name": "Stockfish 17", "options": {"Threads": 1, "Hash": 16}},
        "summary": {
            "positions_scanned": 2,
            "plies": 1,
            "critical_positions": 1,
            "fact_positions": 1,
            "reviewed_moves": 1,
            "classifications": {"best": 0, "good": 0, "inaccuracy": 0, "mistake": 0, "blunder": 1},
        },
        "moves": [stage_move],
        "critical_positions": [critical],
    }


def store_analysis_fixture(data_dir: str) -> dict:
    artifact = analysis_artifact()
    game_dir = Path(data_dir) / "games" / GAME_ID
    side_dir = game_dir / "analysis"
    side_dir.mkdir(parents=True, exist_ok=True)
    content = json.dumps(artifact, sort_keys=True)
    (game_dir / "analysis.json").write_text(content, encoding="utf-8")
    (side_dir / "white.json").write_text(content, encoding="utf-8")
    return artifact


def history_record(index: int) -> dict:
    game_id = f"{index:020x}"
    return {
        "schema_version": 2,
        "game_id": game_id,
        "reviewed_side": "white",
        "player_id": "student",
        "player_name": "Student",
        "analyzed_at": f"2026-08-{18 + index:02d}T12:00:00Z",
        "date": f"2026-08-{18 + index:02d}",
        "white": "Student",
        "black": f"Opponent {index}",
        "player_result": "loss" if index < 3 else "win",
        "accuracy": 70.0 + index,
        "speed": "rapid",
        "opening": "Queen's Pawn Game",
        "counts": {"inaccuracy": 0, "mistake": 1, "blunder": 0},
        "phase_loss": {"opening": 0.0, "middlegame": 12.0, "endgame": 0.0},
        "critical_positions": [
            {
                "critical_id": f"ply-{index * 2 + 1}",
                "ply": index * 2 + 1,
                "classification": "mistake",
                "category": "missed_opponent_threat",
                "phase": "middlegame",
                "win_loss": 12.0,
                "seconds_spent": 5.0,
            }
        ],
    }

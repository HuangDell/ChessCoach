"""Conservative positive awards backed by deep candidates and legal sacrifice lines."""
from __future__ import annotations

import chess

from server import config
from server.core import engine
from server.core.evaluation import classify, win_percent_from_score
from server.core.lines import _settle_leaf, material_balance, pv_to_san

CLASSIFICATION_VERSION = 1


def classify_positive(before: chess.Board, position: dict, thresholds: tuple) -> tuple[str, dict]:
    """Return one final label and auditable evidence; never infer brilliance from cp gains."""
    candidates = position["candidates"]
    if not candidates:
        raise ValueError("Deep analysis returned no candidates")
    played = chess.Move.from_uci(position["played_move"]["uci"])
    top = candidates[0]
    best_win = top["win_percent"]["mover"]
    played_score = position["played_move_scores"]["mover"]
    played_win = position["verified_win_after"]
    is_best = played.uci() == top["move"]["uci"]
    base = classify(best_win, played_win, is_best=is_best, thresholds=thresholds)
    evidence = {
        "version": CLASSIFICATION_VERSION,
        "base_classification": base,
        "best_win_percent": best_win,
        "played_win_percent": played_win,
        "played_score": played_score,
        "candidate_gap": candidates[1]["win_gap_from_best"] if len(candidates) > 1 else None,
        "depth": position["deep_depth"],
    }
    label = base
    if base not in {"best", "excellent"} or played_win < 50:
        return label, evidence
    if (is_best and len(candidates) > 1 and before.legal_moves.count() > 1
            and evidence["candidate_gap"] >= config.GREAT_MOVE_GAP):
        label = "great"
    # Require a genuinely weaker alternative; an already trivially won position is not special.
    if not any(c["move"]["uci"] != played.uci() and c["win_percent"]["mover"] < 75
               for c in candidates):
        return label, evidence

    after = before.copy(stack=True)
    after.push(played)
    material_before = material_balance(before, before.turn)
    for capture in list(after.generate_legal_captures()):
        victim = after.piece_at(capture.to_square)
        if victim is None or victim.piece_type in {chess.PAWN, chess.KING}:
            continue
        # A piece already hanging elsewhere is not a new sacrifice by this move.
        if (capture.to_square != played.to_square
                and before.is_attacked_by(not before.turn, capture.to_square)):
            continue
        accepted = after.copy(stack=True)
        accepted.push(capture)
        if material_before - material_balance(accepted, before.turn) < config.BRILLIANT_MIN_MATERIAL:
            continue
        if accepted.is_game_over(claim_draw=True):
            continue
        response = engine.analyse(accepted.fen(), depth=position["deep_depth"], multipv=1).best
        if win_percent_from_score(response.cp, response.mate) < 50:
            continue
        leaf = _settle_leaf(accepted, response.pv_uci, position["deep_depth"])
        line = [move.uci() for move in leaf.move_stack]
        if not line or (leaf.is_check() and not leaf.is_checkmate()):
            continue
        # Reject a horizon ending before a legal recapture, even after the extension cap.
        previous = leaf.copy(stack=True)
        last = previous.pop()
        if previous.is_capture(last) and any(m.to_square == last.to_square
                                             for m in leaf.generate_legal_captures()):
            continue
        replay = accepted.copy(stack=False)
        recovered = False
        for uci in line:
            replay.push_uci(uci)
            if material_before - material_balance(replay, before.turn) < config.BRILLIANT_MIN_MATERIAL:
                recovered = True
                break
        if recovered:
            continue
        investment = material_before - material_balance(leaf, before.turn)
        if investment < config.BRILLIANT_MIN_MATERIAL:
            continue
        full_line = [played.uci(), capture.uci(), *line]
        evidence["sacrifice"] = {
            "piece": victim.symbol().upper(), "square": chess.square_name(capture.to_square),
            "material_invested": investment, "acceptance_win_percent": win_percent_from_score(response.cp, response.mate),
            "line": {"uci": full_line, "san": pv_to_san(before, full_line, max_plies=len(full_line))},
            "settled_fen": leaf.fen(),
        }
        return "brilliant", evidence
    return label, evidence

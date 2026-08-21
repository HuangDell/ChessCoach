"""Deterministic chess facts derived from FENs and legal engine variations.

This module deliberately contains no Engine or LLM calls.  A fact bundle can be
recomputed from one critical-position artifact, which makes it a stable grounding
layer for later coaching text.
"""
from __future__ import annotations

from collections.abc import Iterable

import chess


FACTS_VERSION = 1
DEFAULT_LINE_PLIES = 8

_PIECE_NAMES = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}
_PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}
_CATEGORY_GROUPS = {
    "hanging_piece": "TACTICAL",
    "missed_capture": "TACTICAL",
    "fork": "TACTICAL",
    "allowed_mate": "KING",
    "missed_mate": "TACTICAL",
    "wrong_exchange_sequence": "CALCULATION",
    "missed_opponent_threat": "CALCULATION",
}
_PRIMARY_ORDER = (
    "allowed_mate",
    "missed_mate",
    "wrong_exchange_sequence",
    "fork",
    "hanging_piece",
    "missed_capture",
    "missed_opponent_threat",
)


class FactExtractionError(ValueError):
    """Raised when a critical artifact cannot be legally reconstructed."""


def _color_name(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def _piece_ref(board: chess.Board, square: chess.Square) -> dict:
    piece = board.piece_at(square)
    if piece is None:
        raise FactExtractionError(f"No piece on {chess.square_name(square)}.")
    return {
        "color": _color_name(piece.color),
        "piece": _PIECE_NAMES[piece.piece_type],
        "square": chess.square_name(square),
        "value": _PIECE_VALUES[piece.piece_type],
    }


def _material(board: chess.Board) -> dict:
    counts: dict[str, dict[str, int]] = {}
    points: dict[str, int] = {}
    for color in (chess.WHITE, chess.BLACK):
        name = _color_name(color)
        counts[name] = {
            _PIECE_NAMES[piece_type]: len(board.pieces(piece_type, color))
            for piece_type in (
                chess.PAWN,
                chess.KNIGHT,
                chess.BISHOP,
                chess.ROOK,
                chess.QUEEN,
                chess.KING,
            )
        }
        points[name] = sum(
            len(board.pieces(piece_type, color)) * value
            for piece_type, value in _PIECE_VALUES.items()
        )
    return {
        "counts": counts,
        "points": points,
        "balance_white": points["white"] - points["black"],
    }


def _attacker_refs(
    board: chess.Board, color: chess.Color, square: chess.Square
) -> list[dict]:
    return [
        _piece_ref(board, attacker)
        for attacker in sorted(board.attackers(color, square))
    ]


def _legal_capture_to(board: chess.Board, square: chess.Square, color: chess.Color) -> bool:
    """Whether ``color`` can legally capture the piece if it has the move.

    Probing both colours lets a snapshot describe a loose piece before its owner moves.
    En-passant state is cleared when the turn is hypothetical because it belongs only to
    the actual side to move.
    """
    probe = board.copy(stack=False)
    if probe.turn != color:
        probe.turn = color
        probe.ep_square = None
    for move in probe.legal_moves:
        if move.to_square == square and probe.is_capture(move):
            return True
    return False


def _piece_safety(board: chess.Board) -> dict:
    attacked: list[dict] = []
    undefended: list[dict] = []
    hanging: list[dict] = []
    for square, piece in sorted(board.piece_map().items()):
        if piece.piece_type == chess.KING:
            continue
        attackers = _attacker_refs(board, not piece.color, square)
        defenders = _attacker_refs(board, piece.color, square)
        item = {
            **_piece_ref(board, square),
            "attackers": attackers,
            "defenders": defenders,
            "attacker_count": len(attackers),
            "defender_count": len(defenders),
        }
        if attackers:
            attacked.append(item)
        if not defenders:
            undefended.append(_piece_ref(board, square))
        # A conservative definition: an undefended non-king piece with a legal direct capture.
        # Defenders use the geometric attack map, so a dubious pinned defender suppresses the
        # tag instead of creating a false positive.
        if attackers and not defenders and _legal_capture_to(board, square, not piece.color):
            hanging.append(item)
    return {
        "attacked_pieces": attacked,
        "undefended_pieces": undefended,
        "hanging_pieces": hanging,
    }


def _captured_piece(board: chess.Board, move: chess.Move) -> dict | None:
    if not board.is_capture(move):
        return None
    square = move.to_square
    if board.is_en_passant(move):
        square += -8 if board.turn == chess.WHITE else 8
    return _piece_ref(board, square)


def _legal_tactical_moves(board: chess.Board) -> dict:
    checks: list[dict] = []
    captures: list[dict] = []
    for move in list(board.legal_moves):
        is_capture = board.is_capture(move)
        is_check = board.gives_check(move)
        if not is_capture and not is_check:
            continue
        item = {
            "uci": move.uci(),
            "san": board.san(move),
            "piece": _PIECE_NAMES[board.piece_type_at(move.from_square) or chess.PAWN],
        }
        if is_capture:
            item["captured"] = _captured_piece(board, move)
            captures.append(item)
        if is_check:
            checks.append(item)
    return {"checks": checks, "captures": captures}


def _file_pawns(board: chess.Board, color: chess.Color, file_index: int) -> list[str]:
    return [
        chess.square_name(square)
        for square in sorted(board.pieces(chess.PAWN, color))
        if chess.square_file(square) == file_index
    ]


def _pawn_structure(board: chess.Board) -> dict:
    open_files: list[str] = []
    semi_open = {"white": [], "black": []}
    doubled = {"white": [], "black": []}
    isolated = {"white": [], "black": []}
    passed = {"white": [], "black": []}

    for file_index in range(8):
        file_name = chess.FILE_NAMES[file_index]
        white = _file_pawns(board, chess.WHITE, file_index)
        black = _file_pawns(board, chess.BLACK, file_index)
        if not white and not black:
            open_files.append(file_name)
        elif not white:
            semi_open["white"].append(file_name)
        elif not black:
            semi_open["black"].append(file_name)
        if len(white) > 1:
            doubled["white"].append({"file": file_name, "squares": white})
        if len(black) > 1:
            doubled["black"].append({"file": file_name, "squares": black})

    for color in (chess.WHITE, chess.BLACK):
        color_name = _color_name(color)
        own = board.pieces(chess.PAWN, color)
        opponent = board.pieces(chess.PAWN, not color)
        own_files = {chess.square_file(square) for square in own}
        for square in sorted(own):
            file_index = chess.square_file(square)
            rank_index = chess.square_rank(square)
            if not any(adj in own_files for adj in (file_index - 1, file_index + 1)):
                isolated[color_name].append(chess.square_name(square))
            is_passed = True
            for opponent_square in opponent:
                opponent_file = chess.square_file(opponent_square)
                opponent_rank = chess.square_rank(opponent_square)
                ahead = opponent_rank > rank_index if color == chess.WHITE else opponent_rank < rank_index
                if ahead and abs(opponent_file - file_index) <= 1:
                    is_passed = False
                    break
            if is_passed:
                passed[color_name].append(chess.square_name(square))

    return {
        "open_files": open_files,
        "semi_open_files": semi_open,
        "doubled_pawns": doubled,
        "isolated_pawns": isolated,
        "passed_pawns": passed,
    }


def _legal_mobility(board: chess.Board, color: chess.Color) -> int:
    probe = board.copy(stack=False)
    if probe.turn != color:
        probe.turn = color
        probe.ep_square = None
    return probe.legal_moves.count()


def _king_shield(board: chess.Board, color: chess.Color) -> dict:
    king = board.king(color)
    if king is None:
        return {"king_square": None, "shield_squares": [], "pawns": [], "count": 0}
    rank = chess.square_rank(king) + (1 if color == chess.WHITE else -1)
    squares: list[chess.Square] = []
    if 0 <= rank <= 7:
        king_file = chess.square_file(king)
        squares = [
            chess.square(file_index, rank)
            for file_index in range(max(0, king_file - 1), min(7, king_file + 1) + 1)
        ]
    pawns = [
        square
        for square in squares
        if board.piece_at(square) == chess.Piece(chess.PAWN, color)
    ]
    return {
        "king_square": chess.square_name(king),
        "shield_squares": [chess.square_name(square) for square in squares],
        "pawns": [chess.square_name(square) for square in pawns],
        "count": len(pawns),
    }


def _phase(board: chess.Board) -> dict:
    non_pawn_points = sum(
        len(board.pieces(piece_type, color)) * _PIECE_VALUES[piece_type]
        for color in (chess.WHITE, chess.BLACK)
        for piece_type in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
    )
    queens = len(board.pieces(chess.QUEEN, chess.WHITE)) + len(
        board.pieces(chess.QUEEN, chess.BLACK)
    )
    if non_pawn_points <= 20 or (queens == 0 and non_pawn_points <= 30):
        name = "endgame"
    elif board.fullmove_number <= 12 and non_pawn_points >= 44:
        name = "opening"
    else:
        name = "middlegame"
    return {
        "name": name,
        "method": "material_and_move_number_v1",
        "non_pawn_material": non_pawn_points,
        "queens": queens,
        "fullmove_number": board.fullmove_number,
    }


def snapshot(board: chess.Board) -> dict:
    """Return a JSON-safe board snapshot used by all critical-position facts."""
    return {
        "fen": board.fen(),
        "turn": _color_name(board.turn),
        "in_check": board.is_check(),
        "material": _material(board),
        "safety": _piece_safety(board),
        "legal_tactics": _legal_tactical_moves(board),
        "structure": _pawn_structure(board),
        "mobility": {
            "white": _legal_mobility(board, chess.WHITE),
            "black": _legal_mobility(board, chess.BLACK),
        },
        "king_safety": {
            "white": _king_shield(board, chess.WHITE),
            "black": _king_shield(board, chess.BLACK),
        },
        "phase": _phase(board),
    }


def _piece_key(item: dict) -> tuple[str, str, str]:
    return (str(item.get("color")), str(item.get("piece")), str(item.get("square")))


def _items_added(before: Iterable[dict], after: Iterable[dict]) -> list[dict]:
    before_keys = {_piece_key(item) for item in before}
    return [item for item in after if _piece_key(item) not in before_keys]


def _items_removed(before: Iterable[dict], after: Iterable[dict]) -> list[dict]:
    after_keys = {_piece_key(item) for item in after}
    return [item for item in before if _piece_key(item) not in after_keys]


def _defender_counts(board: chess.Board, color: chess.Color) -> dict[tuple[str, str, str], int]:
    result: dict[tuple[str, str, str], int] = {}
    for square, piece in board.piece_map().items():
        if piece.color == color and piece.piece_type != chess.KING:
            ref = _piece_ref(board, square)
            result[_piece_key(ref)] = len(board.attackers(color, square))
    return result


def _refs_for_keys(board: chess.Board, keys: Iterable[tuple[str, str, str]]) -> list[dict]:
    refs: list[dict] = []
    for _color, _piece, square_name in keys:
        square = chess.parse_square(square_name)
        if board.piece_at(square) is not None:
            refs.append(_piece_ref(board, square))
    return refs


def _move_effects(
    before: chess.Board,
    move: chess.Move,
    before_snapshot: dict,
    after_snapshot: dict,
) -> dict:
    mover = before.turn
    moved = _piece_ref(before, move.from_square)
    captured = _captured_piece(before, move)
    is_capture = before.is_capture(move)
    is_check = before.gives_check(move)
    is_castle = before.is_castling(move)
    before_attacks = {
        chess.square_name(square) for square in before.attacks(move.from_square)
    }
    after = before.copy(stack=False)
    after.push(move)
    after_attacks = {chess.square_name(square) for square in after.attacks(move.to_square)}

    own_name = _color_name(mover)
    enemy_name = _color_name(not mover)
    attacked_before = [
        item
        for item in before_snapshot["safety"]["attacked_pieces"]
        if item["color"] == enemy_name
    ]
    attacked_after = [
        item
        for item in after_snapshot["safety"]["attacked_pieces"]
        if item["color"] == enemy_name
    ]
    own_hanging_before = [
        item
        for item in before_snapshot["safety"]["hanging_pieces"]
        if item["color"] == own_name
    ]
    own_hanging_after = [
        item
        for item in after_snapshot["safety"]["hanging_pieces"]
        if item["color"] == own_name
    ]

    defense_before = _defender_counts(before, mover)
    defense_after = _defender_counts(after, mover)
    stable_keys = set(defense_before) & set(defense_after)
    newly_defended_keys = sorted(
        key for key in stable_keys if defense_before[key] == 0 and defense_after[key] > 0
    )
    lost_defense_keys = sorted(
        key for key in stable_keys if defense_before[key] > 0 and defense_after[key] == 0
    )
    newly_defended = _refs_for_keys(after, newly_defended_keys)
    lost_defenses = _refs_for_keys(after, lost_defense_keys)
    new_hanging = _items_added(own_hanging_before, own_hanging_after)
    resolved_hanging = _items_removed(own_hanging_before, own_hanging_after)

    effect_labels: list[str] = []
    if is_capture and captured:
        effect_labels.append(f"captures_{captured['piece']}_on_{captured['square']}")
    if is_check:
        effect_labels.append("gives_check")
    if is_castle:
        effect_labels.append("castles")
    if move.promotion:
        effect_labels.append(f"promotes_to_{_PIECE_NAMES[move.promotion]}")
    if newly_defended:
        effect_labels.append("defends_piece")
    if resolved_hanging:
        effect_labels.append("resolves_hanging_piece")
    if new_hanging:
        effect_labels.append("leaves_piece_hanging")
    before_shield = before_snapshot["king_safety"][own_name]["count"]
    after_shield = after_snapshot["king_safety"][own_name]["count"]
    if after_shield > before_shield:
        effect_labels.append("improves_pawn_shield")
    if after_shield < before_shield:
        effect_labels.append("weakens_pawn_shield")

    direct_problem_effects = {
        label
        for label in effect_labels
        if label
        in {
            "defends_piece",
            "resolves_hanging_piece",
            "improves_pawn_shield",
            "castles",
        }
    }
    return {
        "move": {"uci": move.uci(), "san": before.san(move)},
        "moved_piece": moved,
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "is_capture": is_capture,
        "captured_piece": captured,
        "is_check": is_check,
        "is_castle": is_castle,
        "promotion": _PIECE_NAMES.get(move.promotion) if move.promotion else None,
        "controlled_squares_added": sorted(after_attacks - before_attacks),
        "controlled_squares_released": sorted(before_attacks - after_attacks),
        "newly_attacked_pieces": _items_added(attacked_before, attacked_after),
        "no_longer_attacked_pieces": _items_removed(attacked_before, attacked_after),
        "fork_targets": _fork_targets(after, move.to_square, mover),
        "newly_defended_pieces": newly_defended,
        "lost_defenses": lost_defenses,
        "new_hanging_own_pieces": new_hanging,
        "resolved_hanging_own_pieces": resolved_hanging,
        "pawn_shield_change": after_shield - before_shield,
        "effects": effect_labels,
        "solves_multiple_direct_problems": len(direct_problem_effects) >= 2,
    }


def _parse_legal_move(board: chess.Board, uci: str, label: str) -> chess.Move:
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise FactExtractionError(f"Invalid {label} UCI move: {uci!r}.") from exc
    if move not in board.legal_moves:
        raise FactExtractionError(f"Illegal {label} move {uci!r} for {board.fen()}.")
    return move


def _material_delta(before: dict, after: dict, mover: chess.Color) -> dict:
    white_delta = after["points"]["white"] - before["points"]["white"]
    black_delta = after["points"]["black"] - before["points"]["black"]
    balance_delta = after["balance_white"] - before["balance_white"]
    return {
        "white_points": white_delta,
        "black_points": black_delta,
        "balance_white": balance_delta,
        "mover_net": balance_delta if mover == chess.WHITE else -balance_delta,
    }


def _replay_line(
    before: chess.Board,
    line_uci: list[str],
    *,
    mover: chess.Color,
    max_plies: int,
) -> tuple[dict, chess.Board]:
    board = before.copy(stack=False)
    baseline_material = _material(board)
    captures: list[dict] = []
    replayed_uci: list[str] = []
    replayed_san: list[str] = []
    captured_by = {"white": 0, "black": 0}
    invalid_at: str | None = None

    for uci in line_uci[:max_plies]:
        try:
            move = chess.Move.from_uci(str(uci))
        except ValueError:
            invalid_at = str(uci)
            break
        if move not in board.legal_moves:
            invalid_at = str(uci)
            break
        san = board.san(move)
        captured = _captured_piece(board, move)
        if captured:
            capturer = _color_name(board.turn)
            captured_by[capturer] += int(captured["value"])
            captures.append(
                {
                    "ply_in_line": len(replayed_uci) + 1,
                    "move": {"uci": move.uci(), "san": san},
                    "by": capturer,
                    "captured": captured,
                }
            )
        replayed_uci.append(move.uci())
        replayed_san.append(san)
        board.push(move)
        if board.is_game_over(claim_draw=False):
            break

    end_snapshot = snapshot(board)
    mover_name = _color_name(mover)
    opponent_name = _color_name(not mover)
    outcome = board.outcome(claim_draw=False)
    return (
        {
            "requested_plies": min(len(line_uci), max_plies),
            "replayed_plies": len(replayed_uci),
            "line_was_legal": invalid_at is None,
            "invalid_at": invalid_at,
            "uci": replayed_uci,
            "san": replayed_san,
            "end_fen": board.fen(),
            "terminal": board.is_game_over(claim_draw=False),
            "outcome": (
                {
                    "winner": _color_name(outcome.winner) if outcome and outcome.winner is not None else None,
                    "termination": outcome.termination.name.lower() if outcome else None,
                }
                if outcome
                else None
            ),
            "material": end_snapshot["material"],
            "material_delta": _material_delta(
                baseline_material, end_snapshot["material"], mover
            ),
            "captures": captures,
            "exchange_net_for_mover": captured_by[mover_name] - captured_by[opponent_name],
            "mobility": end_snapshot["mobility"],
            "king_safety": end_snapshot["king_safety"],
            "hanging_pieces": end_snapshot["safety"]["hanging_pieces"],
        },
        board,
    )


def _reply_facts(after_played: chess.Board, reply_uci: str | None) -> dict:
    checks = _legal_tactical_moves(after_played)
    best_reply: dict | None = None
    if reply_uci:
        try:
            reply = chess.Move.from_uci(reply_uci)
        except ValueError:
            reply = None
        if reply is not None and reply in after_played.legal_moves:
            reply_san = after_played.san(reply)
            reply_is_check = after_played.gives_check(reply)
            reply_is_capture = after_played.is_capture(reply)
            probe = after_played.copy(stack=False)
            probe.push(reply)
            best_reply = {
                "uci": reply.uci(),
                "san": reply_san,
                "is_check": reply_is_check,
                "is_capture": reply_is_capture,
                "captured_piece": _captured_piece(after_played, reply),
                "is_checkmate": probe.is_checkmate(),
            }
    return {
        "checks": checks["checks"],
        "captures": checks["captures"],
        "engine_best_reply": best_reply,
    }


def _fork_targets(after: chess.Board, moved_to: chess.Square, mover: chess.Color) -> list[dict]:
    targets: list[dict] = []
    for square in after.attacks(moved_to):
        piece = after.piece_at(square)
        if piece is None or piece.color == mover:
            continue
        if piece.piece_type == chess.KING or _PIECE_VALUES[piece.piece_type] >= 3:
            targets.append(_piece_ref(after, square))
    return sorted(targets, key=lambda item: item["square"])


def _motif(name: str, statements: list[str], refs: list[str]) -> dict:
    return {"name": name, "evidence": statements, "evidence_refs": refs}


def _detect_motifs(
    critical: dict,
    before: chess.Board,
    played: chess.Move,
    best: chess.Move,
    after_played: chess.Board,
    played_effects: dict,
    played_result: dict,
    best_result: dict,
    replies: dict,
) -> list[dict]:
    motifs: list[dict] = []
    signals = set(critical.get("signals") or [])
    material_gap = (
        best_result["material_delta"]["mover_net"]
        - played_result["material_delta"]["mover_net"]
    )

    if "allowed_mate" in signals:
        reply = replies.get("engine_best_reply") or {}
        detail = f"The played move allows the forced mating continuation"
        if reply.get("san"):
            detail += f" beginning with {reply['san']}"
        motifs.append(
            _motif(
                "allowed_mate",
                [detail + "."],
                ["signals.allowed_mate", "opponent_direct_replies.engine_best_reply"],
            )
        )
    if "missed_mate" in signals:
        motifs.append(
            _motif(
                "missed_mate",
                [f"A forced mate was available with {before.san(best)}, but was not played."],
                ["signals.missed_mate", "best_line_result"],
            )
        )

    new_hanging = played_effects["new_hanging_own_pieces"]
    if new_hanging:
        pieces = ", ".join(f"{item['piece']} on {item['square']}" for item in new_hanging)
        motifs.append(
            _motif(
                "hanging_piece",
                [f"After the played move, the {pieces} can be captured and has no defender."],
                ["move_effects.played.new_hanging_own_pieces"],
            )
        )

    if best != played and before.is_capture(best) and material_gap >= 1:
        captured = _captured_piece(before, best)
        if captured:
            motifs.append(
                _motif(
                    "missed_capture",
                    [
                        f"The engine line starts with {before.san(best)}, capturing the "
                        f"{captured['piece']} on {captured['square']}."
                    ],
                    ["move_effects.best.captured_piece", "deltas.material_delta"],
                )
            )

    best_after = before.copy(stack=False)
    best_after.push(best)
    fork_targets = _fork_targets(best_after, best.to_square, before.turn)
    if best != played and len(fork_targets) >= 2:
        targets = ", ".join(f"{item['piece']} on {item['square']}" for item in fork_targets)
        motifs.append(
            _motif(
                "fork",
                [f"After {before.san(best)}, the moved piece attacks {targets}."],
                ["move_effects.best.fork_targets", "best_line_result"],
            )
        )

    if (
        len(played_result["captures"]) >= 2
        and played_result["material_delta"]["mover_net"] <= -2
        and material_gap >= 2
    ):
        motifs.append(
            _motif(
                "wrong_exchange_sequence",
                [
                    f"The replayed exchange contains {len(played_result['captures'])} captures "
                    f"and leaves the mover {abs(played_result['material_delta']['mover_net'])} "
                    "material points worse than before."
                ],
                ["played_line_result.captures", "deltas.material_delta"],
            )
        )

    reply = replies.get("engine_best_reply") or {}
    direct_reply = reply.get("is_capture") or reply.get("is_check") or reply.get("is_checkmate")
    if direct_reply and (
        material_gap >= 1
        or bool(new_hanging)
        or "allowed_mate" in signals
        or float(critical.get("win_loss") or 0) >= 8.0
    ):
        kinds = [
            label
            for label, active in (
                ("capture", reply.get("is_capture")),
                ("check", reply.get("is_check")),
                ("mate", reply.get("is_checkmate")),
            )
            if active
        ]
        motifs.append(
            _motif(
                "missed_opponent_threat",
                [
                    f"The opponent's engine reply {reply.get('san', reply.get('uci'))} is a "
                    f"direct {'/'.join(kinds)}."
                ],
                ["opponent_direct_replies.engine_best_reply", "played_line_result"],
            )
        )

    order = {name: index for index, name in enumerate(_PRIMARY_ORDER)}
    return sorted(motifs, key=lambda item: order[item["name"]])


def extract_facts(critical: dict, *, line_plies: int = DEFAULT_LINE_PLIES) -> dict:
    """Recompute one complete fact bundle from a Stage 3 critical-position artifact."""
    try:
        before = chess.Board(str(critical["fen_before"]))
        played_uci = str(critical["played_move"]["uci"])
        best_line_uci = list(critical["best_line"]["uci"])
        played_line_uci = list(critical["played_line"]["uci"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FactExtractionError("Critical position is missing a valid FEN or variation.") from exc
    if not best_line_uci:
        raise FactExtractionError("Critical position has no best line.")
    if not played_line_uci or str(played_line_uci[0]) != played_uci:
        raise FactExtractionError("Played variation does not start with the played move.")

    played = _parse_legal_move(before, played_uci, "played")
    best = _parse_legal_move(before, str(best_line_uci[0]), "best")
    mover = before.turn

    before_snapshot = snapshot(before)
    after_played = before.copy(stack=False)
    after_played.push(played)
    after_best = before.copy(stack=False)
    after_best.push(best)
    after_played_snapshot = snapshot(after_played)
    after_best_snapshot = snapshot(after_best)

    played_effects = _move_effects(before, played, before_snapshot, after_played_snapshot)
    best_effects = _move_effects(before, best, before_snapshot, after_best_snapshot)
    played_result, _played_end = _replay_line(
        before, played_line_uci, mover=mover, max_plies=max(1, line_plies)
    )
    best_result, _best_end = _replay_line(
        before, best_line_uci, mover=mover, max_plies=max(1, line_plies)
    )
    if not played_result["line_was_legal"] or not best_result["line_was_legal"]:
        raise FactExtractionError("Critical position contains an illegal Engine variation.")
    reply_uci = ((critical.get("opponent_best_reply") or {}).get("uci"))
    replies = _reply_facts(after_played, reply_uci)

    mover_name = _color_name(mover)
    new_hanging_played = played_effects["new_hanging_own_pieces"]
    new_hanging_best = best_effects["new_hanging_own_pieces"]
    played_material = played_result["material_delta"]["mover_net"]
    best_material = best_result["material_delta"]["mover_net"]
    played_shield = played_result["king_safety"][mover_name]["count"]
    best_shield = best_result["king_safety"][mover_name]["count"]
    played_mobility = played_result["mobility"][mover_name]
    best_mobility = best_result["mobility"][mover_name]

    motifs = _detect_motifs(
        critical,
        before,
        played,
        best,
        after_played,
        played_effects,
        played_result,
        best_result,
        replies,
    )
    names = [item["name"] for item in motifs]
    primary = next((name for name in _PRIMARY_ORDER if name in names), None)
    secondary = [name for name in names if name != primary]
    primary_motif = next((item for item in motifs if item["name"] == primary), None)

    return {
        "critical_id": critical.get("critical_id"),
        "facts_version": FACTS_VERSION,
        "method": "deterministic-python-chess-v1",
        "line_plies": max(1, line_plies),
        "snapshots": {
            "before": before_snapshot,
            "after_played": after_played_snapshot,
            "after_best": after_best_snapshot,
        },
        "move_effects": {"played": played_effects, "best": best_effects},
        "played_line_result": played_result,
        "best_line_result": best_result,
        "deltas": {
            "material_delta": {
                "played_line": played_material,
                "best_line": best_material,
                "best_minus_played": best_material - played_material,
            },
            "exchange_sequence": {
                "played_line": played_result["exchange_net_for_mover"],
                "best_line": best_result["exchange_net_for_mover"],
            },
            "new_hanging_pieces_after_played": new_hanging_played,
            "new_hanging_pieces_after_best": new_hanging_best,
            "resolved_hanging_pieces": {
                "played": played_effects["resolved_hanging_own_pieces"],
                "best": best_effects["resolved_hanging_own_pieces"],
            },
            "king_safety": {
                "metric": "mover_pawn_shield_count_at_line_end",
                "played_line": played_shield,
                "best_line": best_shield,
                "best_minus_played": best_shield - played_shield,
            },
            "activity": {
                "metric": "mover_legal_mobility_at_line_end",
                "played_line": played_mobility,
                "best_line": best_mobility,
                "best_minus_played": best_mobility - played_mobility,
            },
        },
        "opponent_direct_replies": replies,
        "motifs": motifs,
        "primary_category": primary,
        "primary_category_group": _CATEGORY_GROUPS.get(primary) if primary else None,
        "secondary_categories": secondary,
        "classification_evidence": primary_motif["evidence_refs"] if primary_motif else [],
    }

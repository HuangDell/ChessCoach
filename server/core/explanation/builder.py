"""Build a bounded, auditable prompt input for one critical position."""
from __future__ import annotations

import json
from typing import Any

from server import config
from server.core import history
from server.core.explanation.models import ExplanationRequest

PROMPT_VERSION = 1
EXPLANATION_SCHEMA_VERSION = 1

_BASE_EVIDENCE_REFS = (
    "position.classification",
    "position.scores",
    "position.win_percent",
    "position.criticality",
    "variations.multi_pv",
    "variations.played_line",
    "variations.best_line",
    "facts.snapshots",
    "facts.move_effects.played",
    "facts.move_effects.best",
    "facts.played_line_result",
    "facts.best_line_result",
    "facts.deltas.material_delta",
    "facts.deltas.exchange_sequence",
    "facts.deltas.king_safety",
    "facts.deltas.activity",
    "facts.opponent_direct_replies",
    "facts.motifs",
)


class ExplanationInputError(ValueError):
    """Raised when an Engine artifact lacks the facts required for an explanation."""


def _display_move(move_number: int, side: str, san: str) -> str:
    prefix = f"{move_number}." if side == "white" else f"{move_number}..."
    return f"{prefix}{san}"


def _relevant_history(analysis: dict, facts: dict) -> dict | None:
    """Return only aggregate motifs related to this position, never raw historical games."""
    if not config.PERSONALIZE_HISTORY:
        return None
    categories = {
        str(category)
        for category in [facts.get("primary_category"), *(facts.get("secondary_categories") or [])]
        if category
    }
    if not categories:
        return None
    try:
        player_id, _platform, _name = history.resolve_identity(
            analysis.get("headers") or {}, str(analysis.get("review_side") or "")
        )
        profile = history.get_profile(player_id)
    except Exception:
        return None
    related: dict[str, Any] = {"player_id": player_id}
    for scope in ("recent", "lifetime"):
        aggregate = profile.get(scope) or {}
        matches = [
            item
            for item in aggregate.get("top_motifs") or []
            if item.get("motif") in categories
        ][:3]
        if matches:
            related[scope] = {"games": aggregate.get("games"), "matching_motifs": matches}
    return related if len(related) > 1 else None


def _allowed_refs(facts: dict) -> list[str]:
    refs = set(_BASE_EVIDENCE_REFS)
    for motif in facts.get("motifs") or []:
        refs.update(str(ref) for ref in motif.get("evidence_refs") or [] if ref)
    # Fact Extractor motif refs are relative to the facts object. Prefix them so every reference
    # points into the exact JSON payload the model sees.
    normalized = {
        ref if ref.startswith(("position.", "variations.", "facts.")) else f"facts.{ref}"
        for ref in refs
    }
    return sorted(normalized)


def build_request(analysis: dict, critical: dict) -> ExplanationRequest:
    facts = critical.get("facts")
    if not isinstance(facts, dict):
        raise ExplanationInputError(
            f"Critical position {critical.get('critical_id', '?')} has no extracted facts."
        )
    critical_id = str(critical.get("critical_id") or "").strip()
    if not critical_id:
        raise ExplanationInputError("Critical position has no stable critical_id.")
    side = str(critical.get("side") or "")
    move_number = int(critical.get("move_number") or 0)
    played_san = str((critical.get("played_move") or {}).get("san") or "")
    best_san = str(((critical.get("best_line") or {}).get("san") or [""])[0])
    if side not in {"white", "black"} or move_number < 1 or not played_san or not best_san:
        raise ExplanationInputError(f"Critical position {critical_id} has incomplete move data.")

    stage_move = next(
        (
            move
            for move in analysis.get("moves") or []
            if int(move.get("ply") or -1) == int(critical.get("ply") or -2)
        ),
        {},
    )
    candidates = []
    for item in (critical.get("candidates") or [])[:3]:
        candidates.append(
            {
                "rank": item.get("rank"),
                "move": item.get("move"),
                "scores": item.get("scores"),
                "win_percent": item.get("win_percent"),
                "win_gap_from_best": item.get("win_gap_from_best"),
                "line": item.get("line"),
            }
        )

    expected = {
        "critical_id": critical_id,
        "played_move": _display_move(move_number, side, played_san),
        "recommended_move": _display_move(move_number, side, best_san),
        "primary_category": facts.get("primary_category"),
        "secondary_categories": list(facts.get("secondary_categories") or []),
    }
    payload: dict[str, Any] = {
        "analysis_version": analysis.get("schema_version"),
        "player": {
            "review_side": analysis.get("review_side"),
            "rating": (analysis.get("profile") or {}).get("review", {}).get("elo"),
            "rating_source": "normalized_review_elo",
        },
        "position": {
            "critical_id": critical_id,
            "ply": critical.get("ply"),
            "move_number": move_number,
            "side": side,
            "fen_before": critical.get("fen_before"),
            "played_move": critical.get("played_move"),
            "best_move": (candidates[0].get("move") if candidates else None),
            "classification": critical.get("classification"),
            "scores": critical.get("scores"),
            "win_percent": {
                "before": stage_move.get("win_percent_before"),
                "after": stage_move.get("win_percent_after"),
                "loss_for_mover": critical.get("win_loss"),
            },
            "criticality": critical.get("criticality"),
        },
        "variations": {
            "multi_pv": candidates,
            "played_line": critical.get("played_line"),
            "best_line": critical.get("best_line"),
        },
        "facts": {
            "facts_version": facts.get("facts_version"),
            "signals": critical.get("signals") or [],
            "snapshots": facts.get("snapshots"),
            "move_effects": facts.get("move_effects"),
            "played_line_result": facts.get("played_line_result"),
            "best_line_result": facts.get("best_line_result"),
            "deltas": facts.get("deltas"),
            "opponent_direct_replies": facts.get("opponent_direct_replies"),
            "motifs": facts.get("motifs"),
            "primary_category": facts.get("primary_category"),
            "secondary_categories": facts.get("secondary_categories") or [],
            "classification_evidence": facts.get("classification_evidence") or [],
        },
    }
    related_history = _relevant_history(analysis, facts)
    if related_history:
        payload["related_history"] = related_history

    allowed_refs = _allowed_refs(facts)
    if config.EXPLANATION_LANGUAGE == "en":
        system_prompt = (
            "You are a chess review coach. Turn only the supplied Engine results and deterministic "
            "Chess Facts into concise, concrete English instruction. Engine/Facts are authoritative: "
            "do not recalculate or guess moves, alter classifications or categories, or invent tactics. "
            "Use only the supplied played_line, best_line, and multi_pv variations. State what the user "
            "missed before explaining the recommendation. Describe board reasons rather than saying "
            "'the engine says', never output hidden reasoning, and be conservative when evidence is "
            "limited. why_it_looked_reasonable may describe only an observable intent. For "
            "multiple_good_moves, say the first choice need not be memorized; for only_move, name the "
            "functions it uniquely combines. Return one JSON object and no Markdown fence."
        )
        short_text = "short English string"
        core_text = "required; state the missed problem first"
        reason_text = "1-5 concrete evidence-grounded reasons"
        principle_text = "required transferable principle"
        checklist_text = "1-6 checks for next time"
    else:
        system_prompt = (
            "你是国际象棋复盘教练。你只负责把给定的 Engine 结果和确定性 Chess Facts 转成简洁、"
            "具体的简体中文教学解释。Engine/Facts 是唯一事实来源：不要重新计算或猜测最佳着，不要"
            "改变 classification 或错误分类，不要声称输入 JSON 中不存在的战术；所有变化只能来自"
            "给定的合法 played_line、best_line 或 multi_pv。先说明用户漏看了什么，再解释推荐着。"
            "不要用‘引擎说’代替棋盘上的具体原因，不要输出思维过程。事实不足时明确保守表述，不要"
            "编造用户心理；why_it_looked_reasonable 只能描述这步棋表面上可观察的意图。"
            "criticality=multiple_good_moves 时说明无需死记第一选择；criticality=only_move 时说明"
            "最佳着具体同时承担了哪些功能。只输出一个 JSON 对象，不要 Markdown 代码围栏。"
        )
        short_text = "简短中文字符串"
        core_text = "必填，先指出漏看的问题"
        reason_text = "1-5 条有事实依据的具体原因"
        principle_text = "必填，可迁移棋理"
        checklist_text = "1-6 个下次检查项"
    played_line_text = " ".join(str(move) for move in (critical.get("played_line") or {}).get("san") or [])
    best_line_text = " ".join(str(move) for move in (critical.get("best_line") or {}).get("san") or [])
    output_shape = {
        **expected,
        "why_it_looked_reasonable": short_text,
        "core_problem": core_text,
        "why_recommended": [reason_text],
        "played_line_summary": f"必须以原样 SAN 行‘{played_line_text}’开头，再概括结果",
        "best_line_summary": f"必须以原样 SAN 行‘{best_line_text}’开头，再概括结果",
        "transferable_principle": principle_text,
        "next_time_checklist": [checklist_text],
        "evidence_refs": ["1-8 个 allowed_evidence_refs 中的精确值"],
    }
    if config.EXPLANATION_LANGUAGE == "en":
        user_prompt = (
            "Explain this one critical position. Do not cite the full game or any history that is "
            "not supplied.\n\n"
            f"Fields to preserve exactly:\n{json.dumps(expected, ensure_ascii=False, sort_keys=True)}\n\n"
            f"Allowed evidence paths:\n{json.dumps(allowed_refs, ensure_ascii=False)}\n\n"
            "Required output shape (all fields required; no extra fields):\n"
            f"{json.dumps(output_shape, ensure_ascii=False, indent=2)}\n\n"
            f"Only input JSON:\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )
    else:
        user_prompt = (
            "请基于下面这一处关键局面生成解释。不得引用整盘棋或未提供的历史。\n\n"
            f"必须原样使用的字段：\n{json.dumps(expected, ensure_ascii=False, sort_keys=True)}\n\n"
            f"允许引用的证据路径：\n{json.dumps(allowed_refs, ensure_ascii=False)}\n\n"
            f"输出结构（字段必须齐全且不能增加字段）：\n"
            f"{json.dumps(output_shape, ensure_ascii=False, indent=2)}\n\n"
            f"唯一输入 JSON：\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )
    return ExplanationRequest(
        critical_id=critical_id,
        language=config.EXPLANATION_LANGUAGE,
        prompt_version=PROMPT_VERSION,
        payload=payload,
        expected=expected,
        allowed_evidence_refs=allowed_refs,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

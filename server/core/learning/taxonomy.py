"""Versioned canonical skill taxonomy and deterministic evidence mappings.

This module maps only verified, structured chess artifacts. Unknown aliases,
unrecognized puzzle metadata, and incomplete composite evidence produce no
learning signal.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import chess

from server.core.agent.models import LearningEvidenceType, SkillDefinition
from server.core.evaluation import time_control_clock


TAXONOMY_VERSION = 1

FACT_MOTIF: LearningEvidenceType = "fact_motif"
FACT_COMPOSITE: LearningEvidenceType = "fact_composite"
ATTEMPT_OUTCOME: LearningEvidenceType = "attempt_outcome"
PUZZLE_THEME: LearningEvidenceType = "puzzle_theme"
CLOCK_OUTCOME: LearningEvidenceType = "clock_outcome"
EVIDENCE_TYPES: tuple[LearningEvidenceType, ...] = (
    FACT_MOTIF,
    FACT_COMPOSITE,
    ATTEMPT_OUTCOME,
    PUZZLE_THEME,
    CLOCK_OUTCOME,
)


class TaxonomyError(ValueError):
    """Base error for unsupported or internally inconsistent taxonomy data."""


class TaxonomyVersionError(TaxonomyError):
    """Raised when a caller requests an unavailable taxonomy version."""


class AmbiguousSkillAliasError(TaxonomyError):
    """Raised instead of guessing when one alias identifies multiple skills."""


@dataclass(frozen=True, slots=True)
class EvidenceMapping:
    skill_id: str
    evidence_type: LearningEvidenceType
    evidence_refs: tuple[str, ...] = ()


SKILL_DEFINITIONS: tuple[SkillDefinition, ...] = (
    SkillDefinition(
        taxonomy_version=1,
        skill_id="tactics.fork_detection",
        parent_id="tactics",
        label="Fork detection",
        description="Recognize or complete a verified fork.",
        supported_evidence_types=[FACT_MOTIF, PUZZLE_THEME, ATTEMPT_OUTCOME],
        aliases=["fork"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="tactics.mating_threat_detection",
        parent_id="tactics",
        label="Mating threat detection",
        description="Recognize forced mating opportunities and threats for either side.",
        supported_evidence_types=[FACT_MOTIF, PUZZLE_THEME, ATTEMPT_OUTCOME],
        aliases=["mate", "forced_mate", "allowed_mate", "missed_mate"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="tactics.loose_piece_awareness",
        parent_id="tactics",
        label="Loose piece awareness",
        description="Recognize undefended or hanging pieces.",
        supported_evidence_types=[FACT_MOTIF, PUZZLE_THEME, ATTEMPT_OUTCOME],
        aliases=["hanging_piece", "hangingPiece"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="calculation.opponent_forcing_moves",
        parent_id="calculation",
        label="Opponent forcing moves",
        description="Check the opponent's forcing checks, captures, and threats.",
        supported_evidence_types=[FACT_MOTIF, ATTEMPT_OUTCOME],
        aliases=["missed_opponent_threat", "opponent_threat"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="calculation.exchange_sequence",
        parent_id="calculation",
        label="Exchange sequence",
        description="Calculate complete exchanges and recaptures.",
        supported_evidence_types=[FACT_MOTIF, ATTEMPT_OUTCOME],
        aliases=["wrong_exchange_sequence", "exchange_sequence"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="calculation.candidate_moves",
        parent_id="calculation",
        label="Candidate moves",
        description="Scan forcing candidate moves before committing.",
        supported_evidence_types=[FACT_MOTIF, ATTEMPT_OUTCOME],
        aliases=["missed_capture"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="strategy.king_safety",
        parent_id="strategy",
        label="King safety",
        description="Evaluate concrete changes to king safety.",
        supported_evidence_types=[FACT_COMPOSITE, ATTEMPT_OUTCOME],
        aliases=["king_safety"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="strategy.piece_activity",
        parent_id="strategy",
        label="Piece activity",
        description="Recognize meaningful changes in piece mobility and activity.",
        supported_evidence_types=[FACT_COMPOSITE, ATTEMPT_OUTCOME],
        aliases=["piece_activity", "activity"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="opening.development",
        parent_id="opening",
        label="Opening development",
        description="Complete basic minor-piece development and king safety.",
        supported_evidence_types=[FACT_COMPOSITE, ATTEMPT_OUTCOME],
        aliases=["development"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="endgame.conversion",
        parent_id="endgame",
        label="Endgame conversion",
        description="Convert a verified endgame advantage into a win.",
        supported_evidence_types=[FACT_COMPOSITE, ATTEMPT_OUTCOME],
        aliases=["conversion"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="practical.blunder_check",
        parent_id="practical",
        label="Blunder check",
        description="Run a final check for loose pieces, mate, and forcing replies.",
        supported_evidence_types=[FACT_COMPOSITE, ATTEMPT_OUTCOME],
        aliases=["blunder_check"],
    ),
    SkillDefinition(
        taxonomy_version=1,
        skill_id="practical.time_management",
        parent_id="practical",
        label="Time management",
        description="Allocate time in proportion to verified clock and position demands.",
        supported_evidence_types=[CLOCK_OUTCOME],
        aliases=["time_management"],
    ),
)


def _normalized_alias(value: str) -> str:
    return "".join(character for character in value.strip().casefold() if character.isalnum())


def build_alias_index(definitions: Sequence[SkillDefinition]) -> Mapping[str, str]:
    """Build an alias index, rejecting collisions rather than choosing an owner."""

    index: dict[str, str] = {}
    for definition in definitions:
        if definition.taxonomy_version != TAXONOMY_VERSION:
            raise TaxonomyVersionError("definition does not belong to taxonomy v1")
        for value in (definition.skill_id, *definition.aliases):
            alias = _normalized_alias(value)
            if not alias:
                raise TaxonomyError("skill aliases must not normalize to an empty value")
            owner = index.get(alias)
            if owner is not None and owner != definition.skill_id:
                raise AmbiguousSkillAliasError(
                    f"alias {value!r} identifies both {owner!r} and {definition.skill_id!r}"
                )
            index[alias] = definition.skill_id
    return MappingProxyType(index)


_DEFINITION_INDEX: Mapping[str, SkillDefinition] = MappingProxyType(
    {definition.skill_id: definition for definition in SKILL_DEFINITIONS}
)
ALIAS_INDEX = build_alias_index(SKILL_DEFINITIONS)

# Registry entries describe the rename map applied while entering that version.
# V1 is the baseline and therefore has no predecessor rename step.
MIGRATION_REGISTRY: Mapping[int, Mapping[str, str]] = MappingProxyType(
    {TAXONOMY_VERSION: MappingProxyType({})}
)


def _require_current_version(taxonomy_version: int) -> None:
    if taxonomy_version != TAXONOMY_VERSION:
        raise TaxonomyVersionError(
            f"taxonomy version {taxonomy_version} is unavailable; expected {TAXONOMY_VERSION}"
        )


def get_skill_definition(
    skill_id: str,
    taxonomy_version: int = TAXONOMY_VERSION,
) -> SkillDefinition | None:
    _require_current_version(taxonomy_version)
    return _DEFINITION_INDEX.get(skill_id.strip())


def resolve_skill_id(
    value: str,
    taxonomy_version: int = TAXONOMY_VERSION,
) -> str | None:
    """Resolve a canonical ID or known alias; unknown values remain unknown."""

    _require_current_version(taxonomy_version)
    normalized = _normalized_alias(value)
    return ALIAS_INDEX.get(normalized) if normalized else None


def migrate_skill_id(
    skill_id: str,
    from_version: int,
    to_version: int = TAXONOMY_VERSION,
) -> str | None:
    """Migrate only through explicitly registered adjacent taxonomy versions."""

    if from_version < 1 or to_version < 1 or from_version > TAXONOMY_VERSION:
        raise TaxonomyVersionError("taxonomy migration version is unavailable")
    if to_version > TAXONOMY_VERSION or from_version > to_version:
        raise TaxonomyVersionError("taxonomy migration direction is unavailable")
    current = skill_id.strip()
    for version in range(from_version + 1, to_version + 1):
        step = MIGRATION_REGISTRY.get(version)
        if step is None:
            raise TaxonomyVersionError(f"taxonomy migration into v{version} is unavailable")
        current = step.get(current, current)
    return resolve_skill_id(current, taxonomy_version=to_version)


def supports_evidence_type(skill_id: str, evidence_type: str) -> bool:
    definition = get_skill_definition(skill_id)
    return definition is not None and evidence_type in definition.supported_evidence_types


_DIRECT_FACT_SKILLS: Mapping[str, str] = MappingProxyType(
    {
        "fork": "tactics.fork_detection",
        "allowed_mate": "tactics.mating_threat_detection",
        "missed_mate": "tactics.mating_threat_detection",
        "forced_mate": "tactics.mating_threat_detection",
        "mate": "tactics.mating_threat_detection",
        "hanging_piece": "tactics.loose_piece_awareness",
        "missed_opponent_threat": "calculation.opponent_forcing_moves",
        "opponent_threat": "calculation.opponent_forcing_moves",
        "wrong_exchange_sequence": "calculation.exchange_sequence",
        "exchange_sequence": "calculation.exchange_sequence",
        "missed_capture": "calculation.candidate_moves",
    }
)
_BLUNDER_CHECK_FACTS = frozenset(
    {"allowed_mate", "missed_mate", "forced_mate", "hanging_piece", "missed_opponent_threat"}
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _facts_for(position_or_facts: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = position_or_facts.get("facts")
    return _mapping(nested) if isinstance(nested, Mapping) else position_or_facts


def _fact_names(position: Mapping[str, Any], facts: Mapping[str, Any]) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    names: list[str] = []
    evidence: dict[str, tuple[str, ...]] = {}

    def add(raw_name: Any, refs: Iterable[Any] = ()) -> None:
        name = str(raw_name or "").strip()
        if name not in _DIRECT_FACT_SKILLS or name in names:
            return
        names.append(name)
        cleaned = tuple(dict.fromkeys(str(ref).strip() for ref in refs if str(ref).strip()))
        evidence[name] = cleaned

    add(facts.get("primary_category"), ("facts.primary_category",))
    for name in _sequence(facts.get("secondary_categories")):
        add(name, ("facts.secondary_categories",))
    for motif in _sequence(facts.get("motifs")):
        item = _mapping(motif)
        if item.get("verified") is False or item.get("confidence") in {"low", "speculative"}:
            continue
        add(item.get("name"), _sequence(item.get("evidence_refs")))
    for signal in _sequence(position.get("signals")):
        add(signal, (f"signals.{str(signal).strip()}",))
    return names, evidence


def map_fact_evidence(position: Mapping[str, Any]) -> list[EvidenceMapping]:
    """Map verified direct facts and the conservative blunder-check composite."""

    facts = _facts_for(position)
    names, evidence_by_name = _fact_names(position, facts)
    mappings: list[EvidenceMapping] = []
    seen_skills: set[str] = set()
    for name in names:
        skill_id = _DIRECT_FACT_SKILLS[name]
        if skill_id in seen_skills:
            continue
        mappings.append(EvidenceMapping(skill_id, FACT_MOTIF, evidence_by_name.get(name, ())))
        seen_skills.add(skill_id)
    if str(position.get("classification") or "").strip().casefold() == "blunder" and any(
        name in _BLUNDER_CHECK_FACTS for name in names
    ):
        mappings.append(
            EvidenceMapping(
                "practical.blunder_check",
                FACT_COMPOSITE,
                ("classification", "facts.motifs"),
            )
        )
    return mappings


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _critical_loss_verified(position: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    loss = _number(position.get("win_loss"))
    critical = _mapping(_mapping(analysis.get("profile")).get("critical"))
    thresholds = [
        number
        for number in (_number(value) for value in _sequence(critical.get("thresholds")))
        if number is not None and number > 0
    ]
    return loss is not None and bool(thresholds) and loss >= min(thresholds)


def _phase_name(facts: Mapping[str, Any]) -> str | None:
    before = _mapping(_mapping(facts.get("snapshots")).get("before"))
    phase = _mapping(before.get("phase"))
    name = str(phase.get("name") or "").strip().casefold()
    return name or None


def _opening_development_failure(position: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    if _phase_name(facts) != "opening":
        return False
    before = _mapping(_mapping(facts.get("snapshots")).get("before"))
    fen = str(before.get("fen") or position.get("fen_before") or "").strip()
    try:
        board = chess.Board(fen)
    except ValueError:
        return False
    mover_name = str(position.get("side") or before.get("turn") or "").strip().casefold()
    if mover_name not in {"white", "black"}:
        return False
    mover = chess.WHITE if mover_name == "white" else chess.BLACK
    home_names = ("b1", "c1", "f1", "g1") if mover else ("b8", "c8", "f8", "g8")
    home_squares = {chess.parse_square(name) for name in home_names}
    home_minors = sum(
        1
        for square in home_squares
        if (piece := board.piece_at(square)) is not None
        and piece.color == mover
        and piece.piece_type in {chess.KNIGHT, chess.BISHOP}
    )
    king = board.king(mover)
    king_safe = king in {
        chess.G1 if mover else chess.G8,
        chess.C1 if mover else chess.C8,
    }
    if home_minors < 2 and king_safe:
        return False

    effects = _mapping(facts.get("move_effects"))
    played = _mapping(effects.get("played"))
    best = _mapping(effects.get("best"))

    def improves(effect: Mapping[str, Any]) -> bool:
        if effect.get("is_castle") is True:
            return True
        moved = _mapping(effect.get("moved_piece"))
        return (
            str(moved.get("piece") or "") in {"knight", "bishop"}
            and str(effect.get("from") or "") in home_names
        )

    return improves(best) and not improves(played)


def _reviewer_did_not_win(
    analysis: Mapping[str, Any],
    history_record: Mapping[str, Any],
) -> bool:
    side = str(analysis.get("review_side") or "").strip().casefold()
    if side not in {"white", "black"}:
        return False

    result_values = [
        str(value).strip()
        for value in (
            analysis.get("result"),
            _mapping(analysis.get("headers")).get("Result"),
        )
        if value is not None and str(value).strip()
    ]
    valid_results = {"1-0", "0-1", "1/2-1/2"}
    if result_values:
        if any(value not in valid_results for value in result_values):
            return False
        distinct_results = set(result_values)
        if len(distinct_results) != 1:
            return False
        result = distinct_results.pop()
    else:
        historical_result = str(history_record.get("player_result") or "").strip().casefold()
        if historical_result not in {"win", "loss", "draw"}:
            return False
        return historical_result != "win"

    return result == "1/2-1/2" or (result == "1-0") != (side == "white")


def _has_verified_clock_failure(position: Mapping[str, Any], analysis: Mapping[str, Any]) -> bool:
    if str(position.get("classification") or "").strip().casefold() not in {
        "inaccuracy",
        "mistake",
        "blunder",
    }:
        return False
    headers = _mapping(analysis.get("headers"))
    clock = time_control_clock(str(headers.get("TimeControl") or ""))
    if clock is None:
        return False
    ply_number = position.get("ply")
    try:
        ply = int(ply_number)
    except (TypeError, ValueError):
        return False
    moves = [_mapping(move) for move in _sequence(analysis.get("moves"))]
    current = next((move for move in moves if move.get("ply") == ply), None)
    if current is None:
        return False
    current_clock = _number(current.get("clock_seconds"))
    if current_clock is None or current_clock < 0:
        return False
    previous = next((move for move in moves if move.get("ply") == ply - 2), None)
    previous_clock = _number(previous.get("clock_seconds")) if previous is not None else clock[0]
    if previous_clock is None:
        return False
    return previous_clock - current_clock + clock[1] >= 0


def map_composite_evidence(
    position: Mapping[str, Any],
    *,
    analysis: Mapping[str, Any] | None = None,
    history_record: Mapping[str, Any] | None = None,
) -> list[EvidenceMapping]:
    """Map only composite claims whose complete evidence is present."""

    artifact = analysis or {}
    history = history_record or {}
    facts = _facts_for(position)
    mappings: list[EvidenceMapping] = []
    if _critical_loss_verified(position, artifact):
        deltas = _mapping(facts.get("deltas"))
        king_safety = _mapping(deltas.get("king_safety"))
        activity = _mapping(deltas.get("activity"))
        if (_number(king_safety.get("best_minus_played")) or 0) >= 1:
            mappings.append(
                EvidenceMapping(
                    "strategy.king_safety",
                    FACT_COMPOSITE,
                    ("win_loss", "facts.deltas.king_safety"),
                )
            )
        if (_number(activity.get("best_minus_played")) or 0) >= 5:
            mappings.append(
                EvidenceMapping(
                    "strategy.piece_activity",
                    FACT_COMPOSITE,
                    ("win_loss", "facts.deltas.activity"),
                )
            )
    if _opening_development_failure(position, facts):
        mappings.append(
            EvidenceMapping(
                "opening.development",
                FACT_COMPOSITE,
                ("facts.snapshots.before.phase", "facts.move_effects"),
            )
        )
    signals = {str(value).strip() for value in _sequence(position.get("signals"))}
    if (
        _phase_name(facts) == "endgame"
        and signals.intersection({"missed_win", "winning_to_equal"})
        and _reviewer_did_not_win(artifact, history)
    ):
        mappings.append(
            EvidenceMapping(
                "endgame.conversion",
                FACT_COMPOSITE,
                ("facts.snapshots.before.phase", "signals", "result"),
            )
        )
    if _has_verified_clock_failure(position, artifact):
        mappings.append(
            EvidenceMapping(
                "practical.time_management",
                CLOCK_OUTCOME,
                ("classification", "headers.TimeControl", "moves.clock_seconds"),
            )
        )
    return mappings


def map_analysis_position(
    analysis: Mapping[str, Any],
    position: Mapping[str, Any],
    *,
    history_record: Mapping[str, Any] | None = None,
) -> list[EvidenceMapping]:
    """Return all deterministic mappings for one position without duplicates."""

    combined = [
        *map_fact_evidence(position),
        *map_composite_evidence(position, analysis=analysis, history_record=history_record),
    ]
    seen: set[tuple[str, str]] = set()
    output: list[EvidenceMapping] = []
    for item in combined:
        identity = (item.skill_id, item.evidence_type)
        if identity not in seen:
            output.append(item)
            seen.add(identity)
    return output


def map_attempt_category(category: str) -> EvidenceMapping | None:
    """Map only an owning position's verified category or canonical skill."""

    canonical = resolve_skill_id(category)
    if canonical is None or not supports_evidence_type(canonical, ATTEMPT_OUTCOME):
        return None
    return EvidenceMapping(canonical, ATTEMPT_OUTCOME)


_LICHESS_THEME_SKILLS: Mapping[str, str] = MappingProxyType(
    {
        "fork": "tactics.fork_detection",
        "hangingPiece": "tactics.loose_piece_awareness",
        "mateIn1": "tactics.mating_threat_detection",
        "mateIn2": "tactics.mating_threat_detection",
        "mateIn3": "tactics.mating_threat_detection",
        "mateIn4": "tactics.mating_threat_detection",
        "mateIn5": "tactics.mating_threat_detection",
        "anastasiaMate": "tactics.mating_threat_detection",
        "arabianMate": "tactics.mating_threat_detection",
        "backRankMate": "tactics.mating_threat_detection",
        "bodenMate": "tactics.mating_threat_detection",
        "doubleBishopMate": "tactics.mating_threat_detection",
        "dovetailMate": "tactics.mating_threat_detection",
        "hookMate": "tactics.mating_threat_detection",
        "smotheredMate": "tactics.mating_threat_detection",
    }
)


def map_lichess_themes(themes: Iterable[str]) -> list[EvidenceMapping]:
    """Map the explicit V1 whitelist; metadata and unknown themes are ignored."""

    output: list[EvidenceMapping] = []
    seen_skills: set[str] = set()
    for theme in themes:
        value = theme.strip() if isinstance(theme, str) else ""
        skill_id = _LICHESS_THEME_SKILLS.get(value)
        if skill_id is None or skill_id in seen_skills:
            continue
        output.append(EvidenceMapping(skill_id, PUZZLE_THEME, (f"themes.{value}",)))
        seen_skills.add(skill_id)
    return output

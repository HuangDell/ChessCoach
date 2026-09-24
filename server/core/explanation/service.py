"""Explanation orchestration: build input, call one provider, validate, and cache."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import threading
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from server import config

from server.core.explanation.builder import (
    EXPLANATION_SCHEMA_VERSION,
    ExplanationInputError,
    build_request,
)
from server.core.explanation.models import Explanation, ExplanationRequest
from server.core.knowledge import KnowledgeRetriever
from server.core.knowledge.tracing import knowledge_trace_context
from server.core.learning.taxonomy import resolve_skill_id
from server.core.explanation.providers import (
    ExplanationProvider,
    ExplanationProviderError,
    ProviderInfo,
    configured_provider,
)
from server.core.storage import (
    GameNotFoundError,
    load_analysis,
    load_explanations,
    store_explanations,
)


logger = logging.getLogger("chesscoach.explanation")


class ExplanationError(RuntimeError):
    """A generation, grounding, or persistence error safe to return through the local API."""

    def __init__(self, message: str, *, reason: str = "generation_failed", http_status: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.http_status = http_status


class ExplanationNotFoundError(ExplanationError):
    pass


_locks_guard = threading.Lock()
_locks: dict[tuple[str, str], threading.Lock] = {}


def _lock_for(game_id: str, review_side: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((game_id, review_side), threading.Lock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_hash(request: ExplanationRequest, provider: ProviderInfo) -> str:
    content = {
        "language": request.language,
        "prompt_version": request.prompt_version,
        "payload": request.payload,
        "expected": request.expected,
        "allowed_evidence_refs": request.allowed_evidence_refs,
        "system_prompt": request.system_prompt,
        "user_prompt": request.user_prompt,
        "provider": provider.provider,
        "model": provider.model,
        "knowledge_status": request.knowledge_status,
        "knowledge_index_fingerprint": request.knowledge_index_fingerprint,
        "knowledge_citations": request.knowledge_citations,
    }
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```") and raw.endswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start < 0:
            raise ExplanationError("The model did not return a JSON object.")
        try:
            value, _end = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError as exc:
            raise ExplanationError("The model returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise ExplanationError("The model response must be one JSON object.")
    return value


def _validated_explanation(text: str, request: ExplanationRequest) -> Explanation:
    try:
        explanation = Explanation.model_validate(_json_object(text))
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc") or []) or "response"
        raise ExplanationError(
            f"The model response failed the explanation schema at {location}: "
            f"{first.get('msg', 'invalid value')}."
        ) from exc

    expected = request.expected
    for field in ("critical_id", "played_move", "recommended_move", "primary_category"):
        if getattr(explanation, field) != expected.get(field):
            raise ExplanationError(f"The model changed the authoritative field '{field}'.")
    if explanation.secondary_categories != expected.get("secondary_categories", []):
        raise ExplanationError("The model changed the authoritative secondary_categories.")
    played_line = " ".join(
        str(move)
        for move in (request.payload.get("variations", {}).get("played_line") or {}).get("san") or []
    )
    best_line = " ".join(
        str(move)
        for move in (request.payload.get("variations", {}).get("best_line") or {}).get("san") or []
    )
    if not played_line or not explanation.played_line_summary.startswith(played_line):
        raise ExplanationError("played_line_summary did not preserve the supplied legal SAN line.")
    if not best_line or not explanation.best_line_summary.startswith(best_line):
        raise ExplanationError("best_line_summary did not preserve the supplied legal SAN line.")
    allowed = set(request.allowed_evidence_refs)
    unknown = [ref for ref in explanation.evidence_refs if ref not in allowed]
    if unknown:
        raise ExplanationError(f"The model cited unknown evidence: {', '.join(unknown)}.")
    return explanation


def _empty_artifact(analysis: dict, provider: ProviderInfo, prompt_version: int, language: str) -> dict:
    return {
        "schema_version": EXPLANATION_SCHEMA_VERSION,
        "game_id": analysis["game_id"],
        "review_side": analysis["review_side"],
        "analysis_version": analysis.get("schema_version"),
        "analysis_cache_key": analysis.get("cache_key"),
        "provider": provider.provider,
        "model": provider.model,
        "prompt_version": prompt_version,
        "language": language,
        "positions": [],
    }


def _compatible(artifact: dict, template: dict) -> bool:
    return all(
        artifact.get(key) == template.get(key)
        for key in (
            "schema_version",
            "game_id",
            "review_side",
            "analysis_version",
            "analysis_cache_key",
            "provider",
            "model",
            "prompt_version",
            "language",
        )
    )


def _replace_position(artifact: dict, position: dict, order: dict[str, int]) -> None:
    positions = [
        item
        for item in artifact.get("positions") or []
        if item.get("critical_id") != position["critical_id"]
    ]
    positions.append(position)
    positions.sort(key=lambda item: order.get(str(item.get("critical_id")), 10**9))
    artifact["positions"] = positions


def _knowledge_query(critical: dict) -> tuple[str, list[str]]:
    facts = critical.get("facts") or {}
    primary = str(facts.get("primary_category") or "").strip()
    secondary = [str(item) for item in facts.get("secondary_categories") or []]
    signals = [
        str(item.get("name") or item.get("id") or "") if isinstance(item, dict) else str(item)
        for item in critical.get("signals") or []
    ]
    played = str((critical.get("played_move") or {}).get("san") or "")
    recommended = str(((critical.get("best_line") or {}).get("san") or [""])[0])
    classification = str(critical.get("classification") or "")
    parts = [
        f"Chess lesson for a {classification} move.",
        f"Primary category: {primary}." if primary else "",
        f"Secondary categories: {', '.join(secondary)}." if secondary else "",
        f"Verified signals: {', '.join(filter(None, signals))}." if any(signals) else "",
        f"Played move: {played}; recommended move: {recommended}.",
    ]
    skill_ids = []
    for value in [primary, *secondary, *signals]:
        resolved = resolve_skill_id(value)
        if resolved and resolved not in skill_ids:
            skill_ids.append(resolved)
    return " ".join(item for item in parts if item), skill_ids[:5]


def _retrieve_knowledge(retriever: KnowledgeRetriever | None, critical: dict) -> tuple[dict, str, str, list[dict]]:
    if retriever is None:
        return {}, "unavailable", "", []
    query, skill_ids = _knowledge_query(critical)
    try:
        result = retriever.search(query, skill_ids=skill_ids, limit=3)
    except Exception:  # noqa: BLE001 - optional RAG cannot block Engine-facts explanations
        return {}, "unavailable", "", []
    passages = [
        {
            "passage_id": item.passage_id,
            "text": item.text,
            "text_hash": item.text_hash,
            "citation": {
                "citation_id": item.citation.citation_id,
                "book_id": item.citation.book_id,
                "title": item.citation.title,
                "author": item.citation.author,
                "heading": item.citation.heading,
                "source_locator": item.citation.source_locator,
                **({"source_url": item.citation.source_url} if item.citation.source_url else {}),
            },
        }
        for item in result.passages[:3]
    ]
    citations = [item["citation"] for item in passages]
    return {"passages": passages}, result.status, result.index_fingerprint, citations


def generate_explanations(
    game_id: str,
    *,
    review_side: str | None = None,
    critical_id: str | None = None,
    force: bool = False,
    provider: ExplanationProvider | None = None,
    knowledge_retriever: KnowledgeRetriever | None = None,
) -> dict:
    """Own the terminal outcome log, including preparation and persistence failures."""
    started = time.monotonic()
    progress = {"stage": "input", "critical_id": critical_id or "all"}
    if config.DEBUG:
        logger.debug("event=explanation_started game=%s critical=%s", game_id, progress["critical_id"])
    try:
        result = _generate_explanations(
            game_id, review_side=review_side, critical_id=critical_id, force=force,
            provider=provider, knowledge_retriever=knowledge_retriever, progress=progress,
        )
    except Exception as exc:
        logger.warning(
            "event=explanation_failed game=%s critical=%s stage=%s reason=%s http_status=%s exception_type=%s duration_ms=%s",
            game_id, progress["critical_id"], progress["stage"],
            getattr(exc, "reason", "internal_error"), getattr(exc, "http_status", None),
            type(exc).__name__, round((time.monotonic() - started) * 1000),
        )
        raise
    for error in result["errors"]:
        logger.warning(
            "event=explanation_failed game=%s critical=%s stage=%s reason=%s http_status=%s duration_ms=%s",
            game_id, error["critical_id"], error["stage"], error["reason"], error.get("http_status"),
            round((time.monotonic() - started) * 1000),
        )
    if config.DEBUG:
        logger.debug("event=explanation_completed game=%s generated=%s cached=%s failed=%s duration_ms=%s",
                     game_id, len(result["generated"]), len(result["cached"]), len(result["errors"]),
                     round((time.monotonic() - started) * 1000))
    return result


def _generate_explanations(
    game_id: str,
    *,
    progress: dict,
    review_side: str | None = None,
    critical_id: str | None = None,
    force: bool = False,
    provider: ExplanationProvider | None = None,
    knowledge_retriever: KnowledgeRetriever | None = None,
) -> dict:
    """Generate one or every critical explanation, reusing unchanged validated entries."""
    try:
        analysis = load_analysis(game_id, review_side=review_side)
    except GameNotFoundError as exc:
        raise ExplanationNotFoundError(str(exc)) from exc
    side = str(analysis.get("review_side") or "")
    critical_positions = list(analysis.get("critical_positions") or [])
    if critical_id:
        critical_positions = [
            position
            for position in critical_positions
            if position.get("critical_id") == critical_id
        ]
        if not critical_positions:
            raise ExplanationNotFoundError(f"Unknown critical position '{critical_id}'.")
    if not critical_positions:
        raise ExplanationNotFoundError("This analysis has no critical positions to explain.")

    progress["stage"] = "configuration"
    try:
        active_provider = provider or configured_provider()
    except ExplanationProviderError as exc:
        raise ExplanationError(str(exc), reason=exc.reason, http_status=exc.http_status) from exc
    provider_info = active_provider.info
    progress["stage"] = "input"
    requests: list[tuple[dict, ExplanationRequest]] = []
    try:
        for position in critical_positions:
            request = build_request(analysis, position)
            requests.append((position, request.model_copy(
                update={"input_hash": _request_hash(request, provider_info)}
            )))
    except ExplanationInputError as exc:
        raise ExplanationError(str(exc)) from exc

    first_request = requests[0][1]
    template = _empty_artifact(
        analysis, provider_info, first_request.prompt_version, first_request.language
    )
    order = {
        str(position.get("critical_id")): index
        for index, position in enumerate(analysis.get("critical_positions") or [])
    }
    generated: list[str] = []
    cached: list[str] = []
    errors: list[dict] = []

    progress["stage"] = "cache"
    with _lock_for(game_id, side):
        try:
            existing = load_explanations(game_id, review_side=side)
        except GameNotFoundError:
            existing = {}
        artifact = existing if _compatible(existing, template) else template

        try:
            current_index_fingerprint = (
                knowledge_retriever.current_index_fingerprint()
                if knowledge_retriever is not None
                else ""
            )
        except Exception:  # noqa: BLE001 - optional status lookup follows the same degradation path
            current_index_fingerprint = ""

        for position, base_request in requests:
            progress.update(stage="cache", critical_id=base_request.critical_id)
            prior = next(
                (
                    item
                    for item in artifact.get("positions") or []
                    if item.get("critical_id") == base_request.critical_id
                    and item.get("base_input_hash") == base_request.input_hash
                    and item.get("knowledge_index_fingerprint", "") == current_index_fingerprint
                ),
                None,
            )
            if prior is not None and not force:
                if config.DEBUG:
                    logger.debug("event=explanation_cache_hit game=%s critical=%s", game_id, base_request.critical_id)
                cached.append(base_request.critical_id)
                continue
            progress["stage"] = "knowledge"
            with knowledge_trace_context("explanation", game_id=game_id,
                                         critical_id=base_request.critical_id):
                knowledge_context, knowledge_status, index_fingerprint, citations = _retrieve_knowledge(
                    knowledge_retriever, position
                )
            progress["stage"] = "input"
            request = build_request(analysis, position, knowledge_context=knowledge_context)
            request = request.model_copy(update={
                "knowledge_status": knowledge_status,
                "knowledge_index_fingerprint": index_fingerprint,
                "knowledge_citations": citations,
            })
            request = request.model_copy(update={"input_hash": _request_hash(request, provider_info)})
            try:
                progress["stage"] = "model"
                response = active_provider.explain_position(request)
                progress["stage"] = "validation"
                explanation = _validated_explanation(response.text, request)
            except (ExplanationProviderError, ExplanationError) as exc:
                if critical_id:
                    raise ExplanationError(str(exc), reason=exc.reason, http_status=exc.http_status) from exc
                errors.append({"critical_id": request.critical_id, "error": str(exc),
                               "reason": exc.reason, "http_status": exc.http_status,
                               "stage": progress["stage"]})
                if exc.reason == "authentication_failed":
                    break
                continue
            if config.DEBUG:
                logger.debug("event=explanation_validated game=%s critical=%s", game_id, request.critical_id)
            progress["stage"] = "persistence"
            entry = {
                **explanation.model_dump(mode="json"),
                "input_hash": request.input_hash,
                "base_input_hash": base_request.input_hash,
                "knowledge_status": request.knowledge_status,
                "knowledge_index_fingerprint": request.knowledge_index_fingerprint,
                "knowledge_passage_hashes": [
                    item.get("text_hash") for item in knowledge_context.get("passages", [])
                ],
                "knowledge_citations": citations,
                "generated_at": _now_iso(),
            }
            _replace_position(artifact, entry, order)
            try:
                store_explanations(game_id, side, artifact)
            except (OSError, ValueError) as exc:
                raise ExplanationError("Could not save explanations. Check data directory permissions and disk space.", reason="storage_failed") from exc
            generated.append(request.critical_id)

    if not generated and not cached and errors:
        raise ExplanationError(errors[0]["error"], reason=errors[0]["reason"], http_status=errors[0]["http_status"])
    return {
        "artifact": artifact,
        "generated": generated,
        "cached": cached,
        "errors": errors,
    }

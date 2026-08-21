"""Explanation orchestration: build input, call one provider, validate, and cache."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from server.core.explanation.builder import (
    EXPLANATION_SCHEMA_VERSION,
    ExplanationInputError,
    build_request,
)
from server.core.explanation.models import Explanation, ExplanationRequest
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


class ExplanationError(RuntimeError):
    """A generation, grounding, or persistence error safe to return through the local API."""


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


def generate_explanations(
    game_id: str,
    *,
    review_side: str | None = None,
    critical_id: str | None = None,
    force: bool = False,
    provider: ExplanationProvider | None = None,
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

    try:
        active_provider = provider or configured_provider()
    except ExplanationProviderError as exc:
        raise ExplanationError(str(exc)) from exc
    provider_info = active_provider.info
    requests: list[ExplanationRequest] = []
    try:
        for position in critical_positions:
            request = build_request(analysis, position)
            requests.append(
                request.model_copy(update={"input_hash": _request_hash(request, provider_info)})
            )
    except ExplanationInputError as exc:
        raise ExplanationError(str(exc)) from exc

    first_request = requests[0]
    template = _empty_artifact(
        analysis, provider_info, first_request.prompt_version, first_request.language
    )
    order = {
        str(position.get("critical_id")): index
        for index, position in enumerate(analysis.get("critical_positions") or [])
    }
    generated: list[str] = []
    cached: list[str] = []
    errors: list[dict[str, str]] = []

    with _lock_for(game_id, side):
        try:
            existing = load_explanations(game_id, review_side=side)
        except GameNotFoundError:
            existing = {}
        artifact = existing if _compatible(existing, template) else template

        for request in requests:
            prior = next(
                (
                    item
                    for item in artifact.get("positions") or []
                    if item.get("critical_id") == request.critical_id
                    and item.get("input_hash") == request.input_hash
                ),
                None,
            )
            if prior is not None and not force:
                cached.append(request.critical_id)
                continue
            try:
                response = active_provider.explain_position(request)
                explanation = _validated_explanation(response.text, request)
            except (ExplanationProviderError, ExplanationError) as exc:
                if critical_id:
                    raise ExplanationError(str(exc)) from exc
                errors.append({"critical_id": request.critical_id, "error": str(exc)})
                continue
            entry = {
                **explanation.model_dump(mode="json"),
                "input_hash": request.input_hash,
                "generated_at": _now_iso(),
            }
            _replace_position(artifact, entry, order)
            try:
                store_explanations(game_id, side, artifact)
            except (OSError, ValueError) as exc:
                raise ExplanationError(f"Could not save explanations: {exc}") from exc
            generated.append(request.critical_id)

    if not generated and not cached and errors:
        raise ExplanationError(errors[0]["error"])
    return {
        "artifact": artifact,
        "generated": generated,
        "cached": cached,
        "errors": errors,
    }

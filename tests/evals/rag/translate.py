"""Freeze live Chinese-to-English retrieval translations through the Agent endpoint."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import time
from typing import Any, Awaitable, Callable

from server import config
from server.core.agent.schema_adapter import adapt_schema
from server.core.model_usage import normalize_usage


SCHEMA_VERSION = "rag-query-translations-v1"
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"query_en": {"type": "string", "minLength": 1}},
    "required": ["query_en"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ModelTranslation:
    output_text: str
    status: str
    usage: Any = None


TranslationRequest = Callable[[str, str], Awaitable[ModelTranslation]]


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_prompt(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    blocks = source.split("```text")
    if len(blocks) != 2 or "```" not in blocks[1]:
        raise ValueError("translation_prompt.md must contain exactly one ```text block.")
    prompt, remainder = blocks[1].split("```", 1)
    if "```text" in remainder or not prompt.strip():
        raise ValueError("translation_prompt.md must contain exactly one non-empty ```text block.")
    return prompt.strip()


def _load_queries(path: Path) -> list[dict[str, str]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    queries: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        query_id = row.get("query_id")
        query_zh = row.get("query_zh")
        if not isinstance(query_id, str) or not query_id or query_id in seen:
            raise ValueError("queries.jsonl has a missing or duplicate query_id.")
        if not isinstance(query_zh, str) or not query_zh.strip():
            raise ValueError(f"{query_id}: query_zh must be a non-empty string.")
        queries.append({"query_id": query_id, "query_zh": query_zh})
        seen.add(query_id)
    if not queries:
        raise ValueError("queries.jsonl is empty.")
    return queries


def _parse_translation(response: ModelTranslation) -> str:
    if response.status != "completed":
        raise ValueError("incomplete_response")
    try:
        value = json.loads(response.output_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(value, dict) or set(value) != {"query_en"} or not isinstance(value["query_en"], str):
        raise ValueError("invalid_schema")
    query_en = value["query_en"].strip()
    if not query_en:
        raise ValueError("empty_translation")
    return query_en


def _failure(exc: Exception) -> dict[str, str]:
    if isinstance(exc, TimeoutError):
        code = "timeout"
    elif isinstance(exc, ValueError) and str(exc) in {
        "incomplete_response", "invalid_json", "invalid_schema", "empty_translation"
    }:
        code = str(exc)
    else:
        code = "provider_error"
    return {"code": code, "type": type(exc).__name__}


def _aggregate_usage(entries: list[dict[str, Any]]) -> dict[str, Any]:
    usages = [entry["usage"] for entry in entries if entry.get("usage")]
    fields = sorted({key for usage in usages for key in usage})
    return {
        "totals": {key: sum(usage.get(key, 0) for usage in usages) for key in fields},
        "reported_records": {key: sum(key in usage for usage in usages) for key in fields},
    }


def _run_record(
    *,
    query_file_sha256: str,
    prompt_sha256: str,
    model: str,
    provider: str,
    endpoint_type: str,
    reasoning_effort: str,
    entries: list[dict[str, Any]],
    total_queries: int,
    started_at: str,
    status: str,
) -> dict[str, Any]:
    durations = [entry["duration_ms"] for entry in entries]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "query_sha256": query_file_sha256,
        "prompt_sha256": prompt_sha256,
        "model": model,
        "model_sha256": _sha256(model),
        "provider": provider,
        "endpoint_type": endpoint_type,
        "reasoning_effort": reasoning_effort,
        "total_queries": total_queries,
        "completed_queries": len(entries),
        "successful_queries": sum(entry["status"] == "ok" for entry in entries),
        "failed_queries": sum(entry["status"] == "error" for entry in entries),
        "latency_ms": {
            "total": sum(durations),
            "median": statistics.median(durations) if durations else None,
        },
        "usage": _aggregate_usage(entries),
    }


async def run_translations(
    *,
    dataset_dir: Path,
    output_dir: Path,
    model: str,
    provider: str,
    endpoint_type: str,
    request_translation: TranslationRequest,
    reasoning_effort: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run and durably freeze one isolated translation for every dataset query."""
    if output_dir.exists():
        raise FileExistsError(f"Output directory already exists: {output_dir}")
    query_path = dataset_dir / "queries.jsonl"
    prompt_path = dataset_dir / "translation_prompt.md"
    query_bytes = query_path.read_bytes()
    prompt = _load_prompt(prompt_path)
    queries = _load_queries(query_path)
    query_file_sha256 = _sha256(query_bytes)
    prompt_sha256 = _sha256(prompt)
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entries: list[dict[str, Any]] = []
    output_dir.mkdir(parents=True)
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    def persist(status: str) -> dict[str, Any]:
        run = _run_record(
            query_file_sha256=query_file_sha256,
            prompt_sha256=prompt_sha256,
            model=model,
            provider=provider,
            endpoint_type=endpoint_type,
            reasoning_effort=reasoning_effort,
            entries=entries,
            total_queries=len(queries),
            started_at=started_at,
            status=status,
        )
        _write_json(output_dir / "translations.json", entries)
        _write_json(output_dir / "run.json", run)
        return run

    persist("running")
    for index, query in enumerate(queries, 1):
        query_id = query["query_id"]
        query_zh = query["query_zh"]
        started = time.monotonic()
        entry: dict[str, Any] = {
            "query_id": query_id,
            "input_sha256": _sha256(query_zh),
            "prompt_sha256": prompt_sha256,
            "model_sha256": _sha256(model),
        }
        usage: dict[str, Any] = {}
        try:
            response = await asyncio.wait_for(
                request_translation(prompt, query_zh), timeout=config.AGENT_TIMEOUT
            )
            usage = normalize_usage(response.usage, protocol="responses")
            query_en = _parse_translation(response)
            entry.update(
                status="ok",
                query_en=query_en,
                query_en_sha256=_sha256(query_en),
                usage=usage,
            )
        except Exception as exc:  # each query remains a scored miss without leaking provider text
            entry.update(status="error", error=_failure(exc), usage=usage)
        entry["duration_ms"] = max(0, round((time.monotonic() - started) * 1000, 3))
        entries.append(entry)
        persist("running")
        if progress is not None:
            progress(f"[{index}/{len(queries)}] {query_id}: {entry['status']}")
    return persist("complete")


class ResponsesTranslator:
    def __init__(
        self, *, model: str, base_url: str, api_key: str, provider: str,
        max_output_tokens: int,
    ):
        try:
            import openai
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError("Install the agent extra before running live translation.") from exc
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._output_schema = adapt_schema(_OUTPUT_SCHEMA, provider)
        self._reasoning_effort = config.AGENT_REASONING_EFFORT
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or "https://api.openai.com/v1",
            max_retries=0,
        )

    async def translate(self, prompt: str, query_zh: str) -> ModelTranslation:
        options: dict[str, Any] = {}
        if self._reasoning_effort:
            options["reasoning"] = {"effort": self._reasoning_effort}
        response = await self._client.responses.create(
            model=self._model,
            instructions=prompt,
            input=[{"role": "user", "content": json.dumps({"query_zh": query_zh}, ensure_ascii=False)}],
            tools=[],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "rag_query_translation",
                    "schema": self._output_schema,
                    "strict": True,
                }
            },
            max_output_tokens=self._max_output_tokens,
            store=False,
            timeout=config.AGENT_TIMEOUT,
            **options,
        )
        return ModelTranslation(
            output_text=response.output_text,
            status=response.status,
            usage=response.usage,
        )

    async def close(self) -> None:
        await self._client.close()


async def _main_async(args: argparse.Namespace) -> None:
    if not args.model:
        raise SystemExit("--model or CHESS_AGENT_MODEL is required for live translation")
    endpoint_type = "custom_responses" if args.base_url else "openai_responses"
    api_key = config.AGENT_API_KEY if args.base_url else config.OPENAI_API_KEY
    if not api_key:
        variable = "CHESS_AGENT_API_KEY" if args.base_url else "OPENAI_API_KEY"
        raise SystemExit(f"{variable} is required for live translation")
    provider = config.resolve_agent_provider(config.AGENT_PROVIDER, args.base_url)
    translator = ResponsesTranslator(
        model=args.model,
        base_url=args.base_url,
        api_key=api_key,
        provider=provider,
        max_output_tokens=args.max_output_tokens,
    )
    try:
        run = await run_translations(
            dataset_dir=args.dataset_dir,
            output_dir=args.output_dir,
            model=args.model,
            provider=provider,
            endpoint_type=endpoint_type,
            request_translation=translator.translate,
            reasoning_effort=config.AGENT_REASONING_EFFORT,
            progress=lambda message: print(message, flush=True),
        )
    finally:
        await translator.close()
    print(json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default=config.AGENT_MODEL)
    parser.add_argument("--base-url", default=config.AGENT_BASE_URL)
    parser.add_argument("--max-output-tokens", type=int, default=config.AGENT_MAX_OUTPUT_TOKENS)
    args = parser.parse_args()
    if args.max_output_tokens < 1:
        parser.error("--max-output-tokens must be positive")
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()

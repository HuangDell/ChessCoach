"""Prepare blinded live-translation review shards and merge explicit decisions."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from .validate_dataset import validate


SCHEMA_VERSION = "rag-live-translation-review-v1"
SHARDS = ("A", "B", "C")
TRANSLATION_VERDICTS = {"faithful", "minor_issue", "meaning_changed", "failed"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _value_sha256(value: Any) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _query_rows(dataset_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (dataset_dir / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assigned_query_ids(query_ids: set[str], shard: str) -> list[str]:
    index = SHARDS.index(shard)
    return sorted(query_ids)[index::len(SHARDS)]


def _unique_rows(rows: list[dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        query_id = row.get("query_id")
        if not isinstance(query_id, str) or query_id in result:
            raise ValueError(f"{name} has a missing or duplicate query_id.")
        result[query_id] = row
    return result


def prepare(dataset_dir: Path, retrieval_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Create three rank-blind shards containing only newly retrieved pairs."""
    if output_dir.exists():
        raise FileExistsError(f"Output directory already exists: {output_dir}")
    query_path = dataset_dir / "queries.jsonl"
    query_rows = _query_rows(dataset_dir)
    queries = _unique_rows(query_rows, "Dataset queries")
    labels = _read_json(dataset_dir / "labels.json")
    manifest = _read_json(dataset_dir / "manifest.json")
    if set(queries) != set(labels):
        raise ValueError("Dataset queries and labels differ.")

    run_path = retrieval_dir / "run.json"
    results_path = retrieval_dir / "results.json"
    pool_path = retrieval_dir / "review-pool.json"
    translations_path = retrieval_dir / "translations.json"
    run = _read_json(run_path)
    if run.get("translation") != "frozen_live_outputs":
        raise ValueError("Review preparation requires a frozen live-translation retrieval run.")
    if run.get("query_sha256") != _sha256(query_path.read_bytes()):
        raise ValueError("Retrieval queries do not match the dataset.")
    if run.get("integrity", {}).get("corpus_fingerprint") != manifest.get("corpus_fingerprint"):
        raise ValueError("Retrieval corpus does not match the dataset.")
    if run.get("translation_sha256") != _sha256(translations_path.read_bytes()):
        raise ValueError("Frozen translations changed after retrieval.")

    translations = _unique_rows(_read_json(translations_path), "Translations")
    results = _unique_rows(_read_json(results_path), "Retrieval results")
    pool = _unique_rows(_read_json(pool_path), "Review pool")
    if set(translations) != set(queries) or set(results) != set(queries):
        raise ValueError("Translations and results must contain every dataset query.")
    successful = {query_id for query_id, result in results.items() if result.get("status") == "ok"}
    if set(pool) != successful:
        raise ValueError("Review pool must contain exactly the successful retrieval queries.")

    items_by_query: dict[str, list[dict[str, Any]]] = {query_id: [] for query_id in queries}
    for query_id in sorted(successful):
        result = results[query_id]
        if set(result.get("routes", {})) != {"bm25", "dense", "hybrid"}:
            raise ValueError(f"{query_id}: successful result is missing a retrieval route.")
        expected = {
            (candidate["chunk_id"], candidate["text_hash"])
            for rows in result["routes"].values()
            for candidate in rows
        }
        pool_row = pool[query_id]
        if pool_row.get("query_zh") != queries[query_id]["query_zh"]:
            raise ValueError(f"{query_id}: review pool Chinese query changed.")
        if pool_row.get("context") != queries[query_id]["context"]:
            raise ValueError(f"{query_id}: review pool context changed.")
        candidates = pool_row.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError(f"{query_id}: invalid review candidates.")
        actual = {(candidate.get("chunk_id"), candidate.get("text_hash")) for candidate in candidates}
        if actual != expected or len(actual) != len(candidates):
            raise ValueError(f"{query_id}: review pool differs from retrieval route union.")
        existing = {
            judgment["chunk_id"]
            for field in ("judgments", "diagnostic_sources")
            for judgment in labels[query_id].get(field, [])
        }
        query_en = translations[query_id].get("query_en")
        if translations[query_id].get("status") != "ok" or not isinstance(query_en, str) or not query_en.strip():
            raise ValueError(f"{query_id}: successful retrieval lacks a successful translation.")
        if result.get("query_text") != query_en:
            raise ValueError(f"{query_id}: retrieval query differs from frozen translation.")
        for candidate in candidates:
            if candidate["chunk_id"] not in existing:
                items_by_query[query_id].append({
                    "query_id": query_id,
                    "query_zh": queries[query_id]["query_zh"],
                    "context": queries[query_id]["context"],
                    "query_en_generated": query_en,
                    "candidate": {
                        key: candidate[key]
                        for key in ("chunk_id", "text_hash", "title", "source_locator", "text")
                    },
                })

    source = {
        "dataset_version": manifest["dataset_version"],
        "corpus_fingerprint": manifest["corpus_fingerprint"],
        "query_sha256": _sha256(query_path.read_bytes()),
        "labels_sha256": _sha256((dataset_dir / "labels.json").read_bytes()),
        "manifest_sha256": _sha256((dataset_dir / "manifest.json").read_bytes()),
        "retrieval_run_sha256": _sha256(run_path.read_bytes()),
        "retrieval_results_sha256": _sha256(results_path.read_bytes()),
        "retrieval_pool_sha256": _sha256(pool_path.read_bytes()),
        "translations_sha256": _sha256(translations_path.read_bytes()),
        "translation_run": _read_json(retrieval_dir / "translation-run.json"),
    }
    output_dir.mkdir(parents=True)
    counts: dict[str, int] = {}
    for shard in SHARDS:
        query_ids = _assigned_query_ids(set(queries), shard)
        translation_inputs = []
        items = []
        for query_id in query_ids:
            translation = translations[query_id]
            translation_inputs.append({
                "query_id": query_id,
                "query_zh": queries[query_id]["query_zh"],
                "context": queries[query_id]["context"],
                "translation_status": translation.get("status"),
                "query_en_generated": translation.get("query_en"),
                "translation_error": translation.get("error"),
            })
            items.extend(items_by_query[query_id])
        value = {
            "schema_version": SCHEMA_VERSION,
            "review_type": "model_assisted_blind_pool",
            "shard": shard,
            "source": source,
            "blind_fields": [
                "query_en_reference", "labels", "expected_points", "routes", "ranks", "scores",
            ],
            "query_ids": query_ids,
            "translations": translation_inputs,
            "translations_sha256": _value_sha256(translation_inputs),
            "items": items,
            "items_sha256": _value_sha256(items),
            "reviewer": None,
            "translation_reviews": [],
            "decisions": [],
        }
        _write_json(output_dir / f"shard-{shard}.json", value)
        counts[shard] = len(items)
    summary = {
        "queries": len(queries),
        "successful_retrieval_queries": len(successful),
        "new_candidate_pairs": sum(counts.values()),
        "pairs_by_shard": counts,
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def _corpus_records(corpus: Path) -> tuple[dict[str, str], dict[str, sqlite3.Row], dict[str, dict]]:
    with sqlite3.connect(corpus.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        rows = connection.execute("SELECT * FROM chunks ORDER BY book_id, ordinal").fetchall()
        books = {row["book_id"]: dict(row) for row in connection.execute("SELECT * FROM books")}
    fingerprint = hashlib.sha256("\0".join([
        meta["corpus_version"],
        meta["source_collection_hash"],
        *(f"{row['chunk_id']}:{row['text_hash']}" for row in rows),
    ]).encode("utf-8")).hexdigest()
    meta["fingerprint"] = fingerprint
    return meta, {row["chunk_id"]: row for row in rows}, books


def merge(dataset_dir: Path, corpus: Path, reviews: list[Path], output: Path) -> dict[str, Any]:
    """Merge exactly one completed review for every prepared query/chunk pair."""
    if output.exists():
        raise FileExistsError(f"Output directory already exists: {output}")
    if len(reviews) != len(SHARDS):
        raise ValueError("Exactly three review files are required.")
    query_path = dataset_dir / "queries.jsonl"
    queries = _unique_rows(_query_rows(dataset_dir), "Dataset queries")
    labels = _read_json(dataset_dir / "labels.json")
    manifest = _read_json(dataset_dir / "manifest.json")
    meta, chunks, books = _corpus_records(corpus)
    if meta["fingerprint"] != manifest.get("corpus_fingerprint"):
        raise ValueError("Corpus does not match the source dataset.")

    files_by_shard: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in reviews:
        value = _read_json(path)
        shard = value.get("shard")
        if value.get("schema_version") != SCHEMA_VERSION or shard not in SHARDS or shard in files_by_shard:
            raise ValueError("Reviews must contain one valid file for each shard.")
        files_by_shard[shard] = (path, value)
    if set(files_by_shard) != set(SHARDS):
        raise ValueError("Reviews must cover shards A, B and C.")

    all_decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    translation_reviews: list[dict[str, Any]] = []
    reviewers: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    common_source: dict[str, Any] | None = None
    for shard in SHARDS:
        _, value = files_by_shard[shard]
        source = value.get("source", {})
        if common_source is None:
            common_source = source
        elif source != common_source:
            raise ValueError("All review shards must come from the same retrieval run.")
        if source.get("corpus_fingerprint") != manifest["corpus_fingerprint"]:
            raise ValueError(f"Shard {shard} targets a different corpus.")
        if source.get("query_sha256") != _sha256(query_path.read_bytes()):
            raise ValueError(f"Shard {shard} targets different queries.")
        if source.get("labels_sha256") != _sha256((dataset_dir / "labels.json").read_bytes()):
            raise ValueError(f"Shard {shard} targets different labels.")
        if source.get("manifest_sha256") != _sha256((dataset_dir / "manifest.json").read_bytes()):
            raise ValueError(f"Shard {shard} targets a different manifest.")
        expected_query_ids = _assigned_query_ids(set(queries), shard)
        if value.get("query_ids") != expected_query_ids:
            raise ValueError(f"Shard {shard} has the wrong query assignment.")
        translations = value.get("translations")
        items = value.get("items")
        if value.get("translations_sha256") != _value_sha256(translations):
            raise ValueError(f"Shard {shard} translation inputs changed after preparation.")
        if value.get("items_sha256") != _value_sha256(items):
            raise ValueError(f"Shard {shard} candidate inputs changed after preparation.")
        if [row.get("query_id") for row in translations] != expected_query_ids:
            raise ValueError(f"Shard {shard} translation audit does not match assigned queries.")

        reviewer = value.get("reviewer")
        if not isinstance(reviewer, dict) or reviewer.get("type") != "model_assisted_review":
            raise ValueError(f"Shard {shard} is missing model-review provenance.")
        if not reviewer.get("agent") or not reviewer.get("model") or not reviewer.get("method"):
            raise ValueError(f"Shard {shard} has incomplete reviewer provenance.")
        reviewers.append(deepcopy(reviewer))

        audit = value.get("translation_reviews")
        audit_by_query = _unique_rows(audit, f"Shard {shard} translation reviews")
        if set(audit_by_query) != set(expected_query_ids):
            raise ValueError(f"Shard {shard} must review every assigned translation.")
        translation_status = {row["query_id"]: row.get("translation_status") for row in translations}
        for query_id in expected_query_ids:
            row = audit_by_query[query_id]
            if row.get("verdict") not in TRANSLATION_VERDICTS or not row.get("reason_zh"):
                raise ValueError(f"{query_id}: invalid translation review.")
            if (translation_status[query_id] == "ok") == (row["verdict"] == "failed"):
                raise ValueError(f"{query_id}: translation verdict conflicts with execution status.")
            translation_reviews.append({**deepcopy(row), "shard": shard, "reviewer": deepcopy(reviewer)})

        item_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            pair = (item.get("query_id"), item.get("candidate", {}).get("chunk_id"))
            if pair in item_by_pair or pair in seen_pairs or pair[0] not in expected_query_ids:
                raise ValueError(f"Shard {shard} has a duplicate or misplaced candidate pair.")
            item_by_pair[pair] = item
            seen_pairs.add(pair)
        decisions = value.get("decisions")
        decision_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for decision in decisions:
            pair = (decision.get("query_id"), decision.get("chunk_id"))
            if pair in decision_by_pair:
                raise ValueError(f"Shard {shard} has duplicate review decisions.")
            decision_by_pair[pair] = decision
        if set(decision_by_pair) != set(item_by_pair):
            raise ValueError(f"Shard {shard} decisions must exactly cover its candidate pool.")
        for pair, item in item_by_pair.items():
            decision = decision_by_pair[pair]
            candidate = item["candidate"]
            if decision.get("text_hash") != candidate["text_hash"]:
                raise ValueError(f"{pair[0]}: review decision uses a different passage hash.")
            if _sha256(candidate["text"].encode("utf-8")) != candidate["text_hash"]:
                raise ValueError(f"{pair[0]}: candidate text does not match its hash.")
            if decision.get("grade") not in (0, 1, 2) or not decision.get("reason_zh"):
                raise ValueError(f"{pair[0]}: invalid relevance decision.")
            if not isinstance(decision.get("uncertain"), bool):
                raise ValueError(f"{pair[0]}: uncertainty must be explicit.")
            all_decisions.append((decision, reviewer))

    new_labels = deepcopy(labels)
    for decision, reviewer in all_decisions:
        query_id = decision["query_id"]
        chunk_id = decision["chunk_id"]
        if query_id not in new_labels or chunk_id not in chunks:
            raise ValueError("Review decision references an unknown query or passage.")
        if any(
            judgment["chunk_id"] == chunk_id
            for field in ("judgments", "diagnostic_sources")
            for judgment in new_labels[query_id].get(field, [])
        ):
            raise ValueError(f"{query_id}: review attempts to replace an existing judgment.")
        row = chunks[chunk_id]
        if (decision["text_hash"] != row["text_hash"]
                or _sha256(row["text"].encode("utf-8")) != row["text_hash"]):
            raise ValueError(f"{query_id}: review passage is stale.")
        book = books[row["book_id"]]
        judgment = {
            key: row[key]
            for key in ("chunk_id", "book_id", "ordinal", "text_hash", "source_locator")
        }
        judgment.update(
            title=book["title"],
            author=book["author"],
            heading_path=json.loads(row["heading_path"]),
            reason_zh=decision["reason_zh"],
            grade=decision["grade"],
            pooled_review=True,
            live_translation_pool=True,
            uncertain=decision["uncertain"],
            reviewer_agent=reviewer["agent"],
            reviewer_model=reviewer["model"],
        )
        new_labels[query_id]["judgments"].append(judgment)
        new_labels[query_id]["review_status"] = "model_reviewed"
        new_labels[query_id]["qrels_complete"] = False
        new_labels[query_id]["independent_human_review"] = False

    now = datetime.now(timezone.utc).isoformat()
    new_manifest = deepcopy(manifest)
    new_manifest.update(
        dataset_version="zh-en-rag-model-reviewed-v3-pooled-live-translation",
        status="model_reviewed",
        updated_at=now,
        corpus_snapshot_path="corpus-v3.sqlite3",
        revision_note=(
            "Added complete blinded model review of newly retrieved live-translation candidates; "
            "no independent human approval."
        ),
    )
    translation_run = common_source["translation_run"] if common_source is not None else {}
    new_manifest["translation"] = {
        "reference_translation": deepcopy(new_manifest.get("translation", {})),
        "live_translation": {
            "method": "frozen_live_responses_endpoint",
            "external_endpoint_called": True,
            "model": translation_run.get("model"),
            "provider": translation_run.get("provider"),
            "endpoint_type": translation_run.get("endpoint_type"),
            "prompt_sha256": translation_run.get("prompt_sha256"),
            "translations_sha256": common_source.get("translations_sha256") if common_source else None,
            "review_count": len(translation_reviews),
            "review_status": "model_assisted_not_human_approved",
        },
    }
    new_manifest["judgments"].update(
        complete=False,
        independent_human_review=False,
        retrieval_ranks_used=False,
        metrics="not_run",
        review_status="model_reviewed",
        live_translation_candidate_pool_count=len(all_decisions),
        live_translation_candidate_pool_reviewed=len(all_decisions),
        live_translation_candidate_pool_coverage=1.0,
        live_translation_reviewers=reviewers,
    )

    output.mkdir(parents=True)
    snapshot = output / "corpus-v3.sqlite3"
    with sqlite3.connect(corpus.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(snapshot) as destination:
            source.backup(destination)
    for filename in ("queries.jsonl", "translation_prompt.md"):
        shutil.copyfile(dataset_dir / filename, output / filename)
    _write_json(output / "labels.json", new_labels)
    _write_json(output / "manifest.json", new_manifest)
    review_dir = output / "reviews"
    review_dir.mkdir()
    for shard, (path, _) in files_by_shard.items():
        shutil.copyfile(path, review_dir / f"shard-{shard}.json")
    _write_json(output / "translation-reviews.json", translation_reviews)
    result = {
        "dataset_version": new_manifest["dataset_version"],
        "queries": len(queries),
        "new_judgments": len(all_decisions),
        "translation_reviews": len(translation_reviews),
        "grades": dict(Counter(decision["grade"] for decision, _ in all_decisions)),
        "review_type": "model_assisted_not_human_approved",
        "integrity": validate(output, snapshot),
    }
    _write_json(output / "review-summary.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--dataset-dir", type=Path, required=True)
    prepare_parser.add_argument("--retrieval-dir", type=Path, required=True)
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("--dataset-dir", type=Path, required=True)
    merge_parser.add_argument("--corpus", type=Path, required=True)
    merge_parser.add_argument("--reviews", nargs="+", type=Path, required=True)
    merge_parser.add_argument("--output", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    result = {"prepare": prepare, "merge": merge}[command](**args)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

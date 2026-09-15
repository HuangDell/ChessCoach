"""Explicit local RAG index, retrieval, and known-evidence diagnostics. No network."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time

import chess

from .validate_dataset import validate


def verified_context(context: dict | None) -> str:
    """Project replayed board facts only; exclude curated candidates and labels."""
    if context is None:
        return ""
    board = chess.Board(context["initial_fen"])
    if not board.is_valid():
        raise ValueError("Invalid initial position.")
    history = []
    for san in context["moves_san"]:
        move = board.parse_san(san)
        history.append(f"{board.fullmove_number}{'.' if board.turn else '...'}{board.san(move)}")
        board.push(move)
    if not board.is_valid() or board.fen() != context["fen"]:
        raise ValueError("Invalid position or replay/FEN mismatch.")
    lines = ["Verified board context:",
             f"Side to move: {'White' if board.turn else 'Black'}; in check: {str(board.is_check()).lower()}."]
    for color, name in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
        pieces = [f"{chess.piece_name(piece.piece_type)} {chess.square_name(square)}"
                  for square, piece in sorted(board.piece_map().items()) if piece.color == color]
        lines.append(f"{name}: " + ", ".join(pieces) + ".")
    if history:
        lines.append("Verified move history: " + " ".join(history) + ".")
    return "\n".join(lines)


def write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def unique_candidates(rows: list[dict], limit: int = 5) -> list[dict]:
    result, seen = [], set()
    for row in rows:
        if row["text_hash"] not in seen:
            result.append(row)
            seen.add(row["text_hash"])
        if len(result) == limit:
            break
    return result


def score(results: list[dict], labels: dict) -> dict:
    """Unjudged is unknown; only supported questions enter known-evidence metrics."""
    ids = [r["query_id"] for r in results]
    if len(set(ids)) != len(ids) or set(ids) != set(labels):
        raise ValueError("Results must contain each labeled query exactly once.")
    cases = []
    for result in results:
        label = labels[result["query_id"]]
        grades = {j["chunk_id"]: j["grade"] for j in label["judgments"]}
        eligible = label["answerability"] == "supported"
        metrics = {}
        for route in ("bm25", "dense", "hybrid"):
            rows = result.get("routes", {}).get(route, [])
            if result["status"] == "ok" and route not in result.get("routes", {}):
                raise ValueError("Successful result is missing a retrieval route.")
            metrics[route] = {}
            for k in (3, 5):
                ids = [row["chunk_id"] for row in rows[:k]]
                direct = [i for i, cid in enumerate(ids, 1) if grades.get(cid) == 2]
                any_positive = any(grades.get(cid, 0) > 0 for cid in ids)
                metrics[route][str(k)] = {
                    "known_direct_hit": int(bool(direct)) if eligible else None,
                    "known_positive_hit": int(any_positive) if eligible else None,
                    "known_direct_reciprocal_rank": 1 / direct[0] if direct and eligible else 0.0 if eligible else None,
                    "unjudged_count": sum(cid not in grades for cid in ids),
                    "returned_count": len(ids),
                }
        cases.append({"query_id": result["query_id"], "kind": label["kind"],
                      "eligible": eligible, "status": result["status"], "metrics": metrics})
    aggregate = {}
    for route in ("bm25", "dense", "hybrid"):
        aggregate[route] = {}
        for k in (3, 5):
            values = [c["metrics"][route][str(k)] for c in cases if route in c["metrics"]]
            eligible_values = [v for v in values if v["known_direct_hit"] is not None]
            returned = sum(v["returned_count"] for v in values)
            aggregate[route][str(k)] = {
                "scored_supported_queries": len(eligible_values),
                **{name: sum(v[name] for v in eligible_values) / len(eligible_values) if eligible_values else None
                   for name in ("known_direct_hit", "known_positive_hit", "known_direct_reciprocal_rank")},
                "unjudged_fraction": sum(v["unjudged_count"] for v in values) / returned if returned else None,
            }
    return {"scope": "known_evidence_diagnostic_not_full_corpus_recall",
            "queries": len(cases), "failed_queries": sum(c["status"] != "ok" for c in cases),
            "aggregate": aggregate, "cases": cases}


def build(corpus: Path, data_dir: Path, model_path: Path, device: str, batch_size: int) -> None:
    from server.core.knowledge.index import QwenEmbedder, build_index

    target = data_dir / "knowledge" / "corpus.sqlite3"
    if target.exists():
        raise ValueError("Index data directory already has a corpus; use a new isolated directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(corpus.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)

    class ProgressEmbedder(QwenEmbedder):
        def encode_documents(self, texts):
            print(f"Embedding batch: {len(texts)} passages", flush=True)
            return super().encode_documents(texts)

    embedder = ProgressEmbedder(model_path, device=device, batch_size=batch_size)
    started = time.perf_counter()
    try:
        result = build_index(data_dir, embedder, rebuild_corpus=False)
        record = {**asdict(result), "index_path": str(result.index_path),
                  "duration_seconds": time.perf_counter() - started,
                  "requested_device": device, "batch_size": batch_size}
        write_json(data_dir / "build-report.json", record)
        print(json.dumps(record, indent=2), flush=True)
    finally:
        embedder.close()


class CaptureTrace:
    """Keep the production trace in memory; eval owns persistence and retention."""
    def start(self):
        return {"schema_version": 1, "status": "error", "timings_ms": {}}

    def save(self, record):
        self.record = record


def load_translations(dataset_dir: Path, translations_dir: Path, queries: list[dict]) -> tuple[dict, dict]:
    metadata = json.loads((translations_dir / "run.json").read_text())
    if metadata["query_sha256"] != hashlib.sha256((dataset_dir / "queries.jsonl").read_bytes()).hexdigest():
        raise ValueError("Translation queries do not match the retrieval dataset.")
    rows = json.loads((translations_dir / "translations.json").read_text())
    ids = [row["query_id"] for row in rows]
    if len(set(ids)) != len(ids) or set(ids) != {q["query_id"] for q in queries}:
        raise ValueError("Translations must contain each query exactly once.")
    for row in rows:
        if row["status"] not in ("ok", "error"):
            raise ValueError("Invalid translation status.")
        if row["status"] == "ok" and not isinstance(row.get("query_en"), str):
            raise ValueError("Successful translation must have an English query.")
        if row["status"] == "ok" and not row["query_en"].strip():
            raise ValueError("Successful translation must have an English query.")
    return {row["query_id"]: row for row in rows}, metadata


def run(dataset_dir: Path, data_dir: Path, output_dir: Path, model_path: Path,
        device: str, batch_size: int, query_field: str, translations_dir: Path | None = None,
        context_mode: str = "question_only_no_skill_expansion") -> None:
    from server.core.knowledge.index import LanceDBKnowledgeRetriever, QwenEmbedder, get_index_status

    integrity = validate(dataset_dir, data_dir / "knowledge" / "corpus.sqlite3")
    if context_mode not in ("question_only_no_skill_expansion", "verified_board_v1"):
        raise ValueError("Unsupported context mode.")
    queries = [json.loads(line) for line in (dataset_dir / "queries.jsonl").read_text().splitlines() if line.strip()]
    translations, translation_metadata = load_translations(dataset_dir, translations_dir, queries) if translations_dir else ({}, None)
    if translations_dir:
        query_field = "query_en_translated"
    embedder = QwenEmbedder(model_path, device=device, batch_size=batch_size)
    status = get_index_status(data_dir, enabled=True, embedder=embedder)
    if not status.available:
        raise ValueError(status.error)
    output_dir.mkdir(parents=True, exist_ok=False)
    if translations_dir:
        write_json(output_dir / "translations.json", list(translations.values()))
        write_json(output_dir / "translation-run.json", translation_metadata)
    capture = CaptureTrace()
    retriever = LanceDBKnowledgeRetriever(data_dir, embedder, trace_store=capture)
    results, pool = [], []
    try:
        for query in queries:
            qid = query["query_id"]
            print(f"Retrieving {qid} ({query_field})", flush=True)
            capture.record = {"status": "error", "query_id": qid}
            try:
                if translations_dir and translations[qid]["status"] != "ok":
                    results.append({"query_id": qid, "status": "error", "error_type": "TranslationFailed",
                                    "translation_duration_ms": translations[qid].get("duration_ms")})
                    write_json(output_dir / f"{qid}-trace.json", {"status": "error", "failure_stage": "translation"})
                    write_json(output_dir / "results.json", results)
                    continue
                query_text = translations[qid]["query_en"] if translations_dir else query[query_field]
                context_text = verified_context(query["context"]) if context_mode == "verified_board_v1" else ""
                if context_text:
                    query_text += "\n\n" + context_text
                retrieved = retriever.search(query_text, limit=5)
                trace = capture.record
                hybrid_ids = [p.passage_id for p in retrieved.passages]
                records = {r["chunk_id"]: r for r in trace["dense"] + trace["lexical"]}
                routes = {"bm25": unique_candidates(trace["lexical"]),
                          "dense": unique_candidates(trace["dense"]),
                          "hybrid": [records[cid] for cid in hybrid_ids]}
                result = {"query_id": qid, "status": "ok", "routes": routes,
                          "query_text": query_text,
                          "verified_context_text": context_text,
                          "timings_ms": trace["timings_ms"]}
                if translations_dir:
                    result["translation_duration_ms"] = translations[qid].get("duration_ms")
                # Stable pseudorandom order hides route/rank; no gold enters the pool.
                candidates = {r["chunk_id"]: r for rows in routes.values() for r in rows}
                pool.append({"query_id": qid, "query_zh": query["query_zh"], "context": query["context"],
                             "candidates": [{key: row[key] for key in ("chunk_id", "text_hash", "text", "title", "source_locator")}
                                            for cid, row in sorted(candidates.items(), key=lambda item: hashlib.sha256((qid + item[0]).encode()).hexdigest())]})
            except Exception as exc:
                result = {"query_id": qid, "status": "error", "error_type": type(exc).__name__}
                trace = getattr(capture, "record", {"status": "error"})
            write_json(output_dir / f"{qid}-trace.json", trace)
            results.append(result)
            write_json(output_dir / "results.json", results)
        write_json(output_dir / "review-pool.json", pool)
        try:
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
            dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
        except (OSError, subprocess.CalledProcessError):
            revision, dirty = None, None
        write_json(output_dir / "run.json", {
            "suite": "rag", "integrity": integrity, "query_field": query_field,
            "query_sha256": hashlib.sha256((dataset_dir / "queries.jsonl").read_bytes()).hexdigest(),
            "index_fingerprint": status.index_fingerprint, "embedding_fingerprint": embedder.fingerprint,
            "git_revision": revision, "git_dirty": dirty,
            "requested_device": device, "batch_size": batch_size,
            "timing_scope": "production_hybrid_stages; first_query_includes_model_load; not_standalone_route_latency",
            "context_mode": context_mode, "translation": "frozen_live_outputs" if translations_dir else "not_run",
            "translation_sha256": hashlib.sha256((output_dir / "translations.json").read_bytes()).hexdigest() if translations_dir else None,
            "generation": "not_run", "candidate_pool": "union_of_route_top5_after_text_hash_deduplication",
        })
        report(dataset_dir, output_dir)
    finally:
        retriever.close()


def report(dataset_dir: Path, output_dir: Path) -> None:
    run_metadata = json.loads((output_dir / "run.json").read_text())
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    if run_metadata["integrity"]["corpus_fingerprint"] != manifest["corpus_fingerprint"]:
        raise ValueError("Report dataset uses a different corpus.")
    if run_metadata["query_sha256"] != hashlib.sha256((dataset_dir / "queries.jsonl").read_bytes()).hexdigest():
        raise ValueError("Report queries changed after retrieval.")
    labels_path = dataset_dir / "labels.json"
    result = score(json.loads((output_dir / "results.json").read_text()), json.loads(labels_path.read_text()))
    result["labels_sha256"] = hashlib.sha256(labels_path.read_bytes()).hexdigest()
    result["review_status_counts"] = dict(Counter(x["review_status"] for x in json.loads(labels_path.read_text()).values()))
    results = json.loads((output_dir / "results.json").read_text())
    labels = json.loads(labels_path.read_text())
    result["by_kind"] = {
        kind: score([r for r in results if labels[r["query_id"]]["kind"] == kind],
                    {qid: label for qid, label in labels.items() if label["kind"] == kind})["aggregate"]
        for kind in sorted({label["kind"] for label in labels.values()})
    }
    successful = [r for r in results if r["status"] == "ok"]
    warm = [r["timings_ms"]["total"] for r in successful[1:]]
    result["latency_ms"] = {
        "first_query_including_model_load": successful[0]["timings_ms"]["total"] if successful else None,
        "first_successful_query_id": successful[0]["query_id"] if successful else None,
        "warm_query_count": len(warm), "warm_median": statistics.median(warm) if warm else None,
        "scope": run_metadata["timing_scope"],
    }
    if run_metadata.get("translation") == "frozen_live_outputs":
        translation_path = output_dir / "translations.json"
        if hashlib.sha256(translation_path.read_bytes()).hexdigest() != run_metadata["translation_sha256"]:
            raise ValueError("Frozen translations changed after retrieval.")
        translations = json.loads(translation_path.read_text())
        durations = [r["duration_ms"] for r in translations if isinstance(r.get("duration_ms"), (int, float))]
        paired = [r["translation_duration_ms"] + r["timings_ms"]["total"] for r in successful[1:]
                  if isinstance(r.get("translation_duration_ms"), (int, float))]
        result["translation"] = {
            "failed_queries": sum(r["status"] != "ok" for r in translations),
            "measured_queries": len(durations),
            "median_ms": statistics.median(durations) if durations else None,
            "total_ms": sum(durations),
            "paired_translation_plus_retrieval_warm_median_ms": statistics.median(paired) if paired else None,
            "scope": "sum_of_separately_measured_stages_not_observed_end_to_end_latency",
        }
    write_json(output_dir / "report.json", result)


def compare(dataset_dir: Path, run_dirs: list[Path], output: Path, allow_context_change: bool = False) -> None:
    """Rescore frozen runs with one label snapshot; preserve historical reports."""
    if output.exists():
        raise ValueError("Comparison output already exists.")
    labels_path = dataset_dir / "labels.json"
    labels = json.loads(labels_path.read_text())
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    query_hash = hashlib.sha256((dataset_dir / "queries.jsonl").read_bytes()).hexdigest()
    common, runs = None, []
    for directory in run_dirs:
        metadata = json.loads((directory / "run.json").read_text())
        identity = {key: metadata[key] for key in ("query_sha256", "index_fingerprint", "embedding_fingerprint", "context_mode")}
        if allow_context_change:
            del identity["context_mode"]
            identity["query_field"] = metadata["query_field"]
            identity["translation_sha256"] = metadata.get("translation_sha256")
        identity["corpus_fingerprint"] = metadata["integrity"]["corpus_fingerprint"]
        if identity["query_sha256"] != query_hash or identity["corpus_fingerprint"] != manifest["corpus_fingerprint"]:
            raise ValueError("Comparison dataset differs from a run.")
        if common is not None and identity != common:
            raise ValueError("Comparison requires identical corpus, queries, index, embedding and context mode.")
        common = identity
        if metadata.get("translation") == "frozen_live_outputs":
            if hashlib.sha256((directory / "translations.json").read_bytes()).hexdigest() != metadata["translation_sha256"]:
                raise ValueError("Frozen translations changed after retrieval.")
        results = json.loads((directory / "results.json").read_text())
        runs.append({"run_dir": str(directory), "query_field": metadata["query_field"],
                     "context_mode": metadata["context_mode"],
                     "results_sha256": hashlib.sha256((directory / "results.json").read_bytes()).hexdigest(),
                     "score": score(results, labels)})
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, {"suite": "rag", "identity": common,
                        "labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
                        "label_review": "model_assisted_not_human_approved", "runs": runs})


def diagnose(dataset_dir: Path, run_dirs: list[Path], query_ids: list[str], output: Path) -> None:
    """Locate known evidence in full candidate traces; never infer missing labels."""
    labels = json.loads((dataset_dir / "labels.json").read_text())
    details = []
    for directory in run_dirs:
        for qid in query_ids:
            if qid not in labels:
                raise ValueError("Unknown diagnostic query ID.")
            trace = json.loads((directory / f"{qid}-trace.json").read_text())
            known = {j["chunk_id"]: j["grade"] for j in labels[qid]["judgments"]}
            direct = [cid for cid, grade in known.items() if grade == 2]
            routes = {route: trace.get(key, []) for route, key in (("dense", "dense"), ("bm25", "lexical"))}
            details.append({"run_dir": str(directory), "query_id": qid, "query_text": trace.get("query"),
                            "known_direct_ranks": {cid: {route: next((i for i, row in enumerate(rows, 1)
                                if row["chunk_id"] == cid), None) for route, rows in routes.items()} for cid in direct},
                            "candidates": {route: [{"rank": i, "chunk_id": row["chunk_id"],
                                "grade": known.get(row["chunk_id"]), "text": row["text"]}
                                for i, row in enumerate(rows, 1)] for route, rows in routes.items()}})
    compare(dataset_dir, run_dirs, output, allow_context_change=True)
    result = json.loads(output.read_text())
    result["diagnostics"] = details
    result["rank_scope"] = "null means absent from recorded route candidates, not absent from corpus"
    write_json(output, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build-index")
    build_parser.add_argument("--corpus", type=Path, required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--query-field", choices=("query_zh", "query_en_reference"), default="query_en_reference")
    run_parser.add_argument("--translations-dir", type=Path, help="Frozen live translation output; overrides --query-field.")
    run_parser.add_argument("--context-mode", choices=("question_only_no_skill_expansion", "verified_board_v1"),
                            default="question_only_no_skill_expansion")
    report_parser = sub.add_parser("report")
    compare_parser = sub.add_parser("compare")
    compare_parser.add_argument("--dataset-dir", type=Path, required=True)
    compare_parser.add_argument("--run-dirs", nargs="+", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    compare_parser.add_argument("--allow-context-change", action="store_true",
                                help="Compare context modes with identical query language and frozen translations.")
    diagnose_parser = sub.add_parser("diagnose")
    diagnose_parser.add_argument("--dataset-dir", type=Path, required=True)
    diagnose_parser.add_argument("--run-dirs", nargs="+", type=Path, required=True)
    diagnose_parser.add_argument("--query-ids", nargs="+", required=True)
    diagnose_parser.add_argument("--output", type=Path, required=True)
    for child in (run_parser, report_parser):
        child.add_argument("--dataset-dir", type=Path, required=True)
        child.add_argument("--output-dir", type=Path, required=True)
    for child in (build_parser, run_parser):
        child.add_argument("--data-dir", type=Path, required=True)
        child.add_argument("--model-path", type=Path, required=True)
        child.add_argument("--device", default="auto")
        child.add_argument("--batch-size", type=int, default=1)
    args = vars(parser.parse_args())
    command = args.pop("command")
    {"build-index": build, "run": run, "report": report, "compare": compare, "diagnose": diagnose}[command](**args)


if __name__ == "__main__":
    main()

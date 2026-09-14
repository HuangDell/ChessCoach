"""Validate a local draft against an explicitly selected, read-only corpus.

This checks dataset integrity, not semantic correctness or retrieval quality.
No default data directory, model, Engine, or network access is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

import chess


def validate(dataset_dir: Path, corpus: Path) -> dict:
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    queries = [json.loads(line) for line in (dataset_dir / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    labels = json.loads((dataset_dir / "labels.json").read_text(encoding="utf-8"))
    with sqlite3.connect(corpus.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        rows = connection.execute("SELECT * FROM chunks ORDER BY book_id, ordinal").fetchall()
    fingerprint = hashlib.sha256("\0".join([
        meta["corpus_version"], meta["source_collection_hash"],
        *(f"{row['chunk_id']}:{row['text_hash']}" for row in rows),
    ]).encode("utf-8")).hexdigest()

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(fingerprint == manifest["corpus_fingerprint"], "Corpus fingerprint mismatch; do not reuse stale labels.")
    require(len(queries) == manifest["query_count"], "Query count mismatch.")
    ids = [q["query_id"] for q in queries]
    require(len(set(ids)) == len(ids), "Duplicate query IDs.")
    require(len({q["query_zh"] for q in queries}) == len(queries), "Duplicate Chinese queries.")
    require(set(ids) == set(labels), "Queries and labels must have identical IDs.")
    chunks = {row["chunk_id"]: row for row in rows}
    positions = 0
    evidence_count = 0
    for query in queries:
        qid = query["query_id"]
        require(set(query) == {"query_id", "query_zh", "query_en_reference", "context"}, f"{qid}: unexpected model-input fields.")
        require(any("\u4e00" <= char <= "\u9fff" for char in query["query_zh"]), f"{qid}: missing Chinese question.")
        require(bool(query["query_en_reference"].strip()), f"{qid}: missing English reference translation.")
        label = labels[qid]
        require(label["review_status"] == "draft_pending_human_review", f"{qid}: draft must not claim human approval.")
        require(label["qrels_complete"] is False, f"{qid}: draft judgments are not exhaustive.")
        require(bool(label["expected_points_zh"]) and bool(label["group_id"]), f"{qid}: missing rubric or grouping.")
        context = query["context"]
        if context is not None:
            positions += 1
            board = chess.Board(context["initial_fen"])
            require(board.is_valid(), f"{qid}: invalid initial FEN.")
            for san in context["moves_san"]:
                board.push_san(san)
            require(board.fen() == context["fen"], f"{qid}: replay/FEN mismatch.")
            require(board.is_valid(), f"{qid}: invalid final position.")
            require(context["side_to_move"] == ("white" if board.turn else "black"), f"{qid}: wrong side to move.")
            require(context["piece_map"] == {chess.square_name(s): p.symbol() for s, p in sorted(board.piece_map().items())}, f"{qid}: piece map mismatch.")
            require(context["in_check"] == board.is_check(), f"{qid}: check status mismatch.")
            for uci in context["candidate_moves_uci"]:
                require(chess.Move.from_uci(uci) in board.legal_moves, f"{qid}: illegal candidate {uci}.")
        require(label["kind"] not in {"position", "real_position"} or context is not None, f"{qid}: missing position.")
        if label["kind"] == "real_position":
            require(context["origin"] == "user_provided_fen", f"{qid}: real position needs user provenance.")
            require(context["moves_san"] == [] and context["initial_fen"] == context["fen"], f"{qid}: FEN-only input must not invent move history.")
        require(label["kind"] != "real_missing_context" or context is None, f"{qid}: invented real-user position.")
        seen = set()
        for field in ("judgments", "diagnostic_sources"):
            for judgment in label[field]:
                evidence_count += 1
                chunk_id = judgment["chunk_id"]
                require(chunk_id in chunks and chunk_id not in seen, f"{qid}: missing or duplicate source.")
                seen.add(chunk_id)
                row = chunks[chunk_id]
                require(judgment["text_hash"] == row["text_hash"], f"{qid}: stale source hash.")
                require(judgment["book_id"] == row["book_id"] and judgment["ordinal"] == row["ordinal"], f"{qid}: wrong source identity.")
                require(judgment["source_locator"] == row["source_locator"], f"{qid}: wrong source locator.")
                require(bool(judgment["reason_zh"]), f"{qid}: missing judgment rationale.")
                if field == "judgments":
                    require(judgment["grade"] in (0, 1, 2), f"{qid}: invalid grade.")
        if label["answerability"] == "supported":
            require(any(j["grade"] == 2 for j in label["judgments"]), f"{qid}: no direct supporting passage.")
    return {
        "status": "integrity_checks_passed",
        "queries": len(queries), "verified_positions": positions,
        "source_references": evidence_count,
        "kinds": dict(Counter(label["kind"] for label in labels.values())),
        "corpus_fingerprint": fingerprint,
        "semantic_review": "pending", "retrieval_metrics": "not_run",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.dataset_dir, args.corpus), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

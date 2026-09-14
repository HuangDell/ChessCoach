"""Freeze a new local corpus and materialize explicitly reviewed judgments.

No similarity-based label acceptance: decisions must identify each selected v3
book/ordinal, grade and rationale. Inputs/outputs are explicit local paths.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def prepare(source: Path, corpus: Path, decisions: Path, output: Path) -> dict:
    if source.resolve() == output.resolve():
        raise ValueError("A new dataset directory is required; preserve the old dataset.")
    review = json.loads(decisions.read_text(encoding="utf-8"))
    old = json.loads((source / "labels.json").read_text(encoding="utf-8"))
    if set(review["queries"]) != set(old):
        raise ValueError("Every query requires an explicit review decision.")
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "corpus-v3.sqlite3"
    if corpus.resolve() != snapshot.resolve():
        if snapshot.exists():
            raise ValueError("Refusing to overwrite an existing corpus snapshot.")
        with sqlite3.connect(corpus.resolve().as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(snapshot) as dst:
                src.backup(dst)
    with sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        rows = connection.execute("SELECT * FROM chunks ORDER BY book_id, ordinal").fetchall()
        books = [dict(row) for row in connection.execute("SELECT * FROM books ORDER BY book_id")]
    fingerprint = hashlib.sha256("\0".join([
        meta["corpus_version"], meta["source_collection_hash"],
        *(f"{row['chunk_id']}:{row['text_hash']}" for row in rows),
    ]).encode("utf-8")).hexdigest()
    if fingerprint != review["corpus_fingerprint"]:
        raise ValueError("Review decisions target a different corpus fingerprint.")
    by_key = {(row["book_id"], row["ordinal"]): row for row in rows}
    by_book = {book["book_id"]: book for book in books}
    labels = {}
    for qid, decision in review["queries"].items():
        label = old[qid].copy()
        label.update(decision.get("updates", {}))
        label.update(review_status="model_reviewed", qrels_complete=False,
                     independent_human_review=False, reviewer=review["reviewer"],
                     review_notes_zh=decision["review_notes_zh"])
        for field in ("judgments", "diagnostic_sources"):
            label[field] = []
            for choice in decision.get(field, []):
                book_id = review["book_aliases"][choice[0]]
                row = by_key[(book_id, choice[1])]
                book = by_book[book_id]
                judgment = {key: row[key] for key in (
                    "chunk_id", "book_id", "ordinal", "text_hash", "source_locator")}
                judgment.update(title=book["title"], author=book["author"],
                                heading_path=json.loads(row["heading_path"]),
                                reason_zh=choice[3])
                if field == "judgments":
                    if choice[2] not in (0, 1, 2):
                        raise ValueError("Invalid relevance grade.")
                    judgment["grade"] = choice[2]
                label[field].append(judgment)
        labels[qid] = label
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(dataset_version="zh-en-rag-model-reviewed-v2", status="model_reviewed",
                    corpus_version=meta["corpus_version"], corpus_schema_version=int(meta["schema_version"]),
                    corpus_fingerprint=fingerprint, corpus_chunk_count=len(rows), books=books,
                    corpus_snapshot_path=snapshot.name, reviewer=review["reviewer"],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    revision_note="Explicit passage review against v3; image assets retained but no verified diagram FEN.")
    manifest["judgments"].update(complete=False, independent_human_review=False,
                                review_status="model_reviewed", retrieval_ranks_used=False,
                                metrics="not_run", review_scope="seed evidence and selected hard negatives; pool review pending")
    manifest["split"] = "unassigned; shared source groups must remain together; no held-out claims"
    for filename in ("queries.jsonl", "translation_prompt.md"):
        shutil.copyfile(source / filename, output / filename)
    for filename, value in (("labels.json", labels), ("manifest.json", manifest)):
        (output / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"queries": len(labels), "corpus_fingerprint": fingerprint,
            "grades": dict(Counter(j["grade"] for label in labels.values() for j in label["judgments"]))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "corpus", "decisions", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.corpus, args.decisions, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

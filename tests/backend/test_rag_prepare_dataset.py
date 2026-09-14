"""Dataset preparation tests use only synthetic temporary SQLite data."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.evals.rag.prepare_dataset import prepare


class PrepareDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "old"
        self.source.mkdir()
        self.corpus = self.root / "corpus.sqlite3"
        with sqlite3.connect(self.corpus) as db:
            db.executescript("""
                CREATE TABLE meta (key TEXT, value TEXT);
                INSERT INTO meta VALUES ('corpus_version','book-corpus-v3'),
                    ('schema_version','3'), ('source_collection_hash','source');
                CREATE TABLE books (book_id TEXT, title TEXT, author TEXT);
                INSERT INTO books VALUES ('b','Synthetic','Test');
                CREATE TABLE chunks (chunk_id TEXT, book_id TEXT, ordinal INTEGER,
                    heading_path TEXT, source_locator TEXT, text_hash TEXT);
                INSERT INTO chunks VALUES ('c','b',0,'[]','block-1','hash');
            """)
        fingerprint = hashlib.sha256("book-corpus-v3\0source\0c:hash".encode()).hexdigest()
        (self.source / "manifest.json").write_text(json.dumps({"judgments": {}}))
        (self.source / "labels.json").write_text(json.dumps({"Q1": {"expected_points_zh": ["测试"]}}))
        (self.source / "queries.jsonl").write_text('{"query_id":"Q1"}\n')
        (self.source / "translation_prompt.md").write_text("Synthetic prompt")
        self.review = {"corpus_fingerprint": fingerprint, "reviewer": {"type": "model_assisted_review"},
                       "book_aliases": {"B": "b"}, "queries": {"Q1": {
                           "judgments": [["B", 0, 2, "Explicit reviewed reason"]],
                           "review_notes_zh": "Reviewed synthetic passage"}}}
        self.decisions = self.root / "decisions.json"
        self.decisions.write_text(json.dumps(self.review))

    def test_explicit_decisions_preserve_queries_and_review_limitations(self):
        output = self.root / "new"
        result = prepare(self.source, self.corpus, self.decisions, output)
        label = json.loads((output / "labels.json").read_text())["Q1"]
        self.assertEqual(result["grades"], {2: 1})
        self.assertEqual(label["judgments"][0]["chunk_id"], "c")
        self.assertEqual(label["review_status"], "model_reviewed")
        self.assertFalse(label["independent_human_review"])
        self.assertFalse(label["qrels_complete"])
        self.assertEqual((output / "queries.jsonl").read_bytes(), (self.source / "queries.jsonl").read_bytes())
        self.assertTrue((output / "corpus-v3.sqlite3").exists())

    def test_stale_review_is_not_accepted(self):
        self.review["corpus_fingerprint"] = "stale"
        self.decisions.write_text(json.dumps(self.review))
        with self.assertRaisesRegex(ValueError, "different corpus fingerprint"):
            prepare(self.source, self.corpus, self.decisions, self.root / "new")
        self.assertFalse((self.root / "new" / "labels.json").exists())

    def test_original_dataset_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, "preserve the old dataset"):
            prepare(self.source, self.corpus, self.decisions, self.source)


if __name__ == "__main__":
    unittest.main()

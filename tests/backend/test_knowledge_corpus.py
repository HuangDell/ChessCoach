from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zipfile

from server.core.knowledge import (
    BookParseError,
    CorpusBuildError,
    build_corpus,
    get_corpus_status,
    inspect_book,
    list_books,
)
from server.core.knowledge.chunking import MAX_UNITS, count_units
import server.core.knowledge.corpus as corpus_module


def _epub_bytes(
    *,
    first: str = "<h1>First</h1><p>First body.</p>",
    second: str = "<h1>Second</h1><p>Second body.</p>",
    extra_name: str | None = None,
) -> bytes:
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OPS/content.opf"
        media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0"
             unique-identifier="id">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Practical Chess</dc:title>
        <dc:creator>A. Coach</dc:creator>
        <dc:language>en</dc:language>
      </metadata>
      <manifest>
        <item id="two" href="chapter-two.xhtml" media-type="application/xhtml+xml"/>
        <item id="one" href="chapter-one.xhtml" media-type="application/xhtml+xml"/>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        <item id="image" href="cover.png" media-type="image/png"/>
      </manifest>
      <spine>
        <itemref idref="one"/>
        <itemref idref="nav"/>
        <itemref idref="two"/>
      </spine>
    </package>"""
    nav = "<html><body><nav><p>Navigation must not be indexed.</p></nav></body></html>"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/content.opf", opf)
        archive.writestr("OPS/chapter-one.xhtml", f"<html><body>{first}</body></html>")
        archive.writestr("OPS/chapter-two.xhtml", f"<html><body>{second}</body></html>")
        archive.writestr("OPS/nav.xhtml", nav)
        archive.writestr("OPS/cover.png", b"not-a-real-image")
        if extra_name is not None:
            archive.writestr(extra_name, b"unsafe")
    return buffer.getvalue()


class KnowledgeCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="knowledge-corpus-")
        self.data_dir = Path(self.temporary.name)
        self.books_dir = self.data_dir / "knowledge" / "books"
        self.books_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, name: str, content: bytes) -> Path:
        path = self.books_dir / name
        path.write_bytes(content)
        return path

    def test_epub_uses_spine_metadata_headings_and_body_blocks(self) -> None:
        first = """
          <head><title>Ignored document title</title><style>hidden</style></head>
          <h1>Checks</h1><p>Start with checks.</p>
          <h2>Candidate moves</h2><ul><li>1. e4 e5 2. Nf3</li></ul>
          <blockquote><p>Calculate the reply.</p><p>Then compare.</p></blockquote>
          <pre>line one\n  line two</pre>
          <figure><img src="cover.png"/><figcaption>Board after Nf3.</figcaption></figure>
          <script>secret instructions</script>
        """
        self._write("coach.epub", _epub_bytes(first=first))

        result = build_corpus(self.data_dir)
        self.assertEqual(1, result.book_count)
        book = list_books(self.data_dir)[0]
        self.assertEqual("Practical Chess", book.title)
        self.assertEqual("A. Coach", book.author)
        self.assertEqual("en", book.language)
        self.assertEqual("epub", book.format)

        inspection = inspect_book(self.data_dir, book.book_id, 20)
        texts = [chunk.text for chunk in inspection.chunks]
        combined = "\n".join(texts)
        self.assertLess(combined.index("Start with checks."), combined.index("Second body."))
        self.assertIn("1. e4 e5 2. Nf3", combined)
        self.assertIn("Calculate the reply. Then compare.", combined)
        self.assertIn("line one\n  line two", combined)
        self.assertIn("Board after Nf3.", combined)
        self.assertNotIn("Navigation", combined)
        self.assertNotIn("secret instructions", combined)
        self.assertEqual(("Checks",), inspection.chunks[0].heading_path)
        self.assertIn(("Checks", "Candidate moves"), [item.heading_path for item in inspection.chunks])
        self.assertTrue(inspection.chunks[0].source_locator.startswith("OPS/chapter-one.xhtml"))

    def test_markdown_txt_normalization_heading_paths_and_code(self) -> None:
        markdown = (
            "\ufeff# Ｃａｌｃｕｌａｔｉｏｎ\n\n"
            "Always inspect 1. e4 e5 2. Nf3 before committing.\n\n"
            "## Forcing moves\n\nChecks, captures, and threats.\n\n"
            "```text\n1. Bxh7+ Kxh7\n  2. Ng5+\n```\n"
        ).encode("utf-8")
        self._write("lesson.md", markdown)
        self._write("notes.txt", "Loose pieces\n\nDo not skip opponent replies.".encode())

        build_corpus(self.data_dir)
        books = {book.format: book for book in list_books(self.data_dir)}
        self.assertEqual({"markdown", "txt"}, set(books))
        markdown_chunks = inspect_book(self.data_dir, books["markdown"].book_id, 20).chunks
        self.assertEqual("Calculation", books["markdown"].title)
        self.assertEqual(("Calculation",), markdown_chunks[0].heading_path)
        self.assertEqual(("Calculation", "Forcing moves"), markdown_chunks[1].heading_path)
        combined = "\n".join(chunk.text for chunk in markdown_chunks)
        self.assertIn("1. e4 e5 2. Nf3", combined)
        self.assertIn("1. Bxh7+ Kxh7\n  2. Ng5+", combined)
        txt_chunks = inspect_book(self.data_dir, books["txt"].book_id, 10).chunks
        self.assertEqual(("notes",), txt_chunks[0].heading_path)

    def test_chunking_respects_limit_sentences_and_bounded_overlap(self) -> None:
        paragraphs = [character * 100 for character in "甲乙丙丁戊"]
        long_paragraph = "".join(f"长句{i}。" for i in range(220))
        body = "# Blocks\n\n" + "\n\n".join(paragraphs) + "\n\n## Long\n\n" + long_paragraph
        self._write("long.md", body.encode())

        build_corpus(self.data_dir)
        book = list_books(self.data_dir)[0]
        chunks = inspect_book(self.data_dir, book.book_id, 50).chunks
        self.assertGreaterEqual(len(chunks), 4)
        self.assertTrue(all(0 < chunk.unit_count <= MAX_UNITS for chunk in chunks))
        self.assertTrue(all(chunk.unit_count == count_units(chunk.text) for chunk in chunks))
        block_chunks = [chunk for chunk in chunks if chunk.heading_path == ("Blocks",)]
        self.assertGreaterEqual(len(block_chunks), 2)
        self.assertTrue(block_chunks[0].text.endswith("丁" * 100))
        self.assertTrue(block_chunks[1].text.startswith("丁" * 40))
        long_chunks = [chunk for chunk in chunks if chunk.heading_path == ("Blocks", "Long")]
        self.assertGreaterEqual(len(long_chunks), 2)
        self.assertIn("长句0。", long_chunks[0].text)
        self.assertIn("长句219。", long_chunks[-1].text)

    def test_ids_are_stable_duplicates_deduplicate_and_content_changes_identity(self) -> None:
        source = self._write("guide.md", b"# Fixed title\n\nChecks before captures.\n")
        first_result = build_corpus(self.data_dir)
        first_book = list_books(self.data_dir)[0]
        first_chunks = inspect_book(self.data_dir, first_book.book_id, 20).chunks

        second_result = build_corpus(self.data_dir)
        second_book = list_books(self.data_dir)[0]
        second_chunks = inspect_book(self.data_dir, second_book.book_id, 20).chunks
        self.assertEqual(first_result.source_collection_hash, second_result.source_collection_hash)
        self.assertEqual(first_book.book_id, second_book.book_id)
        self.assertEqual([item.chunk_id for item in first_chunks], [item.chunk_id for item in second_chunks])

        duplicate = self._write("copy.md", source.read_bytes())
        duplicate_result = build_corpus(self.data_dir)
        self.assertEqual(1, duplicate_result.book_count)
        self.assertTrue(any("Duplicate book" in warning for warning in duplicate_result.warnings))
        duplicate.unlink()
        renamed = source.with_name("renamed.md")
        source.rename(renamed)
        build_corpus(self.data_dir)
        renamed_book = list_books(self.data_dir)[0]
        renamed_chunks = inspect_book(self.data_dir, renamed_book.book_id, 20).chunks
        self.assertEqual(first_book.book_id, renamed_book.book_id)
        self.assertEqual([item.chunk_id for item in first_chunks], [item.chunk_id for item in renamed_chunks])

        renamed.write_text("# Fixed title\n\nChecks, captures, then threats.\n", encoding="utf-8")
        build_corpus(self.data_dir)
        changed_book = list_books(self.data_dir)[0]
        changed_chunks = inspect_book(self.data_dir, changed_book.book_id, 20).chunks
        self.assertNotEqual(first_book.book_id, changed_book.book_id)
        self.assertNotEqual(
            [item.chunk_id for item in first_chunks],
            [item.chunk_id for item in changed_chunks],
        )

    def test_parse_failures_and_unsafe_epub_preserve_existing_corpus(self) -> None:
        valid = self._write("valid.txt", b"Valid body text.")
        build_corpus(self.data_dir)
        corpus_path = self.data_dir / "knowledge" / "corpus.sqlite3"
        baseline = corpus_path.read_bytes()

        failures = (
            ("broken.epub", b"not a zip", "broken.epub"),
            ("latin1.txt", b"\xff\xfeinvalid", "latin1.txt"),
            ("empty.md", b"# Heading only\n", "empty.md"),
            ("unsafe.epub", _epub_bytes(extra_name="../escape.txt"), "unsafe archive path"),
        )
        for filename, content, expected in failures:
            with self.subTest(filename=filename):
                path = self._write(filename, content)
                with self.assertRaises((BookParseError, CorpusBuildError)) as captured:
                    build_corpus(self.data_dir)
                self.assertIn(expected, str(captured.exception))
                self.assertEqual(baseline, corpus_path.read_bytes())
                path.unlink()
        self.assertTrue(valid.exists())

    def test_source_change_and_storage_failure_preserve_existing_corpus(self) -> None:
        source = self._write("guide.md", b"# Guide\n\nOriginal text.\n")
        build_corpus(self.data_dir)
        corpus_path = self.data_dir / "knowledge" / "corpus.sqlite3"
        baseline = corpus_path.read_bytes()
        real_parse = corpus_module.parse_book

        def mutate_after_parse(source_name: str, content: bytes, book_id: str):
            parsed = real_parse(source_name, content, book_id)
            source.write_text("# Guide\n\nChanged during build.\n", encoding="utf-8")
            return parsed

        with mock.patch.object(corpus_module, "parse_book", side_effect=mutate_after_parse):
            with self.assertRaisesRegex(CorpusBuildError, "changed during corpus construction"):
                build_corpus(self.data_dir)
        self.assertEqual(baseline, corpus_path.read_bytes())

        with mock.patch.object(corpus_module.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaisesRegex(CorpusBuildError, "Corpus storage failed"):
                build_corpus(self.data_dir)
        self.assertEqual(baseline, corpus_path.read_bytes())

    def test_unsupported_files_are_reported_but_valid_sources_build(self) -> None:
        self._write("guide.md", b"# Guide\n\nValid content.\n")
        self._write("scan.pdf", b"%PDF-test")
        self._write("notes.rtf", b"unsupported")

        result = build_corpus(self.data_dir)
        self.assertEqual(1, result.book_count)
        self.assertTrue(any("scan.pdf" in warning for warning in result.warnings))
        status = get_corpus_status(self.data_dir)
        self.assertTrue(status.available)
        self.assertEqual(("notes.rtf", "scan.pdf"), status.unsupported_files)

        other = Path(self.temporary.name) / "unsupported-only"
        other_books = other / "knowledge" / "books"
        other_books.mkdir(parents=True)
        (other_books / "book.pdf").write_bytes(b"%PDF")
        with self.assertRaisesRegex(CorpusBuildError, "Unsupported files: book.pdf"):
            build_corpus(other)

    def test_sqlite_schema_integrity_foreign_keys_and_hashes(self) -> None:
        content = b"# Guide\n\nChecks before captures.\n"
        self._write("guide.md", content)
        result = build_corpus(self.data_dir)
        expected_book_id = hashlib.sha256(content).hexdigest()
        self.assertEqual(expected_book_id, list_books(self.data_dir)[0].book_id)

        connection = sqlite3.connect(result.corpus_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertEqual({"meta", "books", "chunks"}, tables)
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            chunk_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(chunks)").fetchall()
            }
            self.assertNotIn("embedding", chunk_columns)
            self.assertTrue(
                {
                    "chunk_id",
                    "book_id",
                    "ordinal",
                    "heading_path",
                    "source_locator",
                    "text",
                    "text_hash",
                    "unit_count",
                }.issubset(chunk_columns)
            )
            meta = dict(connection.execute("SELECT key, value FROM meta"))
            self.assertEqual("book-corpus-v1", meta["corpus_version"])
            self.assertEqual(result.source_collection_hash, meta["source_collection_hash"])
            self.assertEqual(
                {"max_units": 480, "overlap_units": 40, "target_units": 320},
                json.loads(meta["chunking"]),
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

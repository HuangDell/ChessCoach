from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from server.knowledge import main
from server.core.knowledge import list_books


class KnowledgeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="knowledge-cli-")
        self.data_dir = Path(self.temporary.name)
        books = self.data_dir / "knowledge" / "books"
        books.mkdir(parents=True)
        (books / "guide.md").write_text(
            "# Calculation\n\nInspect forcing moves first.\n", encoding="utf-8"
        )
        (books / "later.pdf").write_bytes(b"%PDF")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--data-dir", str(self.data_dir), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_build_status_books_and_inspect_commands(self) -> None:
        code, output, errors = self._run("build")
        self.assertEqual(0, code)
        self.assertIn("Books: 1", output)
        self.assertIn("Chunks: 1", output)
        self.assertIn("later.pdf", errors)

        code, output, errors = self._run("status")
        self.assertEqual(0, code)
        self.assertIn("Corpus: ready", output)
        self.assertIn("Version: book-corpus-v1 (schema 1)", output)
        self.assertIn("Unsupported files: later.pdf", output)
        self.assertEqual("", errors)

        book_id = list_books(self.data_dir)[0].book_id
        code, output, errors = self._run("books")
        self.assertEqual(0, code)
        self.assertIn(book_id, output)
        self.assertIn("Calculation", output)
        self.assertEqual("", errors)

        code, output, errors = self._run("inspect", book_id, "--limit", "1")
        self.assertEqual(0, code)
        self.assertIn("Calculation", output)
        self.assertIn("Inspect forcing moves first.", output)
        self.assertEqual("", errors)

    def test_build_and_inspect_failures_return_one(self) -> None:
        empty_dir = Path(self.temporary.name) / "empty"
        code_stdout = io.StringIO()
        code_stderr = io.StringIO()
        with redirect_stdout(code_stdout), redirect_stderr(code_stderr):
            code = main(["build", "--data-dir", str(empty_dir)])
        self.assertEqual(1, code)
        self.assertIn("No supported books", code_stderr.getvalue())

        self.assertEqual(0, self._run("build")[0])
        code, _output, errors = self._run("inspect", "missing", "--limit", "1")
        self.assertEqual(1, code)
        self.assertIn("not present", errors)


if __name__ == "__main__":
    unittest.main()

"""Atomic SQLite snapshot construction and read APIs for local books."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from urllib.parse import quote

from .chunking import MAX_UNITS, OVERLAP_UNITS, TARGET_UNITS, chunk_book
from .models import (
    BookInspection,
    BookNotFoundError,
    BookSummary,
    BuildResult,
    Chunk,
    CORPUS_VERSION,
    CorpusBuildError,
    CorpusReadError,
    CorpusChunkRecord,
    CorpusSnapshot,
    CorpusStatus,
    KnowledgeError,
    SCHEMA_VERSION,
)
from .parsers import SUPPORTED_EXTENSIONS, parse_book


_CORPUS_FILENAME = "corpus.sqlite3"


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    name: str
    content: bytes
    digest: str


def _paths(data_dir: str | os.PathLike[str]) -> tuple[Path, Path, Path]:
    knowledge_dir = Path(data_dir).expanduser() / "knowledge"
    return knowledge_dir, knowledge_dir / "books", knowledge_dir / _CORPUS_FILENAME


def _directory_entries(books_dir: Path) -> list[Path]:
    if not books_dir.exists():
        return []
    try:
        return sorted(books_dir.iterdir(), key=lambda item: (item.name.casefold(), item.name))
    except OSError as exc:
        raise CorpusBuildError(f"Cannot scan books directory '{books_dir}': {exc}.") from exc


def _unsupported_files(books_dir: Path) -> tuple[str, ...]:
    unsupported: list[str] = []
    for path in _directory_entries(books_dir):
        if path.is_symlink():
            unsupported.append(path.name)
        elif path.is_file() and path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            unsupported.append(path.name)
    return tuple(unsupported)


def _snapshot_sources(books_dir: Path) -> tuple[list[_SourceSnapshot], tuple[str, ...]]:
    snapshots: list[_SourceSnapshot] = []
    unsupported: list[str] = []
    for path in _directory_entries(books_dir):
        if path.is_symlink():
            unsupported.append(path.name)
            continue
        if not path.is_file():
            continue
        if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            unsupported.append(path.name)
            continue
        try:
            before = path.stat()
            content = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise CorpusBuildError(f"Cannot read source book '{path.name}': {exc}.") from exc
        before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_signature != after_signature:
            raise CorpusBuildError(f"Source book '{path.name}' changed while it was being read.")
        snapshots.append(
            _SourceSnapshot(
                name=path.name,
                content=content,
                digest=hashlib.sha256(content).hexdigest(),
            )
        )
    return snapshots, tuple(unsupported)


def _verify_sources_unchanged(books_dir: Path, expected: list[_SourceSnapshot]) -> None:
    expected_hashes = {item.name: item.digest for item in expected}
    current_hashes: dict[str, str] = {}
    for path in _directory_entries(books_dir):
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            current_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise CorpusBuildError(f"Cannot re-read source book '{path.name}': {exc}.") from exc
    if current_hashes != expected_hashes:
        raise CorpusBuildError(
            "The supported book collection changed during corpus construction; retry the build."
        )


def _collection_hash(book_ids: set[str]) -> str:
    payload = "book-corpus-sources-v1\0" + "\0".join(sorted(book_ids))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE books (
            book_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            language TEXT NOT NULL,
            format TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_size INTEGER NOT NULL CHECK (source_size >= 0),
            source TEXT NOT NULL,
            source_uri TEXT NOT NULL,
            rights TEXT NOT NULL,
            chunk_count INTEGER NOT NULL CHECK (chunk_count > 0)
        ) WITHOUT ROWID;
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            heading_path TEXT NOT NULL,
            source_locator TEXT NOT NULL,
            text TEXT NOT NULL CHECK (length(text) > 0),
            text_hash TEXT NOT NULL,
            unit_count INTEGER NOT NULL CHECK (unit_count > 0),
            UNIQUE (book_id, ordinal)
        );
        CREATE INDEX chunks_book_order ON chunks(book_id, ordinal);
        """
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def build_corpus(data_dir: str | os.PathLike[str]) -> BuildResult:
    """Fully rebuild the local corpus and atomically replace the prior snapshot."""
    knowledge_dir, books_dir, corpus_path = _paths(data_dir)
    try:
        books_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CorpusBuildError(f"Cannot create books directory '{books_dir}': {exc}.") from exc

    sources, unsupported = _snapshot_sources(books_dir)
    warnings = tuple(
        f"Unsupported file '{name}' was skipped. Supported formats: EPUB, TXT, Markdown."
        for name in unsupported
    )
    if not sources:
        unsupported_detail = (
            " Unsupported files: " + ", ".join(unsupported) + "." if unsupported else ""
        )
        raise CorpusBuildError(
            f"No supported books found in '{books_dir}'. Add an EPUB, UTF-8 TXT, or Markdown file."
            f"{unsupported_detail}"
        )

    books_and_chunks = []
    seen_book_ids: set[str] = set()
    duplicate_warnings: list[str] = []
    for source in sources:
        if source.digest in seen_book_ids:
            duplicate_warnings.append(
                f"Duplicate book '{source.name}' was skipped because identical content is already indexed."
            )
            continue
        book = parse_book(source.name, source.content, source.digest)
        chunks = chunk_book(book)
        if not chunks:
            raise CorpusBuildError(f"Book '{source.name}' produced no indexable chunks.")
        seen_book_ids.add(source.digest)
        books_and_chunks.append((book, chunks))
    if not books_and_chunks:
        raise CorpusBuildError("The supported books contain no extractable body text.")

    source_collection_hash = _collection_hash(seen_book_ids)
    built_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".corpus-", suffix=".sqlite3.tmp", dir=knowledge_dir
        )
        os.close(descriptor)
    except OSError as exc:
        raise CorpusBuildError(f"Cannot create a temporary corpus in '{knowledge_dir}': {exc}.") from exc

    temporary_path = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            _create_schema(connection)
            meta = {
                "corpus_version": CORPUS_VERSION,
                "schema_version": str(SCHEMA_VERSION),
                "source_collection_hash": source_collection_hash,
                "built_at": built_at,
                "chunking": json.dumps(
                    {
                        "target_units": TARGET_UNITS,
                        "max_units": MAX_UNITS,
                        "overlap_units": OVERLAP_UNITS,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta.items())
            for book, chunks in books_and_chunks:
                connection.execute(
                    """
                    INSERT INTO books(
                        book_id, title, author, language, format, source_name, source_size,
                        source, source_uri, rights, chunk_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book.book_id,
                        book.title,
                        book.author,
                        book.language,
                        book.format,
                        book.source_name,
                        book.source_size,
                        book.source,
                        book.source_uri,
                        book.rights,
                        len(chunks),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO chunks(
                        chunk_id, book_id, ordinal, heading_path, source_locator,
                        text, text_hash, unit_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            chunk.chunk_id,
                            chunk.book_id,
                            chunk.ordinal,
                            json.dumps(chunk.heading_path, ensure_ascii=False, separators=(",", ":")),
                            chunk.source_locator,
                            chunk.text,
                            chunk.text_hash,
                            chunk.unit_count,
                        )
                        for chunk in chunks
                    ],
                )
            connection.commit()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise CorpusBuildError("Temporary corpus failed its foreign-key check.")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise CorpusBuildError("Temporary corpus failed its SQLite integrity check.")
        finally:
            connection.close()
        _fsync_file(temporary_path)
        _verify_sources_unchanged(books_dir, sources)
        os.replace(temporary_path, corpus_path)
        _fsync_directory(knowledge_dir)
    except KnowledgeError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise CorpusBuildError(f"Corpus storage failed: {exc}.") from exc
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass

    return BuildResult(
        corpus_path=corpus_path,
        version=CORPUS_VERSION,
        source_collection_hash=source_collection_hash,
        book_count=len(books_and_chunks),
        chunk_count=sum(len(chunks) for _book, chunks in books_and_chunks),
        warnings=warnings + tuple(duplicate_warnings),
    )


def _read_connection(corpus_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(corpus_path.resolve()), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise CorpusReadError("The current corpus cannot be opened; run the build command again.") from exc


def get_corpus_status(data_dir: str | os.PathLike[str]) -> CorpusStatus:
    """Return current snapshot metadata without creating or modifying the corpus."""
    _knowledge_dir, books_dir, corpus_path = _paths(data_dir)
    try:
        unsupported = _unsupported_files(books_dir)
    except CorpusBuildError as exc:
        unsupported = ()
        scan_error = str(exc)
    else:
        scan_error = None
    if not corpus_path.is_file():
        return CorpusStatus(
            books_dir=books_dir,
            corpus_path=corpus_path,
            available=False,
            version=None,
            schema_version=None,
            book_count=0,
            chunk_count=0,
            source_collection_hash=None,
            built_at=None,
            unsupported_files=unsupported,
            error=scan_error,
        )
    try:
        connection = _read_connection(corpus_path)
        try:
            meta = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}
            book_count = int(connection.execute("SELECT count(*) FROM books").fetchone()[0])
            chunk_count = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
            schema_version = int(meta["schema_version"])
            version = meta["corpus_version"]
            source_hash = meta["source_collection_hash"]
            built_at = meta["built_at"]
        finally:
            connection.close()
    except (CorpusReadError, sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        return CorpusStatus(
            books_dir=books_dir,
            corpus_path=corpus_path,
            available=False,
            version=None,
            schema_version=None,
            book_count=0,
            chunk_count=0,
            source_collection_hash=None,
            built_at=None,
            unsupported_files=unsupported,
            error=scan_error
            or (
                f"The current corpus is invalid ({type(exc).__name__}); "
                "run the build command again."
            ),
        )
    return CorpusStatus(
        books_dir=books_dir,
        corpus_path=corpus_path,
        available=True,
        version=version,
        schema_version=schema_version,
        book_count=book_count,
        chunk_count=chunk_count,
        source_collection_hash=source_hash,
        built_at=built_at,
        unsupported_files=unsupported,
        error=scan_error,
    )


def list_books(data_dir: str | os.PathLike[str]) -> tuple[BookSummary, ...]:
    """List books in the current immutable corpus snapshot."""
    _knowledge_dir, _books_dir, corpus_path = _paths(data_dir)
    if not corpus_path.is_file():
        return ()
    connection = _read_connection(corpus_path)
    try:
        rows = connection.execute(
            """
            SELECT book_id, title, author, language, format, source_name, chunk_count,
                   source, source_uri, rights
            FROM books ORDER BY title COLLATE NOCASE, book_id
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise CorpusReadError("The current corpus cannot list books; run the build command again.") from exc
    finally:
        connection.close()
    return tuple(
        BookSummary(
            book_id=row["book_id"],
            title=row["title"],
            author=row["author"],
            language=row["language"],
            format=row["format"],
            source_name=row["source_name"],
            chunk_count=row["chunk_count"],
            source=row["source"],
            source_uri=row["source_uri"],
            rights=row["rights"],
        )
        for row in rows
    )


def inspect_book(
    data_dir: str | os.PathLike[str], book_id: str, limit: int = 10
) -> BookInspection:
    """Read a bounded, ordered sample of one book's chunks."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    _knowledge_dir, _books_dir, corpus_path = _paths(data_dir)
    if not corpus_path.is_file():
        raise CorpusReadError("No corpus has been built yet.")
    connection = _read_connection(corpus_path)
    try:
        row = connection.execute(
            """
            SELECT book_id, title, author, language, format, source_name, chunk_count,
                   source, source_uri, rights
            FROM books WHERE book_id = ?
            """,
            (book_id,),
        ).fetchone()
        if row is None:
            raise BookNotFoundError(f"Book '{book_id}' is not present in the current corpus.")
        chunk_rows = connection.execute(
            """
            SELECT chunk_id, book_id, ordinal, heading_path, source_locator,
                   text, text_hash, unit_count
            FROM chunks WHERE book_id = ? ORDER BY ordinal LIMIT ?
            """,
            (book_id, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CorpusReadError("The current corpus cannot be inspected; run the build command again.") from exc
    finally:
        connection.close()
    summary = BookSummary(
        book_id=row["book_id"],
        title=row["title"],
        author=row["author"],
        language=row["language"],
        format=row["format"],
        source_name=row["source_name"],
        chunk_count=row["chunk_count"],
        source=row["source"],
        source_uri=row["source_uri"],
        rights=row["rights"],
    )
    chunks: list[Chunk] = []
    for item in chunk_rows:
        try:
            heading_path = tuple(json.loads(item["heading_path"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise CorpusReadError("The current corpus contains invalid chunk metadata; rebuild it.") from exc
        chunks.append(
            Chunk(
                chunk_id=item["chunk_id"],
                book_id=item["book_id"],
                ordinal=item["ordinal"],
                heading_path=heading_path,
                source_locator=item["source_locator"],
                text=item["text"],
                text_hash=item["text_hash"],
                unit_count=item["unit_count"],
            )
        )
    return BookInspection(book=summary, chunks=tuple(chunks))


def load_corpus_snapshot(data_dir: str | os.PathLike[str]) -> CorpusSnapshot:
    """Load the immutable corpus rows needed to construct a derived search index."""
    status = get_corpus_status(data_dir)
    if not status.available:
        raise CorpusReadError(status.error or "No valid corpus has been built yet.")
    if status.version != CORPUS_VERSION or status.schema_version != SCHEMA_VERSION:
        raise CorpusReadError("The corpus version is stale; run the build command again.")
    _knowledge_dir, _books_dir, corpus_path = _paths(data_dir)
    connection = _read_connection(corpus_path)
    try:
        rows = connection.execute(
            """
            SELECT c.chunk_id, c.book_id, c.ordinal, c.heading_path, c.source_locator,
                   c.text, c.text_hash, c.unit_count, b.title, b.author, b.language,
                   b.source, b.source_uri, b.rights
            FROM chunks c JOIN books b ON b.book_id = c.book_id
            ORDER BY b.book_id, c.ordinal
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise CorpusReadError("The current corpus cannot be indexed; rebuild it.") from exc
    finally:
        connection.close()
    records: list[CorpusChunkRecord] = []
    fingerprint_items: list[str] = []
    for row in rows:
        try:
            heading_path = tuple(json.loads(row["heading_path"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise CorpusReadError("The current corpus contains invalid chunk metadata; rebuild it.") from exc
        chunk = Chunk(
            chunk_id=row["chunk_id"], book_id=row["book_id"], ordinal=row["ordinal"],
            heading_path=heading_path, source_locator=row["source_locator"], text=row["text"],
            text_hash=row["text_hash"], unit_count=row["unit_count"],
        )
        records.append(CorpusChunkRecord(
            chunk=chunk, title=row["title"], author=row["author"], language=row["language"],
            source=row["source"], source_uri=row["source_uri"], rights=row["rights"],
        ))
        fingerprint_items.append(f"{chunk.chunk_id}:{chunk.text_hash}")
    payload = "\0".join((CORPUS_VERSION, status.source_collection_hash or "", *fingerprint_items))
    return CorpusSnapshot(
        corpus_version=CORPUS_VERSION,
        schema_version=SCHEMA_VERSION,
        source_collection_hash=status.source_collection_hash or "",
        corpus_fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        chunks=tuple(records),
    )

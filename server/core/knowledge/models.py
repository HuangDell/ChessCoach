"""Framework-independent data contracts for the local book corpus."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CORPUS_VERSION = "book-corpus-v2"
SCHEMA_VERSION = 2


class KnowledgeError(Exception):
    """Base class for actionable corpus errors."""


class BookParseError(KnowledgeError):
    """A supported source could not be parsed into book content."""


class CorpusBuildError(KnowledgeError):
    """The corpus snapshot could not be built safely."""


class CorpusReadError(KnowledgeError):
    """The current corpus is missing required data or cannot be read."""


class BookNotFoundError(CorpusReadError):
    """The requested book is not present in the current corpus."""


@dataclass(frozen=True, slots=True)
class Paragraph:
    text: str
    heading_path: tuple[str, ...]
    source_locator: str


@dataclass(frozen=True, slots=True)
class Chapter:
    title: str
    source_locator: str
    paragraphs: tuple[Paragraph, ...]


@dataclass(frozen=True, slots=True)
class Book:
    book_id: str
    title: str
    author: str
    language: str
    format: str
    source_name: str
    source_size: int
    chapters: tuple[Chapter, ...]
    source: str = ""
    source_uri: str = ""
    rights: str = ""


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    book_id: str
    ordinal: int
    heading_path: tuple[str, ...]
    source_locator: str
    text: str
    text_hash: str
    unit_count: int


@dataclass(frozen=True, slots=True)
class BuildResult:
    corpus_path: Path
    version: str
    source_collection_hash: str
    book_count: int
    chunk_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorpusStatus:
    books_dir: Path
    corpus_path: Path
    available: bool
    version: str | None
    schema_version: int | None
    book_count: int
    chunk_count: int
    source_collection_hash: str | None
    built_at: str | None
    unsupported_files: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BookSummary:
    book_id: str
    title: str
    author: str
    language: str
    format: str
    source_name: str
    chunk_count: int
    source: str = ""
    source_uri: str = ""
    rights: str = ""


@dataclass(frozen=True, slots=True)
class BookInspection:
    book: BookSummary
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True, slots=True)
class CorpusChunkRecord:
    chunk: Chunk
    title: str
    author: str
    language: str
    source: str
    source_uri: str
    rights: str


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    corpus_version: str
    schema_version: int
    source_collection_hash: str
    corpus_fingerprint: str
    chunks: tuple[CorpusChunkRecord, ...]

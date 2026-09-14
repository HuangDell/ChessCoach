"""Command-line interface for the local book corpus."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from server import config
from server.core.knowledge import (
    KnowledgeError,
    build_corpus,
    get_corpus_status,
    get_index_status,
    inspect_book,
    list_books,
    build_index,
    LanceDBKnowledgeRetriever,
    QwenEmbedder,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server.knowledge")
    parser.add_argument("--data-dir", help="override CHESSCOACH_DATA_DIR")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "show source, corpus, and search-index status"),
        ("build", "atomically rebuild the corpus"),
        ("index", "rebuild the corpus and atomically activate a LanceDB index"),
        ("books", "list indexed books"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--data-dir", dest="command_data_dir", help=argparse.SUPPRESS)
    inspect = commands.add_parser("inspect", help="inspect ordered chunks for one book")
    inspect.add_argument("book_id")
    inspect.add_argument("--limit", type=int, default=10)
    inspect.add_argument("--data-dir", dest="command_data_dir", help=argparse.SUPPRESS)
    search = commands.add_parser("search", help="search the active local knowledge index")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5, choices=range(1, 6))
    search.add_argument("--skill-id", action="append", default=[])
    search.add_argument("--data-dir", dest="command_data_dir", help=argparse.SUPPRESS)
    return parser


def _data_dir(arguments: argparse.Namespace) -> Path:
    override = getattr(arguments, "command_data_dir", None) or arguments.data_dir
    return Path(override or config.DATA_DIR).expanduser()


def _print_status(data_dir: Path) -> int:
    status = get_corpus_status(data_dir)
    print(f"Books directory: {status.books_dir}")
    print(f"Corpus: {'ready' if status.available else 'not built'} ({status.corpus_path})")
    if status.available:
        print(f"Version: {status.version} (schema {status.schema_version})")
        print(f"Books: {status.book_count}")
        print(f"Chunks: {status.chunk_count}")
        print(f"Source collection hash: {status.source_collection_hash}")
        print(f"Built at: {status.built_at}")
    if status.unsupported_files:
        print("Unsupported files: " + ", ".join(status.unsupported_files))
    else:
        print("Unsupported files: none")
    if status.error:
        print(f"Error: {status.error}", file=sys.stderr)
    index = get_index_status(data_dir, enabled=config.KNOWLEDGE_ENABLED)
    print(f"Knowledge search: {'ready' if index.available else 'unavailable'}")
    if index.index_fingerprint:
        print(f"Index fingerprint: {index.index_fingerprint}")
        print(f"Embedding fingerprint: {index.embedding_fingerprint}")
        print(f"Vectors: {index.vector_count} ({index.dimension} dimensions)")
    if index.error:
        print(f"Index reason: {index.error}")
    return 1 if status.error else 0


def _print_build(data_dir: Path) -> int:
    result = build_corpus(data_dir)
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(f"Built {result.corpus_path}")
    print(f"Version: {result.version}")
    print(f"Books: {result.book_count}")
    print(f"Chunks: {result.chunk_count}")
    print(f"Source collection hash: {result.source_collection_hash}")
    return 0


def _embedder() -> QwenEmbedder:
    return QwenEmbedder(
        config.KNOWLEDGE_MODEL_PATH,
        device=config.KNOWLEDGE_DEVICE,
        batch_size=config.KNOWLEDGE_BATCH_SIZE,
    )


def _print_index(data_dir: Path) -> int:
    embedder = _embedder()
    try:
        result = build_index(data_dir, embedder, rebuild_corpus=True)
    finally:
        embedder.close()
    print(f"Activated {result.index_path}")
    print(f"Index fingerprint: {result.index_fingerprint}")
    print(f"Corpus fingerprint: {result.corpus_fingerprint}")
    print(f"Embedding fingerprint: {result.embedding_fingerprint}")
    print(f"Vectors: {result.vector_count} ({result.reused_vectors} reused, {result.encoded_vectors} encoded)")
    return 0


def _print_search(data_dir: Path, query: str, skill_ids: list[str], limit: int) -> int:
    retriever = LanceDBKnowledgeRetriever(data_dir, _embedder(), enabled=config.KNOWLEDGE_ENABLED)
    try:
        result = retriever.search(query, skill_ids=skill_ids[:5], limit=limit)
    finally:
        retriever.close()
    print(f"Status: {result.status}")
    print(f"Index fingerprint: {result.index_fingerprint}")
    for index, passage in enumerate(result.passages, start=1):
        citation = passage.citation
        byline = f" — {citation.author}" if citation.author else ""
        print(f"\n[{index}] {citation.title}{byline} | {citation.heading} | {citation.source_locator}")
        print(passage.text)
    return 0


def _print_books(data_dir: Path) -> int:
    books = list_books(data_dir)
    if not books:
        print("No indexed books. Run the build command first.")
        return 0
    for book in books:
        print(
            "\t".join(
                (
                    book.book_id,
                    book.title,
                    book.author or "-",
                    book.language or "-",
                    book.format,
                    str(book.chunk_count),
                )
            )
        )
    return 0


def _print_inspection(data_dir: Path, book_id: str, limit: int) -> int:
    inspection = inspect_book(data_dir, book_id, limit)
    print(f"Book: {inspection.book.title} ({inspection.book.book_id})")
    print(
        f"Author: {inspection.book.author or '-'} | Language: "
        f"{inspection.book.language or '-'} | Format: {inspection.book.format}"
    )
    for chunk in inspection.chunks:
        heading = " > ".join(chunk.heading_path)
        print(f"\n[{chunk.ordinal}] {heading} | {chunk.source_locator} | {chunk.unit_count} units")
        print(chunk.text)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    data_dir = _data_dir(arguments)
    try:
        if arguments.command == "status":
            return _print_status(data_dir)
        if arguments.command == "build":
            return _print_build(data_dir)
        if arguments.command == "index":
            return _print_index(data_dir)
        if arguments.command == "books":
            return _print_books(data_dir)
        if arguments.command == "inspect":
            return _print_inspection(data_dir, arguments.book_id, arguments.limit)
        if arguments.command == "search":
            return _print_search(data_dir, arguments.query, arguments.skill_id, arguments.limit)
    except (KnowledgeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

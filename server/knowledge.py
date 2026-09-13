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
    inspect_book,
    list_books,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server.knowledge")
    parser.add_argument("--data-dir", help="override CHESSCOACH_DATA_DIR")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "show source and corpus status"),
        ("build", "atomically rebuild the corpus"),
        ("books", "list indexed books"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--data-dir", dest="command_data_dir", help=argparse.SUPPRESS)
    inspect = commands.add_parser("inspect", help="inspect ordered chunks for one book")
    inspect.add_argument("book_id")
    inspect.add_argument("--limit", type=int, default=10)
    inspect.add_argument("--data-dir", dest="command_data_dir", help=argparse.SUPPRESS)
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
        return 1
    return 0


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
        if arguments.command == "books":
            return _print_books(data_dir)
        if arguments.command == "inspect":
            return _print_inspection(data_dir, arguments.book_id, arguments.limit)
    except (KnowledgeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

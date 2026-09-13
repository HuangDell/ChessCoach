"""Stable, paragraph-aware chunking for the local book corpus."""
from __future__ import annotations

import hashlib

from .models import Book, Chunk, Paragraph


TARGET_UNITS = 320
MAX_UNITS = 480
OVERLAP_UNITS = 40
_SENTENCE_ENDINGS = frozenset("。！？.!?")
_SENTENCE_CLOSERS = frozenset('"\'”’)]}》」』')
_CHESS_TOKEN_CHARS = frozenset("+#=xX:/.-_–")


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _unit_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if _is_cjk(character):
            spans.append((index, index + 1))
            index += 1
            continue
        if character.isalpha() or character.isnumeric():
            end = index + 1
            while end < len(text):
                candidate = text[end]
                if _is_cjk(candidate):
                    break
                if candidate.isalpha() or candidate.isnumeric() or candidate in _CHESS_TOKEN_CHARS:
                    end += 1
                    continue
                break
            spans.append((index, end))
            index = end
            continue
        spans.append((index, index + 1))
        index += 1
    return spans


def count_units(text: str) -> int:
    return len(_unit_spans(text))


def _hard_split(text: str, limit: int) -> list[str]:
    remaining = text.strip()
    pieces: list[str] = []
    while count_units(remaining) > limit:
        spans = _unit_spans(remaining)
        cut = spans[limit - 1][1]
        while cut < len(remaining) and remaining[cut].isspace():
            cut += 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _sentences(text: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        move_number_start = index - 1
        while move_number_start >= 0 and text[move_number_start] == ".":
            move_number_start -= 1
        is_move_number = (
            character == "."
            and move_number_start >= 0
            and text[move_number_start].isdigit()
        )
        if character in _SENTENCE_ENDINGS and not is_move_number:
            end = index + 1
            while end < len(text) and text[end] in _SENTENCE_CLOSERS:
                end += 1
            if end == len(text) or text[end].isspace():
                while end < len(text) and text[end].isspace():
                    end += 1
                value = text[start:end]
                if value:
                    pieces.append(value)
                start = end
        index += 1
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces or [text]


def _split_long_paragraph(paragraph: Paragraph) -> list[Paragraph]:
    if count_units(paragraph.text) <= MAX_UNITS:
        return [paragraph]
    sentence_groups: list[str] = []
    current: list[str] = []
    current_units = 0
    for sentence in _sentences(paragraph.text):
        sentence_units = count_units(sentence)
        if sentence_units > MAX_UNITS:
            if current:
                sentence_groups.append("".join(current).strip())
                current = []
                current_units = 0
            sentence_groups.extend(_hard_split(sentence, MAX_UNITS))
            continue
        if current and current_units + sentence_units > MAX_UNITS:
            sentence_groups.append("".join(current).strip())
            current = []
            current_units = 0
        current.append(sentence)
        current_units += sentence_units
    if current:
        sentence_groups.append("".join(current).strip())
    return [
        Paragraph(text=value, heading_path=paragraph.heading_path, source_locator=paragraph.source_locator)
        for value in sentence_groups
    ]


def _tail_overlap(text: str) -> str:
    spans = _unit_spans(text)
    if len(spans) <= OVERLAP_UNITS:
        return text.strip()
    return text[spans[-OVERLAP_UNITS][0] :].strip()


def _chunk_id(book_id: str, heading_path: tuple[str, ...], ordinal: int, text: str) -> str:
    identity = "\0".join((book_id, "\x1f".join(heading_path), str(ordinal), text))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def chunk_book(book: Book) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    ordinal = 0

    def emit(parts: list[Paragraph]) -> Paragraph:
        nonlocal ordinal
        text = "\n\n".join(part.text for part in parts).strip()
        first_locator = parts[0].source_locator
        last_locator = parts[-1].source_locator
        locator = first_locator if first_locator == last_locator else f"{first_locator}..{last_locator}"
        heading_path = parts[-1].heading_path
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunks.append(
            Chunk(
                chunk_id=_chunk_id(book.book_id, heading_path, ordinal, text),
                book_id=book.book_id,
                ordinal=ordinal,
                heading_path=heading_path,
                source_locator=locator,
                text=text,
                text_hash=text_hash,
                unit_count=count_units(text),
            )
        )
        ordinal += 1
        return Paragraph(
            text=_tail_overlap(text),
            heading_path=heading_path,
            source_locator=last_locator,
        )

    for chapter in book.chapters:
        groups: list[tuple[tuple[str, ...], list[Paragraph]]] = []
        for paragraph in chapter.paragraphs:
            expanded = _split_long_paragraph(paragraph)
            if not groups or groups[-1][0] != paragraph.heading_path:
                groups.append((paragraph.heading_path, []))
            groups[-1][1].extend(expanded)

        for heading_path, paragraphs in groups:
            current: list[Paragraph] = []
            current_units = 0
            for paragraph in paragraphs:
                paragraph_units = count_units(paragraph.text)
                if current and current_units >= TARGET_UNITS:
                    overlap = emit(current)
                    current = []
                    current_units = 0
                    if overlap.text and count_units(overlap.text) + paragraph_units <= MAX_UNITS:
                        current.append(overlap)
                        current_units = count_units(overlap.text)
                if current and current_units + paragraph_units > MAX_UNITS:
                    overlap = emit(current)
                    current = []
                    current_units = 0
                    if overlap.text and count_units(overlap.text) + paragraph_units <= MAX_UNITS:
                        current.append(overlap)
                        current_units = count_units(overlap.text)
                current.append(paragraph)
                current_units += paragraph_units
            if current:
                emit(current)
    return tuple(chunks)

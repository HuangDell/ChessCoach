"""Dependency-free EPUB, Markdown, and plain-text book parsers."""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import io
from pathlib import Path, PurePosixPath
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

from .models import Book, BookParseError, Chapter, Paragraph


SUPPORTED_EXTENSIONS = frozenset({".epub", ".txt", ".md", ".markdown"})
_MAX_EPUB_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def normalize_text(value: str, *, preserve_lines: bool = False) -> str:
    """Normalize Unicode and whitespace without altering chess notation."""
    value = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    if not preserve_lines:
        return re.sub(r"\s+", " ", value).strip()
    lines = [re.sub(r"[^\S\n]+$", "", line) for line in value.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _decode_text(source_name: str, content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BookParseError(
            f"Cannot parse '{source_name}': text files must use UTF-8 or UTF-8 BOM."
        ) from exc


def _paragraph(
    text: str,
    heading_path: tuple[str, ...],
    locator: str,
    *,
    preserve_lines: bool = False,
) -> Paragraph | None:
    normalized = normalize_text(text, preserve_lines=preserve_lines)
    if not normalized:
        return None
    return Paragraph(normalized, heading_path, locator)


def _update_heading(headings: list[str], level: int, title: str) -> None:
    del headings[level - 1 :]
    while len(headings) < level - 1:
        headings.append("")
    headings.append(title)


def _visible_heading_path(headings: list[str], fallback: str) -> tuple[str, ...]:
    path = tuple(item for item in headings if item)
    return path or (fallback,)


def _parse_txt(source_name: str, content: bytes, book_id: str) -> Book:
    title = normalize_text(Path(source_name).stem)
    text = _decode_text(source_name, content)
    paragraphs: list[Paragraph] = []
    for index, raw in enumerate(re.split(r"\n\s*\n+", text), start=1):
        item = _paragraph(raw, (title,), f"{source_name}:paragraph-{index}")
        if item is not None:
            paragraphs.append(item)
    if not paragraphs:
        raise BookParseError(f"Cannot parse '{source_name}': no extractable body text.")
    chapter = Chapter(title=title, source_locator=source_name, paragraphs=tuple(paragraphs))
    return Book(
        book_id=book_id,
        title=title,
        author="",
        language="",
        format="txt",
        source_name=source_name,
        source_size=len(content),
        chapters=(chapter,),
    )


def _parse_markdown(source_name: str, content: bytes, book_id: str) -> Book:
    text = _decode_text(source_name, content)
    fallback_title = normalize_text(Path(source_name).stem)
    headings: list[str] = []
    chapters: list[tuple[str, list[Paragraph]]] = [(fallback_title, [])]
    first_h1: str | None = None
    paragraph_lines: list[str] = []
    paragraph_start = 0
    fence_char = ""
    fence_length = 0
    code_lines: list[str] = []
    code_start = 0

    def current_paragraphs() -> list[Paragraph]:
        return chapters[-1][1]

    def add_paragraph(lines: list[str], start: int, *, code: bool = False) -> None:
        if not lines:
            return
        item = _paragraph(
            "\n".join(lines) if code else " ".join(lines),
            _visible_heading_path(headings, chapters[-1][0]),
            f"{source_name}:line-{start}",
            preserve_lines=code,
        )
        if item is not None:
            current_paragraphs().append(item)

    def flush_paragraph() -> None:
        nonlocal paragraph_lines, paragraph_start
        add_paragraph(paragraph_lines, paragraph_start)
        paragraph_lines = []
        paragraph_start = 0

    def apply_heading(level: int, raw_title: str) -> None:
        nonlocal first_h1
        title = normalize_text(raw_title)
        if not title:
            return
        _update_heading(headings, level, title)
        if level == 1:
            if first_h1 is None:
                first_h1 = title
            if current_paragraphs():
                chapters.append((title, []))
            else:
                chapters[-1] = (title, chapters[-1][1])

    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        if fence_char:
            closing = re.match(r"^\s*([`~]+)\s*$", line)
            if closing and closing.group(1)[0] == fence_char and len(closing.group(1)) >= fence_length:
                add_paragraph(code_lines, code_start, code=True)
                fence_char = ""
                fence_length = 0
                code_lines = []
                code_start = 0
            else:
                code_lines.append(line)
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            code_start = line_number + 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            heading_text = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(2))
            apply_heading(len(heading.group(1)), heading_text)
            continue

        setext = _SETEXT_RE.match(line)
        if setext and paragraph_lines:
            heading_line = paragraph_lines.pop()
            if paragraph_lines:
                add_paragraph(paragraph_lines, paragraph_start)
            paragraph_lines = []
            paragraph_start = 0
            apply_heading(1 if setext.group(1)[0] == "=" else 2, heading_line)
            continue

        if not line.strip():
            flush_paragraph()
            continue
        if not paragraph_lines:
            paragraph_start = line_number
        paragraph_lines.append(line.strip())

    if fence_char:
        add_paragraph(code_lines, code_start, code=True)
    flush_paragraph()

    parsed_chapters = tuple(
        Chapter(title=title, source_locator=source_name, paragraphs=tuple(items))
        for title, items in chapters
        if items
    )
    if not parsed_chapters:
        raise BookParseError(f"Cannot parse '{source_name}': no extractable body text.")
    return Book(
        book_id=book_id,
        title=first_h1 or fallback_title,
        author="",
        language="",
        format="markdown",
        source_name=source_name,
        source_size=len(content),
        chapters=parsed_chapters,
    )


@dataclass(slots=True)
class _HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_HtmlNode | str] = field(default_factory=list)


class _HtmlTreeBuilder(HTMLParser):
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(tag.casefold(), {key.casefold(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if node.tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HtmlNode(tag.casefold(), {key.casefold(): value or "" for key, value in attrs})
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


_HTML_IGNORED = frozenset({"script", "style", "nav", "noscript", "svg"})
_HTML_BLOCKS = frozenset({"p", "li", "blockquote", "pre", "code", "caption", "figcaption"})


def _node_text(node: _HtmlNode, *, preserve_lines: bool = False) -> str:
    parts: list[str] = []

    def visit(item: _HtmlNode | str) -> None:
        if isinstance(item, str):
            parts.append(item)
            return
        if item.tag in _HTML_IGNORED:
            return
        if item.tag == "br":
            parts.append("\n" if preserve_lines else " ")
            return
        for child in item.children:
            child_is_block = isinstance(child, _HtmlNode) and child.tag in _HTML_BLOCKS
            if child_is_block and parts and parts[-1] and not parts[-1][-1].isspace():
                parts.append("\n" if preserve_lines else " ")
            visit(child)
            if child_is_block:
                parts.append("\n" if preserve_lines else " ")

    visit(node)
    return normalize_text("".join(parts), preserve_lines=preserve_lines)


def _decode_epub_document(raw: bytes, href: str, source_name: str) -> str:
    encoding = "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        declaration = re.match(
            br"\s*<\?xml[^>]*encoding\s*=\s*['\"]([^'\"]+)['\"]",
            raw[:256],
            flags=re.IGNORECASE,
        )
        if declaration:
            try:
                encoding = declaration.group(1).decode("ascii")
            except UnicodeDecodeError as exc:
                raise BookParseError(
                    f"Cannot parse '{source_name}': EPUB document '{href}' has an invalid encoding."
                ) from exc
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB document '{href}' cannot be decoded as {encoding}."
        ) from exc


def _parse_html_document(raw: bytes, href: str, source_name: str) -> Chapter | None:
    text = _decode_epub_document(raw, href, source_name)
    parser = _HtmlTreeBuilder()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTMLParser exposes several malformed-input errors
        raise BookParseError(
            f"Cannot parse '{source_name}': invalid EPUB document '{href}'."
        ) from exc

    title_node: _HtmlNode | None = None
    body_node: _HtmlNode | None = None

    def find(node: _HtmlNode) -> None:
        nonlocal title_node, body_node
        if node.tag == "title" and title_node is None:
            title_node = node
        if node.tag == "body" and body_node is None:
            body_node = node
        for child in node.children:
            if isinstance(child, _HtmlNode):
                find(child)

    find(parser.root)
    document_title = _node_text(title_node) if title_node is not None else ""
    fallback_title = document_title or normalize_text(Path(href).stem)
    headings: list[str] = []
    first_heading = ""
    extracted: list[tuple[str, tuple[str, ...]]] = []

    def walk(node: _HtmlNode) -> None:
        nonlocal first_heading
        if node.tag in _HTML_IGNORED:
            return
        if len(node.tag) == 2 and node.tag[0] == "h" and node.tag[1] in "123456":
            heading = _node_text(node)
            if heading:
                _update_heading(headings, int(node.tag[1]), heading)
                if not first_heading:
                    first_heading = heading
            return
        if node.tag in _HTML_BLOCKS:
            value = _node_text(node, preserve_lines=node.tag in {"pre", "code"})
            if value:
                extracted.append(
                    (
                        value,
                        _visible_heading_path(headings, fallback_title),
                    )
                )
            return
        for child in node.children:
            if isinstance(child, _HtmlNode):
                walk(child)

    walk(body_node or parser.root)
    if not extracted:
        return None
    paragraphs = tuple(
        Paragraph(text=value, heading_path=heading_path, source_locator=f"{href}#block-{index}")
        for index, (value, heading_path) in enumerate(extracted, start=1)
    )
    return Chapter(
        title=first_heading or fallback_title,
        source_locator=href,
        paragraphs=paragraphs,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _safe_archive_name(name: str, source_name: str) -> str:
    decoded = unquote(name).replace("\\", "/")
    path = PurePosixPath(decoded)
    has_windows_drive = bool(path.parts and path.parts[0].endswith(":"))
    if (
        not decoded
        or path.is_absolute()
        or has_windows_drive
        or ".." in path.parts
        or "\x00" in decoded
    ):
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB contains an unsafe archive path '{name}'."
        )
    normalized = posixpath.normpath(decoded)
    if normalized in {"", "."} or normalized.startswith("../"):
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB contains an unsafe archive path '{name}'."
        )
    return normalized


def _resolve_archive_href(base_name: str, href: str, source_name: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB contains an unsafe resource path '{href}'."
        )
    combined = posixpath.join(posixpath.dirname(base_name), unquote(parsed.path))
    return _safe_archive_name(combined, source_name)


def _read_archive_file(archive: zipfile.ZipFile, name: str, source_name: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError as exc:
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB is missing required file '{name}'."
        ) from exc
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB entry '{name}' is corrupt or unreadable."
        ) from exc


def _read_xml(archive: zipfile.ZipFile, name: str, source_name: str) -> ET.Element:
    raw = _read_archive_file(archive, name, source_name)
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise BookParseError(
            f"Cannot parse '{source_name}': EPUB XML '{name}' is malformed."
        ) from exc


def _metadata_value(metadata: ET.Element | None, name: str) -> str:
    if metadata is None:
        return ""
    values = [
        normalize_text("".join(element.itertext()))
        for element in metadata.iter()
        if _local_name(element.tag) == name
    ]
    return "; ".join(value for value in values if value)


def _source_uri(metadata: ET.Element | None) -> str:
    """Prefer an absolute dc:source/identifier value as the optional citation URL."""
    candidates = [
        *_metadata_value(metadata, "source").split("; "),
        *_metadata_value(metadata, "identifier").split("; "),
    ]
    for value in candidates:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
    return ""


def _parse_epub(source_name: str, content: bytes, book_id: str) -> Book:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as exc:
        raise BookParseError(f"Cannot parse '{source_name}': EPUB ZIP is corrupt.") from exc

    with archive:
        infos = archive.infolist()
        if any(info.flag_bits & 0x1 for info in infos):
            raise BookParseError(f"Cannot parse '{source_name}': encrypted EPUB files are not supported.")
        if sum(info.file_size for info in infos) > _MAX_EPUB_UNCOMPRESSED_BYTES:
            raise BookParseError(
                f"Cannot parse '{source_name}': EPUB expands beyond the 256 MiB safety limit."
            )
        archive_names: dict[str, str] = {}
        for info in infos:
            safe_name = _safe_archive_name(info.filename, source_name)
            if safe_name in archive_names:
                raise BookParseError(
                    f"Cannot parse '{source_name}': EPUB contains duplicate path '{safe_name}'."
                )
            archive_names[safe_name] = info.filename

        container_name = "META-INF/container.xml"
        container = _read_xml(archive, archive_names.get(container_name, container_name), source_name)
        rootfile_path = ""
        for element in container.iter():
            if _local_name(element.tag) == "rootfile" and element.attrib.get("full-path"):
                rootfile_path = _safe_archive_name(element.attrib["full-path"], source_name)
                break
        if not rootfile_path:
            raise BookParseError(f"Cannot parse '{source_name}': EPUB container has no OPF rootfile.")
        opf_archive_name = archive_names.get(rootfile_path)
        if opf_archive_name is None:
            raise BookParseError(
                f"Cannot parse '{source_name}': EPUB is missing OPF file '{rootfile_path}'."
            )
        package = _read_xml(archive, opf_archive_name, source_name)
        metadata = next((item for item in package if _local_name(item.tag) == "metadata"), None)
        manifest_node = next((item for item in package if _local_name(item.tag) == "manifest"), None)
        spine_node = next((item for item in package if _local_name(item.tag) == "spine"), None)
        if manifest_node is None or spine_node is None:
            raise BookParseError(f"Cannot parse '{source_name}': EPUB OPF lacks manifest or spine.")

        manifest: dict[str, tuple[str, str, frozenset[str]]] = {}
        for item in manifest_node:
            if _local_name(item.tag) != "item":
                continue
            item_id = item.attrib.get("id", "")
            href = item.attrib.get("href", "")
            if not item_id or not href:
                continue
            resolved = _resolve_archive_href(rootfile_path, href, source_name)
            manifest[item_id] = (
                resolved,
                item.attrib.get("media-type", "").casefold(),
                frozenset(item.attrib.get("properties", "").casefold().split()),
            )

        chapters: list[Chapter] = []
        for itemref in spine_node:
            if (
                _local_name(itemref.tag) != "itemref"
                or itemref.attrib.get("linear", "yes").casefold() == "no"
            ):
                continue
            item_id = itemref.attrib.get("idref", "")
            manifest_item = manifest.get(item_id)
            if manifest_item is None:
                raise BookParseError(
                    f"Cannot parse '{source_name}': EPUB spine references missing item '{item_id}'."
                )
            href, media_type, properties = manifest_item
            if "nav" in properties or media_type not in {"application/xhtml+xml", "text/html"}:
                continue
            archive_name = archive_names.get(href)
            if archive_name is None:
                raise BookParseError(
                    f"Cannot parse '{source_name}': EPUB is missing spine document '{href}'."
                )
            chapter = _parse_html_document(
                _read_archive_file(archive, archive_name, source_name), href, source_name
            )
            if chapter is not None:
                chapters.append(chapter)

        if not chapters:
            raise BookParseError(f"Cannot parse '{source_name}': EPUB has no extractable body text.")
        title = _metadata_value(metadata, "title") or normalize_text(Path(source_name).stem)
        return Book(
            book_id=book_id,
            title=title,
            author=_metadata_value(metadata, "creator"),
            language=_metadata_value(metadata, "language"),
            format="epub",
            source_name=source_name,
            source_size=len(content),
            chapters=tuple(chapters),
            source=_metadata_value(metadata, "source"),
            source_uri=_source_uri(metadata),
            rights=_metadata_value(metadata, "rights"),
        )


def parse_book(source_name: str, content: bytes, book_id: str) -> Book:
    """Parse one supported source from an immutable byte snapshot."""
    extension = Path(source_name).suffix.casefold()
    if extension == ".epub":
        return _parse_epub(source_name, content, book_id)
    if extension == ".txt":
        return _parse_txt(source_name, content, book_id)
    if extension in {".md", ".markdown"}:
        return _parse_markdown(source_name, content, book_id)
    raise BookParseError(f"Unsupported book format for '{source_name}'.")

"""Versioned LanceDB index construction and bounded hybrid retrieval."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
from contextlib import contextmanager
from typing import Protocol, Sequence
from uuid import uuid4

from server.core.learning.taxonomy import get_skill_definition, resolve_skill_id

from .corpus import build_corpus, load_corpus_snapshot
from .models import CorpusReadError, KnowledgeError


INDEX_VERSION = "knowledge-index-v1"
PROMPT_VERSION = "chess-instruction-query-v1"
QUERY_INSTRUCTION = (
    "Retrieve chess instructional passages that teach concepts directly relevant to the query. "
    "Preserve concrete chess notation, tactical motifs, strategic plans, and endgame principles."
)
TABLE_NAME = "chunks"
ACTIVE_MANIFEST = "active.json"
RRF_K = 60
RETRIEVAL_CANDIDATES = 20


class KnowledgeUnavailableError(KnowledgeError):
    """The optional search index cannot be used without risking stale results."""


class KnowledgeIndexBuildError(KnowledgeError):
    """A derived search snapshot could not be completed safely."""


class Embedder(Protocol):
    @property
    def dimension(self) -> int: ...

    @property
    def fingerprint(self) -> str: ...

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KnowledgeCitation:
    citation_id: str
    book_id: str
    title: str
    author: str
    heading: str
    source_locator: str
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgePassage:
    passage_id: str
    text: str
    text_hash: str
    citation: KnowledgeCitation


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    status: str
    passages: tuple[KnowledgePassage, ...]
    index_fingerprint: str


class KnowledgeRetriever(Protocol):
    def current_index_fingerprint(self) -> str: ...

    def search(
        self, query: str, *, skill_ids: Sequence[str] = (), limit: int = 5
    ) -> KnowledgeSearchResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class IndexBuildResult:
    index_path: Path
    index_fingerprint: str
    corpus_fingerprint: str
    embedding_fingerprint: str
    vector_count: int
    reused_vectors: int
    encoded_vectors: int


@dataclass(frozen=True, slots=True)
class IndexStatus:
    available: bool
    stale: bool
    enabled: bool
    index_path: Path | None
    index_fingerprint: str | None
    corpus_fingerprint: str | None
    embedding_fingerprint: str | None
    vector_count: int
    dimension: int | None
    error: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _knowledge_dir(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir).expanduser() / "knowledge"


def _manifest_path(data_dir: str | os.PathLike[str]) -> Path:
    return _knowledge_dir(data_dir) / ACTIVE_MANIFEST


def _load_manifest(data_dir: str | os.PathLike[str]) -> dict:
    try:
        value = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeUnavailableError("No active knowledge index. Run the index command.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeUnavailableError("The active knowledge manifest is unreadable; rebuild it.") from exc
    if not isinstance(value, dict):
        raise KnowledgeUnavailableError("The active knowledge manifest is invalid; rebuild it.")
    return value


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _require_lancedb():
    if importlib.util.find_spec("lancedb") is None:
        raise KnowledgeUnavailableError(
            "LanceDB is not installed. Install the project with the rag extra."
        )
    import lancedb

    return lancedb


def _finite_normalized(vector: Sequence[float], dimension: int) -> list[float]:
    values = [float(item) for item in vector]
    if len(values) != dimension or not all(math.isfinite(item) for item in values):
        raise KnowledgeIndexBuildError(
            f"Embedder returned an invalid vector; expected {dimension} finite values."
        )
    norm = math.sqrt(sum(item * item for item in values))
    if norm <= 0:
        raise KnowledgeIndexBuildError("Embedder returned a zero vector.")
    return [item / norm for item in values]


def _model_file_fingerprint(model_path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(str(model_path.resolve()).encode("utf-8"))
    found = False
    for name in ("config.json", "modules.json", "tokenizer_config.json"):
        path = model_path / name
        if not path.is_file():
            continue
        found = True
        digest.update(name.encode("ascii"))
        digest.update(path.read_bytes())
    if not found:
        raise KnowledgeUnavailableError(
            f"Embedding model metadata is missing under '{model_path}'."
        )
    return "qwen3-embedding-8b:" + digest.hexdigest()


class QwenEmbedder:
    """Lazy, serialized local SentenceTransformer boundary for Qwen3-Embedding-8B."""

    dimension = 4096

    def __init__(self, model_path: str | os.PathLike[str], *, device: str = "auto", batch_size: int = 4):
        self.model_path = Path(model_path).expanduser()
        self.device = device.strip().lower() or "auto"
        self.batch_size = max(1, int(batch_size))
        self._fingerprint = _model_file_fingerprint(self.model_path)
        self._model = None
        self._resolved_device: str | None = None
        self._lock = threading.RLock()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise KnowledgeUnavailableError(
                "The Qwen embedding runtime is not installed. Install the rag extra."
            ) from exc
        if self.device == "auto":
            resolved = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            resolved = self.device
            if resolved.startswith("cuda") and not torch.cuda.is_available():
                raise KnowledgeUnavailableError(f"Configured device '{resolved}' is unavailable.")
        try:
            self._model = SentenceTransformer(
                str(self.model_path), device=resolved, trust_remote_code=True, local_files_only=True
            )
        except Exception as exc:
            raise KnowledgeUnavailableError(
                f"Could not load the local embedding model on '{resolved}': {type(exc).__name__}."
            ) from exc
        self._resolved_device = resolved
        return self._model

    def _encode(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            model = self._load()
            try:
                encoded = model.encode(
                    list(texts),
                    batch_size=self.batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    prompt=QUERY_INSTRUCTION if query else None,
                    show_progress_bar=False,
                )
            except Exception as exc:
                raise KnowledgeUnavailableError(
                    f"Embedding inference failed: {type(exc).__name__}."
                ) from exc
        return [_finite_normalized(row, self.dimension) for row in encoded]

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, query=False)

    def encode_query(self, text: str) -> list[float]:
        return self._encode([text], query=True)[0]

    def close(self) -> None:
        with self._lock:
            self._model = None
            try:
                import torch
                if self._resolved_device and self._resolved_device.startswith("cuda"):
                    torch.cuda.empty_cache()
            except ImportError:
                pass


def _document_text(record) -> str:
    headings = " > ".join(record.chunk.heading_path)
    return "\n".join(item for item in (record.title, headings, record.chunk.text) if item)


def _index_fingerprint(corpus_fingerprint: str, embedding_fingerprint: str, dimension: int) -> str:
    value = "\0".join((
        INDEX_VERSION, corpus_fingerprint, embedding_fingerprint, str(dimension),
        "float32", "normalized", "cosine", PROMPT_VERSION,
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_old_vectors(data_dir: str | os.PathLike[str], embedder: Embedder) -> dict[tuple[str, str], list[float]]:
    try:
        manifest = _load_manifest(data_dir)
    except KnowledgeUnavailableError:
        return {}
    if (
        manifest.get("embedding_fingerprint") != embedder.fingerprint
        or manifest.get("dimension") != embedder.dimension
    ):
        return {}
    path = _knowledge_dir(data_dir) / str(manifest.get("generation") or "") / "lancedb"
    try:
        table = _require_lancedb().connect(path).open_table(TABLE_NAME)
        rows = table.to_arrow().select(["chunk_id", "text_hash", "vector"]).to_pylist()
    except Exception:
        return {}
    return {
        (str(row["chunk_id"]), str(row["text_hash"])): list(row["vector"])
        for row in rows
    }


def build_index(
    data_dir: str | os.PathLike[str], embedder: Embedder, *, rebuild_corpus: bool = True
) -> IndexBuildResult:
    """Build a complete derived snapshot, reusing unchanged vectors before atomic activation."""
    if rebuild_corpus:
        build_corpus(data_dir)
    snapshot = load_corpus_snapshot(data_dir)
    lancedb = _require_lancedb()
    knowledge_dir = _knowledge_dir(data_dir)
    generation = f"generations/{uuid4().hex}"
    generation_dir = knowledge_dir / generation
    database_path = generation_dir / "lancedb"
    old_vectors = _read_old_vectors(data_dir, embedder)
    rows: list[dict] = []
    missing_records = []
    reused = 0
    for record in snapshot.chunks:
        key = (record.chunk.chunk_id, record.chunk.text_hash)
        vector = old_vectors.get(key)
        if vector is None:
            missing_records.append(record)
            continue
        rows.append(_row(record, _finite_normalized(vector, embedder.dimension)))
        reused += 1
    try:
        for offset in range(0, len(missing_records), 64):
            batch = missing_records[offset : offset + 64]
            vectors = embedder.encode_documents([_document_text(record) for record in batch])
            if len(vectors) != len(batch):
                raise KnowledgeIndexBuildError("Embedder returned the wrong number of vectors.")
            rows.extend(
                _row(record, _finite_normalized(vector, embedder.dimension))
                for record, vector in zip(batch, vectors, strict=True)
            )
        rows.sort(key=lambda row: (row["book_id"], row["ordinal"]))
        generation_dir.mkdir(parents=True, exist_ok=False)
        table = lancedb.connect(database_path).create_table(TABLE_NAME, data=rows, mode="create")
        table.create_fts_index(
            "search_text", replace=True, base_tokenizer="simple", lower_case=True,
            stem=False, remove_stop_words=False, ascii_folding=False,
        )
        vector_count = int(table.count_rows())
        if vector_count != len(snapshot.chunks):
            raise KnowledgeIndexBuildError("The completed index failed its row-count validation.")
        fingerprint = _index_fingerprint(
            snapshot.corpus_fingerprint, embedder.fingerprint, embedder.dimension
        )
        manifest = {
            "index_version": INDEX_VERSION,
            "generation": generation,
            "built_at": _now_iso(),
            "index_fingerprint": fingerprint,
            "corpus_fingerprint": snapshot.corpus_fingerprint,
            "source_collection_hash": snapshot.source_collection_hash,
            "embedding_fingerprint": embedder.fingerprint,
            "dimension": embedder.dimension,
            "dtype": "float32",
            "normalized": True,
            "distance": "cosine",
            "prompt_version": PROMPT_VERSION,
            "vector_count": vector_count,
        }
        _atomic_write_json(_manifest_path(data_dir), manifest)
    except KnowledgeError:
        shutil.rmtree(generation_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(generation_dir, ignore_errors=True)
        raise KnowledgeIndexBuildError(f"Knowledge index build failed: {type(exc).__name__}: {exc}") from exc
    return IndexBuildResult(
        index_path=database_path, index_fingerprint=fingerprint,
        corpus_fingerprint=snapshot.corpus_fingerprint,
        embedding_fingerprint=embedder.fingerprint, vector_count=vector_count,
        reused_vectors=reused, encoded_vectors=len(missing_records),
    )


def _row(record, vector: list[float]) -> dict:
    chunk = record.chunk
    heading = " > ".join(chunk.heading_path)
    return {
        "chunk_id": chunk.chunk_id, "text_hash": chunk.text_hash, "book_id": chunk.book_id,
        "ordinal": chunk.ordinal, "title": record.title, "author": record.author,
        "language": record.language, "source": record.source, "source_uri": record.source_uri,
        "rights": record.rights, "heading_path": heading,
        "source_locator": chunk.source_locator, "text": chunk.text,
        "search_text": _document_text(record), "vector": vector,
    }


def get_index_status(
    data_dir: str | os.PathLike[str], *, enabled: bool = True, embedder: Embedder | None = None
) -> IndexStatus:
    if not enabled:
        return IndexStatus(False, False, False, None, None, None, None, 0, None, "Knowledge retrieval is disabled.")
    try:
        manifest = _load_manifest(data_dir)
        snapshot = load_corpus_snapshot(data_dir)
        expected = {
            "index_version": INDEX_VERSION, "corpus_fingerprint": snapshot.corpus_fingerprint,
            "dtype": "float32", "normalized": True, "distance": "cosine",
            "prompt_version": PROMPT_VERSION,
        }
        mismatch = next((key for key, value in expected.items() if manifest.get(key) != value), None)
        if embedder is not None and (
            manifest.get("embedding_fingerprint") != embedder.fingerprint
            or manifest.get("dimension") != embedder.dimension
        ):
            mismatch = "embedding_fingerprint"
        path = _knowledge_dir(data_dir) / str(manifest.get("generation") or "") / "lancedb"
        if mismatch:
            raise KnowledgeUnavailableError(f"Knowledge index is stale ({mismatch}); rebuild it.")
        if not path.is_dir():
            raise KnowledgeUnavailableError("The active knowledge generation is missing; rebuild it.")
        if importlib.util.find_spec("lancedb") is None:
            raise KnowledgeUnavailableError("LanceDB is not installed. Install the rag extra.")
        return IndexStatus(
            True, False, True, path, manifest.get("index_fingerprint"),
            manifest.get("corpus_fingerprint"), manifest.get("embedding_fingerprint"),
            int(manifest.get("vector_count") or 0), int(manifest.get("dimension") or 0), None,
        )
    except (KnowledgeUnavailableError, CorpusReadError, OSError, ValueError) as exc:
        stale = "stale" in str(exc).lower()
        return IndexStatus(False, stale, True, None, None, None, None, 0, None, str(exc))


class LanceDBKnowledgeRetriever:
    def __init__(self, data_dir: str | os.PathLike[str], embedder: Embedder, *, enabled: bool = True, trace_store=None):
        self.trace_store = trace_store
        self.data_dir = Path(data_dir).expanduser()
        self.embedder = embedder
        self.enabled = enabled
        self._lock = threading.RLock()

    def _open(self):
        status = get_index_status(self.data_dir, enabled=self.enabled, embedder=self.embedder)
        if not status.available or status.index_path is None or status.index_fingerprint is None:
            raise KnowledgeUnavailableError(status.error or "Knowledge index is unavailable.")
        try:
            table = _require_lancedb().connect(status.index_path).open_table(TABLE_NAME)
        except Exception as exc:
            raise KnowledgeUnavailableError(
                f"The active knowledge index cannot be opened: {type(exc).__name__}."
            ) from exc
        return table, status.index_fingerprint

    def current_index_fingerprint(self) -> str:
        status = get_index_status(self.data_dir, enabled=self.enabled, embedder=self.embedder)
        if not status.available or status.index_fingerprint is None:
            raise KnowledgeUnavailableError(status.error or "Knowledge index is unavailable.")
        return status.index_fingerprint

    def search(self, query: str, *, skill_ids: Sequence[str] = (), limit: int = 5) -> KnowledgeSearchResult:
        trace = self.trace_store.start() if self.trace_store is not None else None
        started = time.perf_counter()
        if trace is not None:
            trace.update(query=query, skill_ids=list(skill_ids), requested_limit=limit,
                         parameters={"candidates_per_route": RETRIEVAL_CANDIDATES, "rrf_k": RRF_K,
                                     "distance_type": "cosine", "query_instruction": QUERY_INSTRUCTION})
        try:
            result = self._search(query, skill_ids=skill_ids, limit=limit, trace=trace)
            if trace is not None:
                trace.update(status=result.status, final_passages=[asdict(p) for p in result.passages])
            return result
        except Exception as exc:
            if trace is not None:
                trace["error"] = {"type": type(exc).__name__,
                                  "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                                  "message": "Retrieval failed; inspect the recorded stage and local index/model configuration."}
            raise
        finally:
            if trace is not None:
                trace["timings_ms"]["total"] = (time.perf_counter() - started) * 1000
                self.trace_store.save(trace)

    def _search(self, query, *, skill_ids, limit, trace):
        if trace is not None:
            trace["stage"] = "validation"
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit), 5))
        if trace is not None:
            trace["effective_limit"] = limit
        additions: list[str] = []
        for raw in list(skill_ids)[:5]:
            canonical = resolve_skill_id(str(raw))
            definition = get_skill_definition(canonical) if canonical else None
            if definition is not None:
                additions.append(f"{definition.label}: {definition.description}")
        expanded = query + ("\nRelevant coaching skills: " + "; ".join(additions) if additions else "")
        if trace is not None:
            trace["expanded_query"] = expanded
        with _trace_stage(trace, "embedding"):
            vector = _finite_normalized(self.embedder.encode_query(expanded), self.embedder.dimension)
            if trace is not None:
                trace.update(embedding_fingerprint=self.embedder.fingerprint, dimension=self.embedder.dimension)
        lock_started = time.perf_counter()
        with self._lock:
            if trace is not None:
                trace["timings_ms"]["lock_wait"] = (time.perf_counter() - lock_started) * 1000
            with _trace_stage(trace, "open_index"):
                table, fingerprint = self._open()
                if trace is not None:
                    trace["index_fingerprint"] = fingerprint
            columns = [
                "chunk_id", "text_hash", "book_id", "ordinal", "title", "author",
                "heading_path", "source_locator", "source_uri", "text",
            ]
            try:
                with _trace_stage(trace, "dense"):
                    dense = (
                        table.search(vector, vector_column_name="vector", query_type="vector")
                        .distance_type("cosine").select(columns).limit(RETRIEVAL_CANDIDATES).to_list()
                    )
                    if trace is not None:
                        trace["dense"] = _trace_candidates(dense, "_distance", "cosine_distance")
                with _trace_stage(trace, "lexical"):
                    lexical = (
                        table.search(expanded, query_type="fts", fts_columns="search_text")
                        .select(columns).limit(RETRIEVAL_CANDIDATES).to_list()
                    )
                    if trace is not None:
                        trace["lexical"] = _trace_candidates(lexical, "_score", "bm25_score")
            except Exception as exc:
                raise KnowledgeUnavailableError(
                    f"Knowledge search failed: {type(exc).__name__}."
                ) from exc
        with _trace_stage(trace, "fusion"):
            ranks: dict[str, float] = {}
            records: dict[str, dict] = {}
            for result_set in (dense, lexical):
                for rank, row in enumerate(result_set, start=1):
                    chunk_id = str(row["chunk_id"])
                    ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                    records[chunk_id] = row
            ordered = sorted(records.values(), key=lambda row: (-ranks[str(row["chunk_id"])], str(row["chunk_id"])))
            passages: list[KnowledgePassage] = []
            seen_hashes: set[str] = set()
            for fusion_rank, row in enumerate(ordered, start=1):
                if trace is None and len(passages) >= limit:
                    break
                text_hash = str(row["text_hash"])
                reason = "duplicate_text" if text_hash in seen_hashes else "limit" if len(passages) >= limit else "selected"
                if trace is not None:
                    trace.setdefault("fusion", []).append({"chunk_id": str(row["chunk_id"]),
                        "rank": fusion_rank, "rrf_score": ranks[str(row["chunk_id"])], "decision": reason})
                if reason != "selected":
                    continue
                seen_hashes.add(text_hash)
                chunk_id = str(row["chunk_id"])
                citation_id = f"knowledge:{chunk_id}:{text_hash[:12]}"
                citation = KnowledgeCitation(
                    citation_id=citation_id, book_id=str(row["book_id"]), title=str(row["title"]),
                    author=str(row.get("author") or ""), heading=str(row.get("heading_path") or ""),
                    source_locator=str(row["source_locator"]), source_url=str(row.get("source_uri") or "") or None,
                )
                passages.append(KnowledgePassage(
                    passage_id=chunk_id, text=str(row["text"]), text_hash=text_hash, citation=citation,
                ))
        return KnowledgeSearchResult(
            status="found" if passages else "no_match", passages=tuple(passages),
            index_fingerprint=fingerprint,
        )

    def close(self) -> None:
        self.embedder.close()


def manifest_json(result: IndexBuildResult) -> dict:
    return {key: str(value) if isinstance(value, Path) else value for key, value in asdict(result).items()}


@contextmanager
def _trace_stage(trace, stage):
    started = time.perf_counter()
    if trace is not None:
        trace["stage"] = stage
    try:
        yield
    finally:
        if trace is not None:
            trace["timings_ms"][stage] = (time.perf_counter() - started) * 1000


def _trace_candidates(rows, score_key, score_kind):
    return [{**{key: value for key, value in row.items() if key != "vector"},
             "rank": rank, "score_kind": score_kind,
             "score": float(row[score_key]) if row.get(score_key) is not None else None}
            for rank, row in enumerate(rows, start=1)]

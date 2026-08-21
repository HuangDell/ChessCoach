"""Persistence for imported game artifacts under <DATA_DIR>/games/<game_id>."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from server import config
from server.core.game_identity import GAME_ID_LENGTH
from server.core.importers.pgn import ImportedGame

_GAME_ID_RE = re.compile(rf"^[0-9a-f]{{{GAME_ID_LENGTH}}}$")


class GameNotFoundError(FileNotFoundError):
    pass


def _game_dir(game_id: str) -> str:
    if not _GAME_ID_RE.fullmatch(game_id or ""):
        raise GameNotFoundError("Unknown game.")
    return os.path.join(config.DATA_DIR, "games", game_id)


def _atomic_write(path: str, content: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


def store_game(game: ImportedGame) -> bool:
    """Store/refresh one identity directory and return whether it already existed."""
    directory = _game_dir(game.game_id)
    source_path = os.path.join(directory, "source.pgn")
    already_imported = os.path.isfile(source_path)
    os.makedirs(directory, exist_ok=True)
    _atomic_write(source_path, game.pgn)
    _atomic_write(os.path.join(directory, "original.pgn"), game.original_pgn)
    _atomic_write(
        os.path.join(directory, "metadata.json"),
        json.dumps(game.metadata(), ensure_ascii=False, indent=2) + "\n",
    )
    game.already_imported = already_imported
    return already_imported


def load_game(game_id: str) -> dict:
    directory = _game_dir(game_id)
    try:
        with open(os.path.join(directory, "metadata.json"), "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        with open(os.path.join(directory, "source.pgn"), "r", encoding="utf-8") as handle:
            pgn = handle.read()
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise GameNotFoundError("Unknown or incomplete game artifact.") from exc
    if metadata.get("game_id") != game_id:
        raise GameNotFoundError("Game artifact identity mismatch.")
    return {**metadata, "pgn": pgn}


def store_analysis(game_id: str, review_side: str, analysis: dict) -> None:
    """Atomically store the versioned Engine artifact, preserving a cache per review side."""
    if review_side not in {"white", "black"}:
        raise ValueError("review_side must be white or black")
    if analysis.get("game_id") != game_id or analysis.get("review_side") != review_side:
        raise ValueError("Analysis artifact identity mismatch.")
    directory = _game_dir(game_id)
    side_directory = os.path.join(directory, "analysis")
    os.makedirs(side_directory, exist_ok=True)
    content = json.dumps(analysis, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(os.path.join(side_directory, f"{review_side}.json"), content)
    # Stable, discoverable artifact for the current review side. The side-specific copies prevent
    # reviewing the opponent later from destroying the first result.
    _atomic_write(os.path.join(directory, "analysis.json"), content)


def load_analysis(game_id: str, review_side: str | None = None) -> dict:
    directory = _game_dir(game_id)
    if review_side is not None and review_side not in {"white", "black"}:
        raise GameNotFoundError("Unknown review side.")
    path = (
        os.path.join(directory, "analysis", f"{review_side}.json")
        if review_side
        else os.path.join(directory, "analysis.json")
    )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            analysis = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise GameNotFoundError("Analysis is not available for this game and side.") from exc
    if analysis.get("game_id") != game_id:
        raise GameNotFoundError("Analysis artifact identity mismatch.")
    if review_side and analysis.get("review_side") != review_side:
        raise GameNotFoundError("Analysis artifact review-side mismatch.")
    return analysis


def analysis_sides(game_id: str) -> list[str]:
    directory = os.path.join(_game_dir(game_id), "analysis")
    return [
        side
        for side in ("white", "black")
        if os.path.isfile(os.path.join(directory, f"{side}.json"))
    ]


def store_explanations(game_id: str, review_side: str, artifact: dict) -> None:
    """Atomically store validated model explanations, preserving one artifact per review side."""
    if review_side not in {"white", "black"}:
        raise ValueError("review_side must be white or black")
    if artifact.get("game_id") != game_id or artifact.get("review_side") != review_side:
        raise ValueError("Explanation artifact identity mismatch.")
    directory = _game_dir(game_id)
    side_directory = os.path.join(directory, "explanations")
    os.makedirs(side_directory, exist_ok=True)
    content = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(os.path.join(side_directory, f"{review_side}.json"), content)
    _atomic_write(os.path.join(directory, "explanations.json"), content)


def load_explanations(game_id: str, review_side: str | None = None) -> dict:
    """Load a stable explanation artifact, optionally pinned to one reviewed side."""
    directory = _game_dir(game_id)
    if review_side is not None and review_side not in {"white", "black"}:
        raise GameNotFoundError("Unknown review side.")
    path = (
        os.path.join(directory, "explanations", f"{review_side}.json")
        if review_side
        else os.path.join(directory, "explanations.json")
    )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            artifact = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise GameNotFoundError("Explanations are not available for this game and side.") from exc
    if artifact.get("game_id") != game_id:
        raise GameNotFoundError("Explanation artifact identity mismatch.")
    if review_side and artifact.get("review_side") != review_side:
        raise GameNotFoundError("Explanation artifact review-side mismatch.")
    return artifact


def delete_game(game_id: str) -> bool:
    """Delete one validated per-game artifact directory. History indexes are handled separately."""
    directory = _game_dir(game_id)
    if not os.path.isdir(directory):
        return False
    shutil.rmtree(directory)
    return True


def clear_engine_caches() -> dict:
    """Clear recomputable Engine caches without touching games, explanations, history, or binary."""
    roots = [
        Path(config.DATA_DIR) / "engine-cache",
        Path(config.DATA_DIR) / "analysis-cache",
        Path(config.DATA_DIR) / "cache" / "engine",
    ]
    files = 0
    bytes_removed = 0
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                files += 1
                try:
                    bytes_removed += path.stat().st_size
                except OSError:
                    pass
        shutil.rmtree(root)
    return {"files_removed": files, "bytes_removed": bytes_removed}

"""User-editable settings for the standalone Web application.

`settings.json` holds local identity, UI preferences, profile windows, and the Stockfish path.
Unknown legacy keys remain readable but are ignored and are never returned or rewritten by new
patches.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from server import config

# The keys the Settings panel can edit, stored as the raw strings a user would type (parsed into
# config the same way the matching env vars are).
KEYS = (
    "username",
    "chesscom_username",
    "chesscom_sync",
    "chesscom_sync_max",
    "aliases",
    "lichess_token",
    "profile_recent",
    "profile_lifetime",
    "player_elo",
    "stockfish_path",
    "personalize_history",
    "puzzle_animations",
    "puzzle_auto_advance",
    "puzzle_mistake_interleave",
    "default_review_side",
    "board_orientation",
    "analysis_preset",
    "explanation_provider",
    "explanation_language",
    "show_threat_arrows",
)


def _path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir if data_dir is not None else config.DATA_DIR, "settings.json")


def load(data_dir: Optional[str] = None) -> dict:
    """Read settings.json (missing/garbled -> {}, so the app still runs on env defaults)."""
    try:
        with open(_path(data_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(settings: dict, data_dir: Optional[str] = None) -> None:
    path = _path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {**settings, "schema_version": 1}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def apply(settings: dict) -> None:
    """Override live `config` values from a settings dict (only the keys that are present)."""
    # Identity is composed from three fields together (Lichess handle, chess.com handle, other
    # accounts) so the canonical player_id + aliases stay coherent. Any field absent from the patch
    # keeps its current live value.
    if any(k in settings for k in ("username", "chesscom_username", "aliases")):
        config._compose_identity(
            settings.get("username", config.LICHESS_USERNAME),
            settings.get("chesscom_username", config.CHESSCOM_USERNAME),
            settings.get("aliases", config.USERNAME_ALIASES_RAW),
        )
    if "chesscom_sync" in settings:
        config.CHESSCOM_SYNC_ENABLED = bool(settings["chesscom_sync"])
    if "chesscom_sync_max" in settings:
        try:
            n = int(settings["chesscom_sync_max"])
            if n > 0:
                config.CHESSCOM_SYNC_MAX = n
        except (ValueError, TypeError):
            pass
    if "lichess_token" in settings:
        config.LICHESS_TOKEN = (settings["lichess_token"] or "").strip()
    if "profile_recent" in settings:
        try:
            config.PROFILE_RECENT_WINDOW = int(settings["profile_recent"])
        except (ValueError, TypeError):
            pass
    if "profile_lifetime" in settings:
        config.PROFILE_LIFETIME = config._parse_lifetime(str(settings["profile_lifetime"]))
    if "player_elo" in settings:
        config.PLAYER_ELO = config._parse_elo(str(settings["player_elo"]))
    if "stockfish_path" in settings:
        sp = config.clean_path(settings["stockfish_path"])
        if sp:
            config.STOCKFISH_PATH = shutil.which(sp) or sp
    if "personalize_history" in settings:
        config.PERSONALIZE_HISTORY = bool(settings["personalize_history"])
    if "puzzle_animations" in settings:
        config.PUZZLE_ANIMATIONS = bool(settings["puzzle_animations"])
    if "puzzle_auto_advance" in settings:
        config.PUZZLE_AUTO_ADVANCE = bool(settings["puzzle_auto_advance"])
    if "puzzle_mistake_interleave" in settings:
        config.PUZZLE_MISTAKE_INTERLEAVE = bool(settings["puzzle_mistake_interleave"])
    if "default_review_side" in settings:
        value = str(settings["default_review_side"] or "auto").strip().lower()
        if value in {"auto", "white", "black"}:
            config.DEFAULT_REVIEW_SIDE = value
    if "board_orientation" in settings:
        value = str(settings["board_orientation"] or "review").strip().lower()
        if value in {"review", "white", "black"}:
            config.BOARD_ORIENTATION = value
    if "analysis_preset" in settings:
        value = str(settings["analysis_preset"] or "balanced").strip().lower()
        if value in {"fast", "balanced", "deep"}:
            config.ANALYSIS_PRESET = value
    if "explanation_provider" in settings:
        value = str(settings["explanation_provider"] or "auto").strip().lower()
        if value in {"auto", "openai-compatible"}:
            config.EXPLANATION_PROVIDER = value
    if "explanation_language" in settings:
        value = str(settings["explanation_language"] or "zh-CN").strip()
        if value in {"zh-CN", "en"}:
            config.EXPLANATION_LANGUAGE = value
    if "show_threat_arrows" in settings:
        config.SHOW_THREAT_ARROWS = bool(settings["show_threat_arrows"])


def apply_saved(data_dir: Optional[str] = None) -> dict:
    """Load + apply settings.json at startup. Returns the loaded settings (possibly empty)."""
    settings = load(data_dir)
    apply(settings)
    return settings


def effective() -> dict:
    """The current effective values (as raw strings) for the Settings form."""
    return {
        "username": config.LICHESS_USERNAME or "",
        "chesscom_username": config.CHESSCOM_USERNAME or "",
        "chesscom_sync": config.CHESSCOM_SYNC_ENABLED,
        "chesscom_sync_max": str(config.CHESSCOM_SYNC_MAX),
        "aliases": config.USERNAME_ALIASES_RAW,
        "lichess_token": config.LICHESS_TOKEN or "",
        "profile_recent": str(config.PROFILE_RECENT_WINDOW),
        "profile_lifetime": "all" if config.PROFILE_LIFETIME is None else str(config.PROFILE_LIFETIME),
        "player_elo": "" if config.PLAYER_ELO is None else str(config.PLAYER_ELO),
        "stockfish_path": config.STOCKFISH_PATH or "",
        "personalize_history": config.PERSONALIZE_HISTORY,
        "puzzle_animations": config.PUZZLE_ANIMATIONS,
        "puzzle_auto_advance": config.PUZZLE_AUTO_ADVANCE,
        "puzzle_mistake_interleave": config.PUZZLE_MISTAKE_INTERLEAVE,
        "default_review_side": config.DEFAULT_REVIEW_SIDE,
        "board_orientation": config.BOARD_ORIENTATION,
        "analysis_preset": config.ANALYSIS_PRESET,
        "explanation_provider": config.EXPLANATION_PROVIDER,
        "explanation_language": config.EXPLANATION_LANGUAGE,
        "show_threat_arrows": config.SHOW_THREAT_ARROWS,
    }


def update(patch: dict, data_dir: Optional[str] = None) -> dict:
    """Merge a partial settings patch into the store, persist it, apply it live, return effective."""
    loaded = load(data_dir)
    settings = {key: loaded[key] for key in KEYS if key in loaded}
    for key in KEYS:
        if key in patch:
            settings[key] = patch[key]
    # Normalise the Stockfish path before persisting so a quoted "Copy as path" paste
    # (common on Windows) is stored clean, not just applied clean.
    if "stockfish_path" in settings and settings["stockfish_path"]:
        settings["stockfish_path"] = config.clean_path(settings["stockfish_path"])
    save(settings, data_dir)
    apply(settings)
    return effective()

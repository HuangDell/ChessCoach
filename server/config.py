"""Configuration shared by the Chess Review Coach Web and optional integrations."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

# Repo root (this file is <repo>/server/config.py), used for repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Public alias for the project root — the install dir the update-checker reasons about (presence of
# a `.git` here decides the update channel; see server.core.updates).
PROJECT_ROOT = _REPO_ROOT


def _read_app_version() -> str:
    """The single canonical app version: pyproject.toml's [project] version.

    pyproject.toml ships in EVERY distribution channel (git checkout, the source zip, and the
    rsynced `.app` bundle), so this one read works everywhere. Python is pinned >=3.11, so tomllib
    is always present; a regex fallback covers a malformed/partial read. Never raises — an unknown
    version degrades to "0.0.0" (the update-check then simply reports no update)."""
    path = os.path.join(_REPO_ROOT, "pyproject.toml")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return "0.0.0"
    try:
        import tomllib
        ver = tomllib.loads(raw.decode("utf-8")).get("project", {}).get("version", "")
        if ver:
            return str(ver).strip()
    except Exception:  # noqa: BLE001 - fall through to the regex
        pass
    import re
    m = re.search(rb'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', raw)
    return m.group(1).decode("utf-8").strip() if m else "0.0.0"


def _default_data_dir() -> str:
    """User-level folder for history/cache/settings — the SHARED store.

    Resolved per-OS to the conventional app-data location so every entry point on a machine lands
    in the same place automatically. Overridable with ``CHESSCOACH_DATA_DIR``; the upstream
    ``CHESS_DATA_DIR`` name remains supported for compatibility.
    """
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Chess Review Coach", "data")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, "Chess Review Coach", "data")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "chess-review-coach", "data")


def _resolve_data_dir() -> str:
    """Return the configured data root without placing personal data in the source tree."""
    return (
        os.environ.get("CHESSCOACH_DATA_DIR", "").strip()
        or os.environ.get("CHESS_DATA_DIR", "").strip()
        or _default_data_dir()
    )


def _managed_stockfish_path(data_dir: str | None = None) -> str:
    """Where the `.app` / launcher downloads Stockfish when there's no system engine AND no
    Homebrew (a clean Mac). It lives under DATA_DIR so it's shared by every entry point and
    auto-detected here — the launcher also exports STOCKFISH_PATH to it, belt-and-suspenders."""
    name = "stockfish.exe" if os.name == "nt" else "stockfish"
    return os.path.join(data_dir or _resolve_data_dir(), "engine", name)


# Common locations a Stockfish binary lands in across the package managers we point
# users at. Searched (in order) only when STOCKFISH_PATH isn't set and `stockfish`
# isn't on PATH, so a normal `brew`/`apt` install needs zero configuration.
_COMMON_STOCKFISH_PATHS = [
    "/opt/homebrew/bin/stockfish",  # macOS, Apple Silicon Homebrew
    "/usr/local/bin/stockfish",     # macOS Intel Homebrew / manual installs
    "/usr/bin/stockfish",           # Debian/Ubuntu apt
    "/usr/games/stockfish",         # some Linux distros put it here
]


def clean_path(value: str | None) -> str:
    """Normalise a user-entered filesystem path.

    Strips whitespace and any surrounding quotes — Windows Explorer's "Copy as path"
    wraps paths in double-quotes, and pasting that verbatim would make `shutil.which`
    look for a file whose name literally contains the quote characters (so a perfectly
    valid path reads as "not found"). Strips matching single/double quotes once.
    """
    s = (value or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def _resolve_stockfish() -> str:
    """Best-effort path to the Stockfish binary.

    Priority: an explicit STOCKFISH_PATH (honoured as set, resolved via PATH if it's a
    bare command) -> `stockfish` on PATH -> the common install locations above -> the
    launcher-managed download under DATA_DIR (so a clean-Mac `.app` install that fetched
    its own Stockfish is found with no config). Falls back to the bare name "stockfish" so
    the engine still raises a clear, actionable error (see stockfish_install_hint).
    """
    explicit = clean_path(os.environ.get("STOCKFISH_PATH"))
    if explicit:
        return shutil.which(explicit) or explicit
    found = shutil.which("stockfish")
    if found:
        return found
    for path in _COMMON_STOCKFISH_PATHS:
        if os.path.isfile(path):
            return path
    managed = _managed_stockfish_path()
    if os.path.isfile(managed):
        return managed
    return "stockfish"


def stockfish_install_hint(path: str | None = None) -> str:
    """One-line, copy-pasteable guidance shown when Stockfish can't be launched."""
    tried = path or STOCKFISH_PATH
    return (
        f"Stockfish engine not found (tried '{tried}'). Install it — macOS: "
        "`brew install stockfish`; Debian/Ubuntu: `sudo apt install stockfish` — or "
        "download it from https://stockfishchess.org/download/ and set STOCKFISH_PATH "
        "to the binary. See the README 'Installation' section."
    )


def is_apple_silicon() -> bool:
    """True on Apple Silicon *hardware*, even when this process runs translated under Rosetta 2.

    `platform.machine()` / `uname -m` report ``x86_64`` inside a Rosetta-translated process, so they
    can't be trusted to tell arm64 hardware from Intel here; ``sysctl -n hw.optional.arm64`` reports
    the hardware capability (``1`` on Apple Silicon) regardless of translation.
    """
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=3
        )
    except Exception:  # noqa: BLE001 - detection is best-effort; assume not-arm on any failure
        return False
    return out.stdout.strip() == "1"


# Mach-O CPU type for arm64 (base type 12 | the 0x01000000 64-bit ABI bit); x86/x86_64 has base 7.
_CPU_TYPE_ARM64 = 0x0100000C


def macho_arch(path: str) -> str:
    """The CPU architecture of a Mach-O binary: ``arm64`` | ``x86_64`` | ``universal`` | ``unknown``.

    Dependency-free: reads the Mach-O header (magic + cputype) directly, so no ``lipo``/``file``
    subprocess is needed. A fat/universal binary reports ``universal`` — it carries a native slice,
    so it is never the Rosetta-mismatch case.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(8)
    except OSError:
        return "unknown"
    if len(header) < 8:
        return "unknown"
    magic = header[:4]
    # Fat / universal (FAT_MAGIC / FAT_MAGIC_64 and their byte-swapped forms).
    if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\xbe\xba\xfe\xca", b"\xbf\xba\xfe\xca"):
        return "universal"
    # Thin Mach-O: the 4-byte cputype follows the magic, in the magic's endianness (64- or 32-bit).
    if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cputype = int.from_bytes(header[4:8], "little")
    elif magic in (b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):
        cputype = int.from_bytes(header[4:8], "big")
    else:
        return "unknown"
    if cputype == _CPU_TYPE_ARM64:
        return "arm64"
    if cputype & 0xFF == 0x07:  # x86 / x86_64 (base cputype 7, with or without the 64-bit ABI bit)
        return "x86_64"
    return "unknown"


def stockfish_arch_report(path: str | None = None) -> dict:
    """Whether the resolved Stockfish is running sub-optimally under Rosetta 2 on Apple Silicon.

    An Intel (x86_64) Stockfish still runs on Apple Silicon, but translated under Rosetta 2 — notably
    slower for a search-heavy engine (and the symptom of the old first-run install bug that fetched
    the wrong build). This flags that case so the UI can offer a one-click swap to the native arm64
    build. Best-effort and macOS-only; everywhere else ``suboptimal`` is False.
    """
    resolved = clean_path(path) or STOCKFISH_PATH
    resolved = shutil.which(resolved) or (resolved if os.path.isfile(resolved) else "")
    if not resolved or not is_apple_silicon():
        return {"suboptimal": False, "hardware": "", "binary": "", "path": resolved, "can_fix": False}
    binary = macho_arch(resolved)
    suboptimal = binary == "x86_64"
    return {
        "suboptimal": suboptimal,
        "hardware": "arm64",
        "binary": binary,
        "path": resolved,
        # A native arm64 static Stockfish build (stockfish-macos-m1-apple-silicon) exists to download.
        "can_fix": suboptimal,
    }


# Path to the Stockfish binary. Auto-detected (PATH + common locations) so a standard
# install needs no config; override with the STOCKFISH_PATH env var.
STOCKFISH_PATH: str = _resolve_stockfish()

# Depth used for on-demand single-position analysis (get_engine_line, REPL checks).
# Fixed depth keeps evals reproducible and cacheable.
DEFAULT_DEPTH: int = int(os.environ.get("CHESS_DEFAULT_DEPTH", "18"))

# Depth used when sweeping every ply of a full game. Lower than DEFAULT_DEPTH so a
# full-game review finishes in reasonable time; positions can be re-deepened on
# demand via get_engine_line.
SWEEP_DEPTH: int = int(os.environ.get("CHESS_SWEEP_DEPTH", "16"))

DEFAULT_MULTIPV: int = int(os.environ.get("CHESS_DEFAULT_MULTIPV", "1"))

# Two-stage full-game review. Stage 1 scans every mainline position at SWEEP_DEPTH with one PV;
# Stage 2 revisits only the selected critical positions with several candidates. Keeping the
# profile version explicit makes whole-game caches deterministic when selection semantics change.
DEEP_ANALYSIS_DEPTH: int = int(os.environ.get("CHESS_DEEP_ANALYSIS_DEPTH", "22"))
DEEP_ANALYSIS_MULTIPV: int = max(3, int(os.environ.get("CHESS_DEEP_ANALYSIS_MULTIPV", "3")))
CRITICAL_MIN: int = max(0, int(os.environ.get("CHESS_CRITICAL_MIN", "3")))
CRITICAL_MAX: int = max(CRITICAL_MIN, int(os.environ.get("CHESS_CRITICAL_MAX", "8")))
ANALYSIS_PROFILE_VERSION: str = os.environ.get(
    "CHESS_ANALYSIS_PROFILE_VERSION", "engine-analysis-v1"
).strip() or "engine-analysis-v1"
ANALYSIS_PRESET: str = os.environ.get("CHESS_ANALYSIS_PRESET", "balanced").strip().lower()
if ANALYSIS_PRESET not in {"fast", "balanced", "deep"}:
    ANALYSIS_PRESET = "balanced"

# Deterministic fact extraction replays only a bounded prefix of each Engine variation. Keeping
# this in the analysis profile makes changing the comparison horizon invalidate whole-game caches.
FACT_LINE_PLIES: int = max(1, int(os.environ.get("CHESS_FACT_LINE_PLIES", "8")))

# Engine process pool size. 1-2 is plenty for a single-user local tool. Default 2 so the
# web /evaluate route and a concurrent MCP call don't serialise behind one engine.
ENGINE_POOL_SIZE: int = int(os.environ.get("CHESS_ENGINE_POOL_SIZE", "2"))

# Per-engine UCI options.
ENGINE_THREADS: int = int(os.environ.get("CHESS_ENGINE_THREADS", "2"))
ENGINE_HASH_MB: int = int(os.environ.get("CHESS_ENGINE_HASH_MB", "128"))

# Centipawn magnitude treated as "mate-equivalent" when converting mate scores.
MATE_SCORE_CP: int = 10000

# Identity. USERNAME is the canonical "me" handle (the player_id join key, coaching profile, and
# analyze_game(player="auto") side detection). Default MUST be empty: a non-empty default would
# silently become every fresh install's "me" (the downloadable app's launcher never sets
# CHESS_USERNAME), suppressing the first-run prompt.
#
# LICHESS_USERNAME is specifically the Lichess handle, which is what drives the "open my latest game"
# autoload. CHESSCOM_USERNAME is the chess.com handle; it drives the chess.com game fetch +
# auto-sync (server.core.chesscom) and folds into USERNAME's profile as a chesscom-pinned alias.
# These are derived together by `_compose_identity` (called from env at import and re-applied
# from settings.json), so a chess.com-only user (no Lichess handle) is still canonically identified
# by their chess.com name.
USERNAME: str = ""
LICHESS_USERNAME: str = ""
CHESSCOM_USERNAME: str = ""
# The user-typed "other accounts" string is preserved verbatim in USERNAME_ALIASES_RAW (so the
# Settings form round-trips it); USERNAME_ALIASES is the parsed (platform|None, handle) pairs *plus*
# the derived chesscom alias.
USERNAME_ALIASES_RAW: str = ""
USERNAME_ALIASES: list[tuple[str | None, str]] = []


def _parse_aliases(raw: str) -> list[tuple[str | None, str]]:
    """Parse CHESS_ALIASES into (platform|None, handle_lower) pairs.

    Just a comma-separated list of your other handles, e.g. "my_chesscom_name, my_other_name".
    Each item normally matches on any site; advanced users can pin one to a single platform with
    "platform:handle" ("chesscom:dpdemler"). All of them resolve to CHESS_USERNAME as the canonical
    player_id, so several accounts fold into one coaching profile (and into player="auto" detection).
    """
    pairs: list[tuple[str | None, str]] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            plat, name = tok.split(":", 1)
            pairs.append((plat.strip().lower() or None, name.strip().lower()))
        else:
            pairs.append((None, tok.lower()))
    return pairs


def _compose_identity(lichess: str, chesscom: str, aliases_raw: str) -> None:
    """Set the identity globals coherently from the three user-facing inputs.

    Canonical USERNAME = the Lichess handle if given, else the chess.com handle (so a chess.com-only
    user is still attributed and profiled by their chess.com name). The chess.com handle folds into
    USERNAME's profile as a chesscom-pinned alias whenever it isn't already the canonical id. Called
    from the env at import and re-applied live by `settings.apply` (settings.json > env > defaults).
    """
    global USERNAME, LICHESS_USERNAME, CHESSCOM_USERNAME, USERNAME_ALIASES, USERNAME_ALIASES_RAW
    LICHESS_USERNAME = (lichess or "").strip()
    CHESSCOM_USERNAME = (chesscom or "").strip()
    USERNAME = LICHESS_USERNAME or CHESSCOM_USERNAME
    USERNAME_ALIASES_RAW = (aliases_raw or "").strip()
    aliases = _parse_aliases(USERNAME_ALIASES_RAW)
    if CHESSCOM_USERNAME and CHESSCOM_USERNAME.lower() != USERNAME.lower():
        aliases.append(("chesscom", CHESSCOM_USERNAME.lower()))
    USERNAME_ALIASES = aliases


# Identity from the env (.mcp.json setup path). Legacy CHESS_USERNAME is treated as the Lichess
# handle (it has always driven autoload); CHESS_CHESSCOM_USERNAME and CHESS_ALIASES are optional.
_compose_identity(
    os.environ.get("CHESS_USERNAME", ""),
    os.environ.get("CHESS_CHESSCOM_USERNAME", ""),
    os.environ.get("CHESS_ALIASES", ""),
)

# Game history (personalised coaching). Each analysed game is appended as one line to
# <DATA_DIR>/games/<game_id>/ stores imported PGN artifacts. Analysed-game history is appended to
# <DATA_DIR>/history/games.jsonl and deduped by (game_id, reviewed_side). Identity aliases
# (one person, several lichess/chess.com accounts) live in <DATA_DIR>/identities.json, and
# a rebuildable per-player profile is cached in <DATA_DIR>/profiles/<player_id>.json.
# CHESS_DATA_DIR overrides the location; CHESS_HISTORY=0 disables recording entirely.
# (_default_data_dir / _resolve_data_dir are defined near the top so Stockfish detection can
# also see the managed-engine path under DATA_DIR.)
DATA_DIR: str = _resolve_data_dir()
HISTORY_ENABLED: bool = os.environ.get("CHESS_HISTORY", "1") != "0"

# Disk cache of fully-analysed games
# (<DATA_DIR>/analysis-cache/<game_id>_<side>_<profile>.json). Reopening a game analysed with the
# same side and profile on this machine — even in a previous app session — loads from disk instead
# of re-running the
# ~20-45s Stockfish sweep. Best-effort; CHESS_ANALYSIS_CACHE=0 disables it. The entry cap bounds
# disk growth (least-recently-used pruned); CHESS_ANALYSIS_CACHE_MAX=0 means unbounded.
ANALYSIS_CACHE_ENABLED: bool = os.environ.get("CHESS_ANALYSIS_CACHE", "1") != "0"
ANALYSIS_CACHE_MAX: int = int(os.environ.get("CHESS_ANALYSIS_CACHE_MAX", "1000"))

# Position-level Stockfish cache. Its key includes the engine-reported version, relevant UCI
# options, FEN, fixed depth and MultiPV, so it remains valid across app restarts and threshold-only
# changes. Entries live separately from whole-game ReviewSession caches.
ENGINE_CACHE_ENABLED: bool = os.environ.get("CHESS_ENGINE_CACHE", "1") != "0"

# The engine-grounded templated coaching blurb (history.coach_summary) is always attached to a
# session summary — it's free (no engine/Claude work). The richer, Claude-WRITTEN summary is
# generated on demand via /api/coach (a button in the UI), so it only spends the user's Claude
# subscription when asked. This flag controls whether the UI presses that button AUTOMATICALLY for
# each game opened; off by default (CHESS_COACH_AI_AUTO=1 to default it on).
COACH_AI_AUTO: bool = os.environ.get("CHESS_COACH_AI_AUTO", "0") == "1"

# Whether a generated AI coach summary is REMEMBERED across app restarts (persisted into the
# analysis cache alongside the game) so reopening a game shows the saved summary instead of
# spending the user's Claude subscription to regenerate it. On by default to save tokens; a
# Settings → Advanced toggle and CHESS_COACH_AI_PERSIST=0 turn it off (regenerate each session).
# The refresh (⟳) button on the summary card always forces a fresh write regardless.
COACH_AI_PERSIST: bool = os.environ.get("CHESS_COACH_AI_PERSIST", "1") == "1"

# Whether the in-browser "Ask your AI coach" chat injects the player's cross-game coaching profile
# (recurring patterns from history) into the prompt. On by default; a Settings-panel toggle and
# CHESS_PERSONALIZE_HISTORY=0 turn it off to send fewer tokens.
PERSONALIZE_HISTORY: bool = os.environ.get("CHESS_PERSONALIZE_HISTORY", "1") == "1"

# Self-terminate the server process after this many seconds of inactivity (no MCP tool call
# and no board request), so an abandoned session doesn't linger as a process forever. Activity
# resets the timer. Default 24h; CHESS_SESSION_TTL=0 disables the watchdog.
SESSION_TTL_SECONDS: int = int(os.environ.get("CHESS_SESSION_TTL", str(24 * 60 * 60)))


def _parse_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _parse_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Coaching profile is a HYBRID of two views so it adapts as a player improves:
#   - "recent form" = the last CHESS_PROFILE_RECENT games (a sliding window; <=0 means all games).
#   - "lifetime"    = CHESS_PROFILE_LIFETIME: unset/"all" -> all history (default); a positive N ->
#                     the last N games; "0" -> DISABLED, leaving only the recent window (i.e. a pure
#                     sliding window). Both are recomputed from the full games.jsonl, so widening a
#                     window later loses nothing.
PROFILE_RECENT_WINDOW: int = _parse_int("CHESS_PROFILE_RECENT", 100)


def _parse_lifetime(raw: str | None) -> int | None:
    raw = (raw or "").strip().lower()
    if raw in ("", "all"):
        return None  # all history
    try:
        return max(int(raw), 0)  # 0 disables the lifetime view; positive caps it
    except ValueError:
        return None


PROFILE_LIFETIME: int | None = _parse_lifetime(os.environ.get("CHESS_PROFILE_LIFETIME"))


def _parse_elo(raw: str | None) -> int | None:
    """Optional player strength (normalized ~chess.com/FIDE Elo). Blank/garbled -> None (Auto)."""
    raw = (raw or "").strip()
    if not raw or raw.lower() == "auto":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# The reviewed player's skill, used to tune how strict mistake detection is (stronger players get
# smaller win%-drop cutoffs flagged + a deeper sweep). None = "Auto": read each game's Elo from the
# PGN headers. A set value overrides the PGN. Surfaced editably in the Settings panel ("Skill level").
PLAYER_ELO: int | None = _parse_elo(os.environ.get("CHESS_PLAYER_ELO"))

# Lichess game import (so users don't paste PGNs). The fetch_games/fetch_game tools call the
# public Lichess API. Auth is OPTIONAL: set LICHESS_TOKEN to a Personal Access Token
# (https://lichess.org/account/oauth/token, no scopes needed for public game export) and requests
# are throttled per-token instead of per-IP — the escape hatch for heavy users who hit rate limits.
# Anonymous (no token) works fine for public games. LICHESS_API_BASE is overridable for testing.
LICHESS_TOKEN: str = os.environ.get("LICHESS_TOKEN", "").strip()
LICHESS_API_BASE: str = os.environ.get("LICHESS_API_BASE", "https://lichess.org").rstrip("/")
# How many recent games fetch_games returns when a count isn't given.
LICHESS_DEFAULT_MAX: int = int(os.environ.get("CHESS_LICHESS_MAX", "3"))
# HTTP timeout (seconds) for Lichess requests.
LICHESS_TIMEOUT: float = float(os.environ.get("CHESS_LICHESS_TIMEOUT", "20"))

# Chess.com game import (server.core.chesscom). Uses the public published-data API (no auth).
# CHESSCOM_API_BASE is overridable for testing. The auto-sync (POST /api/sync/chesscom) checks the
# configured user's newest CHESSCOM_SYNC_MAX games on app launch and analyses any not yet in
# history; CHESS_CHESSCOM_SYNC=0 disables the automatic sync (manual fetch still works). Both the
# on/off flag and the count are user-editable in the Settings panel (settings.json wins over env).
CHESSCOM_API_BASE: str = os.environ.get("CHESS_CHESSCOM_API_BASE", "https://api.chess.com").rstrip("/")
CHESSCOM_TIMEOUT: float = float(os.environ.get("CHESS_CHESSCOM_TIMEOUT", "20"))
CHESSCOM_SYNC_ENABLED: bool = os.environ.get("CHESS_CHESSCOM_SYNC", "1") != "0"
CHESSCOM_SYNC_MAX: int = int(os.environ.get("CHESS_CHESSCOM_SYNC_MAX", "5"))

# Endgame tablebase (server.core.tablebase). For <=7-man positions the in-browser chat / AI coach
# facts include the EXACT theoretical result (win/draw/loss + DTZ/DTM) from the public Lichess
# tablebase API, so endgame advice is precise instead of trusting a depth-limited eval. Best-effort
# (any network/parse failure is silently omitted) and only used by the chat/coach path — the
# interactive board never probes. CHESS_TABLEBASE=0 disables it; the API base is overridable for
# tests.
TABLEBASE_ENABLED: bool = os.environ.get("CHESS_TABLEBASE", "1") != "0"
TABLEBASE_API_BASE: str = os.environ.get(
    "CHESS_TABLEBASE_API_BASE", "https://tablebase.lichess.ovh"
).rstrip("/")
TABLEBASE_TIMEOUT: float = float(os.environ.get("CHESS_TABLEBASE_TIMEOUT", "6"))

# Web board. The standalone FastAPI process is the primary runtime. WEB_AUTOSTART is retained for
# the optional MCP entry point, which can reuse the same engine pool and ReviewSession.
WEB_HOST: str = os.environ.get("CHESS_WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.environ.get("CHESS_WEB_PORT", "8765"))
WEB_AUTOSTART: bool = os.environ.get("CHESS_WEB_AUTOSTART", "1") != "0"
# Auto-open the board in the default browser the first time a game is analysed, so a
# first-time user never has to be told the URL. Set CHESS_WEB_OPEN=0 to disable.
WEB_OPEN: bool = os.environ.get("CHESS_WEB_OPEN", "1") != "0"
# "App mode" is retained for compatibility with packaged launchers. The frontend reads it via
# /api/app-config and, when on, auto-loads the user's most recent Lichess game on open. Left off
# (0) for the MCP-driven board and dev `run_web.py <pgn>` runs, so neither gets a surprise autoload.
APP_MODE: bool = os.environ.get("CHESS_APP_MODE", "0") == "1"

# Review-workspace preferences. These are also editable in the local Settings panel and never
# leave the machine. ``review`` orientation means follow whichever side is being reviewed.
DEFAULT_REVIEW_SIDE: str = os.environ.get("CHESS_DEFAULT_REVIEW_SIDE", "auto").strip().lower()
if DEFAULT_REVIEW_SIDE not in {"auto", "white", "black"}:
    DEFAULT_REVIEW_SIDE = "auto"
BOARD_ORIENTATION: str = os.environ.get("CHESS_BOARD_ORIENTATION", "review").strip().lower()
if BOARD_ORIENTATION not in {"review", "white", "black"}:
    BOARD_ORIENTATION = "review"
SHOW_THREAT_ARROWS: bool = os.environ.get("CHESS_SHOW_THREAT_ARROWS", "0") == "1"

# Local / self-hosted LLM for the in-browser chat + AI coach summary. When LOCAL_LLM_BASE_URL is
# set, the chat/coach are served by DIRECT HTTP to that server (see server.core.local_llm) — no
# `claude` CLI and no login needed — instead of the user's Claude subscription. Any server with an
# OpenAI-compatible /v1/chat/completions endpoint works (Ollama, LM Studio, llama.cpp, a LiteLLM
# proxy). LOCAL_LLM_MODEL names the model to request (e.g. "qwen2.5-coder"); required for the local
# path. Both are editable in the Settings panel. Leave the base URL blank to keep the default
# subscription path (headless `claude -p`).
LOCAL_LLM_BASE_URL: str = os.environ.get("CHESS_LOCAL_LLM_BASE_URL", "").strip()
LOCAL_LLM_MODEL: str = os.environ.get("CHESS_LOCAL_LLM_MODEL", "").strip()

# Structured per-position coaching explanations (Phase 5). ``auto`` reuses the local
# OpenAI-compatible model from Settings when configured, otherwise it falls back to the optional
# Claude CLI. A dedicated base/model/key can be supplied for a remote OpenAI-compatible API without
# exposing credentials to the browser or persisting them in game artifacts.
EXPLANATION_PROVIDER: str = os.environ.get("CHESS_EXPLANATION_PROVIDER", "auto").strip() or "auto"
EXPLANATION_BASE_URL: str = os.environ.get("CHESS_EXPLANATION_BASE_URL", "").strip()
EXPLANATION_MODEL: str = os.environ.get("CHESS_EXPLANATION_MODEL", "").strip()
EXPLANATION_API_KEY: str = os.environ.get("CHESS_EXPLANATION_API_KEY", "").strip()
EXPLANATION_TIMEOUT: int = max(1, int(os.environ.get("CHESS_EXPLANATION_TIMEOUT", "600")))
EXPLANATION_LANGUAGE: str = os.environ.get("CHESS_EXPLANATION_LANGUAGE", "zh-CN").strip() or "zh-CN"

# Optional API-only Chess Coach Agent. This configuration is deliberately separate from the
# bounded explanation provider and the legacy CLI-backed chat: no setting, CLI login, or desktop
# subscription is used as an implicit Agent credential or transport fallback.
AGENT_ENABLED: bool = os.environ.get("CHESS_AGENT_ENABLED", "1") != "0"
AGENT_REVIEW_CHAT_ENABLED: bool = os.environ.get("CHESS_AGENT_REVIEW_CHAT", "0") == "1"
AGENT_MODEL: str = os.environ.get("CHESS_AGENT_MODEL", "").strip()
AGENT_BASE_URL: str = os.environ.get("CHESS_AGENT_BASE_URL", "").strip().rstrip("/")
AGENT_API_KEY: str = os.environ.get("CHESS_AGENT_API_KEY", "").strip()
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "").strip()
AGENT_MAX_TURNS: int = max(1, _parse_int("CHESS_AGENT_MAX_TURNS", 4))
AGENT_MAX_TOOL_CALLS: int = max(0, _parse_int("CHESS_AGENT_MAX_TOOL_CALLS", 6))
AGENT_MAX_ENGINE_CALLS: int = max(0, _parse_int("CHESS_AGENT_MAX_ENGINE_CALLS", 2))
AGENT_TIMEOUT: int = max(1, _parse_int("CHESS_AGENT_TIMEOUT", 120))

# --- Puzzle mode (server.core.puzzles / puzzle_rating) ------------------------------------------
# A tactical-trainer built on the same substrate (board, engine, claude_bridge, DATA_DIR). Puzzles
# ship as small compressed JSONL: a committed offline baseline (server/data/puzzles/baseline.jsonl.gz,
# >=100/band) plus dense per-band shards downloaded on demand (P3) from a SEPARATE data repo so the
# app's Releases tab stays app-versions-only. Per-user state (rating, seen_ids, streak) lives in
# <DATA_DIR>/puzzles/state.json. CHESS_PUZZLES=0 hides the feature entirely.
PUZZLES_ENABLED: bool = os.environ.get("CHESS_PUZZLES", "1") != "0"
# Dedicated puzzle-DATA repo (NOT the app repo) hosting the dense band shards as release assets.
PUZZLE_SHARD_REPO: str = os.environ.get(
    "CHESS_PUZZLE_SHARD_REPO", "Chess-analysis-mcp/tintins-chess-puzzles"
).strip()
PUZZLE_SHARD_TAG: str = os.environ.get("CHESS_PUZZLE_SHARD_TAG", "puzzles-v1").strip()
# Allow the background shard fetch (P3). 0 = baseline-only / fully offline. On first puzzle use the
# whole set (~16 MB / 23 bands) downloads in the background and is kept — no windowing, no LRU.
PUZZLE_DOWNLOAD: bool = os.environ.get("CHESS_PUZZLE_DOWNLOAD", "1") != "0"
# How long (seconds) a cached puzzle manifest.json is trusted before re-fetching from the data-repo
# release (P3), mirroring UPDATE_CHECK_INTERVAL. Best-effort; a failed refresh keeps the stale copy.
PUZZLE_MANIFEST_INTERVAL: int = _parse_int("CHESS_PUZZLE_MANIFEST_INTERVAL", 6 * 3600)
# Occasionally mix "from your own games" mistake puzzles (P3.5) into the main tactic stream. Default
# ON: a small fraction of tactics are replaced by a position from one of your own games, so the
# trainer surfaces your real weaknesses without you having to switch to the "From your games" source.
# A Settings toggle turns it off. Needs the engine (mistake puzzles validate live) + analysed games.
PUZZLE_MISTAKE_INTERLEAVE: bool = os.environ.get("CHESS_PUZZLE_MISTAKE_INTERLEAVE", "1") != "0"
# Probability (0..1) that a curated-tactic request is swapped for an own-game mistake puzzle when
# interleave is on. 0.2 = ~1 in 5, occasional enough not to derail a tactics session.
PUZZLE_MISTAKE_INTERLEAVE_PROB: float = _parse_float("CHESS_PUZZLE_MISTAKE_INTERLEAVE_PROB", 0.2)
# Only let an attempt move the Glicko rating when the puzzle's own RatingDeviation is below this
# (mirrors Lichess: well-established puzzles only). Higher-RD puzzles still play, but unrated.
# Kept generous: Glicko already down-weights a high-RD puzzle in the update, so a low gate here
# needlessly leaves a big slice of perfectly good puzzles unrated (at 90, ~1/3 of the baseline).
PUZZLE_MAX_RD: int = _parse_int("CHESS_PUZZLE_MAX_RD", 130)
# Show the solve/miss board animations (square flash, ripple ring, confetti, shake). Off = the
# result is conveyed by the verdict text colour alone (green / orange / red). Settings toggle.
PUZZLE_ANIMATIONS: bool = os.environ.get("CHESS_PUZZLE_ANIMATIONS", "1") != "0"
# Auto-load the next puzzle a beat after a solve (flow-state grinding) instead of waiting for the
# "Next puzzle" button. The frontend still holds long enough for the solve animations to finish
# before advancing. Only fires on a solve, never after "Show solution". Settings toggle; OFF by
# default (opt in via ⚙ Settings) so a solve leaves you on the board until you press "Next puzzle".
PUZZLE_AUTO_ADVANCE: bool = os.environ.get("CHESS_PUZZLE_AUTO_ADVANCE", "0") != "0"
# "Puzzle storm" timed rush (P4): solve as many as you can before the clock runs out. Base clock in
# seconds; a combo run grants a small time bonus and a wrong move costs time (see puzzle_storm.py).
# Unrated by design (fast, ramping difficulty) - it only tracks a best-score highscore in state.json.
PUZZLE_STORM_DURATION: int = _parse_int("CHESS_PUZZLE_STORM_DURATION", 180)


def _puzzle_dir() -> str:
    """Per-user puzzle state + downloaded shards (under DATA_DIR, shared by every entry point)."""
    return os.path.join(DATA_DIR, "puzzles")


# --- Auto-update (server.core.updates) ----------------------------------------------------------
# The app version (canonical = pyproject.toml). Surfaced via /api/app-config and compared against
# the latest GitHub release tag so the board can show a non-blocking "update available" notice.
APP_VERSION: str = _read_app_version()
# GitHub repo that publishes Releases (the update source of truth). "owner/name".
UPDATE_REPO: str = os.environ.get("CHESS_UPDATE_REPO", "").strip()
# Master switch for the update check (the network call to GitHub). 0 disables it entirely.
UPDATE_CHECK_ENABLED: bool = os.environ.get("CHESS_UPDATE_CHECK", "0") != "0"
# Min seconds between GitHub release lookups (cached in-process + on disk so restarts don't re-hit).
UPDATE_CHECK_INTERVAL: float = float(os.environ.get("CHESS_UPDATE_CHECK_INTERVAL", str(6 * 3600)))
# HTTP timeout (seconds) for the GitHub API call.
UPDATE_TIMEOUT: float = float(os.environ.get("CHESS_UPDATE_TIMEOUT", "8"))

"""Setup self-check: `uv run python -m server.doctor`.

Verifies the two things the Web core needs: a new-enough Python and a working Stockfish binary.
Agent and explanation credentials are optional backend configuration and are not probed here.
"""
from __future__ import annotations

import shutil
import sys

from server import config

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"


def _check_python() -> bool:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 11)
    mark = OK if ok else BAD
    print(f"{mark} Python {v.major}.{v.minor}.{v.micro}")
    if not ok:
        print("    Need Python 3.11+. With uv this is automatic — run the install script "
              "(see README) so uv fetches a compatible Python.")
    return ok


def _check_stockfish() -> bool:
    path = config.STOCKFISH_PATH
    resolved = shutil.which(path) or (path if "/" in path else None)
    if resolved is None:
        print(f"{BAD} Stockfish: not found")
        print(f"    {config.stockfish_install_hint()}")
        return False
    # Confirm it actually launches and speaks UCI, not just that a file exists.
    try:
        import chess.engine

        eng = chess.engine.SimpleEngine.popen_uci(resolved)
        try:
            name = eng.id.get("name", "Stockfish")
        finally:
            eng.quit()
        print(f"{OK} Stockfish: {name}  ({resolved})")
        return True
    except Exception as exc:  # noqa: BLE001 - report any launch failure plainly
        print(f"{BAD} Stockfish at {resolved} would not start: {exc}")
        print(f"    {config.stockfish_install_hint(resolved)}")
        return False


def status() -> dict:
    """Structured self-check for the web UI (``GET /api/doctor``).

    Lightweight on purpose — it resolves binaries on PATH rather than launching Stockfish, so it's
    cheap to call on every page load. Never raises.
    """
    v = sys.version_info
    sf_path = config.STOCKFISH_PATH
    sf_resolved = shutil.which(sf_path) or (sf_path if "/" in sf_path else None)
    return {
        "python": {
            "ok": (v.major, v.minor) >= (3, 11),
            "detail": f"{v.major}.{v.minor}.{v.micro}",
        },
        "stockfish": {
            "ok": bool(sf_resolved),
            "path": sf_resolved or "",
            "hint": "" if sf_resolved else config.stockfish_install_hint(),
            # macOS/Apple-Silicon only: flags an Intel build running under Rosetta 2 so the UI can
            # offer a one-click swap to the native arm64 engine. Best-effort; {suboptimal:False} off-mac.
            "arch": config.stockfish_arch_report(sf_resolved) if sf_resolved else {"suboptimal": False},
        },
    }


def main() -> int:
    print("Chess Review Coach - setup check\n")
    py_ok = _check_python()
    sf_ok = _check_stockfish()
    print()
    if py_ok and sf_ok:
        print(f"{OK} Core is ready. Start the Web app with:")
        print("    uv run python -m server.web.runner")
        return 0
    print(f"{BAD} Setup incomplete — fix the items marked above and re-run `uv run python -m server.doctor`.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

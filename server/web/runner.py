"""Run the sole Chess Review Coach FastAPI + static frontend product runtime."""
from __future__ import annotations

import ipaddress
import sys
import threading
import webbrowser

import uvicorn

from server import config
from server.core import engine
from server.core import settings
from server.web.app import create_app

_opened = False
_open_lock = threading.Lock()


def _web_url() -> str:
    host = f"[{config.WEB_HOST}]" if ":" in config.WEB_HOST else config.WEB_HOST
    return f"http://{host}:{config.WEB_PORT}"


def open_board_once() -> None:
    """Open the board in the default browser, at most once per process.

    Called when a game is analysed (not at server boot) so the tab only appears once
    there is actually a game to look at. Best-effort: a headless box or a missing
    browser just logs to stderr and never raises. Disable with CHESS_WEB_OPEN=0.
    """
    global _opened
    if not config.WEB_OPEN:
        return
    with _open_lock:
        if _opened:
            return
        _opened = True
    url = _web_url()
    try:
        if webbrowser.open(url):
            print(f"[chess-web] opened board in browser: {url}", file=sys.stderr, flush=True)
        else:
            print(
                f"[chess-web] no browser to open; board is at {url}",
                file=sys.stderr,
                flush=True,
            )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[chess-web] could not open browser ({exc}); board is at {url}",
              file=sys.stderr, flush=True)


def _require_loopback(host: str) -> None:
    """Reject accidental LAN/public exposure of this single-user local application."""
    value = (host or "").strip().lower()
    if value == "localhost":
        return
    try:
        if ipaddress.ip_address(value).is_loopback:
            return
    except ValueError:
        pass
    raise SystemExit(
        "CHESS_WEB_HOST must be a loopback address (127.0.0.1, ::1, or localhost)."
    )


def main() -> int:
    """Start the primary, single-process FastAPI + static frontend runtime."""
    settings.apply_saved()
    _require_loopback(config.WEB_HOST)
    url = _web_url()
    print(f"Chess Review Coach is available at {url}", flush=True)
    if config.WEB_OPEN:
        threading.Timer(0.75, open_board_once).start()
    try:
        uvicorn.run(
            create_app(),
            host=config.WEB_HOST,
            port=config.WEB_PORT,
            log_level="info",
            access_log=False,
        )
    finally:
        engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

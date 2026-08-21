"""Update-notifier routes.

`GET /api/update-check` is the throttled GitHub-release lookup behind the board's "update available"
banner (fire-and-forget from the frontend, like /api/doctor). `POST /api/apply-update` stages a
one-click update for self-updatable channels (git / zip) by writing a sentinel the launcher applies
on the next start. Both are best-effort and never raise to the page.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from server.core import updates

router = APIRouter()


@router.get("/update-check")
def get_update_check() -> dict:
    """Is a newer release out? Returns current/latest/severity/channel; never raises (offline ->
    update_available False)."""
    try:
        return updates.check_for_update()
    except Exception:  # noqa: BLE001 - the banner must never break the page
        return {"update_available": False}


@router.post("/apply-update")
def post_apply_update() -> JSONResponse:
    """Stage a one-click update (git + zip channels). Writes a sentinel the launcher consumes on the
    next start; the user just reopens the app. The read-only `.app` can't self-update -> 409."""
    if not updates.can_self_update():
        return JSONResponse(
            {"ok": False, "error": "This install can't self-update; download the latest from Releases."},
            status_code=409,
        )
    try:
        return JSONResponse(updates.request_update())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

"""Bounded local diagnostic: python -m tests.backend.diagnose_runtime_session.

Uses only a temporary database; no model or Engine requests are made.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import os


async def diagnose(data_dir: str) -> None:
    from server.core.agent.runtime_openai import SQLiteConversationSessionFactory

    async def step(name, operation):
        started = time.monotonic()
        print(f"{name}: started", flush=True)
        result = await asyncio.wait_for(operation, timeout=5)
        print(f"{name}: completed in {time.monotonic() - started:.3f}s", flush=True)
        return result

    await step("asyncio.to_thread", asyncio.to_thread(lambda: None))
    factory = SQLiteConversationSessionFactory(data_dir)
    try:
        session = factory.get_session("runtime-diagnostic")
        await step("sqlite.add_items", session.add_items([{"role": "user", "content": "test"}]))
        items = await step("sqlite.get_items", session.get_items())
        assert len(items) == 1
        await step("sqlite.clear_session", factory.clear_session("runtime-diagnostic"))
    finally:
        factory.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agent-session-diagnostic-") as directory:
        os.environ["CHESSCOACH_DATA_DIR"] = directory
        asyncio.run(diagnose(directory))

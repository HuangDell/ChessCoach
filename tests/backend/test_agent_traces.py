from __future__ import annotations

import asyncio
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from server.core.storage.agent_traces import AgentRawTraceStore


class _Request:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers = {"Authorization": "Bearer must-not-be-written"}

    async def aread(self) -> bytes:
        return self.body


class _Response:
    def __init__(self, request: _Request, body: bytes) -> None:
        self.request = request
        self.body = body

    async def aread(self) -> bytes:
        return self.body


class AgentRawTraceStoreTests(unittest.TestCase):
    def test_writes_ordered_private_bodies_without_headers_and_prunes_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-traces-") as data_dir:
            store = AgentRawTraceStore(data_dir, max_runs=2)
            for index in range(3):
                request = _Request(f'{{"request":{index}}}'.encode())
                response = _Response(request, f'{{"response":{index}}}'.encode())
                with store.activate(f"run-{index}"):
                    asyncio.run(store.on_request(request))
                    asyncio.run(store.on_response(response))

            root = Path(data_dir) / "agent" / "traces"
            directories = sorted(path for path in root.iterdir() if path.is_dir())
            self.assertEqual(2, len(directories))
            self.assertFalse(any("run-0" in path.name for path in directories))
            newest = next(path for path in directories if "run-2" in path.name)
            self.assertEqual(b'{"request":2}', (newest / "001-request.json").read_bytes())
            self.assertEqual(b'{"response":2}', (newest / "001-response.json").read_bytes())
            self.assertNotIn(b"must-not-be-written", (newest / "001-request.json").read_bytes())
            if hasattr(stat, "S_IMODE"):
                self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
                self.assertEqual(
                    0o600,
                    stat.S_IMODE((newest / "001-response.json").stat().st_mode),
                )

    def test_trace_write_failure_is_logged_and_does_not_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-traces-") as data_dir:
            store = AgentRawTraceStore(data_dir)
            request = _Request(b'{"request":1}')
            with patch.object(store, "_write_private", side_effect=PermissionError):
                with self.assertLogs("chesscoach.agent", level="WARNING") as logs:
                    with store.activate("run-failure"):
                        asyncio.run(store.on_request(request))

            self.assertIn("raw_trace_write_failed", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from pathlib import Path
import stat
import tempfile
import threading
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


class _SyncRequest:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers = {"Authorization": "Bearer must-not-be-written"}

    def read(self) -> bytes:
        return self.body


class _SyncResponse:
    def __init__(self, request: _SyncRequest, body: bytes) -> None:
        self.request = request
        self.body = body

    def read(self) -> bytes:
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

    def test_sync_and_async_hooks_share_retention_and_keep_contexts_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shared-traces-") as data_dir:
            store = AgentRawTraceStore(data_dir, max_runs=2)
            barrier = threading.Barrier(2)

            def write_sync() -> None:
                request = _SyncRequest(b'{"sync-request":true}')
                with store.activate("explanation-position-hash"):
                    barrier.wait()
                    store.on_sync_request(request)
                    store.on_sync_response(_SyncResponse(request, b'{"sync-response":true}'))

            thread = threading.Thread(target=write_sync)
            thread.start()
            request = _Request(b'{"async-request":true}')
            with store.activate("agent-run"):
                barrier.wait()
                asyncio.run(store.on_request(request))
                asyncio.run(store.on_response(_Response(request, b'{"async-response":true}')))
            thread.join()

            with store.activate("newest-run"):
                newest_request = _SyncRequest(b'{"newest":true}')
                store.on_sync_request(newest_request)

            directories = sorted((Path(data_dir) / "agent" / "traces").iterdir())
            self.assertEqual(2, len(directories))
            contents = {
                path.name: sorted(file.read_bytes() for file in path.iterdir())
                for path in directories
            }
            for bodies in contents.values():
                self.assertFalse(
                    b'{"async-request":true}' in bodies
                    and b'{"sync-request":true}' in bodies
                )
            self.assertTrue(any("newest-run" in name for name in contents))


if __name__ == "__main__":
    unittest.main()

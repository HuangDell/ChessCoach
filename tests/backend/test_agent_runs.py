from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from pydantic import ValidationError

from server.core.agent.models import ToolCallRecord, ToolPositionReference
from server.core.storage.agent_runs import AgentRunRecord, AgentRunStore


def _record(index: int, *, status: str = "success") -> AgentRunRecord:
    return AgentRunRecord(
        run_id=f"run-{index}",
        session_id="session-1",
        generation=index,
        task_kind="position_analysis",
        activity="position_analysis",
        model="gpt-test",
        endpoint_type="openai_responses",
        sdk_version="0.22.0",
        policy_version=1,
        response_schema_version=1,
        started_at="2026-08-25T00:00:00Z",
        duration_ms=(index + 1) * 10,
        usage={"input_tokens": index + 1},
        status=status,
        error_code="agent_timeout" if status == "timeout" else None,
        tool_calls=[
            ToolCallRecord(
                name="analyze_position",
                permission="compute",
                status="ok",
                duration_ms=3,
                cache_hit=index % 2 == 0,
                engine_call_count=index % 2,
                position_reference=ToolPositionReference(
                    fen_fingerprint=f"{index:016x}"
                ),
            )
        ],
    )


class AgentRunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-runs-")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_concurrent_append_is_complete_and_bounded(self) -> None:
        store = AgentRunStore(self.temporary.name, max_records=30)
        threads = [threading.Thread(target=store.append, args=(_record(index),)) for index in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        records = store.read(limit=1000)
        self.assertEqual(30, len(records))
        self.assertEqual(30, len({record.run_id for record in records}))
        self.assertTrue(all(record.schema_version == 1 for record in records))

    def test_corrupt_lines_are_skipped_during_read_retention_and_metrics(self) -> None:
        store = AgentRunStore(self.temporary.name, max_records=3)
        store.append(_record(0))
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write("{not-json}\n")
        store.append(_record(1, status="timeout"))
        store.append(_record(2))
        store.append(_record(3))

        self.assertEqual(["run-1", "run-2", "run-3"], [item.run_id for item in store.read()])
        metrics = store.metrics(limit=100)
        self.assertEqual(3, metrics["record_count"])
        self.assertEqual({"success": 2, "timeout": 1}, metrics["status_counts"])
        self.assertEqual({"agent_timeout": 1}, metrics["error_counts"])
        self.assertEqual(3, metrics["tools"]["calls"])
        self.assertEqual(1, metrics["tools"]["cache_hits"])
        self.assertEqual(2, metrics["tools"]["engine_calls"])
        self.assertEqual({"p50": 30, "p95": 40, "max": 40}, metrics["latency_ms"])

    def test_payload_is_redacted_and_clear_is_isolated(self) -> None:
        store = AgentRunStore(self.temporary.name)
        store.append(_record(0))
        sibling = Path(self.temporary.name) / "agent" / "sessions" / "keep.json"
        sibling.parent.mkdir(parents=True)
        sibling.write_text('{"keep": true}', encoding="utf-8")

        serialized = store.path.read_text(encoding="utf-8")
        record = json.loads(serialized)
        forbidden_fields = {
            "prompt", "message", "base_url", "api_key", "credential", "fen", "pv",
            "arguments", "reasoning", "traceback",
        }
        self.assertTrue(forbidden_fields.isdisjoint(record))
        self.assertNotIn("rnbqkbnr", serialized)
        self.assertNotIn("https://", serialized)

        result = store.clear()
        self.assertEqual(1, result["records_removed"])
        self.assertEqual([], store.read())
        self.assertTrue(sibling.exists())

    def test_usage_rejects_provider_metadata(self) -> None:
        values = _record(0).model_dump(mode="python")
        values["usage"] = {"input_tokens": 2, "api_key": "secret"}
        with self.assertRaises(ValidationError):
            AgentRunRecord.model_validate(values)


if __name__ == "__main__":
    unittest.main()

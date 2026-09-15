"""RAG translation eval contracts; all model calls use local fakes."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.evals.rag.translate import ModelTranslation, run_translations


class RagTranslationTests(unittest.IsolatedAsyncioTestCase):
    def _dataset(self, root: Path) -> Path:
        dataset = root / "dataset"
        dataset.mkdir()
        rows = [
            {"query_id": "Q1", "query_zh": "这是王翼弃兵吗？", "query_en_reference": "SECRET", "context": {"fen": "SECRET"}},
            {"query_id": "Q2", "query_zh": "象应该放在哪里？", "query_en_reference": "SECRET2", "context": None},
        ]
        (dataset / "queries.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        (dataset / "translation_prompt.md").write_text(
            "heading\n```text\nTranslate only the supplied Chinese question.\n```\nfooter\n",
            encoding="utf-8",
        )
        (dataset / "labels.json").write_text("DO NOT READ", encoding="utf-8")
        return dataset

    async def test_freezes_ordered_translations_without_reference_or_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []

            async def fake(prompt, query):
                calls.append((prompt, query))
                return ModelTranslation(
                    output_text=json.dumps({"query_en": f"English {len(calls)}"}),
                    status="completed",
                    usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                )

            output = root / "output"
            report = await run_translations(
                dataset_dir=self._dataset(root), output_dir=output, model="fake-model",
                provider="generic", endpoint_type="custom_responses", request_translation=fake,
            )
            translations = json.loads((output / "translations.json").read_text())
            self.assertEqual([row["query_id"] for row in translations], ["Q1", "Q2"])
            self.assertEqual([row["query_en"] for row in translations], ["English 1", "English 2"])
            self.assertEqual([query for _prompt, query in calls], ["这是王翼弃兵吗？", "象应该放在哪里？"])
            self.assertNotIn("SECRET", json.dumps(calls, ensure_ascii=False))
            self.assertEqual(report["successful_queries"], 2)
            self.assertEqual(report["usage"]["totals"]["input_tokens"], 6)
            self.assertNotIn("base_url", json.dumps(report))
            self.assertEqual(
                (output / "prompt.txt").read_text(encoding="utf-8"),
                "Translate only the supplied Chinese question.",
            )

    async def test_sanitizes_provider_and_invalid_output_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = 0

            async def fake(_prompt, _query):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("credential=private and provider body")
                return ModelTranslation(
                    output_text="not json", status="completed",
                    usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                )

            output = root / "output"
            report = await run_translations(
                dataset_dir=self._dataset(root), output_dir=output, model="fake-model",
                provider="generic", endpoint_type="custom_responses", request_translation=fake,
            )
            payload = (output / "translations.json").read_text()
            translations = json.loads(payload)
            self.assertEqual([row["error"]["code"] for row in translations], ["provider_error", "invalid_json"])
            self.assertNotIn("private", payload)
            self.assertEqual(report["failed_queries"], 2)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["usage"]["totals"]["total_tokens"], 10)

    async def test_timeout_is_sanitized_and_does_not_stop_later_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            async def slow(_prompt, _query):
                await asyncio.sleep(0.05)
                raise AssertionError("wait_for should cancel this request")

            output = root / "output"
            with patch("tests.evals.rag.translate.config.AGENT_TIMEOUT", 0.001):
                report = await run_translations(
                    dataset_dir=self._dataset(root), output_dir=output, model="fake-model",
                    provider="generic", endpoint_type="custom_responses", request_translation=slow,
                )
            translations = json.loads((output / "translations.json").read_text())
            self.assertEqual([row["error"]["code"] for row in translations], ["timeout", "timeout"])
            self.assertEqual(report["completed_queries"], 2)
            self.assertEqual(report["failed_queries"], 2)

    async def test_refuses_to_overwrite_an_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()

            async def unused(_prompt, _query):
                raise AssertionError("request must not run")

            with self.assertRaises(FileExistsError):
                await run_translations(
                    dataset_dir=self._dataset(root), output_dir=output, model="fake-model",
                    provider="generic", endpoint_type="custom_responses", request_translation=unused,
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from server import config
from server.core import settings
from server.core.explanation.providers import ExplanationProviderError, configured_provider
from server.web.app import create_app


ROOT = Path(__file__).parents[2]
LEGACY_ENDPOINTS = {
    "/api/chat",
    "/api/chat-history",
    "/api/chat-reset",
    "/api/coach",
    "/api/puzzle/explain",
    "/api/puzzle/storm/summary",
}


class WebOnlyRemovalTests(unittest.TestCase):
    def test_legacy_runtime_modules_and_openapi_paths_are_absent(self) -> None:
        for relative in (
            "server/mcp_server.py",
            "server/claude_bridge.py",
            "server/core/chat_store.py",
            "server/core/local_llm.py",
            "server/web/routes_chat.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)
        self.assertTrue(LEGACY_ENDPOINTS.isdisjoint(create_app().openapi()["paths"]))

    def test_frontend_has_no_legacy_ai_endpoint_or_control(self) -> None:
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "frontend").rglob("*.js")
        ) + (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        for marker in (
            '"/api/chat"',
            '"/api/coach"',
            '"/api/puzzle/explain"',
            '"/api/puzzle/storm/summary"',
            'id="pz-explain"',
            'id="pz-chat-form"',
            'id="pz-storm-summary-btn"',
        ):
            self.assertNotIn(marker, sources)

    def test_old_settings_are_readable_but_not_returned_or_rewritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="legacy-settings-") as data_dir:
            path = Path(data_dir) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "username": "test-user",
                        "coach_ai_auto": True,
                        "coach_ai_persist": True,
                        "local_llm_base_url": "http://127.0.0.1:1234/v1",
                        "local_llm_model": "old-model",
                        "explanation_provider": "claude-cli",
                    }
                ),
                encoding="utf-8",
            )
            loaded = settings.load(data_dir)
            self.assertTrue(loaded["coach_ai_auto"])
            result = settings.update({"username": "updated"}, data_dir)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("updated", result["username"])
            for key in (
                "coach_ai_auto",
                "coach_ai_persist",
                "local_llm_base_url",
                "local_llm_model",
            ):
                self.assertNotIn(key, result)
                self.assertNotIn(key, persisted)

    def test_legacy_explanation_provider_value_has_no_cli_fallback(self) -> None:
        previous = config.EXPLANATION_PROVIDER
        try:
            config.EXPLANATION_PROVIDER = "claude-cli"
            with self.assertRaises(ExplanationProviderError):
                configured_provider()
        finally:
            config.EXPLANATION_PROVIDER = previous


if __name__ == "__main__":
    unittest.main()

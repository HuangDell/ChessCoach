from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from server import config
from server.core.explanation.models import ExplanationRequest
from server.core.explanation.providers import (
    ExplanationProviderError,
    OpenAICompatibleProvider,
)
from server.core.storage.agent_traces import RawHttpTraceStore


class _Client:
    def __init__(self, *, event_hooks=None, outcome: tuple[int, bytes] | str, **_kwargs) -> None:
        self.event_hooks = event_hooks or {}
        self.outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def post(self, url, *, json, headers, timeout):
        request = httpx.Request("POST", url, json=json, headers=headers)
        for hook in self.event_hooks.get("request", []):
            hook(request)
        if self.outcome == "timeout":
            raise httpx.TimeoutException("timeout", request=request)
        status, body = self.outcome
        response = httpx.Response(status, content=body, request=request)
        for hook in self.event_hooks.get("response", []):
            hook(response)
        return response


def _request() -> ExplanationRequest:
    return ExplanationRequest(
        critical_id="ply-17",
        language="en",
        prompt_version=3,
        payload={"position": {"fen_before": "private-fen"}},
        expected={"critical_id": "ply-17"},
        allowed_evidence_refs=["position.scores"],
        system_prompt="stable system",
        user_prompt="position_context: private position",
        input_hash="abcdef0123456789",
    )


class ExplanationProviderTraceTests(unittest.TestCase):
    def _provider(self, data_dir: str) -> tuple[OpenAICompatibleProvider, RawHttpTraceStore]:
        store = RawHttpTraceStore(data_dir)
        return (
            OpenAICompatibleProvider(
                base_url="https://example.test/v1",
                model="test-model",
                api_key="must-not-be-written",
                raw_trace_store=store,
            ),
            store,
        )

    @staticmethod
    def _trace_files(data_dir: str) -> list[Path]:
        root = Path(data_dir) / "agent" / "traces"
        directories = list(root.iterdir())
        if not directories:
            return []
        return sorted(directories[0].iterdir())

    def test_success_and_http_error_save_raw_request_and_response(self) -> None:
        for status, body in (
            (200, b'{"choices":[{"message":{"content":"model output"}}]}'),
            (429, b'{"error":"rate limited"}'),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory(
                prefix="explanation-trace-"
            ) as data_dir:
                provider, _store = self._provider(data_dir)
                with patch(
                    "server.core.explanation.providers.httpx.Client",
                    side_effect=lambda **kwargs: _Client(
                        outcome=(status, body), **kwargs
                    ),
                ):
                    if status == 200:
                        self.assertEqual("model output", provider.explain_position(_request()).text)
                    else:
                        with self.assertRaises(ExplanationProviderError):
                            provider.explain_position(_request())

                files = self._trace_files(data_dir)
                self.assertEqual(["001-request.json", "001-response.json"], [p.name for p in files])
                self.assertEqual(body, files[1].read_bytes())
                self.assertNotIn(b"must-not-be-written", files[0].read_bytes())
                self.assertIn("explanation-ply-17-abcdef012345", files[0].parent.name)

    def test_authentication_error_has_safe_metadata(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as data_dir:
                provider, _ = self._provider(data_dir)
                with patch(
                    "server.core.explanation.providers.httpx.Client",
                    side_effect=lambda **kwargs: _Client(
                        outcome=(status, b'{"code":"INVALID_API_KEY","message":"private-secret"}'),
                        **kwargs,
                    ),
                ), self.assertRaises(ExplanationProviderError) as raised:
                    provider.explain_position(_request())
                self.assertEqual("authentication_failed", raised.exception.reason)
                self.assertEqual(status, raised.exception.http_status)
                self.assertNotIn("private-secret", str(raised.exception))
                self.assertIn("CHESS_EXPLANATION_API_KEY", str(raised.exception))

    def test_debug_logs_lifecycle_usage_and_trace_without_body(self) -> None:
        body = b'{"choices":[{"message":{"content":"private-model-output"}}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12,"private":"secret"}}'
        for debug in (False, True):
            with self.subTest(debug=debug), tempfile.TemporaryDirectory() as data_dir:
                provider, _ = self._provider(data_dir)
                with patch.object(config, "DEBUG", debug), patch(
                    "server.core.explanation.providers.httpx.Client",
                    side_effect=lambda **kwargs: _Client(outcome=(200, body), **kwargs),
                ), patch("server.core.explanation.providers.logger") as logger:
                    provider.explain_position(_request())
                if not debug:
                    logger.debug.assert_not_called()
                    continue
                text = str(logger.debug.call_args_list)
                for event in ("explanation_model_started", "explanation_model_response",
                              "explanation_model_usage", "explanation_trace_saved"):
                    self.assertIn(event, text)
                self.assertIn("prompt_tokens", text)
                for secret in ("private-model-output", "private position", "must-not-be-written", "secret"):
                    self.assertNotIn(secret, text)

    def test_invalid_and_http_error_bodies_are_not_echoed(self) -> None:
        for status, body, reason in (
            (500, b'private-secret', "http_error"),
            (200, b'private-secret', "invalid_response"),
            (200, b'{"choices":[{"message":{"content":""}}]}', "invalid_response"),
        ):
            with self.subTest(status=status, body=body), tempfile.TemporaryDirectory() as data_dir:
                provider, _ = self._provider(data_dir)
                with patch("server.core.explanation.providers.httpx.Client",
                           side_effect=lambda **kwargs: _Client(outcome=(status, body), **kwargs)), self.assertRaises(ExplanationProviderError) as raised:
                    provider.explain_position(_request())
                self.assertEqual(reason, raised.exception.reason)
                self.assertNotIn("private-secret", str(raised.exception))

    def test_timeout_keeps_request_and_trace_failure_does_not_change_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="explanation-timeout-") as data_dir:
            provider, _store = self._provider(data_dir)
            with patch(
                "server.core.explanation.providers.httpx.Client",
                side_effect=lambda **kwargs: _Client(outcome="timeout", **kwargs),
            ):
                with self.assertRaises(ExplanationProviderError) as raised:
                    provider.explain_position(_request())
            self.assertIn("timed out", str(raised.exception))
            self.assertEqual(["001-request.json"], [p.name for p in self._trace_files(data_dir)])

        success = b'{"choices":[{"message":{"content":"still works"}}]}'
        with tempfile.TemporaryDirectory(prefix="explanation-trace-failure-") as data_dir:
            provider, store = self._provider(data_dir)
            with patch.object(store, "_write_private", side_effect=PermissionError), patch(
                "server.core.explanation.providers.httpx.Client",
                side_effect=lambda **kwargs: _Client(outcome=(200, success), **kwargs),
            ), self.assertLogs("chesscoach.agent", level="WARNING"):
                response = provider.explain_position(_request())
            self.assertEqual("still works", response.text)


if __name__ == "__main__":
    unittest.main()

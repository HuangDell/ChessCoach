from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.core.agent.policy import POLICY_VERSION
from server.core.agent.runtime import AgentRuntimeFailure, UnavailableAgentRuntime
from server.core.agent.runtime_openai import AGENTS_SDK_VERSION, create_openai_runtime
from server.core.storage.agent_compatibility import (
    AgentCompatibilityStore,
    endpoint_fingerprint,
)
from server.core.storage.agent_runs import RESPONSE_SCHEMA_VERSION


BASE_URL = "http://127.0.0.1:9900/v1"


class AgentCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-compatibility-")
        self.store = AgentCompatibilityStore(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _certificate(self, **changes):
        values = {
            "base_url": BASE_URL,
            "model": "custom-model",
            "sdk_version": AGENTS_SDK_VERSION,
            "policy_version": POLICY_VERSION,
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
            "gate_cases": {"responses_tools": True, "structured_output": True},
        }
        values.update(changes)
        return self.store.certificate(**values)

    def _runtime(self, *, base_url: str = BASE_URL, model: str = "custom-model"):
        return create_openai_runtime(
            enabled=True,
            model=model,
            base_url=base_url,
            custom_api_key="secret",
            openai_api_key="",
            domain_tools_factory=lambda _request: object(),
            session_provider=lambda _session_id: object(),
            data_dir=self.temporary.name,
        )

    def test_custom_runtime_fails_closed_for_missing_corrupt_and_mismatched_certificate(self) -> None:
        for setup in (
            lambda: None,
            lambda: self.store.path.parent.mkdir(parents=True) or self.store.path.write_text(
                "not json", encoding="utf-8"
            ),
            lambda: self.store.save(self._certificate(model="another-model")),
        ):
            with self.subTest(setup=setup):
                if self.store.path.exists():
                    self.store.path.unlink()
                setup()
                runtime = self._runtime()
                self.assertIsInstance(runtime, UnavailableAgentRuntime)
                self.assertEqual(
                    "agent_endpoint_incompatible", runtime.availability.error_code
                )
                with self.assertRaises(AgentRuntimeFailure) as raised:
                    import asyncio

                    asyncio.run(runtime.run(object()))  # type: ignore[arg-type]
                self.assertEqual("agent_endpoint_incompatible", raised.exception.error.code)

    def test_certificate_binds_versions_model_and_endpoint_without_storing_url(self) -> None:
        self.store.save(self._certificate())
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(endpoint_fingerprint(BASE_URL), payload["endpoint_sha256"])
        self.assertNotIn(BASE_URL, self.store.path.read_text(encoding="utf-8"))
        self.assertTrue(self.store.is_compatible(
            base_url=BASE_URL,
            model="custom-model",
            sdk_version=AGENTS_SDK_VERSION,
            policy_version=POLICY_VERSION,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
        ))
        self.assertFalse(self.store.is_compatible(
            base_url=BASE_URL + "/changed",
            model="custom-model",
            sdk_version=AGENTS_SDK_VERSION,
            policy_version=POLICY_VERSION,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
        ))

    def test_unknown_certificate_schema_fails_closed(self) -> None:
        certificate = self._certificate().model_dump(mode="json")
        certificate["schema_version"] = 2
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text(json.dumps(certificate), encoding="utf-8")

        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.is_compatible(
            base_url=BASE_URL,
            model="custom-model",
            sdk_version=AGENTS_SDK_VERSION,
            policy_version=POLICY_VERSION,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
        ))

    def test_failing_gate_cannot_be_certified(self) -> None:
        certificate = self._certificate(
            gate_cases={"responses_tools": True, "structured_output": False}
        )
        self.assertFalse(certificate.all_passed)
        with self.assertRaises(ValueError):
            self.store.save(certificate)

    def test_matching_certificate_reaches_locked_responses_adapter(self) -> None:
        self.store.save(self._certificate())
        with patch(
            "server.core.agent.runtime_openai.OpenAIAgentsRuntime",
            return_value="runtime",
        ) as constructor:
            self.assertEqual("runtime", self._runtime())
        self.assertEqual("custom_responses", constructor.call_args.kwargs["endpoint_type"])
        self.assertEqual(BASE_URL, constructor.call_args.kwargs["base_url"])


if __name__ == "__main__":
    unittest.main()

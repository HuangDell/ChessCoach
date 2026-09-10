import unittest
from unittest.mock import patch

from server.core.agent.runtime_openai import create_openai_runtime


BASE_URL = "http://127.0.0.1:9900/v1"


class AgentCustomRuntimeTests(unittest.TestCase):
    def _runtime(self):
        return create_openai_runtime(
            enabled=True,
            model="custom-model",
            base_url=BASE_URL,
            custom_api_key="secret",
            openai_api_key="",
            domain_tools_factory=lambda _request: object(),
            session_provider=lambda _session_id: object(),
        )

    def test_custom_runtime_reaches_responses_adapter_without_local_certificate(self) -> None:
        with patch(
            "server.core.agent.runtime_openai.OpenAIAgentsRuntime",
            return_value="runtime",
        ) as constructor:
            self.assertEqual("runtime", self._runtime())
        self.assertEqual("custom_responses", constructor.call_args.kwargs["endpoint_type"])
        self.assertEqual(BASE_URL, constructor.call_args.kwargs["base_url"])


if __name__ == "__main__":
    unittest.main()

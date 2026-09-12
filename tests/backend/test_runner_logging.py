from __future__ import annotations

import unittest
from unittest.mock import patch

from server.web.runner import _logging_config


class RunnerLoggingTests(unittest.TestCase):
    def test_console_format_has_second_precision_timestamp_and_debug_level(self) -> None:
        with patch("server.web.runner.config.AGENT_DEBUG", True):
            value = _logging_config()

        self.assertIn("%(asctime)s", value["formatters"]["default"]["fmt"])
        self.assertEqual("%Y-%m-%dT%H:%M:%S%z", value["formatters"]["default"]["datefmt"])
        self.assertEqual("DEBUG", value["loggers"]["chesscoach"]["level"])


if __name__ == "__main__":
    unittest.main()

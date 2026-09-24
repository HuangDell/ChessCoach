from __future__ import annotations

import unittest
from unittest.mock import patch

from server.web.runner import _logging_config, main


class RunnerLoggingTests(unittest.TestCase):
    def test_console_format_has_second_precision_timestamp_and_debug_level(self) -> None:
        with patch("server.web.runner.config.DEBUG", True):
            value = _logging_config()

        self.assertIn("%(asctime)s", value["formatters"]["default"]["fmt"])
        self.assertEqual("%Y-%m-%dT%H:%M:%S%z", value["formatters"]["default"]["datefmt"])
        self.assertEqual("DEBUG", value["loggers"]["chesscoach"]["level"])

    def test_debug_controls_access_logs(self) -> None:
        for enabled in (False, True):
            with self.subTest(debug=enabled), patch("server.web.runner.config.DEBUG", enabled), patch(
                "server.web.runner.config.WEB_OPEN", False
            ), patch("server.web.runner.settings.apply_saved"), patch(
                "server.web.runner.create_app"
            ), patch("server.web.runner.logging.config.dictConfig"), patch(
                "server.web.runner.uvicorn.run"
            ) as run, patch("server.web.runner.engine.shutdown"):
                self.assertEqual(0, main())
                self.assertEqual(enabled, run.call_args.kwargs["access_log"])
                self.assertEqual("DEBUG" if enabled else "INFO",
                                 run.call_args.kwargs["log_config"]["loggers"]["chesscoach"]["level"])


if __name__ == "__main__":
    unittest.main()

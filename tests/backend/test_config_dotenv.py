from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[2]


class DotenvConfigTests(unittest.TestCase):
    def _debug_value(self, debug: str | None, legacy: str | None = None) -> bool:
        with tempfile.TemporaryDirectory(prefix="chesscoach-config-") as directory:
            project_root = Path(directory) / "project"
            server_dir = project_root / "server"
            server_dir.mkdir(parents=True)
            (server_dir / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy2(ROOT / "server" / "config.py", server_dir / "config.py")
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root)
            for key, value in (
                ("DEBUG", debug),
                ("CHESS_AGENT_DEBUG", legacy),
                ("CHESS_AGENT_RAW_TRACE", legacy),
            ):
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
            result = subprocess.run(
                [sys.executable, "-c", "from server import config; print(int(config.DEBUG))"],
                cwd=directory,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() == "1"

    def test_debug_is_the_only_switch(self) -> None:
        self.assertFalse(self._debug_value(None))
        self.assertFalse(self._debug_value("0"))
        self.assertTrue(self._debug_value("1"))
        self.assertFalse(self._debug_value("true"))
        self.assertFalse(self._debug_value(None, "1"))
        self.assertTrue(self._debug_value("1", "0"))

    def test_repo_dotenv_loads_without_overriding_process_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="chesscoach-dotenv-") as directory:
            project_root = Path(directory) / "project"
            server_dir = project_root / "server"
            working_dir = Path(directory) / "working"
            server_dir.mkdir(parents=True)
            working_dir.mkdir()
            (server_dir / "__init__.py").write_text("", encoding="utf-8")
            shutil.copy2(ROOT / "server" / "config.py", server_dir / "config.py")
            (project_root / ".env").write_text(
                "CHESS_WEB_PORT=9123\nCHESS_WEB_OPEN=0\nDEBUG=1\n",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project_root)
            environment["CHESS_WEB_PORT"] = "9456"
            environment.pop("CHESS_WEB_OPEN", None)
            environment.pop("DEBUG", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json; from server import config; "
                        "print(json.dumps({'port': config.WEB_PORT, 'open': config.WEB_OPEN, "
                        "'raw_trace': config.DEBUG}))"
                    ),
                ],
                cwd=working_dir,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                {"port": 9456, "open": False, "raw_trace": True},
                json.loads(result.stdout),
            )


if __name__ == "__main__":
    unittest.main()

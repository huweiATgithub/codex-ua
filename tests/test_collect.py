import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.collect import child_environment, initialize_user_agent


class CollectorTests(unittest.TestCase):
    def test_measurement_environment_excludes_host_identity_and_credentials(self):
        inherited = {
            "PATH": "/usr/bin",
            "SystemRoot": "C:\\Windows",
            "HOME": "/real/home",
            "CODEX_HOME": "/real/codex",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "another-client",
            "CODEX_ACCESS_TOKEN": "private-token",
            "OPENAI_API_KEY": "private-key",
            "OPENAI_IDENTITY_TOKEN_FILE": "/real/identity",
            "HTTPS_PROXY": "http://external-proxy.invalid",
            "TERM_PROGRAM": "different-terminal",
            "WT_SESSION": "windows-terminal",
            "TMUX": "/tmux/session",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, inherited, clear=True):
            directory = Path(temporary)
            env = child_environment(directory)
            self.assertEqual(env["PATH"], inherited["PATH"])
            self.assertEqual(env["SystemRoot"], inherited["SystemRoot"])
            self.assertEqual(env["TERM"], "xterm-256color")
            self.assertEqual(Path(env["CODEX_HOME"]), directory / "codex-home")
            self.assertEqual(Path(env["HOME"]), directory / "home")
            self.assertTrue((Path(env["CODEX_HOME"]) / "config.toml").is_file())
            for name in inherited.keys() - {"PATH", "SystemRoot", "HOME", "CODEX_HOME"}:
                self.assertNotIn(name, env)

    def test_initialize_ignores_notifications_and_unrelated_response_ids(self):
        messages = [
            {"method": "notice", "params": {}},
            {"id": 42, "result": {"userAgent": "wrong-response"}},
            {"id": 1, "result": {"userAgent": "exact measured value"}},
        ]
        process = Mock()
        process.stdout = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")
        process.stdin = Mock(closed=False)
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as temporary, patch("scripts.collect.subprocess.Popen", return_value=process):
            actual = initialize_user_agent(Path("codex"), "0.156.1", "codex-tui", Path(temporary) / "probe")
        self.assertEqual(actual, "exact measured value")
        sent = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual(sent[0]["params"]["clientInfo"], {"name": "codex-tui", "version": "0.156.1"})
        self.assertEqual(sent[-1], {"method": "initialized"})

    def test_initialize_error_is_not_published_as_an_observation(self):
        process = Mock()
        process.stdout = io.StringIO('{"id":1,"error":{"code":-32602,"message":"unsupported client"}}\n')
        process.stdin = Mock(closed=False)
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as temporary, patch("scripts.collect.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "unsupported client"):
                initialize_user_agent(Path("codex"), "0.156.1", "codex-tui", Path(temporary) / "probe")


if __name__ == "__main__":
    unittest.main()

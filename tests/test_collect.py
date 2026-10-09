import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import collect, matrix
from scripts.collect import capture_client, child_environment, native_platform


class CollectorTests(unittest.TestCase):
    def test_native_linux_platform_uses_distribution_and_architecture(self):
        for distro in ("ubuntu", "debian", "fedora", "alpine"):
            for machine, architecture in (("x86_64", "x64"), ("aarch64", "arm64")):
                with self.subTest(distro=distro, machine=machine), \
                     patch("scripts.collect.platform.system", return_value="Linux"), \
                     patch("scripts.collect.platform.machine", return_value=machine), \
                     patch("scripts.collect.platform.freedesktop_os_release", return_value={"ID": distro}):
                    self.assertEqual(native_platform(), f"linux-{distro}-{architecture}")

    def test_wrong_distribution_stops_before_downloading_or_running(self):
        with patch("scripts.collect.native_platform", return_value="linux-ubuntu-x64"), \
             patch("scripts.collect.download_binary") as download, \
             patch("scripts.collect.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "requested linux-debian-x64"):
                collect.collect("0.156.1", "linux-debian-x64")
            download.assert_not_called()
            run.assert_not_called()

    def test_distribution_targets_match_publication_and_reuse_official_binaries(self):
        self.assertEqual(collect.TARGETS, matrix.TARGETS)
        self.assertEqual(len(collect.TARGETS), 12)
        for architecture in ("x64", "arm64"):
            urls = {collect.source_url("0.156.1", f"linux-{distro}-{architecture}")
                    for distro in ("ubuntu", "debian", "fedora", "alpine")}
            self.assertEqual(len(urls), 1)

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

    def fake_client(self, directory, headers=None):
        binary = directory / "codex"
        fixture = Path(__file__).parent / "fixtures" / "codex_client.py"
        binary.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        binary.chmod(0o755)
        if headers is not None:
            (directory / "headers.json").write_text(json.dumps(headers), encoding="utf-8")
        return binary

    @unittest.skipUnless(os.name == "posix", "uses a native Unix test client")
    def test_native_clients_capture_headers_without_supplied_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = collect.collect("0.156.1", native_platform(), self.fake_client(directory))
        self.assertEqual(result["schema_version"], 2)
        for mode, identity in (("CLI", "codex-tui"), ("Exec", "codex_exec")):
            ua = f"{identity}/0.156.1 (Measured OS 7; x86_64) xterm-256color ({identity}; 0.156.1)"
            self.assertEqual(result["clients"][mode], {
                "user_agent": ua, "method": "http-capture",
                "http_capture": {"user_agent": ua, "originator": identity},
            })

    @unittest.skipUnless(os.name == "posix", "uses a native Unix test client")
    def test_tui_process_is_reaped_after_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            capture_client(self.fake_client(directory), "CLI", directory / "tui")
            pid = int((directory / "tui" / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @unittest.skipUnless(os.name == "posix", "uses a native Unix test client")
    def test_tui_timeout_reaps_process_and_rejects_missing_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = self.fake_client(directory)
            (directory / "hold-request").touch()
            with patch("scripts.collect.PROCESS_TIMEOUT", 2):
                with self.assertRaisesRegex(RuntimeError, "TUI timed out"):
                    capture_client(binary, "CLI", directory / "tui")
            pid = int((directory / "tui" / "pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    @unittest.skipUnless(os.name == "posix", "uses a native Unix test client")
    def test_missing_user_agent_and_wrong_originator_are_rejected(self):
        for headers, message in (
            ({"User-Agent": "", "originator": "codex_exec"}, "no User-Agent"),
            ({"User-Agent": "measured UA", "originator": "another-client"}, "unexpected Exec originator"),
        ):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                with self.assertRaisesRegex(RuntimeError, message):
                    capture_client(self.fake_client(directory, headers), "Exec", directory / "capture")

    @unittest.skipUnless(os.name == "posix", "uses a native Unix test client")
    def test_repeated_requests_require_identical_captured_headers(self):
        first = {"User-Agent": "measured UA", "originator": "codex_exec"}
        for second in (first, {**first, "User-Agent": "different UA"}):
            with self.subTest(second=second), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                binary = self.fake_client(directory, [first, second])
                if second == first:
                    self.assertEqual(capture_client(binary, "Exec", directory / "capture"), {
                        "user_agent": "measured UA", "originator": "codex_exec",
                    })
                else:
                    with self.assertRaisesRegex(RuntimeError, "inconsistent headers"):
                        capture_client(binary, "Exec", directory / "capture")


if __name__ == "__main__":
    unittest.main()

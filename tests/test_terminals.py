import copy
import io
import json
import os
from pathlib import Path
import plistlib
import tempfile
import sys
import tarfile
import unittest
import zipfile
from unittest.mock import patch

from scripts import terminals


def releases():
    return {
        "WindowsTerminal": {
            "version": "1.25.2733.0", "source_url": "https://github.com/microsoft/terminal/releases/tag/v1.25.2733.0",
            "assets": {f"windows-{arch}": f"https://github.com/microsoft/terminal/releases/download/v1.25.2733.0/Microsoft.WindowsTerminal_1.25.2733.0_{arch}.zip"
                       for arch in ("x64", "arm64")},
        },
        "herdr": {
            "version": "0.9.3", "source_url": "https://github.com/herdrdev/herdr/releases/tag/v0.9.3",
            "assets": {f"{family}-{arch}": f"https://github.com/herdrdev/herdr/releases/download/v0.9.3/herdr-{family}-{native}" + (".zip" if family == "windows" else "")
                       for family in ("linux", "macos", "windows") for arch, native in (("x64", "x86_64"), ("arm64", "aarch64"))
                       if not (family == "windows" and arch == "arm64")},
        },
        "vscode": {
            "version": "1.141.0", "source_url": "https://code.visualstudio.com/updates/v1_141",
            "assets": {f"{family}-{arch}": f"https://update.code.visualstudio.com/1.141.0/{package}/stable"
                       for family, arch, package in (("linux", "x64", "linux-x64"), ("linux", "arm64", "linux-arm64"),
                           ("macos", "x64", "darwin"), ("macos", "arm64", "darwin-arm64"),
                           ("windows", "x64", "win32-x64-archive"), ("windows", "arm64", "win32-arm64-archive"))},
        },
    }


class TerminalTests(unittest.TestCase):
    def test_macos_install_uses_the_bundle_executable_and_preserves_its_path(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Visual Studio Code.app/Contents/Info.plist", plistlib.dumps({"CFBundleExecutable": "Code"}))
            bundle.writestr("Visual Studio Code.app/Contents/MacOS/Code", b"native application")
            bundle.writestr("Visual Studio Code.app/Contents/Resources/app/bin/code", b"CLI wrapper")
        archive.seek(0)
        with tempfile.TemporaryDirectory() as temporary, patch("scripts.terminals.sys.platform", "darwin"), \
                patch("scripts.terminals.urllib.request.urlopen", return_value=archive), \
                patch("scripts.terminals.subprocess.run") as extract:
            def ditto(command, **kwargs):
                with zipfile.ZipFile(command[2]) as source:
                    source.extractall(command[3])
            extract.side_effect = ditto
            root = Path(temporary) / "app"
            selected = terminals.parse_releases(releases())["vscode"]
            binary = terminals.install("vscode", selected, "macos-arm64", root)
            self.assertEqual(binary, root / "Visual Studio Code.app" / "Contents" / "MacOS" / "Code")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux VS Code archive layout")
    def test_install_selects_the_application_instead_of_cli_or_bash_completion(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            for name in ("VSCode-linux-x64/code", "VSCode-linux-x64/bin/code", "VSCode-linux-x64/resources/completions/bash/code"):
                contents = b"native application or supporting file"
                member = tarfile.TarInfo(name)
                member.size = len(contents)
                member.mode = 0o755
                bundle.addfile(member, io.BytesIO(contents))
        archive.seek(0)
        with tempfile.TemporaryDirectory() as temporary, patch("scripts.terminals.urllib.request.urlopen", return_value=archive):
            root = Path(temporary) / "app"
            selected = terminals.parse_releases(releases())["vscode"]
            binary = terminals.install("vscode", selected, "linux-ubuntu-x64", root)
            self.assertEqual(binary, root / "VSCode-linux-x64" / "code")

    def test_release_snapshot_binds_assets_to_selected_official_version(self):
        original = releases()
        parsed = terminals.parse_releases(original)
        self.assertEqual({name: value.to_dict() for name, value in parsed.items()}, original)
        for source in ("https://example.com/herdr", original["herdr"]["assets"]["linux-x64"].replace("v0.9.3", "v0.9.2"),
                       original["herdr"]["assets"]["linux-arm64"]):
            changed = copy.deepcopy(original)
            changed["herdr"]["assets"]["linux-x64"] = source
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "selected official stable release"):
                terminals.parse_releases(changed)
        original["herdr"]["version"] = "0.10.0-alpha.1"
        with self.assertRaisesRegex(ValueError, "stable release version"):
            terminals.parse_releases(original)

    def test_supported_native_combinations_and_explicit_unsupported_reasons(self):
        parsed = terminals.parse_releases(releases())
        for profile, platform, container, image, supported in (
            ("WindowsTerminal", "windows-arm64", None, "win11-arm64", True),
            ("WindowsTerminal", "linux-ubuntu-x64", None, "ubuntu24", False),
            ("vscode", "linux-ubuntu-arm64", None, "ubuntu24", True),
            ("vscode", "linux-debian-x64", "debian:13-slim", "ubuntu24", False),
            ("vscode", "windows-x64", None, "win25", False),
            ("vscode", "windows-arm64", None, "win11-arm64", True),
            ("herdr", "linux-alpine-arm64", "alpine:3.24", "ubuntu24", True),
            ("herdr", "windows-arm64", None, "win11-arm64", False),
        ):
            reason = terminals.unsupported_reason(profile, platform, parsed[profile], container, image)
            with self.subTest(profile=profile, platform=platform):
                self.assertEqual(reason is None, supported)
                if not supported:
                    self.assertIsInstance(reason, str)

    def test_new_official_architecture_asset_enables_herdr_without_emulation(self):
        value = releases()
        value["herdr"]["assets"]["windows-arm64"] = "https://github.com/herdrdev/herdr/releases/download/v0.9.3/herdr-windows-aarch64.zip"
        parsed = terminals.parse_releases(value)
        self.assertIsNone(terminals.unsupported_reason("herdr", "windows-arm64", parsed["herdr"], None, "win11-arm64"))

    def test_unknown_platform_is_an_error_rather_than_unsupported(self):
        with self.assertRaisesRegex(ValueError, "unsupported collection platform"):
            terminals.native_target("freebsd-x64")

    def test_isolation_removes_credentials_and_host_terminal_identity_before_launch(self):
        inherited = {"PATH": "/usr/bin", "HOME": "/real/home", "CODEX_HOME": "/real/codex", "OPENAI_API_KEY": "private",
                     "TERM": "host-term", "TERM_PROGRAM": "host-terminal", "WT_SESSION": "host-session", "HERDR_ENV": "1",
                     "FUTURE_TERMINAL_SIGNAL": "host-signal"}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, inherited, clear=True):
            env = terminals.isolated_environment(Path(temporary))
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(Path(env["HOME"]), Path(temporary) / "home")
            for key in inherited.keys() - {"PATH", "HOME"}:
                self.assertNotIn(key, env)

    def test_pty_helper_preserves_future_terminal_signals_and_failure_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "probe.json"
            output = root / "result.json"
            config.write_text(json.dumps({"command": ["sampler", "--capture"], "cwd": str(root), "output": str(output), "herdr": None}))
            env = {"CODEX_UA_PROBE_CONFIG": str(config), "FUTURE_TERMINAL_SIGNAL": "real-application-value"}
            import runpy
            with patch.dict(os.environ, env, clear=True), patch("os.isatty", return_value=True), patch("subprocess.run") as run:
                run.return_value.returncode = 7
                def execute(*args, **kwargs):
                    self.assertEqual(os.environ["FUTURE_TERMINAL_SIGNAL"], "real-application-value")
                    self.assertNotIn("env", kwargs)
                    return run.return_value
                run.side_effect = execute
                with self.assertRaises(SystemExit):
                    runpy.run_path("scripts/probe.py", run_name="__main__")
            result = json.loads(output.read_text())
            self.assertEqual(result["exit_code"], 7)
            self.assertEqual(result["tty"], [True, True, True])

    def test_helper_rejects_pipes_before_running_the_sampling_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "probe.json"
            output = root / "result.json"
            config.write_text(json.dumps({"command": ["sampler"], "cwd": str(root), "output": str(output), "herdr": None}))
            import runpy
            with patch.dict(os.environ, {"CODEX_UA_PROBE_CONFIG": str(config)}), patch("os.isatty", return_value=False), patch("subprocess.run") as run:
                with self.assertRaises(SystemExit) as exit_status:
                    runpy.run_path("scripts/probe.py", run_name="__main__")
            self.assertEqual(exit_status.exception.code, 1)
            run.assert_not_called()
            self.assertIn("real terminal PTY", json.loads(output.read_text())["error"])

    def test_supported_launcher_failure_is_an_error_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, "-c", "import sys; print('native startup failed', file=sys.stderr); sys.exit(4)"]
            with self.assertRaisesRegex(RuntimeError, r"launcher failed \(4\)[\s\S]*native startup failed"):
                terminals.wait_probe("vscode", command, root, dict(os.environ), root / "result.json")


if __name__ == "__main__":
    unittest.main()

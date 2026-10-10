import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from scripts import matrix, wsl
from test_matrix import COMMIT, PLATFORMS, RUN_URL, VERSION
from test_profiles import profiled_record


class WSLTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for platform in PLATFORMS:
            record = profiled_record(platform)
            if platform.startswith("linux-ubuntu-"):
                profile = record["profiles"].pop("WindowsTerminal")
                supplement = {"schema_version": 4, "codex_version": VERSION, "platform": platform,
                              "terminal_releases": record["terminal_releases"], "profiles": {"WindowsTerminal": profile}}
                (self.root / ("wsl-" + platform + ".json")).write_text(json.dumps(supplement))
            (self.root / (platform + ".json")).write_text(json.dumps(record))

    def assemble(self):
        return matrix.assemble_matrix(VERSION, self.root, COMMIT, RUN_URL)

    def test_wsl_results_fill_both_ubuntu_profiles_without_changing_other_profiles(self):
        results = matrix.publication_matrices(self.assemble())
        for platform in PLATFORMS:
            self.assertEqual(results.run["platforms"][platform]["profiles"], profiled_record(platform)["profiles"])
        for architecture in ("x64", "arm64"):
            profile = results.matrix["platforms"]["linux-ubuntu-" + architecture]["profiles"]["WindowsTerminal"]
            self.assertEqual(set(profile), {"CLI", "Exec"})

    def test_missing_wsl_result_blocks_publication(self):
        (self.root / "wsl-linux-ubuntu-arm64.json").unlink()
        with self.assertRaisesRegex(matrix.MatrixError, "missing fields.*WindowsTerminal"):
            self.assemble()

    def test_wsl_version_architecture_release_and_duplicate_results_cannot_be_merged(self):
        path = self.root / "wsl-linux-ubuntu-x64.json"
        original = json.loads(path.read_text())
        for field, value in (("codex_version", "0.1.0"), ("platform", "linux-ubuntu-arm64"), ("schema_version", 3)):
            changed = {**original, field: value}
            path.write_text(json.dumps(changed))
            with self.subTest(field=field), self.assertRaisesRegex(matrix.MatrixError, "WSL version and platform"):
                self.assemble()
        changed = copy.deepcopy(original)
        changed["terminal_releases"]["WindowsTerminal"]["assets"].pop("windows-arm64")
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(matrix.MatrixError, "release snapshots differ"):
            self.assemble()
        path.write_text(json.dumps(original))
        record = profiled_record("linux-ubuntu-x64")
        (self.root / "linux-ubuntu-x64.json").write_text(json.dumps(record))
        with self.assertRaisesRegex(matrix.MatrixError, "duplicate or invalid"):
            self.assemble()

    def test_native_method_missing_wsl_context_and_wrong_guest_os_are_rejected(self):
        path = self.root / "wsl-linux-ubuntu-x64.json"
        original = json.loads(path.read_text())
        run = self.assemble()
        schema = json.loads(Path("schema/ua-matrix.run.schema.json").read_text())
        variants = []
        for field, value in (("launch_method", "windows-terminal"), ("tty", [True, False, True]),
                             ("terminal", {"TERM": "xterm-256color"})):
            changed = copy.deepcopy(original)
            changed["profiles"]["WindowsTerminal"][field] = value
            variants.append(changed)
        for field, value in (("machine", "aarch64"), ("system", "Windows")):
            changed = copy.deepcopy(original)
            changed["profiles"]["WindowsTerminal"]["runtime"]["os"][field] = value
            variants.append(changed)
        wrong_distribution = copy.deepcopy(original)
        wrong_distribution["profiles"]["WindowsTerminal"]["runtime"]["os"]["distribution"]["version_id"] = "22.04"
        variants.append(wrong_distribution)
        for changed in variants:
            path.write_text(json.dumps(changed))
            with self.subTest(changed=changed), self.assertRaises(matrix.MatrixError):
                self.assemble()
            invalid = copy.deepcopy(run)
            invalid["platforms"]["linux-ubuntu-x64"]["profiles"]["WindowsTerminal"] = changed["profiles"]["WindowsTerminal"]
            with self.subTest(changed=changed), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)

    def test_wrong_windows_architecture_stops_before_launch(self):
        with patch("scripts.wsl.collect.native_platform", return_value="windows-x64"), \
                patch("scripts.wsl.subprocess.check_output") as execute:
            with self.assertRaisesRegex(RuntimeError, "matching native Windows host"):
                wsl.collect_wsl(VERSION, "linux-ubuntu-arm64", {}, "Ubuntu-24.04")
            execute.assert_not_called()

    def test_guest_requires_real_pty_and_native_wsl_session_before_running_codex(self):
        for tty, environment in ((False, {"WT_SESSION": "session", "WSL_DISTRO_NAME": "Ubuntu-24.04"}),
                                 (True, {"WT_SESSION": "session"}), (True, {"WSL_DISTRO_NAME": "Ubuntu-24.04"})):
            with self.subTest(tty=tty, environment=environment), \
                    patch("scripts.wsl.collect.native_platform", return_value="linux-ubuntu-x64"), \
                    patch("scripts.wsl.os.isatty", return_value=tty), patch.dict(os.environ, environment, clear=True), \
                    patch("scripts.wsl.collect.capture_client") as capture:
                with self.assertRaisesRegex(RuntimeError, "real WSL session"):
                    wsl.guest_sample("linux-ubuntu-x64", self.root)
                capture.assert_not_called()

    def test_unsafe_guest_directory_is_rejected_before_preparation_or_cleanup(self):
        from test_terminals import releases
        with patch("scripts.wsl.collect.native_platform", return_value="windows-x64"), \
                patch.dict(os.environ, {"SystemRoot": "C:/Windows"}), \
                patch("scripts.wsl.subprocess.check_output", side_effect=["/workspace/scripts/wsl.py", "/"]) as execute:
            with self.assertRaisesRegex(RuntimeError, "private sampling directory"):
                wsl.collect_wsl(VERSION, "linux-ubuntu-x64", releases(), "Ubuntu-24.04")
            self.assertEqual(execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()

import copy
import json
from pathlib import Path
import tempfile
import unittest

import jsonschema

from scripts import matrix, terminals
from test_matrix import COMMIT, PLATFORMS, RUN_URL, VERSION, platform_record
from test_terminals import releases


def profiled_record(platform):
    record = platform_record(platform)
    record["schema_version"] = 3
    record["terminal_releases"] = releases()
    runner = record["runner"]
    if platform.startswith("linux-") and not platform.startswith("linux-ubuntu-"):
        runner["container_image"] = platform.split("-")[1] + ":sample"
    if platform == "windows-x64":
        runner["image"] = "win25"
    selected = terminals.parse_releases(record["terminal_releases"])
    record["profiles"] = {}
    for profile, release in selected.items():
        reason = terminals.unsupported_reason(profile, platform, release, runner["container_image"], runner["image"])
        if reason:
            record["profiles"][profile] = {"status": "unsupported", "reason": reason}
            continue
        token = profile if profile == "WindowsTerminal" else profile + "/" + release.version
        clients = copy.deepcopy(record["clients"])
        for client in clients.values():
            ua = client["user_agent"].replace("xterm-256color", token)
            client["user_agent"] = ua
            client["http_capture"]["user_agent"] = ua
        record["profiles"][profile] = {
            "status": "collected", "application": {"version": release.version, "source_url": release.assets[terminals.native_target(platform)]},
            "launch_method": terminals.LAUNCH_METHODS[profile], "terminal": {"TERM_PROGRAM": profile},
            "tty": [True, True, True], "clients": clients,
        }
    return record


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for platform in PLATFORMS:
            self.write(profiled_record(platform))

    def write(self, record):
        (self.root / (record["platform"] + ".json")).write_text(json.dumps(record))

    def assemble(self):
        return matrix.assemble_matrix(VERSION, self.root, COMMIT, RUN_URL)

    def test_complete_native_coverage_preserves_actual_headers_and_unsupported(self):
        results = matrix.publication_matrices(self.assemble())
        self.assertEqual(results.run["schema_version"], 3)
        self.assertEqual(results.matrix["schema_version"], 2)
        self.assertEqual(results.run["terminal_releases"], releases())
        for platform in PLATFORMS:
            expected = profiled_record(platform)["profiles"]
            self.assertEqual(results.run["platforms"][platform]["profiles"], expected)
            for profile, context in expected.items():
                compact = results.matrix["platforms"][platform]["profiles"][profile]
                if context["status"] == "unsupported":
                    self.assertIsNone(compact)
                else:
                    self.assertEqual(compact, {mode: context["clients"][mode]["http_capture"]["user_agent"] for mode in ("CLI", "Exec")})

    def test_missing_profile_or_client_and_unsupported_downgrade_block_assembly(self):
        original = profiled_record("linux-ubuntu-x64")
        variants = []
        missing = copy.deepcopy(original)
        del missing["profiles"]["vscode"]
        variants.append(missing)
        missing_client = copy.deepcopy(original)
        del missing_client["profiles"]["vscode"]["clients"]["CLI"]
        variants.append(missing_client)
        downgrade = copy.deepcopy(original)
        downgrade["profiles"]["vscode"] = {"status": "unsupported", "reason": "launch failed"}
        variants.append(downgrade)
        baseline = platform_record("linux-ubuntu-x64")
        variants.append(baseline)
        for record in variants:
            self.write(record)
            with self.subTest(record=record), self.assertRaises(matrix.MatrixError):
                self.assemble()

    def test_native_asset_method_tty_and_header_proof_are_required(self):
        original = profiled_record("linux-ubuntu-x64")
        for field, value in (
            ("application", {"version": "1.141.0", "source_url": releases()["vscode"]["assets"]["linux-arm64"]}),
            ("launch_method", "controlled-environment"),
            ("tty", [True, False, True]),
            ("tty", [1, 1, 1]),
            ("terminal", {"OPENAI_API_KEY": "unexpected"}),
        ):
            changed = copy.deepcopy(original)
            changed["profiles"]["vscode"][field] = value
            self.write(changed)
            with self.subTest(field=field, value=value), self.assertRaises(matrix.MatrixError):
                self.assemble()
        changed = copy.deepcopy(original)
        changed["profiles"]["herdr"]["clients"]["CLI"]["http_capture"]["user_agent"] = "different"
        self.write(changed)
        with self.assertRaisesRegex(matrix.MatrixError, "differs from recorded UA"):
            self.assemble()

    def test_every_platform_uses_the_same_stable_snapshot(self):
        changed = profiled_record("linux-ubuntu-x64")
        release = changed["terminal_releases"]["vscode"]
        release["version"] = "1.141.1"
        release["assets"] = {target: url.replace("1.141.0", "1.141.1") for target, url in release["assets"].items()}
        changed["profiles"]["vscode"]["application"] = {"version": "1.141.1", "source_url": release["assets"]["linux-x64"]}
        self.write(changed)
        with self.assertRaisesRegex(matrix.MatrixError, "different stable release snapshots"):
            self.assemble()

    def test_future_terminal_detection_is_preserved_without_reconstructing_the_ua(self):
        changed = profiled_record("linux-ubuntu-x64")
        client = changed["profiles"]["vscode"]["clients"]["CLI"]
        observed = client["user_agent"].replace("vscode/1.141.0", "future-terminal/2.0")
        client["user_agent"] = client["http_capture"]["user_agent"] = observed
        self.write(changed)
        result = matrix.publication_matrices(self.assemble())
        self.assertEqual(result.matrix["platforms"]["linux-ubuntu-x64"]["profiles"]["vscode"]["CLI"], observed)

    def test_profile_schemas_accept_complete_run_and_reject_missing_context(self):
        results = matrix.publication_matrices(self.assemble())
        for filename, value in (("ua-matrix", results.matrix), ("ua-matrix.run", results.run)):
            schema = json.loads(Path(f"schema/{filename}.schema.json").read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            validator = jsonschema.Draft202012Validator(schema)
            validator.validate(value)
            for platform in PLATFORMS:
                changed = copy.deepcopy(value)
                del changed["platforms"][platform]["profiles"]["herdr"]
                with self.subTest(filename=filename, platform=platform), self.assertRaises(jsonschema.ValidationError):
                    validator.validate(changed)

    def test_run_parser_rejects_hidden_metadata_and_returns_independent_context(self):
        original = self.assemble()
        parsed = matrix.parse_run_matrix(original)
        original["platforms"]["linux-ubuntu-x64"]["profiles"]["vscode"]["terminal"]["TERM_PROGRAM"] = "changed"
        self.assertEqual(parsed["platforms"]["linux-ubuntu-x64"]["profiles"]["vscode"]["terminal"]["TERM_PROGRAM"], "vscode")
        original["platforms"]["linux-ubuntu-x64"]["codex_version"] = "0.1.0"
        with self.assertRaisesRegex(matrix.MatrixError, "unexpected fields"):
            matrix.parse_run_matrix(original)

    def test_release_table_includes_every_native_profile_and_unsupported_reason(self):
        from scripts.releases import release_body
        results = matrix.publication_matrices(self.assemble())
        body = release_body(results)
        self.assertEqual(body.count("<code>"), 60)
        self.assertEqual(body.count("| unsupported |"), 18)
        self.assertIn("| windows-arm64 (WindowsTerminal) | CLI |", body)
        self.assertIn("| linux-debian-x64 (vscode) | unsupported |", body)


if __name__ == "__main__":
    unittest.main()

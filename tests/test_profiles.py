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
    record["schema_version"] = 4
    record["terminal_releases"] = releases()
    runner = record["runner"]
    if platform.startswith("linux-") and not platform.startswith("linux-ubuntu-"):
        runner["container_image"] = platform.split("-")[1] + ":sample"
    if platform == "windows-x64":
        runner["image"] = "win25"
    selected = terminals.parse_releases(record["terminal_releases"])
    record["profiles"] = {"xterm-256color": {
        "status": "collected", "application": None, "launch_method": "controlled-environment",
        "terminal": record.pop("terminal"), "tty": None, "clients": record.pop("clients"),
    }}
    for profile, release in selected.items():
        wsl = terminals.uses_wsl(profile, platform)
        reason = None if wsl else terminals.unsupported_reason(profile, platform, release, runner["container_image"], runner["image"])
        if reason:
            record["profiles"][profile] = {"status": "unsupported", "reason": reason}
            continue
        token = profile if profile == "WindowsTerminal" else profile + "/" + release.version
        clients = copy.deepcopy(record["profiles"]["xterm-256color"]["clients"])
        for client in clients.values():
            ua = client["user_agent"].replace("xterm-256color", token)
            client["user_agent"] = ua
            client["http_capture"]["user_agent"] = ua
        target = "windows-" + platform.rsplit("-", 1)[1] if wsl else terminals.native_target(platform)
        record["profiles"][profile] = {
            "status": "collected", "application": {"version": release.version, "source_url": release.assets[target]},
            "launch_method": "windows-terminal-wsl" if wsl else terminals.LAUNCH_METHODS[profile], "terminal": {"TERM_PROGRAM": profile},
            "tty": [True, True, True], "clients": clients,
        }
        if wsl:
            guest = copy.deepcopy(record["os"])
            guest["distribution"]["version_id"] = "24.04"
            record["profiles"][profile].update(
                terminal={"TERM": "xterm-256color", "WT_SESSION": "real-terminal-session", "WSL_DISTRO_NAME": "Ubuntu-24.04"},
                runtime={"os": guest, "runner": {**runner, "image": "win25" if platform.endswith("-x64") else "win11-arm64"}})
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
        self.assertEqual(results.run["schema_version"], 4)
        self.assertEqual(results.matrix["schema_version"], 3)
        self.assertEqual(results.run["terminal_releases"], releases())
        for platform in PLATFORMS:
            self.assertEqual(set(results.matrix["platforms"][platform]), {"profiles"})
            self.assertNotIn("clients", results.run["platforms"][platform])
            self.assertNotIn("terminal", results.run["platforms"][platform])
            self.assertEqual(set(results.matrix["platforms"][platform]["profiles"]), set(terminals.PROFILES))
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
        missing_xterm = copy.deepcopy(original)
        del missing_xterm["profiles"]["xterm-256color"]
        variants.append(missing_xterm)
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
        for profile in terminals.PROFILES:
            changed = profiled_record("linux-ubuntu-x64")
            client = changed["profiles"][profile]["clients"]["CLI"]
            token = profile if profile in ("xterm-256color", "WindowsTerminal") else profile + "/" + releases()[profile]["version"]
            observed = client["user_agent"].replace(token, "future-terminal/2.0")
            client["user_agent"] = client["http_capture"]["user_agent"] = observed
            self.write(changed)
            result = matrix.publication_matrices(self.assemble())
            with self.subTest(profile=profile):
                self.assertEqual(result.matrix["platforms"]["linux-ubuntu-x64"]["profiles"][profile]["CLI"], observed)

    def test_legacy_profile_layout_remains_readable_without_rewriting_published_assets(self):
        for platform in PLATFORMS:
            record = profiled_record(platform)
            record["schema_version"] = 3
            if terminals.uses_wsl("WindowsTerminal", platform):
                record["profiles"]["WindowsTerminal"] = {
                    "status": "unsupported", "reason": "Windows Terminal has no native release for this operating system."}
            xterm = record["profiles"].pop("xterm-256color")
            record.update(terminal=xterm["terminal"], clients=xterm["clients"])
            self.write(record)
        run = self.assemble()
        results = matrix.publication_matrices(run)
        self.assertEqual(results.run, run)
        self.assertEqual(results.run["schema_version"], 3)
        self.assertEqual(results.matrix["schema_version"], 2)
        self.assertEqual(results.matrix["platforms"]["linux-ubuntu-x64"]["CLI"],
                         profiled_record("linux-ubuntu-x64")["profiles"]["xterm-256color"]["clients"]["CLI"]["user_agent"])

    def test_profile_schemas_accept_complete_run_and_reject_missing_context(self):
        results = matrix.publication_matrices(self.assemble())
        for filename, value in (("ua-matrix", results.matrix), ("ua-matrix.run", results.run)):
            schema = json.loads(Path(f"schema/{filename}.schema.json").read_text())
            jsonschema.Draft202012Validator.check_schema(schema)
            validator = jsonschema.Draft202012Validator(schema)
            validator.validate(value)
            missing_wsl = copy.deepcopy(value)
            missing_wsl["platforms"]["linux-ubuntu-x64"]["profiles"]["WindowsTerminal"] = (
                None if filename == "ua-matrix" else {"status": "unsupported", "reason": "launch failed"})
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(missing_wsl)
            for platform in PLATFORMS:
                for profile in terminals.PROFILES:
                    changed = copy.deepcopy(value)
                    del changed["platforms"][platform]["profiles"][profile]
                    with self.subTest(filename=filename, platform=platform, profile=profile), self.assertRaises(jsonschema.ValidationError):
                        validator.validate(changed)
            legacy = copy.deepcopy(value)
            legacy["schema_version"] -= 1
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate(legacy)

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
        self.assertEqual(body.count("<code>"), 64)
        self.assertEqual(body.count("| unsupported |"), 16)
        self.assertIn("| Platform | Profile | Client | User-Agent |", body)
        self.assertIn("| windows-arm64 | WindowsTerminal | CLI |", body)
        self.assertIn("| linux-ubuntu-x64 | WindowsTerminal | CLI |", body)
        self.assertIn("| linux-ubuntu-arm64 | WindowsTerminal | Exec |", body)
        self.assertIn("| linux-debian-x64 | xterm-256color | CLI |", body)
        self.assertIn("| linux-debian-x64 | vscode | unsupported |", body)


if __name__ == "__main__":
    unittest.main()

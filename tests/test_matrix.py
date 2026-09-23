import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jsonschema

from scripts.matrix import MatrixError, assemble_matrix, parse_run_matrix, publication_matrices


VERSION = "0.155.1"
COMMIT = "a" * 40
RUN_URL = "https://github.com/huweiATgithub/codex-ua/actions/runs/123456789"
PLATFORMS = {
    "linux-ubuntu-x64": ("x86_64-unknown-linux-musl", "Linux", "x86_64"),
    "linux-ubuntu-arm64": ("aarch64-unknown-linux-musl", "Linux", "aarch64"),
    "linux-debian-x64": ("x86_64-unknown-linux-musl", "Linux", "x86_64"),
    "linux-debian-arm64": ("aarch64-unknown-linux-musl", "Linux", "aarch64"),
    "linux-fedora-x64": ("x86_64-unknown-linux-musl", "Linux", "x86_64"),
    "linux-fedora-arm64": ("aarch64-unknown-linux-musl", "Linux", "aarch64"),
    "linux-alpine-x64": ("x86_64-unknown-linux-musl", "Linux", "x86_64"),
    "linux-alpine-arm64": ("aarch64-unknown-linux-musl", "Linux", "aarch64"),
    "macos-x64": ("x86_64-apple-darwin", "Darwin", "x86_64"),
    "macos-arm64": ("aarch64-apple-darwin", "Darwin", "arm64"),
    "windows-x64": ("x86_64-pc-windows-msvc", "Windows", "AMD64"),
    "windows-arm64": ("aarch64-pc-windows-msvc", "Windows", "ARM64"),
}


def platform_record(platform):
    target, system, machine = PLATFORMS[platform]
    extension = ".exe.tar.gz" if system == "Windows" else ".tar.gz"
    clients = {}
    for mode, identity in (("CLI", "codex-tui"), ("Exec", "codex_exec")):
        ua = f"{identity}/{VERSION} ({system} 1.0; {machine}) xterm-256color ({identity}; {VERSION})"
        clients[mode] = {"user_agent": ua, "method": "app-server-initialize"}
        if mode == "Exec":
            clients[mode]["http_capture"] = {"user_agent": ua, "originator": "codex_exec"}
    return {
        "schema_version": 1,
        "codex_version": VERSION,
        "platform": platform,
        "target": target,
        "source_url": f"https://github.com/openai/codex/releases/download/rust-v{VERSION}/codex-{target}{extension}",
        "collected_at": "2026-09-23T12:34:56Z",
        "os": {
            "system": system, "release": "1.0", "version": "OS build 42", "machine": machine,
            "distribution": {"id": platform.split("-")[1], "version_id": "1.0", "pretty_name": "Sample Linux"}
            if system == "Linux" else None,
        },
        "runner": {"name": "Hosted Agent", "image": "sample-runner", "image_version": "20260923.1", "container_image": None},
        "terminal": {"TERM": "xterm-256color"},
        "clients": clients,
    }


class MatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.input_dir = Path(temporary.name)
        for platform in PLATFORMS:
            self.write(platform_record(platform))

    def write(self, record, filename=None):
        path = self.input_dir / (filename or f"{record['platform']}.json")
        path.write_text(json.dumps(record), encoding="utf-8")

    def assemble(self, **kwargs):
        options = dict(version=VERSION, input_dir=self.input_dir, collector_commit=COMMIT, run_url=RUN_URL)
        return assemble_matrix(**(options | kwargs))

    def test_complete_matrix_preserves_observations(self):
        result = self.assemble()
        self.assertEqual(result["codex_version"], VERSION)
        self.assertEqual(result["collector"], {"commit": COMMIT, "run_url": RUN_URL})
        self.assertEqual(result["upstream_release"], "https://github.com/openai/codex/releases/tag/rust-v0.155.1")
        self.assertEqual(set(result["platforms"]), set(PLATFORMS))
        for platform in PLATFORMS:
            expected = platform_record(platform)
            for key in ("schema_version", "codex_version", "platform"):
                expected.pop(key)
            self.assertEqual(result["platforms"][platform], expected)

    def test_missing_platform_cannot_publish(self):
        (self.input_dir / "windows-arm64.json").unlink()
        with self.assertRaisesRegex(MatrixError, "missing platforms: windows-arm64"):
            self.assemble()

    def test_duplicate_platform_is_not_silently_overwritten(self):
        self.write(platform_record("linux-ubuntu-x64"), "duplicate.json")
        with self.assertRaisesRegex(MatrixError, "duplicate platform: linux-ubuntu-x64"):
            self.assemble()

    def test_mixed_versions_cannot_publish(self):
        record = platform_record("linux-ubuntu-x64")
        record["codex_version"] = "0.154.0"
        self.write(record)
        with self.assertRaisesRegex(MatrixError, "differs from requested collection version"):
            self.assemble()

    def test_unstable_or_noncanonical_versions_are_rejected(self):
        for version in ("0.155.1-alpha.1", "v0.155.1", "00.155.1", "0.155", "0.155.1 "):
            with self.subTest(version=version), self.assertRaisesRegex(MatrixError, "stable x.y.z"):
                self.assemble(version=version)

    def test_artifact_must_match_official_version_and_target(self):
        original = platform_record("linux-ubuntu-x64")
        mutations = (
            ("target", "aarch64-unknown-linux-musl"),
            ("source_url", original["source_url"].replace("openai", "someone-else")),
            ("source_url", original["source_url"].replace(VERSION, "0.154.0")),
            ("source_url", original["source_url"].replace("x86_64", "aarch64")),
        )
        for key, value in mutations:
            record = copy.deepcopy(original)
            record[key] = value
            self.write(record)
            with self.subTest(key=key, value=value), self.assertRaisesRegex(MatrixError, key):
                self.assemble()

    def test_exec_capture_must_match_initialization(self):
        for key, value in (("user_agent", "different/0.155.1"), ("originator", "codex-tui")):
            record = platform_record("linux-ubuntu-x64")
            record["clients"]["Exec"]["http_capture"][key] = value
            self.write(record)
            with self.subTest(key=key), self.assertRaisesRegex(MatrixError, f"http_capture.{key}"):
                self.assemble()

    def test_client_identity_version_and_terminal_are_required(self):
        original = platform_record("linux-ubuntu-x64")
        ua = original["clients"]["CLI"]["user_agent"]
        for invalid_ua in (
            ua.replace("codex-tui", "ua-probe"),
            ua.replace("0.155.1", "0.154.0"),
            ua.replace("xterm-256color", "unknown"),
            ua.replace("Linux", "Linux\n"),
        ):
            record = copy.deepcopy(original)
            record["clients"]["CLI"]["user_agent"] = invalid_ua
            self.write(record)
            with self.subTest(ua=invalid_ua), self.assertRaisesRegex(MatrixError, "user_agent"):
                self.assemble()

    def test_invalid_environment_metadata_is_rejected(self):
        mutations = (
            ("collected_at", "2026-09-23T12:34:56"),
            ("collected_at", "2026-09-23T12:34:56+08:00"),
            ("collected_at", "2026-02-30T12:34:56Z"),
            ("terminal", {"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"}),
            ("runner", {"name": "Hosted", "image": "", "image_version": "2026"}),
            ("os", {"system": "Linux", "release": "1", "version": "1", "machine": None}),
        )
        for key, value in mutations:
            record = platform_record("linux-ubuntu-x64")
            record[key] = value
            self.write(record)
            with self.subTest(key=key, value=value), self.assertRaisesRegex(MatrixError, key):
                self.assemble()

    def test_environment_identity_must_match_platform_in_parser_and_schema(self):
        schema = json.loads(Path("schema/ua-matrix.run.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        original = self.assemble()
        for platform, key, value in (
            ("linux-debian-x64", "distribution", {"id": "ubuntu", "version_id": "24.04", "pretty_name": "Ubuntu"}),
            ("linux-debian-x64", "distribution", None),
            ("linux-alpine-arm64", "distribution", {"id": "alpine", "version_id": "", "pretty_name": "Alpine"}),
            ("macos-x64", "distribution", {"id": "debian", "version_id": "13", "pretty_name": "Debian"}),
            ("linux-fedora-arm64", "machine", "x86_64"),
            ("linux-fedora-x64", "system", "Darwin"),
        ):
            value_to_parse = copy.deepcopy(original)
            value_to_parse["platforms"][platform]["os"][key] = value
            with self.subTest(platform=platform, key=key, value=value):
                with self.assertRaises(MatrixError):
                    parse_run_matrix(value_to_parse)
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(value_to_parse)

    def test_unknown_platform_and_fields_are_rejected(self):
        record = platform_record("linux-ubuntu-x64")
        record["platform"] = "freebsd-x64"
        self.write(record, "linux-ubuntu-x64.json")
        with self.assertRaisesRegex(MatrixError, "unsupported platform"):
            self.assemble()
        record = platform_record("linux-ubuntu-x64")
        record["extra"] = "unversioned field"
        self.write(record)
        with self.assertRaisesRegex(MatrixError, "unexpected fields"):
            self.assemble()

    def test_duplicate_json_keys_are_rejected(self):
        path = self.input_dir / "linux-ubuntu-x64.json"
        path.write_text('{"schema_version": 1, "schema_version": 2}', encoding="utf-8")
        with self.assertRaisesRegex(MatrixError, "duplicate JSON key"):
            self.assemble()

    def test_invalid_collector_provenance_is_rejected(self):
        for options in ({"collector_commit": "main"}, {"run_url": "https://example.com/run/42"}):
            with self.subTest(options=options), self.assertRaises(MatrixError):
                self.assemble(**options)

    def run_cli(self, output_dir):
        return subprocess.run(
            [sys.executable, "scripts/matrix.py", "--version", VERSION,
             "--input-dir", str(self.input_dir), "--output-dir", str(output_dir),
             "--collector-commit", COMMIT, "--run-url", RUN_URL],
            text=True, capture_output=True, check=False,
        )

    def test_cli_writes_both_publication_assets(self):
        output_dir = self.input_dir / "output"
        result = self.run_cli(output_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({path.name for path in output_dir.iterdir()}, {"ua-matrix.json", "ua-matrix.run.json"})
        matrix = json.loads((output_dir / "ua-matrix.json").read_text(encoding="utf-8"))
        run = json.loads((output_dir / "ua-matrix.run.json").read_text(encoding="utf-8"))
        self.assertEqual(run, self.assemble())
        self.assertEqual(matrix, publication_matrices(run).matrix)

    def test_invalid_matrix_does_not_create_either_output_file(self):
        (self.input_dir / "macos-arm64.json").unlink()
        output_dir = self.input_dir / "output"
        result = self.run_cli(output_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing platforms: macos-arm64", result.stderr)
        self.assertFalse(output_dir.exists())

    def test_schemas_accept_complete_results_and_require_each_platform(self):
        results = publication_matrices(self.assemble())
        for filename, value in (("ua-matrix", results.matrix), ("ua-matrix.run", results.run)):
            schema = json.loads(Path(f"schema/{filename}.schema.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(schema)
            validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
            validator.validate(value)
            for platform in PLATFORMS:
                incomplete = copy.deepcopy(value)
                del incomplete["platforms"][platform]
                with self.subTest(schema=filename, platform=platform), self.assertRaises(jsonschema.ValidationError):
                    validator.validate(incomplete)

    def test_compact_schema_requires_exact_modes_and_rejects_metadata(self):
        schema = json.loads(Path("schema/ua-matrix.schema.json").read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator(schema)
        original = publication_matrices(self.assemble()).matrix
        mutations = []
        for mode in ("CLI", "Exec"):
            missing_mode = copy.deepcopy(original)
            del missing_mode["platforms"]["linux-ubuntu-x64"][mode]
            mutations.append(missing_mode)
        detailed = copy.deepcopy(original)
        detailed["collector"] = {"commit": COMMIT, "run_url": RUN_URL}
        mutations.append(detailed)
        extra_platform = copy.deepcopy(original)
        extra_platform["platforms"]["other"] = original["platforms"]["linux-ubuntu-x64"]
        mutations.append(extra_platform)
        extra_mode = copy.deepcopy(original)
        extra_mode["platforms"]["linux-ubuntu-x64"]["other"] = "UA"
        mutations.append(extra_mode)
        object_ua = copy.deepcopy(original)
        object_ua["platforms"]["linux-ubuntu-x64"]["Exec"] = {"user_agent": "UA"}
        mutations.append(object_ua)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(jsonschema.ValidationError):
                validator.validate(value)

    def test_publication_matrices_preserve_all_uas_without_metadata(self):
        original = self.assemble()
        results = publication_matrices(original)
        self.assertEqual(results.run, original)
        self.assertEqual(set(results.matrix), {"schema_version", "codex_version", "platforms"})
        self.assertEqual(results.matrix["schema_version"], 1)
        self.assertEqual(results.matrix["codex_version"], VERSION)
        self.assertEqual(set(results.matrix["platforms"]), set(PLATFORMS))
        for platform in PLATFORMS:
            clients = results.matrix["platforms"][platform]
            self.assertEqual(set(clients), {"CLI", "Exec"})
            for mode in ("CLI", "Exec"):
                self.assertEqual(clients[mode], original["platforms"][platform]["clients"][mode]["user_agent"])
        original["platforms"]["linux-ubuntu-x64"]["clients"]["Exec"]["user_agent"] = "tampered"
        self.assertNotEqual(results.run["platforms"]["linux-ubuntu-x64"]["clients"]["Exec"]["user_agent"], "tampered")
        self.assertNotEqual(results.matrix["platforms"]["linux-ubuntu-x64"]["Exec"], "tampered")

    def test_run_parser_returns_an_independent_normalized_matrix(self):
        original = self.assemble()
        parsed = parse_run_matrix(original)
        self.assertEqual(parsed, original)
        original["platforms"]["linux-ubuntu-x64"]["clients"]["Exec"]["http_capture"]["originator"] = "tampered"
        self.assertEqual(parsed["platforms"]["linux-ubuntu-x64"]["clients"]["Exec"]["http_capture"]["originator"], "codex_exec")

    def test_run_parser_and_publication_reject_incomplete_matrix_and_tampered_metadata(self):
        original = self.assemble()
        mutations = []
        incomplete = copy.deepcopy(original)
        del incomplete["platforms"]["linux-ubuntu-arm64"]
        mutations.append(incomplete)
        invalid_capture = copy.deepcopy(original)
        invalid_capture["platforms"]["linux-ubuntu-x64"]["clients"]["Exec"]["http_capture"]["user_agent"] = "tampered"
        mutations.append(invalid_capture)
        invalid_version = copy.deepcopy(original)
        invalid_version["codex_version"] = "0.154.0"
        invalid_version["upstream_release"] = "https://github.com/openai/codex/releases/tag/rust-v0.154.0"
        mutations.append(invalid_version)
        hidden_version = copy.deepcopy(original)
        hidden_version["platforms"]["linux-ubuntu-x64"]["codex_version"] = "0.154.0"
        mutations.append(hidden_version)
        for invalid in mutations:
            for parse in (parse_run_matrix, publication_matrices):
                with self.subTest(parser=parse.__name__, matrix=invalid), self.assertRaises(MatrixError):
                    parse(invalid)


if __name__ == "__main__":
    unittest.main()

import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import releases


REPOSITORY = "owner/catalog"
COMMIT = "a" * 40


def catalog_release(version, **updates):
    release = {
        "id": 1, "tag_name": f"v{version}", "draft": False, "prerelease": False,
        "published_at": "2026-09-23T00:00:00Z",
        "assets": [{"name": "ua-matrix.json", "state": "uploaded", "size": 123}],
    }
    release.update(updates)
    return release


def upstream_release(version, **updates):
    release = {
        "tag_name": f"rust-v{version}", "draft": False, "prerelease": False,
        "assets": [
            {"name": f"codex-{target}{'.exe' if platform.startswith('windows-') else ''}.tar.gz",
             "state": "uploaded", "size": 123}
            for platform, target in releases.TARGETS.items()
        ],
    }
    release.update(updates)
    return release


def source(name):
    value = (
        "let client = start_client(InProcessClientStartArgs {\n"
        "  config: Arc::new(config),\n"
        f'  client_name: "{name}".to_string(),\n'
        '  client_version: env!("CARGO_PKG_VERSION").to_string(),\n'
        "});"
    )
    return {"encoding": "base64", "content": base64.b64encode(value.encode()).decode()}


def matrix_payload():
    platforms = {}
    for platform, target in releases.TARGETS.items():
        system = {"linux": "Linux", "macos": "Darwin", "windows": "Windows"}[platform.split("-")[0]]
        machine = "x86_64" if platform.endswith("-x64") else "aarch64"
        clients = {}
        for mode, identity in (("interactive", "codex-tui"), ("exec", "codex_exec")):
            ua = f"{identity}/0.10.0 ({system} 1.0; {machine}) xterm-256color ({identity}; 0.10.0)"
            clients[mode] = {"user_agent": ua, "method": "app-server-initialize"}
            if mode == "exec":
                clients[mode]["http_capture"] = {"user_agent": ua, "originator": identity}
        extension = ".exe.tar.gz" if system == "Windows" else ".tar.gz"
        platforms[platform] = {
            "target": target,
            "source_url": f"https://github.com/openai/codex/releases/download/rust-v0.10.0/codex-{target}{extension}",
            "collected_at": "2026-09-23T12:34:56Z",
            "os": {"system": system, "release": "1.0", "version": "1", "machine": machine},
            "runner": {"name": "Hosted Agent", "image": "sample-runner", "image_version": "20260923.1"},
            "terminal": {"TERM": "xterm-256color"}, "clients": clients,
        }
    return {
        "schema_version": 1, "codex_version": "0.10.0",
        "upstream_release": "https://github.com/openai/codex/releases/tag/rust-v0.10.0",
        "collector": {"commit": COMMIT, "run_url": "https://github.com/owner/catalog/actions/runs/123"},
        "platforms": platforms,
    }


class DiscoveryTests(unittest.TestCase):
    def github(self, catalog=(), upstream=()):
        github = Mock(spec=releases.GitHub)
        github.releases.side_effect = lambda repository: iter(catalog if repository == REPOSITORY else upstream)
        github.api.side_effect = lambda endpoint: source("codex-tui" if "/tui/" in endpoint else "codex_exec")
        return github

    def test_bootstrap_uses_current_latest(self):
        github = self.github()
        profiles = github.api.side_effect
        github.api.side_effect = lambda endpoint: (
            upstream_release("0.156.1") if endpoint.endswith("/latest") else profiles(endpoint)
        )
        result = releases.discover(github, REPOSITORY)
        self.assertEqual(result["version"], "0.156.1")
        self.assertTrue(result["needed"])
        github.releases.assert_called_once_with(REPOSITORY)

    def test_catchup_selects_oldest_missing_with_numeric_order(self):
        github = self.github(
            [catalog_release("0.9.0")],
            [upstream_release("0.11.0"), upstream_release("0.12.0-alpha.1", prerelease=True),
             upstream_release("0.10.0"), upstream_release("0.9.0")],
        )
        self.assertEqual(releases.discover(github, REPOSITORY)["version"], "0.10.0")

    def test_stop_at_seed_boundary(self):
        github = self.github([catalog_release("0.9.0")])

        def upstream():
            yield upstream_release("0.10.0")
            yield upstream_release("0.9.0")
            self.fail("Discovery fetched old releases beyond the catalog boundary")

        github.releases.side_effect = lambda repository: (
            iter([catalog_release("0.9.0")]) if repository == REPOSITORY else upstream()
        )
        self.assertEqual(releases.discover(github, REPOSITORY)["version"], "0.10.0")

    def test_manual_backfill_does_not_lower_automatic_boundary(self):
        github = self.github(
            [catalog_release("0.7.0", published_at="2026-09-24T00:00:00Z"), catalog_release("0.9.0")],
            [upstream_release("0.10.0"), upstream_release("0.9.0"), upstream_release("0.8.0"), upstream_release("0.7.0")],
        )
        self.assertEqual(releases.discover(github, REPOSITORY)["version"], "0.10.0")

    def test_ignores_drafts_prereleases_and_other_tags(self):
        github = self.github(
            [catalog_release("0.9.0")],
            [upstream_release("0.10.0", draft=True), upstream_release("0.11.0", prerelease=True),
             upstream_release("0.12.0", tag_name="other-v0.12.0"), upstream_release("0.9.0")],
        )
        self.assertFalse(releases.discover(github, REPOSITORY)["needed"])
        github.api.assert_not_called()

    def test_manual_published_version_is_skipped(self):
        github = self.github([catalog_release("0.10.0")])
        self.assertFalse(releases.discover(github, REPOSITORY, "0.10.0")["needed"])
        github.by_tag.assert_not_called()

    def test_partial_published_catalog_is_an_error(self):
        for asset in ([], [{"name": "ua-matrix.json", "state": "uploaded", "size": 0}]):
            with self.subTest(asset=asset):
                github = self.github([catalog_release("0.10.0", assets=asset)])
                with self.assertRaisesRegex(releases.ReleaseError, "never overwritten"):
                    releases.discover(github, REPOSITORY)

    def test_missing_upstream_binary_defers_automatic_but_errors_manual(self):
        github = self.github()
        incomplete = upstream_release("0.10.0", assets=[])
        github.api.return_value = incomplete
        github.api.side_effect = None
        self.assertFalse(releases.discover(github, REPOSITORY)["needed"])
        github.by_tag.return_value = incomplete
        with self.assertRaisesRegex(releases.ReleaseError, "missing required binaries"):
            releases.discover(github, REPOSITORY, "0.10.0")

    def test_manual_requires_exact_stable_release(self):
        github = self.github()
        github.by_tag.return_value = upstream_release("0.10.0", prerelease=True)
        with self.assertRaisesRegex(releases.ReleaseError, "No published stable"):
            releases.discover(github, REPOSITORY, "0.10.0")
        with self.assertRaises(releases.MatrixError):
            releases.discover(github, REPOSITORY, "0.10.0; echo bad")

    def test_source_identity_drift_fails_closed(self):
        github = self.github()
        github.by_tag.return_value = upstream_release("0.10.0")
        github.api.side_effect = lambda endpoint: source("some-other-client")
        with self.assertRaisesRegex(releases.ReleaseError, "CLI profile changed"):
            releases.discover(github, REPOSITORY, "0.10.0")

    def test_profile_supports_named_start_arguments(self):
        github = self.github()
        github.by_tag.return_value = upstream_release("0.10.0")

        def named_arguments(endpoint):
            value = source("codex-tui" if "/tui/" in endpoint else "codex_exec")
            text = base64.b64decode(value["content"]).decode()
            text = text.replace("let client = start_client(", "let in_process_start_args = ")
            value["content"] = base64.b64encode(text.encode()).decode()
            return value

        github.api.side_effect = named_arguments
        self.assertTrue(releases.discover(github, REPOSITORY, "0.10.0")["needed"])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.asset = Path(directory.name) / "ua-matrix.json"
        self.asset.write_text(json.dumps(matrix_payload()))
        self.github = Mock(spec=releases.GitHub)
        self.github.releases.side_effect = lambda repository: iter([])
        self.github.by_tag.return_value = None
        self.api_calls = []
        self.github.api.side_effect = self.api
        self.github.command.side_effect = lambda argv: self.asset.read_bytes() if argv[0] == "api" else b""

    def api(self, endpoint, payload=None, method="GET"):
        self.api_calls.append((endpoint, copy.deepcopy(payload), method))
        if "/git/ref/" in endpoint:
            raise releases.GitHubError("Not Found", 404)
        if method == "POST":
            return {"id": 10}
        if method == "PATCH":
            return {"html_url": "https://github.com/owner/catalog/releases/tag/v0.10.0"}
        return {"assets": [{"id": 100, "name": "ua-matrix.json", "state": "uploaded", "size": self.asset.stat().st_size}]}

    def publish(self):
        return releases.publish(self.github, REPOSITORY, "0.10.0", self.asset, COMMIT)

    def test_new_release_is_drafted_verified_then_published(self):
        result = self.publish()
        self.assertTrue(result["published"])
        self.assertTrue(result["latest"])
        create = next(payload for _, payload, method in self.api_calls if method == "POST")
        self.assertTrue(create["draft"])
        self.assertEqual(create["target_commitish"], COMMIT)
        publish = next(payload for _, payload, method in self.api_calls if method == "PATCH")
        self.assertEqual(publish["make_latest"], "true")

    def test_backfill_does_not_regress_latest(self):
        self.github.releases.side_effect = lambda repository: iter([catalog_release("0.11.0")])
        result = self.publish()
        self.assertFalse(result["latest"])
        publish = next(payload for _, payload, method in self.api_calls if method == "PATCH")
        self.assertEqual(publish["make_latest"], "false")

    def test_existing_published_release_is_never_mutated(self):
        self.github.releases.side_effect = lambda repository: iter([catalog_release("0.10.0")])
        self.assertFalse(self.publish()["published"])
        self.github.by_tag.assert_not_called()
        self.github.api.assert_not_called()
        self.github.command.assert_not_called()

    def test_draft_resume_clobbers_only_unpublished_asset(self):
        self.github.by_tag.return_value = {"id": 10, "draft": True, "target_commitish": COMMIT}
        self.assertTrue(self.publish()["published"])
        self.assertFalse(any(method == "POST" for _, _, method in self.api_calls))
        self.assertIn("--clobber", self.github.command.call_args_list[0].args[0])

    def test_draft_commit_mismatch_stops_before_upload(self):
        self.github.by_tag.return_value = {"id": 10, "draft": True, "target_commitish": "b" * 40}
        with self.assertRaisesRegex(releases.ReleaseError, "targets another commit"):
            self.publish()
        self.github.command.assert_not_called()

    def test_matrix_commit_mismatch_stops_before_network_calls(self):
        matrix = matrix_payload()
        matrix["collector"]["commit"] = "b" * 40
        self.asset.write_text(json.dumps(matrix))
        with self.assertRaisesRegex(releases.ReleaseError, "must match"):
            self.publish()
        self.github.releases.assert_not_called()

    def test_incomplete_matrix_stops_before_network_calls(self):
        matrix = matrix_payload()
        del matrix["platforms"]["windows-arm64"]
        self.asset.write_text(json.dumps(matrix))
        with self.assertRaises(releases.MatrixError):
            self.publish()
        self.github.releases.assert_not_called()

    def test_asset_readback_mismatch_leaves_draft(self):
        self.github.command.side_effect = lambda argv: b"different" if argv[0] == "api" else b""
        with self.assertRaisesRegex(releases.ReleaseError, "differs"):
            self.publish()
        self.assertFalse(any(method == "PATCH" for _, _, method in self.api_calls))


class TransportTests(unittest.TestCase):
    def test_only_known_404_means_absent(self):
        github = releases.GitHub()
        with patch.object(github, "api", side_effect=releases.GitHubError("denied", 403)):
            with self.assertRaises(releases.GitHubError):
                github.by_tag(REPOSITORY, "v0.10.0")
        with patch.object(github, "api", side_effect=releases.GitHubError("missing", 404)):
            self.assertIsNone(github.by_tag(REPOSITORY, "v0.10.0"))

    def test_release_listing_paginates(self):
        github = releases.GitHub()
        with patch.object(github, "api", side_effect=[[{}] * 100, [{"id": 101}]]) as api:
            self.assertEqual(len(list(github.releases(REPOSITORY))), 101)
            self.assertIn("page=2", api.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

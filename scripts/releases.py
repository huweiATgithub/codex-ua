#!/usr/bin/env python3
"""Discover uncollected stable Codex versions and publish their UA matrices."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from matrix import MatrixError, StableVersion, TARGETS, parse_published_matrix, unique_object


ASSET = "ua-matrix.json"
UPSTREAM = "openai/codex"


class ReleaseError(RuntimeError):
    """Discovery or publication cannot safely continue."""


class GitHubError(ReleaseError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GitHub:
    def command(self, arguments: list[str], payload: dict | None = None) -> bytes:
        result = subprocess.run(
            ["gh", *arguments],
            input=None if payload is None else json.dumps(payload).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            status = re.search(r"\(HTTP ([0-9]{3})\)", message)
            raise GitHubError(message or "GitHub command failed", int(status[1]) if status else None)
        return result.stdout

    def api(self, endpoint: str, payload: dict | None = None, method: str = "GET"):
        arguments = ["api", endpoint, "--method", method]
        if payload is not None:
            arguments.extend(["--input", "-"])
        return json.loads(self.command(arguments, payload))

    def by_tag(self, repository: str, tag: str) -> dict | None:
        try:
            return self.api(f"repos/{repository}/releases/tags/{tag}")
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def releases(self, repository: str):
        page = 1
        while True:
            values = self.api(f"repos/{repository}/releases?per_page=100&page={page}")
            yield from values
            if len(values) < 100:
                return
            page += 1


def version_key(version: StableVersion) -> tuple[int, ...]:
    return tuple(map(int, version.value.split(".")))


def release_version(value: dict, prefix: str) -> StableVersion | None:
    tag = value.get("tag_name", "")
    if value.get("draft") or value.get("prerelease") or not tag.startswith(prefix):
        return None
    try:
        return StableVersion.parse(tag[len(prefix):])
    except MatrixError:
        return None


@dataclass(frozen=True)
class CompletedRelease:
    version: StableVersion
    published_at: datetime
    data: dict

    @classmethod
    def parse(cls, value: dict) -> CompletedRelease | None:
        version = release_version(value, "v")
        if version is None:
            return None
        assets = [asset for asset in value.get("assets", []) if asset.get("name") == ASSET]
        if len(assets) != 1 or assets[0].get("size", 0) <= 0 or assets[0].get("state") != "uploaded":
            raise ReleaseError(
                f"Published release v{version.value} has no complete {ASSET}; "
                "repair it explicitly before continuing. Published releases are never overwritten."
            )
        published_at = datetime.fromisoformat(value["published_at"])
        if published_at.tzinfo is None:
            raise ReleaseError(f"Published release v{version.value} has no timezone in published_at")
        return cls(version, published_at, value)


def completed_catalog(github: GitHub, repository: str) -> dict[str, CompletedRelease]:
    completed = {}
    for value in github.releases(repository):
        release = CompletedRelease.parse(value)
        if release:
            completed[release.version.value] = release
    return completed


def missing_binary_assets(release: dict) -> list[str]:
    available = {
        asset.get("name") for asset in release.get("assets", [])
        if asset.get("size", 0) > 0 and asset.get("state") == "uploaded"
    }
    expected = {
        f"codex-{target}{'.exe' if platform.startswith('windows-') else ''}.tar.gz"
        for platform, target in TARGETS.items()
    }
    return sorted(expected - available)


def verify_client_profiles(github: GitHub, version: StableVersion) -> None:
    # initialize accepts arbitrary client identities; verify the real CLI callers
    # at the selected release before treating the composed profiles as CLI UAs.
    for crate, name in (("tui", "codex-tui"), ("exec", "codex_exec")):
        path = f"codex-rs/{crate}/src/lib.rs"
        value = github.api(f"repos/{UPSTREAM}/contents/{path}?ref=rust-v{version.value}")
        if value.get("encoding") != "base64" or not value.get("content"):
            raise ReleaseError(f"Cannot read {path} at rust-v{version.value}")
        source = base64.b64decode(value["content"]).decode("utf-8")
        pattern = (
            r"InProcessClientStartArgs\s*\{"
            r"(?:(?!\bclient_name\s*:).)*?"
            rf'\bclient_name\s*:\s*"{re.escape(name)}"\.to_string\(\)\s*,\s*'
            r'client_version\s*:\s*env!\(\s*"CARGO_PKG_VERSION"\s*\)\.to_string\(\)\s*,'
        )
        if not re.search(pattern, source, re.DOTALL):
            raise ReleaseError(
                f"CLI profile changed or is unsupported in {path} at rust-v{version.value}; "
                "review the upstream initialization before collecting this version."
            )


def discover(github: GitHub, repository: str, requested: str | None = None) -> dict:
    requested = requested or None
    version = StableVersion.parse(requested) if requested is not None else None
    completed = completed_catalog(github, repository)
    if version and version.value in completed:
        return {"needed": False, "version": "", "reason": f"v{version.value} is already published"}
    if version:
        upstream = github.by_tag(UPSTREAM, f"rust-v{version.value}")
        if upstream is None or release_version(upstream, "rust-v") != version:
            raise ReleaseError(f"No published stable Codex release rust-v{version.value}")
    elif not completed:
        upstream = github.api(f"repos/{UPSTREAM}/releases/latest")
        version = release_version(upstream, "rust-v")
        if version is None:
            raise ReleaseError("Upstream latest release is not a published stable rust-vX.Y.Z release")
    else:
        # A later manual historical backfill must not move the automatic
        # collection floor backward from the first published catalog version.
        oldest = min(completed.values(), key=lambda item: item.published_at).version
        candidates = []
        boundary_found = False
        for upstream in github.releases(UPSTREAM):
            candidate = release_version(upstream, "rust-v")
            if candidate is not None and version_key(candidate) >= version_key(oldest):
                if candidate.value not in completed and not missing_binary_assets(upstream):
                    candidates.append((candidate, upstream))
                if candidate == oldest:
                    boundary_found = True
                    break
        if not boundary_found:
            raise ReleaseError(f"Upstream history no longer contains collected rust-v{oldest.value}")
        if not candidates:
            return {"needed": False, "version": "", "reason": "No uncollected stable releases with complete binaries"}
        version, upstream = min(candidates, key=lambda item: version_key(item[0]))
    missing = missing_binary_assets(upstream)
    if missing:
        reason = f"rust-v{version.value} is missing required binaries: {', '.join(missing)}"
        if requested:
            raise ReleaseError(reason)
        return {"needed": False, "version": "", "reason": reason}
    verify_client_profiles(github, version)
    return {"needed": True, "version": version.value, "reason": "Stable release has no published matrix"}


def verify_tag_target(github: GitHub, repository: str, tag: str, commit: str) -> None:
    value = github.api(f"repos/{repository}/git/ref/tags/{tag}")["object"]
    while value["type"] == "tag":
        value = github.api(f"repos/{repository}/git/tags/{value['sha']}")["object"]
    if value["type"] != "commit" or value["sha"] != commit:
        raise ReleaseError(f"Tag {tag} already targets another commit; inspect the draft/tag before retrying")


def publish(github: GitHub, repository: str, requested: str, asset: Path, commit: str) -> dict:
    version = StableVersion.parse(requested)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("collector-commit must be a lowercase 40-character Git commit SHA")
    if asset.name != ASSET:
        raise ReleaseError(f"Release asset must be named {ASSET}")
    raw = asset.read_bytes()
    matrix = parse_published_matrix(json.loads(raw, object_pairs_hook=unique_object))
    if matrix.get("codex_version") != version.value or matrix.get("collector", {}).get("commit") != commit:
        raise ReleaseError("Matrix Codex version and collector commit must match the publication arguments")
    completed = completed_catalog(github, repository)
    if version.value in completed:
        return {"published": False, "reason": f"v{version.value} is already published"}
    tag = f"v{version.value}"
    # REST lookup by tag only returns published releases. The authenticated
    # release listing also includes drafts left by interrupted publication.
    matches = [item for item in github.releases(repository) if item.get("tag_name") == tag]
    if len(matches) > 1:
        raise ReleaseError(f"Multiple releases use {tag}; inspect them before retrying")
    draft = matches[0] if matches else None
    if draft is not None and not draft.get("draft"):
        raise ReleaseError(f"Existing release {tag} is not a draft; inspect it explicitly")
    if draft:
        try:
            verify_tag_target(github, repository, tag, commit)
        except GitHubError as error:
            # GitHub may defer creating a draft's tag until publication.
            if error.status != 404:
                raise
            if draft.get("target_commitish") != commit:
                raise ReleaseError(f"Draft {tag} targets another commit; inspect it before retrying") from error
    else:
        # A release can be absent while its tag already exists; never retarget it.
        try:
            verify_tag_target(github, repository, tag, commit)
        except GitHubError as error:
            if error.status != 404:
                raise
        draft = github.api(f"repos/{repository}/releases", {
            "tag_name": tag, "target_commitish": commit, "name": f"Codex {version.value} UA matrix",
            "body": f"CLI User-Agent matrix for Codex {version.value}.\n\nCollector commit: `{commit}`.\n",
            "draft": True, "prerelease": False,
        }, "POST")
    github.command(["release", "upload", tag, str(asset), "--repo", repository, "--clobber"])
    uploaded = github.api(f"repos/{repository}/releases/{draft['id']}")
    matches = [item for item in uploaded["assets"] if item.get("name") == ASSET]
    if len(uploaded["assets"]) != 1 or len(matches) != 1 or matches[0].get("state") != "uploaded" or matches[0].get("size") != len(raw):
        raise ReleaseError(f"Draft {tag}: uploaded asset is absent, incomplete, or has the wrong size")
    downloaded = github.command([
        "api", f"repos/{repository}/releases/assets/{matches[0]['id']}",
        "--header", "Accept: application/octet-stream",
    ])
    if downloaded != raw:
        raise ReleaseError(f"Draft {tag}: downloaded asset differs from the collected matrix")
    # Workflow concurrency serializes discover/collect/publish across all runs.
    completed = completed_catalog(github, repository)
    latest = all(version_key(version) > version_key(item.version) for item in completed.values())
    release = github.api(f"repos/{repository}/releases/{draft['id']}", {
        "draft": False, "prerelease": False, "make_latest": "true" if latest else "false",
    }, "PATCH")
    return {"published": True, "tag": tag, "latest": latest, "url": release["html_url"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--repository", required=True)
        command.add_argument("--version", required=name == "publish")
        if name == "publish":
            command.add_argument("--asset", type=Path, required=True)
            command.add_argument("--collector-commit", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository must be an owner/name pair")
    try:
        github = GitHub()
        if args.command == "discover":
            result = discover(github, args.repository, args.version)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write(f"version={result['version']}\nneeded={str(result['needed']).lower()}\n")
        else:
            result = publish(github, args.repository, args.version, args.asset, args.collector_commit)
        print(json.dumps(result))
    except (ReleaseError, MatrixError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

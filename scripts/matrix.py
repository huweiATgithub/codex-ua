#!/usr/bin/env python3
"""Assemble platform records into compact and detailed UA matrices."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re


TARGETS = {
    "linux-ubuntu-x64": "x86_64-unknown-linux-musl",
    "linux-ubuntu-arm64": "aarch64-unknown-linux-musl",
    "linux-debian-x64": "x86_64-unknown-linux-musl",
    "linux-debian-arm64": "aarch64-unknown-linux-musl",
    "linux-fedora-x64": "x86_64-unknown-linux-musl",
    "linux-fedora-arm64": "aarch64-unknown-linux-musl",
    "linux-alpine-x64": "x86_64-unknown-linux-musl",
    "linux-alpine-arm64": "aarch64-unknown-linux-musl",
    "macos-x64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
    "windows-x64": "x86_64-pc-windows-msvc",
    "windows-arm64": "aarch64-pc-windows-msvc",
}
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
RUN_URL_PATTERN = (
    r"https://github\.com/[^/\s]+/[^/\s]+/actions/runs/[1-9][0-9]*"
    r"(?:/attempts/[1-9][0-9]*)?"
)


class MatrixError(ValueError):
    """A collected record does not satisfy the published matrix contract."""


def object_fields(value: object, fields: set[str], context: str) -> dict:
    if not isinstance(value, dict):
        raise MatrixError(f"{context}: expected an object")
    missing = fields - value.keys()
    extra = value.keys() - fields
    if missing or extra:
        raise MatrixError(
            f"{context}: missing fields {sorted(missing)}; unexpected fields {sorted(extra)}"
        )
    return value


def nonempty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MatrixError(f"{context}: expected a nonempty string")
    return value


@dataclass(frozen=True)
class StableVersion:
    value: str

    @classmethod
    def parse(cls, value: object) -> StableVersion:
        value = nonempty_string(value, "codex_version")
        if not re.fullmatch(VERSION_PATTERN, value):
            raise MatrixError(f"codex_version: expected a stable x.y.z version, got {value!r}")
        return cls(value)

    @property
    def upstream_release(self) -> str:
        return f"https://github.com/openai/codex/releases/tag/rust-v{self.value}"


@dataclass(frozen=True)
class CollectorProvenance:
    commit: str
    run_url: str

    @classmethod
    def parse(cls, value: object) -> CollectorProvenance:
        values = object_fields(value, {"commit", "run_url"}, "collector")
        commit = nonempty_string(values["commit"], "collector.commit")
        run_url = nonempty_string(values["run_url"], "collector.run_url")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise MatrixError("collector.commit: expected a lowercase 40-character Git commit SHA")
        if not re.fullmatch(RUN_URL_PATTERN, run_url):
            raise MatrixError("collector.run_url: expected a GitHub Actions workflow run URL")
        return cls(commit, run_url)


@dataclass(frozen=True)
class LinuxDistribution:
    id: str
    version_id: str
    pretty_name: str

    @classmethod
    def parse(cls, value: object, expected_id: str) -> LinuxDistribution:
        fields = {"id", "version_id", "pretty_name"}
        values = object_fields(value, fields, "os.distribution")
        distribution = cls(**{key: nonempty_string(values[key], f"os.distribution.{key}") for key in fields})
        if distribution.id != expected_id:
            raise MatrixError(f"os.distribution.id: expected {expected_id}")
        return distribution


@dataclass(frozen=True)
class OSInfo:
    system: str
    release: str
    version: str
    machine: str
    distribution: LinuxDistribution | None

    @classmethod
    def parse(cls, value: object, platform: str) -> OSInfo:
        fields = {"system", "release", "version", "machine"}
        values = object_fields(value, fields | {"distribution"}, "os")
        distribution = None
        if platform.startswith("linux-"):
            distribution = LinuxDistribution.parse(values["distribution"], platform.split("-")[1])
        elif values["distribution"] is not None:
            raise MatrixError("os.distribution: expected null outside Linux")
        info = cls(**{key: nonempty_string(values[key], f"os.{key}") for key in fields}, distribution=distribution)
        expected_system = {"linux": "Linux", "macos": "Darwin", "windows": "Windows"}[platform.split("-")[0]]
        expected_machines = {"x86_64", "amd64"} if platform.endswith("-x64") else {"aarch64", "arm64"}
        if info.system != expected_system or info.machine.lower() not in expected_machines:
            raise MatrixError(f"os: system and machine must match {platform}")
        return info


@dataclass(frozen=True)
class RunnerInfo:
    name: str
    image: str
    image_version: str
    container_image: str | None

    @classmethod
    def parse(cls, value: object) -> RunnerInfo:
        fields = {"name", "image", "image_version"}
        values = object_fields(value, fields | {"container_image"}, "runner")
        container = values["container_image"]
        if container is not None:
            container = nonempty_string(container, "runner.container_image")
        return cls(**{key: nonempty_string(values[key], f"runner.{key}") for key in fields}, container_image=container)


@dataclass(frozen=True)
class HttpCapture:
    user_agent: str
    originator: str


@dataclass(frozen=True)
class ClientObservation:
    user_agent: str
    http_capture: HttpCapture | None = None

    @classmethod
    def parse(cls, value: object, mode: str, version: StableVersion) -> ClientObservation:
        fields = {"user_agent", "method"}
        if mode == "exec":
            fields.add("http_capture")
        values = object_fields(value, fields, f"clients.{mode}")
        if values["method"] != "app-server-initialize":
            raise MatrixError(f"clients.{mode}.method: expected app-server-initialize")
        ua = nonempty_string(values["user_agent"], f"clients.{mode}.user_agent")
        identity = "codex-tui" if mode == "interactive" else "codex_exec"
        prefix = f"{identity}/{version.value} ("
        suffix = f") xterm-256color ({identity}; {version.value})"
        if not ua.startswith(prefix) or not ua.endswith(suffix):
            raise MatrixError(f"clients.{mode}.user_agent: unexpected identity, version, or terminal")
        environment = ua[len(prefix) : -len(suffix)]
        if not re.fullmatch(r"[^;()\r\n]+; [^;()\r\n]+", environment):
            raise MatrixError(f"clients.{mode}.user_agent: expected OS and architecture")
        if any(ord(char) < 32 or ord(char) == 127 for char in ua):
            raise MatrixError(f"clients.{mode}.user_agent: contains a control character")

        capture = None
        if mode == "exec":
            captured = object_fields(values["http_capture"], {"user_agent", "originator"}, "http_capture")
            if captured["user_agent"] != ua:
                raise MatrixError("clients.exec.http_capture.user_agent: differs from initialized UA")
            if captured["originator"] != "codex_exec":
                raise MatrixError("clients.exec.http_capture.originator: expected codex_exec")
            capture = HttpCapture(ua, "codex_exec")
        return cls(ua, capture)

    def to_dict(self) -> dict:
        result = {"user_agent": self.user_agent, "method": "app-server-initialize"}
        if self.http_capture is not None:
            result["http_capture"] = asdict(self.http_capture)
        return result


@dataclass(frozen=True)
class PlatformObservation:
    platform: str
    target: str
    source_url: str
    collected_at: str
    os: OSInfo
    runner: RunnerInfo
    interactive: ClientObservation
    exec: ClientObservation

    @classmethod
    def parse(cls, value: object, version: StableVersion) -> PlatformObservation:
        values = object_fields(
            value,
            {"schema_version", "codex_version", "platform", "target", "source_url", "collected_at", "os", "runner", "terminal", "clients"},
            "platform record",
        )
        if type(values["schema_version"]) is not int or values["schema_version"] != 1:
            raise MatrixError("schema_version: expected 1")
        if StableVersion.parse(values["codex_version"]) != version:
            raise MatrixError("codex_version: differs from requested collection version")
        platform = nonempty_string(values["platform"], "platform")
        if platform not in TARGETS:
            raise MatrixError(f"platform: unsupported platform {platform!r}")
        target = TARGETS[platform]
        if values["target"] != target:
            raise MatrixError(f"target: expected {target}")
        extension = ".exe.tar.gz" if platform.startswith("windows-") else ".tar.gz"
        source_url = (
            f"https://github.com/openai/codex/releases/download/rust-v{version.value}/"
            f"codex-{target}{extension}"
        )
        if values["source_url"] != source_url:
            raise MatrixError(f"source_url: expected {source_url}")
        collected_at = nonempty_string(values["collected_at"], "collected_at")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|\+00:00)", collected_at):
            raise MatrixError("collected_at: expected a UTC ISO 8601 timestamp")
        try:
            timestamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise MatrixError("collected_at: invalid timestamp") from error
        if timestamp.utcoffset() != timedelta(0):
            raise MatrixError("collected_at: expected UTC")
        if values["terminal"] != {"TERM": "xterm-256color"}:
            raise MatrixError("terminal: expected only TERM=xterm-256color")
        clients = object_fields(values["clients"], {"interactive", "exec"}, "clients")
        return cls(
            platform,
            target,
            source_url,
            collected_at,
            OSInfo.parse(values["os"], platform),
            RunnerInfo.parse(values["runner"]),
            ClientObservation.parse(clients["interactive"], "interactive", version),
            ClientObservation.parse(clients["exec"], "exec", version),
        )

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "source_url": self.source_url,
            "collected_at": self.collected_at,
            "os": asdict(self.os),
            "runner": asdict(self.runner),
            "terminal": {"TERM": "xterm-256color"},
            "clients": {"interactive": self.interactive.to_dict(), "exec": self.exec.to_dict()},
        }


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MatrixError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_run_matrix(value: object) -> dict:
    values = object_fields(
        value,
        {"schema_version", "codex_version", "upstream_release", "collector", "platforms"},
        "matrix",
    )
    if type(values["schema_version"]) is not int or values["schema_version"] != 1:
        raise MatrixError("schema_version: expected 1")
    version = StableVersion.parse(values["codex_version"])
    if values["upstream_release"] != version.upstream_release:
        raise MatrixError("upstream_release: differs from the Codex version's official release URL")
    collector = CollectorProvenance.parse(values["collector"])
    records = object_fields(values["platforms"], set(TARGETS), "platforms")
    platforms = {}
    for platform in TARGETS:
        record = object_fields(
            records[platform],
            {"target", "source_url", "collected_at", "os", "runner", "terminal", "clients"},
            f"platforms.{platform}",
        )
        try:
            observation = PlatformObservation.parse(
                {"schema_version": 1, "codex_version": version.value, "platform": platform, **record},
                version,
            )
        except MatrixError as error:
            raise MatrixError(f"platforms.{platform}: {error}") from error
        platforms[platform] = observation.to_dict()
    return {
        "schema_version": 1,
        "codex_version": version.value,
        "upstream_release": version.upstream_release,
        "collector": asdict(collector),
        "platforms": platforms,
    }


@dataclass(frozen=True)
class PublicationMatrices:
    run: dict
    matrix: dict


def publication_matrices(value: object) -> PublicationMatrices:
    run = parse_run_matrix(value)
    matrix = {
        "schema_version": 1,
        "codex_version": run["codex_version"],
        "platforms": {
            platform: {
                mode: observation["clients"][mode]["user_agent"]
                for mode in ("interactive", "exec")
            }
            for platform, observation in run["platforms"].items()
        },
    }
    return PublicationMatrices(run=run, matrix=matrix)


def assemble_matrix(version: str, input_dir: Path, collector_commit: str, run_url: str) -> dict:
    requested_version = StableVersion.parse(version)
    collector = CollectorProvenance.parse({"commit": collector_commit, "run_url": run_url})
    if not input_dir.is_dir():
        raise MatrixError(f"input_dir: not a directory: {input_dir}")
    platforms: dict[str, PlatformObservation] = {}
    for path in sorted(input_dir.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            observation = PlatformObservation.parse(value, requested_version)
            if observation.platform in platforms:
                raise MatrixError(f"duplicate platform: {observation.platform}")
            platforms[observation.platform] = observation
        except (OSError, UnicodeError, ValueError) as error:
            raise MatrixError(f"{path.name}: {error}") from error
    missing = TARGETS.keys() - platforms.keys()
    if missing:
        raise MatrixError(f"missing platforms: {', '.join(sorted(missing))}")
    return {
        "schema_version": 1,
        "codex_version": requested_version.value,
        "upstream_release": requested_version.upstream_release,
        "collector": asdict(collector),
        "platforms": {platform: platforms[platform].to_dict() for platform in TARGETS},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--collector-commit", required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    try:
        results = publication_matrices(
            assemble_matrix(args.version, args.input_dir, args.collector_commit, args.run_url)
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, value in (("ua-matrix.json", results.matrix), ("ua-matrix.run.json", results.run)):
            (args.output_dir / filename).write_text(
                json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
    except (MatrixError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

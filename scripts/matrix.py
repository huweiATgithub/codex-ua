#!/usr/bin/env python3
"""Assemble platform records into compact and detailed UA matrices."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

try:
    from .terminals import APPLICATIONS, LAUNCH_METHODS, TerminalRelease, native_target, parse_releases, unsupported_reason
except ImportError:
    from terminals import APPLICATIONS, LAUNCH_METHODS, TerminalRelease, native_target, parse_releases, unsupported_reason


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


def terminal_releases(value: object) -> dict[str, TerminalRelease]:
    try:
        return parse_releases(value)
    except ValueError as error:
        raise MatrixError(f"terminal_releases: {error}") from error


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
    http_capture: HttpCapture

    @classmethod
    def parse(cls, value: object, mode: str, version: StableVersion, terminal: str | None = "xterm-256color") -> ClientObservation:
        fields = {"user_agent", "method", "http_capture"}
        values = object_fields(value, fields, f"clients.{mode}")
        if values["method"] != "http-capture":
            raise MatrixError(f"clients.{mode}.method: expected http-capture")
        ua = nonempty_string(values["user_agent"], f"clients.{mode}.user_agent")
        identity = "codex-tui" if mode == "CLI" else "codex_exec"
        prefix = f"{identity}/{version.value} ("
        suffix = f" ({identity}; {version.value})"
        if not ua.startswith(prefix) or not ua.endswith(suffix):
            raise MatrixError(f"clients.{mode}.user_agent: unexpected identity, version, or terminal")
        contents = ua[len(prefix) : -len(suffix)]
        if ") " not in contents:
            raise MatrixError(f"clients.{mode}.user_agent: missing terminal")
        environment, observed_terminal = contents.rsplit(") ", 1)
        if not observed_terminal.strip() or any(char in observed_terminal for char in "()") or (terminal is not None and observed_terminal != terminal):
            raise MatrixError(f"clients.{mode}.user_agent: unexpected terminal")
        if not re.fullmatch(r"[^;()\r\n]+; [^;()\r\n]+", environment):
            raise MatrixError(f"clients.{mode}.user_agent: expected OS and architecture")
        if any(ord(char) < 32 or ord(char) == 127 for char in ua):
            raise MatrixError(f"clients.{mode}.user_agent: contains a control character")

        captured = object_fields(values["http_capture"], {"user_agent", "originator"}, "http_capture")
        if captured["user_agent"] != ua:
            raise MatrixError(f"clients.{mode}.http_capture.user_agent: differs from recorded UA")
        if captured["originator"] != identity:
            raise MatrixError(f"clients.{mode}.http_capture.originator: expected {identity}")
        capture = HttpCapture(ua, identity)
        return cls(ua, capture)

    def to_dict(self) -> dict:
        return {"user_agent": self.user_agent, "method": "http-capture", "http_capture": asdict(self.http_capture)}


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
        if type(values["schema_version"]) is not int or values["schema_version"] != 2:
            raise MatrixError("schema_version: expected 2")
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
        clients = object_fields(values["clients"], {"CLI", "Exec"}, "clients")
        return cls(
            platform,
            target,
            source_url,
            collected_at,
            OSInfo.parse(values["os"], platform),
            RunnerInfo.parse(values["runner"]),
            ClientObservation.parse(clients["CLI"], "CLI", version),
            ClientObservation.parse(clients["Exec"], "Exec", version),
        )

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "source_url": self.source_url,
            "collected_at": self.collected_at,
            "os": asdict(self.os),
            "runner": asdict(self.runner),
            "terminal": {"TERM": "xterm-256color"},
            "clients": {"CLI": self.interactive.to_dict(), "Exec": self.exec.to_dict()},
        }


@dataclass(frozen=True)
class UnsupportedProfile:
    reason: str

    def to_dict(self) -> dict:
        return {"status": "unsupported", "reason": self.reason}


@dataclass(frozen=True)
class CollectedProfile:
    profile: str
    release: TerminalRelease
    target: str
    terminal: dict[str, str]
    interactive: ClientObservation
    exec: ClientObservation

    def to_dict(self) -> dict:
        return {
            "status": "collected", "application": {"version": self.release.version, "source_url": self.release.assets[self.target]},
            "launch_method": LAUNCH_METHODS[self.profile], "terminal": dict(self.terminal), "tty": [True, True, True],
            "clients": {"CLI": self.interactive.to_dict(), "Exec": self.exec.to_dict()},
        }


@dataclass(frozen=True)
class ProfiledPlatformObservation:
    baseline: PlatformObservation
    releases: dict[str, TerminalRelease]
    profiles: dict[str, CollectedProfile | UnsupportedProfile]

    @property
    def platform(self) -> str:
        return self.baseline.platform

    @classmethod
    def parse(cls, value: object, version: StableVersion) -> ProfiledPlatformObservation:
        fields = {"schema_version", "codex_version", "platform", "target", "source_url", "collected_at", "os", "runner", "terminal", "clients"}
        values = object_fields(value, fields | {"terminal_releases", "profiles"}, "platform record")
        if type(values["schema_version"]) is not int or values["schema_version"] != 3:
            raise MatrixError("schema_version: expected 3")
        baseline = PlatformObservation.parse({**{key: values[key] for key in fields}, "schema_version": 2}, version)
        selected = terminal_releases(values["terminal_releases"])
        raw_profiles = object_fields(values["profiles"], set(APPLICATIONS), "profiles")
        profiles = {}
        for profile in APPLICATIONS:
            release = selected[profile]
            reason = unsupported_reason(profile, baseline.platform, release, baseline.runner.container_image, baseline.runner.image)
            raw = raw_profiles[profile]
            context = f"profiles.{profile}"
            if reason:
                unsupported = object_fields(raw, {"status", "reason"}, context)
                if unsupported != {"status": "unsupported", "reason": reason}:
                    raise MatrixError(f"{context}: expected unsupported with the native support reason")
                profiles[profile] = UnsupportedProfile(reason)
                continue
            collected = object_fields(raw, {"status", "application", "launch_method", "terminal", "tty", "clients"}, context)
            if collected["status"] != "collected" or collected["launch_method"] != LAUNCH_METHODS[profile]:
                raise MatrixError(f"{context}: supported combinations require native collection")
            target = native_target(baseline.platform)
            if collected["application"] != {"version": release.version, "source_url": release.assets[target]}:
                raise MatrixError(f"{context}.application: differs from the fixed official release and native target")
            if collected["tty"] != [True, True, True] or not all(type(value) is bool for value in collected["tty"]):
                raise MatrixError(f"{context}.tty: expected a real terminal PTY")
            terminal = collected["terminal"]
            if not isinstance(terminal, dict) or not terminal or not all(
                isinstance(key, str) and key.startswith(("TERM", "WT_", "HERDR_")) and isinstance(value, str)
                for key, value in terminal.items()
            ):
                raise MatrixError(f"{context}.terminal: expected the application-generated terminal context")
            clients = object_fields(collected["clients"], {"CLI", "Exec"}, context + ".clients")
            profiles[profile] = CollectedProfile(profile, release, target, dict(terminal),
                ClientObservation.parse(clients["CLI"], "CLI", version, terminal=None),
                ClientObservation.parse(clients["Exec"], "Exec", version, terminal=None))
        return cls(baseline, selected, profiles)

    def to_dict(self) -> dict:
        return {**self.baseline.to_dict(), "profiles": {name: value.to_dict() for name, value in self.profiles.items()}}


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MatrixError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_run_matrix(value: object) -> dict:
    if isinstance(value, dict) and value.get("schema_version") == 3:
        values = object_fields(value, {"schema_version", "codex_version", "upstream_release", "collector", "platforms", "terminal_releases"}, "matrix")
        if type(values["schema_version"]) is not int:
            raise MatrixError("schema_version: expected 3")
        version = StableVersion.parse(values["codex_version"])
        if values["upstream_release"] != version.upstream_release:
            raise MatrixError("upstream_release: differs from the Codex version's official release URL")
        collector = CollectorProvenance.parse(values["collector"])
        selected = terminal_releases(values["terminal_releases"])
        snapshot = {name: release.to_dict() for name, release in selected.items()}
        raw_platforms = object_fields(values["platforms"], set(TARGETS), "platforms")
        platforms = {}
        for platform, record in raw_platforms.items():
            if not isinstance(record, dict):
                raise MatrixError(f"platforms.{platform}: expected an object")
            observation = ProfiledPlatformObservation.parse({**record, "schema_version": 3, "codex_version": version.value,
                "platform": platform, "terminal_releases": snapshot}, version)
            if set(record) != set(observation.to_dict()):
                raise MatrixError(f"platforms.{platform}: unexpected fields")
            platforms[platform] = observation.to_dict()
        return {"schema_version": 3, "codex_version": version.value, "upstream_release": version.upstream_release,
                "collector": asdict(collector), "terminal_releases": snapshot, "platforms": platforms}
    values = object_fields(
        value,
        {"schema_version", "codex_version", "upstream_release", "collector", "platforms"},
        "matrix",
    )
    if type(values["schema_version"]) is not int or values["schema_version"] != 2:
        raise MatrixError("schema_version: expected 2")
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
                {"schema_version": 2, "codex_version": version.value, "platform": platform, **record},
                version,
            )
        except MatrixError as error:
            raise MatrixError(f"platforms.{platform}: {error}") from error
        platforms[platform] = observation.to_dict()
    return {
        "schema_version": 2,
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
        "schema_version": 2 if run["schema_version"] == 3 else 1,
        "codex_version": run["codex_version"],
        "platforms": {
            platform: {
                mode: observation["clients"][mode]["user_agent"]
                for mode in ("CLI", "Exec")
            }
            for platform, observation in run["platforms"].items()
        },
    }
    if run["schema_version"] == 3:
        for platform, observation in run["platforms"].items():
            matrix["platforms"][platform]["profiles"] = {
                profile: {mode: context["clients"][mode]["user_agent"] for mode in ("CLI", "Exec")}
                if context["status"] == "collected" else None
                for profile, context in observation["profiles"].items()
            }
    return PublicationMatrices(run=run, matrix=matrix)


def assemble_matrix(version: str, input_dir: Path, collector_commit: str, run_url: str) -> dict:
    requested_version = StableVersion.parse(version)
    collector = CollectorProvenance.parse({"commit": collector_commit, "run_url": run_url})
    if not input_dir.is_dir():
        raise MatrixError(f"input_dir: not a directory: {input_dir}")
    platforms: dict[str, PlatformObservation | ProfiledPlatformObservation] = {}
    selected = None
    record_schema = None
    for path in sorted(input_dir.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
            schema = value.get("schema_version") if isinstance(value, dict) else None
            if record_schema is not None and schema != record_schema:
                raise MatrixError("mixed collection schemas; every platform must include the same profiles")
            record_schema = schema
            if schema == 3:
                observation = ProfiledPlatformObservation.parse(value, requested_version)
                if selected is not None and selected != observation.releases:
                    raise MatrixError("terminal_releases: platforms used different stable release snapshots")
                selected = observation.releases
            else:
                observation = PlatformObservation.parse(value, requested_version)
            if observation.platform in platforms:
                raise MatrixError(f"duplicate platform: {observation.platform}")
            platforms[observation.platform] = observation
        except (OSError, UnicodeError, ValueError) as error:
            raise MatrixError(f"{path.name}: {error}") from error
    missing = TARGETS.keys() - platforms.keys()
    if missing:
        raise MatrixError(f"missing platforms: {', '.join(sorted(missing))}")
    result = {
        "schema_version": 3 if selected is not None else 2,
        "codex_version": requested_version.value,
        "upstream_release": requested_version.upstream_release,
        "collector": asdict(collector),
        "platforms": {platform: platforms[platform].to_dict() for platform in TARGETS},
    }
    if selected is not None:
        result["terminal_releases"] = {name: release.to_dict() for name, release in selected.items()}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--collector-commit", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--require-profiles", action="store_true", help="require complete terminal application coverage")
    args = parser.parse_args()
    try:
        results = publication_matrices(
            assemble_matrix(args.version, args.input_dir, args.collector_commit, args.run_url)
        )
        if args.require_profiles and results.run["schema_version"] != 3:
            raise MatrixError("terminal profiles are required for every platform")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, value in (("ua-matrix.json", results.matrix), ("ua-matrix.run.json", results.run)):
            (args.output_dir / filename).write_text(
                json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
    except (MatrixError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

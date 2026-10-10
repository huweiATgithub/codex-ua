#!/usr/bin/env python3
"""Collect Ubuntu's WindowsTerminal Profile through Windows Terminal and real WSL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

try:
    from . import collect, terminals
except ImportError:
    import collect
    import terminals


def guest_prepare(version, platform, directory):
    actual = collect.native_platform()
    if actual != platform or collect.os_info()["distribution"]["version_id"] != "24.04":
        raise RuntimeError(f"expected Ubuntu 24.04 on {platform}, received {actual}")
    binary = collect.download_binary(collect.source_url(version, platform), platform, directory)
    check = directory / "version-check"
    check.mkdir()
    result = subprocess.run([str(binary), "--version"], env=collect.child_environment(check),
                            capture_output=True, text=True, check=True, timeout=30)
    if result.stdout.strip() != f"codex-cli {version}":
        raise RuntimeError("WSL binary does not match the requested Codex version")


def guest_sample(platform, directory):
    if collect.native_platform() != platform:
        raise RuntimeError("the WSL sampler must run on the requested native Ubuntu architecture")
    tty = [os.isatty(fd) for fd in (0, 1, 2)]
    if tty != [True, True, True] or not os.environ.get("WT_SESSION") or not os.environ.get("WSL_DISTRO_NAME"):
        raise RuntimeError("sampling requires Windows Terminal's PTY and its real WSL session")
    clients = {}
    for mode in ("CLI", "Exec"):
        capture = collect.capture_client(directory / "codex", mode, directory / mode.lower(), native_terminal=True)
        clients[mode] = {"user_agent": capture["user_agent"], "method": "http-capture", "http_capture": capture}
    result = {"os": collect.os_info(), "tty": tty, "clients": clients,
              "terminal": {key: value for key, value in os.environ.items() if key.startswith(("TERM", "WT_", "WSL_"))}}
    (directory / "capture.json").write_text(json.dumps(result), encoding="utf-8")


def collect_wsl(version, platform, releases, distribution):
    host = collect.native_platform()
    if host != "windows-" + platform.rsplit("-", 1)[1]:
        raise RuntimeError(f"{platform} requires a matching native Windows host, received {host}")
    selected = terminals.parse_releases(releases)
    release = selected["WindowsTerminal"]
    wsl = str(Path(os.environ["SystemRoot"]) / "System32" / "wsl.exe")
    prefix = [wsl, "--distribution", distribution, "--exec"]

    def invoke(*command):
        return subprocess.check_output([*prefix, *command], text=True, encoding="utf-8", timeout=180).strip()

    script = invoke("wslpath", "-u", str(Path(__file__).resolve()))
    guest = invoke("mktemp", "-d", "/tmp/codex-ua-wsl-XXXXXXXX")
    if not re.fullmatch(r"/tmp/codex-ua-wsl-[A-Za-z0-9]{8}", guest):
        raise RuntimeError("WSL did not create the private sampling directory")
    try:
        invoke("python3", script, "prepare", "--version", version, "--platform", platform, "--directory", guest)
        with tempfile.TemporaryDirectory(prefix="cu-wsl-") as temporary:
            directory = Path(temporary)
            app = terminals.install("WindowsTerminal", release, host, directory / "app")
            command = [*prefix, "python3", "-u", script, "sample", "--platform", platform, "--directory", guest]
            context = terminals.run_in_terminal("WindowsTerminal", release, app, host, command, directory / "pty")
            if context["exit_code"]:
                raise RuntimeError(f"WindowsTerminal WSL sampling failed ({context['exit_code']})")
            capture = json.loads(invoke("cat", guest + "/capture.json"))
            session = context["environment"].get("WT_SESSION")
            if not session or capture["terminal"].get("WT_SESSION") != session:
                raise RuntimeError("WSL did not inherit the launched Windows Terminal session")
            profile = {"status": "collected", "application": context["application"],
                       "launch_method": "windows-terminal-wsl", "terminal": capture["terminal"],
                       "tty": capture["tty"], "clients": capture["clients"],
                       "runtime": {"os": capture["os"], "runner": collect.runner_info()}}
        return {"schema_version": 4, "codex_version": version, "platform": platform,
                "terminal_releases": releases, "profiles": {"WindowsTerminal": profile}}
    finally:
        invoke("rm", "-rf", "--", guest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    host = commands.add_parser("collect")
    host.add_argument("--version", required=True, type=collect.stable_version)
    host.add_argument("--platform", required=True, choices=("linux-ubuntu-x64", "linux-ubuntu-arm64"))
    host.add_argument("--terminal-releases", required=True, type=Path)
    host.add_argument("--distribution", default="Ubuntu-24.04")
    host.add_argument("--output", required=True, type=Path)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--version", required=True, type=collect.stable_version)
    prepare.add_argument("--platform", required=True, choices=("linux-ubuntu-x64", "linux-ubuntu-arm64"))
    prepare.add_argument("--directory", required=True, type=Path)
    sample = commands.add_parser("sample")
    sample.add_argument("--platform", required=True, choices=("linux-ubuntu-x64", "linux-ubuntu-arm64"))
    sample.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        guest_prepare(args.version, args.platform, args.directory)
    elif args.action == "sample":
        guest_sample(args.platform, args.directory)
    else:
        result = collect_wsl(args.version, args.platform,
                             json.loads(args.terminal_releases.read_text(encoding="utf-8")), args.distribution)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Resolve stable terminal releases and launch probes inside their native terminals.

Resolve once and share that release snapshot across the collection jobs. ``run``
downloads the selected native application and executes the argv after ``--`` in
its PTY. The sampling command owns UA/request capture and must preserve the
inherited terminal environment. Use absolute paths for its scripts and outputs.
Linux VS Code hosts need Xvfb, xauth and the application's native GUI libraries.

An unsupported combination produces {"unsupported": reason} without launching.
Supported launch failures raise an error; sampling failures retain their exit
status. The command has ten minutes to finish, plus ninety seconds for application
startup. No terminal detection variables are synthesized.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile


PROFILES = ("xterm-256color", "WindowsTerminal", "vscode", "herdr")
APPLICATIONS = PROFILES[1:]
LAUNCH_METHODS = {
    "xterm-256color": "controlled-environment",
    "WindowsTerminal": "windows-terminal",
    "vscode": "vscode-integrated-terminal",
    "herdr": "herdr-pty",
}
VS_CODE_PACKAGES = {
    "linux-x64": "linux-x64", "linux-arm64": "linux-arm64", "macos-x64": "darwin",
    "macos-arm64": "darwin-arm64", "windows-x64": "win32-x64-archive", "windows-arm64": "win32-arm64-archive",
}


def read_json(url):
    headers = {"User-Agent": "codex-ua-collector"}
    if url.startswith("https://api.github.com/"):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        elif shutil.which("gh"):
            result = subprocess.run(["gh", "api", url], check=True, capture_output=True, text=True, timeout=60)
            return json.loads(result.stdout)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


@dataclass(frozen=True)
class TerminalRelease:
    version: str
    source_url: str
    assets: dict[str, str]

    @classmethod
    def parse(cls, profile, value):
        if profile not in APPLICATIONS:
            raise ValueError(f"unknown terminal profile: {profile}")
        if not isinstance(value, dict) or set(value) != {"version", "source_url", "assets"}:
            raise ValueError(f"{profile}: expected version, source_url and assets")
        version = value["version"]
        parts = 4 if profile == "WindowsTerminal" else 3
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){" + str(parts - 1) + "}", version):
            raise ValueError(f"{profile}: expected a stable release version")
        repository = {"WindowsTerminal": "microsoft/terminal", "herdr": "herdrdev/herdr"}.get(profile)
        expected_source = (f"https://github.com/{repository}/releases/tag/v{version}" if repository
                           else f"https://code.visualstudio.com/updates/v{version.rsplit('.', 1)[0].replace('.', '_')}")
        if value["source_url"] != expected_source:
            raise ValueError(f"{profile}: unexpected official release URL")
        assets = value["assets"]
        if not isinstance(assets, dict) or not assets:
            raise ValueError(f"{profile}: no native assets")
        for target, url in assets.items():
            if not isinstance(target, str) or not isinstance(url, str):
                raise ValueError(f"{profile}: invalid asset")
            if not re.fullmatch(r"(?:linux|macos|windows)-(?:x64|arm64)", target):
                raise ValueError(f"{profile}: unsupported native target")
            if profile == "WindowsTerminal" and not target.startswith("windows-"):
                raise ValueError("WindowsTerminal: expected Windows native assets")
            prefix = (f"https://github.com/{repository}/releases/download/v{version}/" if repository
                      else f"https://update.code.visualstudio.com/{version}/")
            family, architecture = target.split("-")
            native = {"x64": "x86_64", "arm64": "aarch64"}[architecture]
            if profile == "WindowsTerminal":
                name = f"Microsoft.WindowsTerminal_{version}_{architecture}.zip"
            elif profile == "herdr":
                name = f"herdr-{family}-{native}" + (".zip" if family == "windows" else "")
            else:
                name = f"{VS_CODE_PACKAGES[target]}/stable"
            if url != prefix + name:
                raise ValueError(f"{profile}: asset is not from the selected official stable release and native target")
        return cls(version, expected_source, dict(assets))

    def to_dict(self):
        return {"version": self.version, "source_url": self.source_url, "assets": self.assets}


def parse_releases(value):
    if not isinstance(value, dict) or set(value) != set(APPLICATIONS):
        raise ValueError("terminal releases: expected WindowsTerminal, vscode and herdr")
    return {profile: TerminalRelease.parse(profile, value[profile]) for profile in APPLICATIONS}


def resolve_releases():
    releases = {}
    for profile, repository in (("WindowsTerminal", "microsoft/terminal"), ("herdr", "herdrdev/herdr")):
        release = read_json(f"https://api.github.com/repos/{repository}/releases/latest")
        if release.get("draft") or release.get("prerelease"):
            raise ValueError(f"{profile}: upstream latest is not stable")
        version = release["tag_name"].removeprefix("v")
        available = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]
                     if asset.get("state") == "uploaded" and asset.get("size", 0) > 0}
        assets = {}
        for family in (("windows",) if profile == "WindowsTerminal" else ("linux", "macos", "windows")):
            for architecture, native in (("x64", "x86_64"), ("arm64", "aarch64")):
                name = (f"Microsoft.WindowsTerminal_{version}_{architecture}.zip" if profile == "WindowsTerminal"
                        else f"herdr-{family}-{native}" + (".zip" if family == "windows" else ""))
                if name in available:
                    assets[f"{family}-{architecture}"] = available[name]
                elif not (profile == "herdr" and family == "windows" and architecture == "arm64"):
                    raise ValueError(f"{profile}: stable release is missing {name}")
        releases[profile] = {"version": version, "source_url": release["html_url"], "assets": assets}
    vscode = read_json("https://update.code.visualstudio.com/api/update/linux-x64/stable/latest")
    version = vscode["productVersion"]
    releases["vscode"] = {
        "version": version,
        "source_url": f"https://code.visualstudio.com/updates/v{version.rsplit('.', 1)[0].replace('.', '_')}",
        "assets": {target: f"https://update.code.visualstudio.com/{version}/{package}/stable" for target, package in VS_CODE_PACKAGES.items()},
    }
    return {profile: release.to_dict() for profile, release in parse_releases(releases).items()}


def native_target(platform):
    if not re.fullmatch(r"(?:linux-(?:ubuntu|debian|fedora|alpine)|macos|windows)-(?:x64|arm64)", platform):
        raise ValueError(f"unsupported collection platform: {platform}")
    return f"{platform.split('-')[0]}-{platform.rsplit('-', 1)[1]}"


def unsupported_reason(profile, platform, release, container_image, runner_image):
    target = native_target(platform)
    if profile == "WindowsTerminal" and not platform.startswith("windows-"):
        return "Windows Terminal has no native release for this operating system."
    if profile == "vscode":
        if container_image:
            return "Microsoft does not support the full VS Code desktop in containers."
        if platform.startswith("windows-") and runner_image.lower().startswith("win25"):
            return "Microsoft does not support VS Code on Windows Server."
        if platform.startswith("linux-alpine-"):
            return "The VS Code desktop requires glibc; Alpine uses musl."
    if target not in release.assets:
        return "The selected stable release has no native binary for this architecture."
    return None


def install(profile, release, platform, directory):
    directory.mkdir()
    url = release.assets[native_target(platform)]
    archive = directory / "download"
    request = urllib.request.Request(url, headers={"User-Agent": "codex-ua-collector"})
    with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as output:
        shutil.copyfileobj(response, output)
    if profile == "herdr" and not platform.startswith("windows-"):
        binary = directory / "herdr"
        archive.rename(binary)
    else:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as source:
                # Official archives may contain directories, but never escape the installation.
                for member in source.infolist():
                    destination = (directory / member.filename).resolve()
                    if not destination.is_relative_to(directory.resolve()):
                        raise ValueError("terminal archive contains a path outside its installation")
                if sys.platform == "darwin":
                    subprocess.run(["ditto", "-xk", str(archive), str(directory)], check=True, timeout=120)
                else:
                    source.extractall(directory)
                if os.name != "nt":
                    for member in source.infolist():
                        mode = member.external_attr >> 16
                        if mode & 0o111:
                            (directory / member.filename).chmod(mode & 0o777)
        else:
            with tarfile.open(archive) as source:
                source.extractall(directory, filter="data")
        names = ({"WindowsTerminal": "WindowsTerminal.exe", "herdr": "herdr.exe"} if os.name == "nt"
                 else {"vscode": "code"})
        name = names.get(profile, "Code.exe")
        candidates = [path for path in directory.rglob(name) if path.is_file()]
        if profile == "vscode" and sys.platform == "darwin":
            contents = directory / "Visual Studio Code.app" / "Contents"
            with (contents / "Info.plist").open("rb") as source:
                name = plistlib.load(source)["CFBundleExecutable"]
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("vscode: invalid native executable in the application bundle")
            expected = contents / "MacOS" / name
            candidates = [expected] if expected.is_file() else []
        elif profile == "vscode" and os.name != "nt":
            expected = directory / ("VSCode-linux-" + platform.rsplit("-", 1)[1]) / "code"
            candidates = [path for path in candidates if path == expected]
        if len(candidates) != 1:
            raise ValueError(f"{profile}: expected exactly one native {name}")
        binary = candidates[0]
        archive.unlink()
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | 0o111)
    if profile == "WindowsTerminal":
        (binary.parent / ".portable").touch()
        settings = binary.parent / "settings"
        settings.mkdir()
        (settings / "settings.json").write_text(json.dumps({"profiles": {"defaults": {"closeOnExit": "always"}}}), encoding="utf-8")
    return binary


@contextmanager
def virtual_display(directory, env):
    """An authenticated X server supplies a real GUI without a desktop session."""
    if not sys.platform.startswith("linux"):
        yield env
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    display = port - 6000
    authority = directory / "xauthority"
    subprocess.run(["xauth", "-f", str(authority), "add", f"127.0.0.1:{display}", "MIT-MAGIC-COOKIE-1", uuid.uuid4().hex],
                   check=True, capture_output=True, timeout=10)
    display_env = dict(env, DISPLAY=f"127.0.0.1:{display}", XAUTHORITY=str(authority))
    with (directory / "display.log").open("w") as log:
        process = subprocess.Popen(["Xvfb", f":{display}", "-screen", "0", "1280x800x24", "-nolisten", "unix",
                                    "-nolisten", "local", "-listen", "tcp", "-auth", str(authority)],
                                   env=display_env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    diagnostic = (directory / "display.log").read_text(errors="replace")[-8000:]
                    raise RuntimeError(f"virtual display did not start\n{diagnostic}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            yield display_env
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def isolated_environment(directory):
    """Sanitize before starting the application; preserve its complete child environment afterward."""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "LANG"}
    env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    home = directory / "home"
    home.mkdir()
    temporary = directory / "tmp"
    temporary.mkdir()
    env.update({"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"), "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"), "TMPDIR": str(temporary),
                "TEMP": str(temporary), "TMP": str(temporary)})
    return env


def run_in_terminal(profile, release, binary, platform, sampling_command, directory):
    """Execute argv in a real PTY and return its exit status and terminal context.

    The sampling command inherits the complete application-generated environment
    and stdin/stdout/stderr. It must not sanitize or synthesize terminal identity.
    UA/request capture remains the sampling command's responsibility. Use a short
    temporary directory on Unix because Herdr's session sockets have a path limit.
    """
    if profile not in APPLICATIONS or not sampling_command or not all(isinstance(arg, str) and arg for arg in sampling_command):
        raise ValueError("expected a supported terminal profile and a nonempty command argv")
    native_target(platform)
    binary = binary.resolve()
    directory = directory.resolve()
    directory.mkdir()
    print(f"Launching {profile} {release.version} on {platform}", flush=True)
    result_path = directory / "result.json"
    configuration = directory / "probe.json"
    configuration.write_text(json.dumps({"command": sampling_command, "cwd": str(directory), "output": str(result_path),
                                          "herdr": str(binary) if profile == "herdr" else None}), encoding="utf-8")
    helper = Path(__file__).with_name("probe.py").resolve()
    launch_env = isolated_environment(directory)
    launch_env["CODEX_UA_PROBE_CONFIG"] = str(configuration)
    if profile == "herdr":
        actual = subprocess.run([str(binary), "--version"], env=launch_env, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        if actual != f"herdr {release.version}":
            raise ValueError(f"herdr: expected {release.version}, received {actual!r}")
        config = directory / "herdr.toml"
        config.write_text('onboarding = false\n[terminal]\ndefault_shell = ' + json.dumps(sys.executable)
                          + '\nshell_mode = "non_login"\n[update]\nversion_check = false\nmanifest_check = false\n', encoding="utf-8")
        launch_env.update({"HERDR_CONFIG_PATH": str(config), "HERDR_STARTUP_CWD": str(directory), "PYTHONSTARTUP": str(helper)})
        command = [str(binary), "--session", "ua-" + uuid.uuid4().hex[:8], "server"]
    elif profile == "vscode":
        extension = directory / "extension"
        extension.mkdir()
        (extension / "package.json").write_text(json.dumps({"name": "codex-ua-probe", "publisher": "codex-ua", "version": "0.0.1",
            "engines": {"vscode": "^1.90.0"}, "activationEvents": ["*"], "main": "index.js"}), encoding="utf-8")
        (extension / "index.js").write_text(
            "const vscode = require('vscode'); const fs = require('fs');\n"
            "exports.activate = () => {\n"
            f"  fs.writeFileSync({json.dumps(str(directory / 'version.txt'))}, vscode.version);\n"
            f"  vscode.window.createTerminal({{name:'Codex UA probe', shellPath:{json.dumps(sys.executable)}, shellArgs:{json.dumps(['-u', str(helper)])}}}).show();\n"
            f"  const timer = setInterval(() => {{ if (fs.existsSync({json.dumps(str(result_path))})) {{ clearInterval(timer); vscode.commands.executeCommand('workbench.action.quit'); }} }}, 100);\n"
            "};\n", encoding="utf-8")
        command = [str(binary), "--disable-gpu", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
                   "--user-data-dir", str(directory / "user"), "--extensions-dir", str(directory / "extensions"),
                   "--extensionDevelopmentPath=" + str(extension), str(directory)]
        if sys.platform.startswith("linux"):
            command.append("--no-sandbox")
    else:
        command = [str(binary), "-w", "new", "new-tab", "--inheritEnvironment", "--startingDirectory", str(directory),
                   sys.executable, "-u", str(helper)]
    if profile == "vscode":
        with virtual_display(directory, launch_env) as display_env:
            result = wait_probe(profile, command, directory, display_env, result_path)
        if (directory / "version.txt").read_text() != release.version:
            raise RuntimeError("vscode: running application differs from selected stable version")
    else:
        result = wait_probe(profile, command, directory, launch_env, result_path)
    return {"application": {"version": release.version, "source_url": release.assets[native_target(platform)]},
            "launch_method": LAUNCH_METHODS[profile], **result}


def collect_profiles(codex_binary, platform, releases, directory):
    """Capture both native Codex clients in each supported terminal application."""
    observations = {}
    for profile, release in releases.items():
        reason = unsupported_reason(profile, platform, release, os.environ.get("COLLECT_CONTAINER_IMAGE", ""),
                                    os.environ.get("ImageOS", ""))
        if reason:
            observations[profile] = {"status": "unsupported", "reason": reason}
            continue
        binary = install(profile, release, platform, directory / (profile + "-app"))
        output = directory / (profile + "-clients.json")
        command = [sys.executable, str(Path(__file__).resolve()), "sample", "--binary", str(codex_binary.resolve()),
                   "--output", str(output.resolve())]
        with tempfile.TemporaryDirectory(prefix="cu-", dir="/tmp" if os.name != "nt" else None) as temporary:
            context = run_in_terminal(profile, release, binary, platform, command, Path(temporary) / "pty")
        clients = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
        if context["exit_code"] or "error" in clients:
            raise RuntimeError(f"{profile}: native capture failed ({context['exit_code']}): {clients.get('error', '')}")
        observations[profile] = {"status": "collected", "application": context["application"],
                                 "launch_method": context["launch_method"], "terminal": context["environment"],
                                 "tty": context["tty"], "clients": clients}
    return observations


def wait_probe(profile, command, directory, launch_env, result_path):
    with (directory / "launcher.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=launch_env, cwd=directory, stdout=log, stderr=log)
        try:
            startup_deadline = time.monotonic() + 90
            deadline = time.monotonic() + 690
            started = False
            while not result_path.exists():
                if not started and result_path.with_name("started.json").exists():
                    started = True
                    print(f"{profile}: sampling started in the application's PTY", flush=True)
                if not started and time.monotonic() >= startup_deadline:
                    raise RuntimeError(f"{profile}: terminal did not start the probe within 90 seconds")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{profile}: terminal probe timed out; see {directory / 'launcher.log'}")
                # Windows Terminal's launcher may exit while its window runs the helper.
                if process.poll() is not None and process.returncode != 0:
                    raise RuntimeError(f"{profile}: launcher failed ({process.returncode}); see {directory / 'launcher.log'}")
                time.sleep(0.1)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if "error" in result:
                raise RuntimeError(f"{profile}: {result['error']}")
            if result["tty"] != [True, True, True]:
                raise RuntimeError(f"{profile}: probe did not run in a terminal-owned PTY")
            print(f"{profile}: sampling completed with exit status {result['exit_code']}", flush=True)
            process.wait(timeout=30)
        except Exception as error:
            log.flush()
            diagnostic = (directory / "launcher.log").read_text(encoding="utf-8", errors="replace")[-8000:]
            raise RuntimeError(f"{error}\n{diagnostic}") from error
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if profile == "vscode" and os.name == "nt":
                # Electron can leave helpers alive after its main process exits.
                # Select only executables in this probe's temporary installation.
                cleanup = r"""
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($env:CODEX_UA_APP_DIRECTORY).TrimEnd('\') + '\'
$owned = @(Get-Process | Where-Object {
    $_.Path -and $_.Path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)
})
$owned | Stop-Process -Force -ErrorAction SilentlyContinue
foreach ($process in $owned) {
    if (-not $process.WaitForExit(10000)) { throw 'VS Code helper did not exit' }
}
"""
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cleanup],
                               env=dict(os.environ, CODEX_UA_APP_DIRECTORY=str(Path(command[0]).parent)),
                               check=True, capture_output=True, text=True, timeout=60)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    resolve = commands.add_parser("resolve", help="resolve official latest stable releases once per collection run")
    resolve.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run", help="download and run a sampling command inside a native terminal")
    run.add_argument("--profile", required=True, choices=APPLICATIONS)
    run.add_argument("--platform", required=True)
    run.add_argument("--releases", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--container-image", default=os.environ.get("COLLECT_CONTAINER_IMAGE", ""))
    run.add_argument("--runner-image", default=os.environ.get("ImageOS", ""))
    run.add_argument("command", nargs=argparse.REMAINDER, help="sampling command argv after --")
    sample = commands.add_parser("sample", help="capture real CLI and Exec requests inside an already running terminal")
    sample.add_argument("--binary", required=True, type=Path)
    sample.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "resolve":
        value = resolve_releases()
    elif args.action == "sample":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            from .collect import capture_client
        except ImportError:
            from collect import capture_client
        try:
            with tempfile.TemporaryDirectory(prefix="capture-") as temporary:
                value = {}
                for mode in ("CLI", "Exec"):
                    capture = capture_client(args.binary, mode, Path(temporary) / mode.lower(), native_terminal=True)
                    value[mode] = {"user_agent": capture["user_agent"], "method": "http-capture", "http_capture": capture}
        except Exception:
            import traceback
            args.output.write_text(json.dumps({"error": traceback.format_exc()}), encoding="utf-8")
            raise
    else:
        release = parse_releases(json.loads(args.releases.read_text(encoding="utf-8")))[args.profile]
        reason = unsupported_reason(args.profile, args.platform, release, args.container_image, args.runner_image)
        if reason:
            value = {"unsupported": reason}
        else:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                parser.error("a sampling command is required after --")
            with tempfile.TemporaryDirectory(prefix="cu-", dir="/tmp" if os.name != "nt" else None) as temporary:
                directory = Path(temporary)
                binary = install(args.profile, release, args.platform, directory / "app")
                value = run_in_terminal(args.profile, release, binary, args.platform, command, directory / "probe")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if value.get("exit_code", 0):
        raise SystemExit(value["exit_code"])


if __name__ == "__main__":
    main()

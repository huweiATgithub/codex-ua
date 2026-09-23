#!/usr/bin/env python3
"""Collect CLI User-Agents from one native official Codex release binary."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request


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
TERMINAL = {"TERM": "xterm-256color"}
PROCESS_TIMEOUT = 90


def stable_version(value):
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise argparse.ArgumentTypeError("expected a stable Codex version such as 0.156.1")
    return value


def native_platform():
    systems = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
    architectures = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    system = platform.system()
    machine = platform.machine().lower()
    if system not in systems or machine not in architectures:
        raise RuntimeError(f"unsupported native platform: {system} {machine}")
    family = systems[system]
    if system == "Linux":
        family = f"linux-{platform.freedesktop_os_release()['ID']}"
    name = f"{family}-{architectures[machine]}"
    if name not in TARGETS:
        raise RuntimeError(f"unsupported native platform: {name}")
    return name


def source_url(version, platform_name):
    target = TARGETS[platform_name]
    suffix = ".exe" if platform_name.startswith("windows-") else ""
    return f"https://github.com/openai/codex/releases/download/rust-v{version}/codex-{target}{suffix}.tar.gz"


def download_binary(url, platform_name, directory):
    archive_path = directory / "codex.tar.gz"
    request = urllib.request.Request(url, headers={"User-Agent": "codex-ua-collector"})
    with urllib.request.urlopen(request, timeout=60) as response, archive_path.open("wb") as output:
        shutil.copyfileobj(response, output)
    suffix = ".exe" if platform_name.startswith("windows-") else ""
    expected = f"codex-{TARGETS[platform_name]}{suffix}"
    # Copy only the expected regular file; never extract archive-provided paths.
    with tarfile.open(archive_path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile() and Path(member.name).name == expected]
        if len(members) != 1:
            raise RuntimeError(f"release archive must contain exactly one {expected}")
        binary = directory / f"codex{suffix}"
        with archive.extractfile(members[0]) as source, binary.open("wb") as output:
            shutil.copyfileobj(source, output)
    binary.chmod(0o755)
    return binary


def child_environment(directory):
    # An allowlist prevents credentials, identity overrides, proxies and terminal
    # detection variables from leaking into the measured process.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE"}
    env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    home = directory / "home"
    codex_home = directory / "codex-home"
    temporary = directory / "tmp"
    for path in (home, codex_home, temporary):
        path.mkdir()
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "CODEX_HOME": str(codex_home),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        **TERMINAL,
    })
    # Both app-server and exec read these settings. No model turn is needed for
    # initialization, and exec receives its loopback-only provider separately.
    (codex_home / "config.toml").write_text(
        "check_for_update_on_startup = false\n"
        "cli_auth_credentials_store = 'file'\n"
        "web_search = 'disabled'\n"
        "[analytics]\nenabled = false\n"
        "[feedback]\nenabled = false\n"
        "[otel]\nexporter = 'none'\ntrace_exporter = 'none'\nmetrics_exporter = 'none'\n",
        encoding="utf-8",
    )
    return env


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def initialize_user_agent(binary, version, client_name, directory):
    directory.mkdir()
    env = child_environment(directory)
    request = {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": client_name, "version": version}}}
    lines = queue.Queue()
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [str(binary), "app-server", "--listen", "stdio://"],
            cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr, text=True, encoding="utf-8", errors="replace",
        )

        def read_stdout():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + PROCESS_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("app-server initialization timed out")
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty as error:
                    raise RuntimeError("app-server initialization timed out") from error
                if line is None:
                    raise RuntimeError("app-server exited before initialization responded")
                response = json.loads(line)
                if not isinstance(response, dict) or response.get("id") != 1:
                    continue
                if "error" in response:
                    raise RuntimeError(f"app-server initialization failed: {response['error']}")
                user_agent = response.get("result", {}).get("userAgent")
                if not isinstance(user_agent, str) or not user_agent:
                    raise RuntimeError("app-server returned no User-Agent")
                process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
                process.stdin.flush()
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    stop_process(process)
                return user_agent
        except Exception as error:
            stop_process(process)
            stderr.seek(0)
            raise RuntimeError(f"{client_name}: {error}\n{stderr.read()[-8000:]}") from error
        finally:
            stop_process(process)
            reader.join(timeout=5)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()


def response_events():
    events = [
        {"type": "response.created", "response": {"id": "ua-capture"}},
        {"type": "response.output_item.done", "item": {
            "type": "message", "role": "assistant", "id": "ua-message",
            "content": [{"type": "output_text", "text": "UA capture complete."}],
        }},
        {"type": "response.completed", "response": {
            "id": "ua-capture", "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }},
    ]
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def capture_exec(binary, composed_user_agent, directory):
    directory.mkdir()
    env = child_environment(directory)
    captures = []
    unexpected = []
    body = response_events()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path != "/v1/responses":
                unexpected.append(f"POST {self.path}")
                self.send_error(404)
                return
            captures.append({
                "user_agent": self.headers.get("User-Agent", ""),
                "originator": self.headers.get("originator", ""),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            unexpected.append(f"GET {self.path}")
            self.send_error(404)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = (
            'model_providers.ua_capture={name="UA capture",'
            f'base_url="http://127.0.0.1:{server.server_port}/v1",'
            'wire_api="responses",requires_openai_auth=false,supports_websockets=false,'
            'request_max_retries=0,stream_max_retries=0,stream_idle_timeout_ms=10000}'
        )
        command = [
            str(binary), "exec", "--skip-git-repo-check", "--ephemeral",
            "--sandbox", "read-only", "--color", "never",
            "-c", 'model_provider="ua_capture"', "-c", provider,
            "-c", 'model="ua-capture"',
            "-c", "features.enable_request_compression=false",
            "Reply with the text: UA capture complete.",
        ]
        try:
            result = subprocess.run(
                command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=PROCESS_TIMEOUT,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f"codex exec failed ({result.returncode}):\n{result.stderr[-8000:]}")
    if unexpected or len(captures) != 1:
        raise RuntimeError(f"expected one Responses request; captured={captures}, unexpected={unexpected}")
    capture = captures[0]
    if capture["user_agent"] != composed_user_agent:
        raise RuntimeError(f"exec HTTP User-Agent differs from app-server: {capture['user_agent']!r} != {composed_user_agent!r}")
    if capture["originator"] != "codex_exec":
        raise RuntimeError(f"unexpected exec originator: {capture['originator']!r}")
    return capture


def collect(version, platform_name, supplied_binary=None):
    actual = native_platform()
    if platform_name != actual:
        raise RuntimeError(f"requested {platform_name}, but this process runs on {actual}")
    url = source_url(version, platform_name)
    with tempfile.TemporaryDirectory(prefix="codex-ua-") as temporary:
        directory = Path(temporary)
        binary = supplied_binary.resolve() if supplied_binary else download_binary(url, platform_name, directory)
        check_directory = directory / "version-check"
        check_directory.mkdir()
        result = subprocess.run(
            [str(binary), "--version"], cwd=check_directory, env=child_environment(check_directory),
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
        )
        if result.stdout.strip() != f"codex-cli {version}":
            raise RuntimeError(f"binary version mismatch: requested {version}, received {result.stdout.strip()!r}")
        interactive = initialize_user_agent(binary, version, "codex-tui", directory / "interactive")
        exec_ua = initialize_user_agent(binary, version, "codex_exec", directory / "exec-init")
        capture = capture_exec(binary, exec_ua, directory / "exec-capture")
    return {
        "schema_version": 1,
        "codex_version": version,
        "platform": platform_name,
        "target": TARGETS[platform_name],
        "source_url": url,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "os": {
            "system": platform.system(), "release": platform.release(),
            "version": platform.version(), "machine": platform.machine(),
            "distribution": {
                key.lower(): platform.freedesktop_os_release()[key]
                for key in ("ID", "VERSION_ID", "PRETTY_NAME")
            } if platform_name.startswith("linux-") else None,
        },
        "runner": {
            "name": os.environ.get("RUNNER_NAME", "local"),
            "image": os.environ.get("ImageOS", "unknown"),
            "image_version": os.environ.get("ImageVersion", "unknown"),
            "container_image": os.environ.get("COLLECT_CONTAINER_IMAGE") or None,
        },
        "terminal": TERMINAL,
        "clients": {
            "interactive": {"user_agent": interactive, "method": "app-server-initialize"},
            "exec": {"user_agent": exec_ua, "method": "app-server-initialize", "http_capture": capture},
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, type=stable_version)
    parser.add_argument("--platform", required=True, choices=TARGETS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--binary", type=Path, help="use an existing binary for local smoke testing")
    args = parser.parse_args()
    observation = collect(args.version, args.platform, args.binary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    print(f"Collected Codex {args.version} on {args.platform}: {args.output}")


if __name__ == "__main__":
    main()

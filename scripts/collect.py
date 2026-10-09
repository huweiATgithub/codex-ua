#!/usr/bin/env python3
"""Collect CLI User-Agents from one native official Codex release binary."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
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


def child_environment(directory, *, native_terminal=False):
    # Baseline sampling starts clean. Native applications were isolated before
    # launch, so their complete terminal environment must reach Codex.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE"}
    env = dict(os.environ) if native_terminal else {name: value for name, value in os.environ.items() if name.upper() in allowed}
    home = directory / "home"
    codex_home = directory / "codex-home"
    temporary = directory / "tmp"
    for path in (home, codex_home, temporary):
        path.mkdir()
    isolated = {
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
    }
    if native_terminal:
        # The application was isolated before launch. Preserve every signal it
        # supplied, including future terminal detection mechanisms.
        env["CODEX_HOME"] = str(codex_home)
    else:
        env.update(isolated)
    # Both clients receive a loopback-only provider separately.
    (codex_home / "config.toml").write_text(
        "check_for_update_on_startup = false\n"
        "cli_auth_credentials_store = 'file'\n"
        "web_search = 'disabled'\n"
        "[features]\nplugins = false\n"
        "[analytics]\nenabled = false\n"
        "[feedback]\nenabled = false\n"
        "[otel]\nexporter = 'none'\ntrace_exporter = 'none'\nmetrics_exporter = 'none'\n"
        # Older TUIs read directory trust from the home config, not CLI overrides.
        f"[projects.{json.dumps(str(directory.resolve()))}]\ntrust_level = 'trusted'\n",
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


@contextmanager
def tui_terminal(command, directory, env):
    if os.name == "nt":
        from winpty import PtyProcess
        from winpty.enums import Backend

        # A string keeps ConPTY's zero enum value from selecting an env override.
        process = PtyProcess.spawn(
            command, cwd=str(directory), env=env, dimensions=(40, 120),
            backend=str(Backend.ConPTY),
        )
        try:
            yield process.read, process.write
        finally:
            try:
                if process.isalive():
                    # The TUI can own an app-server and other child processes.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        env=env, capture_output=True, timeout=10, check=True,
                    )
            finally:
                process.close(force=True)
    else:
        import pty
        import termios

        master, slave = pty.openpty()
        process = None
        try:
            termios.tcsetwinsize(slave, (40, 120))
            process = subprocess.Popen(
                command, cwd=directory, env=env, stdin=slave, stdout=slave,
                stderr=slave, start_new_session=True,
            )
            os.close(slave)
            slave = None

            def read():
                try:
                    return os.read(master, 65536).decode("utf-8", errors="replace")
                except OSError as error:
                    if error.errno == errno.EIO:
                        return ""
                    raise

            def write(value):
                os.write(master, value.encode("utf-8"))

            yield read, write
        finally:
            if process is not None:
                stop_process(process)
            if slave is not None:
                os.close(slave)
            os.close(master)


def run_tui(command, directory, env, response_sent, *, native_terminal=False):
    # Fresh homes cannot reuse a daemon. Disable starting one where supported.
    help_result = subprocess.run(
        [command[0], "--help"], cwd=directory, env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=30, check=True,
    )
    if "--no-daemon" in help_result.stdout:
        command.insert(1, "--no-daemon")
    if native_terminal:
        attributes = None
        if os.name != "nt":
            import termios
            attributes = termios.tcgetattr(0)
        process = subprocess.Popen(command, cwd=directory, env=env)
        try:
            deadline = time.monotonic() + PROCESS_TIMEOUT
            while not response_sent.wait(0.1):
                if process.poll() is not None:
                    raise RuntimeError(f"Codex TUI exited ({process.returncode}) before completing a Responses request")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Codex TUI timed out before completing a Responses request")
        finally:
            if os.name == "nt" and process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               env=env, capture_output=True, timeout=10, check=True)
                process.wait(timeout=10)
            else:
                stop_process(process)
            if attributes is not None:
                termios.tcsetattr(0, termios.TCSANOW, attributes)
        return
    output = queue.Queue()
    transcript = ""
    pending = ""
    reader = None
    try:
        with tui_terminal(command, directory, env) as (read, write):
            def read_output():
                try:
                    while chunk := read():
                        output.put(chunk)
                except (EOFError, OSError):
                    pass
                finally:
                    output.put(None)

            def answer_query(match):
                write("\x1b[1;1R" if match[0] == "\x1b[6n" else "\x1b[?1;2c")
                return ""

            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            deadline = time.monotonic() + PROCESS_TIMEOUT
            while not response_sent.is_set():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Codex TUI timed out before completing a Responses request")
                try:
                    chunk = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if chunk is None:
                    raise RuntimeError("Codex TUI exited before completing a Responses request")
                transcript = (transcript + chunk)[-8000:]
                pending += chunk

                # Answer cursor-position and device queries, including split reads.
                pending = re.sub(r"\x1b\[(?:6n|c)", answer_query, pending)[-4:]
    except Exception as error:
        raise RuntimeError(f"{error}\n{transcript}") from error
    finally:
        if reader is not None:
            reader.join(timeout=5)


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


def capture_client(binary, mode, directory, *, native_terminal=False):
    if mode not in ("CLI", "Exec"):
        raise ValueError(f"unsupported client mode: {mode}")
    if native_terminal and not all(os.isatty(fd) for fd in (0, 1, 2)):
        raise RuntimeError("native terminal capture requires the application's real PTY")
    directory.mkdir()
    env = child_environment(directory, native_terminal=native_terminal)
    captures = []
    unexpected = []
    response_sent = threading.Event()
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
            response_sent.set()

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
        command = [str(binary)]
        if mode == "CLI":
            command.extend(["--no-alt-screen", "--ask-for-approval", "never"])
        else:
            command.extend(["exec", "--skip-git-repo-check", "--ephemeral", "--color", "never"])
        command.extend([
            "--sandbox", "read-only",
            "-c", 'model_provider="ua_capture"', "-c", provider,
            "-c", 'model="ua-capture"',
            "-c", "features.enable_request_compression=false",
            "Reply with the text: UA capture complete.",
        ])
        try:
            if mode == "CLI":
                run_tui(command, directory, env, response_sent, native_terminal=native_terminal)
            elif native_terminal:
                subprocess.run(command, cwd=directory, env=env, timeout=PROCESS_TIMEOUT, check=True)
            else:
                result = subprocess.run(
                    command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=PROCESS_TIMEOUT,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"codex exec failed ({result.returncode}):\n{result.stderr[-8000:]}")
        finally:
            server.shutdown()
            thread.join(timeout=5)
    if unexpected or not captures:
        raise RuntimeError(f"expected Responses requests; captured={captures}, unexpected={unexpected}")
    capture = captures[0]
    if any(item != capture for item in captures[1:]):
        raise RuntimeError(f"Responses requests contain inconsistent headers: {captures}")
    if not capture["user_agent"]:
        raise RuntimeError(f"{mode} request contains no User-Agent")
    identity = "codex-tui" if mode == "CLI" else "codex_exec"
    if capture["originator"] != identity:
        raise RuntimeError(f"unexpected {mode} originator: {capture['originator']!r}")
    return capture


def collect(version, platform_name, supplied_binary=None, terminal_releases=None):
    actual = native_platform()
    if platform_name != actual:
        raise RuntimeError(f"requested {platform_name}, but this process runs on {actual}")
    selected = None
    if terminal_releases is not None:
        try:
            from .terminals import collect_profiles, parse_releases
        except ImportError:
            from terminals import collect_profiles, parse_releases
        selected = parse_releases(terminal_releases)
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
        captures = {
            mode: capture_client(binary, mode, directory / mode.lower())
            for mode in ("CLI", "Exec")
        }
        profiles = None
        if selected is not None:
            profiles = collect_profiles(binary, platform_name, selected, directory)
    result = {
        "schema_version": 3 if profiles is not None else 2,
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
            mode: {"user_agent": capture["user_agent"], "method": "http-capture", "http_capture": capture}
            for mode, capture in captures.items()
        },
    }
    if profiles is not None:
        result.update({"terminal_releases": {name: release.to_dict() for name, release in selected.items()}, "profiles": profiles})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, type=stable_version)
    parser.add_argument("--platform", required=True, choices=TARGETS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--binary", type=Path, help="use an existing binary for local smoke testing")
    parser.add_argument("--terminal-releases", type=Path, help="fixed official stable terminal release snapshot")
    args = parser.parse_args()
    selected = json.loads(args.terminal_releases.read_text(encoding="utf-8")) if args.terminal_releases else None
    observation = collect(args.version, args.platform, args.binary, selected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    print(f"Collected Codex {args.version} on {args.platform}: {args.output}")


if __name__ == "__main__":
    main()

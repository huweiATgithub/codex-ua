#!/usr/bin/env python3
"""Test client that requires a terminal and supplies its own HTTP identity."""

import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request


if "--help" in sys.argv:
    print("--no-daemon")
    sys.exit(0)
if "--version" in sys.argv:
    print("codex-cli 0.156.1")
    sys.exit(0)

assert "app-server" not in sys.argv
assert not any("clientInfo" in arg or "codex-tui" in arg for arg in sys.argv)
interactive = "exec" not in sys.argv
if interactive:
    import tty

    assert all(os.isatty(fd) for fd in (0, 1, 2))
    assert "--no-daemon" in sys.argv
    tty.setraw(0)
    os.write(1, b"\x1b[")
    time.sleep(0.02)
    os.write(1, b"6n")
    answer = b""
    while not answer.endswith(b"R"):
        answer += os.read(0, 1)
    assert answer == b"\x1b[1;1R"
    Path("pid").write_text(str(os.getpid()))

identity = "codex-tui" if interactive else "codex_exec"
ua = f"{identity}/0.156.1 (Measured OS 7; x86_64) xterm-256color ({identity}; 0.156.1)"
headers_file = Path(__file__).with_name("headers.json")
headers = (json.loads(headers_file.read_text()) if headers_file.exists()
           else {"User-Agent": ua, "originator": identity})
provider = next(arg for arg in sys.argv if arg.startswith("model_providers.ua_capture="))
if Path(__file__).with_name("hold-request").exists():
    while True:
        time.sleep(1)
url = re.search(r'base_url="([^"]+)"', provider)[1] + "/responses"
for request_headers in headers if isinstance(headers, list) else [headers]:
    with urllib.request.urlopen(urllib.request.Request(url, data=b"{}", headers=request_headers)) as response:
        response.read()
if interactive:
    while True:
        time.sleep(1)

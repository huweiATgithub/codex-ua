#!/usr/bin/env python3
"""Run a sampling command inside the terminal-owned PTY without changing its environment."""

import json
import os
from pathlib import Path
import subprocess
import sys
import traceback


def main():
    config_path = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--config" else os.environ["CODEX_UA_PROBE_CONFIG"]
    configuration = json.loads(Path(config_path).read_text(encoding="utf-8"))
    output = Path(configuration["output"])
    try:
        if not all(os.isatty(fd) for fd in (0, 1, 2)):
            raise RuntimeError("the probe must run inside a real terminal PTY")
        output.with_name("started.json").write_text(json.dumps({"tty": [True, True, True]}), encoding="utf-8")
        completed = subprocess.run(configuration["command"], cwd=configuration["cwd"], timeout=600)
        result = {
            "exit_code": completed.returncode, "tty": [True, True, True],
            "environment": {name: value for name, value in os.environ.items()
                            if name.startswith(("TERM", "WT_", "HERDR_"))},
        }
    except Exception:
        result = {"error": traceback.format_exc()}
    temporary_output = output.with_suffix(".tmp")
    temporary_output.write_text(json.dumps(result), encoding="utf-8")
    temporary_output.replace(output)
    if configuration["herdr"]:
        # The helper is inside the named test session; stop only its inherited server.
        if os.environ.get("HERDR_ENV") != "1":
            raise RuntimeError("Herdr did not establish a managed pane")
        subprocess.run([configuration["herdr"], "server", "stop"], check=True, timeout=10)
    raise SystemExit(0 if "error" not in result else 1)


if __name__ == "__main__":
    main()

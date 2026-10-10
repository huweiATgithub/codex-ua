# Codex UA matrices

Collect User-Agent samples from official Codex CLI binaries on Ubuntu, Debian,
Fedora, Alpine, macOS, and Windows, on x64 and ARM64. Each matrix contains
CLI and Exec client profiles for one stable Codex version. These are
samples of the recorded runtime environments, not an exhaustive list of possible
Codex User-Agents.

The terminal Profiles are `xterm-256color`, `WindowsTerminal`, `vscode`, and
`herdr`. Application Profiles use the latest official stable releases available
when the collection starts. Ubuntu's `WindowsTerminal` Profile runs through
Windows Terminal → WSL → Ubuntu 24.04; other application Profiles run on their
native targets.

For Desktop backend User-Agents, see
[codex-desktop-ua](https://github.com/huweiATgithub/codex-desktop-ua).

## Download

Fetch the compact matrix for a known Codex version:

```sh
curl -fL https://github.com/huweiATgithub/codex-ua/releases/download/v0.156.1/ua-matrix.json
```

Follow the highest successfully collected stable version:

```sh
curl -fL https://github.com/huweiATgithub/codex-ua/releases/latest/download/ua-matrix.json
```

For detailed collection results, use `ua-matrix.run.json` in either download URL.
The release text also shows the matrix as a table of platforms, Profiles, clients,
and UAs.
Generated data is stored in release assets. The repository contains the collector,
workflow, schemas, tests, and documentation.

## JSON format

Each release provides two JSON files with independently versioned schemas.
The compact matrix uses `schema_version: 3`; run details use `schema_version: 4`.
Both identify the collected `codex_version`.

`ua-matrix.json` follows the [matrix schema](schema/ua-matrix.schema.json).
Its top-level fields are `schema_version`, `codex_version`, and `platforms`.

`platforms` has twelve keys: each of `linux-ubuntu`, `linux-debian`, `linux-fedora`,
`linux-alpine`, `macos`, and `windows` paired with `-x64` and `-arm64`.
Each entry has a `profiles` map with `xterm-256color`, `WindowsTerminal`, `vscode`,
and `herdr` as peer keys. Each Profile maps `CLI` and `Exec` to UA strings,
or is `null` for unsupported combinations. Read a UA using,
for example:

```text
platforms["linux-debian-x64"]["profiles"]["xterm-256color"]["CLI"]
platforms["windows-arm64"]["profiles"]["WindowsTerminal"]["Exec"]
platforms["linux-ubuntu-x64"]["profiles"]["vscode"]["CLI"]
```

`ua-matrix.run.json` follows the [run schema](schema/ua-matrix.run.schema.json)
and describes one collection run. It adds `upstream_release` and `collector`
provenance, including the source commit and workflow run URL. Each platform records
its binary URL, collection time, OS and runner metadata, and `profiles`.
Each collected Profile records its terminal environment and `clients`.
Each client has a `user_agent`, collection `method: "http-capture"`,
and `http_capture` with the UA and originator sent to the loopback server.
`terminal_releases` records the shared stable release snapshot. Each platform's
`profiles` records either `status: "unsupported"` with its reason, or
`status: "collected"` with the native application version and download URL,
launch method, terminal environment, PTY evidence, and both captured clients.
The `xterm-256color` Profile records `application: null` and `tty: null`: it has a
controlled environment rather than an application-owned PTY. Its interactive
client uses a native PTY; Exec uses pipes. Earlier compact schema 2 and run schema
3 stored this Profile at platform level; compact schema 1 and run schema 2
contained only that Profile. Those formats remain readable.
Older run details with `schema_version: 1` used app-server initialization;
only Exec included an HTTP capture in that format.

On Linux, `os.distribution` records the `id`, `version_id`, and `pretty_name`
from the runtime's `/etc/os-release`; it is `null` on other systems.
`runner.container_image` records the container image tag, or `null` for native
host collection. The other runner fields describe the host runner.
For Ubuntu's `WindowsTerminal` Profile, `runtime.os` records the actual WSL guest
and `runtime.runner` records the Windows host. The platform-level metadata
describes the Ubuntu runner used for its other Profiles.

The compact matrix is derived from the validated run details. Corresponding UA
strings are identical in both files. Preserve them verbatim when consuming them.

## Collection method

The collector downloads the exact binary from `openai/codex`'s `rust-v<version>`
release and checks its reported version and native runtime architecture. Both
client modes must send a Responses request with their expected originator and
User-Agent identity before their observations can be published.

Ubuntu, macOS, and Windows are collected directly on GitHub-hosted runners.
Debian 13, Fedora 44, and Alpine 3.24 use their official container images on
matching x64 or ARM64 Ubuntu runners, without CPU emulation. Containers provide
the distribution's userspace and share the host kernel, so Linux `os.release`
and `os.version` describe the host kernel. The collector checks the distribution
identity as well as the native architecture before running the binary.

The `xterm-256color` Profile runs in a fresh process with an empty temporary Codex
home and a controlled terminal environment, `TERM=xterm-256color`. CLI starts the real
interactive `codex` TUI in a native pseudo-terminal: a Unix PTY on Linux/macOS or
ConPTY on Windows. Exec starts `codex exec`. Each receives an initial prompt and
sends a request to a loopback Responses server, which returns a canned response.
The collector records each request's User-Agent and originator verbatim; the
clients set their own names and versions. No OpenAI credentials or model calls
are needed.

Application Profiles launch the downloaded native terminal application first:
Windows Terminal in portable mode, VS Code with an integrated terminal, or a
named Herdr server with its native PTY. A sampler inside that terminal starts
`codex` and `codex exec`, preserving the application's complete environment and
standard input/output. The application responds to terminal queries itself;
the sampler does not create a substitute PTY or set terminal identity variables.
Only the actual requests' User-Agent and originator headers are recorded.

Ubuntu's `WindowsTerminal` Profile is collected on a matching Windows x64 or
ARM64 host: the downloaded Windows Terminal starts WSL's Ubuntu 24.04, and its
sampler runs the native Linux Codex binary inside that terminal. The collector
checks the guest architecture, all three terminal streams, and the Windows
Terminal session inherited by WSL. Its HTTP captures become the corresponding
Ubuntu platform's `WindowsTerminal` Profile. A missing WSL result prevents
publication. CI uses WSL 1; local sampling also supports WSL 2.

Windows Terminal also runs natively on Windows. Other Linux distributions and
macOS record this Profile as unsupported. The VS Code desktop is unsupported in
the distribution containers and on Windows Server; Herdr requires an official
native binary for the selected architecture. Unsupported combinations are
explicit, and a supported launch or capture failure prevents publication.

The collector closes the TUI after its Responses request completes. OS versions
and terminal environments can change the UA independently of the Codex version;
the matrix describes its collection environment.

## Release policy and scheduling

One published release, tagged `v<codex-version>`, contains `ua-matrix.json` and
`ua-matrix.run.json`. The tag points to the collector commit. All twelve platforms
must be present. Every supported terminal Profile must capture both clients;
other combinations must record their native support reason. Both assets must be
uploaded and verified before the workflow publishes. Published versions are skipped; there is no
periodic recollection of an unchanged Codex version.

[Collect and publish](.github/workflows/collect.yml) runs hourly at minute 17 UTC.
The first automatic run collects the current stable version. Subsequent runs
collect the oldest missing stable version from that initial version onward;
later historical backfills do not expand automatic collection backward.
Alpha releases, unrelated upstream releases, and releases without all
required binary assets are excluded. A later check retries incomplete upstream
publication or failed collection.

Run a manual collection or historical backfill with:

```sh
gh workflow run collect.yml --repo huweiATgithub/codex-ua -f version=0.156.1
```

Leave `version` blank to run discovery. Published UA releases provide completion
state. Publication is serialized; drafts can be resumed, and backfills do not
move the Latest selection to an older Codex version. The workflow never replaces
a published asset. A confirmed data error requires an explicitly authorized
operator correction at the same version URL; leave GitHub release immutability
disabled to permit that exceptional correction.

GitHub schedules are best-effort and can be disabled after 60 days of repository
inactivity. An external scheduler can invoke the same `workflow_dispatch` entry
point for unattended operation. See [GitHub's schedule rules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Local verification

Application scripts use Python 3.11 or newer and the standard library, with
`pywinpty` additionally required for Windows ConPTY support. Discovery and
publication use an authenticated GitHub CLI (`gh`). Tests use `jsonschema` to
check the published format; development requirements include runtime dependencies.
Linux VS Code collection additionally requires Xvfb, xauth, and the native GUI
libraries; the workflow installs these on its Ubuntu hosts.

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
```

Run a native collection locally, choosing the platform that matches the machine:

```sh
python -m pip install -r requirements.txt
python scripts/terminals.py resolve --output .local/terminal-releases.json
python scripts/collect.py --version 0.156.1 --platform linux-ubuntu-x64 --terminal-releases .local/terminal-releases.json --output .local/linux-ubuntu-x64.json
```

On Windows, install Ubuntu 24.04 in WSL with `python3` and `ca-certificates`, then
collect the matching Ubuntu WindowsTerminal Profile using Windows Python:

```sh
python scripts/wsl.py collect --version 0.156.1 --platform linux-ubuntu-x64 --terminal-releases .local/terminal-releases.json --output .local/wsl-linux-ubuntu-x64.json
```

Use `linux-ubuntu-arm64` on a native Windows ARM64 host. Matrix assembly merges
these WSL results with the Ubuntu runner records.

For a local smoke test, `--binary /absolute/path/to/codex` uses an already-installed
binary instead of downloading it. This is not evidence of a fresh official-asset
download. Local output, the reference source checkout, and Python caches are
ignored by Git.
Omitting `--terminal-releases` runs an `xterm-256color`-only smoke test. Workflow matrix
assembly requires the complete terminal Profile coverage before publication.

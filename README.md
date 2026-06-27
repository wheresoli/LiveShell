<p align="center">
<img width="356" height="234" alt="Hands_of_God_and_Adam" src="https://github.com/user-attachments/assets/b30a03e8-e7a4-4a83-b9a3-5aa0bc9d3e2c" />
</p>

# LiveShell

LiveShell is a generic local execution substrate for Python. It provides persistent shell sessions, synchronous and asynchronous APIs, durable command records, replayable command events, and a small JSON-lines daemon protocol for process-managed integrations.

It is intentionally reusable infrastructure. LiveShell owns local sessions, commands, output events, cancellation, and backend capability discovery. It does not include agent, plan, scheduler, work-packet, evidence, policy, URL-handler, or network-server concepts.

## What It Provides

- Persistent `cmd`, `bash`, and hosted PowerShell sessions.
- Blocking APIs for simple in-process use.
- Async-capable APIs for event-loop callers.
- A local stdio daemon for durable session and command records.
- SQLite-backed state for sessions, commands, and ordered command events.
- Replayable stdout/stderr events and terminal command result envelopes.
- Best-effort cancellation and timeout handling.
- Backend capability discovery without requiring PowerShell hosting imports.
- A JSON-emitting CLI suitable for scripts and smoke checks.

## Install

Create or update a local development environment:

```powershell
py -3.10 scripts\setup_venv.py
.\.venv\Scripts\Activate.ps1
```

If `python` is already on your PATH:

```powershell
python scripts\setup_venv.py
```

On macOS/Linux:

```bash
python3 scripts/setup_venv.py
source .venv/bin/activate
```

The setup script creates `.venv/`, installs LiveShell in editable mode, and includes test dependencies. To rebuild the environment from scratch:

```powershell
python scripts\setup_venv.py --recreate
```

For runtime dependencies only:

```powershell
python scripts\setup_venv.py --runtime-only
```

## Choosing An API

Use the smallest API surface that matches the job:

- Use `Cmd`, `Bash`, `PowerShell`, and their async variants for direct in-process persistent shell sessions.
- Use `LiveShellClient` when commands should be owned by a local daemon and recorded durably.
- Use the JSON-lines protocol when another process manager needs to embed LiveShell without importing Python objects.
- Use `Store` and `LiveShellService` as lower-level extension points when working on LiveShell itself.

## Synchronous Sessions

The original blocking session style remains supported:

```python
from liveshell import Cmd

with Cmd() as cmd:
    cmd.run("set LIVESHELL_TEST_VALUE=40")
    print(cmd.text("echo %LIVESHELL_TEST_VALUE%"))
```

Process-backed sessions preserve shell state because a single child process remains alive for the session. `PowerShell` hosts the PowerShell engine through `pythonnet` where available rather than shelling out for each command.

## Asynchronous Sessions

Async callers should use the explicit async session classes with `async with`:

```python
from liveshell import AsyncCmd

async with AsyncCmd() as cmd:
    await cmd.run("set LIVESHELL_TEST_VALUE=40")
    print(await cmd.text("echo %LIVESHELL_TEST_VALUE%"))
```

`AsyncCmd` and `AsyncBash` use `asyncio` subprocesses. `AsyncPowerShell` exposes awaitable methods by moving the underlying hosted .NET calls to worker threads, so event-loop callers are not blocked by synchronous PowerShell execution.

## Durable Daemon Client

For durable command records and daemon-owned live sessions, prefer `LiveShellClient` over hand-written protocol calls:

```python
from liveshell import LiveShellClient

with LiveShellClient.stdio(".liveshell-state") as client:
    session = client.create_session("cmd", cwd=r"C:\Projects\LiveShell")
    try:
        command = session.start_command("echo hello", timeout_seconds=5)
        result = command.wait()
        print(result.stdout)
    finally:
        session.close()
```

The client starts a local stdio daemon subprocess, sends JSON-lines requests, validates responses, and returns typed `SessionHandle`, `CommandHandle`, snapshot, event, and result objects.

Async code can use methods such as `discover_capabilities_async`, `create_session_async`, `SessionHandle.run_async`, `CommandHandle.poll_async`, and `CommandHandle.wait_async`. These wrappers use worker threads for protocol calls instead of pretending blocking I/O is natively nonblocking.

## Daemon Protocol

The daemon speaks JSON lines over stdio:

- Each request is one JSON object followed by a newline.
- Each response is one JSON object followed by a newline.
- Successful responses use `{"ok": true, "result": ...}`.
- Error responses use `{"ok": false, "error": {"type": "...", "message": "..."}}`.

Example request:

```json
{"id":"req_1","method":"capability.discover","params":{}}
```

Example response:

```json
{"id":"req_1","ok":true,"result":{"capabilities":[]}}
```

Start a long-running daemon on stdio:

```powershell
liveshell daemon stdio --state-dir .\.liveshell-state
```

Process exactly one request and exit, which is useful for deterministic tests:

```powershell
liveshell daemon stdio --once --state-dir .\.liveshell-state
```

Supported protocol methods:

- `capability.discover`
- `session.create`
- `session.list`
- `session.snapshot`
- `session.close`
- `command.start`
- `command.poll`
- `command.events`
- `command.cancel`
- `command.result`

Typical protocol flow:

```json
{"id":"req_1","method":"session.create","params":{"kind":"cmd","cwd":"C:\\Projects\\LiveShell"}}
{"id":"req_2","method":"command.start","params":{"session_id":"sess_...","command":"echo hello","timeout_seconds":5}}
{"id":"req_3","method":"command.poll","params":{"command_id":"cmd_..."}}
{"id":"req_4","method":"command.events","params":{"command_id":"cmd_...","since_seq":0}}
{"id":"req_5","method":"command.result","params":{"command_id":"cmd_..."}}
{"id":"req_6","method":"session.close","params":{"session_id":"sess_..."}}
```

## State Model

LiveShell stores JSON-serializable records for:

- `Capability`
- `SessionSpec`
- `SessionSnapshot`
- `CommandSpec`
- `CommandSnapshot`
- `CommandResult`
- `CommandEvent`

Session statuses:

- `starting`
- `running`
- `closed`
- `crashed`

Command statuses:

- `queued`
- `starting`
- `running`
- `completed`
- `failed`
- `timed_out`
- `canceled`

Command events:

- `session_started`
- `session_closed`
- `session_crashed`
- `command_started`
- `stdout`
- `stderr`
- `heartbeat`
- `command_completed`
- `command_failed`
- `command_timed_out`
- `command_canceled`

The SQLite store keeps `session`, `command`, and `command_event` tables. It enables WAL mode and a busy timeout for better local reader/writer behavior.

If the daemon restarts, live process handles cannot be recovered. On startup, previously running sessions are marked `crashed`, and previously running commands are marked `failed` with recovery metadata. LiveShell records that honestly rather than pretending a previous process is still controllable.

## CLI

All CLI commands print JSON.

```powershell
liveshell capability discover
liveshell run --kind cmd --command "echo hello" --timeout-seconds 5 --state-dir .\.liveshell-state
liveshell daemon stdio --state-dir .\.liveshell-state
liveshell daemon stdio --once --state-dir .\.liveshell-state
liveshell session list --state-dir .\.liveshell-state
liveshell session snapshot --session-id sess_... --state-dir .\.liveshell-state
liveshell command poll --command-id cmd_... --state-dir .\.liveshell-state
liveshell command events --command-id cmd_... --since-seq 0 --state-dir .\.liveshell-state
liveshell command result --command-id cmd_... --state-dir .\.liveshell-state
liveshell command cancel --command-id cmd_... --state-dir .\.liveshell-state
```

`liveshell run` is a one-shot convenience command. It starts a local stdio daemon, creates a session, runs one command, waits for the durable result envelope, closes the session, and exits.

Long-lived live sessions, command start, and active command cancellation require the daemon process that owns the in-memory shell session. Direct `session create`, `command start`, and active `command cancel` CLI paths fail clearly outside that daemon instead of faking success against only the SQLite store. Use `LiveShellClient` or send protocol requests to a running stdio daemon for live session control.

## Capability Discovery

Discover available features from Python:

```python
from liveshell import discover_capabilities

for capability in discover_capabilities():
    print(capability.to_dict())
```

Or from the CLI:

```powershell
liveshell capability discover
```

Discovery reports backend and protocol capabilities such as:

- `session.persistent_env`
- `command.blocking`
- `command.poll`
- `command.events.replay`
- `command.events.streaming.best_effort`
- `command.stderr.separate`
- `command.exit_code.native`
- `command.cancel.best_effort`
- `shell.cmd.available`
- `shell.bash.available`
- `shell.powershell.available`
- `shell.powershell.hosted`

PowerShell discovery checks whether PowerShell is installed without requiring `pythonnet`. Hosted PowerShell capability is reported separately.

## Diagnose PowerShell

Discovery and diagnostics can be run without loading PowerShell into the Python process:

```powershell
python scripts/diagnose.py
```

To also try loading the hosted PowerShell engine:

```powershell
python scripts/diagnose.py --preload
```

PowerShell Core installations use `pwsh.runtimeconfig.json` and load with CoreCLR. Windows PowerShell 5.1 uses the .NET Framework GAC assembly and loads with `netfx`. A single Python process can only host one .NET runtime mode.

## Smoke Tests

Run the example smoke scripts:

```powershell
python examples/powershell_smoke.py
python examples/process_smoke.py
python examples/async_process_smoke.py
```

They exercise hosted PowerShell, synchronous process-backed sessions, and async process-backed sessions.

## Behavior And Limitations

Process-backed sessions such as `Cmd` and `Bash` keep one child process alive and send commands through it. They preserve shell state, but they are not full terminal or PTY emulators.

Process-backed shells stream stdout events incrementally as lines are produced. Stderr is captured separately and emitted as `stderr` events when available. Hosted PowerShell stdout and stderr are captured at command completion.

Hosted PowerShell error records and nonzero native `$LASTEXITCODE` values mark daemon commands failed instead of being treated as successful empty-stderr runs.

Cancellation is best effort. Stopping a running command may require closing or terminating the backing session, and the command/session metadata records when that happens.

Persistent sessions own their working directory. Set `cwd` on `session.create`; per-command `cwd` is accepted only when it matches the session cwd. Create a separate session for a different working directory.

OS URL protocol handlers, deep links, network servers, and hidden command execution from URLs are intentionally not implemented.

## Tests

Run the no-extra-dependencies test suite:

```powershell
python -m unittest discover -s tests
```

After installing development dependencies, you can also run:

```powershell
pytest
coverage run -m unittest discover -s tests
coverage report
```

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .capabilities import discover_capabilities
from .client import LiveShellClient
from .daemon import JsonLineDaemon, LiveShellService
from .handles import CommandHandle
from .store import Store


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "raw_stdio", False):
        try:
            args.func(args)
            return 0
        except Exception as exc:
            print_json(
                {
                    "ok": False,
                    "error": {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                }
            )
            return 1
    try:
        result = args.func(args)
        print_json({"ok": True, "result": result})
        return 0
    except Exception as exc:
        print_json(
            {
                "ok": False,
                "error": {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                },
            }
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liveshell")
    subcommands = parser.add_subparsers(dest="resource", required=True)

    run = subcommands.add_parser("run")
    run.add_argument("--kind", required=True, choices=["cmd", "bash", "powershell"])
    run.add_argument("--command", required=True)
    run.add_argument("--cwd")
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument("--poll-interval", type=float, default=0.1)
    run.add_argument("--state-dir", required=True)
    run.set_defaults(func=run_command)

    capability = subcommands.add_parser("capability")
    capability_subcommands = capability.add_subparsers(dest="action", required=True)
    capability_discover = capability_subcommands.add_parser("discover")
    capability_discover.set_defaults(func=capability_discover_command)

    daemon = subcommands.add_parser("daemon")
    daemon_subcommands = daemon.add_subparsers(dest="action", required=True)
    daemon_stdio = daemon_subcommands.add_parser("stdio")
    daemon_stdio.add_argument("--state-dir", required=True)
    daemon_stdio.add_argument("--once", action="store_true")
    daemon_stdio.set_defaults(func=daemon_stdio_command, raw_stdio=True)

    session = subcommands.add_parser("session")
    session_subcommands = session.add_subparsers(dest="action", required=True)

    session_create = session_subcommands.add_parser("create")
    session_create.add_argument("--kind", required=True, choices=["cmd", "bash", "powershell"])
    session_create.add_argument("--cwd")
    session_create.add_argument("--state-dir", required=True)
    session_create.set_defaults(func=session_create_command)

    session_list = session_subcommands.add_parser("list")
    session_list.add_argument("--state-dir", required=True)
    session_list.set_defaults(func=session_list_command)

    session_snapshot = session_subcommands.add_parser("snapshot")
    session_snapshot.add_argument("--session-id", required=True)
    session_snapshot.add_argument("--state-dir", required=True)
    session_snapshot.set_defaults(func=session_snapshot_command)

    command = subcommands.add_parser("command")
    command_subcommands = command.add_subparsers(dest="action", required=True)

    command_start = command_subcommands.add_parser("start")
    command_start.add_argument("--session-id", required=True)
    command_start.add_argument("--command", required=True)
    command_start.add_argument("--timeout-seconds", type=float)
    command_start.add_argument("--state-dir", required=True)
    command_start.set_defaults(func=command_start_command)

    command_poll = command_subcommands.add_parser("poll")
    command_poll.add_argument("--command-id", required=True)
    command_poll.add_argument("--state-dir", required=True)
    command_poll.set_defaults(func=command_poll_command)

    command_events = command_subcommands.add_parser("events")
    command_events.add_argument("--command-id", required=True)
    command_events.add_argument("--since-seq", type=int, default=0)
    command_events.add_argument("--state-dir", required=True)
    command_events.set_defaults(func=command_events_command)

    command_result = command_subcommands.add_parser("result")
    command_result.add_argument("--command-id", required=True)
    command_result.add_argument("--state-dir", required=True)
    command_result.set_defaults(func=command_result_command)

    command_cancel = command_subcommands.add_parser("cancel")
    command_cancel.add_argument("--command-id", required=True)
    command_cancel.add_argument("--state-dir", required=True)
    command_cancel.add_argument("--reason")
    command_cancel.set_defaults(func=command_cancel_command)

    return parser


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    with LiveShellClient.stdio(args.state_dir) as client:
        session = client.create_session(args.kind, cwd=args.cwd)
        try:
            result = session.run(
                args.command,
                timeout_seconds=args.timeout_seconds,
                poll_interval=args.poll_interval,
            )
        except Exception:
            try:
                session.close()
            finally:
                raise

        payload = result.to_dict()
        payload["session"] = session.snapshot().to_dict()
        try:
            payload["closed_session"] = session.close().to_dict()
        except Exception as exc:
            payload["session_close_error"] = {
                "type": exc.__class__.__name__,
                "message": str(exc),
            }
        return payload


def capability_discover_command(args: argparse.Namespace) -> dict[str, Any]:
    return {"capabilities": [capability.to_dict() for capability in discover_capabilities()]}


def daemon_stdio_command(args: argparse.Namespace) -> dict[str, Any]:
    service = LiveShellService(Store.from_state_dir(args.state_dir))
    JsonLineDaemon(service).serve_stdio(once=args.once)
    return {"exited": True}


def session_create_command(args: argparse.Namespace) -> dict[str, Any]:
    raise RuntimeError(
        "session create requires a live daemon-owned process. "
        "Use liveshell daemon stdio and send a session.create request."
    )


def session_list_command(args: argparse.Namespace) -> list[dict[str, Any]]:
    store = Store.from_state_dir(args.state_dir)
    return [session.to_dict() for session in store.list_sessions()]


def session_snapshot_command(args: argparse.Namespace) -> dict[str, Any]:
    store = Store.from_state_dir(args.state_dir)
    snapshot = store.get_session(args.session_id)
    if snapshot is None:
        raise KeyError(f"Unknown session: {args.session_id}")
    return snapshot.to_dict()


def command_start_command(args: argparse.Namespace) -> dict[str, Any]:
    raise RuntimeError(
        "command start requires a live daemon-owned session. "
        "Use liveshell daemon stdio and send a command.start request."
    )


def command_poll_command(args: argparse.Namespace) -> dict[str, Any]:
    return CommandHandle(args.command_id, Store.from_state_dir(args.state_dir)).poll().to_dict()


def command_events_command(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [
        event.to_dict()
        for event in CommandHandle(args.command_id, Store.from_state_dir(args.state_dir)).events(
            args.since_seq
        )
    ]


def command_result_command(args: argparse.Namespace) -> dict[str, Any] | None:
    result = CommandHandle(args.command_id, Store.from_state_dir(args.state_dir)).result()
    return result.to_dict() if result is not None else None


def command_cancel_command(args: argparse.Namespace) -> dict[str, Any]:
    return CommandHandle(args.command_id, Store.from_state_dir(args.state_dir)).cancel(
        reason=args.reason
    ).to_dict()


def print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

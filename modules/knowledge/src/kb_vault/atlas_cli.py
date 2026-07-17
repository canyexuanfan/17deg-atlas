from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

from .cli import main as knowledge_main


CLI_VERSION = "0.1.0"
WORKSPACE_ACTIONS = {
    "init": "init-instance",
    "plan": "agent-start-plan",
    "start": "agent-start",
    "migration-source": "agent-migration-source",
    "migration-plan": "agent-migration-plan",
    "migration-start": "agent-migration-start",
    "migration-review": "agent-migration-review",
    "import-review": "agent-workspace-import",
    "migration-repair-plan": "agent-migration-repair-plan",
    "migration-repair-start": "agent-migration-repair-start",
    "retirement-plan": "agent-retirement-plan",
    "retirement-start": "agent-retirement-start",
}
KNOWLEDGE_GLOBAL_OPTIONS = ("--root", "--age-path")


def _print_help() -> None:
    print(
        "\n".join(
            (
                "17deg Atlas CLI",
                "",
                "Usage:",
                "  17deg-atlas workspace <init|plan|start> [options]",
                "  17deg-atlas workspace <import-review|migration-source|migration-plan|migration-start|migration-review|migration-repair-plan|migration-repair-start|retirement-plan|retirement-start> [options]",
                "  17deg-atlas knowledge <command> [options]",
                "  17deg-atlas remote <command> [options]",
            )
        )
    )


def _json_error(message: str) -> int:
    print(json.dumps({"status": "error", "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


def _run_remote(argv: list[str]) -> int:
    module_root = Path(__file__).resolve().parents[2]
    script = module_root / "skills" / "github-api-file-pusher" / "scripts" / "remote.py"
    if not script.is_file():
        return _json_error("remote runtime is unavailable")
    original = sys.argv[:]
    sys.argv = [str(script), *argv]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = original
    return 0


def _with_knowledge_globals(command: str, argv: list[str]) -> list[str]:
    global_args: list[str] = []
    command_args: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in KNOWLEDGE_GLOBAL_OPTIONS:
            if index + 1 >= len(argv):
                return [command, *argv]
            global_args.extend((item, argv[index + 1]))
            index += 2
            continue
        command_args.append(item)
        index += 1
    return [*global_args, command, *command_args]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        _print_help()
        return 0
    if args[0] in ("-V", "--version"):
        print(f"17deg-atlas {CLI_VERSION}")
        return 0
    surface = args.pop(0)
    entry_runtime = os.environ.get("ATLAS_ENTRY_RUNTIME", "").strip().lower()
    if surface == "knowledge":
        return knowledge_main(args)
    if surface == "workspace":
        if not args or args[0] in ("-h", "--help"):
            print(
                "Usage: 17deg-atlas workspace <init|plan|start> [options]\n"
                "       17deg-atlas workspace <import-review|migration-source|migration-plan|migration-start|migration-review|migration-repair-plan|migration-repair-start|retirement-plan|retirement-start> [options]"
            )
            return 0
        action = args.pop(0)
        mapped = WORKSPACE_ACTIONS.get(action)
        if mapped is None:
            return _json_error("unsupported workspace command")
        return knowledge_main(_with_knowledge_globals(mapped, args))
    if surface == "remote":
        if entry_runtime and entry_runtime != "remote":
            return _json_error("remote commands require the remote entry")
        return _run_remote(args)
    return _json_error("unsupported command surface")


if __name__ == "__main__":
    raise SystemExit(main())

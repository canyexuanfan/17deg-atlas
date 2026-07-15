#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


OFFICIAL_REPOSITORY = "https://github.com/canyexuanfan/17deg-atlas.git"
DEFAULT_REPOSITORY = os.environ.get("ATLAS_REPOSITORY", OFFICIAL_REPOSITORY)
MODULE_RELATIVES = (
    Path("modules") / "knowledge",
    Path("domains") / "personal" / "knowledge",
)
CLI_VERSION = "0.1.0"


class BootstrapError(RuntimeError):
    pass


def module_root(source: Path) -> Path | None:
    source = source.expanduser().resolve()
    if (source / "src" / "kb_vault" / "cli.py").is_file():
        return source
    for relative in MODULE_RELATIVES:
        candidate = source / relative
        if (candidate / "src" / "kb_vault" / "cli.py").is_file():
            return candidate
    return None


def copy_source(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(".git", ".local", "__pycache__", ".pytest_cache", "*.pyc"),
    )


def clone_source(repository: str, target: Path) -> None:
    if not shutil.which("git"):
        raise BootstrapError("git is required for network bootstrap")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--no-tags", repository, str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapError("unable to clone the 17deg Atlas tool runtime")


def ensure_local_exclude(workspace: Path) -> bool:
    exclude = workspace / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return False
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if ".17deg-atlas/" not in lines:
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            exclude.write_text(existing + prefix + ".17deg-atlas/\n", encoding="utf-8")
    except OSError as exc:
        raise BootstrapError("unable to protect the project-local tool runtime") from exc
    return True


def install_cli(install_root: Path, tool_root: Path) -> dict[str, str]:
    bin_root = install_root / "bin"
    state_root = install_root / "state"
    bin_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    launcher = bin_root / "17deg-atlas.py"
    launcher.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

tool_root = Path(__file__).resolve().parents[1] / "tool"
if (tool_root / "src" / "kb_vault" / "atlas_cli.py").is_file():
    module_root = tool_root
elif (tool_root / "modules" / "knowledge" / "src" / "kb_vault" / "atlas_cli.py").is_file():
    module_root = tool_root / "modules" / "knowledge"
else:
    module_root = tool_root / "domains" / "personal" / "knowledge"
sys.path.insert(0, str(module_root / "src"))
from kb_vault.atlas_cli import main
raise SystemExit(main())
""",
        encoding="utf-8",
    )
    command = bin_root / "17deg-atlas.cmd"
    command.write_text(
        f'@"{sys.executable}" "%~dp0\\17deg-atlas.py" %*\r\n',
        encoding="utf-8",
    )
    shell = bin_root / "17deg-atlas"
    shell.write_text(
        f'#!/usr/bin/env sh\nexec "{sys.executable}" "$(dirname "$0")/17deg-atlas.py" "$@"\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        shell.chmod(0o755)
    check = subprocess.run(
        [sys.executable, str(launcher), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check.returncode != 0 or check.stdout.strip() != f"17deg-atlas {CLI_VERSION}":
        raise BootstrapError("installed CLI failed validation")
    manifest = {
        "schema_version": 1,
        "cli_version": CLI_VERSION,
        "tool_root": str(tool_root),
        "python": sys.executable,
        "launcher": str(launcher),
    }
    (state_root / "runtime.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "cli_path": str(launcher),
        "cli_command": str(command if os.name == "nt" else shell),
        "cli_version": CLI_VERSION,
    }


def bootstrap(
    workspace: Path,
    *,
    source: Path | None = None,
    repository: str = DEFAULT_REPOSITORY,
    confirm_network_install: bool = False,
) -> dict[str, object]:
    workspace = workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    install_root = workspace / ".17deg-atlas"
    target = install_root / "tool"
    if module_root(target):
        cli = install_cli(install_root, target)
        return {
            "status": "ok",
            "action": "connected-existing-runtime",
            "workspace": str(workspace),
            "tool_root": str(target),
            "network_used": False,
            "local_exclude_configured": ensure_local_exclude(workspace),
            **cli,
        }
    if target.exists():
        raise BootstrapError("existing .17deg-atlas/tool is not a valid runtime")
    if source is not None:
        source = source.expanduser().resolve()
        if module_root(source) is None:
            raise BootstrapError("local bootstrap source is not a 17deg Atlas checkout")
        install_root.mkdir(parents=True, exist_ok=True)
        copy_source(source, target)
        action = "installed-from-local-source"
        network_used = False
    else:
        if not confirm_network_install:
            raise BootstrapError("network bootstrap requires --confirm-network-install")
        if not repository:
            raise BootstrapError("network bootstrap requires --repository or ATLAS_REPOSITORY")
        install_root.mkdir(parents=True, exist_ok=True)
        clone_source(repository, target)
        action = "installed-from-public-repository"
        network_used = True
    resolved_module = module_root(target)
    if resolved_module is None:
        shutil.rmtree(target, ignore_errors=True)
        raise BootstrapError("installed runtime failed structural validation")
    cli = install_cli(install_root, target)
    return {
        "status": "ok",
        "action": action,
        "workspace": str(workspace),
        "tool_root": str(target),
        "module_root": str(resolved_module),
        "network_used": network_used,
        "credentials_created": False,
        "global_skill_installed": False,
        "local_exclude_recommended": ".17deg-atlas/",
        "local_exclude_configured": ensure_local_exclude(workspace),
        **cli,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install the 17deg Atlas tool runtime")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--source", type=Path)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--confirm-network-install", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = bootstrap(
            args.workspace,
            source=args.source,
            repository=args.repository,
            confirm_network_install=args.confirm_network_install,
        )
    except BootstrapError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

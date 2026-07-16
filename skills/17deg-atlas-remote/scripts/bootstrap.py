#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


OFFICIAL_REPOSITORY = "https://github.com/canyexuanfan/17deg-atlas.git"
DEFAULT_REPOSITORY = os.environ.get("ATLAS_REPOSITORY", OFFICIAL_REPOSITORY)
MODULE_RELATIVES = (
    Path("modules") / "knowledge",
    Path("domains") / "personal" / "knowledge",
)
CLI_VERSION = "0.1.0"
ENTRY_RUNTIME = "remote"


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


def source_fingerprint(source: Path) -> str:
    root = module_root(source)
    if root is None:
        raise BootstrapError("runtime source is not a 17deg Atlas checkout")
    digest = hashlib.sha256()
    ignored = {".git", ".local", "__pycache__", ".pytest_cache"}
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts) or path.suffix == ".pyc":
            continue
        if not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise BootstrapError("unable to fingerprint the tool runtime") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def git_value(source: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    result = subprocess.run(
        [git, "-C", str(source), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def source_metadata(source: Path, repository: str = "") -> dict[str, str]:
    source = source.expanduser().resolve()
    return {
        "source_repository": git_value(source, "remote", "get-url", "origin") or repository,
        "source_commit": git_value(source, "rev-parse", "HEAD"),
        "source_fingerprint": source_fingerprint(source),
    }


def replace_runtime(source: Path, install_root: Path) -> Path:
    target = install_root / "tool"
    staged = install_root / "tool.next"
    backup = install_root / "tool.previous"
    for managed in (staged, backup):
        if managed.exists():
            shutil.rmtree(managed)
    copy_source(source, staged)
    if module_root(staged) is None:
        shutil.rmtree(staged, ignore_errors=True)
        raise BootstrapError("updated runtime failed structural validation")
    moved_existing = False
    try:
        if target.exists():
            target.replace(backup)
            moved_existing = True
        staged.replace(target)
    except OSError as exc:
        if moved_existing and backup.exists() and not target.exists():
            backup.replace(target)
        shutil.rmtree(staged, ignore_errors=True)
        raise BootstrapError("unable to activate the updated tool runtime") from exc
    return backup


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


def install_cli(
    install_root: Path,
    tool_root: Path,
    *,
    metadata: dict[str, str] | None = None,
) -> dict[str, str]:
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
    role_launcher = bin_root / f"17deg-atlas-{ENTRY_RUNTIME}.py"
    role_launcher.write_text(
        f'''#!/usr/bin/env python3
import os
import runpy
from pathlib import Path

workspace = Path(__file__).resolve().parents[2]
os.environ["ATLAS_WORKSPACE"] = str(workspace)
os.environ["ATLAS_ENTRY_RUNTIME"] = "{ENTRY_RUNTIME}"
runpy.run_path(str(Path(__file__).with_name("17deg-atlas.py")), run_name="__main__")
''',
        encoding="utf-8",
    )
    role_command = bin_root / f"17deg-atlas-{ENTRY_RUNTIME}.cmd"
    role_command.write_text(
        f'@"{sys.executable}" "%~dp0\\17deg-atlas-{ENTRY_RUNTIME}.py" %*\r\n',
        encoding="utf-8",
    )
    role_shell = bin_root / f"17deg-atlas-{ENTRY_RUNTIME}"
    role_shell.write_text(
        f'#!/usr/bin/env sh\nexec "{sys.executable}" "$(dirname "$0")/17deg-atlas-{ENTRY_RUNTIME}.py" "$@"\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        role_shell.chmod(0o755)
    check = subprocess.run(
        [sys.executable, str(role_launcher), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check.returncode != 0 or check.stdout.strip() != f"17deg-atlas {CLI_VERSION}":
        raise BootstrapError("installed CLI failed validation")
    manifest = {
        "schema_version": 2,
        "cli_version": CLI_VERSION,
        "tool_root": str(tool_root),
        "python": sys.executable,
        "launcher": str(role_launcher),
        "generic_launcher": str(launcher),
        "entry_runtime": ENTRY_RUNTIME,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        **(metadata or {}),
    }
    (state_root / "runtime.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "cli_path": str(role_launcher),
        "cli_command": str(role_command if os.name == "nt" else role_shell),
        "generic_cli_path": str(launcher),
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
        metadata = source_metadata(target, repository)
        action = "connected-existing-runtime"
        backup: Path | None = None
        if source is not None:
            source = source.expanduser().resolve()
            if module_root(source) is None:
                raise BootstrapError("local bootstrap source is not a 17deg Atlas checkout")
            incoming = source_metadata(source, repository)
            if incoming["source_fingerprint"] != metadata["source_fingerprint"]:
                backup = replace_runtime(source, install_root)
                metadata = incoming
                action = "updated-from-local-source"
            else:
                metadata = incoming
        try:
            cli = install_cli(install_root, target, metadata=metadata)
        except BootstrapError:
            if backup is not None and backup.exists():
                shutil.rmtree(target, ignore_errors=True)
                backup.replace(target)
            raise
        return {
            "status": "ok",
            "action": action,
            "workspace": str(workspace),
            "tool_root": str(target),
            "network_used": False,
            "runtime_refreshed": action == "updated-from-local-source",
            "local_exclude_configured": ensure_local_exclude(workspace),
            **cli,
        }
    if target.exists():
        raise BootstrapError("existing .17deg-atlas/tool is not a valid runtime")
    if source is not None:
        source = source.expanduser().resolve()
        if module_root(source) is None:
            raise BootstrapError("local bootstrap source is not a 17deg Atlas checkout")
        metadata = source_metadata(source, repository)
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
        metadata = source_metadata(target, repository)
        action = "installed-from-public-repository"
        network_used = True
    resolved_module = module_root(target)
    if resolved_module is None:
        shutil.rmtree(target, ignore_errors=True)
        raise BootstrapError("installed runtime failed structural validation")
    cli = install_cli(install_root, target, metadata=metadata)
    return {
        "status": "ok",
        "action": action,
        "workspace": str(workspace),
        "tool_root": str(target),
        "module_root": str(resolved_module),
        "network_used": network_used,
        "runtime_refreshed": False,
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

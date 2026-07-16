from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from .core import KBError
from .registry import atlas_workspace


def _existing_path(candidates: list[Path]) -> str | None:
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _windows_age_candidates() -> list[Path]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    user_profile = Path(os.environ.get("USERPROFILE", ""))
    program_data = Path(os.environ.get("ProgramData", ""))
    program_files = Path(os.environ.get("ProgramFiles", ""))
    candidates = [
        user_profile / "scoop" / "apps" / "age" / "current" / "age.exe",
        program_data / "chocolatey" / "bin" / "age.exe",
        program_files / "age" / "age.exe",
    ]
    packages = local_app_data / "Microsoft" / "WinGet" / "Packages"
    try:
        candidates.extend(
            package / "age" / "age.exe"
            for package in packages.glob("FiloSottile.age_*")
        )
    except OSError:
        pass
    return candidates


def _managed_binary_candidates(
    name: str,
    *,
    suffix: str,
    local_root: str | Path | None = None,
    workspace: str | Path | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    if workspace:
        candidates.append(Path(workspace) / ".17deg-atlas" / "bin" / f"{name}{suffix}")
    if local_root:
        root = Path(local_root).expanduser().resolve()
        for candidate in (root, *list(root.parents)[:6]):
            candidates.extend(
                [
                    candidate / ".local" / "bin" / f"{name}{suffix}",
                    candidate / ".17deg-atlas" / "bin" / f"{name}{suffix}",
                ]
            )
    return candidates


def discover_age_executable(
    age_path: str | Path | None = None,
    *,
    common_paths: Mapping[str, list[Path]] | None = None,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    local_root: str | Path | None = None,
    workspace: str | Path | None = None,
) -> Path | None:
    active_system = (system or platform.system()).lower()
    suffix = ".exe" if active_system == "windows" else ""
    candidates: list[Path] = []
    configured = age_path or os.environ.get("KB_AGE_PATH")
    if configured:
        candidates.append(Path(configured))
    selected_workspace = workspace or atlas_workspace()
    candidates.extend(
        _managed_binary_candidates(
            "age",
            suffix=suffix,
            local_root=local_root,
            workspace=selected_workspace,
        )
    )
    candidates.extend((common_paths or {}).get("age", []))
    if discovered := which("age"):
        candidates.append(Path(discovered))
    if active_system == "windows":
        candidates.extend(_windows_age_candidates())
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def discover_age_keygen_executable(
    age_path: str | Path | None = None,
    keygen_path: str | Path | None = None,
    *,
    common_paths: Mapping[str, list[Path]] | None = None,
    system: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    local_root: str | Path | None = None,
    workspace: str | Path | None = None,
) -> Path | None:
    active_system = (system or platform.system()).lower()
    suffix = ".exe" if active_system == "windows" else ""
    candidates: list[Path] = []
    configured = keygen_path or os.environ.get("KB_AGE_KEYGEN_PATH")
    if configured:
        candidates.append(Path(configured))
    if age_path:
        candidates.append(Path(age_path).with_name(f"age-keygen{suffix}"))
    selected_workspace = workspace or atlas_workspace()
    candidates.extend(
        _managed_binary_candidates(
            "age-keygen",
            suffix=suffix,
            local_root=local_root,
            workspace=selected_workspace,
        )
    )
    candidates.extend((common_paths or {}).get("age-keygen", []))
    if discovered := which("age-keygen"):
        candidates.append(Path(discovered))
    if active_system == "windows":
        candidates.extend(
            candidate.with_name("age-keygen.exe") for candidate in _windows_age_candidates()
        )
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def dependency_status(
    age_path: str | Path | None = None,
    *,
    local_root: str | Path | None = None,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    age = discover_age_executable(
        age_path,
        local_root=local_root,
        workspace=workspace,
    )
    keygen = discover_age_keygen_executable(
        age,
        local_root=local_root,
        workspace=workspace,
    )
    values = {
        "python": True,
        "git": bool(shutil.which("git")),
        "age": bool(age),
        "age_keygen": bool(keygen),
    }
    return {
        "available": values,
        "missing": [name for name, available in values.items() if not available],
    }


class LocalDependencyEnvironment:
    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., Any] = subprocess.run,
        system: str | None = None,
        common_paths: Mapping[str, list[Path]] | None = None,
    ):
        self.which = which
        self.runner = runner
        self.system = (system or platform.system()).lower()
        self.common_paths = common_paths or {}

    def command(self, name: str) -> str | None:
        if resolved := self.which(name):
            return resolved
        candidates = list(self.common_paths.get(name, []))
        if not candidates and self.system == "windows" and name == "winget":
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Microsoft"
                / "WindowsApps"
                / "winget.exe",
            ]
        return _existing_path(candidates)

    def discover_age(self, age_path: str | Path | None = None) -> Path | None:
        return discover_age_executable(
            age_path,
            common_paths=self.common_paths,
            system=self.system,
            which=self.which,
        )

    def discover_age_keygen(self, age_path: str | Path | None = None) -> Path | None:
        return discover_age_keygen_executable(
            age_path,
            common_paths=self.common_paths,
            system=self.system,
            which=self.which,
        )

    def age_installation(self, age_path: str | Path | None = None) -> dict[str, Any]:
        if self.discover_age(age_path):
            return {"required": False, "manager": "existing", "command": []}
        candidates: list[tuple[str, list[str]]] = []
        if self.system == "windows":
            if winget := self.command("winget"):
                candidates.append(
                    (
                        "winget",
                        [
                            winget,
                            "install",
                            "--id",
                            "FiloSottile.age",
                            "--exact",
                            "--accept-package-agreements",
                            "--accept-source-agreements",
                        ],
                    )
                )
            if scoop := self.command("scoop"):
                candidates.append(("scoop", [scoop, "install", "age"]))
            if choco := self.command("choco"):
                candidates.append(("choco", [choco, "install", "age.portable", "-y"]))
        elif self.system == "darwin":
            if brew := self.command("brew"):
                candidates.append(("brew", [brew, "install", "age"]))
            if port := self.command("port"):
                candidates.append(("port", [port, "install", "age"]))
        else:
            definitions = (
                ("brew", ["install", "age"]),
                ("apt-get", ["install", "-y", "age"]),
                ("dnf", ["install", "-y", "age"]),
                ("apk", ["add", "age"]),
                ("pacman", ["-S", "--noconfirm", "age"]),
            )
            for manager, arguments in definitions:
                if executable := self.command(manager):
                    candidates.append((manager, [executable, *arguments]))
        if not candidates:
            return {"required": True, "manager": "unavailable", "command": []}
        manager, command = candidates[0]
        return {"required": True, "manager": manager, "command": command}

    def install_age(
        self,
        *,
        age_path: str | Path | None = None,
        confirm: bool = False,
    ) -> Path:
        plan = self.age_installation(age_path)
        if not plan["required"]:
            resolved = self.discover_age(age_path)
            if resolved is None:
                raise KBError("age installation could not be verified")
            return resolved
        if not plan["command"]:
            raise KBError("age requires a supported package manager")
        if not confirm:
            raise KBError("age installation requires confirmation")
        result = self.runner(plan["command"], check=False)
        resolved = self.discover_age(age_path)
        if resolved is None:
            discovered = self.which("age")
            resolved = Path(discovered).resolve() if discovered else None
        if result.returncode != 0 and resolved is None:
            raise KBError("age installation failed")
        if resolved is None or not resolved.is_file():
            raise KBError("age installation could not be verified")
        return resolved

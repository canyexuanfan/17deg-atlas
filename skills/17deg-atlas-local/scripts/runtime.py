from __future__ import annotations

import os
from pathlib import Path


MODULE_RELATIVES = (
    Path("modules") / "knowledge",
    Path("domains") / "personal" / "knowledge",
)


def _normalize(candidate: Path) -> Path | None:
    candidate = candidate.expanduser().resolve()
    if (candidate / "src" / "kb_vault" / "cli.py").is_file():
        return candidate
    for relative in MODULE_RELATIVES:
        module = candidate / relative
        if (module / "src" / "kb_vault" / "cli.py").is_file():
            return module
    return None


def find_product_root() -> Path | None:
    candidates: list[Path] = []
    if configured := os.environ.get("ATLAS_PRODUCT_ROOT"):
        candidates.append(Path(configured))
    script = Path(__file__).resolve()
    candidates.extend(script.parents)
    workspace = Path(os.environ.get("ATLAS_WORKSPACE", Path.cwd())).expanduser().resolve()
    candidates.extend(
        [
            workspace / ".codex" / "reference" / "17deg-atlas",
            workspace / ".claudian" / "reference" / "17deg-atlas",
            workspace / ".agents" / "reference" / "17deg-atlas",
            workspace / "reference" / "17deg-atlas",
            workspace,
            workspace / ".17deg-atlas" / "tool",
        ]
    )
    for parent in (workspace, *workspace.parents):
        candidates.extend(
            [
                parent / ".17deg-atlas" / "tool",
                parent / ".codex" / "reference" / "17deg-atlas",
                parent / ".claudian" / "reference" / "17deg-atlas",
                parent / ".agents" / "reference" / "17deg-atlas",
            ]
        )
    if configured_home := os.environ.get("ATLAS_TOOL_HOME"):
        candidates.append(Path(configured_home))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if normalized := _normalize(resolved):
            return normalized
    return None


def require_product_root() -> Path:
    root = find_product_root()
    if root is None:
        raise RuntimeError(
            "17deg Atlas local runtime is unavailable; run the bundled scripts/bootstrap.py in the active workspace"
        )
    return root

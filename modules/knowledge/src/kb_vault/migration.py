from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .bootstrap import (
    KNOWLEDGE_MODULE_RELATIVE,
    LEGACY_KNOWLEDGE_MODULE_RELATIVES,
    PERSONAL_DOMAIN_RUNTIME_RELATIVE,
    initialize_personal_domain,
    is_knowledge_module_root,
    is_personal_domain_root,
    resolve_knowledge_root,
)
from .core import KBError, KnowledgeVault, stable_token
from .github_onboarding import GitHubCLIEnvironment, configured_repository
from .workspace_views import materialize_workspace_views


MIGRATION_STATE = Path(".atlas") / "state" / "knowledge-migration.json"
TARGET_MANAGED_FILES = {
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "LICENSES.md",
    "README.md",
    "config/instance.json",
    "config/policies.yml",
    "config/projection.yml",
    "config/tiers.yml",
    "index.jsonl",
    "index.md",
    "manifests/catalog.jsonl",
    "manifests/projection-selection.json",
}
TARGET_MANAGED_PREFIXES = (
    "config/schemas/",
    "templates/",
    "tools/",
)
TRANSIENT_PREFIXES = (
    ".git/",
    ".atlas/",
    ".obsidian/",
    ".local/bin/",
    ".local/cache/",
    ".local/decrypted/",
    ".local/authorized-results/",
    ".local/private-index/",
    ".local/semantic/",
    ".local/trusted-search/",
    ".local/test-runs/",
    ".local/recovery-runs/",
    "reference/repos/",
)
CREDENTIAL_PREFIXES = (".local/test-keys/",)
WORKSPACE_CONTENT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".pdf",
    ".doc",
    ".docx",
    ".rtf",
    ".odt",
    ".html",
    ".htm",
    ".epub",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".mp3",
    ".wav",
    ".m4a",
    ".mp4",
    ".mov",
}
WORKSPACE_EXCLUDED_DIRECTORIES = {
    ".17deg-atlas",
    ".claude",
    ".claudian",
    ".codex",
    ".git",
    ".github",
    ".obsidian",
    ".venv",
    "node_modules",
    "venv",
}
WORKSPACE_CONTROL_FILES = {
    "agents.md",
    "claude.md",
    "contributing.md",
    "license",
    "license.md",
    "licenses.md",
    "readme.md",
    "security.md",
    "欢迎.md",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def _layout(root: Path) -> str:
    if is_knowledge_module_root(root):
        return "legacy-module-root"
    if not is_personal_domain_root(root):
        return "unknown"
    knowledge_root = resolve_knowledge_root(root)
    try:
        relative = knowledge_root.relative_to(root)
    except ValueError:
        return "unknown"
    if relative in (KNOWLEDGE_MODULE_RELATIVE, PERSONAL_DOMAIN_RUNTIME_RELATIVE):
        return "current"
    if relative in LEGACY_KNOWLEDGE_MODULE_RELATIVES:
        return "legacy-deep"
    return "unknown"


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _git_commit(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    result = _git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def _path_relation(source: Path, target: Path) -> None:
    if source == target:
        raise KBError("migration source and target must be different")
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise KBError("migration target must not be inside the source instance")
    try:
        source.relative_to(target)
    except ValueError:
        return
    raise KBError("migration source must not be inside the target instance")


def _file_action(relative: str) -> str:
    folded = relative.replace("\\", "/")
    if "__pycache__" in folded.split("/") or folded.endswith((".pyc", ".pyo")):
        return "exclude-transient"
    if any(folded.startswith(prefix) for prefix in TRANSIENT_PREFIXES):
        return "exclude-transient"
    if any(folded.startswith(prefix) for prefix in CREDENTIAL_PREFIXES):
        return "transfer-credential"
    if folded in TARGET_MANAGED_FILES or any(
        folded.startswith(prefix) for prefix in TARGET_MANAGED_PREFIXES
    ):
        return "preserve-current-template"
    if folded.startswith("vault/"):
        return "copy-object"
    if folded.startswith(("receipts/", "recovery/")):
        return "preserve-local-history"
    return "semantic-import-candidate"


def _migration_files(source_knowledge: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(source_knowledge.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_knowledge).as_posix()
        files.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "action": _file_action(relative),
            }
        )
    return files


def _workspace_material_files(source: Path, target: Path) -> list[dict[str, Any]]:
    """Inventory user-facing material without entering tool or Agent state."""
    files: list[dict[str, Any]] = []
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    for current, directory_names, file_names in os.walk(source, topdown=True):
        current_path = Path(current).resolve()
        retained_directories: list[str] = []
        for name in directory_names:
            candidate = (current_path / name).resolve()
            if name.startswith(".") or name.casefold() in WORKSPACE_EXCLUDED_DIRECTORIES:
                continue
            if candidate == target:
                continue
            try:
                candidate.relative_to(target)
            except ValueError:
                pass
            else:
                continue
            if (current_path / name).is_symlink():
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in file_names:
            path = current_path / name
            if path.is_symlink() or name.startswith("."):
                continue
            relative = path.relative_to(source).as_posix()
            if len(Path(relative).parts) == 1 and name.casefold() in WORKSPACE_CONTROL_FILES:
                continue
            if path.suffix.casefold() not in WORKSPACE_CONTENT_SUFFIXES:
                continue
            files.append(
                {
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                    "media_hint": path.suffix.casefold().lstrip(".") or "file",
                }
            )
    return sorted(files, key=lambda item: str(item["path"]).casefold())


def _workspace_material_fingerprint(files: list[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(str(item["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["sha256"]).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def workspace_materials_plan(
    source: str | Path,
    target: str | Path,
) -> dict[str, Any]:
    source_root = Path(source).expanduser().resolve()
    target_root = Path(target).expanduser().resolve()
    if not source_root.is_dir():
        raise KBError("existing material source must be a directory")
    if source_root == target_root:
        return {
            "status": "none",
            "flow": "knowledge-existing-materials",
            "source": str(source_root),
            "target": str(target_root),
            "candidate_count": 0,
            "candidate_bytes": 0,
            "fingerprint": _workspace_material_fingerprint([]),
            "groups": [],
            "sample_paths": [],
            "confirmation_required": [],
            "required_inputs": [],
        }
    try:
        target_root.relative_to(source_root)
    except ValueError:
        return {
            "status": "none",
            "flow": "knowledge-existing-materials",
            "source": str(source_root),
            "target": str(target_root),
            "candidate_count": 0,
            "candidate_bytes": 0,
            "fingerprint": _workspace_material_fingerprint([]),
            "groups": [],
            "sample_paths": [],
            "confirmation_required": [],
            "required_inputs": [],
        }
    files = _workspace_material_files(source_root, target_root)
    migration_state = _read_json(target_root / MIGRATION_STATE) or {}
    absorbed: dict[str, Mapping[str, Any]] = {}
    if (
        migration_state.get("source_kind") == "workspace-materials"
        and migration_state.get("source") == str(source_root)
    ):
        absorbed = {
            str(item.get("path")): item
            for item in migration_state.get("semantic_candidates", [])
            if isinstance(item, Mapping)
            and item.get("path")
            and item.get("status") in ("completed", "routed")
        }
    candidate_files = [
        item
        for item in files
        if not (
            str(item["path"]) in absorbed
            and absorbed[str(item["path"])].get("sha256") == item["sha256"]
        )
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for item in candidate_files:
        parts = Path(str(item["path"])).parts
        group_name = parts[0] if len(parts) > 1 else "root-files"
        group = grouped.setdefault(
            group_name,
            {"group": group_name, "count": 0, "bytes": 0, "sample_paths": []},
        )
        group["count"] += 1
        group["bytes"] += int(item["bytes"])
        if len(group["sample_paths"]) < 5:
            group["sample_paths"].append(item["path"])
    count = len(candidate_files)
    return {
        "status": "needs-input" if count else "none",
        "flow": "knowledge-existing-materials",
        "source": str(source_root),
        "target": str(target_root),
        "candidate_count": count,
        "candidate_bytes": sum(int(item["bytes"]) for item in candidate_files),
        "fingerprint": _workspace_material_fingerprint(files),
        "groups": [grouped[name] for name in sorted(grouped, key=str.casefold)],
        "sample_paths": [str(item["path"]) for item in candidate_files[:10]],
        "confirmation_required": ["choose-existing-materials-action"] if count else [],
        "required_inputs": ["existing-materials-action"] if count else [],
        "action_options": (
            [
                {
                    "choice": "import-review",
                    "recommended": True,
                    "effect": "preserve-source-and-stage-for-semantic-review",
                },
                {
                    "choice": "leave-in-place",
                    "recommended": False,
                    "effect": "continue-without-importing-existing-materials",
                },
            ]
            if count
            else []
        ),
    }


def stage_workspace_materials(
    source: str | Path,
    target: str | Path,
    *,
    confirm_existing_materials_import: bool = False,
    planned_repository: str | None = None,
    age_path: str | Path | None = None,
) -> dict[str, Any]:
    if not confirm_existing_materials_import:
        raise KBError("existing material import requires explicit confirmation")
    source_root = Path(source).expanduser().resolve()
    target_root = Path(target).expanduser().resolve()
    if not is_personal_domain_root(target_root):
        raise KBError("existing materials require an initialized personal workspace")
    plan = workspace_materials_plan(source_root, target_root)
    if not plan["candidate_count"]:
        raise KBError("no existing materials are available for review")
    state_path = target_root / MIGRATION_STATE
    existing = _read_json(state_path) or {}
    files = _workspace_material_files(source_root, target_root)
    current_fingerprint = _workspace_material_fingerprint(files)
    if existing:
        if (
            existing.get("source_kind") == "workspace-materials"
            and existing.get("source") == str(source_root)
            and existing.get("status") in ("needs-semantic-review", "verified")
            and existing.get("source_fingerprint") == current_fingerprint
        ):
            return {
                "status": "needs-action" if existing.get("status") != "verified" else "ok",
                "flow": "knowledge-existing-materials",
                "source": str(source_root),
                "target": str(target_root),
                "candidate_count": existing.get("semantic_import", {}).get("candidate_count", 0),
                "pending_count": existing.get("semantic_import", {}).get("pending_count", 0),
                "terminal_state": (
                    "needs-semantic-review"
                    if existing.get("status") != "verified"
                    else "ready-for-initial-sync"
                ),
                "next_action": (
                    "migration-review"
                    if existing.get("status") != "verified"
                    else "workspace-start"
                ),
                "repository_creation_deferred": existing.get("status") != "verified",
                "initial_sync_deferred": existing.get("status") != "verified",
            }
        if not (
            existing.get("source_kind") == "workspace-materials"
            and existing.get("source") == str(source_root)
            and existing.get("status") in ("copying", "needs-semantic-review", "verified")
        ):
            raise KBError("workspace already contains another migration or import state")
    state: dict[str, Any] = existing or {
        "schema_version": "1.1",
        "flow": "knowledge-existing-materials",
        "status": "copying",
        "source": str(source_root),
        "source_kind": "workspace-materials",
        "source_layout": "loose-workspace",
        "source_fingerprint": current_fingerprint,
        "source_repository": None,
        "source_commit": _git_commit(source_root),
        "target": str(target_root),
        "planned_repository": planned_repository,
        "started_at": _utc_now(),
        "completed_files": [],
        "semantic_candidates": [],
        "source_preserved": True,
        "retirement_required": False,
    }
    state["status"] = "copying"
    state["source_fingerprint"] = current_fingerprint
    if planned_repository:
        state["planned_repository"] = planned_repository
    _atomic_json(state_path, state)
    inbox_root = target_root / KNOWLEDGE_MODULE_RELATIVE / "inbox" / "migration" / "workspace"
    candidates_by_path = {
        str(item.get("path")): dict(item)
        for item in state.get("semantic_candidates", [])
        if isinstance(item, Mapping) and item.get("path")
    }
    completed = {
        str(value) for value in state.get("completed_files", []) if isinstance(value, str)
    }
    for item in files:
        relative = str(item["path"])
        source_path = source_root / Path(relative)
        destination = inbox_root / Path(relative)
        if not destination.is_file() or _sha256(destination) != item["sha256"]:
            _copy_atomic(source_path, destination)
        if _sha256(destination) != item["sha256"]:
            raise KBError(f"existing material staging verification failed: {relative}")
        previous = candidates_by_path.get(relative, {})
        unchanged_finished = (
            previous.get("sha256") == item["sha256"]
            and previous.get("status") in ("completed", "routed")
        )
        candidates_by_path[relative] = dict(previous) if unchanged_finished else {
                "path": relative,
                "staged_path": destination.relative_to(target_root).as_posix(),
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "status": "pending",
                "media_hint": item["media_hint"],
                "raw_object_id": None,
                "wiki_object_ids": [],
                "clarification_status": "required",
                "required_fields": [
                    "authorship_status",
                    "intended_role",
                    "rights",
                    "access",
                    "wiki_compilation",
                ],
            }
        completed.add(relative)
        state["semantic_candidates"] = [
            candidates_by_path[name] for name in sorted(candidates_by_path, key=str.casefold)
        ]
        state["completed_files"] = sorted(completed, key=str.casefold)
        _atomic_json(state_path, state)
    candidates = [
        candidates_by_path[name] for name in sorted(candidates_by_path, key=str.casefold)
    ]
    pending = [
        item for item in candidates if item.get("status") not in ("completed", "routed")
    ]
    imported_count = sum(1 for item in candidates if item.get("status") == "completed")
    routed_count = sum(1 for item in candidates if item.get("status") == "routed")
    state.update(
        {
            "status": "needs-semantic-review" if pending else "verified",
            "semantic_import": {
                "candidate_count": len(candidates),
                "completed_count": len(candidates) - len(pending),
                "pending_count": len(pending),
                "imported_count": imported_count,
                "routed_count": routed_count,
                "llm_wiki_required": any(
                    item.get("status") == "completed" and not item.get("raw_only")
                    for item in candidates
                ),
            },
            "verification": KnowledgeVault(
                resolve_knowledge_root(target_root), age_path=age_path
            ).verify(),
        }
    )
    _atomic_json(state_path, state)
    return {
        "status": "needs-action",
        "flow": "knowledge-existing-materials",
        "source": str(source_root),
        "target": str(target_root),
        "candidate_count": len(pending),
        "total_candidate_count": len(candidates),
        "pending_count": len(pending),
        "groups": plan["groups"],
        "sample_paths": plan["sample_paths"],
        "source_preserved": True,
        "repository_creation_deferred": True,
        "initial_sync_deferred": True,
        "batch_questions": [
            "这些同组文件分别属于本人原创、修改/共同创作、AI 辅助还是外部资料？",
            "这些材料应进入知识库，还是确认保留在知识库之外？",
            "你是否拥有保存和整理权利，默认访问级别应为 public、basic、advanced 还是 core？",
            "这些文件都需要生成来源摘要、原子卡片和主题页，还是有些只保留 raw？",
        ],
        "terminal_state": "needs-semantic-review",
        "next_action": "migration-review",
    }


def _semantic_completed_paths(state: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            normalized
            for value in state.get("completed_files", [])
            if isinstance(value, str)
            and (normalized := _safe_migration_relative(value)) is not None
            and _file_action(normalized) == "semantic-import-candidate"
        }
    )


def _safe_migration_relative(value: str) -> str | None:
    folded = value.replace("\\", "/").strip()
    candidate = Path(folded)
    if (
        not folded
        or candidate.is_absolute()
        or re.match(r"^[A-Za-z]:", folded)
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        return None
    return candidate.as_posix()


def _candidate_source(target: Path, state: Mapping[str, Any], relative: str) -> Path | None:
    candidates = [
        target / KNOWLEDGE_MODULE_RELATIVE / Path(relative),
        resolve_knowledge_root(target) / Path(relative),
    ]
    source_value = str(state.get("source", "")).strip()
    if source_value:
        source_root = Path(source_value).expanduser()
        if source_root.exists():
            try:
                candidates.append(resolve_knowledge_root(source_root) / Path(relative))
            except KBError:
                pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def migration_repair_plan(target: str | Path) -> dict[str, Any]:
    target_root = Path(target).expanduser().resolve()
    state = _read_json(target_root / MIGRATION_STATE)
    if not state:
        return {
            "status": "not-applicable",
            "flow": "knowledge-migration-repair",
            "target": str(target_root),
            "repair_required": False,
            "reasons": [],
            "candidate_count": 0,
        }
    semantic_paths = _semantic_completed_paths(state)
    invalid_paths = sorted(
        str(value)
        for value in state.get("completed_files", [])
        if isinstance(value, str) and _safe_migration_relative(value) is None
    )
    verification = state.get("verification")
    objects_checked = (
        int(verification.get("objects_checked", 0))
        if isinstance(verification, Mapping)
        else 0
    )
    reasons: list[str] = []
    recorded_candidates = state.get("semantic_candidates")
    imported_candidates = (
        [
            item
            for item in recorded_candidates
            if isinstance(item, Mapping) and item.get("status") == "completed"
        ]
        if isinstance(recorded_candidates, list)
        else []
    )
    if state.get("status") == "verified" and imported_candidates and objects_checked <= 0:
        reasons.append("semantic-files-have-no-verified-objects")
    if state.get("status") == "verified" and semantic_paths and not isinstance(
        state.get("semantic_import"), Mapping
    ):
        reasons.append("semantic-import-receipt-is-missing")
    if state.get("status") == "verified" and semantic_paths and not isinstance(
        recorded_candidates, list
    ):
        reasons.append("semantic-candidate-records-are-missing")
    if invalid_paths:
        reasons.append("invalid-completed-file-paths")
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for relative in semantic_paths:
        source = _candidate_source(target_root, state, relative)
        if source is None:
            missing.append(relative)
            continue
        found.append(
            {
                "path": relative,
                "source_path": str(source),
                "bytes": source.stat().st_size,
                "sha256": _sha256(source),
            }
        )
    repair_required = bool(reasons)
    return {
        "status": "needs-confirmation" if repair_required else "ok",
        "flow": "knowledge-migration-repair",
        "target": str(target_root),
        "source": state.get("source"),
        "migration_status": state.get("status"),
        "repair_required": repair_required,
        "reasons": reasons,
        "objects_checked": objects_checked,
        "candidate_count": len(semantic_paths),
        "recoverable_count": len(found),
        "missing_count": len(missing),
        "invalid_count": len(invalid_paths),
        "candidates": found,
        "missing_paths": missing,
        "invalid_paths": invalid_paths,
        "confirmation_required": (
            ["confirm-migration-state-repair"] if repair_required else []
        ),
        "next_action": "migration-repair-start" if repair_required else None,
        "terminal_state": "needs-migration-repair" if repair_required else "current",
    }


def repair_migration(
    target: str | Path,
    *,
    confirm_migration_state_repair: bool = False,
) -> dict[str, Any]:
    target_root = Path(target).expanduser().resolve()
    plan = migration_repair_plan(target_root)
    if not plan["repair_required"]:
        return {**plan, "status": "ok", "changed": False}
    if not confirm_migration_state_repair:
        raise KBError("migration state repair requires explicit confirmation")
    if plan["missing_count"]:
        raise KBError("migration repair cannot locate every recorded source file")
    if plan["invalid_count"]:
        raise KBError("migration repair contains unsafe recorded source paths")
    state_path = target_root / MIGRATION_STATE
    state = _read_json(state_path)
    if not state:
        raise KBError("migration state is unavailable")
    inbox_root = target_root / KNOWLEDGE_MODULE_RELATIVE / "inbox" / "migration"
    backup_root = target_root / ".atlas" / "review" / "migration-upgrade-originals"
    workspace_root = (target_root / KNOWLEDGE_MODULE_RELATIVE).resolve()
    semantic_candidates: list[dict[str, Any]] = []
    moved_from_workspace = 0
    for item in plan["candidates"]:
        relative = str(item["path"])
        source = Path(str(item["source_path"])).resolve()
        destination = inbox_root / Path(relative)
        backup = backup_root / Path(relative)
        if not backup.is_file() or _sha256(backup) != item["sha256"]:
            _copy_atomic(source, backup)
        if not destination.is_file() or _sha256(destination) != item["sha256"]:
            _copy_atomic(source, destination)
        if _sha256(destination) != item["sha256"]:
            raise KBError(f"migration repair copy verification failed: {relative}")
        if source != destination.resolve():
            try:
                source.relative_to(workspace_root)
            except ValueError:
                pass
            else:
                if _sha256(backup) != _sha256(source):
                    raise KBError(f"migration repair backup verification failed: {relative}")
                source.unlink()
                moved_from_workspace += 1
                parent = source.parent
                while parent != workspace_root and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        semantic_candidates.append(
            {
                "path": relative,
                "staged_path": destination.relative_to(target_root).as_posix(),
                "bytes": int(item["bytes"]),
                "sha256": str(item["sha256"]),
                "status": "pending",
                "raw_object_id": None,
                "wiki_object_ids": [],
            }
        )
    prior_verification = state.get("verification")
    state.update(
        {
            "status": "needs-semantic-review",
            "verified_at": None,
            "semantic_candidates": semantic_candidates,
            "semantic_import": {
                "candidate_count": len(semantic_candidates),
                "completed_count": 0,
                "pending_count": len(semantic_candidates),
                "llm_wiki_required": bool(semantic_candidates),
            },
            "upgrade": {
                "repaired_at": _utc_now(),
                "reasons": plan["reasons"],
                "legacy_verification": prior_verification,
                "backup_root": backup_root.relative_to(target_root).as_posix(),
            },
        }
    )
    state.pop("verification", None)
    _atomic_json(state_path, state)
    return {
        "status": "needs-semantic-review",
        "flow": "knowledge-migration-repair",
        "target": str(target_root),
        "changed": True,
        "candidate_count": len(semantic_candidates),
        "moved_from_workspace": moved_from_workspace,
        "backup_root": str(backup_root),
        "terminal_state": "needs-semantic-review",
        "next_action": "migration-review",
    }


def _private_tiers(files: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for item in files:
        parts = Path(str(item["path"])).parts
        if len(parts) >= 2 and parts[0] == "vault" and parts[1] in (
            "basic",
            "advanced",
            "core",
        ):
            found.add(parts[1])
    return [tier for tier in ("basic", "advanced", "core") if tier in found]


def migration_plan(
    source: str | Path,
    target: str | Path,
    *,
    identities: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    source_root = Path(source).expanduser().resolve()
    target_root = Path(target).expanduser().resolve()
    _path_relation(source_root, target_root)
    source_layout = _layout(source_root)
    if not source_layout.startswith("legacy"):
        raise KBError("migration source must be a compatible legacy knowledge instance")
    source_knowledge = resolve_knowledge_root(source_root)
    target_state = "new-path"
    if target_root.exists():
        if not target_root.is_dir():
            raise KBError("migration target is not a directory")
        state = _read_json(target_root / MIGRATION_STATE)
        if state and state.get("status") == "verified":
            repair = migration_repair_plan(target_root)
            target_state = (
                "migration-repair-required"
                if repair["repair_required"]
                else "migrated-instance"
            )
        elif any(target_root.iterdir()):
            target_state = "occupied-directory"
        else:
            target_state = "empty-directory"
    files = _migration_files(source_knowledge)
    private_tiers = _private_tiers(files)
    source_test_identities = _test_identities(source_knowledge)
    supplied_identities = {
        tier
        for tier, path in (identities or {}).items()
        if tier in ("basic", "advanced", "core") and Path(path).is_file()
    }
    verifiable_tiers = set(source_test_identities) | supplied_identities
    missing_identity_tiers = [
        tier for tier in private_tiers if tier not in verifiable_tiers
    ]
    counts: dict[str, int] = {}
    byte_counts: dict[str, int] = {}
    for item in files:
        action = str(item["action"])
        counts[action] = counts.get(action, 0) + 1
        byte_counts[action] = byte_counts.get(action, 0) + int(item["bytes"])
    repository = configured_repository(source_root)
    confirmations = ["confirm-content-migration"]
    if counts.get("transfer-credential"):
        confirmations.append("confirm-local-credential-transfer")
    if target_state == "occupied-directory":
        confirmations.append("choose-empty-migration-target")
    return {
        "status": "needs-confirmation" if confirmations else "ready",
        "flow": "knowledge-instance-migration",
        "source": str(source_root),
        "source_knowledge_root": str(source_knowledge),
        "source_layout": source_layout,
        "source_commit": _git_commit(source_root),
        "source_repository": (
            f"{repository['owner']}/{repository['repo']}" if repository else None
        ),
        "target": str(target_root),
        "target_knowledge_root": str(target_root / PERSONAL_DOMAIN_RUNTIME_RELATIVE),
        "target_knowledge_workspace": str(target_root / KNOWLEDGE_MODULE_RELATIVE),
        "target_state": target_state,
        "files": files,
        "counts": counts,
        "bytes": byte_counts,
        "private_tiers": private_tiers,
        "missing_identity_tiers": missing_identity_tiers,
        "required_inputs": (
            ["private-tier-identities"] if missing_identity_tiers else []
        ),
        "confirmation_required": confirmations,
        "default_source_disposition": "preserve",
        "source_disposition_options": ["preserve", "archive", "delete"],
    }


def prepare_migration_source(
    repository: str,
    target: str | Path,
    *,
    confirm_existing_repository: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    if "/" not in repository:
        raise KBError("migration source repository must use owner/name")
    owner, name = repository.split("/", 1)
    source_root = Path(target).expanduser().resolve()
    if source_root.exists():
        if _layout(source_root).startswith("legacy"):
            configured = configured_repository(source_root)
            configured_name = (
                f"{configured['owner']}/{configured['repo']}" if configured else None
            )
            if configured_name in (None, repository):
                return {
                    "status": "ok",
                    "flow": "knowledge-migration-source",
                    "repository": repository,
                    "source": str(source_root),
                    "cloned": False,
                    "layout": _layout(source_root),
                }
        raise KBError("migration source target already contains another instance or content")
    if not confirm_existing_repository:
        return {
            "status": "needs-confirmation",
            "flow": "knowledge-migration-source",
            "repository": repository,
            "source": str(source_root),
            "confirmation_required": ["confirm-existing-source-repository-clone"],
        }
    active = client or GitHubCLIEnvironment().client()
    if active.repository(owner, name) is None:
        raise KBError("migration source repository is unavailable")
    clone = getattr(active, "clone_repository", None)
    if not callable(clone):
        raise KBError("connected GitHub client does not support source cloning")
    stage = source_root.parent / f".{source_root.name}.source-clone"
    if stage.exists():
        if not _layout(stage).startswith("legacy"):
            raise KBError("migration source staging directory requires review")
    else:
        clone(owner, name, stage)
    layout = _layout(stage)
    if not layout.startswith("legacy"):
        raise KBError("cloned repository is not a compatible legacy knowledge instance")
    staged_repository = configured_repository(stage)
    if staged_repository and (
        f"{staged_repository['owner']}/{staged_repository['repo']}" != repository
    ):
        raise KBError("migration source staging repository does not match the request")
    stage.replace(source_root)
    return {
        "status": "ok",
        "flow": "knowledge-migration-source",
        "repository": repository,
        "source": str(source_root),
        "cloned": True,
        "layout": layout,
    }


def _staging_root(source: Path, target: Path) -> Path:
    marker = hashlib.sha256(str(source).casefold().encode("utf-8")).hexdigest()[:12]
    return target.parent / f".{target.name}.migration-{marker}"


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".migration-tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def _test_identities(knowledge_root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for tier in ("basic", "advanced", "core"):
        path = knowledge_root / ".local" / "test-keys" / f"{tier}.identity"
        if path.is_file():
            found[tier] = path
    return found


def migrate_instance(
    source: str | Path,
    target: str | Path,
    *,
    age_path: str | Path | None = None,
    confirm_content_migration: bool = False,
    confirm_local_credential_transfer: bool = False,
    identities: Mapping[str, str | Path] | None = None,
    _fail_after: int | None = None,
) -> dict[str, Any]:
    plan = migration_plan(source, target, identities=identities)
    if not confirm_content_migration:
        raise KBError("content migration requires explicit confirmation")
    if plan["counts"].get("transfer-credential") and not confirm_local_credential_transfer:
        raise KBError("local credential transfer requires explicit confirmation")
    if plan["target_state"] == "occupied-directory":
        raise KBError("migration target must be empty")
    if plan["target_state"] == "migration-repair-required":
        raise KBError("existing migration state requires repair before migration can continue")
    if plan["missing_identity_tiers"]:
        raise KBError(
            "identity is required to verify migrated private tiers: "
            + ", ".join(plan["missing_identity_tiers"])
        )
    source_root = Path(plan["source"])
    source_knowledge = Path(plan["source_knowledge_root"])
    target_root = Path(plan["target"])
    if plan["target_state"] == "migrated-instance":
        state = _read_json(target_root / MIGRATION_STATE) or {}
        if state.get("source") == str(source_root) and state.get("status") == "verified":
            return {
                "status": "ok",
                "flow": "knowledge-instance-migration",
                "source": str(source_root),
                "target": str(target_root),
                "verified": True,
                "resumed": True,
                "source_preserved": source_root.exists(),
                "retirement_required": True,
                "retirement_options": ["preserve", "archive", "delete"],
            }
        raise KBError("migration target belongs to another migration")
    stage = _staging_root(source_root, target_root)
    state_path = stage / MIGRATION_STATE
    existing_state = _read_json(state_path) if stage.exists() else None
    if stage.exists() and (
        not existing_state or existing_state.get("source") != str(source_root)
    ):
        raise KBError("migration staging directory belongs to another operation")
    if not stage.exists():
        initialize_personal_domain(stage)
    state: dict[str, Any] = existing_state or {
        "schema_version": "1.0",
        "status": "copying",
        "source": str(source_root),
        "source_layout": plan["source_layout"],
        "source_commit": plan["source_commit"],
        "source_repository": plan["source_repository"],
        "target": str(target_root),
        "started_at": _utc_now(),
        "completed_files": [],
    }
    _atomic_json(state_path, state)
    target_knowledge = resolve_knowledge_root(stage)
    backup_root = stage / ".atlas" / "review" / "migration-source-files"
    history_root = stage / ".atlas" / "review" / "migration-history"
    inbox_root = stage / KNOWLEDGE_MODULE_RELATIVE / "inbox" / "migration"
    completed = set(str(value) for value in state.get("completed_files", []))
    semantic_candidates = {
        str(item.get("path")): dict(item)
        for item in state.get("semantic_candidates", [])
        if isinstance(item, Mapping) and item.get("path")
    }
    copied = 0
    preserved = 0
    for item in plan["files"]:
        relative = str(item["path"])
        action = str(item["action"])
        source_path = source_knowledge / Path(relative)
        if action == "exclude-transient":
            continue
        if action == "preserve-current-template":
            backup = backup_root / Path(relative)
            if not backup.is_file() or _sha256(backup) != item["sha256"]:
                _copy_atomic(source_path, backup)
            preserved += 1
            continue
        if action == "preserve-local-history":
            destination = history_root / Path(relative)
            if not destination.is_file() or _sha256(destination) != item["sha256"]:
                _copy_atomic(source_path, destination)
            completed.add(relative)
            continue
        if action == "semantic-import-candidate":
            destination = inbox_root / Path(relative)
            if not destination.is_file() or _sha256(destination) != item["sha256"]:
                _copy_atomic(source_path, destination)
            semantic_candidates[relative] = {
                "path": relative,
                "staged_path": destination.relative_to(stage).as_posix(),
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "status": semantic_candidates.get(relative, {}).get("status", "pending"),
                "raw_object_id": semantic_candidates.get(relative, {}).get("raw_object_id"),
                "wiki_object_ids": semantic_candidates.get(relative, {}).get("wiki_object_ids", []),
            }
            completed.add(relative)
            state["semantic_candidates"] = [
                semantic_candidates[name] for name in sorted(semantic_candidates)
            ]
            state["completed_files"] = sorted(completed)
            _atomic_json(state_path, state)
            copied += 1
            if _fail_after is not None and copied >= _fail_after:
                raise KBError("simulated migration interruption")
            continue
        if action == "transfer-credential" and not confirm_local_credential_transfer:
            continue
        destination = target_knowledge / Path(relative)
        if destination.is_file() and _sha256(destination) == item["sha256"]:
            completed.add(relative)
            continue
        _copy_atomic(source_path, destination)
        if _sha256(destination) != item["sha256"]:
            raise KBError(f"migration copy verification failed: {relative}")
        completed.add(relative)
        copied += 1
        state["completed_files"] = sorted(completed)
        _atomic_json(state_path, state)
        if _fail_after is not None and copied >= _fail_after:
            raise KBError("simulated migration interruption")
    vault = KnowledgeVault(target_knowledge, age_path=age_path)
    vault.reindex()
    verification_identities: dict[str, str | Path] = dict(identities or {})
    verification_identities.update(_test_identities(target_knowledge))
    verification = vault.verify(identities=verification_identities or None)
    if not verification["ok"]:
        state["status"] = "verification-failed"
        state["verification"] = verification
        _atomic_json(state_path, state)
        raise KBError("migrated knowledge verification failed")
    pending_candidates = [
        item for item in semantic_candidates.values() if item.get("status") != "completed"
    ]
    state.update(
        {
            "status": "needs-semantic-review" if pending_candidates else "verified",
            "verified_at": None if pending_candidates else _utc_now(),
            "verification": verification,
            "copied_files": copied,
            "preserved_template_files": preserved,
            "source_preserved": True,
            "semantic_import": {
                "candidate_count": len(semantic_candidates),
                "completed_count": len(semantic_candidates) - len(pending_candidates),
                "pending_count": len(pending_candidates),
                "llm_wiki_required": bool(pending_candidates),
            },
        }
    )
    _atomic_json(state_path, state)
    if target_root.exists():
        if any(target_root.iterdir()):
            raise KBError("migration target changed before activation")
        target_root.rmdir()
    stage.replace(target_root)
    return {
        "status": "ok",
        "flow": "knowledge-instance-migration",
        "source": str(source_root),
        "target": str(target_root),
        "knowledge_root": str(resolve_knowledge_root(target_root)),
        "knowledge_workspace": str(target_root / KNOWLEDGE_MODULE_RELATIVE),
        "copied_files": copied,
        "preserved_template_files": preserved,
        "verified": not pending_candidates,
        "original_files_verified": True,
        "semantic_import": state["semantic_import"],
        "terminal_state": "needs-semantic-review" if pending_candidates else "ready-for-retirement",
        "verification": verification,
        "resumed": bool(existing_state),
        "source_preserved": source_root.exists(),
        "retirement_required": True,
        "retirement_options": ["preserve", "archive", "delete"],
        "recommended_retirement": "preserve",
    }


def record_migration_candidate(
    target: str | Path,
    *,
    source_path: str,
    raw_object_id: str,
    wiki_object_ids: list[str] | tuple[str, ...] = (),
    identities: Mapping[str, str | Path] | None = None,
    confirm_raw_only: bool = False,
    age_path: str | Path | None = None,
) -> dict[str, Any]:
    target_root = Path(target).expanduser().resolve()
    state_path = target_root / MIGRATION_STATE
    state = _read_json(state_path)
    if not state or state.get("status") not in ("needs-semantic-review", "verified"):
        raise KBError("target does not contain a semantic migration review")
    candidates = state.get("semantic_candidates")
    if not isinstance(candidates, list):
        raise KBError("migration semantic candidate list is unavailable")
    selected = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("path")) == source_path
        ),
        None,
    )
    if selected is None:
        raise KBError("migration semantic candidate does not exist")
    staged = target_root / str(selected["staged_path"])
    if not staged.is_file() or _sha256(staged) != selected.get("sha256"):
        raise KBError("migration candidate source verification failed")
    try:
        staged_content = staged.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise KBError("migration candidate requires text extraction before import") from exc
    vault = KnowledgeVault(resolve_knowledge_root(target_root), age_path=age_path)
    raw_path = vault._locate_object(raw_object_id)
    raw = vault._read_object_path(raw_path, identities)
    if raw.get("object_kind") != "raw" or str(raw.get("content")) != staged_content:
        raise KBError("migration raw object does not preserve the staged source")
    wiki_kinds: set[str] = set()
    verified_wiki_ids: list[str] = []
    view_envelopes: list[Mapping[str, Any]] = [raw]
    for object_id in wiki_object_ids:
        path = vault._locate_object(object_id)
        envelope = vault._read_object_path(path, identities)
        if envelope.get("object_kind") != "wiki":
            raise KBError("migration compiled object must be a wiki object")
        sources = {
            *[str(value) for value in envelope.get("source_ids", [])],
            *[str(value) for value in envelope.get("source_refs", [])],
        }
        if raw_object_id not in sources:
            raise KBError("migration wiki object is not traceable to the raw source")
        wiki_kinds.add(str(envelope.get("wiki_kind")))
        verified_wiki_ids.append(object_id)
        view_envelopes.append(envelope)
    required_wiki = {"source_summary", "atomic_card", "topic_page"}
    if not confirm_raw_only and not required_wiki.issubset(wiki_kinds):
        raise KBError(
            "migration LLM Wiki requires source_summary, atomic_card, and topic_page"
        )
    selected.update(
        {
            "status": "completed",
            "raw_object_id": raw_object_id,
            "wiki_object_ids": verified_wiki_ids,
            "wiki_kinds": sorted(wiki_kinds),
            "completed_at": _utc_now(),
            "raw_only": bool(confirm_raw_only),
        }
    )
    finished_statuses = {"completed", "routed"}
    pending = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("status") not in finished_statuses
    ]
    routed_count = sum(
        1
        for item in candidates
        if isinstance(item, dict) and item.get("status") == "routed"
    )
    state["semantic_import"] = {
        "candidate_count": len(candidates),
        "completed_count": len(candidates) - len(pending),
        "pending_count": len(pending),
        "imported_count": len(candidates) - len(pending) - routed_count,
        "routed_count": routed_count,
        "llm_wiki_required": any(
            isinstance(item, dict)
            and item.get("status") == "completed"
            and not item.get("raw_only")
            for item in candidates
        ),
    }
    if not pending:
        verification = vault.verify(identities=identities)
        if not verification["ok"] or not verification["objects_checked"]:
            raise KBError("semantic migration object verification failed")
        state["status"] = "verified"
        state["verified_at"] = _utc_now()
        state["verification"] = verification
    views = materialize_workspace_views(target_root, view_envelopes)
    _atomic_json(state_path, state)
    retirement_required = bool(state.get("retirement_required", True))
    ready_terminal = (
        "ready-for-retirement" if retirement_required else "ready-for-initial-sync"
    )
    return {
        "status": "ok" if not pending else "needs-semantic-review",
        "flow": "knowledge-migration-semantic-review",
        "source_path": source_path,
        "raw_object_id": raw_object_id,
        "wiki_object_ids": verified_wiki_ids,
        "pending_count": len(pending),
        "migration_verified": not pending,
        "objects_checked": (
            state.get("verification", {}).get("objects_checked", 0)
            if isinstance(state.get("verification"), Mapping)
            else 0
        ),
        "workspace_views": views,
        "terminal_state": ready_terminal if not pending else "needs-semantic-review",
        "retirement_required": retirement_required,
    }


def import_workspace_candidate(
    target: str | Path,
    *,
    source_path: str,
    access: str,
    rights: str,
    origin_kind: str,
    authorship_status: str,
    intended_role: str,
    title: str = "",
    summary: str = "",
    card_question: str = "",
    card_answer: str = "",
    card_kind: str = "concept",
    topic_names: Iterable[str] = (),
    raw_only: bool = False,
    confirm_raw_only: bool = False,
    confirm_route_outside_knowledge: bool = False,
    identities: Mapping[str, str | Path] | None = None,
    recipients: Mapping[str, str] | None = None,
    age_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically absorb one staged workspace file into Raw and candidate Wiki objects."""
    if access not in ("public", "basic", "advanced", "core"):
        raise KBError("workspace candidate access must be public, basic, advanced, or core")
    if rights not in ("owned", "licensed", "restricted", "unknown"):
        raise KBError("workspace candidate rights are invalid")
    if origin_kind not in ("self", "external", "mixed", "unknown"):
        raise KBError("workspace candidate origin is invalid")
    if authorship_status not in (
        "self_authored",
        "edited",
        "coauthored",
        "ai_assisted",
        "external",
        "unknown",
    ):
        raise KBError("workspace candidate authorship is invalid")
    if intended_role not in (
        "memory",
        "evidence",
        "knowledge",
        "creation",
        "cognition",
        "capability",
        "work",
        "product",
        "relationship",
        "governance",
        "publication",
        "unknown",
    ):
        raise KBError("workspace candidate intended role is invalid")
    if "unknown" in (origin_kind, authorship_status, intended_role) or rights == "unknown":
        raise KBError("workspace candidate responsibility fields require user clarification")
    topics = [value.strip() for value in topic_names if value.strip()]
    knowledge_candidate = intended_role in ("knowledge", "evidence")
    if knowledge_candidate and raw_only and not confirm_raw_only:
        raise KBError("raw-only workspace import requires explicit confirmation")
    if knowledge_candidate and not raw_only and (not summary.strip() or not topics):
        raise KBError("LLM Wiki import requires an Agent summary and at least one topic")

    target_root = Path(target).expanduser().resolve()
    state = _read_json(target_root / MIGRATION_STATE)
    candidates = state.get("semantic_candidates") if isinstance(state, Mapping) else None
    selected = next(
        (
            item
            for item in candidates or []
            if isinstance(item, Mapping) and str(item.get("path")) == source_path
        ),
        None,
    )
    if selected is None:
        raise KBError("workspace candidate does not exist")
    staged = target_root / str(selected.get("staged_path", ""))
    if not staged.is_file() or _sha256(staged) != selected.get("sha256"):
        raise KBError("workspace candidate source verification failed")
    try:
        content = staged.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise KBError("workspace candidate requires text extraction before import") from exc

    knowledge_root = resolve_knowledge_root(target_root)
    effective_identities = dict(identities or {})
    for tier in ("basic", "advanced", "core"):
        if tier in effective_identities:
            continue
        configured = os.environ.get(f"KB_AGE_IDENTITY_{tier.upper()}_FILE", "").strip()
        identity_candidates = [
            Path(configured).expanduser() if configured else None,
            knowledge_root / ".local" / "test-keys" / f"{tier}.identity",
        ]
        selected_identity = next(
            (
                candidate.resolve()
                for candidate in identity_candidates
                if candidate and candidate.is_file()
            ),
            None,
        )
        if selected_identity is not None:
            effective_identities[tier] = selected_identity
    vault = KnowledgeVault(knowledge_root, age_path=age_path)
    if intended_role not in ("knowledge", "evidence"):
        if not confirm_route_outside_knowledge:
            raise KBError("routing a workspace candidate outside knowledge requires confirmation")
        target_modules = {
            "memory": "memory",
            "creation": "creations",
            "cognition": "cognition",
            "capability": "capabilities",
            "work": "work",
            "product": "products",
            "relationship": "relationships",
            "governance": "governance",
            "publication": "creations",
        }
        selected.update(
            {
                "status": "routed",
                "disposition": "outside-knowledge",
                "target_module": target_modules[intended_role],
                "origin_kind": origin_kind,
                "authorship_status": authorship_status,
                "intended_role": intended_role,
                "rights": rights,
                "access": access,
                "completed_at": _utc_now(),
            }
        )
        finished_statuses = {"completed", "routed"}
        pending = [
            item
            for item in candidates or []
            if isinstance(item, Mapping) and item.get("status") not in finished_statuses
        ]
        routed_count = sum(
            1
            for item in candidates or []
            if isinstance(item, Mapping) and item.get("status") == "routed"
        )
        imported_count = sum(
            1
            for item in candidates or []
            if isinstance(item, Mapping) and item.get("status") == "completed"
        )
        state["semantic_import"] = {
            "candidate_count": len(candidates or []),
            "completed_count": len(candidates or []) - len(pending),
            "pending_count": len(pending),
            "imported_count": imported_count,
            "routed_count": routed_count,
            "llm_wiki_required": imported_count > 0,
        }
        verification = vault.verify(identities=effective_identities)
        if not pending:
            if imported_count and (
                not verification["ok"] or not verification["objects_checked"]
            ):
                raise KBError("workspace candidate import verification failed")
            state["status"] = "verified"
            state["verified_at"] = _utc_now()
            state["verification"] = verification
        _atomic_json(target_root / MIGRATION_STATE, state)
        return {
            "status": "ok" if not pending else "needs-semantic-review",
            "flow": "knowledge-workspace-candidate-route",
            "source_path": source_path,
            "target_module": target_modules[intended_role],
            "source_preserved": True,
            "pending_count": len(pending),
            "migration_verified": not pending,
            "terminal_state": (
                "ready-for-initial-sync" if not pending else "needs-semantic-review"
            ),
            "retirement_required": False,
        }
    request_seed = f"workspace-import:{state.get('source_fingerprint', '')}:{source_path}"
    request_id = stable_token("req", request_seed)
    raw_receipt = vault.add(
        request_id=f"{request_id}:raw",
        tier=access,
        kind="raw",
        title=title.strip() or Path(source_path).stem,
        summary=summary.strip(),
        content=content,
        source_uri=f"workspace:{source_path}",
        source_refs=[f"workspace:{source_path}"],
        rights=rights,
        maturity="seed",
        catalog_visibility="none",
        review_state="candidate",
        origin_kind=origin_kind,
        authorship_status=authorship_status,
        intended_role=intended_role,
        clarification_status="answered",
        recipients=recipients,
        action="workspace-import-raw",
    )
    wiki_ids: list[str] = []
    compilation: dict[str, Any] | None = None
    if not raw_only:
        from .curator import KnowledgeCurator

        compilation = KnowledgeCurator(vault).curate(
            request_id=f"{request_id}:wiki",
            raw_object_ids=[raw_receipt["object_id"]],
            summary=summary.strip(),
            card_question=card_question.strip(),
            card_answer=card_answer.strip(),
            card_kind=card_kind,
            topic_names=topics,
            identities=effective_identities,
            recipients=recipients,
        )
        wiki_ids = [
            compilation["source_summary_id"],
            compilation["atomic_card_id"],
            *[value["object_id"] for value in compilation["topic_pages"]],
        ]
    reviewed = record_migration_candidate(
        target_root,
        source_path=source_path,
        raw_object_id=raw_receipt["object_id"],
        wiki_object_ids=wiki_ids,
        identities=effective_identities,
        confirm_raw_only=raw_only,
        age_path=age_path,
    )
    return {
        **reviewed,
        "flow": "knowledge-workspace-candidate-import",
        "source_path": source_path,
        "raw_object_id": raw_receipt["object_id"],
        "wiki_object_ids": wiki_ids,
        "compilation": compilation,
    }


def _migration_state(target: Path) -> dict[str, Any]:
    state = _read_json(target / MIGRATION_STATE)
    if not state or state.get("status") != "verified":
        raise KBError("target does not contain a verified migration")
    if migration_repair_plan(target)["repair_required"]:
        raise KBError("target migration requires repair before source retirement")
    return state


def _record_retirement(target: Path, details: Mapping[str, Any]) -> None:
    state_path = target / MIGRATION_STATE
    state = _read_json(state_path)
    if not state:
        raise KBError("migration state is unavailable")
    state["retirement"] = {**dict(details), "completed_at": _utc_now()}
    _atomic_json(state_path, state)


def retirement_plan(source: str | Path, target: str | Path) -> dict[str, Any]:
    source_root = Path(source).expanduser().resolve()
    target_root = Path(target).expanduser().resolve()
    state = _migration_state(target_root)
    if state.get("source") != str(source_root):
        raise KBError("migration receipt does not match the selected source")
    source_repository = configured_repository(source_root)
    target_verification = KnowledgeVault(resolve_knowledge_root(target_root)).verify()
    if not target_verification["ok"]:
        raise KBError("target verification must pass before source retirement")
    repository_name = (
        f"{source_repository['owner']}/{source_repository['repo']}"
        if source_repository
        else None
    )
    return {
        "status": "needs-selection",
        "flow": "knowledge-instance-retirement",
        "source": str(source_root),
        "target": str(target_root),
        "source_repository": repository_name,
        "target_verified": True,
        "options": [
            {"choice": "preserve", "recommended": True, "confirmation_required": []},
            {
                "choice": "archive",
                "confirmation_required": ["confirm-source-archive"],
            },
            {
                "choice": "delete",
                "confirmation_required": ["confirm-source-local-delete"]
                + (["confirm-source-remote-delete"] if repository_name else []),
            },
        ],
    }


def _backup_git_history(source: Path, target: Path) -> str | None:
    if not (source / ".git").exists():
        return None
    commit = _git_commit(source) or "unborn"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-") or "source"
    backup = target / ".local" / "migration-backups" / f"{safe}-{commit[:12]}.bundle"
    backup.parent.mkdir(parents=True, exist_ok=True)
    created = _git(source, "bundle", "create", str(backup), "--all")
    if created.returncode != 0:
        raise KBError("unable to create source Git history backup")
    verified = subprocess.run(
        ["git", "bundle", "verify", str(backup)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if verified.returncode != 0:
        raise KBError("source Git history backup verification failed")
    return str(backup)


def _remove_tree(path: Path) -> None:
    def clear_readonly(function: Any, name: str, _error: Any) -> None:
        os.chmod(name, stat.S_IWRITE)
        function(name)

    shutil.rmtree(path, onerror=clear_readonly)


def retire_source(
    source: str | Path,
    target: str | Path,
    *,
    action: str,
    delete_local: bool = False,
    delete_remote: bool = False,
    expected_source_root: str = "",
    expected_repository: str = "",
    confirm_archive: bool = False,
    confirm_delete_local: bool = False,
    confirm_delete_remote: bool = False,
    client: Any | None = None,
) -> dict[str, Any]:
    if action not in ("preserve", "archive", "delete"):
        raise KBError("source disposition must be preserve, archive, or delete")
    plan = retirement_plan(source, target)
    source_root = Path(plan["source"])
    target_root = Path(plan["target"])
    repository = plan["source_repository"]
    if action == "preserve":
        result = {
            "status": "ok",
            "flow": "knowledge-instance-retirement",
            "action": "preserve",
            "source_preserved": source_root.exists(),
            "source_repository_preserved": bool(repository),
            "terminal_state": "complete",
        }
        _record_retirement(target_root, result)
        return result
    if (source_root / ".git").exists():
        dirty = _git(source_root, "status", "--porcelain")
        if dirty.returncode != 0 or dirty.stdout.strip():
            raise KBError("source Git workspace must be clean before archive or deletion")
    backup = _backup_git_history(source_root, target_root)
    if action == "archive":
        if not confirm_archive:
            raise KBError("source archive requires explicit confirmation")
        archived = source_root.with_name(source_root.name + ".archive")
        if archived.exists():
            raise KBError("source archive target already exists")
        source_root.replace(archived)
        result = {
            "status": "ok",
            "flow": "knowledge-instance-retirement",
            "action": "archive",
            "archived_source": str(archived),
            "source_repository_preserved": bool(repository),
            "git_history_backup": backup,
            "terminal_state": "complete",
        }
        _record_retirement(target_root, result)
        return result
    if not delete_local and not delete_remote:
        raise KBError("select local deletion, remote deletion, or both")
    if delete_remote:
        if not repository or expected_repository != repository:
            raise KBError("remote repository name must be repeated exactly before deletion")
        if not confirm_delete_remote:
            raise KBError("remote repository deletion requires explicit confirmation")
        active = client or GitHubCLIEnvironment().client()
        delete_method = getattr(active, "delete_repository", None)
        if not callable(delete_method):
            raise KBError("connected GitHub client does not support repository deletion")
        owner, name = repository.split("/", 1)
        delete_method(owner, name)
    if delete_local:
        if expected_source_root != str(source_root):
            raise KBError("local source path must be repeated exactly before deletion")
        if not confirm_delete_local:
            raise KBError("local source deletion requires explicit confirmation")
        if source_root.exists():
            _remove_tree(source_root)
    result = {
        "status": "ok",
        "flow": "knowledge-instance-retirement",
        "action": "delete",
        "local_deleted": bool(delete_local),
        "remote_deleted": bool(delete_remote),
        "source_repository": repository,
        "git_history_backup": backup,
        "terminal_state": "complete",
    }
    _record_retirement(target_root, result)
    return result

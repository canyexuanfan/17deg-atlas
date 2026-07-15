from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .core import KBError
from .bootstrap import is_knowledge_module_root, is_personal_domain_root, resolve_knowledge_root


REGISTRY_RELATIVE = Path(".17deg-atlas") / "state" / "instances.json"


def atlas_workspace() -> Path | None:
    configured = os.environ.get("ATLAS_WORKSPACE", "").strip()
    return Path(configured).expanduser().resolve() if configured else None


def registry_path(workspace: str | Path | None = None) -> Path | None:
    selected = Path(workspace).expanduser().resolve() if workspace else atlas_workspace()
    return selected / REGISTRY_RELATIVE if selected else None


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "1.0", "instances": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KBError("17deg Atlas instance registry is invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("instances"), list):
        raise KBError("17deg Atlas instance registry is invalid")
    return value


def registered_instances(
    workspace: str | Path | None = None,
    *,
    domain_kind: str = "personal",
    module_kind: str = "knowledge",
) -> list[dict[str, Any]]:
    path = registry_path(workspace)
    if path is None:
        return []
    values: list[dict[str, Any]] = []
    for item in _read(path)["instances"]:
        if not isinstance(item, dict):
            continue
        if item.get("domain_kind") != domain_kind or item.get("module_kind") != module_kind:
            continue
        root_value = str(item.get("root", "")).strip()
        if not root_value:
            continue
        root = Path(root_value).expanduser().resolve()
        if not (is_personal_domain_root(root) or is_knowledge_module_root(root)):
            continue
        retained = dict(item)
        retained["root"] = str(root)
        values.append(retained)
    return values


def register_instance(
    root: str | Path,
    manifest: Mapping[str, Any],
    *,
    workspace: str | Path | None = None,
) -> bool:
    path = registry_path(workspace)
    if path is None:
        return False
    instance_id = str(manifest.get("instance_id", "")).strip()
    if not instance_id:
        raise KBError("knowledge instance cannot be registered without an instance id")
    root_path = Path(root).expanduser().resolve()
    repository = manifest.get("repository")
    modules = manifest.get("modules")
    module_kind = str(manifest.get("module_kind", ""))
    if not module_kind and isinstance(modules, list):
        if any(
            isinstance(item, Mapping) and item.get("module_kind") == "knowledge"
            for item in modules
        ):
            module_kind = "knowledge"
    record = {
        "instance_id": instance_id,
        "domain_kind": str(manifest.get("domain_kind", "")),
        "subject_kind": str(manifest.get("subject_kind", "")),
        "subject_id": str(manifest.get("subject_id", "")),
        "module_kind": module_kind,
        "root": str(root_path),
        "knowledge_root": str(resolve_knowledge_root(root_path)),
        "repository": dict(repository) if isinstance(repository, Mapping) else {},
    }
    value = _read(path)
    instances = [
        item
        for item in value["instances"]
        if not isinstance(item, dict) or item.get("instance_id") != instance_id
    ]
    instances.append(record)
    instances.sort(key=lambda item: str(item.get("instance_id", "")))
    value = {"schema_version": "1.0", "instances": instances}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return True

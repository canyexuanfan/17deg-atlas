from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .core import KBError, KnowledgeVault


PERSONAL_DOMAIN_MANIFEST = Path("personal.yaml")
LEGACY_PERSONAL_DOMAIN_MANIFEST = Path("config") / "instance.json"
KNOWLEDGE_MODULE_RELATIVE = Path("knowledge")
PERSONAL_DOMAIN_RUNTIME_RELATIVE = Path(".atlas") / "runtime"
PERSONAL_KNOWLEDGE_DIRECTORIES = (
    Path("knowledge") / "inbox",
    Path("knowledge") / "raw",
    Path("knowledge") / "library",
    Path("knowledge") / "wiki",
)
LEGACY_KNOWLEDGE_MODULE_RELATIVES = (
    Path("domains") / "personal" / "knowledge",
)
MODULE_MARKERS = (
    Path("config") / "tiers.yml",
    Path("config") / "policies.yml",
    Path("manifests") / "projection-selection.json",
)


def product_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KBError("instance manifest is invalid") from exc
    if not isinstance(loaded, dict):
        raise KBError("instance manifest is invalid")
    return loaded


def is_knowledge_module_root(root: str | Path) -> bool:
    candidate = Path(root).expanduser().resolve()
    return all((candidate / marker).is_file() for marker in MODULE_MARKERS)


def _personal_domain_manifest(candidate: Path) -> tuple[Path, dict[str, Any]] | None:
    for relative in (PERSONAL_DOMAIN_MANIFEST, LEGACY_PERSONAL_DOMAIN_MANIFEST):
        path = candidate / relative
        if not path.is_file():
            continue
        manifest = _read_manifest(path)
        if (
            manifest.get("domain_kind") == "personal"
            and manifest.get("subject_kind") == "person"
            and manifest.get("layout_kind") == "domain-root"
        ):
            return path, manifest
    return None


def _module_candidates(candidate: Path, manifest: dict[str, Any] | None = None) -> list[Path]:
    relatives: list[Path] = [KNOWLEDGE_MODULE_RELATIVE, *LEGACY_KNOWLEDGE_MODULE_RELATIVES]
    modules = manifest.get("modules") if isinstance(manifest, dict) else None
    if isinstance(modules, list):
        for item in modules:
            if not isinstance(item, dict) or item.get("module_kind") != "knowledge":
                continue
            runtime = str(item.get("runtime_path", "")).strip()
            if runtime:
                relatives.insert(0, Path(runtime))
            configured = str(item.get("path", "")).strip()
            if configured:
                relatives.insert(0, Path(configured))
    found: list[Path] = []
    seen: set[Path] = set()
    for relative in relatives:
        resolved = (candidate / relative).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if is_knowledge_module_root(resolved):
            found.append(resolved)
    return found


def is_personal_domain_root(root: str | Path) -> bool:
    candidate = Path(root).expanduser().resolve()
    located = _personal_domain_manifest(candidate)
    if located is None:
        return False
    _manifest_path, manifest = located
    modules = manifest.get("modules")
    if not isinstance(modules, list):
        return False
    has_record = any(
        isinstance(item, dict)
        and item.get("module_kind") == "knowledge"
        for item in modules
    )
    return has_record and len(_module_candidates(candidate, manifest)) == 1


def resolve_knowledge_root(root: str | Path) -> Path:
    """Resolve either a personal-domain root or a legacy module-root instance."""
    candidate = Path(root).expanduser().resolve()
    if is_knowledge_module_root(candidate):
        return candidate
    located = _personal_domain_manifest(candidate)
    manifest = located[1] if located else None
    nested = _module_candidates(candidate, manifest)
    if len(nested) > 1:
        raise KBError("multiple knowledge module roots exist; resolve the layout conflict before writing")
    if len(nested) == 1:
        return nested[0]
    raise KBError("selected path is not a personal domain or knowledge instance")


def ensure_instance_manifest(
    root: str | Path, *, subject_id: str = ""
) -> dict[str, Any]:
    """Create or complete the domain-aware manifest for a knowledge module instance."""
    instance_root = Path(root).resolve()
    path = instance_root / "config" / "instance.json"
    value: dict[str, Any]
    if path.is_file():
        value = _read_manifest(path)
    else:
        value = {
            "schema_version": "1.0",
            "instance_id": f"personal-knowledge-{uuid.uuid4().hex}",
            "domain_kind": "personal",
            "subject_kind": "person",
            "subject_id": "",
            "module_kind": "knowledge",
            "layout_kind": "module-root",
            "repository": {},
        }
    expected = {
        "domain_kind": "personal",
        "subject_kind": "person",
        "module_kind": "knowledge",
        "layout_kind": "module-root",
    }
    for key, expected_value in expected.items():
        current = str(value.get(key, "")).strip()
        if current and current != expected_value:
            raise KBError(f"instance manifest {key} is incompatible")
        value[key] = expected_value
    value.setdefault("schema_version", "1.0")
    value.setdefault("instance_id", f"personal-knowledge-{uuid.uuid4().hex}")
    value.setdefault("subject_id", "")
    value.setdefault("repository", {})
    if subject_id:
        current_subject = str(value.get("subject_id", "")).strip()
        if current_subject and current_subject != subject_id:
            raise KBError("instance manifest belongs to another subject")
        value["subject_id"] = subject_id
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return value


def ensure_personal_domain_manifest(
    root: str | Path, *, subject_id: str = ""
) -> dict[str, Any]:
    """Create or complete the repository-root manifest for a personal-domain instance."""
    domain_root = Path(root).expanduser().resolve()
    located = _personal_domain_manifest(domain_root)
    existing_candidates = _module_candidates(domain_root, located[1] if located else None)
    if len(existing_candidates) > 1:
        raise KBError("multiple knowledge module roots exist; refusing to update the manifest")
    runtime_root = (
        existing_candidates[0]
        if existing_candidates
        else domain_root / PERSONAL_DOMAIN_RUNTIME_RELATIVE
    )
    module = ensure_instance_manifest(runtime_root, subject_id=subject_id)
    path = located[0] if located else domain_root / PERSONAL_DOMAIN_MANIFEST
    value = located[1] if located else {
        "schema_version": "1.0",
        "instance_id": f"personal-{uuid.uuid4().hex}",
        "domain_kind": "personal",
        "subject_kind": "person",
        "subject_id": "",
        "layout_kind": "domain-root",
        "repository": {},
        "modules": [],
    }
    expected = {
        "domain_kind": "personal",
        "subject_kind": "person",
        "layout_kind": "domain-root",
    }
    for key, expected_value in expected.items():
        current = str(value.get(key, "")).strip()
        if current and current != expected_value:
            raise KBError(f"personal domain manifest {key} is incompatible")
        value[key] = expected_value
    value.setdefault("schema_version", "1.0")
    value.setdefault("instance_id", f"personal-{uuid.uuid4().hex}")
    value.setdefault("subject_id", "")
    value.setdefault("repository", {})
    if subject_id:
        current_subject = str(value.get("subject_id", "")).strip()
        if current_subject and current_subject != subject_id:
            raise KBError("personal domain instance belongs to another subject")
        value["subject_id"] = subject_id
    module_record = {
        "module_kind": "knowledge",
        "path": KNOWLEDGE_MODULE_RELATIVE.as_posix(),
        "runtime_path": runtime_root.relative_to(domain_root).as_posix(),
        "module_instance_id": module["instance_id"],
    }
    modules = value.get("modules")
    if not isinstance(modules, list):
        raise KBError("personal domain manifest modules are invalid")
    other_modules = [
        item
        for item in modules
        if isinstance(item, dict) and item.get("module_kind") != "knowledge"
    ]
    value["modules"] = [module_record, *other_modules]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return value


def ensure_personal_knowledge_workspace(root: str | Path) -> list[str]:
    domain_root = Path(root).expanduser().resolve()
    created: list[str] = []
    for relative in PERSONAL_KNOWLEDGE_DIRECTORIES:
        path = domain_root / relative
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(relative.as_posix())
    return created


def initialize_instance(target: str | Path) -> dict[str, Any]:
    """Create a new instance from the bundled, credential-free template."""
    root = Path(target).resolve()
    template = product_root() / "templates" / "instance"
    if not template.is_dir():
        raise KBError("bundled instance template is missing")
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    retained: list[str] = []
    for source in sorted(template.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(template)
        destination = root / relative
        data = source.read_bytes()
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != data:
                raise KBError(f"refusing to overwrite existing instance file: {relative.as_posix()}")
            retained.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        created.append(relative.as_posix())
    manifest_path = root / "config" / "instance.json"
    manifest_existed = manifest_path.is_file()
    ensure_instance_manifest(root)
    manifest_relative = "config/instance.json"
    if manifest_existed:
        retained.append(manifest_relative)
    else:
        created.append(manifest_relative)
    layout = KnowledgeVault(root).init_layout()
    return {
        "status": "ok",
        "root": str(root),
        "created_files": created,
        "retained_files": retained,
        "directories": layout["directories"],
        "git_initialized": False,
        "credentials_created": False,
    }


def initialize_personal_domain(target: str | Path) -> dict[str, Any]:
    """Create a personal-domain root containing only the current knowledge module."""
    root = Path(target).expanduser().resolve()
    template = product_root() / "templates" / "personal-domain"
    if not template.is_dir():
        raise KBError("bundled personal domain template is missing")
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    retained: list[str] = []
    for source in sorted(template.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(template)
        destination = root / relative
        data = source.read_bytes()
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != data:
                raise KBError(
                    f"refusing to overwrite existing personal domain file: {relative.as_posix()}"
                )
            retained.append(relative.as_posix())
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        created.append(relative.as_posix())
    workspace_root = root / KNOWLEDGE_MODULE_RELATIVE
    runtime_root = root / PERSONAL_DOMAIN_RUNTIME_RELATIVE
    module_result = initialize_instance(runtime_root)
    for relative in (
        Path("governance"),
        Path(".atlas") / "review",
        Path(".atlas") / "state",
        Path(".atlas") / "indexes",
        Path(".atlas") / "runs",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    ensure_personal_knowledge_workspace(root)
    manifest_existed = (root / PERSONAL_DOMAIN_MANIFEST).is_file()
    manifest = ensure_personal_domain_manifest(root)
    manifest_relative = PERSONAL_DOMAIN_MANIFEST.as_posix()
    (retained if manifest_existed else created).append(manifest_relative)
    return {
        "status": "ok",
        "root": str(root),
        "knowledge_root": str(runtime_root),
        "knowledge_workspace": str(workspace_root),
        "runtime_root": str(runtime_root),
        "created_files": sorted(set(created + [
            f"{KNOWLEDGE_MODULE_RELATIVE.as_posix()}/{item}"
            for item in module_result["created_files"]
        ])),
        "retained_files": sorted(set(retained + [
            f"{KNOWLEDGE_MODULE_RELATIVE.as_posix()}/{item}"
            for item in module_result["retained_files"]
        ])),
        "directories": module_result["directories"],
        "manifest": manifest,
        "git_initialized": False,
        "credentials_created": False,
    }

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


VIEW_MANIFEST = Path("knowledge") / ".atlas-view-manifest.json"
WIKI_DIRECTORIES = {
    "source_summary": "summaries",
    "atomic_card": "cards",
    "topic_page": "topics",
}
RAW_DIRECTORIES = {
    "article": "articles",
    "book": "books",
    "site": "sites",
    "screenshot": "screenshots",
    "transcript": "transcripts",
    "conversation": "conversation-extracts",
    "data": "data",
    "file": "files",
    "idea": "files",
}
PROTECTED_VIEW_FIELDS = (
    "atlas_id",
    "stage",
    "object_kind",
    "media_type",
    "wiki_kind",
    "access",
    "lifecycle",
    "authorship",
    "rights",
    "source_refs",
    "interaction_refs",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(title: str, object_id: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", title).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)[:72].strip(" .-")
    return f"{cleaned or 'knowledge'}--{object_id}.md"


def _stage(envelope: Mapping[str, Any]) -> str | None:
    kind = str(envelope.get("object_kind", ""))
    if kind == "raw":
        return "raw"
    if kind != "wiki":
        return None
    if (
        envelope.get("review_state") in ("reviewed", "verified")
        and envelope.get("maturity") in ("reviewed", "verified")
    ):
        return "library"
    return "wiki"


def _access(envelope: Mapping[str, Any]) -> str:
    classification = envelope.get("classification")
    if isinstance(classification, Mapping):
        level = str(classification.get("level", ""))
        if level in ("public", "basic", "advanced", "core"):
            return level
    tier = str(envelope.get("tier", ""))
    if tier == "archive":
        return "public"
    return tier if tier in ("public", "basic", "advanced", "core") else "basic"


def _source_values(envelope: Mapping[str, Any], field: str) -> list[str]:
    values = envelope.get(field) or []
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(str(value) for value in values if value))


def _view_metadata(envelope: Mapping[str, Any]) -> dict[str, Any]:
    stage = _stage(envelope)
    if stage is None:
        raise ValueError("unsupported workspace view object kind")
    sources = _source_values(envelope, "source_refs")
    sources.extend(
        value
        for value in _source_values(envelope, "source_ids")
        if value not in sources
    )
    lifecycle = str(envelope.get("lifecycle", "active"))
    if envelope.get("tier") == "archive":
        lifecycle = "archived"
    return {
        "atlas_id": str(envelope["object_id"]),
        "stage": stage,
        "object_kind": str(envelope.get("object_kind", "")),
        "media_type": envelope.get("media_type"),
        "wiki_kind": envelope.get("wiki_kind"),
        "access": _access(envelope),
        "lifecycle": lifecycle,
        "maturity": str(envelope.get("maturity", "seed")),
        "review_state": str(envelope.get("review_state", "candidate")),
        "authorship": str(envelope.get("authorship_status", "unknown")),
        "rights": str(envelope.get("rights", "unknown")),
        "source_refs": sources,
        "interaction_refs": _source_values(envelope, "interaction_refs"),
    }


def _relative_view(envelope: Mapping[str, Any]) -> Path | None:
    stage = _stage(envelope)
    if stage is None:
        return None
    filename = _safe_name(str(envelope.get("title", "")), str(envelope["object_id"]))
    if stage == "raw":
        directory = RAW_DIRECTORIES.get(str(envelope.get("media_type") or "file"), "files")
        return Path("knowledge") / "raw" / directory / filename
    wiki_kind = str(envelope.get("wiki_kind", ""))
    directory = WIKI_DIRECTORIES.get(wiki_kind, "other")
    if wiki_kind == "topic_page":
        topic_ids = _source_values(envelope, "topic_ids")
        if topic_ids:
            # A derived topic page is one rebuildable human view per stable topic,
            # even though immutable candidate versions remain in the object store.
            filename = _safe_name(str(envelope.get("title", "")), topic_ids[0])
    return Path("knowledge") / stage / directory / filename


def _render(envelope: Mapping[str, Any]) -> str:
    metadata = _view_metadata(envelope)
    lines = ["---"]
    for key, value in metadata.items():
        if value is None:
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(
        [
            "---",
            "",
            f"# {envelope.get('title', 'Untitled')}",
            "",
            str(envelope.get("content", "")),
            "",
        ]
    )
    return "\n".join(lines)


def _read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return metadata
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if not key:
            continue
        try:
            metadata[key] = json.loads(raw)
        except json.JSONDecodeError:
            metadata[key] = raw
    return {}


def _property_conflicts(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    actual = _read_frontmatter(path)
    if not actual:
        return {"frontmatter": {"expected": "present", "actual": "missing-or-invalid"}}
    conflicts: dict[str, Any] = {}
    for field in PROTECTED_VIEW_FIELDS:
        if actual.get(field) != expected.get(field):
            conflicts[field] = {
                "expected": expected.get(field),
                "actual": actual.get(field),
            }
    return conflicts


def _read_manifest(domain_root: Path) -> dict[str, Any]:
    path = domain_root / VIEW_MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "files": {}}
    files = value.get("files") if isinstance(value, dict) else None
    return {
        "schema_version": 1,
        "files": dict(files) if isinstance(files, dict) else {},
    }


def materialize_workspace_views(
    domain_root: str | Path,
    envelopes: Iterable[Mapping[str, Any]],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    root = Path(domain_root).expanduser().resolve()
    manifest = _read_manifest(root)
    previous = dict(manifest["files"])
    current = {} if replace else dict(previous)
    written: list[str] = []
    preserved_modified: list[str] = []
    property_conflicts: dict[str, Any] = {}
    unmanaged_views: list[str] = []
    for envelope in envelopes:
        relative = _relative_view(envelope)
        if relative is None:
            continue
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        relative_name = relative.as_posix()
        previous_record = previous.get(relative_name)
        if destination.is_file():
            if not isinstance(previous_record, Mapping):
                preserved_modified.append(relative_name)
                unmanaged_views.append(relative_name)
                continue
            if _sha256(destination) != str(previous_record.get("sha256", "")):
                current[relative_name] = dict(previous_record)
                preserved_modified.append(relative_name)
                expected = previous_record.get("metadata")
                if isinstance(expected, Mapping):
                    conflicts = _property_conflicts(destination, expected)
                    if conflicts:
                        property_conflicts[relative_name] = conflicts
                continue
        content = _render(envelope)
        destination.write_text(content, encoding="utf-8", newline="\n")
        digest = _sha256(destination)
        current[relative_name] = {
            "sha256": digest,
            "object_id": str(envelope["object_id"]),
            "metadata": _view_metadata(envelope),
        }
        written.append(relative_name)
    removed = 0
    if replace:
        for relative, record in previous.items():
            if relative in current:
                continue
            path = root / relative
            if not path.is_file():
                continue
            if _sha256(path) == str(record.get("sha256", "")):
                path.unlink()
                removed += 1
                continue
            current[relative] = dict(record)
            preserved_modified.append(relative)
            expected = record.get("metadata")
            if isinstance(expected, Mapping):
                conflicts = _property_conflicts(path, expected)
                if conflicts:
                    property_conflicts[relative] = conflicts
    manifest_path = root / VIEW_MANIFEST
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {"schema_version": 1, "files": current},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "status": "needs-review" if property_conflicts or unmanaged_views else "ok",
        "written_files": sorted(written),
        "tracked_views": len(current),
        "removed_stale_files": removed,
        "preserved_modified_views": sorted(preserved_modified),
        "property_conflicts": property_conflicts,
        "unmanaged_views": sorted(unmanaged_views),
    }


def audit_workspace_views(domain_root: str | Path) -> dict[str, Any]:
    root = Path(domain_root).expanduser().resolve()
    manifest = _read_manifest(root)
    missing: list[str] = []
    modified: list[str] = []
    property_conflicts: dict[str, Any] = {}
    for relative, record in manifest["files"].items():
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        if _sha256(path) != str(record.get("sha256", "")):
            modified.append(relative)
        expected = record.get("metadata")
        if isinstance(expected, Mapping):
            conflicts = _property_conflicts(path, expected)
            if conflicts:
                property_conflicts[relative] = conflicts
    return {
        "status": "needs-review" if missing or property_conflicts else "ok",
        "tracked_views": len(manifest["files"]),
        "missing_views": sorted(missing),
        "modified_views": sorted(modified),
        "property_conflicts": property_conflicts,
    }


def clear_workspace_views(domain_root: str | Path) -> dict[str, Any]:
    root = Path(domain_root).expanduser().resolve()
    manifest_path = root / VIEW_MANIFEST
    manifest = _read_manifest(root)
    removed = 0
    preserved_modified: list[str] = []
    for relative, record in manifest["files"].items():
        path = root / relative
        if not path.is_file():
            continue
        if _sha256(path) != str(record.get("sha256", "")):
            preserved_modified.append(relative)
            continue
        path.unlink()
        removed += 1
    if preserved_modified:
        remaining = {
            relative: manifest["files"][relative] for relative in preserved_modified
        }
        manifest_path.write_text(
            json.dumps(
                {"schema_version": 1, "files": remaining},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    else:
        manifest_path.unlink(missing_ok=True)
    for relative in ("knowledge/raw", "knowledge/library", "knowledge/wiki"):
        base = root / relative
        if not base.is_dir():
            continue
        for directory in sorted(
            (item for item in base.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "status": "ok",
        "removed_files": removed,
        "preserved_modified_views": sorted(preserved_modified),
    }


def build_workspace_views(
    vault: Any,
    domain_root: str | Path,
    *,
    identities: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    documents = [
        item for item in vault._public_documents() if not item.get("locked")
    ]
    for tier in ("basic", "advanced", "core"):
        identity = (identities or {}).get(tier)
        if not identity:
            continue
        for path in vault.local.glob(f"vault/{tier}/*/*.age"):
            documents.append(vault._read_object_path(path, {tier: identity}))
    return materialize_workspace_views(domain_root, documents, replace=True)

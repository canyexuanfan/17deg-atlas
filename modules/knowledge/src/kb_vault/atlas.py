from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from .adapters.local import LocalAdapter
from .core import KBError, KnowledgeVault, PRIVATE_TIERS, SECRET_PATTERNS, utc_now


FORBIDDEN_SUFFIXES = {".identity", ".key", ".pem"}
SELECTION_MODES = {
    "local-only",
    "projection-plaintext",
    "projection-ciphertext",
    "atlas-plaintext",
    "atlas-ciphertext",
    "service-only",
}
REMOTE_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
REMOTE_OBJECT_RE = re.compile(r"^obj_[a-z0-9_-]+$")


def file_sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ContentProjection:
    """Build and verify an explicitly configured content repository."""

    def __init__(self, vault: KnowledgeVault):
        self.vault = vault
        self.root = vault.root
        config_path = self.root / "config" / "projection.yml"
        if not config_path.is_file():
            config_path = self.root / "config" / "atlas.yml"
        try:
            self.config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KBError("invalid or missing configuration: config/projection.yml") from exc
        repo_name = str(self.config.get("repo_name", "")).strip()
        producer_name = str(self.config.get("producer_name", "")).strip()
        namespace = Path(str(self.config.get("knowledge_namespace", "knowledge")))
        if not repo_name or not re.fullmatch(r"[A-Za-z0-9._-]+", repo_name):
            raise KBError("projection repo_name is missing or invalid")
        if not producer_name or not re.fullmatch(r"[A-Za-z0-9._-]+", producer_name):
            raise KBError("projection producer_name is missing or invalid")
        if namespace.is_absolute() or ".." in namespace.parts or ".git" in namespace.parts:
            raise KBError("projection knowledge_namespace must be repository-relative")
        self.producer_name = str(self.config["producer_name"])
        self.namespace = namespace
        self.remote_inbox = self.namespace / "inbox" / "remote"
        self.selection_path = self.root / str(
            self.config.get("selection_file", "manifests/projection-selection.json")
        )

    @property
    def repo_name(self) -> str:
        return str(self.config["repo_name"])

    def assert_remote_target(self, *, owner: str, repo: str, branch: str) -> None:
        policy = self.config.get("remote_policy")
        if not isinstance(policy, dict):
            raise KBError("projection remote policy is missing")
        allowed = (
            str(policy.get("allowed_owner", "")),
            str(policy.get("allowed_repo", "")),
            str(policy.get("allowed_branch", "")),
        )
        requested = (owner, repo, branch)
        if requested != allowed:
            raise KBError(
                "GitHub target is outside the configured projection allowlist: "
                f"expected {allowed[0]}/{allowed[1]} on {allowed[2]}"
            )

    def _target(self, output: str | Path) -> Path:
        target = Path(output).resolve()
        if target.name != self.repo_name:
            raise KBError(f"projection output directory must be named {self.repo_name}")
        if target == self.root or self.root in target.parents:
            raise KBError("projection output must be outside the instance repository")
        return target

    def _template(self, name: str) -> bytes:
        template_dir = str(self.config.get("template_dir", "templates/projection"))
        path = self.root / template_dir / name
        if not path.is_file() and template_dir != "templates/atlas":
            path = self.root / "templates" / "atlas" / name
        try:
            return path.read_bytes()
        except OSError as exc:
            raise KBError(f"missing projection template: {name}") from exc

    @staticmethod
    def _jsonl(records: list[Mapping[str, Any]]) -> bytes:
        text = "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records
        )
        return (text + ("\n" if text else "")).encode("utf-8")

    @staticmethod
    def _index_md(records: list[Mapping[str, Any]]) -> bytes:
        lines = [
            "# 个人域知识公开目录",
            "",
            "内容会按主题持续整理，已开放条目出现时从此处进入。",
            "",
        ]
        for item in records:
            locked = "（需相应授权）" if item.get("locked") else ""
            summary = f" — {item['summary']}" if item.get("summary") else ""
            lines.append(
                f"- [{item['title']}]({item['path']}) {locked}`[{item['tier']}]`{summary}"
            )
        return ("\n".join(lines).rstrip() + "\n").encode("utf-8")

    def _empty_selection(self) -> dict[str, Any]:
        return {"selection_version": 1, "entries": []}

    def _load_selection(self) -> list[dict[str, Any]]:
        if not self.selection_path.is_file():
            raise KBError("projection selection file is missing; select objects before building")
        try:
            document = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KBError("projection selection file is invalid") from exc
        if document.get("selection_version") != 1 or not isinstance(document.get("entries"), list):
            raise KBError("unsupported projection selection format")
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in document["entries"]:
            if not isinstance(raw, dict):
                raise KBError("projection selection entry must be an object")
            object_id = str(raw.get("object_id", ""))
            if not object_id or object_id in seen:
                raise KBError("projection selection contains an empty or duplicate object id")
            distribution = str(raw.get("distribution", ""))
            if distribution not in SELECTION_MODES:
                raise KBError("projection selection contains an invalid distribution mode")
            if raw.get("target") != self.namespace.as_posix():
                raise KBError("projection selection target is outside the knowledge namespace")
            if raw.get("human_confirmed") is not True:
                raise KBError("projection selection requires explicit human confirmation")
            if not str(raw.get("license_id", "")).strip():
                raise KBError("projection selection requires a license id")
            self.vault._locate_object(object_id)
            seen.add(object_id)
            entries.append(dict(raw))
        entries.sort(key=lambda item: item["object_id"])
        return entries

    def select(
        self,
        object_id: str,
        distribution: str,
        license_id: str,
        *,
        confirm: bool,
        confirmed_at: str | None = None,
    ) -> dict[str, Any]:
        if not confirm:
            raise KBError("projection selection requires --confirm")
        if distribution not in SELECTION_MODES:
            raise KBError("invalid projection distribution mode")
        source = self.vault._locate_object(object_id)
        tier = source.relative_to(self.root).parts[1]
        if distribution in ("projection-plaintext", "atlas-plaintext") and tier not in ("public", "archive"):
            raise KBError("only public/archive objects may use plaintext projection")
        if distribution in ("projection-ciphertext", "atlas-ciphertext") and tier not in PRIVATE_TIERS:
            raise KBError("only basic/advanced/core objects may use ciphertext projection")
        if not license_id.strip():
            raise KBError("license_id is required")
        document = self._empty_selection()
        if self.selection_path.is_file():
            try:
                document = json.loads(self.selection_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise KBError("projection selection file is invalid") from exc
        entries = [
            item for item in document.get("entries", []) if item.get("object_id") != object_id
        ]
        entries.append(
            {
                "object_id": object_id,
                "distribution": distribution,
                "target": self.namespace.as_posix(),
                "license_id": license_id,
                "human_confirmed": True,
                "confirmed_at": confirmed_at or utc_now(),
            }
        )
        entries.sort(key=lambda item: item["object_id"])
        payload = {"selection_version": 1, "entries": entries}
        LocalAdapter(self.root).atomic_write_text(
            self.selection_path.relative_to(self.root),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"status": "ok", "object_id": object_id, "distribution": distribution}

    def unselect(self, object_id: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise KBError("removing a projection selection requires --confirm")
        entries = self._load_selection()
        remaining = [item for item in entries if item["object_id"] != object_id]
        if len(remaining) == len(entries):
            raise KBError("object is not selected for projection distribution")
        payload = {"selection_version": 1, "entries": remaining}
        LocalAdapter(self.root).atomic_write_text(
            self.selection_path.relative_to(self.root),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"status": "ok", "object_id": object_id, "distribution": "local-only"}

    def _teaser(self, record: Mapping[str, Any]) -> tuple[str, bytes, dict[str, Any]]:
        relative = Path("catalog") / "teasers" / f"{record['object_id']}.md"
        root_path = (self.namespace / relative).as_posix()
        body = (
            "---\n"
            "schema_version: 1\n"
            f"object_id: {json.dumps(record['object_id'], ensure_ascii=False)}\n"
            f"access_tier: {json.dumps(record['tier'], ensure_ascii=False)}\n"
            'projection_type: "authorized-content-teaser"\n'
            "human_confirmed: true\n"
            "---\n"
            f"# {record['title']}\n\n"
            f"{record.get('summary', '')}\n\n"
            "此页为专题简介，完整内容按对应访问规则开放。\n"
        ).encode("utf-8")
        public_record = {
            "schema_version": 1,
            "object_id": record["object_id"],
            "object_kind": record["object_kind"],
            "tier": record["tier"],
            "title": record["title"],
            "summary": record.get("summary", ""),
            "path": root_path,
            "content_hash": file_sha256(body),
            "updated_at": record["updated_at"],
            "locked": True,
        }
        return root_path, body, public_record

    def _skeleton_files(self) -> dict[str, bytes]:
        mapping = {
            ".gitattributes": ".gitattributes",
            ".gitignore": ".gitignore",
            "README.md": "README.md",
            "governance/LICENSES.md": "governance/LICENSES.md",
            "governance/ACCESS.md": "governance/ACCESS.md",
            "governance/SECURITY.md": "governance/SECURITY.md",
            "governance/REVOCATION.md": "governance/REVOCATION.md",
        }
        files = {target: self._template(source) for target, source in mapping.items()}
        reader_path = self.root / "tools" / "projection_reader.py"
        if not reader_path.is_file():
            reader_path = self.root / "tools" / "atlas_reader.py"
        files["tools/projection_reader.py"] = reader_path.read_bytes()
        return files

    def expected_files(self) -> dict[str, bytes]:
        files = self._skeleton_files()
        selection = [
            entry for entry in self._load_selection() if entry["distribution"] not in ("local-only", "service-only")
        ]
        selected = {entry["object_id"]: entry for entry in selection}
        public_records_by_id = {
            record["object_id"]: record for record in self.vault.public_catalog_records()
        }
        catalog_records: list[dict[str, Any]] = []
        knowledge_members: list[dict[str, Any]] = []

        for object_id, entry in sorted(selected.items()):
            source = self.vault._locate_object(object_id)
            source_parts = source.relative_to(self.root).parts
            tier, kind = source_parts[1], source_parts[2]
            distribution = entry["distribution"]
            if tier in ("public", "archive"):
                if distribution not in ("projection-plaintext", "atlas-plaintext"):
                    raise KBError("public/archive selection must use plaintext projection")
                envelope = self.vault._read_object_path(source)
                if envelope.get("human_confirmed") is not True:
                    raise KBError("public/archive projected content must be human confirmed")
                relative = self.namespace / tier / kind / source.name
                data = source.read_bytes()
            else:
                if tier not in PRIVATE_TIERS or distribution not in ("projection-ciphertext", "atlas-ciphertext"):
                    raise KBError("private selection must use ciphertext projection")
                data = source.read_bytes()
                if source.suffix not in (".age", ".enc") or not data.startswith(b"age-encryption.org/v1"):
                    raise KBError("private projected object is not valid age ciphertext")
                relative = self.namespace / "encrypted" / tier / source.name
            root_path = relative.as_posix()
            files[root_path] = data
            knowledge_members.append(
                {
                    "path": relative.relative_to(self.namespace).as_posix(),
                    "object_id": object_id,
                    "sha256": file_sha256(data),
                    "size": len(data),
                    "distribution": "public-plaintext" if tier in ("public", "archive") else "public-ciphertext",
                    "access_tier": tier,
                    "license_id": entry["license_id"],
                }
            )

            record = public_records_by_id.get(object_id)
            if not record:
                continue
            if record.get("locked"):
                teaser_path, teaser, safe_record = self._teaser(record)
                files[teaser_path] = teaser
                catalog_records.append(safe_record)
            else:
                safe_record = dict(record)
                safe_record["path"] = root_path
                catalog_records.append(safe_record)

        catalog_records.sort(key=lambda item: (item["tier"], item["object_id"], item["path"]))
        catalog_jsonl = self._jsonl(catalog_records)
        catalog_root = self.namespace / "catalog"
        files[(catalog_root / "index.md").as_posix()] = self._index_md(catalog_records)
        files[(catalog_root / "index.jsonl").as_posix()] = catalog_jsonl

        issued_at = max((str(item.get("confirmed_at", "")) for item in selection), default="")
        source_digest = file_sha256(
            self._jsonl(
                [
                    {"object_id": item["object_id"], "sha256": member["sha256"]}
                    for item, member in zip(selection, knowledge_members)
                ]
            )
        )
        knowledge_manifest = {
            "schema_version": 1,
            "producer": self.producer_name,
            "namespace": self.namespace.as_posix(),
            "source_revision": source_digest,
            "release_id": "knowledge-" + source_digest.split(":", 1)[1][:16],
            "issued_at": issued_at,
            "files": sorted(knowledge_members, key=lambda item: item["path"]),
        }
        knowledge_manifest_path = (self.namespace / "knowledge-manifest.json").as_posix()
        files[knowledge_manifest_path] = (
            json.dumps(knowledge_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        manifest_files = [
            {"path": path, "sha256": file_sha256(data), "size": len(data)}
            for path, data in sorted(files.items())
        ]
        manifest = {
            "schema_version": 1,
            "repo_name": self.repo_name,
            "projection_mode": str(self.config.get("projection_mode", "content-repository")),
            "producers": [
                {
                    "name": self.producer_name,
                    "namespace": self.namespace.as_posix(),
                    "manifest": knowledge_manifest_path,
                }
            ],
            "files": manifest_files,
        }
        manifest_file = str(self.config.get("manifest_file", "projection-manifest.json"))
        files[manifest_file] = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        return files

    def _assert_managed_target(self, target: Path) -> None:
        if not target.exists():
            return
        visible = [item for item in target.iterdir() if item.name != ".git"]
        if not visible:
            return
        marker = target / str(self.config.get("manifest_file", "projection-manifest.json"))
        if not marker.is_file():
            raise KBError("refusing to replace a non-empty directory without a projection manifest")
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KBError("existing projection manifest is invalid") from exc
        if value.get("repo_name") != self.repo_name:
            raise KBError("existing directory is not the configured projection")

    def build(self, output: str | Path) -> dict[str, Any]:
        instance_verification = self.vault.verify(leak_scan=True)
        if not instance_verification["ok"]:
            raise KBError("instance verification failed before projection build")
        target = self._target(output)
        self._assert_managed_target(target)
        target.mkdir(parents=True, exist_ok=True)
        preserved_remote: dict[str, bytes] = {}
        remote_root = target / self.remote_inbox
        if remote_root.exists():
            for path in sorted(remote_root.rglob("*")):
                if path.is_symlink():
                    raise KBError("refusing to preserve a symbolic link in the remote inbox")
                if path.is_file():
                    preserved_remote[path.relative_to(target).as_posix()] = path.read_bytes()
            remote_issues = self._validate_remote_inbox(preserved_remote)
            if remote_issues:
                raise KBError("remote inbox validation failed before projection build: " + "; ".join(remote_issues))
        for child in list(target.iterdir()):
            if child.name == ".git":
                continue
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
        adapter = LocalAdapter(target)
        expected = self.expected_files()
        for relative, data in expected.items():
            adapter.atomic_write_bytes(relative, data)
        for relative, data in preserved_remote.items():
            adapter.atomic_write_bytes(relative, data)
        result = self.verify(target)
        if not result["ok"]:
            raise KBError("projection verification failed after build")
        manifest_file = str(self.config.get("manifest_file", "projection-manifest.json"))
        manifest = json.loads(expected[manifest_file].decode("utf-8"))
        knowledge = json.loads(
            expected[(self.namespace / "knowledge-manifest.json").as_posix()].decode("utf-8")
        )
        return {
            "status": "ok",
            "repository": self.repo_name,
            "output": str(target),
            "projection_mode": manifest["projection_mode"],
            "namespace": self.namespace.as_posix(),
            "files": len(expected),
            "selected_objects": len(knowledge["files"]),
            "ciphertext_objects": len(
                [item for item in knowledge["files"] if item["distribution"] == "public-ciphertext"]
            ),
        }

    def _validate_remote_inbox(self, files: Mapping[str, bytes]) -> list[str]:
        issues: list[str] = []
        prefix = self.remote_inbox.parts
        groups: dict[tuple[str, str], dict[str, tuple[str, bytes]]] = {}
        for relative, data in files.items():
            parts = Path(relative).parts
            if parts[: len(prefix)] != prefix or len(parts) != len(prefix) + 3:
                issues.append(f"invalid remote inbox path: {relative}")
                continue
            agent_id, object_id, name = parts[-3:]
            if not REMOTE_AGENT_RE.fullmatch(agent_id) or not REMOTE_OBJECT_RE.fullmatch(object_id):
                issues.append(f"invalid remote inbox identity: {relative}")
                continue
            if name not in ("payload.json", "payload.json.age", "READY.json"):
                issues.append(f"invalid remote inbox file: {relative}")
                continue
            groups.setdefault((agent_id, object_id), {})[name] = (relative, data)
        for (agent_id, object_id), members in sorted(groups.items()):
            ready_item = members.get("READY.json")
            payload_names = [name for name in ("payload.json", "payload.json.age") if name in members]
            if ready_item is None or len(payload_names) != 1 or len(members) != 2:
                issues.append(f"incomplete remote inbox event: {agent_id}/{object_id}")
                continue
            payload_name = payload_names[0]
            payload_path, payload = members[payload_name]
            if payload_name.endswith(".age"):
                if not payload.startswith(b"age-encryption.org/v1"):
                    issues.append(f"invalid remote age payload: {payload_path}")
            else:
                try:
                    payload_text = payload.decode("utf-8")
                    json.loads(payload_text)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    issues.append(f"invalid remote plaintext payload: {payload_path}")
                else:
                    if any(pattern.search(payload_text) for pattern in SECRET_PATTERNS):
                        issues.append(f"credential-like content in remote inbox: {payload_path}")
            ready_path, ready_bytes = ready_item
            try:
                ready_text = ready_bytes.decode("utf-8")
                ready = json.loads(ready_text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                issues.append(f"invalid remote READY marker: {ready_path}")
                continue
            if any(pattern.search(ready_text) for pattern in SECRET_PATTERNS):
                issues.append(f"credential-like content in remote READY marker: {ready_path}")
            if ready.get("object_id") != object_id or ready.get("submitted_by") != agent_id:
                issues.append(f"remote READY identity mismatch: {ready_path}")
            if ready.get("schema_version") != 1 or ready.get("event_type") != "remote-knowledge-ready":
                issues.append(f"remote READY schema mismatch: {ready_path}")
            if ready.get("payload_path") != payload_path:
                issues.append(f"remote READY path mismatch: {ready_path}")
            if ready.get("payload_sha256") != file_sha256(payload):
                issues.append(f"remote READY hash mismatch: {ready_path}")
            if ready.get("payload_size") != len(payload):
                issues.append(f"remote READY size mismatch: {ready_path}")
            tier = ready.get("tier")
            distribution = ready.get("distribution")
            if payload_name.endswith(".age"):
                if tier not in PRIVATE_TIERS or distribution != "public-ciphertext":
                    issues.append(f"remote READY private tier mismatch: {ready_path}")
            elif tier not in ("public", "archive") or distribution != "public-plaintext":
                issues.append(f"remote READY public tier mismatch: {ready_path}")
        return issues

    def verify(self, output: str | Path) -> dict[str, Any]:
        target = self._target(output)
        issues: list[str] = []
        if not target.is_dir():
            return {"ok": False, "issues": ["content projection directory does not exist"], "files_checked": 0}
        expected = self.expected_files()
        actual: dict[str, Path] = {}
        for path in sorted(target.rglob("*")):
            relative_parts = path.relative_to(target).parts
            if ".git" in relative_parts or ".local" in relative_parts:
                continue
            relative = path.relative_to(target).as_posix()
            if path.is_symlink():
                issues.append(f"symbolic link is not allowed: {relative}")
            elif path.is_file():
                actual[relative] = path
        remote_actual = {
            relative: path.read_bytes()
            for relative, path in actual.items()
            if Path(relative).parts[: len(self.remote_inbox.parts)] == self.remote_inbox.parts
        }
        issues.extend(self._validate_remote_inbox(remote_actual))
        allowed_extra = set(remote_actual)
        for relative in sorted(set(actual).difference(expected).difference(allowed_extra)):
            issues.append(f"unexpected file: {relative}")
        for relative in sorted(set(expected).difference(actual)):
            issues.append(f"missing file: {relative}")
        cipher_prefix = (self.namespace / "encrypted").parts
        for relative in sorted(set(actual).intersection(expected)):
            data = actual[relative].read_bytes()
            if data != expected[relative]:
                issues.append(f"generated file differs from expected projection: {relative}")
            parts = Path(relative).parts
            suffix = Path(relative).suffix.casefold()
            if suffix in FORBIDDEN_SUFFIXES:
                issues.append(f"forbidden private file type in projection: {relative}")
            if suffix in (".age", ".enc"):
                if len(parts) < len(cipher_prefix) + 2 or parts[: len(cipher_prefix)] != cipher_prefix or parts[len(cipher_prefix)] not in PRIVATE_TIERS:
                    issues.append(f"ciphertext outside the knowledge encrypted namespace: {relative}")
                if not data.startswith(b"age-encryption.org/v1"):
                    issues.append(f"invalid age ciphertext: {relative}")
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(f"non-text non-ciphertext file: {relative}")
                continue
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                issues.append(f"credential-like content in projection: {relative}")
        return {"ok": not issues, "issues": sorted(set(issues)), "files_checked": len(actual)}

    def verified_file(self, output: str | Path, relative: str | Path) -> bytes:
        target = self._target(output)
        result = self.verify(target)
        if not result["ok"]:
            raise KBError("projection must verify before creating a GitHub request plan")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise KBError("projection file path must be repository-relative")
        key = relative_path.as_posix()
        expected = self.expected_files()
        if key not in expected:
            raise KBError("requested file is not part of the verified projection")
        return expected[key]


# Backward-compatible import for existing v0.1 instances.
PublicAtlas = ContentProjection

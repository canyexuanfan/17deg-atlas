from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .adapters.local import LocalAdapter
from .model import (
    materialize_orthogonal_fields,
    new_orthogonal_fields,
    validate_orthogonal_fields,
)
from .semantic import (
    OBJECT_KINDS,
    clarification_questions,
    materialize_semantic_fields,
    new_semantic_fields,
    validate_semantic_fields,
)


TIERS = ("public", "archive", "basic", "advanced", "core")
PRIVATE_TIERS = ("basic", "advanced", "core")
KINDS = OBJECT_KINDS
TIER_RANK = {"public": 0, "archive": 0, "basic": 1, "advanced": 2, "core": 3}
ID_RE = re.compile(r"^obj_[a-z0-9_-]+$")
SECRET_PATTERNS = (
    re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{40,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class KBError(RuntimeError):
    """Safe, user-facing vault operation error."""


class ConflictError(KBError):
    """A request id or object path conflicts with existing state."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_token(prefix: str, value: str, length: int = 24) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def render_markdown(envelope: Mapping[str, Any]) -> str:
    metadata = {key: value for key, value in envelope.items() if key != "content"}
    lines = ["---"]
    for key in sorted(metadata):
        lines.append(f"{key}: {json.dumps(metadata[key], ensure_ascii=False)}")
    lines.extend(["---", envelope["content"]])
    return "\n".join(lines)


def parse_markdown(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise KBError("public object is missing frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise KBError("public object frontmatter is not closed")
    header = text[4:marker]
    content = text[marker + 5 :]
    envelope: dict[str, Any] = {}
    for line in header.splitlines():
        if ":" not in line:
            raise KBError("public object frontmatter contains an invalid line")
        key, raw_value = line.split(":", 1)
        try:
            envelope[key.strip()] = json.loads(raw_value.strip())
        except json.JSONDecodeError as exc:
            raise KBError(f"invalid frontmatter value for {key.strip()}") from exc
    envelope["content"] = content
    return envelope


class KnowledgeVault:
    def __init__(self, root: str | Path, *, age_path: str | Path | None = None):
        self.root = Path(root).resolve()
        self.local = LocalAdapter(self.root)
        self.tiers_config = self._load_json_config("config/tiers.yml")
        self.policies = self._load_json_config("config/policies.yml")
        self.age_path = self._find_binary("age", age_path)
        self.age_keygen_path = self._find_binary("age-keygen", None)

    def _git_repository_root(self) -> Path | None:
        """Return the containing Git work tree without requiring a nested repository."""
        for candidate in (self.root, *self.root.parents):
            marker = candidate / ".git"
            if marker.is_dir() or marker.is_file():
                return candidate
        return None

    def _load_json_config(self, relative: str) -> dict[str, Any]:
        try:
            return json.loads(self.local.read_text(relative))
        except (OSError, json.JSONDecodeError) as exc:
            raise KBError(f"invalid or missing configuration: {relative}") from exc

    def _find_binary(self, name: str, explicit: str | Path | None) -> Path | None:
        if explicit is not None:
            path = Path(explicit).resolve()
            return path if path.is_file() else None
        env_name = {"age": "KB_AGE_PATH", "age-keygen": "KB_AGE_KEYGEN_PATH"}.get(name)
        if env_name and os.environ.get(env_name):
            path = Path(os.environ[env_name]).resolve()
            if path.is_file():
                return path
        suffix = ".exe" if os.name == "nt" else ""
        local_path = self.root / ".local" / "bin" / f"{name}{suffix}"
        if local_path.is_file():
            return local_path
        if name == "age-keygen" and getattr(self, "age_path", None):
            sibling = Path(self.age_path).with_name(f"age-keygen{suffix}")
            if sibling.is_file():
                return sibling.resolve()
        found = shutil.which(name)
        return Path(found).resolve() if found else None

    def init_layout(self) -> dict[str, Any]:
        required = [
            "docs",
            "questions",
            "reference",
            "config/schemas",
            "inbox/remote",
            "inbox/local",
            "manifests/projections",
            "receipts",
            "recovery",
            ".local/decrypted",
            ".local/cache",
            ".local/test-keys",
            ".local/private-index",
            ".local/authorized-results",
            ".local/semantic",
            ".local/trusted-search",
            ".local/capabilities",
            ".local/bin",
        ]
        for tier in TIERS:
            for kind in KINDS:
                required.append(f"vault/{tier}/{kind}")
        for relative in required:
            self.local.resolve(relative).mkdir(parents=True, exist_ok=True)
        for relative in ("index.jsonl", "manifests/catalog.jsonl"):
            if not self.local.exists(relative):
                self.local.atomic_write_text(relative, "")
        if not self.local.exists("index.md"):
            self.local.atomic_write_text("index.md", self._render_index_md([]))
        return {"status": "ok", "directories": len(required)}

    def _require_age(self) -> Path:
        if not self.age_path or not self.age_path.is_file():
            raise KBError("age executable is unavailable; run doctor")
        return self.age_path

    def _require_age_keygen(self) -> Path:
        if not self.age_keygen_path or not self.age_keygen_path.is_file():
            raise KBError("age-keygen executable is unavailable; run doctor")
        return self.age_keygen_path

    @staticmethod
    def _run(args: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def encrypt_bytes(self, plaintext: bytes, recipient: str) -> bytes:
        if not recipient.startswith("age1"):
            raise KBError("recipient is missing or invalid")
        result = self._run([str(self._require_age()), "-r", recipient], input_bytes=plaintext)
        if result.returncode != 0:
            raise KBError("age encryption failed")
        return result.stdout

    def decrypt_bytes(self, ciphertext: bytes, identity: str | Path) -> bytes:
        identity_path = Path(identity).resolve()
        if not identity_path.is_file():
            raise KBError("identity path does not exist")
        result = self._run(
            [str(self._require_age()), "-d", "-i", str(identity_path)],
            input_bytes=ciphertext,
        )
        if result.returncode != 0:
            raise KBError("identity is not authorized for this object")
        return result.stdout

    def identity_recipient(self, identity: str | Path) -> str:
        identity_path = Path(identity).resolve()
        if not identity_path.is_file():
            raise KBError("identity path does not exist")
        result = self._run(
            [str(self._require_age_keygen()), "-y", str(identity_path)]
        )
        if result.returncode != 0:
            raise KBError("unable to derive recipient from identity")
        return result.stdout.decode("utf-8").strip()

    def _restrict_identity_acl(self, path: Path) -> None:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return
        whoami = subprocess.run(
            ["whoami"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
        if whoami.returncode != 0:
            raise KBError("unable to determine current Windows identity")
        account = whoami.stdout.decode("mbcs", errors="replace").strip()
        grant = f"{account}:(F)"
        result = subprocess.run(
            [
                "icacls.exe",
                str(path),
                "/inheritance:r",
                "/grant:r",
                grant,
                "*S-1-5-18:(F)",
                "*S-1-5-32-544:(F)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise KBError("unable to restrict identity ACL")

    def generate_test_keys(self, *, force: bool = False) -> dict[str, str]:
        keygen = self._require_age_keygen()
        recipients: dict[str, str] = {}
        for tier in PRIVATE_TIERS:
            relative = Path(".local/test-keys") / f"{tier}.identity"
            target = self.local.resolve(relative)
            if target.exists() and not force:
                recipients[tier] = self.identity_recipient(target)
                continue
            result = self._run([str(keygen)])
            if result.returncode != 0 or b"AGE-SECRET-KEY-" not in result.stdout:
                raise KBError(f"failed to generate {tier} test identity")
            self.local.atomic_write_bytes(relative, result.stdout)
            self._restrict_identity_acl(target)
            recipients[tier] = self.identity_recipient(target)
        self.local.atomic_write_text(
            ".local/test-keys/recipients.json",
            json.dumps(recipients, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return recipients

    def recipient_for(
        self, tier: str, overrides: Mapping[str, str] | None = None
    ) -> str:
        if tier not in PRIVATE_TIERS:
            raise KBError(f"tier does not use a recipient: {tier}")
        if overrides and overrides.get(tier):
            return overrides[tier]
        tier_cfg = self.tiers_config["tiers"][tier]
        recipient = os.environ.get(tier_cfg["recipient_env"], "")
        if not recipient:
            local_recipients = self.local.resolve(".local/test-keys/recipients.json")
            if local_recipients.is_file():
                recipient = json.loads(local_recipients.read_text(encoding="utf-8")).get(
                    tier, ""
                )
        if not recipient:
            raise KBError(f"recipient is not configured for tier: {tier}")
        return recipient

    @staticmethod
    def _validate_tier(tier: str) -> None:
        if tier not in TIERS:
            raise KBError(f"unsupported tier: {tier}")

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in KINDS:
            raise KBError(f"unsupported object kind: {kind}")

    @staticmethod
    def validate_envelope(envelope: Mapping[str, Any]) -> None:
        required = {
            "schema_version",
            "object_id",
            "object_kind",
            "tier",
            "title",
            "summary",
            "source_ids",
            "source_uri",
            "content_hash",
            "created_at",
            "updated_at",
            "rights",
            "maturity",
            "lifecycle",
            "catalog_visibility",
            "human_confirmed",
            "content",
        }
        missing = required.difference(envelope)
        if missing:
            raise KBError(f"object envelope is missing fields: {', '.join(sorted(missing))}")
        if envelope["schema_version"] not in (1, 2, 3, 4):
            raise KBError("unsupported object schema version")
        if not ID_RE.match(str(envelope["object_id"])):
            raise KBError("invalid object id")
        if envelope["tier"] not in TIERS or envelope["object_kind"] not in KINDS:
            raise KBError("invalid tier or object kind")
        if envelope["content_hash"] != sha256_text(str(envelope["content"])):
            raise KBError("object content hash mismatch")
        if envelope["catalog_visibility"] not in ("public", "private", "none"):
            raise KBError("invalid catalog visibility")
        if envelope["schema_version"] in (2, 3, 4):
            orthogonal_required = {
                "classification",
                "storage_binding",
                "policy_refs",
                "distribution_decision",
            }
            orthogonal_missing = orthogonal_required.difference(envelope)
            if orthogonal_missing:
                raise KBError(
                    "object envelope is missing orthogonal fields: "
                    + ", ".join(sorted(orthogonal_missing))
                )
        if any(
            field in envelope
            for field in (
                "classification",
                "storage_binding",
                "policy_refs",
                "distribution_decision",
            )
        ):
            issues = validate_orthogonal_fields(envelope)
            if issues:
                raise KBError("invalid orthogonal object fields: " + "; ".join(issues))
        if envelope["schema_version"] in (3, 4):
            semantic_required = {
                "media_type",
                "capture_purpose",
                "wiki_kind",
                "card_kind",
                "topic_ids",
                "relations",
                "capture_state",
                "compile_state",
                "review_state",
            }
            semantic_missing = semantic_required.difference(envelope)
            if semantic_missing:
                raise KBError(
                    "object envelope is missing semantic fields: "
                    + ", ".join(sorted(semantic_missing))
                )
        if envelope["schema_version"] == 4:
            routing_required = {
                "origin_kind",
                "authorship_status",
                "contributors",
                "source_refs",
                "intended_role",
                "corpus_eligibility",
                "style_eligibility",
                "training_permission",
                "clarification_status",
                "clarification_refs",
            }
            routing_missing = routing_required.difference(envelope)
            if routing_missing:
                raise KBError(
                    "object envelope is missing routing fields: "
                    + ", ".join(sorted(routing_missing))
                )
        if any(
            field in envelope
            for field in (
                "media_type",
                "capture_purpose",
                "wiki_kind",
                "card_kind",
                "topic_ids",
                "relations",
                "capture_state",
                "compile_state",
                "review_state",
                "origin_kind",
                "authorship_status",
                "contributors",
                "source_refs",
                "intended_role",
                "corpus_eligibility",
                "style_eligibility",
                "training_permission",
                "clarification_status",
                "clarification_refs",
            )
        ):
            issues = validate_semantic_fields(envelope)
            if issues:
                raise KBError("invalid semantic object fields: " + "; ".join(issues))

    def _domain_root(self) -> Path:
        for candidate in (self.root.parent, *self.root.parents):
            if (candidate / "personal.yaml").is_file():
                return candidate
            legacy = candidate / "config" / "instance.json"
            if not legacy.is_file():
                continue
            try:
                manifest = json.loads(legacy.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get("layout_kind") == "domain-root":
                return candidate
        return self.root

    def _write_clarification_review(self, envelope: Mapping[str, Any]) -> Path:
        questions = clarification_questions(envelope)
        review = {
            "schema_version": 1,
            "object_id": envelope["object_id"],
            "status": "needs-clarification",
            "authorship_status": envelope["authorship_status"],
            "intended_role": envelope["intended_role"],
            "questions": questions,
            "created_at": envelope["created_at"],
        }
        path = self._domain_root() / ".atlas" / "review" / f"{envelope['object_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def _object_path(self, tier: str, kind: str, object_id: str) -> Path:
        suffix = ".age" if tier in PRIVATE_TIERS else ".md"
        return Path("vault") / tier / kind / f"{object_id}{suffix}"

    def _locate_object(self, object_id: str) -> Path:
        if not ID_RE.match(object_id):
            raise KBError("invalid object id")
        matches = [
            path
            for path in self.local.glob(f"vault/*/*/{object_id}.*")
            if path.suffix in (".md", ".age", ".enc")
        ]
        if not matches:
            raise KBError("object does not exist")
        if len(matches) > 1:
            raise ConflictError("object id exists in multiple paths")
        return matches[0]

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _request_fingerprint(self, payload: Mapping[str, Any]) -> str:
        return sha256_text(canonical_json(payload))

    def _receipt_path(self, request_id: str) -> Path:
        return Path("receipts") / f"{stable_token('rct', request_id)}.json"

    def _check_replay(self, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        path = self._receipt_path(request_id)
        if not self.local.exists(path):
            return None
        receipt = json.loads(self.local.read_text(path))
        existing = receipt.get("details", {}).get("request_fingerprint")
        if existing != fingerprint:
            raise ConflictError("request_id was already used with different content")
        replay = copy.deepcopy(receipt)
        replay["idempotent_replay"] = True
        return replay

    def _write_receipt(
        self,
        *,
        request_id: str,
        action: str,
        object_ids: list[str],
        paths: list[str],
        fingerprint: str,
        status: str = "success",
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_details = {"request_fingerprint": fingerprint}
        if details:
            merged_details.update(details)
        receipt = {
            "schema_version": 1,
            "receipt_id": stable_token("rct", request_id),
            "request_id": request_id,
            "action": action,
            "status": status,
            "object_ids": object_ids,
            "paths": paths,
            "created_at": utc_now(),
            "details": merged_details,
        }
        self.local.atomic_write_text(
            self._receipt_path(request_id),
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return receipt

    def _write_public_projection(
        self,
        envelope: Mapping[str, Any],
        object_path: Path,
        title: str,
        summary: str,
    ) -> Path:
        if not title.strip():
            raise KBError("a separate public catalog title is required")
        projection = {
            "schema_version": 1,
            "object_id": envelope["object_id"],
            "object_kind": envelope["object_kind"],
            "tier": envelope["tier"],
            "title": title.strip(),
            "summary": summary.strip(),
            "path": object_path.as_posix(),
            "content_hash": envelope["content_hash"],
            "updated_at": envelope["updated_at"],
            "human_confirmed": True,
        }
        relative = Path("manifests/projections") / f"{envelope['object_id']}.json"
        self.local.atomic_write_text(
            relative,
            json.dumps(projection, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return relative

    def add(
        self,
        *,
        request_id: str,
        tier: str,
        kind: str,
        title: str,
        summary: str,
        content: str,
        source_ids: Iterable[str] = (),
        source_uri: str = "",
        rights: str = "owned",
        maturity: str = "seed",
        lifecycle: str = "active",
        catalog_visibility: str = "private",
        human_confirmed: bool = False,
        object_id: str | None = None,
        catalog_title: str = "",
        catalog_summary: str = "",
        recipients: Mapping[str, str] | None = None,
        action: str = "add",
        media_type: str | None = None,
        capture_purpose: str | None = None,
        wiki_kind: str | None = None,
        card_kind: str | None = None,
        topic_ids: Iterable[str] = (),
        relations: Iterable[Mapping[str, Any]] = (),
        capture_state: str | None = None,
        compile_state: str | None = None,
        review_state: str = "candidate",
        distribution_channel: str | None = None,
        distribution_audience: Iterable[str] = (),
        origin_kind: str | None = None,
        authorship_status: str | None = None,
        contributors: Iterable[str] = (),
        source_refs: Iterable[str] = (),
        intended_role: str | None = None,
        corpus_eligibility: str = "denied",
        style_eligibility: str = "denied",
        training_permission: str = "denied",
        clarification_status: str | None = None,
        clarification_refs: Iterable[str] = (),
    ) -> dict[str, Any]:
        self._validate_tier(tier)
        self._validate_kind(kind)
        normalized_distribution_audience = list(distribution_audience)
        resolved_origin = origin_kind
        if resolved_origin is None:
            resolved_origin = "self" if rights == "owned" else (
                "external" if rights in ("licensed", "restricted") else "unknown"
            )
        resolved_authorship = authorship_status
        if resolved_authorship is None:
            resolved_authorship = "self_authored" if rights == "owned" else (
                "external" if rights in ("licensed", "restricted") else "unknown"
            )
        resolved_intended_role = intended_role
        if resolved_intended_role is None:
            resolved_intended_role = {
                "source_profile": "evidence",
                "subscription": "evidence",
                "capture_event": "evidence",
                "raw": "evidence",
                "wiki": "knowledge",
                "release": "publication",
                "run": "work",
                "feedback": "knowledge",
            }.get(kind, "unknown")
        resolved_clarification = clarification_status
        if resolved_clarification is None:
            resolved_clarification = (
                "required"
                if rights == "unknown"
                or resolved_origin == "unknown"
                or resolved_authorship == "unknown"
                else "answered" if human_confirmed else "not_needed"
            )
        if tier == "archive":
            lifecycle = "archived"
        if not request_id.strip():
            raise KBError("request_id is required")
        if tier in ("public", "archive") and not human_confirmed:
            raise KBError("public and archive writes require human confirmation")
        if catalog_visibility == "public" and not human_confirmed:
            raise KBError("public catalog projection requires human confirmation")
        payload = {
            "action": action,
            "tier": tier,
            "kind": kind,
            "title": title,
            "summary": summary,
            "content": content,
            "source_ids": list(source_ids),
            "source_uri": source_uri,
            "rights": rights,
            "maturity": maturity,
            "lifecycle": lifecycle,
            "catalog_visibility": catalog_visibility,
            "human_confirmed": human_confirmed,
            "object_id": object_id,
            "catalog_title": catalog_title,
            "catalog_summary": catalog_summary,
            "media_type": media_type,
            "capture_purpose": capture_purpose,
            "wiki_kind": wiki_kind,
            "card_kind": card_kind,
            "topic_ids": list(topic_ids),
            "relations": [dict(item) for item in relations],
            "capture_state": capture_state,
            "compile_state": compile_state,
            "review_state": review_state,
            "origin_kind": resolved_origin,
            "authorship_status": resolved_authorship,
            "contributors": list(contributors),
            "source_refs": list(source_refs),
            "intended_role": resolved_intended_role,
            "corpus_eligibility": corpus_eligibility,
            "style_eligibility": style_eligibility,
            "training_permission": training_permission,
            "clarification_status": resolved_clarification,
            "clarification_refs": list(clarification_refs),
        }
        if distribution_channel is not None or normalized_distribution_audience:
            payload["distribution_channel"] = distribution_channel
            payload["distribution_audience"] = normalized_distribution_audience
        fingerprint = self._request_fingerprint(payload)
        replay = self._check_replay(request_id, fingerprint)
        if replay:
            return replay
        object_id = object_id or stable_token("obj", f"{request_id}:{kind}")
        if not ID_RE.match(object_id):
            raise KBError("invalid object id")
        try:
            self._locate_object(object_id)
        except KBError as exc:
            if str(exc) != "object does not exist":
                raise
        else:
            raise ConflictError("object id already exists")
        now = utc_now()
        relative = self._object_path(tier, kind, object_id)
        content_hash = sha256_text(content)
        envelope = {
            "schema_version": 4,
            "object_id": object_id,
            "object_kind": kind,
            "tier": tier,
            "title": title,
            "summary": summary,
            "source_ids": list(payload["source_ids"]),
            "source_uri": source_uri,
            "content_hash": content_hash,
            "created_at": now,
            "updated_at": now,
            "rights": rights,
            "maturity": maturity,
            "lifecycle": lifecycle,
            "catalog_visibility": catalog_visibility,
            "human_confirmed": human_confirmed,
            "content": content,
        }
        try:
            envelope.update(
                new_orthogonal_fields(
                    tier=tier,
                    lifecycle=lifecycle,
                    content_ref=relative.as_posix(),
                    content_hash=content_hash,
                    catalog_visibility=catalog_visibility,
                    human_confirmed=human_confirmed,
                    timestamp=now,
                    distribution_channel=distribution_channel,
                    distribution_audience=normalized_distribution_audience,
                )
            )
        except ValueError as exc:
            raise KBError(str(exc)) from exc
        try:
            envelope.update(
                new_semantic_fields(
                    object_kind=kind,
                    media_type=media_type,
                    capture_purpose=capture_purpose,
                    wiki_kind=wiki_kind,
                    card_kind=card_kind,
                    topic_ids=list(payload["topic_ids"]),
                    relations=list(payload["relations"]),
                    capture_state=capture_state,
                    compile_state=compile_state or ("uncompiled" if kind == "raw" else None),
                    review_state=review_state,
                    origin_kind=resolved_origin,
                    authorship_status=resolved_authorship,
                    contributors=list(payload["contributors"]),
                    source_refs=list(payload["source_refs"]),
                    intended_role=resolved_intended_role,
                    corpus_eligibility=corpus_eligibility,
                    style_eligibility=style_eligibility,
                    training_permission=training_permission,
                    clarification_status=resolved_clarification,
                    clarification_refs=list(payload["clarification_refs"]),
                )
            )
        except ValueError as exc:
            raise KBError(str(exc)) from exc
        self.validate_envelope(envelope)
        if envelope["clarification_status"] == "required":
            channel = envelope["distribution_decision"]["channel"]
            if tier in ("public", "archive") or catalog_visibility == "public" or channel != "local-only":
                raise KBError("unresolved authorship or usage questions cannot be published")
            if review_state != "candidate" or maturity not in ("seed", "draft"):
                raise KBError(
                    "unresolved authorship or usage questions must remain draft candidates"
                )
        projection: Path | None = None
        clarification_review: Path | None = None
        created = False
        try:
            if tier in PRIVATE_TIERS:
                encrypted = self.encrypt_bytes(
                    (canonical_json(envelope) + "\n").encode("utf-8"),
                    self.recipient_for(tier, recipients),
                )
                self.local.atomic_write_bytes(relative, encrypted)
                created = True
                if catalog_visibility == "public":
                    projection = self._write_public_projection(
                        envelope, relative, catalog_title, catalog_summary
                    )
            else:
                self.local.atomic_write_text(relative, render_markdown(envelope))
                created = True
            self.reindex()
            if envelope["clarification_status"] == "required":
                clarification_review = self._write_clarification_review(envelope)
            receipt = self._write_receipt(
                request_id=request_id,
                action=action,
                object_ids=[object_id],
                paths=[relative.as_posix()],
                fingerprint=fingerprint,
                status=(
                    "needs-clarification"
                    if envelope["clarification_status"] == "required"
                    else "success"
                ),
                details={
                    "tier": tier,
                    "content_hash": envelope["content_hash"],
                    "clarification_status": envelope["clarification_status"],
                    "questions": clarification_questions(envelope),
                    "review_path": str(clarification_review) if clarification_review else None,
                },
            )
            receipt["object_id"] = object_id
            return receipt
        except Exception:
            if created:
                self.local.unlink(relative, missing_ok=True)
            if projection:
                self.local.unlink(projection, missing_ok=True)
            if clarification_review:
                clarification_review.unlink(missing_ok=True)
            try:
                self.reindex()
            except Exception:
                pass
            raise

    def _read_object_path(
        self, path: Path, identities: Mapping[str, str | Path] | None = None
    ) -> dict[str, Any]:
        relative = self._relative(path)
        parts = Path(relative).parts
        tier = parts[1]
        if tier in PRIVATE_TIERS:
            identity = (identities or {}).get(tier)
            if not identity:
                raise KBError(f"identity is required for tier: {tier}")
            plaintext = self.decrypt_bytes(path.read_bytes(), identity)
            try:
                envelope = json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KBError("decrypted object envelope is invalid") from exc
        else:
            envelope = parse_markdown(path.read_text(encoding="utf-8"))
        envelope = materialize_orthogonal_fields(envelope, content_ref=relative)
        envelope = materialize_semantic_fields(envelope)
        self.validate_envelope(envelope)
        if envelope["tier"] != tier:
            raise KBError("object tier does not match its path")
        return envelope

    def get(
        self,
        object_id: str,
        *,
        identities: Mapping[str, str | Path] | None = None,
    ) -> dict[str, Any]:
        path = self._locate_object(object_id)
        envelope = self._read_object_path(path, identities)
        output = Path(".local/decrypted") / envelope["tier"] / envelope["object_kind"] / f"{object_id}.md"
        self.local.atomic_write_text(output, render_markdown(envelope))
        return {
            "status": "ok",
            "object_id": object_id,
            "tier": envelope["tier"],
            "content_hash": envelope["content_hash"],
            "output_path": output.as_posix(),
        }

    def _projection_path(self, object_id: str) -> Path:
        return Path("manifests/projections") / f"{object_id}.json"

    def move(
        self,
        *,
        request_id: str,
        object_id: str,
        target_tier: str,
        identities: Mapping[str, str | Path] | None = None,
        recipients: Mapping[str, str] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self._validate_tier(target_tier)
        payload = {
            "action": "move",
            "object_id": object_id,
            "target_tier": target_tier,
            "confirm": confirm,
        }
        fingerprint = self._request_fingerprint(payload)
        replay = self._check_replay(request_id, fingerprint)
        if replay:
            return replay
        source_path = self._locate_object(object_id)
        envelope = self._read_object_path(source_path, identities)
        source_tier = envelope["tier"]
        if source_tier == target_tier:
            raise ConflictError("object is already in the target tier")
        if TIER_RANK[target_tier] < TIER_RANK[source_tier] and not confirm:
            raise KBError("moving to a lower tier requires explicit confirmation")
        target_relative = self._object_path(target_tier, envelope["object_kind"], object_id)
        if self.local.exists(target_relative):
            raise ConflictError("target object path already exists")
        moved = dict(envelope)
        moved["schema_version"] = 3
        moved["tier"] = target_tier
        moved["updated_at"] = utc_now()
        if target_tier == "archive":
            moved["lifecycle"] = "archived"
        elif target_tier == "public":
            moved["lifecycle"] = "active"
        try:
            moved.update(
                new_orthogonal_fields(
                    tier=target_tier,
                    lifecycle=moved["lifecycle"],
                    content_ref=target_relative.as_posix(),
                    content_hash=moved["content_hash"],
                    catalog_visibility=moved["catalog_visibility"],
                    human_confirmed=moved["human_confirmed"],
                    timestamp=moved["updated_at"],
                )
            )
        except ValueError as exc:
            raise KBError(str(exc)) from exc
        moved["classification"]["reason"] = "updated-by-compatibility-move"
        moved["classification"]["reviewed_at"] = moved["updated_at"] if confirm else None
        self.validate_envelope(moved)
        if target_tier in PRIVATE_TIERS:
            target_bytes = self.encrypt_bytes(
                (canonical_json(moved) + "\n").encode("utf-8"),
                self.recipient_for(target_tier, recipients),
            )
            self.local.atomic_write_bytes(target_relative, target_bytes)
            target_identity = (identities or {}).get(target_tier)
            if not target_identity:
                self.local.unlink(target_relative, missing_ok=True)
                raise KBError("target identity is required to verify the new ciphertext")
            verified = json.loads(
                self.decrypt_bytes(target_bytes, target_identity).decode("utf-8")
            )
        else:
            self.local.atomic_write_text(target_relative, render_markdown(moved))
            verified = parse_markdown(self.local.read_text(target_relative))
        self.validate_envelope(verified)
        if verified["content_hash"] != envelope["content_hash"]:
            self.local.unlink(target_relative, missing_ok=True)
            raise KBError("target verification hash mismatch")
        self.local.unlink(self._relative(source_path))
        projection_path = self._projection_path(object_id)
        existing_projection: dict[str, Any] | None = None
        if self.local.exists(projection_path):
            existing_projection = json.loads(self.local.read_text(projection_path))
        if target_tier in PRIVATE_TIERS and moved["catalog_visibility"] == "public":
            safe_title = existing_projection["title"] if existing_projection else moved["title"]
            safe_summary = existing_projection["summary"] if existing_projection else moved["summary"]
            self._write_public_projection(moved, target_relative, safe_title, safe_summary)
        else:
            self.local.unlink(projection_path, missing_ok=True)
        self.reindex()
        return self._write_receipt(
            request_id=request_id,
            action="move",
            object_ids=[object_id],
            paths=[self._relative(source_path), target_relative.as_posix()],
            fingerprint=fingerprint,
            details={
                "from_tier": source_tier,
                "to_tier": target_tier,
                "content_hash": moved["content_hash"],
            },
        )

    def release(
        self,
        *,
        request_id: str,
        source_object_id: str,
        title: str,
        summary: str,
        content: str,
        identities: Mapping[str, str | Path] | None = None,
        rights: str = "owned",
        maturity: str = "reviewed",
        human_confirmed: bool = False,
        origin_kind: str | None = None,
        authorship_status: str | None = None,
        contributors: Iterable[str] = (),
        source_refs: Iterable[str] = (),
    ) -> dict[str, Any]:
        if not human_confirmed:
            raise KBError("release requires explicit human confirmation")
        source_path = self._locate_object(source_object_id)
        source = self._read_object_path(source_path, identities)
        resolved_origin = origin_kind or str(source.get("origin_kind", "unknown"))
        resolved_authorship = authorship_status or str(
            source.get("authorship_status", "unknown")
        )
        if resolved_origin == "unknown" or resolved_authorship == "unknown":
            raise KBError("release authorship must be confirmed before publication")
        result = self.add(
            request_id=request_id,
            tier="public",
            kind="release",
            title=title,
            summary=summary,
            content=content,
            source_ids=[source_object_id],
            source_uri="",
            rights=rights,
            maturity=maturity,
            lifecycle="active",
            catalog_visibility="public",
            human_confirmed=True,
            origin_kind=resolved_origin,
            authorship_status=resolved_authorship,
            contributors=list(contributors) or list(source.get("contributors", [])),
            source_refs=list(source_refs) or [source_object_id],
            intended_role="publication",
            clarification_status="answered",
            clarification_refs=[source_object_id],
            action="release",
        )
        result["source_object_id"] = source["object_id"]
        return result

    def _catalog_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for tier in ("public", "archive"):
            for path in self.local.glob(f"vault/{tier}/*/*.md"):
                envelope = self._read_object_path(path)
                if not (
                    envelope["catalog_visibility"] == "public"
                    and envelope["human_confirmed"] is True
                ):
                    continue
                records.append(
                    {
                        "schema_version": 1,
                        "object_id": envelope["object_id"],
                        "object_kind": envelope["object_kind"],
                        "tier": tier,
                        "title": envelope["title"],
                        "summary": envelope["summary"],
                        "path": self._relative(path),
                        "content_hash": envelope["content_hash"],
                        "updated_at": envelope["updated_at"],
                        "locked": False,
                    }
                )
        for path in self.local.glob("manifests/projections/*.json"):
            projection = json.loads(path.read_text(encoding="utf-8"))
            if projection.get("human_confirmed") is not True:
                raise KBError("public projection is not human confirmed")
            record = {key: value for key, value in projection.items() if key != "human_confirmed"}
            record["locked"] = True
            records.append(record)
        records.sort(key=lambda item: (item["tier"], item["object_id"], item["path"]))
        return records

    def public_catalog_records(self) -> list[dict[str, Any]]:
        """Return only the explicitly confirmed catalog used by public projections."""
        return self._catalog_records()

    @staticmethod
    def _render_jsonl(records: Iterable[Mapping[str, Any]]) -> str:
        lines = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records]
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _render_index_md(records: Iterable[Mapping[str, Any]]) -> str:
        lines = [
            "# Knowledge Vault 公共目录",
            "",
            "此文件由 `python scripts/kb.py reindex` 从安全公开投影重建，请勿手工追加。",
            "",
        ]
        count = 0
        for item in records:
            count += 1
            locked = " 🔒" if item.get("locked") else ""
            summary = f" — {item['summary']}" if item.get("summary") else ""
            lines.append(
                f"- [{item['title']}]({item['path']}){locked} `[{item['tier']}]`{summary}"
            )
        if count == 0:
            lines.append("当前没有公开目录项。")
        return "\n".join(lines) + "\n"

    def reindex(self) -> dict[str, Any]:
        records = self._catalog_records()
        jsonl = self._render_jsonl(records)
        self.local.atomic_write_text("index.jsonl", jsonl)
        self.local.atomic_write_text("manifests/catalog.jsonl", jsonl)
        self.local.atomic_write_text("index.md", self._render_index_md(records))
        return {"status": "ok", "records": len(records)}

    def unlock_index(
        self, identities: Mapping[str, str | Path]
    ) -> dict[str, Any]:
        docs: list[dict[str, Any]] = []
        unlocked: list[str] = []
        recipients: dict[str, str] = {}
        for tier in PRIVATE_TIERS:
            identity = identities.get(tier)
            if not identity:
                continue
            identity_recipient = self.identity_recipient(identity)
            configured = self.recipient_for(tier)
            if identity_recipient != configured:
                raise KBError(f"identity does not match configured {tier} recipient")
            tier_docs: list[dict[str, Any]] = []
            for path in self.local.glob(f"vault/{tier}/*/*.age"):
                envelope = self._read_object_path(path, {tier: identity})
                item = dict(envelope)
                item["path"] = self._relative(path)
                item["locked"] = False
                tier_docs.append(item)
            docs.extend(tier_docs)
            unlocked.append(tier)
            recipients[tier] = identity_recipient
        docs.sort(key=lambda item: (item["tier"], item["object_id"]))
        self.local.atomic_write_text(
            ".local/private-index/authorized.jsonl", self._render_jsonl(docs)
        )
        state = {"schema_version": 1, "built_at": utc_now(), "tiers": unlocked, "recipients": recipients}
        self.local.atomic_write_text(
            ".local/private-index/state.json",
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return {"status": "ok", "tiers": unlocked, "records": len(docs)}

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def _public_documents(self) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for tier in ("public", "archive"):
            for path in self.local.glob(f"vault/{tier}/*/*.md"):
                envelope = self._read_object_path(path)
                item = dict(envelope)
                item["path"] = self._relative(path)
                item["locked"] = False
                by_id[item["object_id"]] = item
        for record in self._load_jsonl(self.local.resolve("index.jsonl")):
            if record.get("locked"):
                by_id.setdefault(record["object_id"], record)
        return sorted(by_id.values(), key=lambda item: (item["tier"], item["object_id"]))

    def _visible_documents(self) -> list[dict[str, Any]]:
        by_id = {item["object_id"]: item for item in self._public_documents()}
        authorized = self._load_jsonl(
            self.local.resolve(".local/private-index/authorized.jsonl")
        )
        for item in authorized:
            by_id[item["object_id"]] = item
        return sorted(by_id.values(), key=lambda item: (item["tier"], item["object_id"]))

    @staticmethod
    def _snippet(content: str, query: str, width: int = 120) -> str:
        if not content or not query:
            return ""
        folded = content.casefold()
        index = folded.find(query.casefold())
        if index < 0:
            return ""
        start = max(0, index - width // 3)
        end = min(len(content), start + width)
        return content[start:end].replace("\n", " ").strip()

    def _search_documents(
        self, documents: Iterable[Mapping[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        results: list[dict[str, Any]] = []
        for item in documents:
            fields = [
                str(item.get("title", "")),
                str(item.get("summary", "")),
                str(item.get("source_uri", "")),
                str(item.get("content", "")),
            ]
            if needle and not any(needle in field.casefold() for field in fields):
                continue
            results.append(
                {
                    "object_id": item["object_id"],
                    "object_kind": item["object_kind"],
                    "tier": item["tier"],
                    "title": item["title"],
                    "summary": item.get("summary", ""),
                    "path": item["path"],
                    "locked": bool(item.get("locked", False)),
                    "snippet": self._snippet(str(item.get("content", "")), query),
                }
            )
        return results

    def search_public(self, query: str) -> list[dict[str, Any]]:
        return self._search_documents(self._public_documents(), query)

    def search(self, query: str) -> list[dict[str, Any]]:
        return self._search_documents(self._visible_documents(), query)

    def list_visible(self) -> list[dict[str, Any]]:
        return self.search("")

    def lock(self) -> dict[str, Any]:
        removed = 0
        for relative in (
            ".local/private-index",
            ".local/decrypted",
            ".local/authorized-results",
            ".local/semantic",
            ".local/trusted-search",
            ".local/capabilities",
        ):
            path = self.local.resolve(relative)
            if path.exists():
                removed += sum(1 for item in path.rglob("*") if item.is_file())
                shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)
        return {"status": "ok", "removed_files": removed}

    def _publishable_files(self) -> list[Path]:
        if not (self.root / ".git").exists():
            return [
                path
                for path in self.root.rglob("*")
                if path.is_file()
                and ".local" not in path.parts
                and not ("reference" in path.parts and "repos" in path.parts)
            ]
        empty_config = self.local.resolve(".local/cache/empty-gitconfig")
        empty_config.parent.mkdir(parents=True, exist_ok=True)
        empty_config.touch(exist_ok=True)
        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = str(empty_config)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise KBError("unable to enumerate tracked files")
        return [self.root / value.decode("utf-8") for value in result.stdout.split(b"\0") if value]

    def verify(
        self,
        *,
        identities: Mapping[str, str | Path] | None = None,
        forbidden_terms: Iterable[str] = (),
        leak_scan: bool = True,
    ) -> dict[str, Any]:
        issues: list[str] = []
        checked = 0
        for tier in PRIVATE_TIERS:
            for path in self.local.glob(f"vault/{tier}/**/*"):
                if not path.is_file():
                    continue
                checked += 1
                if path.suffix not in (".age", ".enc"):
                    issues.append(f"private tier contains non-ciphertext: {self._relative(path)}")
                    continue
                if not path.read_bytes().startswith(b"age-encryption.org/v1"):
                    issues.append(f"invalid age header: {self._relative(path)}")
                if identities and identities.get(tier):
                    try:
                        self._read_object_path(path, identities)
                    except KBError:
                        issues.append(f"unable to verify private object: {self._relative(path)}")
        for tier in ("public", "archive"):
            for path in self.local.glob(f"vault/{tier}/*/*.md"):
                checked += 1
                try:
                    self._read_object_path(path)
                except KBError:
                    issues.append(f"invalid public object: {self._relative(path)}")
        try:
            expected = self._catalog_records()
            expected_jsonl = self._render_jsonl(expected)
            if self.local.read_text("index.jsonl") != expected_jsonl:
                issues.append("index.jsonl is stale")
            if self.local.read_text("manifests/catalog.jsonl") != expected_jsonl:
                issues.append("catalog.jsonl is stale")
            if self.local.read_text("index.md") != self._render_index_md(expected):
                issues.append("index.md is stale")
        except (KBError, OSError, json.JSONDecodeError):
            issues.append("public indexes cannot be rebuilt safely")
        temp_files = [path for path in self.local.glob("vault/**/.*.tmp") if path.is_file()]
        for path in temp_files:
            issues.append(f"orphan atomic temp file: {self._relative(path)}")
        if leak_scan:
            terms = [term for term in forbidden_terms if term]
            for path in self._publishable_files():
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                    issues.append(f"credential-like content in publishable file: {self._relative(path)}")
                for term in terms:
                    if term in text:
                        issues.append(f"forbidden term in publishable file: {self._relative(path)}")
                        break
        return {"ok": not issues, "issues": sorted(set(issues)), "objects_checked": checked}

    def doctor(
        self, identities: Mapping[str, str | Path] | None = None
    ) -> dict[str, Any]:
        git_root = self._git_repository_root()
        checks: dict[str, Any] = {
            "python": sys.version.split()[0],
            "root": str(self.root),
            "git_repository": git_root is not None,
            "git_repository_root": str(git_root) if git_root else None,
            "age": str(self.age_path) if self.age_path else None,
            "age_keygen": str(self.age_keygen_path) if self.age_keygen_path else None,
            "configs": True,
            "recipients": {},
            "identities": {},
        }
        for tier in PRIVATE_TIERS:
            try:
                checks["recipients"][tier] = bool(self.recipient_for(tier))
            except KBError:
                checks["recipients"][tier] = False
            identity = (identities or {}).get(tier)
            checks["identities"][tier] = bool(identity and Path(identity).is_file())
        checks["ok"] = bool(
            checks["git_repository"]
            and checks["age"]
            and checks["age_keygen"]
            and all(checks["recipients"].values())
        )
        return checks

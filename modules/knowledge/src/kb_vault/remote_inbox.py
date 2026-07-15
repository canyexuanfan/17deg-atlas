from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Callable, Iterable

from .adapters.github_contents import GitHubContentsAdapter
from .core import (
    KBError,
    PRIVATE_TIERS,
    SECRET_PATTERNS,
    TIERS,
    canonical_json,
    stable_token,
    utc_now,
)
from .model import new_orthogonal_fields


AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
OBJECT_ID_RE = re.compile(r"^obj_[a-z0-9_-]+$")
KINDS = ("raw", "wiki", "release")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class GitHubRemoteInbox:
    """Append-only remote knowledge intake; it never accepts an age identity."""

    def __init__(
        self,
        adapter: GitHubContentsAdapter,
        *,
        namespace: str = "knowledge",
    ):
        self.adapter = adapter
        self.namespace = namespace.strip("/")

    def check(self, *, path: str = "README.md", ref: str = "main") -> dict[str, Any]:
        result = self.adapter.get_file(path=path, ref=ref)
        return {
            "status": "ok",
            "repository": f"{self.adapter.owner}/{self.adapter.repo}",
            "branch": ref,
            "path": path,
            "sha": result["sha"],
        }

    @staticmethod
    def _content_field(content: bytes) -> tuple[str, str]:
        try:
            return "utf-8", content.decode("utf-8")
        except UnicodeDecodeError:
            return "base64", base64.b64encode(content).decode("ascii")

    def build_event(
        self,
        *,
        request_id: str,
        agent_id: str,
        tier: str,
        kind: str,
        title: str,
        summary: str,
        content: bytes,
        source_uri: str = "",
        object_id: str | None = None,
        human_confirmed: bool = False,
        distribution_channel: str | None = None,
        distribution_audience: Iterable[str] = (),
        recipient: str | None = None,
        encrypt: Callable[[bytes, str], bytes] | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        if not request_id.strip():
            raise KBError("remote request_id is required")
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise KBError("agent_id must use 1-48 lowercase letters, digits, underscores, or hyphens")
        if tier not in TIERS:
            raise KBError("invalid remote knowledge tier")
        if kind not in KINDS:
            raise KBError("invalid remote object kind")
        if tier in ("public", "archive") and not human_confirmed:
            raise KBError("public/archive remote input requires explicit human confirmation")
        if not title.strip():
            raise KBError("remote title is required")
        resolved_id = object_id or stable_token("obj_remote", f"{agent_id}:{request_id}")
        if not OBJECT_ID_RE.fullmatch(resolved_id):
            raise KBError("invalid remote object_id")
        created = created_at or utc_now()
        payload_name = "payload.json.age" if tier in PRIVATE_TIERS else "payload.json"
        base = f"{self.namespace}/inbox/remote/{agent_id}/{resolved_id}"
        payload_path = f"{base}/{payload_name}"
        lifecycle = "archived" if tier == "archive" else "active"
        encoding, value = self._content_field(content)
        envelope = {
            "schema_version": 1,
            "object_id": resolved_id,
            "request_id": request_id,
            "submitted_by": agent_id,
            "tier": tier,
            "object_kind": kind,
            "title": title,
            "summary": summary,
            "source_uri": source_uri,
            "content_hash": _sha256(content),
            "created_at": created,
            "lifecycle": lifecycle,
            "content_encoding": encoding,
            "content": value,
        }
        try:
            envelope.update(
                new_orthogonal_fields(
                    tier=tier,
                    lifecycle=lifecycle,
                    content_ref=payload_path,
                    content_hash=envelope["content_hash"],
                    catalog_visibility="private",
                    human_confirmed=human_confirmed,
                    timestamp=created,
                    backend="github",
                    distribution_channel=distribution_channel,
                    distribution_audience=list(distribution_audience),
                )
            )
        except ValueError as exc:
            raise KBError(str(exc)) from exc
        plaintext = (canonical_json(envelope) + "\n").encode("utf-8")
        if tier in PRIVATE_TIERS:
            if not recipient or not recipient.startswith("age1"):
                raise KBError(f"production age recipient is not configured for {tier}")
            if encrypt is None:
                raise KBError("age encryptor is required for private remote input")
            payload = encrypt(plaintext, recipient)
            if not payload.startswith(b"age-encryption.org/v1"):
                raise KBError("remote encryption did not produce an age payload")
            distribution = "public-ciphertext"
        else:
            text = plaintext.decode("utf-8")
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                raise KBError("credential-like content is forbidden in a public remote payload")
            payload = plaintext
            distribution = "public-plaintext"
        ready = {
            "schema_version": 1,
            "event_type": "remote-knowledge-ready",
            "object_id": resolved_id,
            "submitted_by": agent_id,
            "tier": tier,
            "distribution": distribution,
            "payload_path": payload_path,
            "payload_sha256": _sha256(payload),
            "payload_size": len(payload),
            "created_at": envelope["created_at"],
        }
        return {
            "object_id": resolved_id,
            "payload_path": payload_path,
            "payload": payload,
            "ready_path": f"{base}/READY.json",
            "ready": (json.dumps(ready, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        }

    def add(
        self,
        *,
        branch: str,
        confirm_remote_write: bool,
        commit_prefix: str = "知识库远端追加",
        **event_args: Any,
    ) -> dict[str, Any]:
        if not confirm_remote_write:
            raise KBError("real GitHub write requires --confirm-remote-write")
        event = self.build_event(**event_args)
        payload_result = self.adapter.put_file(
            path=event["payload_path"],
            content=event["payload"],
            message=f"{commit_prefix}：写入 {event['object_id']} 载荷",
            branch=branch,
        )
        try:
            ready_result = self.adapter.put_file(
                path=event["ready_path"],
                content=event["ready"],
                message=f"{commit_prefix}：确认 {event['object_id']} 完成",
                branch=branch,
            )
        except KBError as exc:
            raise KBError(
                f"remote payload was written but READY marker failed for {event['object_id']}; "
                "the incomplete event must remain ignored"
            ) from exc
        return {
            "status": "ok",
            "object_id": event["object_id"],
            "payload_path": event["payload_path"],
            "ready_path": event["ready_path"],
            "payload_commit_sha": payload_result["commit_sha"],
            "ready_commit_sha": ready_result["commit_sha"],
        }

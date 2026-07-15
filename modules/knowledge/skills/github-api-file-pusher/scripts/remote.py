#!/usr/bin/env python3
"""Standalone, append-only remote intake client for a configured GitHub repository."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote


API_VERSION = "2026-03-10"
PRIVATE_TIERS = ("basic", "advanced", "core")
RECIPIENT_ENVS = {
    "basic": "KB_AGE_RECIPIENT_BASIC",
    "advanced": "KB_AGE_RECIPIENT_ADVANCED",
    "core": "KB_AGE_RECIPIENT_CORE",
}
IDENTITY_ENVS = {
    "basic": "KB_AGE_IDENTITY_BASIC_FILE",
    "advanced": "KB_AGE_IDENTITY_ADVANCED_FILE",
    "core": "KB_AGE_IDENTITY_CORE_FILE",
}
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
OBJECT_RE = re.compile(r"^obj_[a-z0-9_-]+$")
SECRET_PATTERNS = (
    re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{40,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class RemoteError(RuntimeError):
    pass


Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, dict[str, Any]]]


def default_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"message": "GitHub API request failed"}
        return exc.code, payload


class Client:
    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        branch: str = "main",
        token: str | None = None,
        require_token: bool = True,
        transport: Transport | None = None,
    ):
        if not owner.strip() or not repo.strip() or not branch.strip():
            raise RemoteError("GitHub owner, repository, and branch are required")
        self.owner = owner.strip()
        self.repo = repo.strip()
        self.branch = branch.strip()
        self.token = os.environ.get("KB_GITHUB_TOKEN", "") if token is None else token
        if require_token and not self.token:
            raise RemoteError("KB_GITHUB_TOKEN is not configured")
        self.custom_transport = transport is not None
        self.transport = transport or default_transport

    def _url(self, path: str, ref: str | None = None) -> str:
        parts = path.replace("\\", "/").strip("/").split("/")
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise RemoteError("invalid repository path")
        encoded = "/".join(quote(part, safe="") for part in parts)
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents/{encoded}"
        return url + (f"?ref={quote(ref, safe='')}" if ref else "")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "kb-remote-pusher",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _raw_url(self, path: str, ref: str | None = None) -> str:
        parts = path.replace("\\", "/").strip("/").split("/")
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise RemoteError("invalid repository path")
        encoded_path = "/".join(quote(part, safe="") for part in parts)
        encoded_owner = quote(self.owner, safe="")
        encoded_repo = quote(self.repo, safe="")
        encoded_ref = quote(ref or self.branch, safe="")
        return f"https://raw.githubusercontent.com/{encoded_owner}/{encoded_repo}/{encoded_ref}/{encoded_path}"

    def _get_public_raw(self, path: str, ref: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self._raw_url(path, ref),
            headers={"Accept": "application/octet-stream", "User-Agent": "kb-remote-pusher"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            raise RemoteError(f"public GitHub read failed with status {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RemoteError("public GitHub read failed because the network is unavailable") from exc
        return {"sha": "sha256:" + hashlib.sha256(data).hexdigest()}

    def get(self, path: str, ref: str | None = None) -> dict[str, Any]:
        ref = ref or self.branch
        if not self.token and not self.custom_transport:
            return self._get_public_raw(path, ref)
        status, payload = self.transport("GET", self._url(path, ref), self._headers(), None)
        if status != 200:
            raise RemoteError(f"GitHub read failed with status {status}")
        return {"sha": payload.get("sha")}

    def create(self, path: str, content: bytes, message: str) -> dict[str, Any]:
        if not self.token:
            raise RemoteError("KB_GITHUB_TOKEN is required for remote writes")
        body = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.branch,
        }
        status, payload = self.transport(
            "PUT",
            self._url(path),
            self._headers(),
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        if status in (409, 422):
            raise RemoteError("remote object already exists or conflicts")
        if status != 201:
            raise RemoteError(f"GitHub create failed with status {status}")
        return {"commit_sha": payload.get("commit", {}).get("sha")}


def age_encrypt(data: bytes, recipient: str) -> bytes:
    executable = os.environ.get("KB_AGE_PATH") or shutil.which("age")
    if not executable:
        raise RemoteError("age is unavailable; install age or configure KB_AGE_PATH")
    result = subprocess.run(
        [executable, "-r", recipient],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.startswith(b"age-encryption.org/v1"):
        raise RemoteError("age encryption failed")
    return result.stdout


def build_event(args: argparse.Namespace, encryptor: Callable[[bytes, str], bytes] = age_encrypt) -> dict[str, Any]:
    if not AGENT_RE.fullmatch(args.agent_id):
        raise RemoteError("invalid agent id")
    if args.tier in ("public", "archive") and not args.human_confirmed:
        raise RemoteError("public/archive input requires --human-confirmed")
    object_id = args.object_id or (
        "obj_remote_" + hashlib.sha256(f"{args.agent_id}:{args.request_id}".encode()).hexdigest()[:24]
    )
    if not OBJECT_RE.fullmatch(object_id):
        raise RemoteError("invalid object id")
    try:
        data = args.content_file.read_bytes()
    except OSError as exc:
        raise RemoteError("content file cannot be read") from exc
    try:
        encoding, content = "utf-8", data.decode("utf-8")
    except UnicodeDecodeError:
        encoding, content = "base64", base64.b64encode(data).decode("ascii")
    created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    distribution_channel = args.distribution_channel or "local-only"
    distribution_audience = list(dict.fromkeys(args.distribution_audience))
    if not distribution_audience:
        distribution_audience = ["public"] if distribution_channel.startswith("public-") else ["self"]
    if distribution_channel == "controlled-channel" and distribution_audience == ["self"]:
        raise RemoteError("controlled-channel requires an explicit non-self audience")
    if distribution_channel.startswith("public-") and distribution_audience != ["public"]:
        raise RemoteError("public distribution requires the public audience")
    namespace = str(args.namespace).replace("\\", "/").strip("/")
    parts = namespace.split("/")
    if not namespace or any(part in ("", ".", "..", ".git") for part in parts):
        raise RemoteError("invalid remote inbox namespace")
    payload_name = "payload.json.age" if args.tier in PRIVATE_TIERS else "payload.json"
    base = f"{namespace}/{args.agent_id}/{object_id}"
    payload_path = f"{base}/{payload_name}"
    classification_level = "public" if args.tier == "archive" else args.tier
    lifecycle = "archived" if args.tier == "archive" else "active"
    encryption_profile = {
        "public": "plaintext",
        "basic": "age-basic-v1",
        "advanced": "age-advanced-v1",
        "core": "age-core-v1",
    }[classification_level]
    envelope = {
        "schema_version": 1,
        "object_id": object_id,
        "request_id": args.request_id,
        "submitted_by": args.agent_id,
        "tier": args.tier,
        "object_kind": args.kind,
        "title": args.title,
        "summary": args.summary,
        "source_uri": args.source_uri,
        "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        "created_at": created_at,
        "lifecycle": lifecycle,
        "classification": {
            "level": classification_level,
            "reason": "remote-intake-compatibility-tier",
            "authority": "subject:self",
            "classified_at": created_at,
            "reviewed_at": None,
        },
        "storage_binding": {
            "backend": "github",
            "content_ref": payload_path,
            "encryption_profile": encryption_profile,
            "key_version": "v1",
            "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(),
        },
        "policy_refs": [],
        "distribution_decision": {
            "channel": distribution_channel,
            "audience": distribution_audience,
            "license_id": None,
            "approved_by": "subject:self"
            if distribution_channel != "local-only" and args.human_confirmed
            else None,
            "approved_at": created_at
            if distribution_channel != "local-only" and args.human_confirmed
            else None,
        },
        "content_encoding": encoding,
        "content": content,
    }
    plaintext = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if args.tier in PRIVATE_TIERS:
        env_name = RECIPIENT_ENVS[args.tier]
        recipient = os.environ.get(env_name, "")
        if not recipient.startswith("age1"):
            raise RemoteError(f"{env_name} is not configured")
        payload = encryptor(plaintext, recipient)
        distribution = "public-ciphertext"
    else:
        text = plaintext.decode("utf-8")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise RemoteError("credential-like content is forbidden in a public remote payload")
        payload = plaintext
        distribution = "public-plaintext"
    ready = {
        "schema_version": 1,
        "event_type": "remote-knowledge-ready",
        "object_id": object_id,
        "submitted_by": args.agent_id,
        "tier": args.tier,
        "distribution": distribution,
        "payload_path": payload_path,
        "payload_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "payload_size": len(payload),
        "created_at": created_at,
    }
    return {
        "object_id": object_id,
        "payload_path": payload_path,
        "payload": payload,
        "ready_path": f"{base}/READY.json",
        "ready": (json.dumps(ready, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", default=os.environ.get("KB_GITHUB_REPOSITORY", ""))
    parser.add_argument("--owner", default=os.environ.get("KB_GITHUB_OWNER", ""))
    parser.add_argument("--repo", default=os.environ.get("KB_GITHUB_REPO", ""))
    parser.add_argument("--branch", default=os.environ.get("KB_GITHUB_BRANCH", "main"))
    parser.add_argument(
        "--namespace",
        default=os.environ.get("KB_GITHUB_INBOX_PREFIX", "knowledge/inbox/remote"),
    )


def _repository_parts(value: str) -> tuple[str, str]:
    text = value.strip().replace("\\", "/").rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    parts = text.split("/")
    if len(parts) != 2 or not all(parts):
        raise RemoteError("repository must be owner/repo or a GitHub repository URL")
    return parts[0], parts[1]


def _resolve_repository(args: argparse.Namespace) -> None:
    value = getattr(args, "repository", "")
    if not value:
        return
    owner, repo = _repository_parts(value)
    if args.owner and args.owner != owner:
        raise RemoteError("repository owner conflicts with --owner")
    if args.repo and args.repo != repo:
        raise RemoteError("repository name conflicts with --repo")
    args.owner, args.repo = owner, repo


def _infer_role(args: argparse.Namespace) -> tuple[str, bool]:
    if args.role != "auto":
        return args.role, False
    if any(os.environ.get(name) for name in IDENTITY_ENVS.values()):
        raise RemoteError("identity Secret detected; choose authorized-read explicitly and acknowledge risk")
    configured_tiers = [
        tier for tier, name in RECIPIENT_ENVS.items() if os.environ.get(name, "").startswith("age1")
    ]
    if os.environ.get("KB_GITHUB_TOKEN") and configured_tiers:
        if args.tier and args.tier not in configured_tiers:
            raise RemoteError("selected tier recipient is not configured")
        if not args.tier and len(configured_tiers) != 1:
            raise RemoteError("multiple recipients are configured; choose --tier")
        args.tier = args.tier or configured_tiers[0]
        return "encrypted-write", True
    return "public-read", True


def _safe_temp_check() -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-remote-check-") as temp:
            probe = Path(temp) / "probe"
            probe.write_text("ok", encoding="utf-8")
            return probe.read_text(encoding="utf-8") == "ok"
    except OSError:
        return False


def connection_profile(args: argparse.Namespace) -> dict[str, Any]:
    _resolve_repository(args)
    role, role_inferred = _infer_role(args)
    token_configured = bool(os.environ.get("KB_GITHUB_TOKEN", ""))
    profile: dict[str, Any] = {
        "status": "ready",
        "flow": "remote-agent",
        "role": role,
        "role_inferred": role_inferred,
        "repository": f"{args.owner}/{args.repo}" if args.owner and args.repo else None,
        "branch": args.branch,
        "safe_temp": _safe_temp_check(),
        "token_configured": token_configured,
        "token_used": False,
        "recipient_configured": False,
        "identity_configured": False,
        "network_tested": False,
        "permissions": [],
        "risk": "low",
        "expiry": "managed-by-secret-provider",
        "revocation": [],
    }
    if not profile["safe_temp"]:
        raise RemoteError("a writable temporary directory is required")
    if role == "public-read":
        if not args.owner or not args.repo:
            raise RemoteError("public-read requires a content repository")
        profile["permissions"] = ["read-public-content"]
        profile["expiry"] = "not-applicable-without-token"
        profile["revocation"] = ["remove-private-repository-read-token-if-configured"]
    elif role == "encrypted-write":
        if not args.owner or not args.repo:
            raise RemoteError("encrypted-write requires a content repository")
        if not args.confirm_content_repository:
            raise RemoteError("confirm the target is a content repository with --confirm-content-repository")
        if not args.tier:
            raise RemoteError("encrypted-write requires --tier")
        if not token_configured:
            raise RemoteError("KB_GITHUB_TOKEN is required for encrypted-write")
        recipient_name = RECIPIENT_ENVS[args.tier]
        recipient = os.environ.get(recipient_name, "")
        if not recipient.startswith("age1"):
            raise RemoteError(f"{recipient_name} is not configured")
        configured_identities = [name for name in IDENTITY_ENVS.values() if os.environ.get(name)]
        if configured_identities:
            raise RemoteError("encrypted-write runtime must not contain age identity Secrets")
        profile["recipient_configured"] = True
        profile["token_used"] = True
        profile["permissions"] = [f"append-{args.tier}-ciphertext"]
        profile["risk"] = "medium"
        profile["revocation"] = ["revoke-target-repository-token", "rotate-recipient-for-future-writes"]
    elif role == "authorized-read":
        if not args.tier:
            raise RemoteError("authorized-read requires --tier")
        if not args.acknowledge_trusted_runtime:
            raise RemoteError("authorized-read requires explicit trusted-runtime acknowledgement")
        if args.tier == "core" and not args.allow_core:
            raise RemoteError("core remote retrieval is disabled by default")
        identity_name = IDENTITY_ENVS[args.tier]
        identity_value = os.environ.get(identity_name, "")
        if not identity_value or not Path(identity_value).is_file():
            raise RemoteError(f"{identity_name} must reference an identity file Secret")
        if not args.vault_root or not Path(args.vault_root).is_dir():
            raise RemoteError("authorized-read requires a controlled local vault checkout")
        profile["identity_configured"] = True
        profile["permissions"] = [f"read-{args.tier}-within-trusted-runtime"]
        profile["risk"] = "high"
        profile["expiry"] = "long-lived-until-secret-is-removed"
        profile["revocation"] = [
            "remove-runtime-identity-secret-and-clear-workspace",
            "rotate-tier-key-to-revoke-future-access",
        ]
        profile["status"] = "preview"
    else:
        raise RemoteError("unsupported remote role")
    if args.test_connection:
        if not args.owner or not args.repo:
            raise RemoteError("connection test requires owner and repository")
        client = Client(
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            token="" if role == "public-read" else None,
            require_token=role != "public-read",
        )
        client.get(args.path)
        profile["network_tested"] = True
    return profile


def authorized_search(args: argparse.Namespace) -> dict[str, Any]:
    if not args.acknowledge_trusted_runtime:
        raise RemoteError("authorized-search requires explicit trusted-runtime acknowledgement")
    if args.tier == "core" and not args.allow_core:
        raise RemoteError("core remote retrieval is disabled by default")
    identity_name = IDENTITY_ENVS[args.tier]
    identity_value = os.environ.get(identity_name, "")
    identity_path = Path(identity_value).expanduser().resolve() if identity_value else None
    if not identity_path or not identity_path.is_file():
        raise RemoteError(f"{identity_name} must reference an identity file Secret")
    vault_root = args.vault_root.expanduser().resolve()
    if not vault_root.is_dir():
        raise RemoteError("authorized-search requires a controlled local vault checkout")
    product_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(product_root / "src"))
    try:
        from kb_vault import KBError, KnowledgeVault
        from kb_vault.agent import search as search_vault

        vault = KnowledgeVault(vault_root)
        result = search_vault(
            vault,
            args.query,
            authorized=True,
            keep_unlocked=False,
            identities={args.tier: identity_path},
        )
    except (ImportError, KBError) as exc:
        raise RemoteError("authorized search failed safely") from exc
    output_dir = vault_root / ".local" / "authorized-results"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (
        "search-" + hashlib.sha256(f"{args.tier}:{args.query}".encode("utf-8")).hexdigest()[:16] + ".json"
    )
    temporary = output_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return {
        "status": "ok",
        "role": "authorized-read",
        "tier": args.tier,
        "matches": len(result["results"]),
        "result_file": str(output_path),
        "private_index_locked": result["locked_after"],
        "cleanup_required": "run clear-results after the Agent consumes the result",
    }


def clear_authorized_results(args: argparse.Namespace) -> dict[str, Any]:
    product_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(product_root / "src"))
    try:
        from kb_vault import KnowledgeVault

        return KnowledgeVault(args.vault_root).lock()
    except (ImportError, OSError) as exc:
        raise RemoteError("unable to clear authorized results") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone remote knowledge intake")
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect", help="diagnose and configure one remote Agent role")
    add_target_arguments(connect)
    connect.add_argument(
        "--role", choices=("auto", "public-read", "encrypted-write", "authorized-read"), default="auto"
    )
    connect.add_argument("--tier", choices=PRIVATE_TIERS)
    connect.add_argument("--vault-root", type=Path)
    connect.add_argument("--path", default="README.md")
    connect.add_argument("--test-connection", action="store_true")
    connect.add_argument("--acknowledge-trusted-runtime", action="store_true")
    connect.add_argument("--allow-core", action="store_true")
    connect.add_argument("--confirm-content-repository", action="store_true")
    authorized = sub.add_parser(
        "authorized-search", help="search one authorized tier in a trusted runtime without printing results"
    )
    authorized.add_argument("query")
    authorized.add_argument("--tier", choices=PRIVATE_TIERS, required=True)
    authorized.add_argument("--vault-root", type=Path, required=True)
    authorized.add_argument("--acknowledge-trusted-runtime", action="store_true")
    authorized.add_argument("--allow-core", action="store_true")
    clear = sub.add_parser("clear-results", help="remove authorized results and other temporary plaintext")
    clear.add_argument("--vault-root", type=Path, required=True)
    check = sub.add_parser("check", help="verify Token access without returning file content")
    add_target_arguments(check)
    check.add_argument("--path", default="README.md")
    add = sub.add_parser("add", help="append one new remote knowledge event")
    add_target_arguments(add)
    add.add_argument("--request-id", required=True)
    add.add_argument("--agent-id", required=True)
    add.add_argument("--object-id")
    add.add_argument("--tier", choices=("public", "archive", "basic", "advanced", "core"), required=True)
    add.add_argument("--kind", choices=("raw", "wiki", "release"), default="raw")
    add.add_argument("--title", required=True)
    add.add_argument("--summary", default="")
    add.add_argument("--source-uri", default="")
    add.add_argument("--content-file", type=Path, required=True)
    add.add_argument("--human-confirmed", action="store_true")
    add.add_argument(
        "--distribution-channel",
        choices=("local-only", "public-plaintext", "public-ciphertext", "controlled-channel"),
    )
    add.add_argument("--distribution-audience", action="append", default=[])
    add.add_argument("--confirm-remote-write", action="store_true")
    add.add_argument("--confirm-content-repository", action="store_true")
    return parser


def execute(args: argparse.Namespace, client: Client | None = None) -> dict[str, Any]:
    if args.command == "connect":
        return connection_profile(args)
    if args.command == "authorized-search":
        return authorized_search(args)
    if args.command == "clear-results":
        return clear_authorized_results(args)
    if args.command == "add" and not args.confirm_remote_write:
        raise RemoteError("real GitHub write requires --confirm-remote-write")
    _resolve_repository(args)
    if args.command == "add" and not args.confirm_content_repository:
        raise RemoteError("confirm the target is a content repository with --confirm-content-repository")
    active = client or Client(
        owner=args.owner,
        repo=args.repo,
        branch=args.branch,
        require_token=args.command != "check",
    )
    if args.command == "check":
        result = active.get(args.path)
        return {
            "status": "ok",
            "repository": f"{active.owner}/{active.repo}",
            "branch": active.branch,
            "path": args.path,
            "sha": result["sha"],
        }
    event = build_event(args)
    payload = active.create(
        event["payload_path"], event["payload"], f"知识库远端追加：写入 {event['object_id']} 载荷"
    )
    try:
        ready = active.create(
            event["ready_path"], event["ready"], f"知识库远端追加：确认 {event['object_id']} 完成"
        )
    except RemoteError as exc:
        raise RemoteError(
            f"payload was written but READY failed for {event['object_id']}; keep it ignored"
        ) from exc
    return {
        "status": "ok",
        "object_id": event["object_id"],
        "payload_path": event["payload_path"],
        "ready_path": event["ready_path"],
        "payload_commit_sha": payload["commit_sha"],
        "ready_commit_sha": ready["commit_sha"],
    }


def main() -> int:
    try:
        result = execute(build_parser().parse_args())
    except RemoteError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

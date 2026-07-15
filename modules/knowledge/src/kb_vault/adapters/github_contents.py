from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import quote

from kb_vault.core import ConflictError, KBError


Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, dict[str, Any]]]


def _default_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"message": "GitHub API request failed"}
        return exc.code, payload


@dataclass(frozen=True)
class GitHubRequestPlan:
    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any] | None

    def sanitized(self) -> dict[str, Any]:
        headers = {key: value for key, value in self.headers.items() if key.lower() != "authorization"}
        body: dict[str, Any] | None = None
        if self.body is not None:
            body = dict(self.body)
            if "content" in body:
                encoded = str(body["content"])
                body["content"] = f"<base64 omitted; {len(encoded)} chars>"
        return {"method": self.method, "url": self.url, "headers": headers, "body": body}


class GitHubContentsAdapter:
    """Minimal GitHub Contents API adapter. It never accepts age identities."""

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        token: str | None = None,
        api_version: str = "2026-03-10",
        transport: Transport | None = None,
    ):
        if not owner or not repo:
            raise KBError("GitHub owner and repository are required")
        self.owner = owner
        self.repo = repo
        self._token = token or os.environ.get("KB_GITHUB_TOKEN")
        self.api_version = api_version
        self.transport = transport or _default_transport

    @staticmethod
    def _validate_path(path: str) -> str:
        cleaned = path.replace("\\", "/").lstrip("/")
        if not cleaned or any(part in ("", ".", "..") for part in cleaned.split("/")):
            raise KBError("invalid repository path")
        return cleaned

    def _url(self, path: str) -> str:
        cleaned = self._validate_path(path)
        encoded = "/".join(quote(part, safe="") for part in cleaned.split("/"))
        return f"https://api.github.com/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}/contents/{encoded}"

    def _headers(self, *, require_token: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "personal-knowledge-kb-v0.1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        elif require_token:
            raise KBError("GitHub token is not configured")
        return headers

    def plan_put(
        self,
        *,
        path: str,
        content: bytes,
        message: str,
        branch: str | None = None,
        sha: str | None = None,
    ) -> GitHubRequestPlan:
        if not message.strip():
            raise KBError("GitHub commit message is required")
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
        }
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha
        return GitHubRequestPlan(
            method="PUT",
            url=self._url(path),
            headers=self._headers(require_token=False),
            body=body,
        )

    def put_file(
        self,
        *,
        path: str,
        content: bytes,
        message: str,
        branch: str | None = None,
        sha: str | None = None,
    ) -> dict[str, Any]:
        plan = self.plan_put(path=path, content=content, message=message, branch=branch, sha=sha)
        headers = self._headers(require_token=True)
        status, payload = self.transport(
            plan.method,
            plan.url,
            headers,
            json.dumps(plan.body, ensure_ascii=False).encode("utf-8"),
        )
        if status in (409, 422):
            raise ConflictError("GitHub Contents API reported a path or SHA conflict")
        if status not in (200, 201):
            raise KBError(f"GitHub Contents API write failed with status {status}")
        return {
            "status": "ok",
            "http_status": status,
            "path": path,
            "content_sha": payload.get("content", {}).get("sha"),
            "commit_sha": payload.get("commit", {}).get("sha"),
        }

    def get_file(self, *, path: str, ref: str | None = None) -> dict[str, Any]:
        url = self._url(path)
        if ref:
            url += f"?ref={quote(ref, safe='')}"
        status, payload = self.transport("GET", url, self._headers(require_token=True), None)
        if status == 404:
            raise KBError("GitHub object does not exist")
        if status != 200:
            raise KBError(f"GitHub Contents API read failed with status {status}")
        try:
            content = base64.b64decode(payload["content"], validate=True)
        except (KeyError, ValueError) as exc:
            raise KBError("GitHub response content is invalid") from exc
        return {"status": "ok", "path": path, "sha": payload.get("sha"), "content": content}

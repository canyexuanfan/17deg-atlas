from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from .bootstrap import (
    ensure_instance_manifest,
    ensure_personal_domain_manifest,
    is_personal_domain_root,
    resolve_knowledge_root,
)
from .core import KBError


Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Any]]
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
DEFAULT_PERSONAL_DOMAIN_REPOSITORY_NAME = "17deg-personal"
INSTANCE_MANIFEST_PATHS = ("personal.yaml", "config/instance.json")
INSTANCE_PROBE_PATHS = (
    "knowledge/config/tiers.yml",
    "domains/personal/knowledge/config/tiers.yml",
    "config/tiers.yml",
)


def _manifest_matches(
    value: Mapping[str, Any] | None,
    *,
    domain_kind: str,
    module_kind: str,
    subject_id: str,
) -> bool:
    if not value:
        return False
    if value.get("domain_kind") != domain_kind:
        return False
    modules = value.get("modules")
    module_matches = value.get("module_kind") == module_kind or (
        isinstance(modules, list)
        and any(
            isinstance(item, Mapping) and item.get("module_kind") == module_kind
            for item in modules
        )
    )
    if not module_matches:
        return False
    configured_subject = str(value.get("subject_id", "")).strip()
    return not subject_id or not configured_subject or configured_subject == subject_id


def _decode_manifest_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    try:
        raw = base64.b64decode(content.replace("\n", ""), validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _transport(
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
            payload = {"message": "GitHub request failed"}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise KBError("GitHub is unreachable") from exc


def _parse_credentials(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _git_environment(git_path: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if os.name == "nt":
        root = Path(git_path).resolve().parent.parent
        additions = [root / "usr" / "bin", root / "mingw64" / "bin"]
        existing = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(str(path) for path in additions if path.is_dir()) + os.pathsep + existing
    return env


def resolve_github_token(explicit: str | None = None) -> tuple[str, str]:
    if explicit:
        return explicit, "explicit-secret"
    if configured := os.environ.get("KB_GITHUB_TOKEN", "").strip():
        return configured, "runtime-secret"
    git_path = shutil.which("git")
    if not git_path:
        raise KBError("GitHub connection requires Git or a runtime Secret")
    query = b"protocol=https\nhost=github.com\n\n"
    attempts = (
        [git_path, "credential", "fill"],
        [
            git_path,
            "-c",
            "credential.helper=",
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            "credential.https://github.com.helper=store",
            "credential",
            "fill",
        ],
    )
    for command in attempts:
        result = subprocess.run(
            command,
            input=query,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(git_path),
            check=False,
        )
        if result.returncode != 0:
            continue
        values = _parse_credentials(result.stdout.decode("utf-8", errors="replace"))
        if password := values.get("password"):
            return password, "git-credential"
    raise KBError("GitHub is not connected; complete the provider login once and retry")


class GitHubRepositoryClient:
    def __init__(self, token: str, *, transport: Transport | None = None):
        if not token:
            raise KBError("GitHub connection is unavailable")
        self._token = token
        self.transport = transport or _transport

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "17deg-atlas",
        }

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.transport(method, f"https://api.github.com{path}", self._headers(), payload)

    def account(self) -> dict[str, str]:
        status, payload = self._request("GET", "/user")
        login = str(payload.get("login", "")).strip()
        if status != 200 or not login:
            raise KBError("GitHub connection could not identify the current account")
        return {"login": login}

    def repository(self, owner: str, name: str) -> dict[str, Any] | None:
        status, payload = self._request(
            "GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        )
        if status == 404:
            return None
        if status != 200:
            raise KBError("GitHub repository lookup failed")
        return {
            "owner": str(payload.get("owner", {}).get("login", owner)),
            "name": str(payload.get("name", name)),
            "private": bool(payload.get("private", True)),
            "default_branch": str(payload.get("default_branch", "main")),
            "html_url": str(payload.get("html_url", "")),
        }

    def create_repository(self, name: str, *, private: bool) -> dict[str, Any]:
        if not REPOSITORY_RE.fullmatch(name):
            raise KBError("suggested GitHub repository name is invalid")
        status, payload = self._request(
            "POST",
            "/user/repos",
            {"name": name, "private": private, "auto_init": False},
        )
        if status == 422:
            raise KBError("GitHub repository name is unavailable")
        if status != 201:
            raise KBError("GitHub repository creation failed")
        owner = str(payload.get("owner", {}).get("login", "")).strip()
        repo_name = str(payload.get("name", name)).strip()
        if not owner or not repo_name:
            raise KBError("GitHub repository creation returned an invalid result")
        return {
            "owner": owner,
            "name": repo_name,
            "private": bool(payload.get("private", private)),
            "default_branch": str(payload.get("default_branch", "main")),
            "html_url": str(payload.get("html_url", f"https://github.com/{owner}/{repo_name}")),
        }

    def repositories(self) -> list[dict[str, Any]]:
        status, payload = self._request(
            "GET",
            "/user/repos?affiliation=owner,collaborator,organization_member"
            "&per_page=100&sort=updated",
        )
        if status != 200 or not isinstance(payload, list):
            raise KBError("GitHub repository discovery failed")
        values: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            owner = str(item.get("owner", {}).get("login", "")).strip()
            name = str(item.get("name", "")).strip()
            if not owner or not name:
                continue
            values.append(
                {
                    "owner": owner,
                    "name": name,
                    "private": bool(item.get("private", True)),
                    "default_branch": str(item.get("default_branch", "main")),
                    "html_url": str(item.get("html_url", "")),
                }
            )
        return values

    def has_instance_marker(self, owner: str, name: str) -> bool:
        if self.instance_manifest(owner, name) is not None:
            return True
        return self.has_legacy_instance_marker(owner, name)

    def instance_manifest(self, owner: str, name: str) -> dict[str, Any] | None:
        for path in INSTANCE_MANIFEST_PATHS:
            status, payload = self._request(
                "GET",
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
                f"/contents/{quote(path, safe='/')}",
            )
            if status == 404:
                continue
            if status != 200:
                raise KBError("GitHub knowledge repository discovery failed")
            decoded = _decode_manifest_payload(payload)
            if decoded is not None:
                return decoded
        return None

    def has_legacy_instance_marker(self, owner: str, name: str) -> bool:
        for path in INSTANCE_PROBE_PATHS:
            status, _payload = self._request(
                "GET",
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
                f"/contents/{quote(path, safe='/')}",
            )
            if status == 404:
                continue
            if status != 200:
                raise KBError("GitHub knowledge repository discovery failed")
            return True
        return False

    def discover_instances(
        self,
        *,
        domain_kind: str = "personal",
        module_kind: str = "knowledge",
        subject_id: str = "",
    ) -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        for repository in self.repositories():
            owner = repository["owner"]
            name = repository["name"]
            manifest = self.instance_manifest(owner, name)
            if _manifest_matches(
                manifest,
                domain_kind=domain_kind,
                module_kind=module_kind,
                subject_id=subject_id,
            ):
                discovered.append({**repository, "instance": manifest})
                continue
            legacy_subject = f"person:github:{owner}"
            if (
                manifest is None
                and domain_kind == "personal"
                and module_kind == "knowledge"
                and (not subject_id or subject_id == legacy_subject)
                and self.has_legacy_instance_marker(owner, name)
            ):
                discovered.append(repository)
        return discovered


class GitHubCLIRepositoryClient:
    def __init__(self, executable: str, *, runner: Callable[..., Any] = subprocess.run):
        self.executable = executable
        self.runner = runner
        self._account_login = ""

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GH_PAGER"] = "cat"
        env["NO_COLOR"] = "1"
        result = self.runner(
            [self.executable, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        return result

    def _json(self, arguments: list[str], *, missing_ok: bool = False) -> Any:
        result = self._run(arguments)
        if missing_ok and result.returncode != 0 and self._is_not_found(result.stderr):
            return None
        if result.returncode != 0:
            raise KBError("GitHub CLI request failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise KBError("GitHub CLI returned an invalid result") from exc

    @staticmethod
    def _is_not_found(message: str | None) -> bool:
        value = (message or "").lower()
        return any(
            marker in value
            for marker in ("404", "not found", "could not resolve to a repository")
        )

    @staticmethod
    def _repository(value: Mapping[str, Any]) -> dict[str, Any]:
        owner_value = value.get("owner", {})
        owner = (
            str(owner_value.get("login", ""))
            if isinstance(owner_value, Mapping)
            else str(owner_value)
        )
        branch_value = value.get("defaultBranchRef")
        if isinstance(branch_value, Mapping):
            branch = str(branch_value.get("name", "main"))
        else:
            branch = str(value.get("defaultBranch", "main"))
        return {
            "owner": owner,
            "name": str(value.get("name", "")),
            "private": bool(value.get("isPrivate", value.get("private", True))),
            "default_branch": branch or "main",
            "html_url": str(value.get("url", value.get("html_url", ""))),
        }

    def account(self) -> dict[str, str]:
        if self._account_login:
            return {"login": self._account_login}
        payload = self._json(["api", "user"])
        login = str(payload.get("login", "")).strip() if isinstance(payload, dict) else ""
        if not login:
            raise KBError("GitHub connection could not identify the current account")
        self._account_login = login
        return {"login": login}

    def repository(self, owner: str, name: str) -> dict[str, Any] | None:
        payload = self._json(
            [
                "repo",
                "view",
                f"{owner}/{name}",
                "--json",
                "name,owner,isPrivate,defaultBranchRef,url",
            ],
            missing_ok=True,
        )
        if not isinstance(payload, dict):
            return None
        return self._repository(payload)

    def repositories(self) -> list[dict[str, Any]]:
        login = self.account()["login"]
        payload = self._json(
            [
                "repo",
                "list",
                login,
                "--limit",
                "100",
                "--json",
                "name,owner,isPrivate,defaultBranchRef,url",
            ]
        )
        if not isinstance(payload, list):
            raise KBError("GitHub repository discovery failed")
        return [self._repository(item) for item in payload if isinstance(item, dict)]

    def has_instance_marker(self, owner: str, name: str) -> bool:
        if self.instance_manifest(owner, name) is not None:
            return True
        return self.has_legacy_instance_marker(owner, name)

    def instance_manifest(self, owner: str, name: str) -> dict[str, Any] | None:
        for path in INSTANCE_MANIFEST_PATHS:
            result = self._run(
                [
                    "api",
                    "-H",
                    "Accept: application/vnd.github.raw+json",
                    f"repos/{owner}/{name}/contents/{path}",
                ]
            )
            if result.returncode != 0 and self._is_not_found(result.stderr):
                continue
            if result.returncode != 0:
                raise KBError("GitHub knowledge repository discovery failed")
            try:
                value = json.loads(result.stdout)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                return dict(value)
        return None

    def has_legacy_instance_marker(self, owner: str, name: str) -> bool:
        for path in INSTANCE_PROBE_PATHS:
            result = self._run(["api", f"repos/{owner}/{name}/contents/{path}"])
            if result.returncode != 0 and self._is_not_found(result.stderr):
                continue
            if result.returncode != 0:
                raise KBError("GitHub knowledge repository discovery failed")
            return True
        return False

    def discover_instances(
        self,
        *,
        domain_kind: str = "personal",
        module_kind: str = "knowledge",
        subject_id: str = "",
    ) -> list[dict[str, Any]]:
        owner = self.account()["login"]
        query = """
query($login: String!) {
  user(login: $login) {
    repositories(first: 100, ownerAffiliations: OWNER, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        name
        isPrivate
        url
        defaultBranchRef { name }
        personal: object(expression: "HEAD:personal.yaml") { ... on Blob { text } }
        instance: object(expression: "HEAD:config/instance.json") { ... on Blob { text } }
        knowledge: object(expression: "HEAD:knowledge/config/tiers.yml") { id }
        deepKnowledge: object(expression: "HEAD:domains/personal/knowledge/config/tiers.yml") { id }
        legacyKnowledge: object(expression: "HEAD:config/tiers.yml") { id }
      }
    }
  }
}
""".strip()
        payload = self._json(
            ["api", "graphql", "-f", f"query={query}", "-F", f"login={owner}"]
        )
        try:
            nodes = payload["data"]["user"]["repositories"]["nodes"]
        except (KeyError, TypeError):
            raise KBError("GitHub knowledge repository discovery failed")
        if not isinstance(nodes, list):
            raise KBError("GitHub knowledge repository discovery failed")
        discovered: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            repository = self._repository({**node, "owner": owner})
            manifest = None
            for key in ("personal", "instance"):
                blob = node.get(key)
                text = blob.get("text") if isinstance(blob, Mapping) else None
                if not isinstance(text, str):
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, Mapping):
                    manifest = dict(value)
                    break
            if _manifest_matches(
                manifest,
                domain_kind=domain_kind,
                module_kind=module_kind,
                subject_id=subject_id,
            ):
                discovered.append({**repository, "instance": manifest})
                continue
            legacy_subject = f"person:github:{owner}"
            legacy_marker = any(
                isinstance(node.get(key), Mapping)
                for key in ("knowledge", "deepKnowledge", "legacyKnowledge")
            )
            if (
                manifest is None
                and domain_kind == "personal"
                and module_kind == "knowledge"
                and (not subject_id or subject_id == legacy_subject)
                and legacy_marker
            ):
                discovered.append(repository)
        return discovered

    def create_repository(self, name: str, *, private: bool) -> dict[str, Any]:
        if not REPOSITORY_RE.fullmatch(name):
            raise KBError("suggested GitHub repository name is invalid")
        visibility = "--private" if private else "--public"
        result = self._run(
            ["repo", "create", name, visibility, "--disable-issues", "--disable-wiki"]
        )
        if result.returncode != 0:
            raise KBError("GitHub repository creation failed")
        login = self.account()["login"]
        repository = self.repository(login, name)
        if repository is None:
            raise KBError("GitHub repository creation could not be verified")
        return repository

    def clone_repository(self, owner: str, name: str, target: Path) -> None:
        result = self._run(["repo", "clone", f"{owner}/{name}", str(target)])
        if result.returncode != 0:
            raise KBError("GitHub knowledge repository clone failed")


class GitHubCLIEnvironment:
    def __init__(
        self,
        *,
        executable: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., Any] = subprocess.run,
        common_paths: list[Path] | None = None,
        package_manager_paths: Mapping[str, list[Path]] | None = None,
    ):
        self._executable = executable
        self.which = which
        self.runner = runner
        self.common_paths = common_paths
        self.package_manager_paths = package_manager_paths

    @staticmethod
    def _existing_path(candidates: list[Path]) -> str | None:
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
        return None

    def command(self, name: str) -> str | None:
        if resolved := self.which(name):
            return resolved
        configured = self.package_manager_paths or {}
        candidates = list(configured.get(name, []))
        if not candidates and platform.system().lower() == "windows" and name == "winget":
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Microsoft"
                / "WindowsApps"
                / "winget.exe",
            ]
        return self._existing_path(candidates)

    @property
    def executable(self) -> str | None:
        resolved = self._executable or self.which("gh")
        if resolved:
            return resolved
        if platform.system().lower() == "windows":
            candidates = self.common_paths
            if candidates is None:
                candidates = [
                    Path(os.environ.get("ProgramFiles", ""))
                    / "GitHub CLI"
                    / "gh.exe",
                    Path(os.environ.get("LOCALAPPDATA", ""))
                    / "Programs"
                    / "GitHub CLI"
                    / "gh.exe",
                ]
            if resolved_candidate := self._existing_path(candidates):
                return resolved_candidate
        return None

    def installation(self) -> dict[str, Any]:
        if self.executable:
            return {"required": False, "manager": "existing", "command": []}
        system = platform.system().lower()
        candidates: list[tuple[str, list[str]]] = []
        if system == "windows":
            if winget := self.command("winget"):
                candidates.append(
                    (
                        "winget",
                        [
                            winget,
                            "install",
                            "--id",
                            "GitHub.cli",
                            "--exact",
                            "--accept-package-agreements",
                            "--accept-source-agreements",
                        ],
                    )
                )
        elif system == "darwin":
            if brew := self.command("brew"):
                candidates.append(("brew", [brew, "install", "gh"]))
        else:
            for manager in ("brew", "apt-get", "dnf"):
                if executable := self.command(manager):
                    arguments = [executable, "install"]
                    if manager in ("apt-get", "dnf"):
                        arguments.append("-y")
                    arguments.append("gh")
                    candidates.append((manager, arguments))
        for manager, command in candidates:
            return {"required": True, "manager": manager, "command": command}
        return {"required": True, "manager": "unavailable", "command": []}

    def install(self, *, confirm: bool = False) -> str:
        plan = self.installation()
        if not plan["required"]:
            return str(self.executable)
        if not plan["command"]:
            raise KBError("GitHub CLI requires a supported package manager")
        if not confirm:
            raise KBError("GitHub CLI installation requires confirmation")
        result = self.runner(plan["command"], check=False)
        if result.returncode != 0:
            raise KBError("GitHub CLI installation failed")
        resolved = self.executable
        if not resolved:
            raise KBError("GitHub CLI installation could not be verified")
        self._executable = resolved
        return resolved

    def authenticated(self) -> bool:
        executable = self.executable
        if not executable:
            return False
        result = self.runner(
            [executable, "auth", "status", "--hostname", "github.com"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode == 0

    def authenticate(self, *, confirm: bool = False) -> None:
        executable = self.executable
        if not executable:
            raise KBError("GitHub CLI is unavailable")
        if not self.authenticated():
            if not confirm:
                raise KBError("GitHub browser authorization requires confirmation")
            login = self.runner(
                [
                    executable,
                    "auth",
                    "login",
                    "--hostname",
                    "github.com",
                    "--git-protocol",
                    "https",
                    "--web",
                ],
                check=False,
            )
            if login.returncode != 0 or not self.authenticated():
                raise KBError("GitHub browser authorization did not finish")
        configured = self.runner(
            [executable, "auth", "setup-git", "--hostname", "github.com"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if configured.returncode != 0:
            raise KBError("GitHub Git authentication setup failed")

    def client(self) -> GitHubCLIRepositoryClient:
        executable = self.executable
        if not executable or not self.authenticated():
            raise KBError("GitHub is not connected")
        return GitHubCLIRepositoryClient(executable, runner=self.runner)

    def plan(self) -> dict[str, Any]:
        install = self.installation()
        installed = not install["required"]
        environment_identity = bool(os.environ.get("GH_TOKEN", "").strip())
        authenticated = self.authenticated() if installed else environment_identity
        confirmations: list[str] = []
        if not installed:
            confirmations.append("install-github-cli")
        if not authenticated:
            confirmations.append("authorize-github-account")
        return {
            "installed": installed,
            "authenticated": authenticated,
            "package_manager": install["manager"],
            "confirmation_required": confirmations,
        }


def suggested_repository_name(workspace: Path) -> str:
    del workspace
    return DEFAULT_PERSONAL_DOMAIN_REPOSITORY_NAME


def configured_repository(workspace: Path) -> dict[str, str] | None:
    manifest_path = workspace / "config" / "instance.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = None
        repository = manifest.get("repository") if isinstance(manifest, dict) else None
        if isinstance(repository, dict):
            owner = str(repository.get("owner", "")).strip()
            repo = str(repository.get("name", "")).strip()
            branch = str(repository.get("branch", "main")).strip() or "main"
            if owner and repo:
                return {"owner": owner, "repo": repo, "branch": branch}
    path = workspace / "config" / "projection.yml"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    policy = value.get("remote_policy")
    if not isinstance(policy, dict):
        return None
    owner = str(policy.get("allowed_owner", "")).strip()
    repo = str(policy.get("allowed_repo", "")).strip()
    branch = str(policy.get("allowed_branch", "main")).strip() or "main"
    if not owner or not repo:
        return None
    return {"owner": owner, "repo": repo, "branch": branch}


def bind_repository(
    workspace: Path,
    *,
    owner: str,
    repo: str,
    branch: str = "main",
    visibility: str = "unknown",
    subject_id: str = "",
) -> None:
    domain_layout = is_personal_domain_root(workspace)
    manifest = (
        ensure_personal_domain_manifest(workspace)
        if domain_layout
        else ensure_instance_manifest(workspace)
    )
    expected_subject = subject_id or f"person:github:{owner}"
    current_subject = str(manifest.get("subject_id", "")).strip()
    if current_subject and current_subject != expected_subject:
        raise KBError("knowledge instance belongs to another subject")
    manifest["subject_id"] = expected_subject
    manifest["repository"] = {
        "owner": owner,
        "name": repo,
        "branch": branch,
        "visibility": visibility,
    }
    manifest_path = workspace / "config" / "instance.json"
    manifest_temporary = manifest_path.with_suffix(".tmp")
    manifest_temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_temporary.replace(manifest_path)
    knowledge_root = resolve_knowledge_root(workspace)
    path = knowledge_root / "config" / "projection.yml"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KBError("knowledge repository configuration is unavailable") from exc
    value["repo_name"] = repo
    value["remote_policy"] = {
        "allowed_owner": owner,
        "allowed_repo": repo,
        "allowed_branch": branch,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def bind_git_remote(workspace: Path, repository_url: str) -> bool:
    if not (workspace / ".git").is_dir():
        return False
    git_path = shutil.which("git")
    if not git_path:
        raise KBError("Git is unavailable")
    env = _git_environment(git_path)
    git = [
        git_path,
        "-c",
        f"core.excludesFile={workspace / '.gitignore'}",
    ]
    current = subprocess.run(
        [*git, "remote", "get-url", "origin"],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if current.returncode == 0:
        existing = current.stdout.strip().removesuffix(".git")
        expected = repository_url.strip().removesuffix(".git")
        if existing != expected:
            raise KBError("workspace Git origin already points to another repository")
        return True
    added = subprocess.run(
        [*git, "remote", "add", "origin", repository_url],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if added.returncode != 0:
        raise KBError("unable to connect the workspace Git repository to GitHub")
    return True


def initial_git_sync(
    workspace: Path,
    *,
    account: str,
    branch: str = "main",
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, bool]:
    if not (workspace / ".git").is_dir():
        raise KBError("knowledge workspace Git repository is unavailable")
    git_path = shutil.which("git")
    if not git_path:
        raise KBError("Git is unavailable")
    env = _git_environment(git_path)
    git = [
        git_path,
        "-c",
        f"core.excludesFile={workspace / '.gitignore'}",
    ]

    def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return runner(
            [*git, *arguments],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )

    for key, value in (
        ("user.name", account),
        ("user.email", f"{account}@users.noreply.github.com"),
    ):
        configured = run(["config", "--local", key, value])
        if configured.returncode != 0:
            raise KBError("knowledge workspace Git identity setup failed")
    added = run(["add", "--all"])
    if added.returncode != 0:
        raise KBError("knowledge workspace staging failed")
    staged = run(["diff", "--cached", "--quiet"])
    committed = staged.returncode == 1
    if staged.returncode not in (0, 1):
        raise KBError("knowledge workspace change check failed")
    if committed:
        commit = run(["commit", "-m", "初始化个人域知识模块"])
        if commit.returncode != 0:
            raise KBError("knowledge workspace initial commit failed")
    pushed = run(["push", "--set-upstream", "origin", f"HEAD:{branch}"])
    if pushed.returncode != 0:
        raise KBError("knowledge workspace initial sync failed; the local commit is retained for retry")
    return {"committed": committed, "pushed": True}

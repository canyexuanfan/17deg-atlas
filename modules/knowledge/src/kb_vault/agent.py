from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from .bootstrap import (
    KNOWLEDGE_MODULE_RELATIVE,
    ensure_instance_manifest,
    ensure_personal_domain_manifest,
    initialize_instance,
    initialize_personal_domain,
    is_knowledge_module_root,
    is_personal_domain_root,
    resolve_knowledge_root,
)
from .core import KBError, KnowledgeVault
from .dependencies import (
    LocalDependencyEnvironment,
    dependency_status,
    discover_age_executable,
)
from .github_onboarding import (
    DEFAULT_PERSONAL_DOMAIN_REPOSITORY_NAME,
    GitHubCLIEnvironment,
    GitHubRepositoryClient,
    bind_git_remote,
    bind_repository,
    configured_repository,
    initial_git_sync,
    resolve_github_token,
    suggested_repository_name,
)
from .registry import register_instance, registered_instances


PRIVATE_TIERS = ("basic", "advanced", "core")
IDENTITY_ENVS = {
    "basic": "KB_AGE_IDENTITY_BASIC_FILE",
    "advanced": "KB_AGE_IDENTITY_ADVANCED_FILE",
    "core": "KB_AGE_IDENTITY_CORE_FILE",
}
INSTANCE_MARKERS = ("config/tiers.yml", "config/policies.yml", "manifests/projection-selection.json")


def _request_id(action: str) -> str:
    return f"agent-{action}-{uuid.uuid4().hex}"


def discover_identities(root: str | Path, *, include_test: bool = True) -> dict[str, Path]:
    root_path = Path(root).resolve()
    found: dict[str, Path] = {}
    for tier, env_name in IDENTITY_ENVS.items():
        configured = os.environ.get(env_name, "").strip()
        candidate = Path(configured).expanduser().resolve() if configured else None
        if candidate and candidate.is_file():
            found[tier] = candidate
            continue
        test_identity = root_path / ".local" / "test-keys" / f"{tier}.identity"
        if include_test and test_identity.is_file():
            found[tier] = test_identity
    return found


def resolve_workspace(target: str | Path | None = None) -> Path:
    selected = target or os.environ.get("KB_INSTANCE_ROOT")
    if selected:
        return Path(selected).expanduser().resolve()
    current = Path.cwd().resolve()
    if is_personal_domain_root(current) or is_knowledge_module_root(current):
        return current
    registered = registered_instances()
    if len(registered) == 1:
        return Path(registered[0]["root"])
    meaningful = [
        entry for entry in current.iterdir() if entry.name not in (".git", ".17deg-atlas")
    ]
    return current if not meaningful else current / DEFAULT_PERSONAL_DOMAIN_REPOSITORY_NAME


def detect_agent_runtime(value: str = "auto") -> str:
    if value not in ("auto", "local", "remote"):
        raise KBError("Agent runtime must be auto, local, or remote")
    entry_runtime = os.environ.get("ATLAS_ENTRY_RUNTIME", "").strip().lower()
    if entry_runtime and entry_runtime not in ("local", "remote"):
        raise KBError("the installed Atlas entry has an invalid runtime role")
    if entry_runtime:
        if value != "auto" and value != entry_runtime:
            raise KBError("the requested runtime conflicts with the installed Atlas entry")
        return entry_runtime
    if value != "auto":
        return value
    configured = os.environ.get("KB_AGENT_RUNTIME", "").strip().lower()
    if configured in ("local", "remote"):
        return configured
    remote_markers = ("CI", "CODESPACES", "GITHUB_ACTIONS", "REMOTE_CONTAINERS")
    if any(os.environ.get(name) for name in remote_markers):
        return "remote"
    raise KBError(
        "Agent runtime is not explicit; use the local or remote Atlas entry"
    )


def workspace_state(target: str | Path | None = None) -> dict[str, Any]:
    root = resolve_workspace(target)
    layout_kind = "none"
    if not root.exists():
        state = "new-path"
    elif not root.is_dir():
        state = "invalid-path"
    elif is_personal_domain_root(root):
        state = "existing-instance"
        layout_kind = "domain-root"
    elif is_knowledge_module_root(root):
        state = "existing-instance"
        layout_kind = "module-root"
    else:
        meaningful = [
            entry for entry in root.iterdir() if entry.name not in (".git", ".17deg-atlas")
        ]
        state = "empty-directory" if not meaningful else "occupied-directory"
    knowledge_root = None
    if state == "existing-instance":
        knowledge_root = str(resolve_knowledge_root(root))
    return {
        "root": str(root),
        "state": state,
        "layout_kind": layout_kind,
        "knowledge_root": knowledge_root,
        "is_instance": state == "existing-instance",
        "safe_to_create": state in ("new-path", "empty-directory"),
    }


def local_plan(
    target: str | Path | None = None,
    *,
    mode: str = "test",
    age_path: str | Path | None = None,
) -> dict[str, Any]:
    if mode not in ("test", "production"):
        raise KBError("local Agent mode must be test or production")
    workspace = workspace_state(target)
    action = {
        "existing-instance": "connect-existing",
        "new-path": "create-new",
        "empty-directory": "create-current-directory",
        "occupied-directory": "confirm-before-creating-in-current-directory",
        "invalid-path": "choose-directory",
    }[workspace["state"]]
    confirmations: list[str] = []
    registry_choices = (
        registered_instances()
        if target is None and not os.environ.get("KB_INSTANCE_ROOT")
        else []
    )
    if len(registry_choices) > 1:
        confirmations.append("select-local-knowledge-instance")
    if workspace["state"] == "occupied-directory":
        confirmations.append("create-inside-nonempty-current-directory")
    if workspace["state"] == "invalid-path":
        confirmations.append("choose-valid-instance-directory")
    if mode == "production":
        confirmations.extend(
            ["production-key-location", "production-key-backup", "production-key-use"]
        )
    dependencies = dependency_status(age_path)
    automatic_steps = ["check-runtime"]
    if workspace["is_instance"]:
        automatic_steps.extend(["connect-existing-instance", "verify-lock-and-recovery"])
    else:
        automatic_steps.extend(
            [
                "create-independent-instance",
                "initialize-local-git",
                "run-five-tier-synthetic-test",
                "verify-lock-and-recovery",
            ]
        )
    return {
        "status": "needs-confirmation"
        if confirmations
        else "blocked" if dependencies["missing"] else "ready",
        "flow": "local-agent",
        "domain_kind": "personal",
        "module_kind": "knowledge",
        "target": workspace["root"],
        "workspace_state": workspace["state"],
        "action": action,
        "mode": mode,
        "automatic_steps": automatic_steps,
        "dependencies": dependencies,
        "confirmation_required": confirmations,
        "credentials_required": [] if mode == "test" else [
            "KB_AGE_RECIPIENT_BASIC",
            "KB_AGE_RECIPIENT_ADVANCED",
            "KB_AGE_RECIPIENT_CORE",
        ],
        "registered_instance_choices": [value["root"] for value in registry_choices]
        if len(registry_choices) > 1
        else [],
    }


def _github_client(
    client: GitHubRepositoryClient | None = None, *, token: str | None = None
) -> GitHubRepositoryClient:
    if client is not None:
        return client
    resolved, _source = resolve_github_token(token)
    return GitHubRepositoryClient(resolved)


def github_bootstrap_plan(
    *, cli_environment: GitHubCLIEnvironment | None = None
) -> dict[str, Any]:
    environment = cli_environment or GitHubCLIEnvironment()
    result = environment.plan()
    return {
        "status": "needs-confirmation" if result["confirmation_required"] else "ready",
        "flow": "github-bootstrap",
        **result,
    }


def _unique_default_repository(active: Any, owner: str) -> tuple[str, dict[str, Any] | None]:
    for index in range(1, 101):
        base = DEFAULT_PERSONAL_DOMAIN_REPOSITORY_NAME
        candidate = base if index == 1 else f"{base}-{index}"
        existing = active.repository(owner, candidate)
        if existing is None:
            return candidate, None
        manifest_method = getattr(active, "instance_manifest", None)
        if callable(manifest_method):
            manifest = manifest_method(owner, candidate)
            if manifest is not None:
                modules = manifest.get("modules")
                has_knowledge = manifest.get("module_kind") == "knowledge" or (
                    isinstance(modules, list)
                    and any(
                        isinstance(item, dict) and item.get("module_kind") == "knowledge"
                        for item in modules
                    )
                )
                if (
                    manifest.get("domain_kind") == "personal"
                    and has_knowledge
                    and str(manifest.get("subject_id", "")).strip()
                    in ("", f"person:github:{owner}")
                ):
                    return candidate, existing
                continue
        has_marker = getattr(active, "has_instance_marker", None)
        if callable(has_marker) and has_marker(owner, candidate):
            return candidate, existing
    raise KBError("GitHub knowledge repository name could not be selected")


def github_first_plan(
    target: str | Path | None = None,
    *,
    runtime: str = "auto",
    repository_name: str | None = None,
    visibility: str = "private",
    mode: str = "test",
    age_path: str | Path | None = None,
    token: str | None = None,
    client: GitHubRepositoryClient | None = None,
    cli_environment: GitHubCLIEnvironment | None = None,
    dependency_environment: LocalDependencyEnvironment | None = None,
) -> dict[str, Any]:
    if visibility not in ("private", "public"):
        raise KBError("GitHub visibility must be private or public")
    workspace = workspace_state(target)
    if workspace["state"] == "invalid-path":
        raise KBError("the selected workspace is not a directory")
    local = local_plan(workspace["root"], mode=mode, age_path=age_path)
    dependency_installation = {"required": False, "manager": "existing", "command": []}
    dependency_confirmations: list[str] = []
    if dependency_environment is not None or (client is None and token is None):
        dependencies = dependency_environment or LocalDependencyEnvironment()
        dependency_installation = dependencies.age_installation(age_path)
        if dependency_installation["required"] and dependency_installation["command"]:
            dependency_confirmations.append("install-age")
    if client is None and token is None:
        bootstrap = github_bootstrap_plan(cli_environment=cli_environment)
        if bootstrap["confirmation_required"]:
            confirmations = [*dependency_confirmations, *bootstrap["confirmation_required"]]
            return {
                "status": "needs-confirmation",
                "flow": "agent-onboarding",
                "runtime": detect_agent_runtime(runtime),
                "domain_kind": "personal",
                "module_kind": "knowledge",
                "subject_id": None,
                "workspace": workspace["root"],
                "workspace_state": workspace["state"],
                "repository": None,
                "repository_action": "prepare-github-connection",
                "repository_visibility": visibility,
                "confirmation_required": confirmations,
                "github": bootstrap,
                "dependency_installation": {
                    "age": {
                        "required": dependency_installation["required"],
                        "manager": dependency_installation["manager"],
                    }
                },
                "local_plan": local,
            }
        environment = cli_environment or GitHubCLIEnvironment()
        active = environment.client()
    else:
        active = _github_client(client, token=token)
    account = active.account()
    remembered = configured_repository(Path(workspace["root"]))
    if remembered:
        owner = remembered["owner"]
        repo = remembered["repo"]
        branch = remembered["branch"]
        existing = active.repository(owner, repo)
        action = "connect-remembered-repository" if existing else "recreate-remembered-repository"
        repository_confirmations = [] if existing else ["create-github-repository"]
    elif repository_name:
        if "/" in repository_name:
            owner, repo = repository_name.split("/", 1)
        else:
            owner = account["login"]
            repo = repository_name
        branch = "main"
        existing = active.repository(owner, repo)
        if existing is None and owner != account["login"]:
            raise KBError("a new knowledge repository must use the connected personal account")
        action = "connect-existing-repository" if existing else "create-github-repository"
        repository_confirmations = [
            "connect-existing-repository" if existing else "create-github-repository"
        ]
    else:
        discovered_method = getattr(active, "discover_instances", None)
        discovered = (
            discovered_method(
                domain_kind="personal",
                module_kind="knowledge",
                subject_id=f"person:github:{account['login']}",
            )
            if callable(discovered_method)
            else []
        )
        if len(discovered) > 1:
            confirmations = [*dependency_confirmations, *local["confirmation_required"]]
            confirmations.append("select-knowledge-repository")
            return {
                "status": "needs-confirmation",
                "flow": "agent-onboarding",
                "runtime": detect_agent_runtime(runtime),
                "domain_kind": "personal",
                "module_kind": "knowledge",
                "subject_id": f"person:github:{account['login']}",
                "workspace": workspace["root"],
                "workspace_state": workspace["state"],
                "repository": None,
                "repository_action": "select-discovered-repository",
                "repository_choices": [
                    f"{value['owner']}/{value['name']}" for value in discovered
                ],
                "repository_visibility": visibility,
                "confirmation_required": confirmations,
                "dependency_installation": {
                    "age": {
                        "required": dependency_installation["required"],
                        "manager": dependency_installation["manager"],
                    }
                },
                "local_plan": local,
            }
        if discovered:
            existing = discovered[0]
            owner = existing["owner"]
            repo = existing["name"]
            branch = existing["default_branch"]
            action = "connect-discovered-repository"
            repository_confirmations = []
        else:
            owner = account["login"]
            repo, existing = _unique_default_repository(active, owner)
            branch = "main" if existing is None else existing["default_branch"]
            if existing is None:
                action = "create-github-repository"
                repository_confirmations = ["create-github-repository"]
            else:
                action = "connect-discovered-repository"
                repository_confirmations = []
    confirmations = [*dependency_confirmations, *local["confirmation_required"]]
    confirmations.extend(repository_confirmations)
    return {
        "status": "needs-confirmation"
        if confirmations
        else "blocked" if local["status"] == "blocked" else "ready",
        "flow": "agent-onboarding",
        "runtime": detect_agent_runtime(runtime),
        "domain_kind": "personal",
        "module_kind": "knowledge",
        "subject_id": f"person:github:{account['login']}",
        "workspace": workspace["root"],
        "workspace_state": workspace["state"],
        "repository": f"{owner}/{repo}",
        "repository_url": f"https://github.com/{owner}/{repo}",
        "repository_visibility": visibility if not existing else ("private" if existing["private"] else "public"),
        "repository_action": action,
        "branch": branch if not existing else existing["default_branch"],
        "confirmation_required": confirmations,
        "local_plan": local,
        "dependency_installation": {
            "age": {
                "required": dependency_installation["required"],
                "manager": dependency_installation["manager"],
            }
        },
    }


def github_first_setup(
    target: str | Path | None = None,
    *,
    runtime: str = "auto",
    repository_name: str | None = None,
    visibility: str = "private",
    mode: str = "test",
    age_path: str | Path | None = None,
    initialize_git: bool = True,
    run_self_test: bool = True,
    confirm_repository_create: bool = False,
    confirm_existing_repository: bool = False,
    confirm_production_key_use: bool = False,
    confirm_nonempty_directory: bool = False,
    confirm_github_cli_install: bool = False,
    confirm_github_login: bool = False,
    confirm_age_install: bool = False,
    confirm_initial_sync: bool = False,
    run_initial_sync: bool = True,
    token: str | None = None,
    client: GitHubRepositoryClient | None = None,
    cli_environment: GitHubCLIEnvironment | None = None,
    dependency_environment: LocalDependencyEnvironment | None = None,
    syncer: Any = initial_git_sync,
) -> dict[str, Any]:
    dependencies = dependency_environment or LocalDependencyEnvironment()
    resolved_dependency_age = dependencies.install_age(
        age_path=age_path,
        confirm=confirm_age_install,
    )
    if client is None and token is None:
        environment = cli_environment or GitHubCLIEnvironment()
        environment.install(confirm=confirm_github_cli_install)
        environment.authenticate(confirm=confirm_github_login)
        active = environment.client()
    else:
        active = _github_client(client, token=token)
    plan = github_first_plan(
        target,
        runtime=runtime,
        repository_name=repository_name,
        visibility=visibility,
        mode=mode,
        age_path=resolved_dependency_age,
        client=active,
        dependency_environment=dependencies,
    )
    if plan["repository"] is None:
        raise KBError("choose one discovered knowledge repository before setup")
    if "create-github-repository" in plan["confirmation_required"] and not confirm_repository_create:
        raise KBError("GitHub repository creation requires confirmation")
    if "connect-existing-repository" in plan["confirmation_required"] and not confirm_existing_repository:
        raise KBError("connecting an existing GitHub repository requires confirmation")
    if "create-inside-nonempty-current-directory" in plan["confirmation_required"] and not confirm_nonempty_directory:
        raise KBError("current directory is not empty; confirmation required")
    if "select-local-knowledge-instance" in plan["confirmation_required"]:
        raise KBError("choose one registered local knowledge instance before setup")
    if mode == "production" and not confirm_production_key_use:
        raise KBError("production setup requires explicit production key confirmation")
    if run_initial_sync and not confirm_initial_sync:
        raise KBError("initial GitHub sync requires confirmation")
    resolved_age = _preflight(resolved_dependency_age, require_git=initialize_git)
    owner, repo = plan["repository"].split("/", 1)
    created = False
    cloned = False
    repository = active.repository(owner, repo)
    if repository is None:
        repository = active.create_repository(repo, private=visibility == "private")
        created = True
    elif plan["workspace_state"] in ("new-path", "empty-directory"):
        clone = getattr(active, "clone_repository", None)
        if not callable(clone):
            raise KBError("existing knowledge repository requires GitHub CLI clone support")
        clone(owner, repo, Path(plan["workspace"]))
        cloned = True
    try:
        local = local_setup(
            plan["workspace"],
            mode=mode,
            age_path=resolved_age,
            initialize_git=initialize_git,
            run_self_test=run_self_test,
            confirm_production_key_use=confirm_production_key_use,
            confirm_nonempty_directory=confirm_nonempty_directory,
        )
        root = Path(local["root"])
        knowledge_root = Path(local["knowledge_root"])
        bind_repository(
            root,
            owner=repository["owner"],
            repo=repository["name"],
            branch=repository["default_branch"],
            visibility="private" if repository["private"] else "public",
            subject_id=plan["subject_id"],
        )
        manifest = (
            ensure_personal_domain_manifest(root, subject_id=plan["subject_id"])
            if local["layout_kind"] == "domain-root"
            else ensure_instance_manifest(root, subject_id=plan["subject_id"])
        )
        instance_registered = register_instance(root, manifest)
        git_remote_bound = bind_git_remote(root, repository["html_url"])
        from .atlas import ContentProjection

        projection = ContentProjection(KnowledgeVault(knowledge_root, age_path=resolved_age))
        projection.assert_remote_target(
            owner=repository["owner"],
            repo=repository["name"],
            branch=repository["default_branch"],
        )
        sync = (
            syncer(
                root,
                account=repository["owner"],
                branch=repository["default_branch"],
            )
            if run_initial_sync
            else {"committed": False, "pushed": False}
        )
    except KBError as exc:
        if created:
            raise KBError("GitHub repository was created, but workspace setup did not finish") from exc
        raise
    return {
        "status": "ok",
        "flow": "agent-onboarding",
        "runtime": plan["runtime"],
        "domain_kind": plan["domain_kind"],
        "module_kind": plan["module_kind"],
        "subject_id": plan["subject_id"],
        "workspace": local["root"],
        "workspace_action": local["action"],
        "repository": f"{repository['owner']}/{repository['name']}",
        "repository_url": repository["html_url"],
        "repository_created": created,
        "repository_cloned": cloned,
        "repository_private": repository["private"],
        "branch": repository["default_branch"],
        "git_remote_bound": git_remote_bound,
        "instance_registered": instance_registered,
        "initial_sync": sync,
        "verified": local["verification"]["ok"],
        "next_prompts": local["next_prompts"],
    }


def _run_git_init(root: Path) -> bool:
    if (root / ".git").exists():
        return False
    if not shutil.which("git"):
        raise KBError("git is unavailable")
    empty_config = root / ".local" / "cache" / "empty-gitconfig"
    empty_config.parent.mkdir(parents=True, exist_ok=True)
    empty_config.touch(exist_ok=True)
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    result = subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise KBError("unable to initialize the instance Git repository")
    return True


def _preflight(age_path: str | Path | None, *, require_git: bool) -> Path:
    resolved_age = discover_age_executable(age_path)
    if not resolved_age:
        raise KBError("age is required before local Agent setup")
    suffix = ".exe" if os.name == "nt" else ""
    configured_keygen = os.environ.get("KB_AGE_KEYGEN_PATH", "")
    sibling_keygen = resolved_age.with_name(f"age-keygen{suffix}")
    discovered_keygen = Path(configured_keygen).expanduser().resolve() if configured_keygen else None
    if not (
        (discovered_keygen and discovered_keygen.is_file())
        or sibling_keygen.is_file()
        or shutil.which("age-keygen")
    ):
        raise KBError("age-keygen is required before local Agent setup")
    if require_git and not shutil.which("git"):
        raise KBError("git is required before local Agent setup")
    return resolved_age


def _synthetic_recovery_test(age_path: str | Path | None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="atlas-agent-test-") as temp:
        base = Path(temp)
        root = base / "scenario"
        initialize_instance(root)
        vault = KnowledgeVault(root, age_path=age_path)
        recipients = vault.generate_test_keys(force=True)
        identities = discover_identities(root)
        object_ids: dict[str, str] = {}
        for tier in ("public", "archive", *PRIVATE_TIERS):
            receipt = vault.add(
                request_id=_request_id(f"self-test-{tier}"),
                tier=tier,
                kind="raw",
                title=f"Synthetic {tier}",
                summary=f"atlas-self-test-{tier}",
                content=f"synthetic content for {tier}",
                catalog_visibility="public" if tier == "public" else "none",
                human_confirmed=tier in ("public", "archive"),
                recipients=recipients,
            )
            object_ids[tier] = receipt["object_id"]
        unlocked = vault.unlock_index(identities)
        searchable = all(vault.search(f"atlas-self-test-{tier}") for tier in ("public", *PRIVATE_TIERS))
        wrong_key_rejected = False
        try:
            vault.get(object_ids["basic"], identities={"basic": identities["core"]})
        except KBError:
            wrong_key_rejected = True
        vault.get(object_ids["core"], identities={"core": identities["core"]})
        locked = vault.lock()
        locked_clean = not (root / ".local" / "private-index" / "authorized.jsonl").exists()
        verified_before = vault.verify(identities=identities)["ok"]

        snapshot = base / "snapshot"
        shutil.copytree(root, snapshot)
        public_path = vault._locate_object(object_ids["public"])
        public_path.write_text("tampered", encoding="utf-8")
        tamper_detected = not vault.verify()["ok"]
        shutil.rmtree(root)
        shutil.copytree(snapshot, root)
        restored = KnowledgeVault(root, age_path=age_path)
        restored_ok = restored.verify(identities=discover_identities(root))["ok"]
        return {
            "five_tiers": len(object_ids) == 5,
            "authorized_tiers": unlocked["tiers"],
            "authorized_search": searchable,
            "wrong_key_rejected": wrong_key_rejected,
            "lock_removed_files": locked["removed_files"],
            "lock_clean": locked_clean,
            "verified_before_recovery": verified_before,
            "tamper_detected": tamper_detected,
            "recovery_verified": restored_ok,
        }


def local_setup(
    target: str | Path | None = None,
    *,
    mode: str = "test",
    age_path: str | Path | None = None,
    initialize_git: bool = True,
    run_self_test: bool = True,
    confirm_production_key_use: bool = False,
    confirm_nonempty_directory: bool = False,
) -> dict[str, Any]:
    plan = local_plan(target, mode=mode, age_path=age_path)
    state = plan["workspace_state"]
    if state == "invalid-path":
        raise KBError("the selected workspace is not a directory")
    if state == "occupied-directory" and not confirm_nonempty_directory:
        raise KBError("current directory is not a knowledge instance and is not empty; confirmation required")
    if mode == "production" and not confirm_production_key_use:
        raise KBError("production setup requires explicit production key confirmation")
    resolved_age = _preflight(age_path, require_git=initialize_git)
    root = Path(plan["target"])
    existing_instance = state == "existing-instance"
    if existing_instance:
        layout_kind = workspace_state(root)["layout_kind"]
        knowledge_root = resolve_knowledge_root(root)
        if layout_kind == "domain-root":
            ensure_personal_domain_manifest(root)
        else:
            ensure_instance_manifest(root)
        initialized = {
            "root": str(root),
            "knowledge_root": str(knowledge_root),
            "created_files": [],
            "retained_files": [*INSTANCE_MARKERS, "config/instance.json"],
        }
    else:
        initialized = initialize_personal_domain(root)
        layout_kind = "domain-root"
        knowledge_root = Path(initialized["knowledge_root"])
    vault = KnowledgeVault(knowledge_root, age_path=resolved_age)
    if not vault.age_path or not vault.age_keygen_path:
        raise KBError("age and age-keygen are required before local Agent setup")
    credentials_created = False
    if mode == "test":
        recipients_file = knowledge_root / ".local" / "test-keys" / "recipients.json"
        if not recipients_file.is_file():
            existing_verification = vault.verify()
            if existing_instance and existing_verification["objects_checked"]:
                raise KBError("refusing to add test keys to a non-empty existing instance")
            vault.generate_test_keys(force=False)
            credentials_created = True
    else:
        missing = [tier for tier in PRIVATE_TIERS if not vault.doctor()["recipients"][tier]]
        if missing:
            raise KBError("production recipients are not configured for: " + ", ".join(missing))
    git_initialized = _run_git_init(root) if initialize_git else False
    verification = vault.verify()
    if not verification["ok"]:
        raise KBError("new instance verification failed")
    self_test = _synthetic_recovery_test(resolved_age) if run_self_test else {"skipped": True}
    if run_self_test and not all(
        self_test[key]
        for key in (
            "five_tiers",
            "authorized_search",
            "wrong_key_rejected",
            "lock_clean",
            "verified_before_recovery",
            "tamper_detected",
            "recovery_verified",
        )
    ):
        raise KBError("local Agent synthetic verification failed")
    manifest = (
        ensure_personal_domain_manifest(root)
        if layout_kind == "domain-root"
        else ensure_instance_manifest(root)
    )
    instance_registered = register_instance(root, manifest)
    return {
        "status": "ok",
        "flow": "local-agent",
        "domain_kind": "personal",
        "module_kind": "knowledge",
        "root": str(root),
        "knowledge_root": str(knowledge_root),
        "layout_kind": layout_kind,
        "mode": mode,
        "action": "connected" if existing_instance else "created",
        "created_new_instance": not existing_instance,
        "git_initialized": git_initialized,
        "credentials_created": credentials_created,
        "production_credentials_created": False,
        "instance_registered": instance_registered,
        "verification": verification,
        "self_test": self_test,
        "next_prompts": [
            "把这段内容保存到基础区。",
            "搜索我关于某个主题的资料。",
            "锁定知识库并清理临时明文。",
        ],
        "confirmation_required_next": [
            "production-key-setup",
            "public-release",
            "remote-connection",
        ],
        "plan": plan,
    }


def save(
    vault: KnowledgeVault,
    *,
    tier: str,
    title: str,
    content: str,
    summary: str = "",
    kind: str = "raw",
    confirm_public: bool = False,
    origin_kind: str = "unknown",
    authorship_status: str = "unknown",
    contributors: tuple[str, ...] = (),
    source_refs: tuple[str, ...] = (),
    intended_role: str | None = None,
    corpus_eligibility: str = "denied",
    style_eligibility: str = "denied",
    training_permission: str = "denied",
    clarification_status: str | None = None,
    clarification_refs: tuple[str, ...] = (),
) -> dict[str, Any]:
    routed_module = {
        "creation": "creations",
        "cognition": "cognition",
        "work": "work",
        "publication": "products",
    }.get(intended_role or "")
    if routed_module:
        return {
            "status": "needs-module",
            "target_module": routed_module,
            "content_saved": False,
            "questions": [
                f"Enable or select the {routed_module} module before saving this content."
            ],
        }
    if tier in ("public", "archive") and not confirm_public:
        raise KBError("public or archive save requires explicit confirmation")
    return vault.add(
        request_id=_request_id("save"),
        tier=tier,
        kind=kind,
        title=title,
        summary=summary,
        content=content,
        catalog_visibility="public" if tier == "public" else "private",
        human_confirmed=confirm_public,
        origin_kind=origin_kind,
        authorship_status=authorship_status,
        contributors=contributors,
        source_refs=source_refs,
        intended_role=intended_role or "unknown",
        corpus_eligibility=corpus_eligibility,
        style_eligibility=style_eligibility,
        training_permission=training_permission,
        clarification_status=clarification_status,
        clarification_refs=clarification_refs,
    )


def search(
    vault: KnowledgeVault,
    query: str,
    *,
    authorized: bool = False,
    keep_unlocked: bool = False,
    identities: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    unlocked: list[str] = []
    if authorized:
        selected = dict(identities or discover_identities(vault.root))
        if not selected:
            raise KBError("authorized search requires identity file Secrets")
        result = vault.unlock_index(selected)
        unlocked = result["tiers"]
    results = vault.search(query)
    locked_after = False
    if authorized and not keep_unlocked:
        vault.lock()
        locked_after = True
    return {
        "status": "ok",
        "query": query,
        "authorized_tiers": unlocked,
        "locked_after": locked_after,
        "results": results,
    }

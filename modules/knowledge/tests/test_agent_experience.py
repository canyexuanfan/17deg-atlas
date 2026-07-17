from __future__ import annotations

import base64
import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
REMOTE_PATH = MODULE_ROOT / "skills" / "github-api-file-pusher" / "scripts" / "remote.py"
sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import KBError, KnowledgeVault  # noqa: E402
from kb_vault.adapters.github_contents import GitHubContentsAdapter  # noqa: E402
from kb_vault.bootstrap import (  # noqa: E402
    KNOWLEDGE_MODULE_RELATIVE,
    initialize_instance,
    initialize_personal_domain,
    resolve_knowledge_root,
)
from kb_vault.dependencies import LocalDependencyEnvironment  # noqa: E402
from kb_vault.agent import (  # noqa: E402
    detect_agent_runtime,
    github_first_plan,
    github_first_setup,
    local_plan,
    local_setup,
    save,
    workspace_state,
)
from kb_vault.github_onboarding import (  # noqa: E402
    GitHubCLIEnvironment,
    GitHubCLIRepositoryClient,
    GitHubRepositoryClient,
    bind_repository,
    configured_repository,
    initial_git_sync,
    resolve_github_token,
)
from kb_vault.migration import import_workspace_candidate, migration_repair_plan  # noqa: E402


def load_remote():
    spec = importlib.util.spec_from_file_location("atlas_remote_experience", REMOTE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalAgentExperienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = tempfile.TemporaryDirectory()
        self.base = Path(self.context.name)
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        self.age = Path(configured_age).resolve() if configured_age else None

    def tearDown(self) -> None:
        self.context.cleanup()

    def test_runtime_auto_requires_an_explicit_entry_or_remote_marker(self) -> None:
        cleared = {
            "ATLAS_ENTRY_RUNTIME": "",
            "KB_AGENT_RUNTIME": "",
            "CI": "",
            "CODESPACES": "",
            "GITHUB_ACTIONS": "",
            "REMOTE_CONTAINERS": "",
        }
        with mock.patch.dict(os.environ, cleared, clear=False):
            with self.assertRaises(KBError):
                detect_agent_runtime("auto")

    def test_unknown_offline_runtime_cannot_report_onboarding_complete(self) -> None:
        environment = {
            "ATLAS_RUNTIME_UPDATE_CHECK": "unavailable",
            "ATLAS_RUNTIME_SOURCE_COMMIT": "",
            "ATLAS_RUNTIME_REFRESHED": "false",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            result = github_first_plan(self.base / "vault", runtime="local")
        self.assertEqual("blocked", result["status"])
        self.assertEqual("runtime-update-unverified", result["terminal_state"])
        self.assertFalse(result["onboarding_complete"])

    def test_u01_u02_u04_u05_plan_gate_and_no_internal_ids(self) -> None:
        plan = local_plan(self.base / "vault", mode="test")
        self.assertEqual("local-agent", plan["flow"])
        self.assertEqual([], plan["confirmation_required"])
        production = local_plan(self.base / "vault", mode="production")
        self.assertIn("production-key-use", production["confirmation_required"])
        with self.assertRaises(KBError):
            local_setup(self.base / "production", mode="production", run_self_test=False)

        parser = subprocess.run(
            [sys.executable, str(MODULE_ROOT / "scripts" / "kb.py"), "agent-save", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, parser.returncode, parser.stderr)
        self.assertNotIn("request-id", parser.stdout)
        self.assertNotIn("object-id", parser.stdout)
        self.assertNotIn("recipient", parser.stdout)

    def test_cli_access_lifecycle_legacy_compatibility_and_view_audit(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "access-cli"
        local_setup(
            root,
            mode="test",
            age_path=self.age,
            initialize_git=False,
            run_self_test=False,
        )

        def run_cli(*arguments: str) -> dict[str, object]:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_ROOT / "scripts" / "kb.py"),
                    "--root",
                    str(root),
                    "--age-path",
                    str(self.age),
                    *arguments,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            return json.loads(completed.stdout)

        current = run_cli(
            "add",
            "--request-id",
            "access-current",
            "--access",
            "basic",
            "--lifecycle",
            "archived",
            "--kind",
            "raw",
            "--media-type",
            "article",
            "--title",
            "Archived basic note",
            "--content",
            "kept encrypted while archived",
        )
        legacy = run_cli(
            "add",
            "--request-id",
            "access-legacy",
            "--tier",
            "basic",
            "--kind",
            "raw",
            "--media-type",
            "file",
            "--title",
            "Legacy basic note",
            "--content",
            "legacy compatibility remains available",
        )
        self.assertEqual("basic", current["details"]["tier"])
        self.assertEqual("basic", legacy["details"]["tier"])

        knowledge_root = resolve_knowledge_root(root)
        run_cli(
            "workspace-view-build",
            "--identity-basic",
            str(knowledge_root / ".local" / "test-keys" / "basic.identity"),
        )
        audit = run_cli("workspace-view-audit")
        self.assertEqual("ok", audit["status"])
        self.assertEqual({}, audit["property_conflicts"])
        article = next((root / "knowledge" / "raw" / "articles").glob("*.md"))
        text = article.read_text(encoding="utf-8")
        self.assertIn('access: "basic"', text)
        self.assertIn('lifecycle: "archived"', text)

    def test_u03_u06_u07_u08_u10_full_local_setup(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "agent-vault"
        result = local_setup(root, mode="test", age_path=self.age)
        self.assertTrue((root / ".git").is_dir())
        self.assertTrue((root / "AGENTS.md").is_file())
        knowledge_root = resolve_knowledge_root(root)
        self.assertTrue((knowledge_root / "AGENTS.md").is_file())
        for name in ("inbox", "raw", "library", "wiki"):
            self.assertTrue((root / "knowledge" / name).is_dir())
        manifest = json.loads((root / "personal.yaml").read_text(encoding="utf-8"))
        self.assertEqual("personal", manifest["domain_kind"])
        self.assertEqual("domain-root", manifest["layout_kind"])
        self.assertTrue(manifest["instance_id"].startswith("personal-"))
        self.assertEqual("knowledge", manifest["modules"][0]["module_kind"])
        self.assertEqual(KNOWLEDGE_MODULE_RELATIVE.as_posix(), manifest["modules"][0]["path"])
        self.assertEqual(str(knowledge_root.resolve()), result["knowledge_root"])
        self.assertFalse(
            {"creations", "cognition", "work", "products"}
            & {item.name for item in root.iterdir() if item.is_dir()}
        )
        self.assertTrue(result["self_test"]["five_tiers"])
        self.assertTrue(result["self_test"]["wrong_key_rejected"])
        self.assertTrue(result["self_test"]["lock_clean"])
        self.assertTrue(result["self_test"]["recovery_verified"])
        self.assertEqual(3, len(result["next_prompts"]))
        self.assertFalse((knowledge_root / ".git").exists())
        vault = KnowledgeVault(knowledge_root, age_path=self.age)
        identities = {
            tier: knowledge_root / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        doctor = vault.doctor(identities)
        self.assertTrue(doctor["ok"], doctor)
        self.assertEqual(str(root.resolve()), doctor["git_repository_root"])
        self.assertFalse(result["production_credentials_created"])

    def test_u05_agent_save_generates_identifiers(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "save-vault"
        local_setup(root, mode="test", age_path=self.age, run_self_test=False)
        receipt = save(
            KnowledgeVault(resolve_knowledge_root(root), age_path=self.age),
            tier="basic",
            title="Agent note",
            content="agent-generated identifier",
        )
        self.assertTrue(receipt["object_id"].startswith("obj_"))
        self.assertTrue(receipt["receipt_id"].startswith("rct_"))
        self.assertEqual("needs-clarification", receipt["status"])
        stored_receipt = json.loads(
            (resolve_knowledge_root(root) / "receipts" / f"{receipt['receipt_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("needs-clarification", stored_receipt["status"])
        self.assertTrue(receipt["details"]["questions"])

    def test_u05_nonknowledge_content_is_not_misfiled(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "routing-vault"
        local_setup(root, mode="test", age_path=self.age, run_self_test=False)
        vault = KnowledgeVault(resolve_knowledge_root(root), age_path=self.age)
        before = list(vault.root.glob("vault/**/*"))
        result = save(
            vault,
            tier="basic",
            title="Draft article",
            content="A draft that belongs to a creation module.",
            intended_role="creation",
        )
        after = list(vault.root.glob("vault/**/*"))
        self.assertEqual("needs-module", result["status"])
        self.assertEqual("creations", result["target_module"])
        self.assertFalse(result["content_saved"])
        self.assertEqual(before, after)

    def test_u09_failure_does_not_overwrite_existing_file(self) -> None:
        target = self.base / "unsafe"
        target.mkdir()
        marker = target / "README.md"
        marker.write_text("keep me", encoding="utf-8")
        with self.assertRaises(KBError):
            local_setup(target, mode="test", age_path=self.age, run_self_test=False)
        self.assertEqual("keep me", marker.read_text(encoding="utf-8"))

    def test_u02_missing_dependency_fails_before_creating_target(self) -> None:
        target = self.base / "missing-age"
        with self.assertRaises(KBError):
            local_setup(target, mode="test", age_path=self.base / "not-age", run_self_test=False)
        self.assertFalse(target.exists())

    def test_current_workspace_is_detected_and_existing_instance_is_connected(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "opened-workspace"
        first = local_setup(root, mode="test", age_path=self.age, run_self_test=False)
        self.assertEqual("created", first["action"])
        with mock.patch.dict(os.environ, {"KB_INSTANCE_ROOT": str(root)}, clear=False):
            plan = local_plan()
            second = local_setup(mode="test", age_path=self.age, run_self_test=False)
        self.assertEqual("existing-instance", plan["workspace_state"])
        self.assertEqual("connect-existing", plan["action"])
        self.assertEqual("connected", second["action"])
        self.assertFalse(second["created_new_instance"])
        self.assertFalse(second["credentials_created"])

    def test_legacy_module_root_remains_connected_without_relayout(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "legacy-vault-fixture"
        initialize_instance(root)
        state = workspace_state(root)
        self.assertEqual("existing-instance", state["state"])
        self.assertEqual("module-root", state["layout_kind"])
        connected = local_setup(root, mode="test", age_path=self.age, run_self_test=False)
        self.assertEqual("module-root", connected["layout_kind"])
        self.assertEqual(str(root.resolve()), connected["knowledge_root"])
        self.assertFalse((root / KNOWLEDGE_MODULE_RELATIVE).exists())

    def test_occupied_current_workspace_requires_confirmation(self) -> None:
        root = self.base / "ordinary-project"
        root.mkdir()
        (root / "app.py").write_text("print('project')", encoding="utf-8")
        state = workspace_state(root)
        plan = local_plan(root)
        self.assertEqual("occupied-directory", state["state"])
        self.assertEqual("needs-confirmation", plan["status"])
        self.assertIn("create-inside-nonempty-current-directory", plan["confirmation_required"])

    def test_default_target_uses_independent_child_inside_occupied_workspace(self) -> None:
        root = self.base / "ordinary-project"
        root.mkdir()
        (root / "app.py").write_text("print('project')", encoding="utf-8")
        with mock.patch("kb_vault.agent.Path.cwd", return_value=root):
            plan = local_plan()
        self.assertEqual(str((root / "17deg-personal").resolve()), plan["target"])
        self.assertEqual("new-path", plan["workspace_state"])
        self.assertEqual([], plan["confirmation_required"])

    def test_loose_workspace_materials_are_detected_before_github(self) -> None:
        workspace = self.base / "existing-materials"
        questions = workspace / "questions"
        questions.mkdir(parents=True)
        (questions / "first.md").write_text("first user note", encoding="utf-8")
        (questions / "second.md").write_text("second user note", encoding="utf-8")
        (workspace / ".obsidian").mkdir()
        (workspace / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
        (workspace / "欢迎.md").write_text("editor control file", encoding="utf-8")
        fake = self.FakeGitHub()
        with mock.patch.dict(
            os.environ,
            {"ATLAS_WORKSPACE": str(workspace), "KB_INSTANCE_ROOT": ""},
            clear=False,
        ):
            plan = github_first_plan(
                runtime="local",
                repository_name="alice-space",
                client=fake,
                run_initial_sync=False,
            )
        self.assertEqual("needs-existing-materials-decision", plan["terminal_state"])
        self.assertEqual("review-existing-materials-before-github", plan["repository_action"])
        self.assertEqual(
            "review-existing-materials-before-continue", plan["local_plan"]["action"]
        )
        self.assertEqual(2, plan["existing_materials"]["candidate_count"])
        self.assertEqual(["questions"], [value["group"] for value in plan["existing_materials"]["groups"]])
        self.assertEqual(["existing-materials-action"], plan["required_inputs"])
        leave_option = next(
            item
            for item in plan["existing_materials"]["action_options"]
            if item["choice"] == "leave-in-place"
        )
        self.assertEqual(
            "continue-without-importing-existing-materials", leave_option["effect"]
        )
        self.assertEqual([], fake.created)

    def test_existing_instance_still_detects_unabsorbed_parent_materials(self) -> None:
        workspace = self.base / "existing-target-with-materials"
        questions = workspace / "questions"
        questions.mkdir(parents=True)
        (questions / "legacy.md").write_text("legacy user material", encoding="utf-8")
        target = workspace / "alice-space"
        initialize_personal_domain(target)
        fake = self.FakeGitHub()
        with mock.patch.dict(os.environ, {"ATLAS_WORKSPACE": str(workspace)}, clear=False):
            plan = github_first_plan(
                target,
                runtime="local",
                repository_name="alice-space",
                client=fake,
                run_initial_sync=False,
            )
        self.assertEqual(1, plan["existing_materials"]["candidate_count"])
        self.assertEqual("needs-existing-materials-decision", plan["terminal_state"])

    def test_remote_entry_cannot_silently_skip_local_workspace_materials(self) -> None:
        workspace = self.base / "remote-materials"
        workspace.mkdir()
        (workspace / "note.md").write_text("local-only source material", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ATLAS_WORKSPACE": str(workspace)}, clear=False):
            plan = github_first_plan(
                runtime="remote",
                repository_name="alice-space",
                client=self.FakeGitHub(),
                run_initial_sync=False,
            )
        self.assertEqual("existing-materials-require-local-entry", plan["terminal_state"])
        self.assertEqual("switch-to-local-entry-for-existing-materials", plan["repository_action"])

    def test_workspace_materials_are_staged_then_absorbed_before_remote_creation(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        workspace = self.base / "absorb-workspace"
        questions = workspace / "questions"
        questions.mkdir(parents=True)
        original = questions / "rough-note.md"
        original.write_text("A rough note that must be preserved exactly.\n", encoding="utf-8")
        target = workspace / "alice-space"
        fake = self.FakeGitHub()
        with mock.patch.dict(
            os.environ,
            {"ATLAS_WORKSPACE": str(workspace), "KB_INSTANCE_ROOT": ""},
            clear=False,
        ):
            staged = github_first_setup(
                target,
                runtime="local",
                repository_name="alice-space",
                client=fake,
                age_path=self.age,
                run_self_test=False,
                run_initial_sync=False,
                existing_materials_action="import-review",
                confirm_existing_materials_import=True,
            )
            absorbed = import_workspace_candidate(
                target,
                source_path="questions/rough-note.md",
                access="basic",
                rights="owned",
                origin_kind="self",
                authorship_status="self_authored",
                intended_role="knowledge",
                summary="This note records a reusable test insight.",
                card_question="What does the rough note preserve?",
                card_answer="It preserves the original material and its traceability.",
                topic_names=["Knowledge migration"],
                age_path=self.age,
            )
        self.assertEqual("needs-semantic-review", staged["terminal_state"])
        self.assertFalse(staged["onboarding_complete"])
        self.assertEqual([], fake.created)
        staged_copy = target / "knowledge" / "inbox" / "migration" / "workspace" / "questions" / original.name
        self.assertEqual(original.read_bytes(), staged_copy.read_bytes())
        self.assertEqual("ready-for-initial-sync", absorbed["terminal_state"])
        self.assertFalse(absorbed["retirement_required"])
        self.assertTrue(original.is_file())
        self.assertTrue(any((target / "knowledge" / "raw").rglob("*.md")))
        self.assertGreaterEqual(len(list((target / "knowledge" / "wiki").rglob("*.md"))), 3)
        state = json.loads(
            (target / ".atlas" / "state" / "knowledge-migration.json").read_text(encoding="utf-8")
        )
        self.assertEqual("verified", state["status"])
        self.assertEqual(0, state["semantic_import"]["pending_count"])

    def test_verified_workspace_import_reopens_only_new_or_changed_materials(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        workspace = self.base / "incremental-workspace"
        questions = workspace / "questions"
        questions.mkdir(parents=True)
        first = questions / "first.md"
        first.write_text("First stable note.\n", encoding="utf-8")
        target = workspace / "alice-space"
        environment = {"ATLAS_WORKSPACE": str(workspace), "KB_INSTANCE_ROOT": ""}
        with mock.patch.dict(os.environ, environment, clear=False):
            github_first_setup(
                target,
                runtime="local",
                repository_name="alice-space",
                client=self.FakeGitHub(),
                age_path=self.age,
                run_self_test=False,
                run_initial_sync=False,
                existing_materials_action="import-review",
                confirm_existing_materials_import=True,
            )
            import_workspace_candidate(
                target,
                source_path="questions/first.md",
                access="basic",
                rights="owned",
                origin_kind="self",
                authorship_status="self_authored",
                intended_role="knowledge",
                summary="The first stable note.",
                card_question="Which note is stable?",
                card_answer="The first note is stable.",
                topic_names=["Incremental import"],
                age_path=self.age,
            )
            second = questions / "second.md"
            second.write_text("A later note must not be hidden by the old receipt.\n", encoding="utf-8")
            plan = github_first_plan(
                target,
                runtime="local",
                repository_name="alice-space",
                client=self.FakeGitHub(),
                age_path=self.age,
                run_initial_sync=False,
            )
            restaged = github_first_setup(
                target,
                runtime="local",
                repository_name="alice-space",
                client=self.FakeGitHub(),
                age_path=self.age,
                run_self_test=False,
                run_initial_sync=False,
                existing_materials_action="import-review",
                confirm_existing_materials_import=True,
            )
        self.assertEqual("needs-existing-materials-decision", plan["terminal_state"])
        self.assertEqual(1, plan["existing_materials"]["candidate_count"])
        self.assertEqual(["questions/second.md"], plan["existing_materials"]["sample_paths"])
        self.assertEqual(1, restaged["candidate_count"])
        self.assertEqual(1, restaged["pending_count"])
        self.assertIn(
            "这些材料应进入知识库，还是确认保留在知识库之外？",
            restaged["batch_questions"],
        )
        state = json.loads(
            (target / ".atlas" / "state" / "knowledge-migration.json").read_text(
                encoding="utf-8"
            )
        )
        by_path = {item["path"]: item for item in state["semantic_candidates"]}
        self.assertEqual("completed", by_path["questions/first.md"]["status"])
        self.assertEqual("pending", by_path["questions/second.md"]["status"])

    def test_nonknowledge_workspace_material_is_routed_without_empty_module(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        workspace = self.base / "route-workspace"
        drafts = workspace / "drafts"
        drafts.mkdir(parents=True)
        original = drafts / "article.md"
        original.write_text("A draft article, not a knowledge object.\n", encoding="utf-8")
        target = workspace / "alice-space"
        with mock.patch.dict(os.environ, {"ATLAS_WORKSPACE": str(workspace)}, clear=False):
            github_first_setup(
                target,
                runtime="local",
                repository_name="alice-space",
                client=self.FakeGitHub(),
                age_path=self.age,
                run_self_test=False,
                run_initial_sync=False,
                existing_materials_action="import-review",
                confirm_existing_materials_import=True,
            )
            routed = import_workspace_candidate(
                target,
                source_path="drafts/article.md",
                access="basic",
                rights="owned",
                origin_kind="self",
                authorship_status="self_authored",
                intended_role="creation",
                confirm_route_outside_knowledge=True,
                age_path=self.age,
            )
        self.assertEqual("ready-for-initial-sync", routed["terminal_state"])
        self.assertEqual("creations", routed["target_module"])
        self.assertTrue(original.is_file())
        self.assertFalse((target / "creations").exists())
        self.assertFalse(migration_repair_plan(target)["repair_required"])
        state = json.loads(
            (target / ".atlas" / "state" / "knowledge-migration.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, state["semantic_import"]["candidate_count"])
        self.assertEqual(1, state["semantic_import"]["routed_count"])
        self.assertEqual(0, state["semantic_import"]["imported_count"])

    def test_github_first_reuses_single_existing_child_instance(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        workspace = self.base / "obsidian-vault"
        workspace.mkdir()
        (workspace / ".obsidian").mkdir()
        existing = workspace / "17deg-atlas-knowledge"
        initialize_personal_domain(existing)
        bind_repository(
            existing,
            owner="example-user",
            repo="17deg-atlas-knowledge",
            branch="main",
            visibility="private",
            subject_id="person:github:example-user",
        )
        fake = self.FakeGitHub()
        fake.repositories["example-user/17deg-atlas-knowledge"] = {
            "owner": "example-user",
            "name": "17deg-atlas-knowledge",
            "private": True,
            "default_branch": "main",
            "html_url": "https://github.com/example-user/17deg-atlas-knowledge",
        }
        with mock.patch.dict(
            os.environ,
            {"ATLAS_WORKSPACE": str(workspace), "KB_INSTANCE_ROOT": ""},
            clear=False,
        ):
            plan = github_first_plan(runtime="local", client=fake, run_initial_sync=False)
            result = github_first_setup(
                runtime="local",
                client=fake,
                age_path=self.age,
                run_self_test=False,
                run_initial_sync=False,
            )
        self.assertEqual(str(existing.resolve()), plan["workspace"])
        self.assertEqual("existing-instance", plan["workspace_state"])
        self.assertEqual("connect-remembered-repository", plan["repository_action"])
        self.assertEqual(str(existing.resolve()), result["workspace"])
        self.assertFalse(result["repository_cloned"])
        self.assertEqual([], fake.cloned)
        self.assertFalse((workspace / "17deg-personal").exists())

    def test_github_first_gates_duplicate_local_clones_of_one_repository(self) -> None:
        workspace = self.base / "duplicate-workspace"
        workspace.mkdir()
        fake = self.FakeGitHub()
        repository = {
            "owner": "example-user",
            "name": "shared-vault",
            "private": True,
            "default_branch": "main",
            "html_url": "https://github.com/example-user/shared-vault",
        }
        fake.repositories["example-user/shared-vault"] = repository
        fake.instances.add("example-user/shared-vault")
        roots = []
        for name in ("first-copy", "second-copy"):
            root = workspace / name
            initialize_personal_domain(root)
            bind_repository(
                root,
                owner="example-user",
                repo="shared-vault",
                branch="main",
                visibility="private",
                subject_id="person:github:example-user",
            )
            roots.append(str(root.resolve()))
        with mock.patch.dict(
            os.environ,
            {"ATLAS_WORKSPACE": str(workspace), "KB_INSTANCE_ROOT": ""},
            clear=False,
        ):
            plan = github_first_plan(runtime="local", client=fake)
        self.assertEqual(str(workspace.resolve()), plan["workspace"])
        self.assertIn("select-local-knowledge-instance", plan["confirmation_required"])
        self.assertNotIn(
            "create-inside-nonempty-current-directory", plan["confirmation_required"]
        )
        self.assertEqual(
            "select-existing-local-instance", plan["local_plan"]["action"]
        )
        self.assertIn("reuse-selected-instance", plan["local_plan"]["automatic_steps"])
        self.assertNotIn(
            "create-independent-instance", plan["local_plan"]["automatic_steps"]
        )
        self.assertEqual(roots, plan["local_plan"]["registered_instance_choices"])

    def test_legacy_remote_layout_requires_choice_before_new_instance(self) -> None:
        workspace = self.base / "legacy-choice-workspace"
        workspace.mkdir()
        fake = self.FakeGitHub()
        repository = {
            "owner": "example-user",
            "name": "legacy-knowledge",
            "private": True,
            "default_branch": "main",
            "html_url": "https://github.com/example-user/legacy-knowledge",
            "instance": {
                "domain_kind": "personal",
                "subject_kind": "person",
                "subject_id": "person:github:example-user",
                "layout_kind": "domain-root",
                "modules": [
                    {
                        "module_kind": "knowledge",
                        "path": "domains/personal/knowledge",
                    }
                ],
            },
        }
        fake.repositories["example-user/legacy-knowledge"] = repository
        fake.instances.add("example-user/legacy-knowledge")
        with mock.patch.dict(
            os.environ,
            {"ATLAS_WORKSPACE": str(workspace), "KB_INSTANCE_ROOT": ""},
            clear=False,
        ):
            plan = github_first_plan(runtime="local", client=fake)
        self.assertIsNone(plan["repository"])
        self.assertEqual(
            "select-legacy-migration-or-current-instance", plan["repository_action"]
        )
        self.assertIn(
            "select-migration-legacy-or-empty-current-instance",
            plan["confirmation_required"],
        )
        self.assertEqual("migrate-current", plan["repository_options"][0]["choice"])
        self.assertTrue(plan["repository_options"][0]["recommended"])
        self.assertEqual("migration-plan", plan["repository_options"][0]["next_action"])
        self.assertEqual("local", plan["repository_options"][0]["execution_entry"])
        self.assertEqual("connect-legacy", plan["repository_options"][1]["choice"])
        self.assertEqual(
            "create-empty-current", plan["repository_options"][2]["choice"]
        )
        self.assertFalse(plan["repository_options"][2]["existing_content_copied"])
        self.assertTrue(
            plan["repository_options"][0]["target"].endswith("17deg-personal")
        )

    class FakeGitHub:
        def __init__(self) -> None:
            self.repositories: dict[str, dict[str, object]] = {}
            self.instances: set[str] = set()
            self.created: list[str] = []
            self.cloned: list[str] = []

        def account(self):
            return {"login": "example-user"}

        def repository(self, owner, name):
            return self.repositories.get(f"{owner}/{name}")

        def create_repository(self, name, *, private):
            self.created.append(name)
            value = {
                "owner": "example-user",
                "name": name,
                "private": private,
                "default_branch": "main",
                "html_url": f"https://github.com/example-user/{name}",
            }
            self.repositories[f"example-user/{name}"] = value
            return value

        def has_instance_marker(self, owner, name):
            return f"{owner}/{name}" in self.instances

        def discover_instances(self, **filters):
            del filters
            return [self.repositories[name] for name in sorted(self.instances)]

        def clone_repository(self, owner, name, target):
            self.cloned.append(f"{owner}/{name}")
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=target,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

    def test_github_first_plan_suggests_but_does_not_choose_repository_name(self) -> None:
        root = self.base / "research notes"
        fake = self.FakeGitHub()
        plan = github_first_plan(root, runtime="local", client=fake)
        self.assertEqual("agent-onboarding", plan["flow"])
        self.assertEqual("local", plan["runtime"])
        self.assertIsNone(plan["repository"])
        self.assertEqual("personal", plan["domain_kind"])
        self.assertEqual("knowledge", plan["module_kind"])
        self.assertEqual("person:github:example-user", plan["subject_id"])
        self.assertEqual("choose-repository-name", plan["repository_action"])
        self.assertEqual("17deg-personal", plan["suggested_repository_name"])
        self.assertEqual(["repository-name"], plan["required_inputs"])
        self.assertIn("choose-repository-name", plan["confirmation_required"])
        self.assertNotIn("token", json.dumps(plan).lower())

    def test_selected_repository_name_also_drives_the_default_local_folder(self) -> None:
        workspace = self.base / "occupied"
        workspace.mkdir()
        (workspace / "existing.md").write_text("keep", encoding="utf-8")
        fake = self.FakeGitHub()
        with mock.patch("kb_vault.agent.atlas_workspace", return_value=workspace):
            plan = github_first_plan(
                runtime="local",
                repository_name="alice-memory",
                client=fake,
            )
        self.assertEqual(str((workspace / "alice-memory").resolve()), plan["workspace"])
        self.assertEqual("alice-memory", plan["repository"])
        self.assertEqual("needs-existing-materials-decision", plan["terminal_state"])
        self.assertEqual(1, plan["existing_materials"]["candidate_count"])

    def test_domain_repository_binding_stays_in_personal_manifest(self) -> None:
        root = self.base / "alice-space"
        initialize_personal_domain(root)
        bind_repository(
            root,
            owner="example-user",
            repo="alice-space",
            branch="main",
            visibility="private",
        )
        self.assertFalse((root / "config").exists())
        manifest = json.loads((root / "personal.yaml").read_text(encoding="utf-8"))
        self.assertEqual("alice-space", manifest["repository"]["name"])
        self.assertEqual(
            {"owner": "example-user", "repo": "alice-space", "branch": "main"},
            configured_repository(root),
        )

    def test_github_first_existing_repository_requires_connection_confirmation(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "existing-notes"
        fake = self.FakeGitHub()
        fake.repositories["example-user/existing-notes-knowledge"] = {
            "owner": "example-user",
            "name": "existing-notes-knowledge",
            "private": True,
            "default_branch": "stable",
            "html_url": "https://github.com/example-user/existing-notes-knowledge",
        }
        plan = github_first_plan(
            root,
            runtime="remote",
            repository_name="existing-notes-knowledge",
            client=fake,
        )
        self.assertEqual("connect-existing-repository", plan["repository_action"])
        self.assertEqual("stable", plan["branch"])
        self.assertIn("connect-existing-repository", plan["confirmation_required"])
        with self.assertRaises(KBError):
            github_first_setup(
                root,
                runtime="remote",
                repository_name="existing-notes-knowledge",
                client=fake,
                age_path=self.age,
                run_self_test=False,
                run_initial_sync=False,
            )
        result = github_first_setup(
            root,
            runtime="remote",
            repository_name="existing-notes-knowledge",
            client=fake,
            age_path=self.age,
            run_self_test=False,
            run_initial_sync=False,
            confirm_existing_repository=True,
        )
        self.assertFalse(result["repository_created"])
        self.assertEqual("stable", result["branch"])
        self.assertEqual([], fake.created)

    def test_github_first_setup_creates_binds_and_remembers_repository(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "opened-workspace"
        fake = self.FakeGitHub()
        with self.assertRaises(KBError):
            github_first_setup(
                root,
                runtime="local",
                client=fake,
                age_path=self.age,
                run_self_test=False,
            )
        self.assertEqual([], fake.created)
        result = github_first_setup(
            root,
            runtime="remote",
            repository_name="alice-memory",
            client=fake,
            age_path=self.age,
            confirm_repository_create=True,
            confirm_initial_sync=True,
            syncer=lambda root, **kwargs: {"committed": True, "pushed": True},
        )
        self.assertTrue(result["repository_created"])
        self.assertTrue(result["repository_private"])
        self.assertEqual("remote", result["runtime"])
        self.assertTrue((root / "knowledge").is_dir())
        self.assertFalse((root / "domains" / "personal" / "knowledge").exists())
        config = json.loads(
            (resolve_knowledge_root(root) / "config" / "projection.yml").read_text(encoding="utf-8")
        )
        self.assertEqual("example-user", config["remote_policy"]["allowed_owner"])
        self.assertEqual("alice-memory", config["remote_policy"]["allowed_repo"])
        manifest = json.loads((root / "personal.yaml").read_text(encoding="utf-8"))
        self.assertEqual("person:github:example-user", manifest["subject_id"])
        self.assertEqual("alice-memory", manifest["repository"]["name"])
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, origin.returncode, origin.stderr)
        self.assertEqual(
            "https://github.com/example-user/alice-memory",
            origin.stdout.strip(),
        )
        self.assertTrue(result["git_remote_bound"])
        self.assertTrue(result["onboarding_complete"])
        self.assertEqual("complete", result["terminal_state"])
        self.assertTrue(all(result["validation"].values()))
        remembered = github_first_plan(root, runtime="remote", client=fake)
        self.assertEqual("connect-remembered-repository", remembered["repository_action"])
        self.assertEqual(["initial-github-sync"], remembered["confirmation_required"])

    def test_github_first_setup_preflights_before_remote_creation(self) -> None:
        fake = self.FakeGitHub()
        with self.assertRaises(KBError):
            github_first_setup(
                self.base / "missing-runtime",
                runtime="local",
                client=fake,
                age_path=self.base / "missing-age",
                run_self_test=False,
                confirm_repository_create=True,
                confirm_initial_sync=True,
            )
        self.assertEqual([], fake.created)

    def test_initial_sync_confirmation_stops_before_remote_creation(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        fake = self.FakeGitHub()
        with self.assertRaises(KBError):
            github_first_setup(
                self.base / "no-sync-confirmation",
                runtime="local",
                client=fake,
                age_path=self.age,
                run_self_test=False,
                confirm_repository_create=True,
            )
        self.assertEqual([], fake.created)

    def test_selected_discovered_repository_accepts_owner_name(self) -> None:
        fake = self.FakeGitHub()
        fake.repositories["example-user/selected-vault"] = {
            "owner": "example-user",
            "name": "selected-vault",
            "private": True,
            "default_branch": "main",
            "html_url": "https://github.com/example-user/selected-vault",
        }
        plan = github_first_plan(
            self.base / "selected",
            runtime="local",
            repository_name="example-user/selected-vault",
            client=fake,
        )
        self.assertEqual("example-user/selected-vault", plan["repository"])
        self.assertEqual("connect-existing-repository", plan["repository_action"])

    def test_discovered_instance_connects_without_repository_address(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "remote-copy"
        fake = self.FakeGitHub()
        fake.repositories["example-user/my-existing-vault"] = {
            "owner": "example-user",
            "name": "my-existing-vault",
            "private": True,
            "default_branch": "main",
            "html_url": "https://github.com/example-user/my-existing-vault",
        }
        fake.instances.add("example-user/my-existing-vault")
        plan = github_first_plan(root, runtime="remote", client=fake)
        self.assertEqual("connect-discovered-repository", plan["repository_action"])
        self.assertEqual("example-user/my-existing-vault", plan["repository"])
        self.assertEqual(["initial-github-sync"], plan["confirmation_required"])
        result = github_first_setup(
            root,
            runtime="remote",
            client=fake,
            age_path=self.age,
            run_self_test=False,
            run_initial_sync=False,
        )
        self.assertTrue(result["repository_cloned"])
        self.assertEqual(["example-user/my-existing-vault"], fake.cloned)

    def test_domain_aware_discovery_uses_instance_manifest(self) -> None:
        repositories = [
            {
                "owner": {"login": "example-user"},
                "name": name,
                "private": True,
                "default_branch": "main",
                "html_url": f"https://github.com/example-user/{name}",
            }
            for name in ("knowledge", "enterprise", "someone-else")
        ]
        manifests = {
            "knowledge": {
                "domain_kind": "personal",
                "layout_kind": "domain-root",
                "modules": [
                    {
                        "module_kind": "knowledge",
                        "path": KNOWLEDGE_MODULE_RELATIVE.as_posix(),
                        "module_instance_id": "personal-knowledge-example",
                    }
                ],
                "subject_id": "person:github:example-user",
            },
            "enterprise": {
                "domain_kind": "enterprise",
                "module_kind": "knowledge",
                "subject_id": "organization:example",
            },
            "someone-else": {
                "domain_kind": "personal",
                "module_kind": "knowledge",
                "subject_id": "person:github:someone-else",
            },
        }

        def transport(method, url, headers, body):
            del headers, body
            if method == "GET" and "/user/repos?" in url:
                return 200, repositories
            name = url.split("/repos/example-user/", 1)[1].split("/", 1)[0]
            encoded = base64.b64encode(
                json.dumps(manifests[name]).encode("utf-8")
            ).decode("ascii")
            return 200, {"encoding": "base64", "content": encoded}

        client = GitHubRepositoryClient("test-token", transport=transport)
        discovered = client.discover_instances(
            domain_kind="personal",
            module_kind="knowledge",
            subject_id="person:github:example-user",
        )
        self.assertEqual(["knowledge"], [value["name"] for value in discovered])
        self.assertEqual("personal", discovered[0]["instance"]["domain_kind"])

    def test_project_registry_reconnects_one_instance_and_gates_multiple(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        atlas_workspace = self.base / "atlas-workspace"
        atlas_workspace.mkdir()
        first_root = self.base / "first-vault"
        second_root = self.base / "second-vault"
        with mock.patch.dict(
            os.environ,
            {"ATLAS_WORKSPACE": str(atlas_workspace)},
            clear=False,
        ):
            first = local_setup(
                first_root,
                mode="test",
                age_path=self.age,
                run_self_test=False,
            )
            self.assertTrue(first["instance_registered"])
            self.assertEqual(str(first_root.resolve()), local_plan()["target"])
            local_setup(
                second_root,
                mode="test",
                age_path=self.age,
                run_self_test=False,
            )
            multiple = local_plan()
        registry = atlas_workspace / ".17deg-atlas" / "state" / "instances.json"
        self.assertTrue(registry.is_file())
        self.assertIn("select-local-knowledge-instance", multiple["confirmation_required"])
        self.assertEqual(2, len(multiple["registered_instance_choices"]))

    def test_multiple_discovered_instances_only_asks_for_selection(self) -> None:
        fake = self.FakeGitHub()
        for name in ("vault-one", "vault-two"):
            key = f"example-user/{name}"
            fake.repositories[key] = {
                "owner": "example-user",
                "name": name,
                "private": True,
                "default_branch": "main",
                "html_url": f"https://github.com/{key}",
            }
            fake.instances.add(key)
        plan = github_first_plan(self.base / "selection", runtime="local", client=fake)
        self.assertIsNone(plan["repository"])
        self.assertEqual("select-discovered-repository", plan["repository_action"])
        self.assertEqual(
            ["example-user/vault-one", "example-user/vault-two"],
            plan["repository_choices"],
        )
        self.assertIn("select-knowledge-repository", plan["confirmation_required"])

    def test_repository_client_creates_private_repository_without_leaking_token(self) -> None:
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, dict(headers), body))
            if url.endswith("/user"):
                return 200, {"login": "example-user"}
            if method == "GET":
                return 404, {"message": "Not Found"}
            return 201, {
                "owner": {"login": "example-user"},
                "name": "notes-knowledge",
                "private": True,
                "default_branch": "main",
                "html_url": "https://github.com/example-user/notes-knowledge",
            }

        token = "secret-value-that-must-not-return"
        client = GitHubRepositoryClient(token, transport=transport)
        self.assertEqual("example-user", client.account()["login"])
        self.assertIsNone(client.repository("example-user", "notes-knowledge"))
        created = client.create_repository("notes-knowledge", private=True)
        self.assertTrue(created["private"])
        self.assertNotIn(token, json.dumps(created))
        self.assertTrue(all(call[2]["Authorization"] == f"Bearer {token}" for call in calls))

    def test_github_cli_does_not_treat_network_failure_as_missing_repository(self) -> None:
        def not_found_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="GraphQL: Could not resolve to a Repository",
            )

        missing = GitHubCLIRepositoryClient("gh", runner=not_found_runner)
        self.assertIsNone(missing.repository("example-user", "missing"))

        def network_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="network connection failed",
            )

        unavailable = GitHubCLIRepositoryClient("gh", runner=network_runner)
        with self.assertRaises(KBError):
            unavailable.repository("example-user", "unknown")

    def test_onboarding_cli_does_not_require_repository_address_or_secret_flags(self) -> None:
        parser = subprocess.run(
            [sys.executable, str(MODULE_ROOT / "scripts" / "kb.py"), "agent-start", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, parser.returncode, parser.stderr)
        self.assertNotIn("--owner", parser.stdout)
        self.assertNotIn("--repo ", parser.stdout)
        self.assertNotIn("--token", parser.stdout)
        self.assertNotIn("--identity", parser.stdout)

    def test_github_cli_bootstrap_plans_install_and_browser_authorization(self) -> None:
        commands = []
        launches = []
        installed = {"value": False}
        authenticated = {"value": False}

        def which(name):
            if name == "winget":
                return "winget"
            if name == "gh" and installed["value"]:
                return "gh"
            return None

        def runner(command, **kwargs):
            commands.append(command)
            if command[0] == "winget":
                installed["value"] = True
                return subprocess.CompletedProcess(command, 0)
            if command[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(command, 0 if authenticated["value"] else 1)
            return subprocess.CompletedProcess(command, 0)

        class PendingProcess:
            pid = 4242

            @staticmethod
            def poll():
                return None

            @staticmethod
            def terminate():
                raise AssertionError("a visible device-code flow must not be terminated")

        def launch(command, **kwargs):
            launches.append(command)
            kwargs["stderr"].write(
                b"! First copy your one-time code: ABCD-1234\n"
                b"Open this URL to continue in your web browser: "
                b"https://github.com/login/device\n"
            )
            kwargs["stderr"].flush()
            return PendingProcess()

        environment = GitHubCLIEnvironment(
            which=which,
            runner=runner,
            process_launcher=launch,
            process_checker=lambda pid: pid == 4242,
            state_root=self.base / "github-auth",
            sleeper=lambda _seconds: None,
            common_paths=[],
        )
        with mock.patch("kb_vault.github_onboarding.platform.system", return_value="Windows"):
            plan = environment.plan()
            self.assertEqual(
                ["install-github-cli", "authorize-github-account"],
                plan["confirmation_required"],
            )
            with self.assertRaises(KBError):
                environment.install()
            environment.install(confirm=True)
            needs_confirmation = environment.authenticate()
            self.assertEqual("needs-confirmation", needs_confirmation["terminal_state"])
            pending = environment.authenticate(confirm=True)
            self.assertEqual("needs-user-github-authorization", pending["terminal_state"])
            self.assertEqual("ABCD-1234", pending["user_action"]["device_code"])
            self.assertEqual("https://github.com/login/device", pending["user_action"]["verification_uri"])
            repeated = environment.authenticate(confirm=True)
            self.assertEqual(pending, repeated)
            self.assertEqual(1, len(launches))
            authenticated["value"] = True
            ready = environment.authenticate()
            self.assertEqual("ready", ready["terminal_state"])
        self.assertTrue(any(command[0] == "winget" for command in commands))
        self.assertTrue(any(command[1:3] == ["auth", "login"] for command in launches))
        self.assertTrue(any(command[1:3] == ["auth", "setup-git"] for command in commands))

    def test_github_authorization_failure_requires_a_separate_retry_confirmation(self) -> None:
        launches = []

        def runner(command, **_kwargs):
            if command[1:3] == ["auth", "status"]:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0)

        class ExitedProcess:
            pid = 5151

            @staticmethod
            def poll():
                return 1

            @staticmethod
            def terminate():
                return None

        def launch(command, **kwargs):
            launches.append(command)
            kwargs["stderr"].write(
                b"! First copy your one-time code: EFGH-5678\n"
                b"Open this URL to continue in your web browser: "
                b"https://github.com/login/device\n"
                b"failed to authenticate via web browser: unexpected EOF\n"
            )
            kwargs["stderr"].flush()
            return ExitedProcess()

        environment = GitHubCLIEnvironment(
            executable="gh",
            runner=runner,
            process_launcher=launch,
            process_checker=lambda _pid: False,
            state_root=self.base / "failed-github-auth",
            sleeper=lambda _seconds: None,
        )
        pending = environment.authenticate(confirm=True)
        self.assertEqual("needs-user-github-authorization", pending["terminal_state"])
        failed = environment.authenticate()
        self.assertEqual("github-authorization-failed", failed["terminal_state"])
        self.assertEqual("network-or-proxy", failed["failure_reason"])
        self.assertEqual(["retry-github-authorization"], failed["confirmation_required"])
        still_failed = environment.authenticate(confirm=True)
        self.assertEqual("github-authorization-failed", still_failed["terminal_state"])
        self.assertEqual(1, len(launches))
        retried = environment.authenticate(confirm_retry=True)
        self.assertEqual("needs-user-github-authorization", retried["terminal_state"])
        self.assertEqual(2, len(launches))

    def test_windows_authorization_process_check_is_read_only(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows process query contract")
        with mock.patch("kb_vault.github_onboarding.os.kill") as unsafe_kill:
            self.assertTrue(GitHubCLIEnvironment._process_is_running(os.getpid()))
        unsafe_kill.assert_not_called()

    def test_github_first_setup_hands_device_authorization_back_to_the_user(self) -> None:
        class Dependencies:
            @staticmethod
            def install_age(**_kwargs):
                return "age"

        class Environment:
            client_called = False

            @staticmethod
            def install(**_kwargs):
                return "gh"

            @staticmethod
            def authenticate(**_kwargs):
                return {
                    "status": "needs-user-action",
                    "flow": "github-authorization",
                    "terminal_state": "needs-user-github-authorization",
                    "next_action": "complete-github-browser-authorization",
                    "confirmation_required": [],
                    "user_action": {
                        "verification_uri": "https://github.com/login/device",
                        "device_code": "IJKL-9012",
                    },
                    "do_not_retry": True,
                }

            def client(self):
                self.client_called = True
                raise AssertionError("repository access must wait for user authorization")

        environment = Environment()
        result = github_first_setup(
            self.base / "device-code-workspace",
            runtime="local",
            repository_name="device-code-workspace",
            run_initial_sync=False,
            cli_environment=environment,
            dependency_environment=Dependencies(),
            confirm_github_login=True,
        )
        self.assertEqual("needs-user-github-authorization", result["terminal_state"])
        self.assertEqual("IJKL-9012", result["user_action"]["device_code"])
        self.assertFalse(environment.client_called)

    def test_contents_adapter_accepts_github_multiline_base64(self) -> None:
        encoded = base64.b64encode("知识内容".encode("utf-8")).decode("ascii")
        multiline = "\n".join(encoded[index : index + 5] for index in range(0, len(encoded), 5))

        def transport(method, url, headers, body):
            del method, url, headers, body
            return 200, {"content": multiline, "sha": "content-sha"}

        adapter = GitHubContentsAdapter(
            owner="example-user",
            repo="example-vault",
            token="test-token",
            transport=transport,
        )
        result = adapter.get_file(path="knowledge/example.md")
        self.assertEqual("知识内容".encode("utf-8"), result["content"])
        self.assertEqual("content-sha", result["sha"])

    def test_contents_adapter_reuses_authenticated_github_cli_token(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gh", "auth", "token", "--hostname", "github.com"],
            0,
            stdout=b"github-cli-secret\n",
            stderr=b"",
        )
        cleared = {"KB_GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}
        with mock.patch.dict(os.environ, cleared, clear=False), mock.patch(
            "kb_vault.github_onboarding.shutil.which",
            side_effect=lambda name: "gh" if name == "gh" else None,
        ), mock.patch(
            "kb_vault.github_onboarding.subprocess.run",
            return_value=completed,
        ) as runner:
            token, source = resolve_github_token()
        self.assertEqual("github-cli-secret", token)
        self.assertEqual("github-cli", source)
        runner.assert_called_once()

    def test_windows_package_manager_can_be_found_outside_path(self) -> None:
        winget = self.base / "WindowsApps" / "winget.exe"
        winget.parent.mkdir()
        winget.write_bytes(b"placeholder")
        environment = GitHubCLIEnvironment(
            which=lambda _name: None,
            common_paths=[],
            package_manager_paths={"winget": [winget]},
        )
        with mock.patch("kb_vault.github_onboarding.platform.system", return_value="Windows"):
            installation = environment.installation()
        self.assertTrue(installation["required"])
        self.assertEqual("winget", installation["manager"])
        self.assertEqual(str(winget), installation["command"][0])
        self.assertIn("--accept-package-agreements", installation["command"])

    def test_age_installation_is_planned_and_runs_only_after_confirmation(self) -> None:
        bin_root = self.base / "installed-bin"
        age = bin_root / "age.exe"
        keygen = bin_root / "age-keygen.exe"
        installed = {"value": False}
        commands = []

        def which(name):
            if name == "winget":
                return "winget"
            if name == "age" and installed["value"]:
                return str(age)
            return None

        def runner(command, **kwargs):
            del kwargs
            commands.append(command)
            bin_root.mkdir()
            age.write_bytes(b"age")
            keygen.write_bytes(b"age-keygen")
            installed["value"] = True
            return subprocess.CompletedProcess(command, 0)

        environment = LocalDependencyEnvironment(
            which=which,
            runner=runner,
            system="windows",
        )
        with mock.patch(
            "kb_vault.dependencies._windows_age_candidates", return_value=[]
        ):
            installation = environment.age_installation()
            self.assertTrue(installation["required"])
            self.assertEqual("winget", installation["manager"])
            self.assertIn("FiloSottile.age", installation["command"])
            plan = github_first_plan(
                self.base / "age-install-plan",
                runtime="local",
                client=self.FakeGitHub(),
                dependency_environment=environment,
            )
            self.assertIn("install-age", plan["confirmation_required"])
            with self.assertRaises(KBError):
                environment.install_age()
            resolved = environment.install_age(confirm=True)
        self.assertEqual(age.resolve(), resolved)
        self.assertEqual(1, len(commands))

    def test_windows_age_is_found_outside_path_without_reinstalling(self) -> None:
        package = self.base / "WinGet" / "age"
        package.mkdir(parents=True)
        age = package / "age.exe"
        keygen = package / "age-keygen.exe"
        age.write_bytes(b"age")
        keygen.write_bytes(b"age-keygen")
        environment = LocalDependencyEnvironment(
            which=lambda _name: None,
            system="windows",
            common_paths={"age": [age]},
        )
        installation = environment.age_installation()
        self.assertFalse(installation["required"])
        self.assertEqual("existing", installation["manager"])
        self.assertEqual(age.resolve(), environment.install_age())

    def test_vault_doctor_reuses_winget_age_without_local_copies(self) -> None:
        root = self.base / "doctor-vault"
        initialize_instance(root)
        local_app_data = self.base / "LocalAppData"
        package = (
            local_app_data
            / "Microsoft"
            / "WinGet"
            / "Packages"
            / "FiloSottile.age_test"
            / "age"
        )
        package.mkdir(parents=True)
        age = package / "age.exe"
        keygen = package / "age-keygen.exe"
        age.write_bytes(b"age")
        keygen.write_bytes(b"age-keygen")
        environment = {
            "LOCALAPPDATA": str(local_app_data),
            "KB_AGE_PATH": "",
            "KB_AGE_KEYGEN_PATH": "",
            "PATH": "",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch(
            "kb_vault.dependencies.platform.system", return_value="Windows"
        ):
            doctor = KnowledgeVault(root).doctor()
        self.assertEqual(str(age.resolve()), doctor["age"])
        self.assertEqual(str(keygen.resolve()), doctor["age_keygen"])

    def test_github_cli_discovers_instances_with_one_graphql_request(self) -> None:
        commands = []
        manifest = {
            "domain_kind": "personal",
            "modules": [{"module_kind": "knowledge", "path": "knowledge"}],
            "subject_id": "person:github:example-user",
        }

        def runner(command, **kwargs):
            del kwargs
            commands.append(command)
            if command[1:3] == ["api", "user"]:
                payload = {"login": "example-user"}
            else:
                payload = {
                    "data": {
                        "user": {
                            "repositories": {
                                "nodes": [
                                    {
                                        "name": "personal-space",
                                        "isPrivate": True,
                                        "url": "https://github.com/example-user/personal-space",
                                        "defaultBranchRef": {"name": "main"},
                                        "personal": {"text": json.dumps(manifest)},
                                        "instance": None,
                                        "knowledge": None,
                                        "deepKnowledge": None,
                                        "legacyKnowledge": None,
                                    }
                                ]
                            }
                        }
                    }
                }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        client = GitHubCLIRepositoryClient("gh", runner=runner)
        self.assertEqual("example-user", client.account()["login"])
        discovered = client.discover_instances(
            domain_kind="personal",
            module_kind="knowledge",
            subject_id="person:github:example-user",
        )
        self.assertEqual(["personal-space"], [value["name"] for value in discovered])
        self.assertEqual(2, len(commands))
        self.assertEqual(["api", "graphql"], commands[1][1:3])
        self.assertFalse(any("/contents/" in argument for command in commands for argument in command))

    def test_initial_sync_commits_and_pushes_to_local_bare_remote(self) -> None:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        root = self.base / "sync-source"
        remote = self.base / "sync-remote.git"
        local_setup(root, mode="test", age_path=self.age, run_self_test=False)
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        calls = []

        def recording_runner(command, **kwargs):
            calls.append((command, dict(kwargs.get("env", {}))))
            return subprocess.run(command, **kwargs)

        result = initial_git_sync(root, account="example-user", runner=recording_runner)
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertTrue(all("GIT_CONFIG_GLOBAL" not in env for _command, env in calls))
        self.assertTrue(
            all(any(str(value).startswith("core.excludesFile=") for value in command) for command, _env in calls)
        )
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, remote_head.returncode, remote_head.stderr)
        author = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(
            "example-user <example-user@users.noreply.github.com>",
            author.stdout.strip(),
        )

    def test_initial_sync_rejects_questions_and_local_credentials(self) -> None:
        root = self.base / "unsafe-sync"
        remote = self.base / "unsafe-sync.git"
        root.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote)],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        (root / "questions").mkdir()
        (root / "questions" / "internal.md").write_text("do not upload", encoding="utf-8")
        with self.assertRaisesRegex(KBError, "scope audit rejected"):
            initial_git_sync(root, account="example-user")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(0, head.returncode)


class RemoteAgentExperienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.remote = load_remote()

    def args(self, *extra: str):
        return self.remote.build_parser().parse_args(["connect", *extra])

    def test_u11_u12_u13_public_read_profile(self) -> None:
        args = self.args(
            "--role", "public-read", "--owner", "example", "--repo", "content"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            profile = self.remote.connection_profile(args)
        self.assertEqual("remote-agent", profile["flow"])
        self.assertEqual("public-read", profile["role"])
        self.assertTrue(profile["safe_temp"])
        self.assertFalse(profile["token_configured"])

    def test_u14_u15_u18_encrypted_write_uses_target_token_and_recipient_only(self) -> None:
        args = self.args(
            "--role", "encrypted-write", "--tier", "basic",
            "--owner", "example", "--repo", "content-only", "--confirm-content-repository",
        )
        secrets = {
            "KB_GITHUB_TOKEN": "secret-token-value",
            "KB_AGE_RECIPIENT_BASIC": "age1" + "x" * 58,
        }
        with mock.patch.dict(os.environ, secrets, clear=True):
            profile = self.remote.connection_profile(args)
        encoded = json.dumps(profile, ensure_ascii=False)
        self.assertEqual("example/content-only", profile["repository"])
        self.assertTrue(profile["recipient_configured"])
        self.assertFalse(profile["identity_configured"])
        self.assertNotIn(secrets["KB_GITHUB_TOKEN"], encoded)
        self.assertNotIn(secrets["KB_AGE_RECIPIENT_BASIC"], encoded)

        unsafe = dict(secrets)
        unsafe["KB_AGE_IDENTITY_BASIC_FILE"] = "configured"
        with mock.patch.dict(os.environ, unsafe, clear=True):
            with self.assertRaises(self.remote.RemoteError):
                self.remote.connection_profile(args)

    def test_u16_authorized_read_requires_secret_ack_and_core_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = root / "basic.identity"
            identity.write_text("test-placeholder", encoding="utf-8")
            args = self.args(
                "--role", "authorized-read", "--tier", "basic", "--vault-root", str(root)
            )
            with mock.patch.dict(os.environ, {"KB_AGE_IDENTITY_BASIC_FILE": str(identity)}, clear=True):
                with self.assertRaises(self.remote.RemoteError):
                    self.remote.connection_profile(args)
            acknowledged = self.args(
                "--role", "authorized-read", "--tier", "basic", "--vault-root", str(root),
                "--acknowledge-trusted-runtime",
            )
            with mock.patch.dict(os.environ, {"KB_AGE_IDENTITY_BASIC_FILE": str(identity)}, clear=True):
                profile = self.remote.connection_profile(acknowledged)
            self.assertEqual("preview", profile["status"])
            self.assertEqual("high", profile["risk"])

    def test_u17_connection_test_uses_role_appropriate_auth(self) -> None:
        calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

        def transport(method, url, headers, body):
            calls.append((method, url, dict(headers), body))
            return 200, {"sha": "abc"}

        original = self.remote.Client

        class FakeClient(original):
            def __init__(self, **kwargs):
                super().__init__(transport=transport, **kwargs)

        args = self.args(
            "--role", "public-read", "--owner", "example", "--repo", "content", "--test-connection"
        )
        with mock.patch.object(self.remote, "Client", FakeClient), mock.patch.dict(os.environ, {}, clear=True):
            profile = self.remote.connection_profile(args)
        self.assertTrue(profile["network_tested"])
        self.assertEqual(1, len(calls))
        self.assertNotIn("Authorization", calls[0][2])

    def test_public_read_ignores_environment_token(self) -> None:
        calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

        def transport(method, url, headers, body):
            calls.append((method, url, dict(headers), body))
            return 200, {"sha": "abc"}

        with mock.patch.dict(os.environ, {"KB_GITHUB_TOKEN": "must-not-be-used"}, clear=True):
            client = self.remote.Client(
                owner="example",
                repo="public-content",
                token="",
                require_token=False,
                transport=transport,
            )
            client.get("README.md")
        self.assertNotIn("Authorization", calls[0][2])

    def test_public_read_without_token_uses_raw_content_and_local_hash(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b"public-read-probe"

        client = self.remote.Client(
            owner="example",
            repo="public-content",
            branch="release/test",
            require_token=False,
        )
        with mock.patch.object(self.remote.urllib.request, "urlopen", return_value=FakeResponse()) as opened:
            result = client.get("docs/hello world.md")
        request = opened.call_args.args[0]
        self.assertEqual(
            "https://raw.githubusercontent.com/example/public-content/release%2Ftest/docs/hello%20world.md",
            request.full_url,
        )
        self.assertNotIn("Authorization", dict(request.header_items()))
        self.assertEqual(
            "sha256:" + hashlib.sha256(b"public-read-probe").hexdigest(),
            result["sha"],
        )

    def test_u19_u20_write_stops_before_network_and_reports_revocation(self) -> None:
        args = self.remote.build_parser().parse_args(
            [
                "add", "--owner", "example", "--repo", "content", "--request-id", "r1",
                "--agent-id", "agent", "--tier", "basic", "--title", "x",
                "--content-file", str(REMOTE_PATH),
            ]
        )
        with self.assertRaises(self.remote.RemoteError):
            self.remote.execute(args)

        connect = self.args(
            "--role", "encrypted-write", "--tier", "advanced",
            "--owner", "example", "--repo", "content", "--confirm-content-repository",
        )
        secrets = {
            "KB_GITHUB_TOKEN": "token",
            "KB_AGE_RECIPIENT_ADVANCED": "age1" + "y" * 58,
        }
        with mock.patch.dict(os.environ, secrets, clear=True):
            profile = self.remote.connection_profile(connect)
        self.assertIn("revoke-target-repository-token", profile["revocation"])
        self.assertIn("rotate-recipient-for-future-writes", profile["revocation"])

    def test_remote_repository_url_and_safe_auto_role(self) -> None:
        public = self.args("--repository", "https://github.com/example/public-content.git")
        with mock.patch.dict(os.environ, {}, clear=True):
            public_profile = self.remote.connection_profile(public)
        self.assertEqual("example/public-content", public_profile["repository"])
        self.assertEqual("public-read", public_profile["role"])
        self.assertTrue(public_profile["role_inferred"])

        encrypted = self.args(
            "--repository", "example/private-content", "--confirm-content-repository"
        )
        secrets = {
            "KB_GITHUB_TOKEN": "token",
            "KB_AGE_RECIPIENT_BASIC": "age1" + "z" * 58,
        }
        with mock.patch.dict(os.environ, secrets, clear=True):
            encrypted_profile = self.remote.connection_profile(encrypted)
        self.assertEqual("encrypted-write", encrypted_profile["role"])
        self.assertEqual(["append-basic-ciphertext"], encrypted_profile["permissions"])

    def test_auto_role_never_adopts_identity_secret(self) -> None:
        args = self.args("--repository", "example/private-content")
        with mock.patch.dict(
            os.environ, {"KB_AGE_IDENTITY_BASIC_FILE": "configured-secret-path"}, clear=True
        ):
            with self.assertRaises(self.remote.RemoteError):
                self.remote.connection_profile(args)

    def test_trusted_runtime_authorized_search_writes_then_clears_ignored_result(self) -> None:
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        if not configured_age:
            self.skipTest("set KB_TEST_AGE or install age")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "trusted-vault"
            local_setup(root, mode="test", age_path=configured_age, run_self_test=False)
            knowledge_root = resolve_knowledge_root(root)
            save(
                KnowledgeVault(knowledge_root, age_path=configured_age),
                tier="basic",
                title="Trusted remote note",
                content="trusted-runtime-private-needle",
            )
            identity = knowledge_root / ".local" / "test-keys" / "basic.identity"
            args = self.remote.build_parser().parse_args(
                [
                    "authorized-search",
                    "private-needle",
                    "--tier",
                    "basic",
                    "--vault-root",
                    str(knowledge_root),
                    "--acknowledge-trusted-runtime",
                ]
            )
            secrets = {
                "KB_AGE_IDENTITY_BASIC_FILE": str(identity),
                "KB_AGE_PATH": str(configured_age),
            }
            with mock.patch.dict(os.environ, secrets, clear=False):
                receipt = self.remote.execute(args)
                clear_args = self.remote.build_parser().parse_args(
                    ["clear-results", "--vault-root", str(knowledge_root)]
                )
                result_path = Path(receipt["result_file"])
                private_result = result_path.read_text(encoding="utf-8")
                cleared = self.remote.execute(clear_args)
        self.assertNotIn("trusted-runtime-private-needle", json.dumps(receipt))
        self.assertIn("trusted-runtime-private-needle", private_result)
        self.assertFalse(result_path.exists())
        self.assertGreaterEqual(cleared["removed_files"], 1)


if __name__ == "__main__":
    unittest.main()

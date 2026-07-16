from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import KBError, KnowledgeCurator, KnowledgeVault  # noqa: E402
from kb_vault.bootstrap import (  # noqa: E402
    initialize_instance,
    initialize_personal_domain,
    resolve_knowledge_root,
)
from kb_vault.agent import github_first_setup  # noqa: E402
from kb_vault.github_onboarding import (  # noqa: E402
    GitHubCLIRepositoryClient,
    GitHubRepositoryClient,
    bind_repository,
)
from kb_vault.migration import (  # noqa: E402
    migrate_instance,
    migration_plan,
    prepare_migration_source,
    record_migration_candidate,
    retire_source,
    retirement_plan,
)
from kb_vault.workspace_views import (  # noqa: E402
    audit_workspace_views,
    clear_workspace_views,
    materialize_workspace_views,
)


class InstanceMigrationAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = tempfile.TemporaryDirectory()
        self.base = Path(self.context.name)
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        self.age = Path(configured_age).resolve() if configured_age else None

    def tearDown(self) -> None:
        self.context.cleanup()

    def legacy_instance(self, name: str = "legacy") -> tuple[Path, Path, KnowledgeVault]:
        root = self.base / name
        knowledge = root / "domains" / "personal" / "knowledge"
        initialize_instance(knowledge)
        manifest = {
            "schema_version": "1.0",
            "instance_id": f"{name}-personal",
            "domain_kind": "personal",
            "subject_kind": "person",
            "subject_id": "person:github:example-user",
            "layout_kind": "domain-root",
            "repository": {},
            "modules": [
                {
                    "module_kind": "knowledge",
                    "path": "domains/personal/knowledge",
                    "module_instance_id": f"{name}-knowledge",
                }
            ],
        }
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "instance.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        bind_repository(
            root,
            owner="example-user",
            repo=f"{name}-repo",
            branch="main",
            visibility="private",
            subject_id="person:github:example-user",
        )
        return root, knowledge, KnowledgeVault(knowledge, age_path=self.age)

    def add_real_content(
        self, knowledge: Path, vault: KnowledgeVault, *, include_document: bool = True
    ) -> dict[str, str]:
        if not self.age:
            self.skipTest("set KB_TEST_AGE or install age")
        recipients = vault.generate_test_keys(force=True)
        public = vault.add(
            request_id="migration-public",
            tier="public",
            kind="raw",
            title="Existing public note",
            summary="legacy-public-needle",
            content="A real document that must survive migration.",
            catalog_visibility="public",
            human_confirmed=True,
            recipients=recipients,
        )
        private = vault.add(
            request_id="migration-private",
            tier="basic",
            kind="raw",
            title="Existing private note",
            summary="legacy-private-needle",
            content="Private content must remain decryptable after migration.",
            catalog_visibility="none",
            recipients=recipients,
        )
        if include_document:
            (knowledge / "docs").mkdir(exist_ok=True)
            (knowledge / "docs" / "existing-note.md").write_text(
                "# Existing user document\n\nThis file must be copied.\n",
                encoding="utf-8",
            )
        return {"public": public["object_ids"][0], "private": private["object_ids"][0]}

    def complete_document_review(self, target: Path) -> dict[str, object]:
        runtime = resolve_knowledge_root(target)
        identities = {
            tier: runtime / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        recipients = json.loads(
            (runtime / ".local" / "test-keys" / "recipients.json").read_text(
                encoding="utf-8"
            )
        )
        staged = target / "knowledge" / "inbox" / "migration" / "docs" / "existing-note.md"
        content = staged.read_text(encoding="utf-8")
        vault = KnowledgeVault(runtime, age_path=self.age)
        raw = vault.add(
            request_id="migration-semantic-raw",
            tier="basic",
            kind="raw",
            title="Existing user document",
            summary="Preserved legacy source awaiting Wiki review.",
            content=content,
            rights="owned",
            origin_kind="self",
            authorship_status="ai_assisted",
            intended_role="knowledge",
            clarification_status="answered",
            recipients=recipients,
        )
        raw_id = raw["object_id"]
        compiled = KnowledgeCurator(vault).curate(
            request_id="migration-semantic-wiki",
            raw_object_ids=[raw_id],
            summary="The preserved document is compiled into reviewable knowledge.",
            card_question="How is the migrated document preserved and compiled?",
            card_answer="The raw source remains intact and Wiki objects cite it.",
            topic_names=["Migration"],
            identities=identities,
            recipients=recipients,
        )
        wiki_ids = [
            compiled["source_summary_id"],
            compiled["atomic_card_id"],
            *[item["object_id"] for item in compiled["topic_pages"]],
        ]
        return record_migration_candidate(
            target,
            source_path="docs/existing-note.md",
            raw_object_id=raw_id,
            wiki_object_ids=wiki_ids,
            identities=identities,
            age_path=self.age,
        )

    def test_m01_plan_separates_content_credentials_templates_and_retirement(self) -> None:
        root, knowledge, vault = self.legacy_instance()
        self.add_real_content(knowledge, vault)
        target = self.base / "current"
        plan = migration_plan(root, target)
        self.assertEqual("legacy-deep", plan["source_layout"])
        self.assertEqual("new-path", plan["target_state"])
        self.assertGreater(plan["counts"]["copy-object"], 0)
        self.assertGreater(plan["counts"]["semantic-import-candidate"], 0)
        self.assertGreater(plan["counts"]["transfer-credential"], 0)
        self.assertGreater(plan["counts"]["preserve-current-template"], 0)
        self.assertIn("confirm-content-migration", plan["confirmation_required"])
        self.assertIn(
            "confirm-local-credential-transfer", plan["confirmation_required"]
        )
        self.assertEqual("preserve", plan["default_source_disposition"])
        self.assertEqual(["preserve", "archive", "delete"], plan["source_disposition_options"])

    def test_m02_migration_preserves_documents_ciphertext_keys_and_source(self) -> None:
        root, knowledge, vault = self.legacy_instance()
        object_ids = self.add_real_content(knowledge, vault)
        target = self.base / "current"
        with self.assertRaises(KBError):
            migrate_instance(root, target, age_path=self.age)
        with self.assertRaises(KBError):
            migrate_instance(
                root,
                target,
                age_path=self.age,
                confirm_content_migration=True,
            )
        result = migrate_instance(
            root,
            target,
            age_path=self.age,
            confirm_content_migration=True,
            confirm_local_credential_transfer=True,
        )
        self.assertFalse(result["verified"])
        reviewed = self.complete_document_review(target)
        self.assertTrue(reviewed["migration_verified"])
        self.assertGreater(reviewed["objects_checked"], 0)
        view_files = reviewed["workspace_views"]["written_files"]
        self.assertTrue(any(value.startswith("knowledge/raw/") for value in view_files))
        self.assertTrue(any("knowledge/wiki/summaries/" in value for value in view_files))
        self.assertTrue(any("knowledge/wiki/cards/" in value for value in view_files))
        self.assertTrue(any("knowledge/wiki/topics/" in value for value in view_files))
        self.assertTrue(result["source_preserved"])
        self.assertTrue(root.is_dir())
        current_knowledge = resolve_knowledge_root(target)
        self.assertEqual(target / ".atlas" / "runtime", current_knowledge)
        self.assertTrue(
            (target / "knowledge" / "inbox" / "migration" / "docs" / "existing-note.md").is_file()
        )
        identities = {
            tier: current_knowledge / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        current = KnowledgeVault(current_knowledge, age_path=self.age)
        self.assertTrue(current.verify(identities=identities)["ok"])
        self.assertEqual(object_ids["private"], current.get(object_ids["private"], identities=identities)["object_id"])
        self.assertTrue(current.search_public("legacy-public-needle"))
        locked = current.lock()
        self.assertGreater(locked["workspace_views"]["removed_files"], 0)
        self.assertEqual([], list((target / "knowledge" / "raw").glob("*.md")))
        self.assertEqual([], list((target / "knowledge" / "wiki").rglob("*.md")))
        disposition = retirement_plan(root, target)
        self.assertTrue(disposition["target_verified"])
        self.assertTrue(disposition["options"][0]["recommended"])

    def test_m03_interrupted_copy_resumes_without_touching_source(self) -> None:
        root, knowledge, vault = self.legacy_instance()
        self.add_real_content(knowledge, vault)
        target = self.base / "current"
        with self.assertRaisesRegex(KBError, "simulated migration interruption"):
            migrate_instance(
                root,
                target,
                age_path=self.age,
                confirm_content_migration=True,
                confirm_local_credential_transfer=True,
                _fail_after=1,
            )
        self.assertTrue(root.is_dir())
        self.assertFalse(target.exists())
        self.assertTrue(any(self.base.glob(".current.migration-*")))
        result = migrate_instance(
            root,
            target,
            age_path=self.age,
            confirm_content_migration=True,
            confirm_local_credential_transfer=True,
        )
        self.assertTrue(result["resumed"])
        self.assertFalse(result["verified"])
        self.assertTrue(self.complete_document_review(target)["migration_verified"])
        self.assertTrue(root.is_dir())

    def test_m04_occupied_target_and_nested_paths_are_rejected(self) -> None:
        root, _knowledge, _vault = self.legacy_instance()
        occupied = self.base / "occupied"
        occupied.mkdir()
        (occupied / "mine.md").write_text("keep", encoding="utf-8")
        plan = migration_plan(root, occupied)
        self.assertEqual("occupied-directory", plan["target_state"])
        self.assertIn("choose-empty-migration-target", plan["confirmation_required"])
        with self.assertRaises(KBError):
            migrate_instance(
                root,
                occupied,
                age_path=self.age,
                confirm_content_migration=True,
                confirm_local_credential_transfer=True,
            )
        with self.assertRaises(KBError):
            migration_plan(root, root / "nested")

    def test_m04b_new_domain_is_shallow_and_unstructured_docs_wait_for_semantic_review(self) -> None:
        root, knowledge, _vault = self.legacy_instance("rough")
        rough = knowledge / "questions" / "粗糙排查记录.md"
        rough.parent.mkdir(parents=True, exist_ok=True)
        rough.write_text("# 粗糙排查记录\n\n原件必须保留，不能冒充已经编译。\n", encoding="utf-8")
        target = self.base / "custom-personal-space"

        plan = migration_plan(root, target)
        self.assertEqual(1, plan["counts"]["semantic-import-candidate"])
        self.assertEqual(target / ".atlas" / "runtime", Path(plan["target_knowledge_root"]))

        result = migrate_instance(
            root,
            target,
            confirm_content_migration=True,
        )
        self.assertFalse(result["verified"])
        self.assertTrue(result["original_files_verified"])
        self.assertEqual("needs-semantic-review", result["terminal_state"])
        self.assertEqual(1, result["semantic_import"]["pending_count"])
        self.assertEqual(target / ".atlas" / "runtime", resolve_knowledge_root(target))
        self.assertTrue((target / "knowledge" / "inbox" / "migration" / "questions" / rough.name).is_file())
        self.assertFalse((target / "knowledge" / "questions").exists())
        for name in ("inbox", "raw", "library", "wiki"):
            self.assertTrue((target / "knowledge" / name).is_dir())
        with self.assertRaisesRegex(KBError, "verified migration"):
            retirement_plan(root, target)

    def test_m04c_fresh_personal_domain_keeps_runtime_out_of_human_workspace(self) -> None:
        target = self.base / "alice-space"
        result = initialize_personal_domain(target)
        self.assertEqual(str(target / ".atlas" / "runtime"), result["runtime_root"])
        self.assertEqual(str(target / "knowledge"), result["knowledge_workspace"])
        self.assertFalse((target / "config").exists())
        self.assertFalse((target / "knowledge" / "config").exists())
        manifest = json.loads((target / "personal.yaml").read_text(encoding="utf-8"))
        module = manifest["modules"][0]
        self.assertEqual("knowledge", module["path"])
        self.assertEqual(".atlas/runtime", module["runtime_path"])

    def test_m04d_workspace_view_rebuild_and_lock_preserve_user_edits(self) -> None:
        target = self.base / "editable-view"
        initialize_personal_domain(target)
        envelope = {
            "object_id": "obj_editable_view",
            "object_kind": "raw",
            "tier": "advanced",
            "title": "Editable view",
            "content": "generated content",
            "source_refs": [],
            "interaction_refs": [],
            "media_type": "article",
            "review_state": "candidate",
            "maturity": "seed",
            "lifecycle": "active",
            "authorship_status": "external",
            "rights": "licensed",
        }
        first = materialize_workspace_views(target, [envelope], replace=True)
        self.assertTrue(first["written_files"][0].startswith("knowledge/raw/articles/"))
        view = target / first["written_files"][0]
        view.write_text("user edited content\n", encoding="utf-8")
        rebuilt = materialize_workspace_views(target, [envelope], replace=True)
        self.assertIn(first["written_files"][0], rebuilt["preserved_modified_views"])
        self.assertEqual("user edited content\n", view.read_text(encoding="utf-8"))
        renamed = {**envelope, "title": "Renamed generated view"}
        relocated = materialize_workspace_views(target, [renamed], replace=True)
        self.assertIn(first["written_files"][0], relocated["preserved_modified_views"])
        self.assertEqual(1, len(relocated["written_files"]))
        self.assertTrue((target / relocated["written_files"][0]).is_file())
        locked = clear_workspace_views(target)
        self.assertIn(first["written_files"][0], locked["preserved_modified_views"])
        self.assertTrue(view.is_file())
        self.assertFalse((target / relocated["written_files"][0]).exists())

    def test_m04e_workspace_views_keep_media_kind_stage_and_access_visible(self) -> None:
        target = self.base / "typed-views"
        initialize_personal_domain(target)
        envelopes = [
            {
                "object_id": "obj_conversation_extract",
                "object_kind": "raw",
                "tier": "core",
                "title": "Selected exchange",
                "content": "minimal extract",
                "media_type": "conversation",
                "source_refs": ["memory:chat_01"],
                "interaction_refs": ["chat_01#msg_03"],
                "review_state": "candidate",
                "maturity": "seed",
                "lifecycle": "active",
                "authorship_status": "coauthored",
                "rights": "restricted",
            },
            {
                "object_id": "obj_topic_candidate",
                "object_kind": "wiki",
                "tier": "basic",
                "title": "Topic candidate",
                "content": "candidate",
                "wiki_kind": "topic_page",
                "source_refs": ["obj_conversation_extract"],
                "interaction_refs": [],
                "review_state": "candidate",
                "maturity": "draft",
                "lifecycle": "active",
                "authorship_status": "ai_assisted",
                "rights": "owned",
            },
            {
                "object_id": "obj_card_verified",
                "object_kind": "wiki",
                "tier": "archive",
                "title": "Verified card",
                "content": "verified",
                "wiki_kind": "atomic_card",
                "source_refs": ["obj_conversation_extract"],
                "interaction_refs": [],
                "review_state": "verified",
                "maturity": "verified",
                "lifecycle": "archived",
                "authorship_status": "self_authored",
                "rights": "owned",
            },
        ]
        result = materialize_workspace_views(target, envelopes, replace=True)
        self.assertEqual("ok", result["status"])
        self.assertTrue(
            any(value.startswith("knowledge/raw/conversation-extracts/") for value in result["written_files"])
        )
        self.assertTrue(
            any(value.startswith("knowledge/wiki/topics/") for value in result["written_files"])
        )
        library_path = next(
            value for value in result["written_files"] if value.startswith("knowledge/library/cards/")
        )
        rendered = (target / library_path).read_text(encoding="utf-8")
        self.assertIn('access: "public"', rendered)
        self.assertIn('lifecycle: "archived"', rendered)
        self.assertIn('stage: "library"', rendered)
        self.assertIn('authorship: "self_authored"', rendered)

    def test_m04f_workspace_property_conflicts_stop_clean_audit(self) -> None:
        target = self.base / "view-conflict"
        initialize_personal_domain(target)
        envelope = {
            "object_id": "obj_conflicted_view",
            "object_kind": "raw",
            "tier": "core",
            "title": "Conflicted view",
            "content": "body",
            "media_type": "file",
            "source_refs": [],
            "interaction_refs": [],
            "review_state": "candidate",
            "maturity": "seed",
            "lifecycle": "active",
            "authorship_status": "external",
            "rights": "restricted",
        }
        built = materialize_workspace_views(target, [envelope], replace=True)
        view = target / built["written_files"][0]
        text = view.read_text(encoding="utf-8").replace('access: "core"', 'access: "public"')
        view.write_text(text, encoding="utf-8", newline="\n")
        audit = audit_workspace_views(target)
        self.assertEqual("needs-review", audit["status"])
        self.assertIn(built["written_files"][0], audit["property_conflicts"])
        self.assertIn("access", audit["property_conflicts"][built["written_files"][0]])

    def test_m05_retirement_defaults_to_preserve_and_deletion_needs_exact_repetition(self) -> None:
        root, knowledge, vault = self.legacy_instance("retire")
        self.add_real_content(knowledge, vault)
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Migration Test",
                "-c",
                "user.email=migration@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        target = self.base / "current"
        migrate_instance(
            root,
            target,
            age_path=self.age,
            confirm_content_migration=True,
            confirm_local_credential_transfer=True,
        )
        self.complete_document_review(target)
        preserved = retire_source(root, target, action="preserve")
        self.assertTrue(preserved["source_preserved"])
        with self.assertRaises(KBError):
            retire_source(
                root,
                target,
                action="delete",
                delete_local=True,
                expected_source_root=str(root),
            )
        with self.assertRaises(KBError):
            retire_source(
                root,
                target,
                action="delete",
                delete_remote=True,
                expected_repository="example-user/wrong",
                confirm_delete_remote=True,
            )

        class FakeDeleteClient:
            def __init__(self) -> None:
                self.deleted: list[str] = []

            def delete_repository(self, owner: str, name: str) -> None:
                self.deleted.append(f"{owner}/{name}")

        fake = FakeDeleteClient()
        deleted = retire_source(
            root,
            target,
            action="delete",
            delete_local=True,
            delete_remote=True,
            expected_source_root=str(root.resolve()),
            expected_repository="example-user/retire-repo",
            confirm_delete_local=True,
            confirm_delete_remote=True,
            client=fake,
        )
        self.assertTrue(deleted["local_deleted"])
        self.assertTrue(deleted["remote_deleted"])
        self.assertFalse(root.exists())
        self.assertEqual(["example-user/retire-repo"], fake.deleted)
        self.assertTrue(Path(deleted["git_history_backup"]).is_file())

    def test_m06_workspace_cli_exposes_migration_and_retirement_surfaces(self) -> None:
        root, _knowledge, _vault = self.legacy_instance("cli")
        target = self.base / "current"
        repository_root = MODULE_ROOT.parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(repository_root / "scripts" / "17deg-atlas.py"),
                "workspace",
                "migration-plan",
                "--source",
                str(root),
                "--target",
                str(target),
            ],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("knowledge-instance-migration", payload["flow"])
        help_result = subprocess.run(
            [sys.executable, str(repository_root / "scripts" / "17deg-atlas.py"), "workspace", "--help"],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("migration-plan", help_result.stdout)
        self.assertIn("migration-review", help_result.stdout)
        self.assertIn("retirement-start", help_result.stdout)

    def test_m07_verified_migration_can_complete_github_first_onboarding(self) -> None:
        root, knowledge, vault = self.legacy_instance("onboarding")
        self.add_real_content(knowledge, vault)
        target = self.base / "current"
        migrate_instance(
            root,
            target,
            age_path=self.age,
            confirm_content_migration=True,
            confirm_local_credential_transfer=True,
        )
        self.complete_document_review(target)

        class FakeGitHub:
            def __init__(self) -> None:
                self.repositories: dict[str, dict[str, object]] = {}

            def account(self):
                return {"login": "example-user"}

            def repository(self, owner, name):
                return self.repositories.get(f"{owner}/{name}")

            def create_repository(self, name, *, private):
                value = {
                    "owner": "example-user",
                    "name": name,
                    "private": private,
                    "default_branch": "main",
                    "html_url": f"https://github.com/example-user/{name}",
                }
                self.repositories[f"example-user/{name}"] = value
                return value

        result = github_first_setup(
            target,
            runtime="local",
            repository_name="migrated-current",
            client=FakeGitHub(),
            age_path=self.age,
            confirm_repository_create=True,
            confirm_initial_sync=True,
            syncer=lambda root, **kwargs: {"committed": True, "pushed": True},
        )
        self.assertFalse(result["onboarding_complete"])
        self.assertEqual("needs-retirement-selection", result["terminal_state"])
        self.assertTrue(result["retirement_required"])
        self.assertFalse(result["retirement_complete"])
        self.assertTrue(root.exists())
        retirement = retire_source(root, target, action="preserve")
        self.assertEqual("complete", retirement["terminal_state"])

    def test_m08_github_clients_delete_only_the_exact_repository_and_verify_absence(self) -> None:
        calls: list[tuple[str, str]] = []

        def transport(method, url, headers, body):
            del headers, body
            calls.append((method, url))
            if method == "DELETE":
                return 204, {}
            return 404, {"message": "Not Found"}

        GitHubRepositoryClient("test-token", transport=transport).delete_repository(
            "example-user", "legacy-repo"
        )
        self.assertEqual("DELETE", calls[0][0])
        self.assertTrue(calls[0][1].endswith("/repos/example-user/legacy-repo"))
        self.assertEqual("GET", calls[1][0])

        cli_calls: list[list[str]] = []

        def runner(arguments, **kwargs):
            del kwargs
            cli_calls.append(arguments)
            if arguments[1:3] == ["repo", "delete"]:
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(
                arguments,
                1,
                "",
                "GraphQL: Could not resolve to a Repository",
            )

        GitHubCLIRepositoryClient("gh", runner=runner).delete_repository(
            "example-user", "legacy-repo"
        )
        self.assertEqual(
            ["gh", "repo", "delete", "example-user/legacy-repo", "--yes"],
            cli_calls[0],
        )

    def test_m09_remote_only_legacy_repository_is_prepared_after_confirmation(self) -> None:
        fixture, _knowledge, _vault = self.legacy_instance("remote-source")
        target = self.base / "prepared-source"

        class FakeCloneClient:
            def repository(self, owner: str, name: str):
                if f"{owner}/{name}" != "example-user/remote-source-repo":
                    return None
                return {
                    "owner": owner,
                    "name": name,
                    "private": True,
                    "default_branch": "main",
                    "html_url": f"https://github.com/{owner}/{name}",
                }

            def clone_repository(self, owner: str, name: str, destination: Path) -> None:
                del owner, name
                shutil.copytree(fixture, destination)

        planned = prepare_migration_source(
            "example-user/remote-source-repo", target, client=FakeCloneClient()
        )
        self.assertEqual("needs-confirmation", planned["status"])
        self.assertFalse(target.exists())
        prepared = prepare_migration_source(
            "example-user/remote-source-repo",
            target,
            confirm_existing_repository=True,
            client=FakeCloneClient(),
        )
        self.assertTrue(prepared["cloned"])
        self.assertEqual("legacy-deep", prepared["layout"])
        repeated = prepare_migration_source(
            "example-user/remote-source-repo", target, client=FakeCloneClient()
        )
        self.assertFalse(repeated["cloned"])

    def test_m10_private_ciphertext_requires_real_identity_verification(self) -> None:
        root, knowledge, vault = self.legacy_instance("external-identity")
        self.add_real_content(knowledge, vault, include_document=False)
        external_identity = self.base / "basic.identity"
        shutil.copy2(
            knowledge / ".local" / "test-keys" / "basic.identity",
            external_identity,
        )
        shutil.rmtree(knowledge / ".local" / "test-keys")
        target = self.base / "current"
        plan = migration_plan(root, target)
        self.assertEqual(["basic"], plan["private_tiers"])
        self.assertEqual(["basic"], plan["missing_identity_tiers"])
        self.assertEqual(["private-tier-identities"], plan["required_inputs"])
        with self.assertRaisesRegex(KBError, "identity is required"):
            migrate_instance(
                root,
                target,
                age_path=self.age,
                confirm_content_migration=True,
            )
        migrated = migrate_instance(
            root,
            target,
            age_path=self.age,
            confirm_content_migration=True,
            identities={"basic": external_identity},
        )
        self.assertTrue(migrated["verified"])
        self.assertEqual(
            [],
            list(
                (resolve_knowledge_root(target) / ".local" / "test-keys").glob("*.identity")
            ),
        )


if __name__ == "__main__":
    unittest.main()

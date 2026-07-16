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

from kb_vault import KBError, KnowledgeVault  # noqa: E402
from kb_vault.bootstrap import initialize_instance, resolve_knowledge_root  # noqa: E402
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
    retire_source,
    retirement_plan,
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

    def add_real_content(self, knowledge: Path, vault: KnowledgeVault) -> dict[str, str]:
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
        (knowledge / "docs").mkdir(exist_ok=True)
        (knowledge / "docs" / "existing-note.md").write_text(
            "# Existing user document\n\nThis file must be copied.\n",
            encoding="utf-8",
        )
        return {"public": public["object_ids"][0], "private": private["object_ids"][0]}

    def test_m01_plan_separates_content_credentials_templates_and_retirement(self) -> None:
        root, knowledge, vault = self.legacy_instance()
        self.add_real_content(knowledge, vault)
        target = self.base / "current"
        plan = migration_plan(root, target)
        self.assertEqual("legacy-deep", plan["source_layout"])
        self.assertEqual("new-path", plan["target_state"])
        self.assertGreater(plan["counts"]["copy-content"], 0)
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
        self.assertTrue(result["verified"])
        self.assertTrue(result["source_preserved"])
        self.assertTrue(root.is_dir())
        current_knowledge = resolve_knowledge_root(target)
        self.assertEqual(target / "knowledge", current_knowledge)
        self.assertTrue((current_knowledge / "docs" / "existing-note.md").is_file())
        identities = {
            tier: current_knowledge / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        current = KnowledgeVault(current_knowledge, age_path=self.age)
        self.assertTrue(current.verify(identities=identities)["ok"])
        self.assertEqual(object_ids["private"], current.get(object_ids["private"], identities=identities)["object_id"])
        self.assertTrue(current.search_public("legacy-public-needle"))
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
        self.assertTrue(result["verified"])
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
        self.assertTrue(result["onboarding_complete"])
        self.assertEqual("complete", result["terminal_state"])
        self.assertTrue(root.exists())
        self.assertTrue((target / "knowledge" / "docs" / "existing-note.md").is_file())

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
        self.add_real_content(knowledge, vault)
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
                (target / "knowledge" / ".local" / "test-keys").glob("*.identity")
            ),
        )


if __name__ == "__main__":
    unittest.main()

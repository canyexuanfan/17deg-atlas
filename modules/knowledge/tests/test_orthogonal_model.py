from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import (  # noqa: E402
    KBError,
    GitHubRemoteInbox,
    KnowledgeVault,
    compatibility_tier_for,
    highest_classification_level,
    materialize_orthogonal_fields,
    validate_orthogonal_fields,
)
from kb_vault.bootstrap import initialize_instance  # noqa: E402
from kb_vault.core import render_markdown, sha256_text  # noqa: E402


class OrthogonalModelAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        if not configured_age:
            raise unittest.SkipTest("set KB_TEST_AGE or install age")
        cls.context = tempfile.TemporaryDirectory()
        cls.root = Path(cls.context.name) / "orthogonal-vault"
        initialize_instance(cls.root)
        cls.vault = KnowledgeVault(cls.root, age_path=configured_age)
        cls.vault.generate_test_keys(force=True)
        cls.identities = {
            tier: cls.root / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.cleanup()

    def add_object(
        self,
        *,
        request_id: str,
        tier: str,
        kind: str = "raw",
        lifecycle: str = "active",
        catalog_visibility: str = "private",
        catalog_title: str = "",
    ) -> tuple[Path, dict]:
        confirmed = tier in ("public", "archive") or catalog_visibility == "public"
        receipt = self.vault.add(
            request_id=request_id,
            tier=tier,
            kind=kind,
            title=f"Title {request_id}",
            summary=f"Summary {request_id}",
            content=f"Body {request_id}",
            lifecycle=lifecycle,
            catalog_visibility=catalog_visibility,
            catalog_title=catalog_title,
            catalog_summary="Safe teaser" if catalog_title else "",
            human_confirmed=confirmed,
        )
        path = self.vault._locate_object(receipt["object_id"])
        envelope = self.vault._read_object_path(path, self.identities)
        return path, envelope

    def test_o01_all_compatibility_kinds_use_independent_classifications(self) -> None:
        for kind in ("raw", "wiki", "release"):
            for tier in ("public", "basic", "advanced", "core"):
                _path, envelope = self.add_object(
                    request_id=f"o01-{kind}-{tier}", tier=tier, kind=kind
                )
                self.assertEqual(4, envelope["schema_version"])
                self.assertEqual(tier, envelope["classification"]["level"])
                self.assertEqual(
                    tier,
                    compatibility_tier_for(
                        classification_level=envelope["classification"]["level"],
                        lifecycle=envelope["lifecycle"],
                    ),
                )

    def test_o02_lifecycle_does_not_change_private_classification(self) -> None:
        _path, envelope = self.add_object(
            request_id="o02-private-archive", tier="basic", lifecycle="archived"
        )
        self.assertEqual("archived", envelope["lifecycle"])
        self.assertEqual("basic", envelope["classification"]["level"])
        self.assertEqual("basic", envelope["tier"])

    def test_o03_storage_backend_change_preserves_identity_and_classification(self) -> None:
        _path, envelope = self.add_object(request_id="o03-storage", tier="advanced")
        changed = copy.deepcopy(envelope)
        before = (
            changed["object_id"],
            changed["classification"],
            list(changed["policy_refs"]),
        )
        changed["storage_binding"].update(
            {
                "backend": "controlled-service",
                "content_ref": "service://knowledge/o03-storage",
                "encryption_profile": "managed",
                "key_version": "kms-v2",
            }
        )
        self.assertEqual([], validate_orthogonal_fields(changed))
        self.assertEqual(
            before,
            (
                changed["object_id"],
                changed["classification"],
                changed["policy_refs"],
            ),
        )

    def test_o04_encryption_profile_never_creates_a_grant(self) -> None:
        _path, envelope = self.add_object(request_id="o04-profile", tier="core")
        changed = copy.deepcopy(envelope)
        changed["storage_binding"].update(
            {
                "backend": "object-store",
                "content_ref": "object://private/o04-profile",
                "encryption_profile": "managed",
                "key_version": "managed-v1",
            }
        )
        self.assertEqual([], changed["policy_refs"])
        self.assertNotIn("grant", changed)
        self.assertEqual([], validate_orthogonal_fields(changed))

    def test_o05_policy_revocation_does_not_delete_the_object(self) -> None:
        path, envelope = self.add_object(request_id="o05-revoke", tier="basic")
        governed = copy.deepcopy(envelope)
        governed["policy_refs"] = ["grant:temporary-reader"]
        self.assertEqual([], validate_orthogonal_fields(governed))
        governed["policy_refs"] = []
        self.assertEqual([], validate_orthogonal_fields(governed))
        self.assertTrue(path.is_file())
        reread = self.vault._read_object_path(path, self.identities)
        self.assertEqual(envelope["content_hash"], reread["content_hash"])

    def test_o06_multiple_sources_inherit_the_highest_classification(self) -> None:
        sources = [
            {"tier": "public"},
            {"classification": {"level": "advanced"}},
            {"classification": {"level": "core"}},
        ]
        self.assertEqual("core", highest_classification_level(sources))

    def test_o07_knowledge_compilation_cannot_downgrade_a_source(self) -> None:
        _path, core = self.add_object(request_id="o07-core-source", tier="core")
        _path, public = self.add_object(request_id="o07-public-source", tier="public")
        inherited = highest_classification_level([public, core])
        self.assertEqual("core", inherited)
        self.assertNotEqual("public", inherited)

    def test_o08_archive_compatibility_never_publicizes_private_history(self) -> None:
        legacy_archive = materialize_orthogonal_fields(
            {
                "tier": "archive",
                "lifecycle": "active",
                "content_hash": sha256_text("archive"),
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "catalog_visibility": "private",
                "human_confirmed": True,
            },
            content_ref="vault/archive/raw/obj_legacy.md",
        )
        self.assertEqual("public", legacy_archive["classification"]["level"])
        self.assertEqual("archived", legacy_archive["lifecycle"])
        self.assertEqual(
            "basic",
            compatibility_tier_for(
                classification_level="basic", lifecycle="archived"
            ),
        )

    def test_o09_catalog_visibility_cannot_bypass_read_authorization(self) -> None:
        _path, envelope = self.add_object(
            request_id="o09-catalog",
            tier="basic",
            catalog_visibility="public",
            catalog_title="Safe locked teaser",
        )
        public_results = self.vault.search_public("Safe locked teaser")
        self.assertEqual(1, len(public_results))
        self.assertTrue(public_results[0]["locked"])
        self.assertNotIn("Body o09-catalog", json.dumps(public_results))
        with self.assertRaises(KBError):
            self.vault.get(envelope["object_id"])

    def test_o10_decryption_does_not_change_distribution_or_license(self) -> None:
        path, envelope = self.add_object(request_id="o10-decrypt", tier="core")
        self.assertEqual("local-only", envelope["distribution_decision"]["channel"])
        self.vault.get(envelope["object_id"], identities=self.identities)
        reread = self.vault._read_object_path(path, self.identities)
        self.assertEqual("local-only", reread["distribution_decision"]["channel"])
        self.assertIsNone(reread["distribution_decision"]["license_id"])

    def test_o11_legacy_dual_read_does_not_rewrite_bytes_and_new_writes_use_v4(self) -> None:
        legacy_content = "Legacy v1 body"
        legacy = {
            "schema_version": 1,
            "object_id": "obj_legacy_dual_read",
            "object_kind": "raw",
            "tier": "public",
            "title": "Legacy",
            "summary": "Legacy summary",
            "source_ids": [],
            "source_uri": "",
            "content_hash": sha256_text(legacy_content),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "rights": "owned",
            "maturity": "seed",
            "lifecycle": "active",
            "catalog_visibility": "private",
            "human_confirmed": True,
            "content": legacy_content,
        }
        path = self.root / "vault" / "public" / "raw" / "obj_legacy_dual_read.md"
        path.write_text(render_markdown(legacy), encoding="utf-8")
        original = path.read_bytes()
        normalized = self.vault._read_object_path(path)
        self.assertEqual(1, normalized["schema_version"])
        self.assertEqual("public", normalized["classification"]["level"])
        self.assertEqual("plaintext", normalized["storage_binding"]["encryption_profile"])
        self.assertEqual(original, path.read_bytes())
        _new_path, current = self.add_object(request_id="o11-v2", tier="public")
        self.assertEqual(4, current["schema_version"])

    def test_o12_one_contract_accepts_domain_specific_policy_and_storage(self) -> None:
        _path, envelope = self.add_object(request_id="o12-domains", tier="advanced")
        for policy_ref, content_ref in (
            ("policy:personal-owner", "service://personal/object"),
            ("policy:enterprise-role-review", "service://enterprise/object"),
            ("policy:statutory-isolation", "service://isolated/object"),
        ):
            adapted = copy.deepcopy(envelope)
            adapted["policy_refs"] = [policy_ref]
            adapted["storage_binding"].update(
                {
                    "backend": "controlled-service",
                    "content_ref": content_ref,
                    "encryption_profile": "managed",
                    "key_version": "managed-v1",
                }
            )
            self.assertEqual([], validate_orthogonal_fields(adapted))
            self.assertEqual("advanced", adapted["classification"]["level"])

    def test_o13_human_access_and_lifecycle_compile_to_compatibility_storage(self) -> None:
        private_receipt = self.vault.add_by_access(
            request_id="o13-private-archived",
            access="basic",
            lifecycle="archived",
            kind="raw",
            title="Archived private knowledge",
            summary="",
            content="private archive",
            rights="owned",
        )
        private_path = self.vault._locate_object(private_receipt["object_id"])
        private = self.vault._read_object_path(private_path, self.identities)
        self.assertEqual("basic", private["tier"])
        self.assertEqual("basic", private["classification"]["level"])
        self.assertEqual("archived", private["lifecycle"])

        public_receipt = self.vault.add_by_access(
            request_id="o13-public-archived",
            access="public",
            lifecycle="archived",
            kind="raw",
            title="Archived public knowledge",
            summary="",
            content="public archive",
            rights="owned",
            human_confirmed=True,
        )
        public_path = self.vault._locate_object(public_receipt["object_id"])
        public = self.vault._read_object_path(public_path)
        self.assertEqual("archive", public["tier"])
        self.assertEqual("public", public["classification"]["level"])
        self.assertEqual("archived", public["lifecycle"])

        with self.assertRaises(KBError):
            self.vault.add_by_access(
                request_id="o13-invalid-access",
                access="archive",
                kind="raw",
                title="Invalid",
                summary="",
                content="invalid",
            )

    def test_remote_inbox_event_carries_orthogonal_fields_without_identity(self) -> None:
        class Adapter:
            owner = "example"
            repo = "knowledge"

        inbox = GitHubRemoteInbox(Adapter())
        event = inbox.build_event(
            request_id="remote-orthogonal",
            agent_id="remote-agent",
            tier="public",
            kind="raw",
            title="Remote",
            summary="Remote summary",
            content=b"remote body",
            human_confirmed=True,
        )
        envelope = json.loads(event["payload"].decode("utf-8"))
        self.assertEqual("public", envelope["classification"]["level"])
        self.assertEqual("github", envelope["storage_binding"]["backend"])
        self.assertEqual("local-only", envelope["distribution_decision"]["channel"])
        collaboration = inbox.build_event(
            request_id="remote-collaboration",
            agent_id="remote-agent",
            tier="public",
            kind="raw",
            title="Remote collaboration",
            summary="Controlled audience",
            content=b"remote collaboration body",
            human_confirmed=True,
            distribution_channel="controlled-channel",
            distribution_audience=["team:trusted-partners"],
        )
        collaboration_envelope = json.loads(collaboration["payload"].decode("utf-8"))
        self.assertEqual(
            ["team:trusted-partners"],
            collaboration_envelope["distribution_decision"]["audience"],
        )
        self.assertNotIn("identity", json.dumps(envelope).casefold())


if __name__ == "__main__":
    unittest.main()

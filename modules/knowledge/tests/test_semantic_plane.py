from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import KBError, KnowledgeCurator, KnowledgeVault  # noqa: E402
from kb_vault.bootstrap import initialize_instance  # noqa: E402
from kb_vault.core import render_markdown, sha256_text  # noqa: E402
from kb_vault.model import new_orthogonal_fields  # noqa: E402


class SemanticPlaneAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        if not configured_age:
            raise unittest.SkipTest("set KB_TEST_AGE or install age")
        cls.context = tempfile.TemporaryDirectory()
        cls.root = Path(cls.context.name) / "semantic-vault"
        initialize_instance(cls.root)
        cls.vault = KnowledgeVault(cls.root, age_path=configured_age)
        cls.vault.generate_test_keys(force=True)
        cls.curator = KnowledgeCurator(cls.vault)
        cls.identities = {
            tier: cls.root / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        source = cls.curator.register_source(
            request_id="s-source-main",
            source_kind="website",
            name="Example source",
            locator="https://example.invalid/source",
            tier="basic",
            rights_default="owned",
        )
        cls.source_id = source["source_id"]
        cls.subscription = cls.curator.subscribe(
            request_id="s-subscription-main",
            source_id=cls.source_id,
            capture_purpose="deep_learning",
            frequency="weekly",
            identities=cls.identities,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.cleanup()

    def read(self, object_id: str) -> tuple[Path, dict]:
        path = self.vault._locate_object(object_id)
        return path, self.vault._read_object_path(path, self.identities)

    def capture(self, request_id: str, content: str, *, tier: str = "basic") -> dict:
        return self.curator.capture(
            request_id=request_id,
            source_id=self.source_id,
            title=f"Title {request_id}",
            content=content,
            media_type="article",
            capture_purpose="deep_learning",
            locator=f"https://example.invalid/{request_id}",
            tier=tier,
            rights="owned",
            identities=self.identities,
        )

    def test_s01_source_capture_and_raw_are_separate_encrypted_objects(self) -> None:
        result = self.capture("s01", "S01 original evidence")
        _event_path, event = self.read(result["capture_event_object_id"])
        raw_path, raw = self.read(result["raw_object_id"])
        source_objects = [
            item for item in self.curator._accessible_envelopes(self.identities)
            if item["object_kind"] == "source_profile"
        ]
        _subscription_path, subscription = self.read(
            self.subscription["subscription_object_id"]
        )
        self.assertTrue(source_objects)
        self.assertEqual("subscription", subscription["object_kind"])
        self.assertNotEqual(subscription["object_id"], event["object_id"])
        self.assertEqual("capture_event", event["object_kind"])
        self.assertEqual("raw", raw["object_kind"])
        self.assertTrue(raw_path.suffix == ".age")
        self.assertNotIn(b"S01 original evidence", raw_path.read_bytes())

    def test_s02_duplicate_rejected_and_failed_inputs_do_not_create_raw(self) -> None:
        first = self.capture("s02-first", "S02 same evidence")
        duplicate = self.capture("s02-duplicate", "S02 same evidence")
        rejected = self.curator.capture(
            request_id="s02-rejected",
            source_id=self.source_id,
            title="Rejected",
            content="Rejected input",
            media_type="article",
            capture_purpose="reference",
            decision="reject",
            identities=self.identities,
        )
        failed = self.curator.capture(
            request_id="s02-failed",
            source_id=self.source_id,
            title="Failed",
            content="",
            media_type="article",
            capture_purpose="reference",
            identities=self.identities,
        )
        self.assertTrue(first["raw_created"])
        self.assertEqual("duplicate", duplicate["capture_state"])
        self.assertFalse(duplicate["raw_created"])
        self.assertEqual(first["raw_object_id"], duplicate["raw_object_id"])
        self.assertIsNone(rejected["raw_object_id"])
        self.assertIsNone(failed["raw_object_id"])
        with self.assertRaises(KBError):
            self.curator.capture(
                request_id="s02-unknown-source",
                source_id="src_unknown",
                title="Unknown",
                content="Must not enter raw.",
                media_type="article",
                capture_purpose="reference",
                identities=self.identities,
            )

    def test_s03_s04_s05_curation_preserves_raw_and_creates_distinct_candidates(self) -> None:
        captured = self.capture("s03", "First paragraph.\n\nSecond paragraph remains evidence.")
        raw_path, before = self.read(captured["raw_object_id"])
        original_bytes = raw_path.read_bytes()
        result = self.curator.curate(
            request_id="s03-curate",
            raw_object_ids=[captured["raw_object_id"]],
            card_question="原始证据与摘要应当是什么关系？",
            card_answer="摘要只能派生，不能覆盖原始证据。",
            card_kind="practice",
            topic_names=["知识证据"],
            identities=self.identities,
        )
        _summary_path, summary = self.read(result["source_summary_id"])
        _card_path, card = self.read(result["atomic_card_id"])
        _after_path, after = self.read(captured["raw_object_id"])
        self.assertEqual(original_bytes, raw_path.read_bytes())
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertEqual("source_summary", summary["wiki_kind"])
        self.assertEqual("atomic_card", card["wiki_kind"])
        self.assertEqual("practice", card["card_kind"])
        self.assertIn("## 问题", card["content"])
        self.assertIn("？", card["title"])

    def test_s06_relations_have_type_statement_evidence_and_candidate_review(self) -> None:
        captured = self.capture("s06", "Relations need evidence and explanation.")
        result = self.curator.curate(
            request_id="s06-curate",
            raw_object_ids=[captured["raw_object_id"]],
            topic_names=["关系治理"],
            identities=self.identities,
        )
        for object_id in (
            result["source_summary_id"],
            result["atomic_card_id"],
            result["topic_pages"][0]["object_id"],
        ):
            _path, envelope = self.read(object_id)
            self.assertTrue(envelope["relations"])
            for relation in envelope["relations"]:
                self.assertTrue(relation["type"])
                self.assertTrue(relation["statement"])
                self.assertTrue(relation["evidence_ids"])
                self.assertEqual("candidate", relation["review_state"])

    def test_s07_s09_semantic_security_and_state_axes_remain_independent(self) -> None:
        captured = self.capture("s07", "Independent axes", tier="advanced")
        _event_path, event = self.read(captured["capture_event_object_id"])
        _raw_path, raw = self.read(captured["raw_object_id"])
        self.assertEqual("article", raw["media_type"])
        self.assertEqual("advanced", raw["classification"]["level"])
        self.assertEqual("accepted", event["capture_state"])
        self.assertEqual("uncompiled", raw["compile_state"])
        self.assertEqual("candidate", raw["review_state"])
        self.assertEqual("seed", raw["maturity"])
        self.assertEqual("active", raw["lifecycle"])

    def test_s08_s11_topics_are_stable_suggestions_and_never_auto_canonical(self) -> None:
        captured = self.capture("s08", "Topic proposal evidence")
        first = self.curator.curate(
            request_id="s08-curate-a",
            raw_object_ids=[captured["raw_object_id"]],
            topic_names=["Agent 原生知识库"],
            identities=self.identities,
        )
        second = self.curator.curate(
            request_id="s08-curate-b",
            raw_object_ids=[captured["raw_object_id"]],
            topic_names=["Agent 原生知识库"],
            identities=self.identities,
        )
        self.assertEqual(first["topic_pages"][0]["topic_id"], second["topic_pages"][0]["topic_id"])
        self.assertFalse(first["canonical"])
        self.assertEqual("candidate", first["review_state"])
        self.assertFalse(self.vault.public_catalog_records())

    def test_s13_multiple_raws_inherit_the_highest_classification(self) -> None:
        basic = self.capture("s13-basic", "Basic evidence", tier="basic")
        core = self.capture("s13-core", "Core evidence", tier="core")
        result = self.curator.curate(
            request_id="s13-curate",
            raw_object_ids=[basic["raw_object_id"], core["raw_object_id"]],
            identities=self.identities,
        )
        self.assertEqual("core", result["classification"])
        _path, card = self.read(result["atomic_card_id"])
        self.assertEqual("core", card["classification"]["level"])

    def test_s15_v2_dual_read_adds_semantics_without_rewriting_bytes(self) -> None:
        content = "Legacy v2 evidence"
        now = "2026-01-01T00:00:00Z"
        envelope = {
            "schema_version": 2,
            "object_id": "obj_semantic_legacy_v2",
            "object_kind": "raw",
            "tier": "public",
            "title": "Legacy v2",
            "summary": "",
            "source_ids": [],
            "source_uri": "",
            "content_hash": sha256_text(content),
            "created_at": now,
            "updated_at": now,
            "rights": "owned",
            "maturity": "seed",
            "lifecycle": "active",
            "catalog_visibility": "private",
            "human_confirmed": True,
            "content": content,
        }
        envelope.update(
            new_orthogonal_fields(
                tier="public",
                lifecycle="active",
                content_ref="vault/public/raw/obj_semantic_legacy_v2.md",
                content_hash=envelope["content_hash"],
                catalog_visibility="private",
                human_confirmed=True,
                timestamp=now,
            )
        )
        path = self.root / "vault" / "public" / "raw" / "obj_semantic_legacy_v2.md"
        path.write_text(render_markdown(envelope), encoding="utf-8")
        before = path.read_bytes()
        loaded = self.vault._read_object_path(path)
        self.assertEqual(2, loaded["schema_version"])
        self.assertIsNone(loaded["media_type"])
        self.assertEqual("uncompiled", loaded["compile_state"])
        self.assertEqual(before, path.read_bytes())

    def test_s15_ambiguous_v1_wiki_is_not_guessed_or_rewritten(self) -> None:
        content = "Legacy wiki with no reliable semantic subtype"
        envelope = {
            "schema_version": 1,
            "object_id": "obj_semantic_legacy_wiki",
            "object_kind": "wiki",
            "tier": "public",
            "title": "Legacy wiki",
            "summary": "Insufficient evidence to infer a wiki subtype.",
            "source_ids": [],
            "source_uri": "",
            "content_hash": sha256_text(content),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "rights": "owned",
            "maturity": "seed",
            "lifecycle": "active",
            "catalog_visibility": "private",
            "human_confirmed": False,
            "content": content,
        }
        path = self.root / "vault" / "public" / "wiki" / "obj_semantic_legacy_wiki.md"
        path.write_text(render_markdown(envelope), encoding="utf-8")
        before = path.read_bytes()
        loaded = self.vault._read_object_path(path)
        self.assertEqual(1, loaded["schema_version"])
        self.assertIsNone(loaded["wiki_kind"])
        self.assertIsNone(loaded["card_kind"])
        self.assertEqual("candidate", loaded["review_state"])
        self.assertEqual(before, path.read_bytes())

    def test_schema_file_is_v4_and_has_no_unknown_properties(self) -> None:
        schema = json.loads(
            (self.root / "config" / "schemas" / "object-envelope.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual([1, 2, 3, 4], schema["properties"]["schema_version"]["enum"])
        self.assertFalse(schema["additionalProperties"])
        v4_required = schema["allOf"][-1]["then"]["required"]
        self.assertNotIn("interaction_refs", v4_required)

    def test_conversation_knowledge_extract_requires_message_level_sources(self) -> None:
        with self.assertRaises(KBError):
            self.vault.add(
                request_id="s-conversation-without-message-ref",
                tier="basic",
                kind="raw",
                title="Unsafe full conversation import",
                summary="",
                content="conversation",
                media_type="conversation",
                origin_kind="mixed",
                authorship_status="coauthored",
                intended_role="evidence",
                rights="restricted",
            )
        receipt = self.vault.add(
            request_id="s-conversation-extract",
            tier="basic",
            kind="raw",
            title="Selected conversation evidence",
            summary="",
            content="minimal extract",
            media_type="conversation",
            origin_kind="mixed",
            authorship_status="coauthored",
            intended_role="knowledge",
            rights="restricted",
            source_refs=["memory:chat_01"],
            interaction_refs=["chat_01#msg_03", "chat_01#msg_04"],
        )
        _path, envelope = self.read(receipt["object_id"])
        self.assertEqual(
            ["chat_01#msg_03", "chat_01#msg_04"],
            envelope["interaction_refs"],
        )


if __name__ == "__main__":
    unittest.main()

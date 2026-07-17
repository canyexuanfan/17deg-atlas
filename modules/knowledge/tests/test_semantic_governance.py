from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import KBError, KnowledgeCurator, KnowledgeCycle, KnowledgeVault  # noqa: E402
from kb_vault.bootstrap import initialize_instance  # noqa: E402
from kb_vault.core import stable_token  # noqa: E402
from kb_vault.semantic import governance_requirements  # noqa: E402


class SemanticGovernanceAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        if not configured_age:
            self.skipTest("set KB_TEST_AGE or install age")
        self.context = tempfile.TemporaryDirectory()
        self.age = Path(configured_age).resolve()
        self.root = Path(self.context.name) / "governance-vault"
        initialize_instance(self.root)
        self.vault = KnowledgeVault(self.root, age_path=self.age)
        self.vault.generate_test_keys(force=True)
        self.identities = {
            tier: self.root / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        self.curator = KnowledgeCurator(self.vault)
        self.cycle = KnowledgeCycle(self.vault)
        source = self.curator.register_source(
            request_id="governance-source",
            source_kind="website",
            name="Governance source",
            locator="https://example.invalid/governance",
            tier="basic",
            rights_default="owned",
        )
        captured = self.curator.capture(
            request_id="governance-capture",
            source_id=source["source_id"],
            title="Governance evidence",
            content="Evidence remains immutable. Feedback can improve applicability.",
            media_type="article",
            capture_purpose="deep_learning",
            tier="basic",
            rights="owned",
            identities=self.identities,
        )
        self.raw_id = captured["raw_object_id"]
        self.curated = self.curator.curate(
            request_id="governance-curate",
            raw_object_ids=[self.raw_id],
            card_question="反馈怎样改进知识而不覆盖证据？",
            card_answer="反馈形成追加事件，并触发适用范围投影或重新编译。",
            card_kind="practice",
            topic_names=["知识反馈闭环"],
            identities=self.identities,
        )

    def tearDown(self) -> None:
        self.context.cleanup()

    def read(self, object_id: str) -> tuple[Path, dict]:
        path = self.vault._locate_object(object_id)
        return path, self.vault._read_object_path(path, self.identities)

    def semantic_json(self, name: str) -> dict:
        return json.loads(
            (self.root / ".local" / "semantic" / name).read_text(encoding="utf-8")
        )

    def semantic_jsonl(self, name: str) -> list[dict]:
        path = self.root / ".local" / "semantic" / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def run_cli(self, *args: str) -> dict:
        root_cli = MODULE_ROOT.parents[1] / "scripts" / "17deg-atlas.py"
        result = subprocess.run(
            [
                sys.executable,
                str(root_cli),
                "knowledge",
                "--root",
                str(self.root),
                "--age-path",
                str(self.age),
                *args,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_s08_taxonomy_requires_proposal_confirmation_and_preserves_ids(self) -> None:
        proposal = self.curated["taxonomy_proposals"][0]
        self.cycle.rebuild_state(self.identities)
        initial = self.semantic_json("taxonomy.json")
        topic = next(item for item in initial["topics"] if item["topic_id"] == proposal["topic_id"])
        self.assertEqual("proposed", topic["state"])
        with self.assertRaises(KBError):
            self.cycle.review_topic(
                request_id="taxonomy-review-denied",
                proposal_object_id=proposal["proposal_object_id"],
                decision="active",
                human_confirmed=False,
                identities=self.identities,
            )
        reviewed = self.cycle.review_topic(
            request_id="taxonomy-review-active",
            proposal_object_id=proposal["proposal_object_id"],
            decision="active",
            human_confirmed=True,
            identities=self.identities,
        )
        self.assertEqual(proposal["topic_id"], reviewed["topic_id"])
        registry = self.semantic_json("taxonomy.json")
        active = next(item for item in registry["topics"] if item["topic_id"] == proposal["topic_id"])
        self.assertEqual("active", active["state"])

        merged_proposal = self.cycle.propose_topic(
            request_id="taxonomy-propose-merge",
            name="反馈型知识库",
            definition="知识反馈闭环的候选别名主题。",
            evidence_ids=[self.curated["atomic_card_id"]],
            identities=self.identities,
        )
        self.cycle.review_topic(
            request_id="taxonomy-review-merge",
            proposal_object_id=merged_proposal["proposal_object_id"],
            decision="merged",
            successor_ids=[proposal["topic_id"]],
            human_confirmed=True,
            identities=self.identities,
        )
        registry = self.semantic_json("taxonomy.json")
        merged = next(
            item for item in registry["topics"] if item["topic_id"] == merged_proposal["topic_id"]
        )
        self.assertEqual("merged", merged["state"])
        self.assertEqual([proposal["topic_id"]], merged["successor_ids"])

    def test_s00_schemas_cover_taxonomy_feedback_and_rebuildable_state(self) -> None:
        schema_root = self.root / "config" / "schemas"
        expected = {
            "taxonomy-event.schema.json": "kb://schemas/taxonomy-event-v1",
            "feedback-event.schema.json": "kb://schemas/feedback-event-v1",
            "governance-event.schema.json": "kb://schemas/governance-event-v1",
            "semantic-state.schema.json": "kb://schemas/semantic-state-v1",
        }
        for name, schema_id in expected.items():
            schema = json.loads((schema_root / name).read_text(encoding="utf-8"))
            self.assertEqual(schema_id, schema["$id"])
            self.assertFalse(schema["additionalProperties"])

    def test_s06_relation_review_is_append_only_and_causal_verification_is_stricter(self) -> None:
        card_path, card = self.read(self.curated["atomic_card_id"])
        original = card_path.read_bytes()
        relation_id = card["relations"][0]["relation_id"]
        with self.assertRaises(KBError):
            self.cycle.review_relation(
                request_id="relation-review-denied",
                source_object_id=card["object_id"],
                relation_id=relation_id,
                decision="verified",
                human_confirmed=False,
                identities=self.identities,
            )
        self.cycle.review_relation(
            request_id="relation-review-verified",
            source_object_id=card["object_id"],
            relation_id=relation_id,
            decision="verified",
            human_confirmed=True,
            identities=self.identities,
        )
        relations = self.semantic_jsonl("relations.jsonl")
        projected = next(item for item in relations if item["relation_id"] == relation_id)
        self.assertEqual("verified", projected["effective_review_state"])
        self.assertEqual(original, card_path.read_bytes())

        causal_relation = {
            "relation_id": stable_token("rel", "single-evidence-cause"),
            "type": "causes",
            "target_id": card["object_id"],
            "statement": "单一证据暂不足以确认因果关系。",
            "evidence_ids": [self.raw_id],
            "review_state": "candidate",
            "created_by": "agent",
        }
        causal = self.vault.add(
            request_id="causal-candidate",
            tier="basic",
            kind="wiki",
            title="因果候选是否成立？",
            summary="因果候选",
            content="需要更多独立证据。",
            source_ids=[self.raw_id],
            rights="owned",
            maturity="draft",
            catalog_visibility="none",
            wiki_kind="atomic_card",
            card_kind="claim",
            relations=[causal_relation],
            compile_state="compiled",
            review_state="candidate",
        )
        with self.assertRaises(KBError):
            self.cycle.review_relation(
                request_id="causal-review",
                source_object_id=causal["object_id"],
                relation_id=causal_relation["relation_id"],
                decision="verified",
                human_confirmed=True,
                identities=self.identities,
            )

    def test_s10_review_package_and_semantic_projections_are_rebuildable(self) -> None:
        package_path, package = self.read(self.curated["review_package_object_id"])
        package_payload = json.loads(package["content"])
        self.assertEqual("review-package", package_payload["event_type"])
        self.assertTrue(package_payload["checks"]["all_candidates"])
        self.assertFalse(package_payload["checks"]["canonical_changes"])
        self.assertTrue(package_path.suffix == ".age")

        first = self.cycle.rebuild_state(self.identities)
        before = {
            path.name: path.read_bytes()
            for path in (self.root / ".local" / "semantic").iterdir()
            if path.is_file()
        }
        shutil.rmtree(self.root / ".local" / "semantic")
        second = self.cycle.rebuild_state(self.identities)
        after = {
            path.name: path.read_bytes()
            for path in (self.root / ".local" / "semantic").iterdir()
            if path.is_file()
        }
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_s11_run_output_stays_candidate_and_never_becomes_canonical(self) -> None:
        result = self.cycle.record_run(
            request_id="knowledge-run",
            title="Use the feedback card",
            operation="answer-query",
            outcome="success",
            input_object_ids=[self.curated["atomic_card_id"]],
            notes="The result still needs human review.",
            identities=self.identities,
        )
        _path, run = self.read(result["run_object_id"])
        self.assertEqual("run", run["object_kind"])
        self.assertEqual("candidate", run["review_state"])
        self.assertFalse(result["canonical"])
        self.assertFalse(run["human_confirmed"])

    def test_s09_candidate_review_builds_canonical_view_without_rewriting_knowledge(self) -> None:
        card_path, card = self.read(self.curated["atomic_card_id"])
        original = card_path.read_bytes()
        with self.assertRaises(KBError):
            self.cycle.review_candidate(
                request_id="knowledge-review-denied",
                candidate_object_id=card["object_id"],
                decision="verified",
                human_confirmed=False,
                identities=self.identities,
            )
        reviewed = self.cycle.review_candidate(
            request_id="knowledge-review-verified",
            candidate_object_id=card["object_id"],
            decision="verified",
            human_confirmed=True,
            identities=self.identities,
        )
        self.assertTrue(reviewed["canonical"])
        knowledge = self.semantic_jsonl("knowledge.jsonl")
        projected = next(item for item in knowledge if item["object_id"] == card["object_id"])
        self.assertEqual("verified", projected["effective_review_state"])
        self.assertTrue(projected["canonical"])
        self.assertEqual(original, card_path.read_bytes())

    def test_s11_health_check_reports_without_rewriting_knowledge(self) -> None:
        orphan = self.vault.add(
            request_id="orphan-claim",
            tier="basic",
            kind="wiki",
            title="Unsupported claim",
            summary="A deliberately unsupported candidate for health checking.",
            content="This candidate has no evidence reference.",
            source_ids=[],
            rights="owned",
            maturity="seed",
            catalog_visibility="none",
            wiki_kind="atomic_card",
            card_kind="claim",
            compile_state="compiled",
            review_state="candidate",
            action="test-fixture",
        )
        orphan_path, _envelope = self.read(orphan["object_id"])
        original = orphan_path.read_bytes()
        result = self.cycle.health_check(
            request_id="semantic-health",
            identities=self.identities,
        )
        self.assertFalse(result["knowledge_modified"])
        self.assertFalse(result["canonical"])
        codes = {item["code"] for item in result["issues"]}
        self.assertIn("orphan-knowledge", codes)
        self.assertIn("claim-without-evidence", codes)
        _path, run = self.read(result["health_run_object_id"])
        self.assertEqual("run", run["object_kind"])
        self.assertEqual("candidate", run["review_state"])
        self.assertEqual(original, orphan_path.read_bytes())

    def test_s12_feedback_adjusts_scope_queues_and_recompiles_without_overwrite(self) -> None:
        card_path, card = self.read(self.curated["atomic_card_id"])
        original = card_path.read_bytes()
        run = self.cycle.record_run(
            request_id="feedback-run",
            title="Apply feedback knowledge",
            operation="real-task",
            outcome="partial",
            input_object_ids=[card["object_id"]],
            identities=self.identities,
        )
        candidate_feedback = self.cycle.record_feedback(
            request_id="feedback-candidate",
            target_object_id=card["object_id"],
            run_object_id=run["run_object_id"],
            outcome="partial",
            notes="This unconfirmed suggestion must not affect active views.",
            scope_add=["仅用于个人知识整理"],
            trigger_recompile=True,
            human_confirmed=False,
            identities=self.identities,
        )
        self.assertFalse(candidate_feedback["recompile_queued"])
        self.assertEqual([], self.semantic_jsonl("recompile-queue.jsonl"))

        confirmed = self.cycle.record_feedback(
            request_id="feedback-confirmed",
            target_object_id=card["object_id"],
            run_object_id=run["run_object_id"],
            outcome="outdated",
            notes="The scope and answer should be recompiled.",
            scope_add=["适用于候选知识"],
            trigger_recompile=True,
            human_confirmed=True,
            identities=self.identities,
        )
        queue = self.semantic_jsonl("recompile-queue.jsonl")
        self.assertEqual(1, len(queue))
        applicability = self.semantic_jsonl("applicability.jsonl")
        self.assertIn("适用于候选知识", applicability[0]["scope"])

        recompiled = self.cycle.recompile(
            request_id="feedback-recompile",
            target_object_id=card["object_id"],
            feedback_object_ids=[confirmed["feedback_object_id"]],
            card_question="反馈后怎样界定知识适用范围？",
            card_answer="以已确认反馈形成适用范围投影，并从原始证据产生新候选。",
            identities=self.identities,
        )
        self.assertEqual(original, card_path.read_bytes())
        new_card_id = recompiled["candidate_object_ids"][1]
        _new_path, new_card = self.read(new_card_id)
        supersedes = [
            item for item in new_card["relations"] if item["type"] == "supersedes"
        ]
        self.assertEqual(card["object_id"], supersedes[0]["target_id"])
        self.assertEqual([], self.semantic_jsonl("recompile-queue.jsonl"))

    def test_s14_three_domains_share_semantics_but_not_governance_authority(self) -> None:
        personal = governance_requirements("personal")
        enterprise = governance_requirements("enterprise")
        public = governance_requirements("public-national")
        self.assertNotEqual(personal["canonical_authority"], enterprise["canonical_authority"])
        self.assertNotEqual(enterprise["taxonomy_authority"], public["taxonomy_authority"])
        self.assertIn("role-separation", enterprise["required_controls"])
        self.assertIn("appeal", public["required_controls"])
        with self.assertRaises(ValueError):
            governance_requirements("unknown")

    def test_s07_public_knowledge_never_makes_governance_events_public_by_inheritance(self) -> None:
        public_raw = self.vault.add(
            request_id="public-governance-raw",
            tier="public",
            kind="raw",
            title="Public evidence",
            summary="",
            content="Public evidence does not make internal feedback public.",
            rights="owned",
            maturity="seed",
            catalog_visibility="private",
            human_confirmed=True,
            media_type="article",
            compile_state="uncompiled",
            review_state="candidate",
        )
        relation = {
            "relation_id": stable_token("rel", "public-governance-relation"),
            "type": "derived_from",
            "target_id": public_raw["object_id"],
            "statement": "The public card derives from public evidence.",
            "evidence_ids": [public_raw["object_id"]],
            "review_state": "candidate",
            "created_by": "agent",
        }
        public_card = self.vault.add(
            request_id="public-governance-card",
            tier="public",
            kind="wiki",
            title="Can public evidence have private governance?",
            summary="Yes.",
            content="Governance events remain private candidates.",
            source_ids=[public_raw["object_id"]],
            rights="owned",
            maturity="reviewed",
            catalog_visibility="private",
            human_confirmed=True,
            wiki_kind="atomic_card",
            card_kind="claim",
            relations=[relation],
            compile_state="compiled",
            review_state="reviewed",
        )
        feedback = self.cycle.record_feedback(
            request_id="public-governance-feedback",
            target_object_id=public_card["object_id"],
            outcome="partial",
            notes="Internal applicability note.",
            human_confirmed=False,
            identities=self.identities,
        )
        review = self.cycle.review_relation(
            request_id="public-governance-relation-review",
            source_object_id=public_card["object_id"],
            relation_id=relation["relation_id"],
            decision="reviewed",
            human_confirmed=True,
            identities=self.identities,
        )
        for object_id in (feedback["feedback_object_id"], review["review_object_id"]):
            path, envelope = self.read(object_id)
            self.assertEqual("basic", envelope["tier"])
            self.assertEqual(".age", path.suffix)
        self.assertFalse(self.vault.public_catalog_records())

    def test_s16_lock_clears_rebuildable_semantic_views_but_preserves_events(self) -> None:
        self.cycle.rebuild_state(self.identities)
        proposal_id = self.curated["taxonomy_proposals"][0]["proposal_object_id"]
        proposal_path, _proposal = self.read(proposal_id)
        proposal_bytes = proposal_path.read_bytes()
        self.assertTrue((self.root / ".local" / "semantic" / "taxonomy.json").is_file())
        self.vault.lock()
        self.assertFalse((self.root / ".local" / "semantic" / "taxonomy.json").exists())
        self.assertEqual(proposal_bytes, proposal_path.read_bytes())
        rebuilt = self.cycle.rebuild_state(self.identities)
        self.assertGreaterEqual(rebuilt["topics"], 1)

    def test_s03_s12_cli_runs_the_complete_kb2_cycle(self) -> None:
        source = self.run_cli(
            "source-register",
            "--request-id",
            "cli-source",
            "--source-kind",
            "project",
            "--name",
            "CLI source",
            "--locator",
            "https://example.invalid/cli",
            "--rights",
            "owned",
        )
        subscription = self.run_cli(
            "subscribe",
            "--request-id",
            "cli-subscribe",
            "--source-id",
            source["source_id"],
            "--capture-purpose",
            "case",
            "--frequency",
            "weekly",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        self.assertTrue(subscription["active"])
        capture = self.run_cli(
            "capture",
            "--request-id",
            "cli-capture",
            "--source-id",
            source["source_id"],
            "--title",
            "CLI evidence",
            "--content",
            "CLI evidence must remain recoverable.",
            "--media-type",
            "article",
            "--capture-purpose",
            "case",
            "--rights",
            "owned",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        curated = self.run_cli(
            "curate",
            "--request-id",
            "cli-curate",
            "--raw-object-id",
            capture["raw_object_id"],
            "--topic",
            "CLI 知识闭环",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        standalone_proposal = self.run_cli(
            "taxonomy-propose",
            "--request-id",
            "cli-taxonomy-propose",
            "--name",
            "CLI governance",
            "--definition",
            "A standalone CLI taxonomy proposal.",
            "--evidence-id",
            curated["atomic_card_id"],
            "--identity-basic",
            str(self.identities["basic"]),
        )
        self.assertFalse(standalone_proposal["active"])
        proposal = curated["taxonomy_proposals"][0]
        self.run_cli(
            "taxonomy-review",
            "--request-id",
            "cli-taxonomy-review",
            "--proposal-object-id",
            proposal["proposal_object_id"],
            "--decision",
            "active",
            "--human-confirmed",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        review_package = self.run_cli(
            "review-package",
            "--request-id",
            "cli-review-package",
            "--candidate-object-id",
            curated["atomic_card_id"],
            "--taxonomy-proposal-id",
            proposal["proposal_object_id"],
            "--identity-basic",
            str(self.identities["basic"]),
        )
        self.assertFalse(review_package["canonical_changes"])
        card = self.read(curated["atomic_card_id"])[1]
        relation_id = card["relations"][0]["relation_id"]
        relation_review = self.run_cli(
            "relation-review",
            "--request-id",
            "cli-relation-review",
            "--source-object-id",
            curated["atomic_card_id"],
            "--relation-id",
            relation_id,
            "--decision",
            "reviewed",
            "--human-confirmed",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        self.assertEqual("reviewed", relation_review["decision"])
        knowledge_review = self.run_cli(
            "knowledge-review",
            "--request-id",
            "cli-knowledge-review",
            "--candidate-object-id",
            curated["atomic_card_id"],
            "--decision",
            "verified",
            "--human-confirmed",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        self.assertTrue(knowledge_review["canonical"])
        run = self.run_cli(
            "run-record",
            "--request-id",
            "cli-run",
            "--title",
            "CLI run",
            "--operation",
            "answer-query",
            "--outcome",
            "partial",
            "--input-object-id",
            curated["atomic_card_id"],
            "--identity-basic",
            str(self.identities["basic"]),
        )
        feedback = self.run_cli(
            "feedback-record",
            "--request-id",
            "cli-feedback",
            "--target-object-id",
            curated["atomic_card_id"],
            "--run-object-id",
            run["run_object_id"],
            "--outcome",
            "partial",
            "--scope-add",
            "CLI 场景",
            "--trigger-recompile",
            "--human-confirmed",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        recompiled = self.run_cli(
            "recompile",
            "--request-id",
            "cli-recompile",
            "--target-object-id",
            curated["atomic_card_id"],
            "--feedback-object-id",
            feedback["feedback_object_id"],
            "--identity-basic",
            str(self.identities["basic"]),
        )
        rebuilt = self.run_cli(
            "semantic-rebuild",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        health = self.run_cli(
            "health-check",
            "--request-id",
            "cli-health-check",
            "--identity-basic",
            str(self.identities["basic"]),
        )
        self.assertTrue(recompiled["old_object_preserved"])
        self.assertGreaterEqual(rebuilt["topics"], 1)
        self.assertEqual(0, rebuilt["recompile_pending"])
        self.assertFalse(health["knowledge_modified"])


if __name__ == "__main__":
    unittest.main()

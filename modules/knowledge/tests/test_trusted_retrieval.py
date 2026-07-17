from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import (  # noqa: E402
    KBError,
    KnowledgeCurator,
    KnowledgeCycle,
    KnowledgeVault,
    TrustedRetrieval,
)
from kb_vault.bootstrap import initialize_instance  # noqa: E402
from kb_vault.core import render_markdown, sha256_text, stable_token  # noqa: E402


class TrustedRetrievalAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        if not configured_age:
            raise unittest.SkipTest("set KB_TEST_AGE or install age")
        cls.context = tempfile.TemporaryDirectory()
        cls.root = Path(cls.context.name) / "trusted-retrieval-vault"
        initialize_instance(cls.root)
        cls.vault = KnowledgeVault(cls.root, age_path=Path(configured_age).resolve())
        cls.vault.generate_test_keys(force=True)
        cls.identities = {
            tier: cls.root / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        cls.curator = KnowledgeCurator(cls.vault)
        cls.cycle = KnowledgeCycle(cls.vault)
        cls.retrieval = TrustedRetrieval(cls.vault)

        source = cls.curator.register_source(
            request_id="r-source",
            source_kind="website",
            name="Trusted retrieval source",
            locator="https://example.invalid/trusted-retrieval",
            tier="basic",
            rights_default="owned",
        )
        captured = cls.curator.capture(
            request_id="r-capture",
            source_id=source["source_id"],
            title="可信检索原始证据",
            content="知识反馈闭环必须先过滤权限，再执行全文检索，并保留原始证据。",
            media_type="article",
            capture_purpose="deep_learning",
            tier="basic",
            rights="owned",
            identities=cls.identities,
        )
        cls.raw_id = captured["raw_object_id"]
        cls.curated = cls.curator.curate(
            request_id="r-curate",
            raw_object_ids=[cls.raw_id],
            summary="可信检索先授权、再搜索，并返回来源证据。",
            card_question="可信检索为什么必须先过滤权限？",
            card_answer="因为无权对象不能进入当前检索数据库，更不能通过目录或片段泄漏。",
            card_kind="practice",
            topic_names=["可信检索"],
            identities=cls.identities,
        )
        proposal = cls.curated["taxonomy_proposals"][0]
        cls.cycle.review_topic(
            request_id="r-topic-review",
            proposal_object_id=proposal["proposal_object_id"],
            decision="active",
            human_confirmed=True,
            identities=cls.identities,
        )
        cls.cycle.review_candidate(
            request_id="r-card-review",
            candidate_object_id=cls.curated["atomic_card_id"],
            decision="verified",
            human_confirmed=True,
            identities=cls.identities,
        )

        cls.public = cls.vault.add(
            request_id="r-public",
            tier="public",
            kind="wiki",
            title="Public search guidance",
            summary="A confirmed public catalog entry.",
            content="Public knowledge can be searched without a private identity.",
            source_ids=[],
            rights="owned",
            maturity="verified",
            catalog_visibility="public",
            human_confirmed=True,
            wiki_kind="atomic_card",
            card_kind="practice",
            compile_state="compiled",
            review_state="verified",
        )
        cls.collaboration = cls.vault.add(
            request_id="r-collaboration",
            tier="basic",
            kind="wiki",
            title="Partner retrieval playbook",
            summary="Shared only with a named collaboration audience.",
            content="合作检索条目只能出现在受控合作目录。",
            source_ids=[cls.raw_id],
            rights="owned",
            maturity="reviewed",
            catalog_visibility="none",
            human_confirmed=True,
            wiki_kind="atomic_card",
            card_kind="practice",
            compile_state="compiled",
            review_state="reviewed",
            distribution_channel="controlled-channel",
            distribution_audience=["team:trusted-partners"],
        )
        cls.core_secret = cls.vault.add(
            request_id="r-core-secret",
            tier="core",
            kind="raw",
            title="核心检索隐藏标题",
            summary="This title must never enter a less privileged index.",
            content="火星密室方案只允许核心身份检索。",
            rights="owned",
            maturity="seed",
            catalog_visibility="none",
            media_type="idea",
            compile_state="uncompiled",
            review_state="candidate",
        )
        old_card = cls.vault._read_object_path(
            cls.vault._locate_object(cls.curated["atomic_card_id"]), cls.identities
        )
        replacement_relations = [
            {
                "relation_id": stable_token("rel", "retrieval-supersedes"),
                "type": "supersedes",
                "target_id": old_card["object_id"],
                "statement": "The newer verified guidance replaces the earlier answer.",
                "evidence_ids": [cls.raw_id],
                "review_state": "candidate",
                "created_by": "agent",
            },
            {
                "relation_id": stable_token("rel", "retrieval-contradicts"),
                "type": "contradicts",
                "target_id": cls.collaboration["object_id"],
                "statement": "The two guidance cards disagree about the allowed directory.",
                "evidence_ids": [cls.raw_id],
                "review_state": "candidate",
                "created_by": "agent",
            },
        ]
        cls.replacement = cls.vault.add(
            request_id="r-replacement",
            tier="basic",
            kind="wiki",
            title="可信检索当前规则",
            summary="当前规则：先构造授权可见集合，再执行 FTS5。",
            content="知识反馈闭环使用独立目录数据库，并保留 raw 证据链。",
            source_ids=[cls.curated["source_summary_id"], cls.raw_id],
            rights="owned",
            maturity="reviewed",
            catalog_visibility="none",
            wiki_kind="atomic_card",
            card_kind="practice",
            topic_ids=old_card["topic_ids"],
            relations=replacement_relations,
            compile_state="compiled",
            review_state="candidate",
        )
        cls.cycle.review_candidate(
            request_id="r-replacement-review",
            candidate_object_id=cls.replacement["object_id"],
            decision="verified",
            human_confirmed=True,
            identities=cls.identities,
        )
        cls.vault.reindex()
        cls.build = cls.retrieval.build(cls.identities)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.context.cleanup()

    @classmethod
    def run_cli(cls, *args: str) -> dict:
        root_cli = MODULE_ROOT.parents[1] / "scripts" / "17deg-atlas.py"
        result = subprocess.run(
            [
                sys.executable,
                str(root_cli),
                "knowledge",
                "--root",
                str(cls.root),
                "--age-path",
                str(cls.vault.age_path),
                *args,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def test_r00_schemas_cover_state_results_queries_and_evaluation(self) -> None:
        schema_root = self.root / "config" / "schemas"
        expected = {
            "trusted-query-set.schema.json": "kb://schemas/trusted-query-set-v1",
            "trusted-search-state.schema.json": "kb://schemas/trusted-search-state-v1",
            "trusted-search-result.schema.json": "kb://schemas/trusted-search-result-v1",
            "trusted-evaluation.schema.json": "kb://schemas/trusted-evaluation-v1",
        }
        for name, schema_id in expected.items():
            schema = json.loads((schema_root / name).read_text(encoding="utf-8"))
            self.assertEqual(schema_id, schema["$id"])
            self.assertFalse(schema["additionalProperties"])
        policies = json.loads(
            (MODULE_ROOT / "templates" / "instance" / "config" / "policies.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("sqlite-fts5-v1", policies["trusted_search_backend"])
        self.assertEqual(
            ["private", "collaboration", "public"], policies["trusted_search_scopes"]
        )

    def test_r01_public_directory_only_contains_confirmed_catalog(self) -> None:
        public = self.retrieval.directory("public")
        self.assertEqual([self.public["object_id"]], [item["object_id"] for item in public])
        self.assertEqual(
            {"object_id", "object_kind", "tier", "title", "summary", "path", "locked"},
            set(public[0]),
        )

    def test_r02_collaboration_directory_requires_controlled_audience(self) -> None:
        collaboration = self.retrieval.directory("collaboration")
        ids = {item["object_id"] for item in collaboration}
        self.assertIn(self.public["object_id"], ids)
        self.assertIn(self.collaboration["object_id"], ids)
        self.assertNotIn(self.core_secret["object_id"], ids)

    def test_r03_r04_private_directory_and_database_only_include_authorized_objects(self) -> None:
        limited = self.retrieval.build({"basic": self.identities["basic"]})
        self.assertEqual(["public", "archive", "basic"], limited["authorized_tiers"])
        private_ids = {item["object_id"] for item in self.retrieval.directory("private")}
        self.assertNotIn(self.core_secret["object_id"], private_ids)
        self.assertEqual([], self.retrieval.search("火星密室", scope="private"))
        connection = sqlite3.connect(
            self.root / ".local" / "trusted-search" / "indexes" / "private.sqlite3"
        )
        try:
            count = connection.execute(
                "SELECT count(*) FROM documents WHERE object_id = ?",
                (self.core_secret["object_id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, count)
        self.retrieval.build(self.identities)

    def test_r05_r06_fts5_chinese_english_and_structured_filters(self) -> None:
        chinese = self.retrieval.search("知识反馈闭环", scope="private")
        self.assertTrue(chinese)
        self.assertIn(chinese[0]["explanation"]["match"], ("sqlite-fts5", "substring-fallback"))
        english = self.retrieval.search("Public knowledge", scope="public")
        self.assertEqual(self.public["object_id"], english[0]["object_id"])
        topic_id = self.curated["taxonomy_proposals"][0]["topic_id"]
        filtered = self.retrieval.search(
            "",
            scope="private",
            object_kinds=["wiki"],
            wiki_kinds=["atomic_card"],
            card_kinds=["practice"],
            topic_ids=[topic_id],
            review_states=["verified"],
            lifecycles=["active"],
        )
        self.assertTrue(filtered)
        self.assertTrue(all(item["card_kind"] == "practice" for item in filtered))

    def test_r07_result_traces_to_summary_and_raw(self) -> None:
        trace = self.retrieval.trace(self.replacement["object_id"])
        chain_ids = [item["object_id"] for item in trace["source_chain"]]
        self.assertIn(self.curated["source_summary_id"], chain_ids)
        self.assertIn(self.raw_id, trace["raw_source_ids"])

    def test_r08_r09_conflict_superseded_and_canonical_ranking(self) -> None:
        topic_id = self.curated["taxonomy_proposals"][0]["topic_id"]
        results = self.retrieval.search(
            "",
            scope="private",
            object_kinds=["wiki"],
            card_kinds=["practice"],
            topic_ids=[topic_id],
            review_states=["verified"],
        )
        ids = [item["object_id"] for item in results]
        self.assertIn(self.replacement["object_id"], ids)
        self.assertIn(self.curated["atomic_card_id"], ids)
        self.assertLess(ids.index(self.replacement["object_id"]), ids.index(self.curated["atomic_card_id"]))
        old = next(item for item in results if item["object_id"] == self.curated["atomic_card_id"])
        current = next(item for item in results if item["object_id"] == self.replacement["object_id"])
        self.assertIn(self.replacement["object_id"], old["superseded_by"])
        self.assertIn(self.collaboration["object_id"], current["conflicts_with"])
        self.assertTrue(current["canonical"])

    def test_r10_result_contains_snippet_and_explanation(self) -> None:
        result = self.retrieval.search("权限", scope="private")[0]
        self.assertTrue(result["snippet"])
        self.assertEqual("prebuilt-private-visible-set", result["explanation"]["permission"])
        self.assertIn("match", result["explanation"])

    def test_r11_query_set_reports_hit_recall_mrr_and_failures(self) -> None:
        query_set = self.root / ".local" / "retrieval-query-set.json"
        query_set.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cases": [
                        {
                            "query": "Public knowledge",
                            "scope": "public",
                            "top_k": 3,
                            "expected_ids": [self.public["object_id"]],
                        },
                        {
                            "query": "火星密室",
                            "scope": "private",
                            "top_k": 3,
                            "expected_ids": [self.core_secret["object_id"]],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        report = self.retrieval.evaluate(query_set)
        self.assertEqual(1.0, report["hit_at_k"])
        self.assertEqual(1.0, report["recall_at_k"])
        self.assertEqual(1.0, report["mrr"])
        self.assertEqual([], report["failures"])

    def test_r11_windows_utf8_bom_query_set_preserves_chinese_evaluation(self) -> None:
        query_set = self.root / ".local" / "retrieval-query-set-bom.json"
        query_set.write_text(
            json.dumps(
                {
                    "version": 2,
                    "cases": [
                        {
                            "query": "知识反馈闭环",
                            "scope": "private",
                            "top_k": 5,
                            "expected_ids": [self.replacement["object_id"]],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8-sig",
        )
        report = self.retrieval.evaluate(query_set)
        self.assertEqual("知识反馈闭环", report["details"][0]["query"])
        self.assertEqual(1.0, report["hit_at_k"])
        self.assertEqual(1.0, report["recall_at_k"])
        self.assertEqual(1.0, report["mrr"])

    def test_r12_rebuild_preserves_logical_fingerprints_and_directories(self) -> None:
        before = {
            scope: (self.root / ".local" / "trusted-search" / "directories" / f"{scope}.jsonl").read_bytes()
            for scope in ("private", "collaboration", "public")
        }
        first = self.retrieval.build(self.identities)
        second = self.retrieval.build(self.identities)
        for scope in ("private", "collaboration", "public"):
            self.assertEqual(
                first["indexes"][scope]["fingerprint"],
                second["indexes"][scope]["fingerprint"],
            )
            self.assertEqual(
                before[scope],
                (self.root / ".local" / "trusted-search" / "directories" / f"{scope}.jsonl").read_bytes(),
            )

    def test_r13_lock_removes_indexes_and_authorized_rebuild_restores_them(self) -> None:
        private_db = self.root / ".local" / "trusted-search" / "indexes" / "private.sqlite3"
        self.assertTrue(private_db.is_file())
        self.vault.lock()
        self.assertFalse(private_db.exists())
        with self.assertRaises(KBError):
            self.retrieval.search("知识", scope="private")
        rebuilt = self.retrieval.build(self.identities)
        self.assertTrue(private_db.is_file())
        self.assertIn("core", rebuilt["authorized_tiers"])

    def test_r14_public_directory_leak_verification(self) -> None:
        result = self.retrieval.verify_public_directory()
        self.assertTrue(result["ok"], result["issues"])
        public_text = (self.root / ".local" / "trusted-search" / "directories" / "public.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("核心检索隐藏标题", public_text)
        self.assertNotIn("可信检索", public_text)

    def test_r15_legacy_object_is_searchable_without_rewrite(self) -> None:
        content = "legacy compatible retrieval body"
        envelope = {
            "schema_version": 1,
            "object_id": "obj_retrieval_legacy_v1",
            "object_kind": "raw",
            "tier": "public",
            "title": "Legacy retrieval record",
            "summary": "",
            "source_ids": [],
            "source_uri": "",
            "content_hash": sha256_text(content),
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "rights": "owned",
            "maturity": "seed",
            "lifecycle": "active",
            "catalog_visibility": "private",
            "human_confirmed": True,
            "content": content,
        }
        path = self.root / "vault" / "public" / "raw" / "obj_retrieval_legacy_v1.md"
        path.write_text(render_markdown(envelope), encoding="utf-8")
        original = path.read_bytes()
        self.retrieval.build(self.identities)
        results = self.retrieval.search("legacy compatible", scope="private")
        self.assertEqual("obj_retrieval_legacy_v1", results[0]["object_id"])
        self.assertEqual(original, path.read_bytes())

    def test_r16_existing_jsonl_search_remains_compatible(self) -> None:
        self.vault.unlock_index(self.identities)
        results = self.vault.search("知识反馈闭环")
        self.assertTrue(results)

    def test_r16_cli_exposes_build_search_directory_trace_evaluate_and_verify(self) -> None:
        identity_args = []
        for tier in ("basic", "advanced", "core"):
            identity_args.extend([f"--identity-{tier}", str(self.identities[tier])])
        built = self.run_cli("trusted-build", *identity_args)
        self.assertEqual(3, len(built["indexes"]))
        searched = self.run_cli(
            "trusted-search", "Public knowledge", "--scope", "public", "--top-k", "3"
        )
        self.assertEqual(self.public["object_id"], searched["results"][0]["object_id"])
        directory = self.run_cli("trusted-directory", "--scope", "public")
        self.assertEqual(self.public["object_id"], directory["results"][0]["object_id"])
        traced = self.run_cli(
            "trusted-trace", self.replacement["object_id"], "--scope", "private"
        )
        self.assertIn(self.raw_id, traced["raw_source_ids"])
        query_set = self.root / ".local" / "cli-query-set.json"
        query_set.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cases": [
                        {
                            "query": "Public knowledge",
                            "scope": "public",
                            "expected_ids": [self.public["object_id"]],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        evaluated = self.run_cli("trusted-evaluate", "--query-set", str(query_set))
        self.assertEqual(1.0, evaluated["hit_at_k"])
        verified = self.run_cli("trusted-verify-public")
        self.assertTrue(verified["ok"])


if __name__ == "__main__":
    unittest.main()

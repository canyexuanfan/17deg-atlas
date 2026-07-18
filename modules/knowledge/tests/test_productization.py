from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
OFFICIAL_REPOSITORY = "https://github.com/canyexuanfan/17deg-atlas.git"
OFFICIAL_LOCAL_SKILL_URL = "https://github.com/canyexuanfan/17deg-atlas/tree/main/skills/17deg-atlas-local"
OFFICIAL_REMOTE_SKILL_URL = "https://github.com/canyexuanfan/17deg-atlas/tree/main/skills/17deg-atlas-remote"
OFFICIAL_REPOSITORY_PAGE = "https://github.com/canyexuanfan/17deg-atlas"
sys.path.insert(0, str(MODULE_ROOT / "src"))

from kb_vault import KBError, KnowledgeVault  # noqa: E402
from kb_vault.bootstrap import initialize_instance  # noqa: E402


class ProductizationAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = tempfile.TemporaryDirectory()
        self.base = Path(self.context.name)
        configured_age = os.environ.get("KB_TEST_AGE") or shutil.which("age")
        if not configured_age:
            self.skipTest("set KB_TEST_AGE or install age to run encryption acceptance tests")
        self.age = Path(configured_age).resolve()

    def tearDown(self) -> None:
        self.context.cleanup()

    def new_instance(self, name: str) -> tuple[Path, KnowledgeVault]:
        root = self.base / name
        result = initialize_instance(root)
        self.assertFalse(result["git_initialized"])
        self.assertFalse(result["credentials_created"])
        manifest = json.loads((root / "config" / "instance.json").read_text(encoding="utf-8"))
        self.assertEqual("personal", manifest["domain_kind"])
        self.assertEqual("knowledge", manifest["module_kind"])
        return root, KnowledgeVault(root, age_path=self.age)

    @staticmethod
    def git(root: Path, *args: str) -> str:
        command = ["git", "-c", f"core.excludesFile={root / '.gitignore'}", *args]
        result = subprocess.run(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def test_p01_p02_fresh_users_are_independent(self) -> None:
        alice_root, alice = self.new_instance("alice-vault")
        bob_root, bob = self.new_instance("bob-vault")
        self.assertFalse((alice_root / ".git").exists())
        self.assertFalse((bob_root / ".git").exists())

        alice_keys = alice.generate_test_keys(force=True)
        bob_keys = bob.generate_test_keys(force=True)
        self.assertNotEqual(alice_keys, bob_keys)

        alice.add(
            request_id="shared-request",
            tier="public",
            kind="raw",
            title="Alice title",
            summary="alice-only",
            content="alice body",
            catalog_visibility="public",
            human_confirmed=True,
        )
        bob.add(
            request_id="shared-request",
            tier="public",
            kind="raw",
            title="Bob title",
            summary="bob-only",
            content="bob body",
            catalog_visibility="public",
            human_confirmed=True,
        )
        self.assertIn("alice-only", (alice_root / "index.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("alice-only", (bob_root / "index.jsonl").read_text(encoding="utf-8"))

        for root, label in ((alice_root, "Alice"), (bob_root, "Bob")):
            self.git(root, "init")
            self.git(root, "config", "user.name", label)
            self.git(root, "config", "user.email", f"{label.lower()}@example.invalid")
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", f"初始化 {label} 的本地实例")
        self.assertNotEqual(self.git(alice_root, "rev-parse", "HEAD"), self.git(bob_root, "rev-parse", "HEAD"))

    def test_p03_p06_p07_product_has_no_personal_payload_or_empty_modules(self) -> None:
        personal_account = "canye" + "xuanfan"
        forbidden = ("soraa" + "igc", "F:" + "\\Trae AI")
        credential_patterns = (
            re.compile(r"AGE-SECRET-KEY-1[A-Z0-9]{40,}", re.IGNORECASE),
            re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
            re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        )
        product_files = [*MODULE_ROOT.rglob("*"), *(REPOSITORY_ROOT / "skills").rglob("*")]
        for path in product_files:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix.casefold() not in {".py", ".md", ".json", ".yml", ".yaml", ".toml"}:
                continue
            text = path.read_text(encoding="utf-8")
            sanitized = (
                text.replace(OFFICIAL_REPOSITORY, "")
                .replace(OFFICIAL_LOCAL_SKILL_URL, "")
                .replace(OFFICIAL_REMOTE_SKILL_URL, "")
                .replace(OFFICIAL_REPOSITORY_PAGE, "")
            )
            self.assertNotIn(personal_account, sanitized, path)
            for needle in forbidden:
                self.assertNotIn(needle, text, path)
            for pattern in credential_patterns:
                self.assertIsNone(pattern.search(text), path)
        for forbidden_dir in ("vault", "receipts", "recovery", "questions"):
            self.assertFalse((MODULE_ROOT / forbidden_dir).exists())
        siblings = sorted(path.name for path in MODULE_ROOT.parent.iterdir() if path.is_dir())
        self.assertEqual(["knowledge"], siblings)

    def test_p04_five_tiers_wrong_key_lock_and_snapshot_recovery(self) -> None:
        root, vault = self.new_instance("recovery-vault")
        recipients = vault.generate_test_keys(force=True)
        identities = {
            tier: root / ".local" / "test-keys" / f"{tier}.identity"
            for tier in ("basic", "advanced", "core")
        }
        created: dict[str, str] = {}
        for tier in ("public", "archive", "basic", "advanced", "core"):
            result = vault.add(
                request_id=f"add-{tier}",
                tier=tier,
                kind="raw",
                title=f"{tier} title",
                summary=f"needle-{tier}",
                content=f"body-{tier}",
                catalog_visibility="public" if tier == "public" else "none",
                human_confirmed=tier in ("public", "archive"),
                recipients=recipients,
            )
            created[tier] = result["object_id"]
        vault.unlock_index(identities)
        for tier in ("public", "basic", "advanced", "core"):
            self.assertTrue(vault.search(f"needle-{tier}"))
        with self.assertRaises(KBError):
            vault.get(created["basic"], identities={"basic": identities["core"]})
        vault.lock()
        self.assertFalse((root / ".local" / "private-index" / "authorized.jsonl").exists())

        snapshot = self.base / "snapshot"
        shutil.copytree(root, snapshot)
        object_path = vault._locate_object(created["public"])
        object_path.write_text("tampered", encoding="utf-8")
        self.assertFalse(vault.verify()["ok"])
        shutil.rmtree(root)
        shutil.copytree(snapshot, root)
        restored = KnowledgeVault(root, age_path=self.age)
        self.assertTrue(restored.verify()["ok"])

    def test_p05_instance_wrapper_consumes_product_module(self) -> None:
        instance = self.base / "cli-instance"
        initialized = subprocess.run(
            [
                sys.executable,
                str(MODULE_ROOT / "scripts" / "kb.py"),
                "init-instance",
                "--target",
                str(instance),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.assertTrue((instance / "knowledge").is_dir())
        self.assertFalse((instance / "domains" / "personal" / "tasks").exists())
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_ROOT / "scripts" / "kb.py"),
                "--root",
                str(instance),
                "verify",
            ],
            cwd=MODULE_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_project_skill_bootstrap_registers_and_rediscovers_local_instance(self) -> None:
        workspace = self.base / "skill-workspace"
        skill_scripts = REPOSITORY_ROOT / "skills" / "17deg-atlas-local" / "scripts"
        installed = subprocess.run(
            [
                sys.executable,
                str(skill_scripts / "bootstrap.py"),
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        installed_error = installed.stderr.decode("gb18030", errors="replace")
        self.assertEqual(0, installed.returncode, installed_error)
        json.loads(installed.stdout.decode("utf-8"))
        self.assertTrue((workspace / ".17deg-atlas" / "tool").is_dir())

        instance = workspace / "17deg-personal"
        setup = subprocess.run(
            [
                sys.executable,
                str(skill_scripts / "kb.py"),
                "--age-path",
                str(self.age),
                "agent-local-setup",
                "--target",
                str(instance),
                "--no-self-test",
            ],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        setup_error = setup.stderr.decode("gb18030", errors="replace")
        self.assertEqual(0, setup.returncode, setup_error)
        self.assertTrue(json.loads(setup.stdout.decode("utf-8"))["instance_registered"])
        registry = workspace / ".17deg-atlas" / "state" / "instances.json"
        self.assertTrue(registry.is_file())

        rediscovered = subprocess.run(
            [
                sys.executable,
                str(skill_scripts / "kb.py"),
                "--age-path",
                str(self.age),
                "agent-local-plan",
            ],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        rediscovered_error = rediscovered.stderr.decode("gb18030", errors="replace")
        self.assertEqual(0, rediscovered.returncode, rediscovered_error)
        self.assertEqual(
            str(instance.resolve()),
            json.loads(rediscovered.stdout.decode("utf-8"))["target"],
        )

    def test_p08_public_prose_uses_real_commands_and_minimal_disclosure(self) -> None:
        public_files = [REPOSITORY_ROOT / "README.md", *sorted((REPOSITORY_ROOT / "governance").glob("*.md"))]
        public_files.extend(sorted(MODULE_ROOT.rglob("*.md")))
        public_files.extend(sorted(MODULE_ROOT.rglob("*.yaml")))
        public_files.extend(sorted((REPOSITORY_ROOT / "skills").rglob("*.md")))
        public_files.extend(sorted((REPOSITORY_ROOT / "skills").rglob("*.yaml")))
        combined = "\n".join(path.read_text(encoding="utf-8") for path in public_files)
        combined_without_official_source = combined.replace(OFFICIAL_REPOSITORY, "").replace(
            OFFICIAL_LOCAL_SKILL_URL, ""
        ).replace(
            OFFICIAL_REMOTE_SKILL_URL, ""
        ).replace(OFFICIAL_REPOSITORY_PAGE, "")
        forbidden = (
            "canye" + "xuanfan",
            "soraa" + "igc",
            "F:" + "\\Trae AI",
            "v6.4-" + "product-baseline",
            "mother " + "repository",
            "母" + "库",
        )
        for needle in forbidden:
            self.assertNotIn(needle, combined_without_official_source)
        root_readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(OFFICIAL_LOCAL_SKILL_URL, root_readme)
        self.assertIn(OFFICIAL_REMOTE_SKILL_URL, root_readme)
        self.assertIn("只需要安装一个入口", root_readme)
        self.assertIn("工具不会猜测或自动切换", root_readme)
        self.assertIn("如果不能直接安装 GitHub 子目录", root_readme)
        self.assertIn("并加载 `skills/17deg-atlas-local`", root_readme)
        self.assertIn("并加载 `skills/17deg-atlas-remote`", root_readme)
        self.assertIn("在当前设备为我创建或连接知识库", root_readme)
        self.assertIn("需要 GitHub 操作时先向我确认", root_readme)
        self.assertIn("不要自行改成纯本地流程", root_readme)
        self.assertIn(".claude/skills/17deg-atlas-local", root_readme)
        self.assertIn(".codex/skills/17deg-atlas-local", root_readme)
        self.assertIn("不得自行发明 `.skills`", root_readme)
        self.assertIn("完整源码缓存或嵌套路径不能冒充已经安装", root_readme)
        self.assertIn("完成后直接向我报告", root_readme)
        self.assertIn("把当前远端 Agent 连接到我的知识库", root_readme)
        self.assertIn("Skill 会自动准备项目所需工具", root_readme)
        self.assertIn("普通用户无需安装 CLI 或手工输入命令", root_readme)
        self.assertNotIn("Token", root_readme)
        self.assertNotIn("mock", root_readme.lower())
        self.assertNotIn("动态授权", root_readme)
        self.assertNotIn("recipient", root_readme)
        self.assertNotIn("identity", root_readme)
        self.assertNotIn(".local/test-keys/basic.identity", root_readme)
        self.assertNotIn("python -m domains.personal", root_readme)
        self.assertNotIn(".local/identities", root_readme)

        skill_text = (MODULE_ROOT / "skills" / "github-api-file-pusher" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        frontmatter = skill_text.split("---", 2)[1]
        keys = [line.split(":", 1)[0].strip() for line in frontmatter.splitlines() if ":" in line]
        self.assertEqual(["name", "description"], keys)

        local_skill = (REPOSITORY_ROOT / "skills" / "17deg-atlas-local" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        remote_skill = (REPOSITORY_ROOT / "skills" / "17deg-atlas-remote" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        for command in (
            "knowledge trusted-build",
            "knowledge trusted-search",
            "knowledge trusted-trace",
            "knowledge trusted-evaluate",
        ):
            self.assertIn(command, local_skill)
            self.assertNotIn(command, remote_skill)
        self.assertIn("knowledge lock", local_skill)
        self.assertIn("不得因为 GitHub 操作需要确认而静默改用", local_skill)
        self.assertIn("只有用户明确要求", local_skill)
        self.assertIn("workspace migration-review", local_skill)
        self.assertIn("terminal_state=complete", local_skill)
        self.assertIn("不得自行追加 `doctor`", local_skill)
        self.assertIn("不安装到全局 Skill 目录", local_skill)
        self.assertIn(".claude/skills/17deg-atlas-local", local_skill)
        self.assertIn(".codex/skills/17deg-atlas-local", local_skill)
        self.assertIn("不得自行发明 `.skills`", local_skill)
        self.assertIn("完整仓库的嵌套副本不能冒充安装完成", local_skill)
        self.assertIn("用户未明确选择前禁止新建、连接或迁移", local_skill)
        self.assertIn("workspace migration-source", local_skill)
        self.assertIn("workspace migration-plan", local_skill)
        self.assertIn("workspace migration-start", local_skill)
        self.assertIn("existing_materials.candidate_count", local_skill)
        self.assertIn("workspace import-review", local_skill)
        self.assertIn("workspace source-plan", local_skill)
        self.assertIn("workspace source-start", local_skill)
        self.assertIn("--target <已选目标目录>", local_skill)
        self.assertIn("--repository-name <已选仓库名>", local_skill)
        self.assertIn(".codex/backups/skills", local_skill)
        self.assertIn("非 knowledge 路由确认", local_skill)
        self.assertIn("missing_identity_tiers", local_skill)
        self.assertIn("workspace retirement-plan", local_skill)
        self.assertIn("workspace retirement-start", local_skill)
        self.assertIn("新建空实例（明确不复制旧内容）", local_skill)
        self.assertIn("onboarding_complete: true", remote_skill)
        self.assertIn("terminal_state: complete", remote_skill)
        self.assertIn("用户未明确选择前禁止新建、连接或迁移", remote_skill)
        self.assertIn("execution_entry=local", remote_skill)
        self.assertIn("新建空实例不会复制旧实例的任何内容", remote_skill)


if __name__ == "__main__":
    unittest.main()

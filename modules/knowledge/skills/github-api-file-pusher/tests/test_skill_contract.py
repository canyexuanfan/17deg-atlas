from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import importlib.util
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = SKILL_ROOT.parents[1]
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
LOCAL_SKILL_ROOT = REPOSITORY_ROOT / "skills" / "17deg-atlas-local"
REMOTE_SKILL_ROOT = REPOSITORY_ROOT / "skills" / "17deg-atlas-remote"


class SkillContractTests(unittest.TestCase):
    def test_entry_skills_share_one_bootstrap_seed(self) -> None:
        local_bootstrap = (LOCAL_SKILL_ROOT / "scripts" / "bootstrap.py").read_bytes()
        remote_bootstrap = (REMOTE_SKILL_ROOT / "scripts" / "bootstrap.py").read_bytes()
        self.assertEqual(local_bootstrap, remote_bootstrap)

    def test_local_and_remote_entry_skills_are_explicit_and_wrappers_run(self) -> None:
        for runtime, root in (("local", LOCAL_SKILL_ROOT), ("remote", REMOTE_SKILL_ROOT)):
            skill = (root / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = skill.split("---", 2)[1]
            keys = [line.split(":", 1)[0].strip() for line in frontmatter.splitlines() if ":" in line]
            self.assertEqual(["name", "description"], keys)
            self.assertIn(f"name: 17deg-atlas-{runtime}", skill)
            self.assertNotIn("TODO", skill)
            self.assertNotIn("Placeholder", skill)
            self.assertIn("scripts/atlas.py", skill)
            self.assertNotIn("scripts/kb.py", skill)
            atlas = subprocess.run(
                [sys.executable, str(root / "scripts" / "atlas.py"), "--help"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, atlas.returncode, atlas.stderr)
            self.assertIn("workspace <init|plan|start>", atlas.stdout)
            self.assertIn("knowledge <command>", atlas.stdout)
            self.assertIn("remote <command>", atlas.stdout)
        self.assertFalse((REPOSITORY_ROOT / "skills" / "17deg-atlas" / "SKILL.md").exists())
        remote = subprocess.run(
            [sys.executable, str(REMOTE_SKILL_ROOT / "scripts" / "remote.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(0, remote.returncode, remote.stderr)
        self.assertIn("connect", remote.stdout)

    def test_entry_runtime_conflicts_stop_and_remote_local_commands_are_blocked(self) -> None:
        for root, conflicting_runtime in (
            (LOCAL_SKILL_ROOT, "remote"),
            (REMOTE_SKILL_ROOT, "local"),
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "kb.py"),
                    "agent-start-plan",
                    "--runtime",
                    conflicting_runtime,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("conflicts with the installed Atlas entry", result.stderr)
        blocked = subprocess.run(
            [sys.executable, str(REMOTE_SKILL_ROOT / "scripts" / "kb.py"), "agent-local-plan"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(2, blocked.returncode)
        self.assertIn("remote Atlas entry only supports onboarding", blocked.stderr)

    def test_skill_has_no_placeholders_and_wrapper_runs(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("TODO", skill)
        self.assertIn("name: github-api-file-pusher", skill)
        result = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "kb.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("unlock-index", result.stdout)
        self.assertIn("github-plan", result.stdout)
        self.assertIn("github-check", result.stdout)
        self.assertIn("github-put", result.stdout)
        self.assertIn("github-connect", result.stdout)
        self.assertIn("github-inbox-add", result.stdout)
        remote = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "remote.py"), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(0, remote.returncode, remote.stderr)
        self.assertIn("check", remote.stdout)
        self.assertIn("add", remote.stdout)

    def test_real_remote_write_requires_explicit_confirmation_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance = Path(temp) / "instance"
            projection = Path(temp) / "my-knowledge-content"
            initialized = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_ROOT / "scripts" / "kb.py"),
                    "init-instance",
                    "--target",
                    str(instance),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_ROOT / "scripts" / "kb.py"),
                    "--root",
                    str(instance),
                    "github-put",
                    "--owner",
                    "example-owner",
                    "--repo",
                    "my-knowledge-content",
                    "--branch",
                    "main",
                    "--projection-root",
                    str(projection),
                    "--path",
                    "README.md",
                    "--message",
                    "测试：确认门",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(2, result.returncode)
        self.assertIn("--confirm-remote-write", result.stderr)

    def test_standalone_remote_client_appends_payload_then_ready_with_mock(self) -> None:
        module_path = SKILL_ROOT / "scripts" / "remote.py"
        spec = importlib.util.spec_from_file_location("standalone_remote", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, headers, body))
            number = len(calls)
            return 201, {"commit": {"sha": f"commit{number}"}}

        client = module.Client(
            owner="example-owner", repo="example-content", token="mock-token", transport=transport
        )
        with tempfile.TemporaryDirectory() as temp:
            content = Path(temp) / "note.md"
            content.write_text("standalone remote body", encoding="utf-8")
            args = module.build_parser().parse_args(
                [
                    "add",
                    "--request-id",
                    "standalone-001",
                    "--agent-id",
                    "hermes",
                    "--tier",
                    "public",
                    "--title",
                    "Standalone public note",
                    "--content-file",
                    str(content),
                    "--human-confirmed",
                    "--confirm-remote-write",
                    "--confirm-content-repository",
                ]
            )
            result = module.execute(args, client=client)
        self.assertEqual(2, len(calls))
        self.assertIn("payload.json", calls[0][1])
        self.assertIn("READY.json", calls[1][1])
        self.assertEqual("commit2", result["ready_commit_sha"])
        request_body = json.loads(calls[0][3].decode("utf-8"))
        envelope = json.loads(base64.b64decode(request_body["content"]).decode("utf-8"))
        self.assertEqual("public", envelope["classification"]["level"])
        self.assertEqual("github", envelope["storage_binding"]["backend"])
        self.assertEqual("local-only", envelope["distribution_decision"]["channel"])
        self.assertEqual([], envelope["policy_refs"])

    def test_standalone_remote_rejects_public_credential_pattern(self) -> None:
        module_path = SKILL_ROOT / "scripts" / "remote.py"
        spec = importlib.util.spec_from_file_location("standalone_remote_secret", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            content = Path(temp) / "unsafe.md"
            content.write_text("github_pat_" + "a" * 30, encoding="utf-8")
            args = module.build_parser().parse_args(
                [
                    "add",
                    "--request-id",
                    "standalone-unsafe",
                    "--agent-id",
                    "hermes",
                    "--tier",
                    "public",
                    "--title",
                    "Unsafe",
                    "--content-file",
                    str(content),
                    "--human-confirmed",
                    "--confirm-remote-write",
                    "--confirm-content-repository",
                ]
            )
            with self.assertRaises(module.RemoteError):
                module.build_event(args)

    def test_primary_skill_bootstraps_outside_product_checkout(self) -> None:
        primary = LOCAL_SKILL_ROOT
        repository_root = REPOSITORY_ROOT
        bootstrap_path = primary / "scripts" / "bootstrap.py"
        spec = importlib.util.spec_from_file_location("primary_bootstrap", bootstrap_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        bootstrap_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap_module)
        self.assertEqual(
            "https://github.com/canyexuanfan/17deg-atlas.git",
            bootstrap_module.DEFAULT_REPOSITORY,
        )
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            standalone = base / "skill"
            workspace = base / "workspace"
            cloned_workspace = base / "cloned-workspace"
            public_fixture = base / "public-fixture"
            shutil.copytree(primary, standalone)
            shutil.copytree(
                repository_root,
                public_fixture,
                ignore=shutil.ignore_patterns(".git", ".local", "__pycache__", "*.pyc"),
            )
            for command in (
                ["git", "init", "--initial-branch=main"],
                ["git", "config", "user.name", "Atlas Test"],
                ["git", "config", "user.email", "atlas-test@example.invalid"],
                ["git", "add", "."],
                ["git", "commit", "-m", "Create public fixture"],
            ):
                prepared = subprocess.run(
                    command,
                    cwd=public_fixture,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(0, prepared.returncode, prepared.stderr)
            denied_workspace = base / "denied-workspace"
            denied = subprocess.run(
                [
                    sys.executable,
                    str(standalone / "scripts" / "bootstrap.py"),
                    "--workspace",
                    str(denied_workspace),
                    "--repository",
                    "https://github.com/example/atlas.git",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(2, denied.returncode)
            self.assertIn("--confirm-network-install", denied.stderr)
            self.assertFalse((denied_workspace / ".17deg-atlas").exists())
            cloned = subprocess.run(
                [
                    sys.executable,
                    str(standalone / "scripts" / "bootstrap.py"),
                    "--workspace",
                    str(cloned_workspace),
                    "--repository",
                    str(public_fixture),
                    "--confirm-network-install",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, cloned.returncode, cloned.stderr)
            cloned_result = json.loads(cloned.stdout)
            self.assertEqual("installed-from-public-repository", cloned_result["action"])
            self.assertTrue(cloned_result["network_used"])
            boot = subprocess.run(
                [
                    sys.executable,
                    str(standalone / "scripts" / "bootstrap.py"),
                    "--workspace",
                    str(workspace),
                    "--source",
                    str(repository_root),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, boot.returncode, boot.stderr)
            boot_result = json.loads(boot.stdout)
            self.assertEqual("0.1.0", boot_result["cli_version"])
            self.assertTrue(Path(boot_result["cli_path"]).is_file())
            plan = subprocess.run(
                [sys.executable, str(standalone / "scripts" / "atlas.py"), "workspace", "plan"],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, plan.returncode, plan.stderr)
            result = json.loads(plan.stdout)
            self.assertEqual("local", result["runtime"])
            self.assertEqual(str(workspace.resolve()), result["local_plan"]["target"])
            self.assertEqual("empty-directory", result["local_plan"]["workspace_state"])

    def test_primary_bootstrap_hides_project_local_runtime_from_git(self) -> None:
        primary = LOCAL_SKILL_ROOT
        repository_root = REPOSITORY_ROOT
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            standalone = base / "skill"
            workspace = base / "workspace"
            shutil.copytree(primary, standalone)
            workspace.mkdir()
            subprocess.run(
                ["git", "init", "--initial-branch=main"],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            boot = subprocess.run(
                [
                    sys.executable,
                    str(standalone / "scripts" / "bootstrap.py"),
                    "--workspace",
                    str(workspace),
                    "--source",
                    str(repository_root),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, boot.returncode, boot.stderr)
            result = json.loads(boot.stdout)
            self.assertTrue(result["local_exclude_configured"])
            ignored = subprocess.run(
                ["git", "check-ignore", ".17deg-atlas/bin/17deg-atlas.py"],
                cwd=workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, ignored.returncode, ignored.stderr)


if __name__ == "__main__":
    unittest.main()

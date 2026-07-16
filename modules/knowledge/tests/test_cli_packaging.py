from __future__ import annotations

import importlib.util
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
ROOT_CLI = REPOSITORY_ROOT / "scripts" / "17deg-atlas.py"
LOCAL_SCRIPTS = REPOSITORY_ROOT / "skills" / "17deg-atlas-local" / "scripts"
REMOTE_SCRIPTS = REPOSITORY_ROOT / "skills" / "17deg-atlas-remote" / "scripts"


class CLIPackagingAcceptanceTests(unittest.TestCase):
    def run_python(self, script: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=cwd or REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_root_cli_has_stable_workspace_knowledge_and_remote_surfaces(self) -> None:
        version = self.run_python(ROOT_CLI, "--version")
        self.assertEqual(0, version.returncode, version.stderr)
        self.assertEqual("17deg-atlas 0.1.0", version.stdout.strip())
        help_result = self.run_python(ROOT_CLI, "--help")
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("workspace <init|plan|start>", help_result.stdout)
        self.assertIn("knowledge <command>", help_result.stdout)
        self.assertIn("remote <command>", help_result.stdout)
        capture_help = self.run_python(ROOT_CLI, "knowledge", "capture", "--help")
        self.assertEqual(0, capture_help.returncode, capture_help.stderr)
        self.assertIn("--capture-purpose", capture_help.stdout)
        capability_help = self.run_python(ROOT_CLI, "knowledge", "capability-propose", "--help")
        self.assertEqual(0, capability_help.returncode, capability_help.stderr)
        self.assertIn("--knowledge-ref", capability_help.stdout)
        self.assertIn("--input-contract", capability_help.stdout)
        self.assertIn("--evaluation-suite", capability_help.stdout)
        preference_help = self.run_python(ROOT_CLI, "knowledge", "capability-preference", "--help")
        self.assertEqual(0, preference_help.returncode, preference_help.stderr)
        self.assertIn("project", preference_help.stdout)
        self.assertIn("runtime-native", preference_help.stdout)

    def test_one_entry_bootstrap_installs_and_validates_project_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "empty-workspace"
            bootstrap = self.run_python(
                LOCAL_SCRIPTS / "bootstrap.py",
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
                cwd=workspace.parent,
            )
            self.assertEqual(0, bootstrap.returncode, bootstrap.stderr)
            installed = json.loads(bootstrap.stdout)
            self.assertEqual("0.1.0", installed["cli_version"])
            launcher = Path(installed["cli_path"])
            self.assertEqual("17deg-atlas-local.py", launcher.name)
            self.assertTrue(launcher.is_file())
            self.assertTrue((workspace / ".17deg-atlas" / "state" / "runtime.json").is_file())
            runtime = json.loads(
                (workspace / ".17deg-atlas" / "state" / "runtime.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(2, runtime["schema_version"])
            self.assertEqual("local", runtime["entry_runtime"])
            self.assertEqual(str(launcher), runtime["launcher"])
            self.assertEqual(64, len(runtime["source_fingerprint"]))
            self.assertIn("source_commit", runtime)
            version = self.run_python(launcher, "--version", cwd=workspace)
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual("17deg-atlas 0.1.0", version.stdout.strip())

            nested = workspace / "notes" / "drafts"
            nested.mkdir(parents=True)
            environment = os.environ.copy()
            environment.pop("ATLAS_ENTRY_RUNTIME", None)
            environment.pop("ATLAS_WORKSPACE", None)
            role_plan = subprocess.run(
                [sys.executable, str(launcher), "workspace", "plan"],
                cwd=nested,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, role_plan.returncode, role_plan.stderr)
            self.assertEqual("local", json.loads(role_plan.stdout)["runtime"])

            instance = workspace / "17deg-personal"
            initialized = self.run_python(
                launcher,
                "workspace",
                "init",
                "--target",
                str(instance),
                cwd=workspace,
            )
            self.assertEqual(0, initialized.returncode, initialized.stderr)
            self.assertTrue((instance / "knowledge").is_dir())

    def test_skill_entry_refreshes_a_stale_runtime_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "refresh-workspace"
            bootstrap = self.run_python(
                LOCAL_SCRIPTS / "bootstrap.py",
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
            )
            self.assertEqual(0, bootstrap.returncode, bootstrap.stderr)
            installed_module = workspace / ".17deg-atlas" / "tool" / "modules" / "knowledge"
            if not installed_module.is_dir():
                installed_module = workspace / ".17deg-atlas" / "tool"
            stale_marker = installed_module / "src" / "kb_vault" / "stale-runtime.txt"
            stale_marker.write_text("stale\n", encoding="utf-8")

            version = self.run_python(
                LOCAL_SCRIPTS / "atlas.py", "--version", cwd=workspace
            )
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual("17deg-atlas 0.1.0", version.stdout.strip())
            self.assertFalse(stale_marker.exists())
            self.assertTrue((workspace / ".17deg-atlas" / "tool.previous").is_dir())

    def test_copied_project_skill_refreshes_an_unversioned_runtime_from_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local_remote = Path(temporary) / "atlas-remote.git"
            prepared_remote = subprocess.run(
                ["git", "clone", "--bare", str(REPOSITORY_ROOT), str(local_remote)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, prepared_remote.returncode, prepared_remote.stderr)
            workspace = Path(temporary) / "copied-skill-workspace"
            installed = self.run_python(
                LOCAL_SCRIPTS / "bootstrap.py",
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
            )
            self.assertEqual(0, installed.returncode, installed.stderr)
            runtime_path = workspace / ".17deg-atlas" / "state" / "runtime.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["source_commit"] = "0" * 40
            runtime_path.write_text(
                json.dumps(runtime, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            installed_module = workspace / ".17deg-atlas" / "tool" / "modules" / "knowledge"
            if not installed_module.is_dir():
                installed_module = workspace / ".17deg-atlas" / "tool"
            stale_marker = installed_module / "src" / "kb_vault" / "copied-skill-stale.txt"
            stale_marker.write_text("stale\n", encoding="utf-8")
            copied_skill = workspace / ".codex" / "skills" / "17deg-atlas-local"
            copied_skill.parent.mkdir(parents=True)
            shutil.copytree(LOCAL_SCRIPTS.parent, copied_skill)
            environment = os.environ.copy()
            environment["ATLAS_REPOSITORY"] = str(local_remote)
            result = subprocess.run(
                [sys.executable, str(copied_skill / "scripts" / "atlas.py"), "--version"],
                cwd=workspace,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            refreshed = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertFalse(
                stale_marker.exists(),
                f"stdout={result.stdout!r} stderr={result.stderr!r} runtime={refreshed!r}",
            )
            expected_commit = subprocess.run(
                ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                check=True,
            ).stdout.strip()
            self.assertEqual(expected_commit, refreshed["source_commit"])
            self.assertEqual("updated", refreshed["update_check"])

    def test_runtime_refresh_restores_previous_tool_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "rollback-workspace"
            installed = self.run_python(
                LOCAL_SCRIPTS / "bootstrap.py",
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
            )
            self.assertEqual(0, installed.returncode, installed.stderr)
            installed_module = workspace / ".17deg-atlas" / "tool" / "modules" / "knowledge"
            if not installed_module.is_dir():
                installed_module = workspace / ".17deg-atlas" / "tool"
            stale_marker = installed_module / "src" / "kb_vault" / "stale-runtime.txt"
            stale_marker.write_text("stale\n", encoding="utf-8")

            spec = importlib.util.spec_from_file_location(
                "atlas_local_bootstrap_test", LOCAL_SCRIPTS / "bootstrap.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with mock.patch.object(
                module,
                "install_cli",
                side_effect=module.BootstrapError("validation failed"),
            ):
                with self.assertRaises(module.BootstrapError):
                    module.bootstrap(workspace, source=REPOSITORY_ROOT)
            self.assertTrue(stale_marker.is_file())
            self.assertFalse((workspace / ".17deg-atlas" / "tool.previous").exists())

    def test_offline_update_check_preserves_the_last_verified_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "offline-workspace"
            installed = self.run_python(
                LOCAL_SCRIPTS / "bootstrap.py",
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
            )
            self.assertEqual(0, installed.returncode, installed.stderr)
            state_path = workspace / ".17deg-atlas" / "state" / "runtime.json"
            before = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(before["source_commit"])
            spec = importlib.util.spec_from_file_location(
                "atlas_local_bootstrap_offline_test", LOCAL_SCRIPTS / "bootstrap.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.LAST_UPDATE_ERROR = "simulated-offline"
            with mock.patch.object(module, "remote_head", return_value=""):
                result = module.bootstrap(
                    workspace,
                    source=workspace / ".17deg-atlas" / "tool",
                    check_updates=True,
                )
            self.assertEqual("unavailable", result["update_check"])
            after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(before["source_commit"], after["source_commit"])
            self.assertEqual("unavailable", after["update_check"])

    def test_local_and_remote_skills_call_one_cli_with_fixed_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "role-workspace"
            bootstrap = self.run_python(
                LOCAL_SCRIPTS / "bootstrap.py",
                "--workspace",
                str(workspace),
                "--source",
                str(REPOSITORY_ROOT),
                cwd=workspace.parent,
            )
            self.assertEqual(0, bootstrap.returncode, bootstrap.stderr)

            local_plan = self.run_python(
                LOCAL_SCRIPTS / "atlas.py", "workspace", "plan", cwd=workspace
            )
            self.assertEqual(0, local_plan.returncode, local_plan.stderr)
            self.assertEqual("local", json.loads(local_plan.stdout)["runtime"])

            remote_plan = self.run_python(
                REMOTE_SCRIPTS / "atlas.py", "workspace", "plan", cwd=workspace
            )
            self.assertEqual(0, remote_plan.returncode, remote_plan.stderr)
            self.assertEqual("remote", json.loads(remote_plan.stdout)["runtime"])

            local_remote = self.run_python(
                LOCAL_SCRIPTS / "atlas.py", "remote", "check", "--help", cwd=workspace
            )
            self.assertEqual(2, local_remote.returncode)
            self.assertIn("remote entry", local_remote.stderr)

            remote_help = self.run_python(
                REMOTE_SCRIPTS / "atlas.py", "remote", "check", "--help", cwd=workspace
            )
            self.assertEqual(0, remote_help.returncode, remote_help.stderr)
            self.assertIn("--owner", remote_help.stdout)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
            self.assertTrue(launcher.is_file())
            self.assertTrue((workspace / ".17deg-atlas" / "state" / "runtime.json").is_file())
            version = self.run_python(launcher, "--version", cwd=workspace)
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual("17deg-atlas 0.1.0", version.stdout.strip())

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

"""Security regression tests for the manifest policy (TASK-SECURITY-009 P0-A).

Every fixture is synthetic (fake paths, fake values) and lives in a
throwaway temp git repo. Nothing here touches the real repo, the
production dir, Drive, or any credential. The fake "secret-like"
values are distinctive constants, and the tests assert they NEVER
appear in tool output.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REGENERATE = SCRIPTS / "regenerate_manifest.py"
VERIFY = SCRIPTS / "verify_manifest.py"
REAL_GITIGNORE = ROOT / ".gitignore"

# Distinctive fake values -- never real secrets, never printed by tools.
FAKE_TOKEN = "FAKE-TOKEN-VALUE-7Q3-NOT-REAL"
FAKE_PROMPT = "FAKE-PROMPT-9X2-NOT-REAL"
FAKE_TASK_ID = "fake-task-0000"
FAKE_IMAGE_ID = "fake-image-id-0000"

SOURCE_FILES = [
    "h3_controller.py",
    "h3_remote_worker.py",
    "h3_task_spec.py",
    "h3_drive_download.py",
    "h3_drive_upload.py",
    "h3_shutdown_executor.py",
]


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, check=False,
    )


def run_tool(script: Path, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), "--root", str(root)],
        capture_output=True, text=True, check=False,
    )


class ManifestSecurityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.assertEqual(run_git(self.repo, "init").returncode, 0)
        # The real .gitignore is the rule under test: copy it in.
        (self.repo / ".gitignore").write_text(REAL_GITIGNORE.read_text())
        run_git(self.repo, "add", ".gitignore")
        for name in SOURCE_FILES:
            (self.repo / name).write_text(f"# stub {name}\n")
        run_git(self.repo, "add", *SOURCE_FILES)

    def tearDown(self):
        self._tmp.cleanup()

    def _manifest_paths(self) -> set[str]:
        data = json.loads((self.repo / "SOURCE_MANIFEST.json").read_text())
        return {e["path"] for e in data["files"]}

    def _regenerate_ok(self) -> subprocess.CompletedProcess:
        r = run_tool(REGENERATE, self.repo)
        self.assertEqual(r.returncode, 0, f"regenerate failed: {r.stdout}{r.stderr}")
        return r

    def test_1_private_venv_not_listed(self):
        p = self.repo / ".venv-colab-cli" / "lib" / "example.py"
        p.parent.mkdir(parents=True)
        p.write_text("# fake venv file\n")
        ignored = run_git(self.repo, "check-ignore", "-v", str(p.relative_to(self.repo)))
        self.assertEqual(ignored.returncode, 0, ".venv-colab-cli/ must be gitignored")
        self._regenerate_ok()
        self.assertNotIn(".venv-colab-cli/lib/example.py", self._manifest_paths())

    def test_2_oauth_dirs_not_listed(self):
        for d in (".secrets", ".gcloud-colab"):
            p = self.repo / d / "example.json"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"token": FAKE_TOKEN}) + "\n")
            ignored = run_git(self.repo, "check-ignore", "-v", str(p.relative_to(self.repo)))
            self.assertEqual(ignored.returncode, 0, f"{d}/ must be gitignored")
        r = self._regenerate_ok()
        paths = self._manifest_paths()
        self.assertNotIn(".secrets/example.json", paths)
        self.assertNotIn(".gcloud-colab/example.json", paths)
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)

    def test_3_real_task_spec_dir_not_listed(self):
        p = self.repo / "config" / "tasks" / "private-task.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({
            "task_id": FAKE_TASK_ID,
            "prompt": FAKE_PROMPT,
            "ref_image_1_id": FAKE_IMAGE_ID,
        }) + "\n")
        ignored = run_git(self.repo, "check-ignore", "-v", str(p.relative_to(self.repo)))
        self.assertEqual(ignored.returncode, 0, "config/tasks/ must be gitignored")
        example = self.repo / "config" / "task_spec.example.json"
        example.parent.mkdir(exist_ok=True)
        example.write_text(json.dumps({"task_id": "example"}) + "\n")
        run_git(self.repo, "add", "config/task_spec.example.json")
        r = self._regenerate_ok()
        paths = self._manifest_paths()
        self.assertNotIn("config/tasks/private-task.json", paths)
        self.assertIn("config/task_spec.example.json", paths)
        self.assertNotIn(FAKE_PROMPT, r.stdout + r.stderr)
        self.assertNotIn(FAKE_TASK_ID, r.stdout + r.stderr)

    def test_4_tracked_sensitive_file_refused(self):
        self._regenerate_ok()  # baseline manifest without the sensitive file
        p = self.repo / ".secrets" / "staged.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"token": FAKE_TOKEN}) + "\n")
        self.assertEqual(run_git(self.repo, "add", "-f", ".secrets/staged.json").returncode, 0)
        r = run_tool(REGENERATE, self.repo)
        self.assertNotEqual(r.returncode, 0, "generator must refuse a tracked sensitive file")
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        self.assertIn(".secrets/staged.json", r.stdout + r.stderr)
        # The baseline manifest is left untouched: no sensitive entry added.
        self.assertNotIn(".secrets/staged.json", self._manifest_paths())
        v = run_tool(VERIFY, self.repo)
        self.assertNotEqual(v.returncode, 0, "verifier must refuse a tracked sensitive file")
        self.assertNotIn(FAKE_TOKEN, v.stdout + v.stderr)

    def test_5_normal_source_files_listed(self):
        self._regenerate_ok()
        data = json.loads((self.repo / "SOURCE_MANIFEST.json").read_text())
        by_path = {e["path"]: e for e in data["files"]}
        for name in SOURCE_FILES:
            self.assertIn(name, by_path, f"{name} must be listed")
            entry = by_path[name]
            raw = (self.repo / name).read_bytes()
            self.assertEqual(entry["size_bytes"], len(raw))
            self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())

    def test_6_symlink_not_followed(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text(FAKE_TOKEN + "\n")
        link = self.repo / "linked-secret.txt"
        link.symlink_to(secret)
        self.assertEqual(run_git(self.repo, "add", "-f", "linked-secret.txt").returncode, 0)
        r = run_tool(REGENERATE, self.repo)
        self.assertNotEqual(r.returncode, 0, "generator must refuse a tracked symlink")
        self.assertNotIn(FAKE_TOKEN, r.stdout + r.stderr)
        self.assertFalse((self.repo / "SOURCE_MANIFEST.json").exists(),
                         "manifest must not be written on refusal")

    def test_7_repeat_generation_stable(self):
        self._regenerate_ok()
        first = (self.repo / "SOURCE_MANIFEST.json").read_bytes()
        r = run_tool(REGENERATE, self.repo)
        self.assertEqual(r.returncode, 0)
        second = (self.repo / "SOURCE_MANIFEST.json").read_bytes()
        self.assertEqual(first, second, "repeat generation must be byte-identical")

    def test_8_tampered_file_detected(self):
        self._regenerate_ok()
        target = self.repo / "h3_controller.py"
        original = target.read_bytes()
        target.write_bytes(original + b"# tampered\n")
        v = run_tool(VERIFY, self.repo)
        self.assertNotEqual(v.returncode, 0, "verifier must catch a tampered file")
        self.assertIn("SHA MISMATCH", v.stdout + v.stderr)
        target.write_bytes(original)
        v2 = run_tool(VERIFY, self.repo)
        self.assertEqual(v2.returncode, 0, "verifier must pass again after restore")


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the Muse adapter (TASK-VERSION-ROADMAP-011 Stage 1).

Everything here is offline: no GPU, no Drive, no credentials, no
network. All token-like values are distinctive fakes; tests assert
they NEVER appear in adapter output (redaction) and NEVER reach a
log. The adapter wraps the existing production core — it does not
reimplement it — so these tests pin the adapter's own gates:
fail-closed TaskSpec mapping, explicit-only authorization, path
denials, restart read-only checks, and budget-constant assertions.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adapters"))

from muse.adapter import (  # noqa: E402
    Authorization,
    assert_budget_constants,
    build_task_spec,
    check_existing_task_state,
    status_report,
    to_wrapper_args,
)
from muse.paths import (  # noqa: E402
    locate_repo_root,
    locate_task_spec_file,
    resolve_production_dir,
)
from muse.redact import redact_mapping, redact_text  # noqa: E402

# Distinctive fake values — never real secrets, never printed by tools.
FAKE_SHA256 = "a" * 64
FAKE_DRIVE_ID = "fake-drive-file-id-0000"
FAKE_TASK_ID = "fake-task-0000"
FAKE_PAT = "ghp_" + "F" * 36
FAKE_YA29 = "ya29." + "G" * 30


def _valid_spec_dict(**overrides):
    spec = {
        "task_id": FAKE_TASK_ID,
        "output_prefix": "T",
        "ref_image_1": {"drive_file_id": FAKE_DRIVE_ID, "sha256": FAKE_SHA256},
        "prompt": "A short fake prompt for adapter testing only.",
        "seed": 42,
        "animation_frames": 226,
    }
    spec.update(overrides)
    return spec


class AdapterSpecTest(unittest.TestCase):
    def test_valid_spec_builds(self):
        spec = build_task_spec(_valid_spec_dict())
        self.assertEqual(spec.task_id, FAKE_TASK_ID)
        self.assertEqual(spec.animation_frames, 226)
        self.assertEqual(spec.total_frames, 226)

    def test_missing_ref_image_1_fails_closed(self):
        spec = _valid_spec_dict()
        del spec["ref_image_1"]
        with self.assertRaises(ValueError):
            build_task_spec(spec)

    def test_missing_seed_fails_closed(self):
        spec = _valid_spec_dict()
        del spec["seed"]
        with self.assertRaises(ValueError):
            build_task_spec(spec)

    def test_bad_frame_rule_fails_closed(self):
        # 227 % 17 == 6, not 5 — the MiniMax rule must reject it.
        with self.assertRaises(ValueError):
            build_task_spec(_valid_spec_dict(animation_frames=227))

    def test_unknown_top_level_key_fails_closed(self):
        with self.assertRaises(ValueError):
            build_task_spec(_valid_spec_dict(surprise_field="nope"))

    def test_prompt_and_shots_together_fails_closed(self):
        with self.assertRaises(ValueError):
            build_task_spec(
                _valid_spec_dict(shots=[{"text": "a fake shot"}])
            )

    def test_non_dict_spec_fails_closed(self):
        with self.assertRaises(ValueError):
            build_task_spec(["not", "a", "dict"])


class AuthorizationTest(unittest.TestCase):
    def _spec_file(self, tmp: Path) -> Path:
        path = tmp / "spec.json"
        path.write_text(json.dumps(_valid_spec_dict()), encoding="utf-8")
        return path

    def test_wrapper_args_refuse_without_explicit_confirm(self):
        with tempfile.TemporaryDirectory() as d:
            spec_path = self._spec_file(Path(d))
            auth = Authorization(
                production_task_id=FAKE_TASK_ID, confirm_live=False
            )
            with self.assertRaises(PermissionError):
                to_wrapper_args(auth, spec_path)

    def test_wrapper_args_map_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as d:
            spec_path = self._spec_file(Path(d))
            auth = Authorization(
                production_task_id=FAKE_TASK_ID,
                confirm_live=True,
                authorize_new_task=True,
                max_task_attempts=2,
            )
            args = to_wrapper_args(auth, spec_path)
            self.assertIn("--confirm-live", args)
            self.assertIn("--production-task-id", args)
            self.assertIn(FAKE_TASK_ID, args)
            self.assertIn("--task-spec", args)
            self.assertIn(str(spec_path), args)
            self.assertIn("--deliver", args)
            self.assertIn("--authorize-new-task", args)
            # No bare `colab new` anywhere in the mapped args.
            self.assertNotIn("new", args)

    def test_max_attempts_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            Authorization(production_task_id=FAKE_TASK_ID, max_task_attempts=4)
        with self.assertRaises(ValueError):
            Authorization(production_task_id=FAKE_TASK_ID, max_task_attempts=0)

    def test_empty_task_id_rejected(self):
        with self.assertRaises(ValueError):
            Authorization(production_task_id="  ")


class BudgetConstantsTest(unittest.TestCase):
    def test_budget_constants_unchanged(self):
        budget = assert_budget_constants()
        self.assertEqual(budget["project_budget_cu"], 30.0)
        self.assertEqual(budget["per_session_budget_cu"], 25.0)
        self.assertEqual(budget["cleanup_reserve_cu"], 0.75)
        self.assertEqual(budget["max_session_seconds"], 14400)


class RedactionTest(unittest.TestCase):
    def test_fake_pat_redacted_and_never_echoed(self):
        out = redact_text(f"token is {FAKE_PAT} end")
        self.assertNotIn(FAKE_PAT, out)
        self.assertNotIn("F" * 36, out)
        self.assertIn("[REDACTED]", out)

    def test_fake_ya29_redacted(self):
        out = redact_text(f"ya token {FAKE_YA29}")
        self.assertNotIn(FAKE_YA29, out)
        self.assertIn("[REDACTED]", out)

    def test_private_key_block_redacted_whole(self):
        # The header/footer are built from fragments at runtime so the
        # static secret scan (scripts/secret_scan.py) does not flag this
        # test file for containing a key-block literal. The value under
        # test is still a realistic fake block; it is never a real key.
        begin = "-----" + "BEGIN PRIVATE KEY" + "-----"
        end = "-----" + "END PRIVATE KEY" + "-----"
        key = begin + "\nFAKEKEYMATERIAL\n" + end
        out = redact_text(f"leaked {key} done")
        self.assertNotIn("FAKEKEYMATERIAL", out)
        self.assertNotIn("BEGIN PRIVATE KEY", out)
        self.assertIn("[REDACTED]", out)

    def test_redaction_is_idempotent(self):
        once = redact_text(f"x {FAKE_PAT}")
        twice = redact_text(once)
        self.assertEqual(once, twice)

    def test_status_report_redacts_note(self):
        report = status_report(
            production_task_id=FAKE_TASK_ID,
            run_id=None,
            note=f"failed with {FAKE_PAT}",
        )
        blob = json.dumps(report)
        self.assertNotIn(FAKE_PAT, blob)
        self.assertIn("[REDACTED]", blob)

    def test_redact_mapping_redacts_values_not_keys(self):
        out = redact_mapping({"token": FAKE_PAT, "safe": "hello"})
        self.assertEqual(out["safe"], "hello")
        self.assertNotIn(FAKE_PAT, json.dumps(out))


class PathsTest(unittest.TestCase):
    def test_locate_repo_root(self):
        self.assertEqual(locate_repo_root(), ROOT)

    def test_production_dir_requires_explicit_absolute_path(self):
        with self.assertRaises(ValueError):
            resolve_production_dir()
        with self.assertRaises(ValueError):
            resolve_production_dir("relative/path")

    def test_production_dir_requires_release_commit(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                resolve_production_dir(Path(d))

    def test_production_dir_accepts_explicit_dir_with_release_commit(self):
        with tempfile.TemporaryDirectory() as d:
            prod = Path(d) / "prod"
            prod.mkdir()
            (prod / "RELEASE_COMMIT").write_text("fake-sha\n", encoding="utf-8")
            self.assertEqual(resolve_production_dir(prod), prod.resolve())

    def test_task_spec_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            real = tmp / "real.json"
            real.write_text("{}", encoding="utf-8")
            link = tmp / "link.json"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                locate_task_spec_file(link)

    def test_task_spec_refuses_non_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spec.txt"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                locate_task_spec_file(path)

    def test_task_spec_refuses_repo_config_tasks(self):
        denied = ROOT / "config" / "tasks" / "fake.json"
        with self.assertRaises(ValueError):
            locate_task_spec_file(denied, repo_root=ROOT)

    def test_task_spec_accepts_external_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spec.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(locate_task_spec_file(path), path.resolve())


class RestartCheckTest(unittest.TestCase):
    def test_no_existing_state(self):
        with tempfile.TemporaryDirectory() as d:
            result = check_existing_task_state(FAKE_TASK_ID, [Path(d)])
            self.assertEqual(result["manifests_found"], 0)
            self.assertIn("no existing state", result["action"])

    def test_finds_matching_manifest_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            manifest_dir = root / "model_cache" / "run_manifests"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "h3-20261120T000000Z-abcdef.json").write_text(
                json.dumps(
                    {
                        "run_id": "h3-20261120T000000Z-abcdef",
                        "production_task_id": FAKE_TASK_ID,
                        "status": "in-flight",
                    }
                ),
                encoding="utf-8",
            )
            (manifest_dir / "other.json").write_text(
                json.dumps(
                    {
                        "run_id": "h3-other",
                        "production_task_id": "someone-else",
                        "status": "done",
                    }
                ),
                encoding="utf-8",
            )
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["manifests_found"], 1)
            self.assertEqual(
                result["manifests"][0]["run_id"], "h3-20261120T000000Z-abcdef"
            )
            self.assertIn("review the newest manifest", result["action"])

    def test_empty_task_id_rejected(self):
        with self.assertRaises(ValueError):
            check_existing_task_state("  ", [])


class HookNonEquivalenceTest(unittest.TestCase):
    def test_no_agents_hook_file_is_loaded_by_adapter(self):
        # Muse does not load Antigravity's .agents/hooks.json. The repo
        # must not contain one, and the adapter must not reference it
        # as a config it reads.
        self.assertFalse((ROOT / ".agents" / "hooks.json").exists())
        adapter_src = (ROOT / "adapters" / "muse" / "adapter.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".agents/hooks.json", adapter_src)
        compat = (ROOT / "adapters" / "antigravity" / "COMPATIBILITY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("does not load `.agents/hooks.json`", compat)

    def test_manifest_policy_files_retained(self):
        self.assertTrue((ROOT / "scripts" / "manifest_policy.py").is_file())
        self.assertTrue((ROOT / "tests" / "test_manifest_security.py").is_file())


if __name__ == "__main__":
    unittest.main()

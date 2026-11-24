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
from muse.redact import redact_mapping, redact_text, redact_value  # noqa: E402

# Distinctive fake values — never real secrets, never printed by tools.
FAKE_SHA256 = "a" * 64
FAKE_DRIVE_ID = "fake-drive-file-id-0000"
FAKE_TASK_ID = "fake-task-0000"
FAKE_PAT = "ghp_" + "F" * 36
FAKE_YA29 = "ya29." + "G" * 30
# Built from fragments: a 16-hex literal next to a "token"
# key trips gitleaks generic-api-key, so the value is never
# spelled out in this file (same approach as the fake
# private-key block below).
FAKE_TOKEN_ID = "abcdef" + "0123456789"


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


class AuthorizationExtendedParamsTest(unittest.TestCase):
    """TASK-RELEASE-V032 R5: explicit M1/M2/M4 wrapper options.

    allocation_rounds None or 1..5, bootstrap_mode None or
    bundled|legacy, keep_warm_ttl 0 (OFF, the default) or 60..900.
    Every boundary is pinned: the values just inside and just
    outside each range, plus bool / non-int / wrong-case rejections
    (the adapter is stricter than the controller's int()/lower()
    coercions by design — a wrong-typed value is refused at 0 CU,
    never coerced into a live run).
    """

    def _spec_file(self, tmp: Path) -> Path:
        path = tmp / "spec.json"
        path.write_text(json.dumps(_valid_spec_dict()), encoding="utf-8")
        return path

    def _args(self, **auth_kwargs):
        with tempfile.TemporaryDirectory() as d:
            spec_path = self._spec_file(Path(d))
            auth = Authorization(
                production_task_id=FAKE_TASK_ID, confirm_live=True,
                **auth_kwargs)
            return to_wrapper_args(auth, spec_path)

    def _flag_value(self, args, flag):
        self.assertIn(flag, args)
        return args[args.index(flag) + 1]

    def test_allocation_rounds_boundaries_accepted_and_mapped(self):
        for rounds in (1, 5):
            args = self._args(allocation_rounds=rounds)
            self.assertEqual(
                self._flag_value(args, "--allocation-rounds"), str(rounds))

    def test_allocation_rounds_out_of_range_rejected(self):
        for rounds in (0, 6, -1):
            with self.assertRaises(ValueError, msg=f"rounds={rounds}"):
                Authorization(
                    production_task_id=FAKE_TASK_ID,
                    allocation_rounds=rounds)

    def test_allocation_rounds_rejects_bool_and_non_int(self):
        for rounds in (True, False, "3", 2.5, [3]):
            with self.assertRaises(ValueError, msg=f"rounds={rounds!r}"):
                Authorization(
                    production_task_id=FAKE_TASK_ID,
                    allocation_rounds=rounds)

    def test_allocation_rounds_none_omits_flag(self):
        args = self._args(allocation_rounds=None)
        self.assertNotIn("--allocation-rounds", args)

    def test_bootstrap_mode_accepted_and_mapped(self):
        for mode in ("bundled", "legacy"):
            args = self._args(bootstrap_mode=mode)
            self.assertEqual(
                self._flag_value(args, "--bootstrap-mode"), mode)

    def test_bootstrap_mode_rejects_other_values(self):
        for mode in ("Bundled", "BUNDLED", "warm", "", "bundled ", 1, True):
            with self.assertRaises(ValueError, msg=f"mode={mode!r}"):
                Authorization(
                    production_task_id=FAKE_TASK_ID, bootstrap_mode=mode)

    def test_bootstrap_mode_none_omits_flag(self):
        args = self._args(bootstrap_mode=None)
        self.assertNotIn("--bootstrap-mode", args)

    def test_keep_warm_ttl_boundaries_accepted_and_mapped(self):
        for ttl in (0, 60, 900):
            args = self._args(keep_warm_ttl=ttl)
            self.assertEqual(
                self._flag_value(args, "--keep-warm-ttl"), str(ttl))

    def test_keep_warm_ttl_out_of_range_rejected(self):
        for ttl in (59, 901, -1, 1):
            with self.assertRaises(ValueError, msg=f"ttl={ttl}"):
                Authorization(
                    production_task_id=FAKE_TASK_ID, keep_warm_ttl=ttl)

    def test_keep_warm_ttl_rejects_bool_and_non_int(self):
        for ttl in (True, False, "60", 60.0, None):
            with self.assertRaises(ValueError, msg=f"ttl={ttl!r}"):
                Authorization(
                    production_task_id=FAKE_TASK_ID, keep_warm_ttl=ttl)

    def test_keep_warm_ttl_defaults_to_zero_and_maps_explicit_off(self):
        # The warm pool is OFF unless explicitly enabled: the default
        # is 0, and 0 is mapped explicitly (never omitted into an
        # ambiguous default).
        auth = Authorization(production_task_id=FAKE_TASK_ID)
        self.assertEqual(auth.keep_warm_ttl, 0)
        self.assertIsNone(auth.allocation_rounds)
        self.assertIsNone(auth.bootstrap_mode)
        args = self._args()
        self.assertEqual(self._flag_value(args, "--keep-warm-ttl"), "0")
        self.assertNotIn("--allocation-rounds", args)
        self.assertNotIn("--bootstrap-mode", args)

    def test_all_three_params_map_together(self):
        args = self._args(allocation_rounds=3, bootstrap_mode="legacy",
                          keep_warm_ttl=300)
        self.assertEqual(
            self._flag_value(args, "--allocation-rounds"), "3")
        self.assertEqual(
            self._flag_value(args, "--bootstrap-mode"), "legacy")
        self.assertEqual(
            self._flag_value(args, "--keep-warm-ttl"), "300")


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


class TaskSpecPathDefaultRootTest(unittest.TestCase):
    """Fix 1 (review): the config/tasks/ denial applies even when no
    repo_root is passed — including through to_wrapper_args() — and
    survives parent-directory symlink bypasses in both directions."""

    def _fake_root(self, tmp: Path) -> Path:
        root = tmp / "fake_root"
        root.mkdir()
        (root / "h3_controller.py").write_text("# fake\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        return root

    def test_denied_without_explicit_repo_root(self):
        denied = ROOT / "config" / "tasks" / "fake.json"
        with self.assertRaises(ValueError):
            locate_task_spec_file(denied)

    def test_wrapper_args_refuses_repo_config_tasks_on_real_path(self):
        auth = Authorization(
            production_task_id=FAKE_TASK_ID, confirm_live=True
        )
        with self.assertRaises(ValueError):
            to_wrapper_args(auth, ROOT / "config" / "tasks" / "fake.json")

    def test_parent_symlink_pointing_out_is_refused(self):
        # config/tasks itself is a symlink to a directory outside the
        # repo: the resolved path escapes, so only the as-addressed
        # path can catch it.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            outside = tmp / "outside_tasks"
            outside.mkdir()
            (outside / "spec.json").write_text("{}", encoding="utf-8")
            root = self._fake_root(tmp)
            (root / "config").mkdir()
            (root / "config" / "tasks").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaises(ValueError):
                locate_task_spec_file(
                    root / "config" / "tasks" / "spec.json", repo_root=root
                )

    def test_parent_symlink_pointing_in_is_refused(self):
        # Addressed outside the repo, but a parent directory is a
        # symlink into the repo's config/tasks/: the resolved path
        # lands inside it.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = self._fake_root(tmp)
            tasks = root / "config" / "tasks"
            tasks.mkdir(parents=True)
            (tasks / "spec.json").write_text("{}", encoding="utf-8")
            alias = tmp / "alias"
            alias.symlink_to(tasks, target_is_directory=True)
            with self.assertRaises(ValueError):
                locate_task_spec_file(alias / "spec.json", repo_root=root)

    def test_dotdot_traversal_into_config_tasks_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = self._fake_root(tmp)
            tasks = root / "config" / "tasks"
            tasks.mkdir(parents=True)
            (tasks / "spec.json").write_text("{}", encoding="utf-8")
            addressed = root / "config" / "tasks" / ".." / "tasks" / "spec.json"
            with self.assertRaises(ValueError):
                locate_task_spec_file(addressed, repo_root=root)

    def test_benign_parent_symlink_still_accepted(self):
        # A symlinked parent that has nothing to do with config/tasks/
        # must not break legitimate external specs.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            real = tmp / "realdir"
            real.mkdir()
            spec = real / "spec.json"
            spec.write_text("{}", encoding="utf-8")
            link = tmp / "linkdir"
            link.symlink_to(real, target_is_directory=True)
            self.assertEqual(
                locate_task_spec_file(link / "spec.json"), spec.resolve()
            )


class DeepRedactionTest(unittest.TestCase):
    """Fix 2 (review): status_report deep-redacts strings, nested
    dicts and lists — including shutdown and budget — and replaces
    values under credential-named keys wholesale."""

    def test_status_report_redacts_nested_shutdown_and_budget(self):
        report = status_report(
            production_task_id=FAKE_TASK_ID,
            run_id="h3-20261120T000000Z-abcdef",
            budget={"project_budget_cu": 30.0,
                    "note": "rotated key " + FAKE_PAT},
            shutdown={
                "result": "ok",
                "token": FAKE_PAT,
                "nested": {"access_token": FAKE_YA29,
                           "items": ["plain", "leaked " + FAKE_PAT]},
                "token_id": FAKE_TOKEN_ID,
            },
            note="plain note",
        )
        blob = json.dumps(report)
        self.assertNotIn(FAKE_PAT, blob)
        self.assertNotIn(FAKE_YA29, blob)
        self.assertNotIn("F" * 36, blob)
        self.assertIn("[REDACTED]", blob)
        # Numbers and safe identifiers survive the redaction.
        self.assertEqual(report["budget"]["project_budget_cu"], 30.0)
        self.assertEqual(report["shutdown"]["result"], "ok")
        self.assertEqual(report["shutdown"]["token_id"], FAKE_TOKEN_ID)
        self.assertEqual(report["shutdown"]["nested"]["items"][0], "plain")

    def test_status_report_redacts_short_password_by_key(self):
        # "hunter2-short" matches no credential pattern — only the
        # key ("password") marks it as a credential.
        report = status_report(
            production_task_id=FAKE_TASK_ID,
            run_id=None,
            shutdown={"password": "hunter2-short", "result": "ok"},
        )
        blob = json.dumps(report)
        self.assertNotIn("hunter2-short", blob)
        self.assertEqual(report["shutdown"]["result"], "ok")

    def test_redact_value_walks_lists_tuples_and_dicts(self):
        value = {"items": [FAKE_PAT, ("nested", {"secret": "s3cr3t-value"})],
                 "safe": 42}
        out = redact_value(value)
        blob = json.dumps(out)
        self.assertNotIn(FAKE_PAT, blob)
        self.assertNotIn("s3cr3t-value", blob)
        self.assertEqual(out["safe"], 42)
        self.assertIsInstance(out["items"][1], tuple)

    def test_redact_value_keeps_safe_keys(self):
        out = redact_value({"author": "someone",
                            "token_id": FAKE_TOKEN_ID,
                            "status": "ok"})
        self.assertEqual(out, {"author": "someone",
                               "token_id": FAKE_TOKEN_ID,
                               "status": "ok"})

    def test_redact_value_depth_cap_bounds_nesting(self):
        deep = {"level": "deep-secret-marker"}
        for _ in range(12):
            deep = {"next": deep}
        out = redact_value(deep)
        self.assertNotIn("deep-secret-marker", json.dumps(out))


class RestartCheckTest(unittest.TestCase):
    def test_no_existing_state(self):
        with tempfile.TemporaryDirectory() as d:
            result = check_existing_task_state(FAKE_TASK_ID, [Path(d)])
            self.assertEqual(result["state"], "CONFIRMED_ABSENT")
            self.assertTrue(result["allow_new_gpu_request"])
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
            self.assertEqual(result["state"], "FOUND")
            self.assertFalse(result["allow_new_gpu_request"])
            self.assertEqual(result["manifests_found"], 1)
            self.assertEqual(
                result["manifests"][0]["run_id"], "h3-20261120T000000Z-abcdef"
            )
            self.assertIn("review the newest manifest", result["action"])

    def test_empty_task_id_rejected(self):
        with self.assertRaises(ValueError):
            check_existing_task_state("  ", [])


class RestartCheckTriStateTest(unittest.TestCase):
    """Fix 3 (review): FOUND / CONFIRMED_ABSENT / UNKNOWN. Absence is
    only confirmed when every search root was read cleanly; anything
    else is UNKNOWN and blocks a new GPU request."""

    def _manifest_dir(self, root: Path) -> Path:
        d = root / "model_cache" / "run_manifests"
        d.mkdir(parents=True)
        return d

    def _write(self, d: Path, name: str, data: dict) -> None:
        (d / name).write_text(json.dumps(data), encoding="utf-8")

    def test_no_search_roots_is_unknown(self):
        for roots in ([], None):
            result = check_existing_task_state(FAKE_TASK_ID, roots)
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertFalse(result["allow_new_gpu_request"])
            self.assertTrue(result["problems"])
            self.assertIn("do NOT request", result["action"])

    def test_unusable_root_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            result = check_existing_task_state(
                FAKE_TASK_ID, [Path(d) / "nope"]
            )
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertFalse(result["allow_new_gpu_request"])

    def test_corrupt_manifest_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = self._manifest_dir(root)
            (md / "broken.json").write_text("{not json", encoding="utf-8")
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertFalse(result["allow_new_gpu_request"])
            self.assertIn("broken.json", result["problems"][0])

    def test_non_object_manifest_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = self._manifest_dir(root)
            (md / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertFalse(result["allow_new_gpu_request"])

    def test_clean_roots_are_confirmed_absent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # A root with no manifest dir at all: zero manifests, clean.
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["state"], "CONFIRMED_ABSENT")
            self.assertTrue(result["allow_new_gpu_request"])
            # An empty manifest dir, plus a non-matching manifest.
            md = self._manifest_dir(root)
            self._write(md, "other.json", {
                "run_id": "h3-other",
                "production_task_id": "someone-else",
                "status": "done",
            })
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["state"], "CONFIRMED_ABSENT")
            self.assertTrue(result["allow_new_gpu_request"])

    def test_found_blocks_new_gpu_and_still_reports_problems(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = self._manifest_dir(root)
            self._write(md, "a.json", {
                "run_id": "h3-found",
                "production_task_id": FAKE_TASK_ID,
                "status": "done",
            })
            (md / "broken.json").write_text("{oops", encoding="utf-8")
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["state"], "FOUND")
            self.assertFalse(result["allow_new_gpu_request"])
            self.assertTrue(result["problems"])

    def test_multiple_manifests_sorted_by_reliable_time(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            md = self._manifest_dir(root)
            # Filenames are deliberately anti-chronological: z.json is
            # the newest by time, a.json is older, m.json has no time.
            self._write(md, "z.json", {
                "run_id": "h3-newest",
                "production_task_id": FAKE_TASK_ID,
                "status": "done",
                "created_at_utc": "2026-01-01T00:00:00Z",
                "updated_at_utc": "2026-11-22T00:00:00Z",
            })
            self._write(md, "a.json", {
                "run_id": "h3-older",
                "production_task_id": FAKE_TASK_ID,
                "status": "done",
                "created_at_utc": "2026-11-20T00:00:00Z",
                "updated_at_utc": "2026-11-20T00:00:00Z",
            })
            self._write(md, "m.json", {
                "run_id": "h3-notime",
                "production_task_id": FAKE_TASK_ID,
                "status": "done",
            })
            result = check_existing_task_state(FAKE_TASK_ID, [root])
            self.assertEqual(result["state"], "FOUND")
            self.assertEqual(
                [m["run_id"] for m in result["manifests"]],
                ["h3-newest", "h3-older", "h3-notime"],
            )
            self.assertEqual(
                result["manifests"][0]["time_source"], "updated_at_utc"
            )
            self.assertEqual(
                result["manifests"][0]["sort_time"], "2026-11-22T00:00:00Z"
            )
            self.assertEqual(result["manifests"][2]["time_source"], "none")
            self.assertIsNone(result["manifests"][2]["sort_time"])


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

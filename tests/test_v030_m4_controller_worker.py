"""v0.3.0 M4 tests: warm-pool controller + worker.

Covers --keep-warm-ttl validation, the five claim gates, the warm-arm
tail and warm reuse through the ChainHarness, and the worker-side warm
helpers. Offline only (0 CU): all Colab CLI, Drive, and sleep calls are
mocked/injected. No warm session is ever created live.
"""

from __future__ import annotations

import json
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_drive_token as h3_drive_token  # noqa: E402
import h3_gpu_lease_registry as reg_mod  # noqa: E402
import h3_remote_worker as hrw  # noqa: E402
import h3_run_manifest as rm  # noqa: E402
import h3_session_watchdog as hwd  # noqa: E402
from tests.test_fault_injection import (  # noqa: E402
    ChainHarness,
    FAKE_TOKEN_DOC,
    RUN_ID,
    TASK,
    _args as _run_args,
    _FIXTURE_DIR,
)

OWNER = "owner-test-0001"
COMFY_REF = "61e524bdba7a4e193b45c2697f1af83c1beb86d6"
WARM_SID = "muse-h3-warm0001"
FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


class ValidateKeepWarmTtlTest(unittest.TestCase):
    def test_zero_disables(self):
        self.assertEqual(hc._validate_keep_warm_ttl(0), 0)

    def test_bounds_are_inclusive(self):
        self.assertEqual(hc._validate_keep_warm_ttl(60), 60)
        self.assertEqual(hc._validate_keep_warm_ttl(900), 900)

    def test_out_of_range_rejected(self):
        for bad in (1, 59, 901, 3600, -1):
            with self.assertRaises(ValueError, msg=str(bad)):
                hc._validate_keep_warm_ttl(bad)

    def test_string_coerced_and_invalid_rejected(self):
        self.assertEqual(hc._validate_keep_warm_ttl("300"), 300)
        # None means "not given" and disables the pool, like the 0 default.
        self.assertEqual(hc._validate_keep_warm_ttl(None), 0)
        with self.assertRaises(ValueError):
            hc._validate_keep_warm_ttl("warm")


def _claim_args(cache_manifest):
    return SimpleNamespace(
        run_id=RUN_ID, production_task_id=TASK, comfy_ref=COMFY_REF,
        cache_manifest=str(cache_manifest), max_rate_cu_h=14.0,
        max_wall_sec=3600, keep_warm_ttl=0)


class TryClaimWarmSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest_file = self.root / "manifest.json"
        self.manifest_file.write_bytes(b"fake-manifest")
        self.manifest_sha = hc._file_sha256(self.manifest_file)
        self.registry = reg_mod.LeaseRegistry(
            path=self.root / "gpu_leases.json", mirror=None)
        self.args = _claim_args(self.manifest_file)
        self.pre = {"cu_balance": 150.0, "rate": 5.0, "sessions": []}
        self.patches = [
            mock.patch.object(hc, "_lease_registry",
                              return_value=self.registry),
            mock.patch.object(reg_mod, "current_owner_id",
                              return_value=OWNER),
            mock.patch.object(hwd, "query_session_state",
                              return_value=hwd.SessionState.ACTIVE),
            mock.patch.object(hc, "exec_script",
                              return_value={"returncode": 0,
                                            "stdout": "MUSE_WARM_HEALTH_OK {}",
                                            "stderr": ""}),
            mock.patch.object(hc, "allowed_runtime_seconds",
                              return_value=3600),
            mock.patch.object(hwd, "update_session_deadline"),
            mock.patch.object(hc, "record"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def _seed_warm(self, sid=WARM_SID, comfy_ref=COMFY_REF, sha=None,
                   spent=0.1, expires=FUTURE, started_at_utc=None):
        self.registry.register_lease(
            sid, "h3-m4-prev-run", TASK, "G4", FUTURE, 25.0, OWNER,
            started_at_utc=started_at_utc)
        return self.registry.mark_warm_idle(
            sid, warm_ttl_sec=300, warm_expires_at_utc=expires,
            session_budget_spent_cu=spent, run_id="h3-m4-prev-run",
            comfy_ref=comfy_ref,
            model_manifest_sha256=sha if sha is not None
            else self.manifest_sha,
            rate_cu_h=5.0)

    def _claim(self):
        return hc._try_claim_warm_session(
            self.args, mock.Mock(), {}, self.root, self.pre,
            time.monotonic())

    def test_no_warm_leases_returns_none(self):
        self.assertIsNone(self._claim())

    def test_happy_path_claims_and_updates_deadline(self):
        self._seed_warm()
        claim = self._claim()
        self.assertIsNotNone(claim)
        self.assertEqual(claim["session"], WARM_SID)
        self.assertEqual(claim["budget_sec"], 3600)
        self.assertEqual(claim["rate_cu_h"], 5.0)
        lease = self.registry.get_lease(WARM_SID)
        self.assertIs(lease["warm_idle"], False)
        self.assertIsNone(lease["warm_expires_at_utc"])
        self.assertIn(RUN_ID, lease["warm_run_ids"])
        hwd.update_session_deadline.assert_called_once()

    def test_comfy_ref_mismatch_returns_none(self):
        self._seed_warm(comfy_ref="deadbeef" * 5)
        self.assertIsNone(self._claim())

    def test_manifest_sha_mismatch_returns_none(self):
        self._seed_warm(sha="0" * 64)
        self.assertIsNone(self._claim())

    def test_lifetime_cap_returns_none(self):
        self._seed_warm()
        path = self.root / "gpu_leases.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["leases"][0]["created_at_utc"] = PAST
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertIsNone(self._claim())

    def test_budget_gate_returns_none(self):
        self._seed_warm(spent=24.5)
        self.assertIsNone(self._claim())

    def test_session_not_active_returns_none(self):
        self._seed_warm()
        hwd.query_session_state.return_value = hwd.SessionState.STOPPED
        self.assertIsNone(self._claim())

    def test_health_probe_without_ok_marker_returns_none(self):
        self._seed_warm()
        hc.exec_script.return_value = {"returncode": 0, "stdout": "",
                                       "stderr": ""}
        self.assertIsNone(self._claim())


class WorkerWarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _object_info(self, missing=()):
        nodes = {"MiniMaxH3ReferenceToVideo": {}, "SaveVideo": {},
                 "UNETLoader": {}, "CLIPLoader": {}}
        for node in missing:
            nodes.pop(node, None)
        return nodes

    def test_check_warm_comfy_alive(self):
        with mock.patch.object(hrw, "get_json",
                               return_value=self._object_info()):
            info = hrw.check_warm_comfy_alive("http://127.0.0.1:8188")
        self.assertIsNotNone(info)
        self.assertIn("SaveVideo", info)
        with mock.patch.object(
                hrw, "get_json",
                return_value=self._object_info(missing=("CLIPLoader",))):
            self.assertIsNone(hrw.check_warm_comfy_alive("http://x"))
        with mock.patch.object(hrw, "get_json",
                               return_value=["not", "a", "dict"]):
            self.assertIsNone(hrw.check_warm_comfy_alive("http://x"))
        with mock.patch.object(hrw, "get_json",
                               side_effect=ConnectionError("down")):
            self.assertIsNone(hrw.check_warm_comfy_alive("http://x"))

    def _manifest(self, sizes):
        files = [{"path": f"diffusion_models/model_{i}.safetensors",
                  "size": size, "sha256": "a" * 64}
                 for i, size in enumerate(sizes)]
        return {"files": files}

    def test_all_models_present_in_comfy(self):
        comfy = self.root / "ComfyUI"
        models = comfy / "models" / "diffusion_models"
        models.mkdir(parents=True)
        manifest = self._manifest([10, 20, 30, 40])
        for item in manifest["files"]:
            path = comfy / "models" / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * item["size"])
        self.assertIs(hrw._all_models_present_in_comfy(comfy, manifest),
                      True)
        # One file with the wrong size: not intact.
        bad = comfy / "models" / manifest["files"][0]["path"]
        bad.write_bytes(b"x" * 9)
        self.assertIs(hrw._all_models_present_in_comfy(comfy, manifest),
                      False)
        # Fewer than four files: never warm.
        self.assertIs(hrw._all_models_present_in_comfy(
            comfy, self._manifest([10, 20, 30])), False)

    def test_build_coldstart_metrics_warm_flag(self):
        started = time.monotonic() - 42.0
        warm = hrw.build_coldstart_metrics(started, vram_mib=97808,
                                           warm_session_reused=True)
        self.assertIs(warm["warm_session_reused"], True)
        self.assertEqual(warm["vram_mib"], 97808)
        cold = hrw.build_coldstart_metrics(started, vram_mib=97808,
                                           warm_session_reused=False)
        self.assertNotIn("warm_session_reused", cold)
        default = hrw.build_coldstart_metrics(started)
        self.assertNotIn("warm_session_reused", default)

    def test_cleanup_ephemeral_tokens(self):
        for name in ("drive_token.json", "drive_token_probe.json",
                     "drive_upload_token.json", "other.json"):
            (self.root / name).write_text("x", encoding="utf-8")
        hrw._cleanup_ephemeral_tokens(self.root)
        remaining = {p.name for p in self.root.iterdir()}
        self.assertEqual(remaining, {"other.json"})

    def test_terminate_comfy_pid(self):
        pid_file = self.root / "comfyui.pid"
        pid_file.write_text("12345", encoding="utf-8")
        with mock.patch.object(hrw.os, "kill") as kill:
            hrw._terminate_comfy_pid(self.root)
        kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertFalse(pid_file.exists())
        # Unparseable pid: no kill, still unlinked.
        pid_file.write_text("not-a-pid", encoding="utf-8")
        with mock.patch.object(hrw.os, "kill") as kill:
            hrw._terminate_comfy_pid(self.root)
        kill.assert_not_called()
        self.assertFalse(pid_file.exists())
        # Missing pid file: no-op.
        hrw._terminate_comfy_pid(self.root)


class WarmArmChainTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = reg_mod.LeaseRegistry(
            path=Path(self.tmp.name) / "gpu_leases.json", mirror=None)

    def tearDown(self):
        self.h.close()

    def _run_with_registry(self, keep_warm_ttl, register_in_registry):
        """Run the happy-path chain with hc._lease_registry pointed at
        the tmp registry; optionally mirror _register_gpu_lease into it
        so the arm tail finds the lease."""
        h = self.h
        patches = [
            mock.patch.object(hc, "_lease_registry",
                              return_value=self.registry),
        ]
        if register_in_registry:
            def _wrap_register(session, gpu_model, run_id,
                               production_task_id, stop_deadline_utc,
                               budget_cap_cu, max_wall_sec=None):
                self.registry.register_lease(
                    session, run_id, production_task_id, gpu_model,
                    stop_deadline_utc, budget_cap_cu, OWNER,
                    max_wall_sec=max_wall_sec)
                return {"session_id": session, "version": 1,
                        "deadline_final": False}

            patches.append(mock.patch.object(
                hc, "_register_gpu_lease", side_effect=_wrap_register))
        for p in patches:
            p.start()
        try:
            return hc.run_variant(_run_args(keep_warm_ttl=keep_warm_ttl))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_arm_pools_warm_session_instead_of_stopping(self):
        res = self._run_with_registry(keep_warm_ttl=300,
                                      register_in_registry=True)
        h = self.h
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        # No stop CLI call: the session was pooled, not stopped.
        self.assertEqual(h.stop_cli_sessions, [])
        manifest = h.manifest()
        self.assertEqual(manifest["session_state"], "WARM_POOLED")
        self.assertIs(manifest["artifacts"]["warm_lease_armed"], True)
        self.assertIn("warm_expires_at_utc",
                      manifest["artifacts"])
        self.assertIn("warm_session_pooled", h.audit_jsonl_text())
        sid = h.allocate_ids[0]
        lease = self.registry.get_lease(sid)
        self.assertIsNotNone(lease)
        self.assertIs(lease["warm_idle"], True)
        self.assertEqual(lease["warm_ttl_sec"], 300)
        self.assertIn(RUN_ID, lease["warm_run_ids"])
        self.assertEqual(lease["comfy_ref"], COMFY_REF)

    def test_arm_failure_falls_back_to_stop(self):
        # The lease was never mirrored into the tmp registry, so
        # mark_warm_idle returns None and the arm fails closed.
        res = self._run_with_registry(keep_warm_ttl=300,
                                      register_in_registry=False)
        h = self.h
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        sid = h.allocate_ids[0]
        self.assertEqual(h.stop_cli_sessions, [sid])
        manifest = h.manifest()
        self.assertEqual(manifest["session_state"], "STOPPED")
        self.assertIn("warm_pool_arm_failed_stopping",
                      h.audit_jsonl_text())

    def test_ttl_zero_keeps_cold_stop(self):
        res = self._run_with_registry(keep_warm_ttl=0,
                                      register_in_registry=True)
        h = self.h
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        sid = h.allocate_ids[0]
        self.assertEqual(h.stop_cli_sessions, [sid])
        self.assertEqual(h.manifest()["session_state"], "STOPPED")
        self.assertNotIn("warm_session_pooled", h.audit_jsonl_text())


class WarmReuseChainTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = reg_mod.LeaseRegistry(
            path=Path(self.tmp.name) / "gpu_leases.json", mirror=None)

    def tearDown(self):
        self.h.close()

    def _seed_warm_session(self):
        """A previous run's pooled session: registry lease (warm idle,
        matching comfy_ref + manifest SHA) + an ACTIVE managed record."""
        manifest_sha = hc._file_sha256(
            Path(_FIXTURE_DIR) / "verified_manifest.json")
        self.registry.register_lease(
            WARM_SID, "h3-m4-prev-run", TASK, "G4", FUTURE, 25.0, OWNER)
        marked = self.registry.mark_warm_idle(
            WARM_SID, warm_ttl_sec=300, warm_expires_at_utc=FUTURE,
            session_budget_spent_cu=0.1, run_id="h3-m4-prev-run",
            comfy_ref=COMFY_REF, model_manifest_sha256=manifest_sha,
            rate_cu_h=5.0)
        self.assertIsNotNone(marked)
        hwd.record_session(WARM_SID, "G4", "h3-m4-prev-run", FUTURE,
                           production_task_id=TASK, status="active")
        self.h.inspect_plan = [
            {"cu_balance": 150.0, "rate": 5.0,
             "sessions": [{"name": WARM_SID, "gpu": "G4"}]},
            {"cu_balance": 150.0, "rate": 5.0, "sessions": []},
        ]
        # Query plan: claim probe (ACTIVE), stop probe 1 (ACTIVE -> the
        # stop CLI call runs), stop probe 2 (STOPPED -> confirmed).
        self.h.query_plan = [hwd.SessionState.ACTIVE,
                             hwd.SessionState.ACTIVE,
                             hwd.SessionState.STOPPED]

    def _run_reuse(self):
        patches = [
            mock.patch.object(hc, "_lease_registry",
                              return_value=self.registry),
            mock.patch.object(reg_mod, "current_owner_id",
                              return_value=OWNER),
            mock.patch.object(h3_drive_token, "mint_file_token",
                              return_value=FAKE_TOKEN_DOC),
        ]
        for p in patches:
            p.start()
        try:
            return hc.run_variant(_run_args(keep_warm_ttl=0))
        finally:
            for p in reversed(patches):
                p.stop()

    def test_second_run_reuses_warm_session_without_allocating(self):
        self._seed_warm_session()
        res = self._run_reuse()
        h = self.h
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        # No new GPU was allocated.
        self.assertEqual(h.allocate_ids, [])
        self.assertNotIn(["new"], [c[:1] for c in h.run_cli_calls])
        manifest = h.manifest()
        self.assertIs(manifest["artifacts"]["warm_session_reused"], True)
        self.assertEqual(manifest["bootstrap"]["mode"], "warm_reuse")
        self.assertEqual(manifest["bootstrap"]["cli_round_trips"], 2)
        self.assertIn("warm_session_claimed", h.audit_jsonl_text())
        self.assertIn("warm_session_reused", h.audit_jsonl_text())
        # keep_warm_ttl=0: the reused session is stopped normally.
        self.assertEqual(h.stop_cli_sessions, [WARM_SID])
        self.assertEqual(manifest["session_state"], "STOPPED")
        # The stage-2 task.json carried the warm flags.
        task_path = h.runs_root / f"task_{WARM_SID}.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertIs(task["warm_session_reused"], True)
        self.assertEqual(task["drive_cache_dir"], "/content/nvme_models/h3")
        self.assertEqual(task["keep_warm_ttl_sec"], 0)

    def test_unhealthy_resident_server_refuses_reuse(self):
        self._seed_warm_session()
        self.h.warm_probe_ok = False
        with self.assertRaisesRegex(
                RuntimeError, "Existing Colab sessions present"):
            self._run_reuse()
        # No new GPU was allocated beside the live session.
        self.assertEqual(self.h.allocate_ids, [])


if __name__ == "__main__":
    unittest.main()

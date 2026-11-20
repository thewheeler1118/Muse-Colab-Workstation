"""v0.3.0 M4 tests: warm-pool infrastructure — registry, manifest gate,
reliability counts, shutdown-executor warm mode, health probe.

Offline only (0 CU): no GPU, no CU, no Drive, no live Colab. Fixtures are
built through the real modules on temp roots.
"""

import contextlib
import datetime
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h3_gpu_lease_registry as reg_mod
import h3_reliability as rel
import h3_run_manifest as rm
import h3_shutdown_executor as hse
import h3_warm_health_probe as probe

TASK = "h3-m4-task"
OWNER = "owner-test-0001"
COMFY_REF = "61e524bdba7a4e193b45c2697f1af83c1beb86d6"
SID = "muse-h3-warm0001"
RUN_ID = "h3-m4-run-1"
FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


def _registry(tmp):
    return reg_mod.LeaseRegistry(path=Path(tmp) / "gpu_leases.json",
                                 mirror=None)


def _register(registry, sid=SID, run_id=RUN_ID, owner=OWNER,
              comfy_ref=COMFY_REF, started_at_utc=None):
    return registry.register_lease(
        sid, run_id, TASK, "G4", FUTURE, 25.0, owner,
        started_at_utc=started_at_utc)


def _mark_warm(registry, sid=SID, run_id=RUN_ID, spent=0.1,
               expires=FUTURE, comfy_ref=COMFY_REF, sha=None):
    return registry.mark_warm_idle(
        sid, warm_ttl_sec=300, warm_expires_at_utc=expires,
        session_budget_spent_cu=spent, run_id=run_id,
        comfy_ref=comfy_ref, model_manifest_sha256=sha, rate_cu_h=5.0)


def _walk_to_verified(root, run_id, session_state="ACTIVE"):
    rm.create_run_manifest(root, TASK, run_id, budget={})
    rm.update_run(root, run_id, session_id=SID,
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": "a" * 64, "local_frames": 274})
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  session_state=session_state)


class RegistryWarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = _registry(self.tmp.name)

    def test_old_lease_normalizes_with_warm_defaults(self):
        # A lease written before M4 lacks every warm field; it reads as
        # not warm, with zero prior spend.
        doc = {"leases": [{"session_id": SID, "run_id": RUN_ID,
                           "production_task_id": TASK, "gpu_model": "G4",
                           "stop_deadline_utc": FUTURE, "budget_cap_cu": 25.0,
                           "owner_id": OWNER,
                           "last_confirmed_state": "ACTIVE",
                           "started_at_utc": FUTURE,
                           "created_at_utc": FUTURE,
                           "version": 1}],
               "version": 1, "updated_at_utc": FUTURE,
               "mirror_pending": False, "mirror_error": None,
               "mirror_file_id": None, "mirror_version": None}
        Path(self.tmp.name, "gpu_leases.json").write_text(
            json.dumps(doc), encoding="utf-8")
        leases = _registry(self.tmp.name).list_leases()
        self.assertEqual(len(leases), 1)
        lease = leases[0]
        self.assertIs(lease["warm_idle"], False)
        self.assertIsNone(lease["warm_expires_at_utc"])
        self.assertEqual(lease["warm_ttl_sec"], 0)
        self.assertEqual(lease["session_budget_spent_cu"], 0.0)
        self.assertEqual(lease["warm_run_ids"], [])
        self.assertIsNone(lease["comfy_ref"])
        self.assertIsNone(lease["model_manifest_sha256"])
        self.assertIsNone(lease["rate_cu_h"])

    def test_mark_warm_idle_sets_fields_and_appends_run(self):
        _register(self.registry)
        marked = _mark_warm(self.registry, sha="c" * 64)
        self.assertIsNotNone(marked)
        self.assertIs(marked["warm_idle"], True)
        self.assertEqual(marked["stop_deadline_utc"], FUTURE)
        self.assertIs(marked["deadline_final"], True)
        self.assertEqual(marked["warm_expires_at_utc"], FUTURE)
        self.assertEqual(marked["warm_ttl_sec"], 300)
        self.assertEqual(marked["session_budget_spent_cu"], 0.1)
        self.assertEqual(marked["warm_run_ids"], [RUN_ID])
        self.assertEqual(marked["comfy_ref"], COMFY_REF)
        self.assertEqual(marked["model_manifest_sha256"], "c" * 64)
        self.assertEqual(marked["rate_cu_h"], 5.0)
        # A second mark with another run id appends, never duplicates.
        marked2 = _mark_warm(self.registry, run_id="h3-m4-run-2")
        self.assertEqual(marked2["warm_run_ids"], [RUN_ID, "h3-m4-run-2"])

    def test_mark_warm_idle_refuses_non_active_and_missing(self):
        _register(self.registry)
        self.registry.confirm_stopped(SID, "STOPPED")
        self.assertIsNone(_mark_warm(self.registry))
        self.assertIsNone(_mark_warm(self.registry, sid="muse-h3-nope"))

    def test_claim_warm_lease_transitions_and_refuses_second(self):
        _register(self.registry)
        _mark_warm(self.registry)
        claimed = self.registry.claim_warm_lease(
            SID, "h3-m4-run-2", TASK, FUTURE)
        self.assertIsNotNone(claimed)
        self.assertIs(claimed["warm_idle"], False)
        self.assertIsNone(claimed["warm_expires_at_utc"])
        self.assertEqual(claimed["run_id"], "h3-m4-run-2")
        self.assertEqual(claimed["stop_deadline_utc"], FUTURE)
        self.assertEqual(claimed["warm_run_ids"], [RUN_ID, "h3-m4-run-2"])
        # Already claimed: a second claim is refused.
        self.assertIsNone(self.registry.claim_warm_lease(
            SID, "h3-m4-run-3", TASK, FUTURE))
        # Not warm at all: refused too.
        _register(self.registry, sid="muse-h3-cold0001")
        self.assertIsNone(self.registry.claim_warm_lease(
            "muse-h3-cold0001", "h3-m4-run-9", TASK, FUTURE))

    def test_find_warm_leases_filters_and_orders_newest_first(self):
        old = _register(self.registry, sid="muse-h3-old0001",
                        run_id="h3-m4-run-old",
                        started_at_utc="2026-11-19T00:00:00Z")
        new = _register(self.registry, sid="muse-h3-new0001",
                        run_id="h3-m4-run-new",
                        started_at_utc="2026-11-20T00:00:00Z")
        _mark_warm(self.registry, sid="muse-h3-old0001",
                   run_id="h3-m4-run-old")
        _mark_warm(self.registry, sid="muse-h3-new0001",
                   run_id="h3-m4-run-new")
        # Expired warm lease: excluded.
        _register(self.registry, sid="muse-h3-exp0001",
                  run_id="h3-m4-run-exp")
        _mark_warm(self.registry, sid="muse-h3-exp0001",
                   run_id="h3-m4-run-exp", expires=PAST)
        # Other owner's warm lease: excluded by owner filter.
        _register(self.registry, sid="muse-h3-other001",
                  run_id="h3-m4-run-other", owner="owner-other-0002")
        _mark_warm(self.registry, sid="muse-h3-other001",
                   run_id="h3-m4-run-other")
        # Stopped warm lease: excluded.
        _register(self.registry, sid="muse-h3-stop001",
                  run_id="h3-m4-run-stop")
        _mark_warm(self.registry, sid="muse-h3-stop001",
                   run_id="h3-m4-run-stop")
        self.registry.confirm_stopped("muse-h3-stop001", "STOPPED")

        found = self.registry.find_warm_leases(owner_id=OWNER)
        self.assertEqual([x["session_id"] for x in found],
                         ["muse-h3-new0001", "muse-h3-old0001"])
        # Without an owner filter the other owner's lease appears too.
        found_all = self.registry.find_warm_leases()
        self.assertIn("muse-h3-other001",
                      [x["session_id"] for x in found_all])
        self.assertNotIn("muse-h3-exp0001",
                         [x["session_id"] for x in found_all])

    def test_confirm_stopped_clears_warm_fields(self):
        _register(self.registry)
        _mark_warm(self.registry)
        self.registry.confirm_stopped(SID, "STOPPED")
        lease = self.registry.get_lease(SID)
        self.assertEqual(lease["last_confirmed_state"], "STOPPED")
        self.assertIs(lease["warm_idle"], False)
        self.assertIsNone(lease["warm_expires_at_utc"])


class ManifestWarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _warm_pooled_manifest(self, run_id=RUN_ID, armed=True):
        _walk_to_verified(self.root, run_id, session_state="WARM_POOLED")
        rm.update_run(self.root, run_id,
                      artifacts={"warm_lease_armed": armed,
                                 "warm_expires_at_utc": FUTURE})
        return rm.load_run_manifest(self.root, run_id)

    def test_task_completed_allowed_with_warm_pooled_and_armed(self):
        self._warm_pooled_manifest()
        updated, applied, anomaly = rm.update_run(
            self.root, RUN_ID, task_state="TASK_COMPLETED")
        self.assertIsNone(anomaly)
        self.assertIs(applied, True)
        self.assertEqual(updated["task_state"], "TASK_COMPLETED")
        self.assertEqual(updated["session_state"], "WARM_POOLED")

    def test_task_completed_refused_when_unarmed_or_active(self):
        self._warm_pooled_manifest(armed=False)
        _, applied, anomaly = rm.update_run(
            self.root, RUN_ID, task_state="TASK_COMPLETED")
        self.assertIs(applied, False)
        self.assertIn("not STOPPED", anomaly or "")
        # Plain ACTIVE session (no warm arm) still fails closed.
        _walk_to_verified(self.root, "h3-m4-run-2", session_state="ACTIVE")
        _, applied2, anomaly2 = rm.update_run(
            self.root, "h3-m4-run-2", task_state="TASK_COMPLETED")
        self.assertIs(applied2, False)
        self.assertIn("not STOPPED", anomaly2 or "")

    def test_record_warm_session_stopped_transitions(self):
        self._warm_pooled_manifest()
        rm.update_run(self.root, RUN_ID, task_state="TASK_COMPLETED")
        events_before = rm.read_audit_log(self.root, RUN_ID)
        stopped = rm.record_warm_session_stopped(
            self.root, RUN_ID, session_id=SID)
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped["session_state"], "STOPPED")
        self.assertIs(stopped["artifacts"]["warm_lease_armed"], False)
        self.assertIn("warm_stopped_at_utc", stopped["artifacts"])
        events_after = rm.read_audit_log(self.root, RUN_ID)
        transitions = [e for e in events_after
                       if e.get("event") == "STAGE"
                       and e.get("detail", {}).get("from") == "WARM_POOLED"]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["detail"]["to"],
                         "GPU_STOP_CONFIRMED")
        # Idempotent: a second call returns the manifest unchanged and
        # records no duplicate STAGE event.
        again = rm.record_warm_session_stopped(
            self.root, RUN_ID, session_id=SID)
        self.assertEqual(again["session_state"], "STOPPED")
        self.assertEqual(len(rm.read_audit_log(self.root, RUN_ID)),
                         len(events_after))

    def test_record_warm_session_stopped_refusals(self):
        self.assertIsNone(rm.record_warm_session_stopped(
            self.root, "h3-m4-missing", session_id=SID))
        self._warm_pooled_manifest()
        # Wrong session id: the manifest belongs to another session.
        self.assertIsNone(rm.record_warm_session_stopped(
            self.root, RUN_ID, session_id="muse-h3-other-session"))
        # ACTIVE (never pooled) manifest: refused.
        _walk_to_verified(self.root, "h3-m4-run-2", session_state="ACTIVE")
        self.assertIsNone(rm.record_warm_session_stopped(
            self.root, "h3-m4-run-2", session_id=SID))

    def test_validate_coldstart_metrics_warm_flag(self):
        report = {"setup_seconds": 42.5, "comfy_install_seconds": 12.5,
                  "restore_seconds": 30.0, "wheelhouse_hit": True,
                  "models_reused": 3, "models_downloaded": 1,
                  "vram_mib": 81920, "warm_session_reused": True}
        cleaned = rm.validate_coldstart_metrics(report)
        self.assertIs(cleaned["warm_session_reused"], True)
        report2 = dict(report)
        del report2["warm_session_reused"]
        cleaned2 = rm.validate_coldstart_metrics(report2)
        self.assertNotIn("warm_session_reused", cleaned2)
        report3 = dict(report, warm_session_reused="yes")
        with self.assertRaises(ValueError):
            rm.validate_coldstart_metrics(report3)


def _walk_to_validated(root, run_id):
    rm.create_run_manifest(root, TASK, run_id, budget={})
    rm.update_run(root, run_id, session_id=SID,
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": "a" * 64, "local_frames": 274})


def _completed_run(root, run_id, report=None, warm_pooled=False):
    _walk_to_validated(root, run_id)
    if warm_pooled:
        rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                      session_state="WARM_POOLED",
                      artifacts={"warm_lease_armed": True,
                                 "warm_expires_at_utc": FUTURE})
    else:
        rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                      session_state="STOPPED")
        rm.update_run(root, run_id, task_state="DRIVE_VERIFIED")
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")
    if report is not None:
        rm.update_run(root, run_id, coldstart_metrics=report)


class ReliabilityWarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_warm_counts_and_metric_d_excludes_pooled(self):
        # One normal completed run (metric D numerator) and one
        # warm-pooled completed run (no GPU_STOP_CONFIRMED, session
        # still WARM_POOLED): D counts 1/2.
        _completed_run(self.root, "h3-m4-run-0")
        _completed_run(self.root, "h3-m4-run-1", warm_pooled=True)
        report = rel.compute_reliability(self.root)
        self.assertEqual(report["warm_pooled_active_runs"], 1)
        self.assertEqual(report["warm_session_hits"], 0)
        metric_d = report["metrics"]["verified_shutdown_rate"]
        self.assertEqual(metric_d["denominator"], 2)
        self.assertEqual(metric_d["numerator"], 1)

    def test_warm_session_hits_count_metrics_and_artifacts(self):
        warm_report = {"setup_seconds": 3.0, "comfy_install_seconds": 0.0,
                       "restore_seconds": 0.0, "wheelhouse_hit": None,
                       "models_reused": 4, "models_downloaded": 0,
                       "vram_mib": 97808, "warm_session_reused": True}
        _completed_run(self.root, "h3-m4-run-0", warm_report)
        # A run whose artifacts (not metrics) carry the reuse flag.
        _completed_run(self.root, "h3-m4-run-1")
        rm.update_run(self.root, "h3-m4-run-1",
                      artifacts={"warm_session_reused": True})
        report = rel.compute_reliability(self.root)
        self.assertEqual(report["warm_session_hits"], 2)
        self.assertEqual(report["warm_pooled_active_runs"], 0)


class ExecutorWarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = _registry(self.tmp.name)

    def _now(self, iso):
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))

    def test_audit_force_stop_warm_bypasses_task_filter(self):
        _register(self.registry)
        _mark_warm(self.registry)
        # The warm lease belongs to TASK; audited against another task
        # it is still overdue when force_stop_warm is set...
        report = hse.audit_leases(
            self.registry, now=self._now("2026-11-20T00:00:00Z"),
            owner_id=OWNER, production_task_id="h3-other-task",
            force_stop_warm=True)
        self.assertEqual(
            [(e["session_id"], e["reason"]) for e in report["overdue"]],
            [(SID, "manual_stop_warm")])
        self.assertTrue(report["overdue"][0]["warm_idle"])
        # ...and merely skipped (not yet due) without the flag.
        report2 = hse.audit_leases(
            self.registry, now=self._now("2026-11-20T00:00:00Z"),
            owner_id=OWNER, production_task_id="h3-other-task")
        self.assertEqual(report2["overdue"], [])
        skipped = {s["session_id"]: s["reason"]
                   for s in report2["skipped"]}
        self.assertEqual(skipped.get(SID), "not_yet_due")
        self.assertTrue(
            [s for s in report2["skipped"]
             if s["session_id"] == SID][0]["warm_idle"])

    def test_audit_expired_warm_lease_is_overdue(self):
        _register(self.registry)
        _mark_warm(self.registry, expires=PAST)
        report = hse.audit_leases(
            self.registry, now=self._now("2026-11-20T00:00:00Z"),
            owner_id=OWNER, production_task_id="h3-other-task")
        self.assertEqual(
            [(e["session_id"], e["reason"]) for e in report["overdue"]],
            [(SID, "warm_ttl_expired")])

    def test_audit_warm_lease_without_deadline_is_skipped(self):
        _register(self.registry)
        _mark_warm(self.registry)
        # Strip both deadline fields from the stored doc.
        path = Path(self.tmp.name) / "gpu_leases.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["leases"][0]["warm_expires_at_utc"] = None
        doc["leases"][0]["stop_deadline_utc"] = None
        path.write_text(json.dumps(doc), encoding="utf-8")
        report = hse.audit_leases(
            _registry(self.tmp.name),
            now=self._now("2026-11-20T00:00:00Z"), owner_id=OWNER,
            force_stop_warm=False)
        skipped = {s["session_id"]: s["reason"]
                   for s in report["skipped"]}
        self.assertEqual(skipped.get(SID), "unparseable_deadline")

    def test_run_executor_stop_warm_only(self):
        # Warm lease (expired -> overdue) + a non-warm overdue lease
        # from the same task. Warm mode stops only the warm one and
        # records the pooled manifest as stopped.
        _register(self.registry, run_id="h3-m4-run-warm")
        _mark_warm(self.registry, run_id="h3-m4-run-warm", expires=PAST)
        _register(self.registry, sid="muse-h3-cold0001",
                  run_id="h3-m4-run-cold")
        path = Path(self.tmp.name) / "gpu_leases.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for lease in doc["leases"]:
            if lease["session_id"] == "muse-h3-cold0001":
                lease["stop_deadline_utc"] = PAST
                lease["deadline_final"] = True
        path.write_text(json.dumps(doc), encoding="utf-8")

        manifest_root = self.root / "manifests"
        manifest_root.mkdir()
        _walk_to_verified(manifest_root, "h3-m4-run-warm",
                          session_state="WARM_POOLED")
        rm.update_run(manifest_root, "h3-m4-run-warm",
                      artifacts={"warm_lease_armed": True,
                                 "warm_expires_at_utc": PAST})
        rm.update_run(manifest_root, "h3-m4-run-warm",
                      task_state="TASK_COMPLETED")

        stopped_calls = []

        def _stop(sid):
            stopped_calls.append(sid)
            return {"state": "STOPPED", "attempts": 1}

        report = hse.run_executor(
            _registry(self.tmp.name), confirm_stop=True,
            owner_id_fn=lambda: OWNER, stop_fn=_stop,
            stop_warm_only=True, manifest_root=manifest_root,
            now=self._now("2026-11-20T00:00:00Z"))
        self.assertEqual(stopped_calls, [SID])
        self.assertEqual([s["session_id"] for s in report["stopped"]],
                         [SID])
        skipped = {s["session_id"]: s["reason"]
                   for s in report["skipped"]}
        self.assertEqual(skipped.get("muse-h3-cold0001"),
                         "skipped_non_warm_in_warm_mode")
        self.assertEqual(report["pending"], [])
        lease = _registry(self.tmp.name).get_lease(SID)
        self.assertEqual(lease["last_confirmed_state"], "STOPPED")
        self.assertIs(lease["warm_idle"], False)
        manifest = rm.load_run_manifest(manifest_root, "h3-m4-run-warm")
        self.assertEqual(manifest["session_state"], "STOPPED")
        self.assertIs(manifest["artifacts"]["warm_lease_armed"], False)


class WarmProbeTest(unittest.TestCase):
    def _fake_urlopen(self, payload):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_probe_ok_when_all_required_nodes_present(self):
        info = {node: {"input": {}} for node in probe.REQUIRED_NODES}
        info["ExtraNode"] = {}
        with mock.patch.object(probe.urllib.request, "urlopen",
                               return_value=self._fake_urlopen(info)):
            result = probe.probe_warm_comfy()
        self.assertIs(result["ok"], True)
        self.assertEqual(result["node_count"], 5)
        self.assertEqual(result["required_nodes"],
                         list(probe.REQUIRED_NODES))

    def test_probe_fails_on_missing_node(self):
        info = {"MiniMaxH3ReferenceToVideo": {}, "SaveVideo": {},
                "UNETLoader": {}}
        with mock.patch.object(probe.urllib.request, "urlopen",
                               return_value=self._fake_urlopen(info)):
            with self.assertRaises(RuntimeError) as ctx:
                probe.probe_warm_comfy()
        self.assertIn("CLIPLoader", str(ctx.exception))

    def test_probe_fails_on_non_dict_payload(self):
        with mock.patch.object(probe.urllib.request, "urlopen",
                               return_value=self._fake_urlopen(["not", "a", "dict"])):
            with self.assertRaises(RuntimeError):
                probe.probe_warm_comfy()

    def test_main_prints_ok_line(self):
        info = {node: {} for node in probe.REQUIRED_NODES}
        buf = io.StringIO()
        with mock.patch.object(probe.urllib.request, "urlopen",
                               return_value=self._fake_urlopen(info)):
            with contextlib.redirect_stdout(buf):
                probe.main()
        self.assertTrue(buf.getvalue().startswith("MUSE_WARM_HEALTH_OK "))


if __name__ == "__main__":
    unittest.main()

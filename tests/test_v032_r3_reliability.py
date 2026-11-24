"""TASK-RELEASE-V032 R3: metric-F / lease-registry cross-check, Wilson 95%
CIs, A-vs-F comparison, and cold-start vs warm-reuse breakdown.

Offline only (0 CU): fixtures are built through the real
h3_run_manifest module plus hand-written lease-registry JSON on temp
roots — no GPU, no CU, no Drive. Written test-first: these tests fail
on the v0.3.1 baseline (no lease cross-check, no wilson_95_ci dict,
no a_vs_f_comparison, no startup_mode_breakdown).
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h3_reliability as rel
import h3_run_manifest as rm

TASK = "h3-r3-task-1"
SHA = hashlib.sha256(b"fake-mp4").hexdigest()


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _ledger(root, task=TASK, spent=2.5, budget=30.0, deadline=None,
            authorized=True):
    path = root / "model_cache" / f"h3_production_budget_{task}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"production_task_id": task, "project_budget_cu": budget,
           "hard_stop_balance_cu": 132.77, "max_observed_spent_cu": spent,
           "run_ids": [], "authorized": authorized}
    if deadline is not None:
        doc["deadline_utc"] = deadline
    path.write_text(json.dumps(doc))


def _good_run(root, run_id, task=TASK, session="muse-h3-aaaa0001"):
    """A run meeting all seven F conditions. Returns its completion ts."""
    rm.create_run_manifest(root, task, run_id, variant="A", budget={})
    rm.update_run(root, run_id, session_id=session,
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": SHA, "local_frames": 274})
    rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                  session_state="STOPPED")
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  artifacts={"delivery_state": "DELIVERED",
                             "drive_file_id": "drive-file-1"})
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")
    stages = rel._stage_times(root, run_id)
    return stages["TASK_COMPLETED"][-1]


def _warm_pooled_run(root, run_id, task=TASK,
                     session="muse-h3-aaaa0001"):
    """A run that completed with its GPU warm-pooled (WARM_POOLED,
    warm_lease_armed) instead of confirmed STOPPED. Returns its
    completion ts."""
    rm.create_run_manifest(root, task, run_id, variant="A", budget={})
    rm.update_run(root, run_id, session_id=session,
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": SHA, "local_frames": 274})
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  artifacts={"delivery_state": "DELIVERED",
                             "drive_file_id": "drive-file-1"})
    rm.update_run(root, run_id, task_state="TASK_COMPLETED",
                  session_state="WARM_POOLED",
                  artifacts={"warm_lease_armed": True})
    stages = rel._stage_times(root, run_id)
    return stages["TASK_COMPLETED"][-1]


def _lease(session_id, run_id, task=TASK, state="ACTIVE", warm_idle=False,
           deadline_final=True, warm_run_ids=None):
    """One lease-registry entry, mirroring h3_gpu_lease_registry."""
    return {"session_id": session_id, "run_id": run_id,
            "production_task_id": task, "owner_id": "0123456789abcdef",
            "gpu_model": "G4", "started_at_utc": "2026-11-20T10:00:00Z",
            "stop_deadline_utc": "2026-11-20T12:00:00Z",
            "deadline_final": deadline_final,
            "emergency_deadline_utc": None, "budget_cap_cu": 30.0,
            "last_confirmed_state": state,
            "last_audit_at_utc": "2026-11-20T10:00:00Z", "version": 1,
            "executor_claim": None, "stop_attempts": 0,
            "last_stop_error": None, "next_retry_at_utc": None,
            "warm_idle": warm_idle, "warm_expires_at_utc": None,
            "warm_ttl_sec": 0, "session_budget_spent_cu": 0.0,
            "warm_run_ids": list(warm_run_ids or []),
            "comfy_ref": None, "model_manifest_sha256": None,
            "rate_cu_h": None}


def _write_leases(root, leases):
    path = root / "model_cache" / "h3_gpu_leases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"leases": leases, "version": 1,
                                "updated_at_utc": "2026-11-20T10:00:00Z",
                                "mirror_pending": False,
                                "mirror_error": None,
                                "mirror_file_id": None,
                                "mirror_version": None}))


def _deadline_window(root, completed):
    deadline = _fmt(_parse(completed) + timedelta(hours=1))
    now = _fmt(_parse(completed) + timedelta(hours=2))
    _ledger(root, TASK, deadline=deadline)
    return now


def _cold_report(setup_seconds, wheelhouse_hit, warm=False):
    report = {"setup_seconds": setup_seconds,
              "comfy_install_seconds": 1.0, "restore_seconds": 2.0,
              "wheelhouse_hit": wheelhouse_hit, "models_reused": 0,
              "models_downloaded": 4, "vram_mib": 81920}
    if warm:
        report["warm_session_reused"] = True
    return report


def _completed_run_with_report(root, run_id, report=None):
    rm.create_run_manifest(root, TASK, run_id, budget={})
    rm.update_run(root, run_id, session_id="muse-h3-aaaa0001",
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": SHA, "local_frames": 274})
    rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                  session_state="STOPPED")
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED")
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")
    if report is not None:
        rm.update_run(root, run_id, coldstart_metrics=report)


class LeaseCrossCheckTest(unittest.TestCase):
    def test_lease_active_but_no_manifest_session_id_fails_f(self):
        # The discriminating case: run-1 fully succeeds and run-2's
        # manifest never got a session_id written back, so the old
        # manifest-only check (b) skips run-2 entirely. The lease
        # registry proves run-2 DID allocate session S2 and it was
        # never confirmed STOPPED — F must fail and name the session.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            rm.create_run_manifest(root, TASK, "run-2", variant="B",
                                   budget={})
            rm.update_run(root, "run-2", task_state="FAILED",
                          last_error="RuntimeError: boom")
            _write_leases(root, [_lease("muse-h3-aaaa0002", "run-2")])
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        conds = f["verdicts"][0]["failed_conditions"]
        lease_conds = [c for c in conds if "lease" in c]
        self.assertTrue(lease_conds, f"no lease reason in {conds}")
        self.assertTrue(any("muse-h3-aaaa0002" in c for c in lease_conds))
        self.assertTrue(any("STOPPED" in c for c in lease_conds))

    def test_lease_matched_by_task_only_fails_f(self):
        # A lease for this task whose run_id matches NO manifest at
        # all (no write-back anywhere) is still real allocation
        # evidence for the task and must fail F.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            _write_leases(root, [_lease("muse-h3-aaaa0009",
                                        "run-unknown")])
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        conds = f["verdicts"][0]["failed_conditions"]
        self.assertTrue(any("lease" in c and "muse-h3-aaaa0009" in c
                            for c in conds))

    def test_preregistered_failed_attempt_stopped_does_not_block_f(self):
        # P0-B pre-registration: `colab new` failed and the live-list
        # check confirmed no remote session, so the pre-registered
        # lease was marked STOPPED. That is NOT a leak and must not
        # fail F — neither the failed attempt's lease nor the
        # successful run's own confirmed-stopped lease.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            _write_leases(root, [
                _lease("muse-h3-aaaa0001", "run-1", state="STOPPED"),
                _lease("muse-h3-aaaa0002", "run-1", state="STOPPED",
                       deadline_final=False),
            ])
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "success")
        self.assertEqual(f["verdicts"][0]["failed_conditions"], [])
        self.assertEqual(f["verdicts"][0]["lease_evidence"], "present")
        self.assertEqual(f["verdicts"][0]["lease_outstanding"], [])

    def test_warm_idle_lease_not_yet_stopped_fails_f(self):
        # WARM_POOLED / warm_idle is a live, billable session that has
        # not been shut down and recycled yet: F must fail and the
        # reason must name the warm lease.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _warm_pooled_run(root, "run-1")
            _write_leases(root, [_lease("muse-h3-aaaa0001", "run-1",
                                        warm_idle=True)])
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (0, 1))
        conds = f["verdicts"][0]["failed_conditions"]
        lease_conds = [c for c in conds if "lease" in c]
        self.assertTrue(lease_conds, f"no lease reason in {conds}")
        self.assertTrue(any("warm" in c.lower() for c in lease_conds))
        self.assertTrue(any("muse-h3-aaaa0001" in c for c in lease_conds))

    def test_missing_lease_registry_is_failsafe_and_recorded(self):
        # No lease file at all: F is judged from manifest/audit
        # evidence only (existing behaviour preserved — a good run
        # still counts), but the absence must be RECORDED on the
        # verdict and the metric, never silently treated as "verified".
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["verdicts"][0]["verdict"], "success")
        self.assertEqual(f["verdicts"][0]["lease_evidence"], "missing")
        self.assertIn("missing", f["verdicts"][0]["lease_cross_check"])
        self.assertEqual(f["lease_cross_check"]["registry"], "missing")
        self.assertIn("no lease evidence",
                      f["lease_cross_check"]["detail"])

    def test_corrupt_lease_registry_is_failsafe_and_recorded(self):
        # An unreadable lease file must neither crash the report nor
        # silently pass: F falls back to manifest/audit evidence and
        # the corruption is recorded.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            path = root / "model_cache" / "h3_gpu_leases.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json")
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        f = report["metrics"]["deadline_bound_e2e_success_rate"]
        self.assertEqual((f["numerator"], f["denominator"]), (1, 1))
        self.assertEqual(f["verdicts"][0]["lease_evidence"], "corrupt")
        self.assertEqual(f["lease_cross_check"]["registry"], "corrupt")


class WilsonIntervalTest(unittest.TestCase):
    def test_wilson_95_ci_known_values(self):
        # Hand-checked against the Wilson formula (z = 1.96):
        # 73/73 is the zero-failure threshold for a 0.95 lower bound.
        self.assertEqual(rel.wilson_95_ci(73, 73),
                         {"lower": 0.95, "upper": 1.0})
        self.assertEqual(rel.wilson_95_ci(60, 60)["lower"], 0.9398)
        self.assertEqual(rel.wilson_95_ci(0, 10),
                         {"lower": 0.0, "upper": 0.2775})
        self.assertEqual(rel.wilson_95_ci(1, 1),
                         {"lower": 0.2065, "upper": 1.0})
        self.assertIsNone(rel.wilson_95_ci(0, 0))
        self.assertIsNone(rel.wilson_95_ci(5, 0))
        # The dict form agrees with the legacy list form.
        self.assertEqual(rel.wilson_95_ci(5, 10),
                         {"lower": rel.wilson_interval(5, 10)[0],
                          "upper": rel.wilson_interval(5, 10)[1]})

    def test_every_ratio_metric_carries_wilson_95_ci(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _completed_run_with_report(root, "run-1",
                                       _cold_report(10.0, True))
            report = rel.compute_reliability(root)
        for name, metric in report["metrics"].items():
            with self.subTest(metric=name):
                self.assertIn("wilson_95_ci", metric)
                self.assertIn("confidence_interval_95", metric)
                if metric["denominator"] == 0:
                    self.assertIsNone(metric["wilson_95_ci"])
                else:
                    self.assertEqual(
                        metric["wilson_95_ci"],
                        {"lower": metric["confidence_interval_95"][0],
                         "upper": metric["confidence_interval_95"][1]})
        self.assertIn("wilson_95_ci",
                      report["coldstart"]["wheelhouse_hit_rate"])

    def test_empty_root_wilson_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            report = rel.compute_reliability(d)
        for metric in report["metrics"].values():
            self.assertIsNone(metric["wilson_95_ci"])


class AvsFAndStartupBreakdownTest(unittest.TestCase):
    def test_a_vs_f_comparison_and_f95_not_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            now = _deadline_window(root, completed)
            report = rel.compute_reliability(root, now_utc=now)
        comp = report["a_vs_f_comparison"]
        self.assertEqual(comp["first_attempt_success_rate"]["rate"], 1.0)
        self.assertEqual(comp["first_attempt_success_rate"]["denominator"], 1)
        self.assertEqual(
            comp["deadline_bound_e2e_success_rate"]["rate"], 1.0)
        self.assertEqual(
            comp["deadline_bound_e2e_success_rate"]["denominator"], 1)
        # A counts terminal runs; F counts authorised tasks — different
        # denominators, compared side by side, never pooled.
        self.assertIn("73/73", comp["note"])
        self.assertIn("0.95", comp["note"])
        # Honest conclusion: one sample can never verify a 95% target.
        self.assertFalse(report["f_95_verified"])
        self.assertEqual(report["f_95_status"], "F_95_NOT_VERIFIED")
        self.assertTrue(report["f_95_evidence"])

    def test_f95_not_verified_on_empty_root(self):
        with tempfile.TemporaryDirectory() as d:
            report = rel.compute_reliability(d)
        self.assertFalse(report["f_95_verified"])
        self.assertEqual(report["f_95_status"], "F_95_NOT_VERIFIED")
        self.assertIn("unverified", report["f_95_evidence"].lower())

    def test_startup_mode_breakdown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _completed_run_with_report(root, "cold-1",
                                       _cold_report(10.0, True))
            rm.create_run_manifest(root, TASK, "cold-2", budget={})
            rm.update_run(root, "cold-2", session_id="muse-h3-aaaa0002",
                          session_state="ACTIVE",
                          task_state="GPU_ALLOCATED")
            rm.update_run(root, "cold-2",
                          coldstart_metrics=_cold_report(20.0, False))
            rm.update_run(root, "cold-2", task_state="FAILED",
                          last_error="RuntimeError: boom")
            _completed_run_with_report(root, "warm-1",
                                       _cold_report(5.0, True, warm=True))
            _completed_run_with_report(root, "unknown-1")
            report = rel.compute_reliability(root)
        bd = report["startup_mode_breakdown"]
        self.assertEqual(bd["cold_start"]["runs"], 2)
        self.assertEqual(bd["cold_start"]["completed"], 1)
        self.assertEqual(
            bd["cold_start"]["first_attempt_success_rate"]["rate"], 0.5)
        self.assertEqual(
            bd["cold_start"]["eventual_task_success_rate"]["rate"], 0.5)
        self.assertEqual(bd["warm_reuse"]["runs"], 1)
        self.assertEqual(bd["warm_reuse"]["completed"], 1)
        self.assertEqual(
            bd["warm_reuse"]["first_attempt_success_rate"]["rate"], 1.0)
        self.assertEqual(bd["unknown"]["runs"], 1)
        self.assertEqual(bd["unknown"]["completed"], 1)
        for mode in ("cold_start", "warm_reuse", "unknown"):
            self.assertIn(
                "wilson_95_ci",
                bd[mode]["first_attempt_success_rate"])
            self.assertIn(
                "wilson_95_ci",
                bd[mode]["eventual_task_success_rate"])

    def test_top_level_json_backward_compat(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _completed_run_with_report(root, "run-1",
                                       _cold_report(10.0, True))
            report = rel.compute_reliability(root)
        for key in ("root", "sample_scope", "sample_size", "runs_sampled",
                    "warm_session_hits", "warm_pooled_active_runs",
                    "observation_dates", "breakdown", "in_flight_run_ids",
                    "metrics", "coldstart", "target_95_verified",
                    "target_95_evidence", "e_unknown_ledger_runs",
                    "f_in_flight_tasks", "f_deadline_unknown_tasks",
                    "f_time_window_excluded_tasks"):
            self.assertIn(key, report, f"missing top-level key {key}")
        self.assertEqual(
            set(report["metrics"]),
            {"first_attempt_success_rate", "eventual_task_success_rate",
             "artifact_preservation_rate", "verified_shutdown_rate",
             "budget_compliance_rate", "deadline_bound_e2e_success_rate"})
        for metric in report["metrics"].values():
            for key in ("definition", "rate", "numerator", "denominator",
                        "confidence_interval_95", "status",
                        "unknown_reason"):
                self.assertIn(key, metric, f"missing metric key {key}")


class ReliabilityReportScriptTest(unittest.TestCase):
    def test_summary_flag_keeps_json_on_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            completed = _good_run(root, "run-1")
            now = _deadline_window(root, completed)
            proc = subprocess.run(
                [sys.executable, "scripts/reliability_report.py",
                 "--root", str(root), "--summary"],
                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
                timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        parsed = json.loads(proc.stdout)  # stdout stays pure JSON
        self.assertIn("a_vs_f_comparison", parsed)
        self.assertIn("startup_mode_breakdown", parsed)
        self.assertIn("first_attempt_success_rate", proc.stderr)
        self.assertIn("deadline_bound_e2e_success_rate", proc.stderr)
        self.assertIn("F_95_NOT_VERIFIED", proc.stderr)

    def test_default_run_is_pure_json(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _good_run(root, "run-1")
            proc = subprocess.run(
                [sys.executable, "scripts/reliability_report.py",
                 "--root", str(root)],
                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
                timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        parsed = json.loads(proc.stdout)
        self.assertIn("wilson_95_ci",
                      parsed["metrics"]["first_attempt_success_rate"])


if __name__ == "__main__":
    unittest.main()

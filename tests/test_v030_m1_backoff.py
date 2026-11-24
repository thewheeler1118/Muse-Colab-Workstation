"""v0.3.0 M1 unit & fault-injection tests: GPU 503 elastic backoff & error classification.

Muse-original tests for the Muse-original M1 reimplementation (Issue #14).
Strictly 0.00 CU: all Colab CLI, Drive, and sleep calls are mocked/injected.
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_budget as hb  # noqa: E402
import h3_controller as hc  # noqa: E402
import h3_run_manifest as rm  # noqa: E402
from tests.test_fault_injection import ChainHarness, RUN_ID, TASK, _args as _run_args  # noqa: E402


class ClassifyAllocationErrorTest(unittest.TestCase):
    def test_auth_fatal(self):
        for msg in (
            "HTTP 401 Unauthorized",
            "HTTP 403 Forbidden",
            "invalid credentials",
            "permission denied",
        ):
            self.assertEqual(hb.classify_allocation_error(msg), "AUTH_FATAL")

    def test_quota_exceeded(self):
        for msg in (
            "Quota exceeded for project",
            "usage limit reached",
            "compute units exhausted",
            "insufficient CU for this request",
            "billing disabled for this account",
        ):
            self.assertEqual(hb.classify_allocation_error(msg), "QUOTA_EXCEEDED")

    def test_capacity_transient(self):
        for msg in (
            "HTTP 503 Service Unavailable",
            "HTTP 429 Too Many Requests",
            "502 Bad Gateway",
            "504 Gateway Timeout",
            "412 Precondition Failed",
            "capacity exhausted",
            "no available GPU backend",
            "too many assignments",
            "stockout in this region",
        ):
            self.assertEqual(hb.classify_allocation_error(msg), "CAPACITY_TRANSIENT")

    def test_transport_transient(self):
        for msg in (
            "ProxyError: cannot connect to proxy",
            "SSL EOF occurred",
            "read timed out",
            "connection reset by peer",
            "HTTP 411 Length Required",
            "TLS handshake failed",
        ):
            self.assertEqual(hb.classify_allocation_error(msg), "TRANSPORT_TRANSIENT")

    def test_unknown_fatal(self):
        for msg in ("something completely different", "", "exit code 1"):
            self.assertEqual(hb.classify_allocation_error(msg), "UNKNOWN_FATAL")

    def test_precedence_auth_beats_capacity(self):
        # Auth is checked first: a 401 that also mentions 503 is fatal.
        self.assertEqual(
            hb.classify_allocation_error("401 Unauthorized (503 retry?)"),
            "AUTH_FATAL")
        self.assertEqual(
            hb.classify_allocation_error("quota exceeded; also 503"),
            "QUOTA_EXCEEDED")


class ComputeBackoffDelayTest(unittest.TestCase):
    def test_schedule_with_unit_jitter(self):
        self.assertEqual(hb.compute_backoff_delay(0, jitter_fn=lambda: 1.0), 15.0)
        self.assertEqual(hb.compute_backoff_delay(1, jitter_fn=lambda: 1.0), 45.0)
        self.assertEqual(hb.compute_backoff_delay(2, jitter_fn=lambda: 1.0), 90.0)

    def test_later_rounds_reuse_last_delay(self):
        self.assertEqual(hb.compute_backoff_delay(5, jitter_fn=lambda: 1.0), 90.0)
        self.assertEqual(hb.compute_backoff_delay(-1, jitter_fn=lambda: 1.0), 15.0)

    def test_jitter_scales_within_twenty_percent(self):
        self.assertEqual(hb.compute_backoff_delay(0, jitter_fn=lambda: 0.8), 12.0)
        self.assertEqual(hb.compute_backoff_delay(0, jitter_fn=lambda: 1.2), 18.0)
        self.assertEqual(hb.compute_backoff_delay(1, jitter_fn=lambda: 0.8), 36.0)

    def test_negative_factor_clamps_to_zero(self):
        self.assertEqual(hb.compute_backoff_delay(0, jitter_fn=lambda: -1.0), 0.0)

    def test_random_jitter_stays_in_band(self):
        for _ in range(10):
            delay = hb.compute_backoff_delay(0)
            self.assertGreaterEqual(delay, 12.0)
            self.assertLessEqual(delay, 18.0)


class ResolveAllocationRoundsTest(unittest.TestCase):
    def _ns(self, **over):
        return SimpleNamespace(**over)

    def test_raw_run_defaults_to_one(self):
        self.assertEqual(hc.resolve_allocation_rounds(self._ns(action="run")), 1)
        self.assertEqual(hc.resolve_allocation_rounds(self._ns()), 1)

    def test_deliver_defaults_to_three(self):
        self.assertEqual(hc.resolve_allocation_rounds(self._ns(action="deliver")), 3)

    def test_auto_recover_defaults_to_three(self):
        self.assertEqual(
            hc.resolve_allocation_rounds(self._ns(action="run", auto_recover=True)), 3)

    def test_explicit_value_wins(self):
        self.assertEqual(
            hc.resolve_allocation_rounds(self._ns(action="run", allocation_rounds=5)), 5)
        self.assertEqual(
            hc.resolve_allocation_rounds(self._ns(action="deliver", allocation_rounds=1)), 1)

    def test_out_of_range_rejected(self):
        for bad in (0, 6, -1, "abc"):
            with self.assertRaises(ValueError):
                hc.resolve_allocation_rounds(self._ns(action="run", allocation_rounds=bad))


class ElasticBackoffExecutionTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_round1_503_exhaustion_sleeps_and_succeeds_in_round2(self):
        h = self.h
        # v0.3.2 R6: Round 1: G4 (503) -> fast-forward -> A100 (503) ->
        # backoff 15.0s -> Round 2: G4 (503) -> fast-forward -> A100
        # succeeds. The successful allocation lands on A100, so the
        # fake probe reports A100 hardware.
        h.probe_output = "NVIDIA A100 80GB PCIe, 81920"
        h.new_failures = [
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
        ]
        sleeps = []
        args = _run_args(
            allocation_rounds=3,
            sleep_fn=lambda s: sleeps.append(s),
            jitter_fn=lambda: 1.0,
        )
        res = hc.run_variant(args)
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        # 3 failed attempts + 1 succeeded attempt = 4 allocations attempted
        self.assertEqual(len(h.allocate_ids), 4)
        # Backoff delay of 15.0s recorded (alongside 5s late-alloc checks)
        self.assertIn(15.0, sleeps)
        # GPU_ALLOCATION_BACKOFF recorded in both h3_audit.jsonl and run manifest audit log
        self.assertIn("GPU_ALLOCATION_BACKOFF", h.audit_jsonl_text())
        audit_path = rm._audit_path(h.root, RUN_ID)
        audit_events = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        backoff_events = [
            e for e in audit_events
            if e.get("event") == "GPU_ALLOCATION_BACKOFF"
        ]
        self.assertEqual(len(backoff_events), 1)
        self.assertEqual(backoff_events[0]["detail"]["round"], 1)
        self.assertEqual(backoff_events[0]["detail"]["sleep_sec"], 15.0)

    def test_auth_fatal_and_quota_fail_fast_without_backoff(self):
        self.h.close()
        for err_msg in ("HTTP 401 Unauthorized", "Quota exceeded for project"):
            h = ChainHarness(self)
            try:
                h.new_failures = [RuntimeError(err_msg)]
                sleeps = []
                args = _run_args(
                    allocation_rounds=3,
                    sleep_fn=lambda s: sleeps.append(s),
                    jitter_fn=lambda: 1.0,
                )
                with self.assertRaisesRegex(RuntimeError, "GPU allocation failed"):
                    hc.run_variant(args)
                # Only 1 attempt: no A100 fallback, no round 2 backoff
                self.assertEqual(len(h.allocate_ids), 1)
                self.assertNotIn(15.0, sleeps)
            finally:
                h.close()

    def test_deadline_prevents_sleeping_into_next_round(self):
        h = self.h
        h.new_failures = [
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
        ]
        # Deadline only 100s from now (< MIN_ATTEMPT_WALL_SEC + DEADLINE_MARGIN_SEC = 2700s)
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=100)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        sleeps = []
        args = _run_args(
            allocation_rounds=3,
            task_deadline_utc=soon,
            sleep_fn=lambda s: sleeps.append(s),
            jitter_fn=lambda: 1.0,
        )
        with self.assertRaisesRegex(RuntimeError, "task deadline"):
            hc.run_variant(args)
        # Aborted immediately without sleeping 15.0s
        self.assertNotIn(15.0, sleeps)
        # v0.3.2 R6: round 1 spends 2 attempts (G4 503 -> fast-forward
        # -> A100 503), not 3 — the second G4 attempt is skipped.
        self.assertEqual(len(h.allocate_ids), 2)

    def test_stale_historical_ledger_deadline_ignored_for_backoff(self):
        h = self.h
        # Ledger carries a historical deadline already in the past; args.task_deadline_utc is unset.
        # v0.3.2 R6: the successful allocation lands on A100 after the
        # round-2 fast-forward, so the fake probe reports A100 hardware.
        h.probe_output = "NVIDIA A100 80GB PCIe, 81920"
        h.new_failures = [
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
        ]
        sleeps = []
        stale_ledger = {
            "max_observed_spent_cu": 0.0,
            "production_task_id": TASK,
            "project_budget_cu": 30.0,
            "hard_stop_balance_cu": 132.77,
            "deadline_utc": "2020-01-01T00:00:00Z",
        }
        with mock.patch.object(hc, "initialize_project_budget", return_value=stale_ledger):
            args = _run_args(
                allocation_rounds=3,
                sleep_fn=lambda s: sleeps.append(s),
                jitter_fn=lambda: 1.0,
            )
            res = hc.run_variant(args)
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        self.assertIn(15.0, sleeps)
        self.assertIn("stale_ledger_deadline_ignored", h.audit_jsonl_text())

    def test_failed_allocation_attempts_mark_leases_stopped_immediately(self):
        import h3_gpu_lease_registry as reg_mod

        h = self.h
        registry = reg_mod.LeaseRegistry(h.root / "model_cache" / "gpu_leases.json")
        registered_sids = []
        stopped_order = []
        register_marks = []

        def real_register(session, gpu, run_id, task_id, deadline, budget_cu, max_wall_sec=14400):
            registered_sids.append(session)
            # How many leases were already confirmed stopped at the
            # moment this session was registered.
            register_marks.append((session, len(stopped_order)))
            return registry.register_lease(
                session_id=session,
                run_id=run_id,
                production_task_id=task_id,
                gpu_model=gpu,
                stop_deadline_utc=deadline,
                budget_cap_cu=budget_cu,
                owner_id="owner-m1-test",
                max_wall_sec=max_wall_sec,
            )

        def real_confirm_stopped(session):
            stopped_order.append(session)
            registry.confirm_stopped(session, "STOPPED")

        # v0.3.2 R6: Round 1 (G4 503 -> fast-forward -> A100 503) fails,
        # then Round 2 G4 (503) fast-forwards to A100 which succeeds.
        # The successful allocation lands on A100, so the fake probe
        # reports A100 hardware.
        h.probe_output = "NVIDIA A100 80GB PCIe, 81920"
        h.new_failures = [
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
        ]
        with mock.patch.object(hc, "_lease_registry", return_value=registry), \
                mock.patch.object(hc, "_register_gpu_lease", side_effect=real_register), \
                mock.patch.object(hc, "_confirm_gpu_lease_stopped", side_effect=real_confirm_stopped):
            args = _run_args(
                allocation_rounds=3,
                sleep_fn=lambda s: None,
                jitter_fn=lambda: 1.0,
            )
            res = hc.run_variant(args)
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        self.assertEqual(len(registered_sids), 4)
        # The three failed Round-1 sessions were marked stopped at once:
        # when the successful Round-2 session was registered, all three
        # failed leases were already confirmed stopped.
        self.assertEqual(register_marks[3][1], 3)
        self.assertEqual(stopped_order[:3], registered_sids[:3])
        for sid in registered_sids[:3]:
            lease = registry.get_lease(sid)
            self.assertIsNotNone(lease, sid)
            self.assertEqual(lease["last_confirmed_state"], "STOPPED", sid)
        # The successful session is stopped by the run's own finally at
        # the end, so it ends STOPPED too — the point is the failed ones
        # never lingered as phantom `not_yet_due` leases in between.
        self.assertEqual(stopped_order[3], registered_sids[3])


if __name__ == "__main__":
    unittest.main()

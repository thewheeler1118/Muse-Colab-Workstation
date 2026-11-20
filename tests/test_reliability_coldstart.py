"""v0.2.0 F5 tests: cold-start P50/P90 + wheelhouse_hit_rate aggregation.

Offline only (0 CU): fixtures are built through the real
h3_run_manifest module on temp roots — no GPU, no CU, no Drive.
"""

import tempfile
import unittest
from pathlib import Path

import h3_reliability as rel
import h3_run_manifest as rm

TASK = "h3-f5-task"


def _completed_run(root, run_id, report=None):
    rm.create_run_manifest(root, TASK, run_id, budget={})
    rm.update_run(root, run_id, session_id="muse-h3-aaaa0001",
                  session_state="ACTIVE", task_state="GPU_ALLOCATED")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": "a" * 64, "local_frames": 274})
    rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                  session_state="STOPPED")
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED")
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")
    if report is not None:
        rm.update_run(root, run_id, coldstart_metrics=report)


def _report(setup_seconds, wheelhouse_hit):
    return {"setup_seconds": setup_seconds, "comfy_install_seconds": 1.0,
            "restore_seconds": 2.0, "wheelhouse_hit": wheelhouse_hit,
            "models_reused": 0, "models_downloaded": 4, "vram_mib": 81920}


class ColdstartAggregationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_p50_p90_and_hit_rate(self):
        # setup_seconds 10..50 -> P50 30, P90 46 (linear interpolation);
        # hits True, True, False, None, True -> 3/4 (None excluded).
        setups = [10.0, 20.0, 30.0, 40.0, 50.0]
        hits = [True, True, False, None, True]
        for i, (s, h) in enumerate(zip(setups, hits)):
            _completed_run(self.root, f"h3-f5-run-{i}", _report(s, h))
        report = rel.compute_reliability(self.root)
        cold = report["coldstart"]
        self.assertEqual(cold["status"], "MEASURED")
        self.assertEqual(cold["runs_with_metrics"], 5)
        self.assertEqual(cold["setup_seconds_p50"], 30.0)
        self.assertEqual(cold["setup_seconds_p90"], 46.0)
        rate = cold["wheelhouse_hit_rate"]
        # 4 reporting runs cannot verify a rate (MIN_SAMPLE_FOR_MEASURED),
        # but the rate itself is computed: 3/4.
        self.assertEqual(rate["status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(rate["rate"], 0.75)
        self.assertEqual(rate["numerator"], 3)
        self.assertEqual(rate["denominator"], 4)

    def test_no_metrics_is_unknown(self):
        _completed_run(self.root, "h3-f5-run-0")
        _completed_run(self.root, "h3-f5-run-1")
        report = rel.compute_reliability(self.root)
        cold = report["coldstart"]
        self.assertEqual(cold["status"], "UNKNOWN")
        self.assertEqual(cold["runs_with_metrics"], 0)
        self.assertIsNone(cold["setup_seconds_p50"])
        self.assertIsNone(cold["setup_seconds_p90"])
        self.assertEqual(cold["wheelhouse_hit_rate"]["status"], "UNKNOWN")

    def test_single_report_percentiles_equal_the_value(self):
        _completed_run(self.root, "h3-f5-run-0", _report(17.5, True))
        report = rel.compute_reliability(self.root)
        cold = report["coldstart"]
        self.assertEqual(cold["setup_seconds_p50"], 17.5)
        self.assertEqual(cold["setup_seconds_p90"], 17.5)


class PercentileHelperTest(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(rel._percentile([], 0.5))

    def test_single_value(self):
        self.assertEqual(rel._percentile([7.0], 0.9), 7.0)

    def test_even_count_interpolates(self):
        # [10, 20, 30, 40]: P50 sits between 20 and 30.
        self.assertEqual(rel._percentile([10.0, 20.0, 30.0, 40.0], 0.5), 25.0)


if __name__ == "__main__":
    unittest.main()

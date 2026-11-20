"""v0.2.1 P3 tests: monotonic MODELS_READY evidence for metric F.

Offline only (0 CU): runs are built through the real run-manifest
machinery on temp roots; no GPU, no Drive, no network.
"""

import json
import tempfile
import unittest
from pathlib import Path

import h3_reliability as rel
import h3_run_manifest as rm

REPORT = {"setup_seconds": 42.5, "comfy_install_seconds": 12.5,
          "restore_seconds": 30.0, "wheelhouse_hit": True,
          "models_reused": 3, "models_downloaded": 1, "vram_mib": 81920}


def _completed_run(root: Path, run_id: str, with_models_ready: bool = True):
    """A TASK_COMPLETED run built through the real manifest stages."""
    rm.create_run_manifest(root, "h3-p3-task", run_id)
    rm.update_run(root, run_id, task_state="GPU_ALLOCATED",
                  session_id="muse-h3-aaaa0001", session_state="ACTIVE")
    rm.update_run(root, run_id, task_state="WORKER_STARTED")
    if with_models_ready:
        rm.update_run(root, run_id, task_state="MODELS_READY")
    rm.update_run(root, run_id, task_state="INFERENCE_COMPLETED")
    rm.update_run(root, run_id, task_state="VIDEO_VALIDATED",
                  artifacts={"local_sha256": "a" * 64, "local_frames": 274})
    rm.update_run(root, run_id, task_state="GPU_STOP_CONFIRMED",
                  session_state="STOPPED")
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  artifacts={"delivery_state": "DELIVERED",
                             "drive_file_id": "drive-file-1"})
    rm.update_run(root, run_id, task_state="TASK_COMPLETED")
    return rm.load_run_manifest(root, run_id)


class HasModelsReadyEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _stages(self, run_id):
        return rel._stage_times(self.root, run_id)

    def test_explicit_models_ready(self):
        _completed_run(self.root, "r1", with_models_ready=True)
        self.assertTrue(rel._has_models_ready_evidence(
            self.root, "r1", self._stages("r1")))

    def test_monotonic_chain_without_explicit_stage(self):
        # A fast worker crossed MODELS_READY between 60s polls: the
        # audit log shows WORKER_STARTED -> INFERENCE_COMPLETED ->
        # VIDEO_VALIDATED in order, with no MODELS_READY stage.
        _completed_run(self.root, "r1", with_models_ready=False)
        self.assertTrue(rel._has_models_ready_evidence(
            self.root, "r1", self._stages("r1")))

    def test_audit_coldstart_metrics_event(self):
        # The run's only models evidence is the COLDSTART_METRICS audit
        # event: no MODELS_READY stage, no complete chain.
        rm.create_run_manifest(self.root, "h3-p3-task", "r2")
        rm.update_run(self.root, "r2", task_state="GPU_ALLOCATED",
                      session_id="muse-h3-aaaa0002", session_state="ACTIVE")
        rm.update_run(self.root, "r2", task_state="WORKER_STARTED")
        rm.record_audit_event(self.root, "r2", "COLDSTART_METRICS",
                              {"setup_seconds": 42.5})
        stages = rel._stage_times(self.root, "r2")
        self.assertNotIn("MODELS_READY", stages)
        self.assertTrue(rel._has_models_ready_evidence(self.root, "r2", stages))

    def test_incomplete_chain_fails(self):
        rm.create_run_manifest(self.root, "h3-p3-task", "r1")
        rm.update_run(self.root, "r1", task_state="GPU_ALLOCATED",
                      session_id="muse-h3-aaaa0001", session_state="ACTIVE")
        rm.update_run(self.root, "r1", task_state="WORKER_STARTED")
        rm.update_run(self.root, "r1", task_state="INFERENCE_COMPLETED")
        # No VIDEO_VALIDATED: the chain is incomplete, so the run must
        # still fail condition (2).
        self.assertFalse(rel._has_models_ready_evidence(
            self.root, "r1", self._stages("r1")))

    def test_out_of_order_chain_fails(self):
        # Stages recorded out of order cannot prove a monotonic crossing.
        stages = {"WORKER_STARTED": ["2026-11-20T04:00:03Z"],
                  "INFERENCE_COMPLETED": ["2026-11-20T04:00:02Z"],
                  "VIDEO_VALIDATED": ["2026-11-20T04:00:01Z"]}
        self.assertFalse(rel._has_models_ready_evidence(
            self.root, "r1", stages))

    def test_no_evidence_at_all_fails(self):
        self.assertFalse(rel._has_models_ready_evidence(self.root, "r1", {}))


class MetricFBackfilledRunTest(unittest.TestCase):
    def test_backfilled_run_counts_as_success(self):
        # A run whose MODELS_READY was only ever backfilled (no explicit
        # stage in the audit log) still satisfies metric F condition (2)
        # via the monotonic chain.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            manifest = _completed_run(root, "r1", with_models_ready=False)
            failed = rel._evaluate_run_f(root, manifest)
            self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()

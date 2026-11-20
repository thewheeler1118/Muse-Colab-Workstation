"""TASK-HOTFIX-016 (Issue #19): v0.3.1 P0-A worker stage contract.

v0.3.0 defect A (TASK-LIVE-E2E-015, run h3-20261120T071209Z-4ea108):
the F3 worker calls set_stage('setup_parallel'), but
WORKER_PHASE_TO_JOB_STAGE has no entry for it, so the raw phase is
written into the job state and the controller's wait_for_worker raises
"Unknown worker job stage: 'setup_parallel'" — deterministically, on
every run that reaches worker setup. Upstream Antigravity normalises
its equivalent phase ("dual_track_setup") to RESTORING_MODELS via
_job_stage_for(); the Muse F3 port dropped that normaliser.

The same mechanism had two narrower holes, fixed in the same commit:
emit_coldstart_metrics wrote the raw _LAST_STAGE (comfyui_start /
object_info_validation, both unmapped) into the job state, and the
worker wrote REMOTE_UPLOAD_FAILED / REMOTE_UPLOAD_VERIFIED directly,
although the mapping already declares all three upload phases as
INFERENCE_COMPLETED on the job-state axis.

Every test in this file fails on v0.3.0 and passes after the fix,
except the explicitly pinned keep-behaviour tests (unknown stages
still raise; MODELS_READY only under the pre-existing P3 conditions).
No GPU, no live CLI, no real Drive: every external boundary is a mock.
"""

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
import h3_job_state as hjs  # noqa: E402
import h3_remote_worker as hrw  # noqa: E402
import h3_run_manifest as rm  # noqa: E402

RUN_ID = "h3-20261120T000000Z-hotfix01"
TASK = "h3-hotfix-016"
SESSION = "muse-h3-hotfix01"
DONE = {"file": "/content/muse_h3/output/shao_nian_h3_A.mp4", "frames": 274,
        "seconds": 274 / 24, "sha256": "a" * 64, "with_audio": True}
REPORT = {"setup_seconds": 42.5, "comfy_install_seconds": 12.5,
          "restore_seconds": 30.0, "wheelhouse_hit": True,
          "models_reused": 3, "models_downloaded": 1, "vram_mib": 81920}

# Every phase the worker can pass to set_stage() in main(), audited from
# h3_remote_worker.py (plus the two SetupTrackFailed stage values, which
# are the track names 'ensure_comfy' / 'restore_weights').
AUDIT_PHASES = (
    "setup_parallel",
    "ensure_comfy",
    "restore_weights",
    "comfyui_start",
    "object_info_validation",
    "inference_submit",
    "find_rendered_video",
    "rescue_rendered_video",
    "assemble_mp4",
    "remote_upload",
    "done",
)


def _state(run_id=RUN_ID, variant="A", stage="VIDEO_READY", output=None,
           error=None, **extra):
    s = {"run_id": run_id, "variant": variant, "stage": stage,
         "updated_at_utc": "2026-11-20T00:00:00Z",
         "heartbeat_utc": "2026-11-20T00:00:00Z"}
    if output is not None:
        s["output"] = output
    if error is not None:
        s["error"] = error
    s.update(extra)
    return s


class WorkerStageMappingTest(unittest.TestCase):
    """The worker must never write a non-job-stage into the job state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._saved = (hrw.ROOT, hrw._JOB_RUN_ID, hrw._JOB_VARIANT,
                       hrw._LAST_STAGE, hrw._LAST_REFRESH_REQUEST_TOKEN_ID)
        hrw.ROOT = self.root
        hrw._JOB_RUN_ID = RUN_ID
        hrw._JOB_VARIANT = "A"
        hrw._LAST_STAGE = ""
        hrw._LAST_REFRESH_REQUEST_TOKEN_ID = None

    def tearDown(self):
        (hrw.ROOT, hrw._JOB_RUN_ID, hrw._JOB_VARIANT, hrw._LAST_STAGE,
         hrw._LAST_REFRESH_REQUEST_TOKEN_ID) = self._saved

    def _job_stage(self):
        return hjs.read_job_state(self.root)["stage"]

    def test_setup_parallel_maps_to_restoring_models(self):
        # TASK-HOTFIX-016 A1: the F3 phase is setup work, not a finished
        # model restore — it must never be claimed as MODELS_READY.
        self.assertEqual(
            hjs.WORKER_PHASE_TO_JOB_STAGE["setup_parallel"],
            "RESTORING_MODELS")

    def test_every_declared_phase_maps_to_a_job_stage(self):
        for phase, stage in hjs.WORKER_PHASE_TO_JOB_STAGE.items():
            self.assertIn(stage, hjs.JOB_STAGES,
                          f"phase {phase!r} maps to {stage!r}, "
                          f"which is not a job stage")

    def test_every_phase_reachable_from_main_maps(self):
        for phase in AUDIT_PHASES:
            self.assertIn(phase, hjs.WORKER_PHASE_TO_JOB_STAGE,
                          f"phase {phase!r} has no mapping")
            self.assertIn(hjs.WORKER_PHASE_TO_JOB_STAGE[phase],
                          hjs.JOB_STAGES)

    def test_set_stage_writes_mapped_stage_for_every_phase(self):
        for phase in AUDIT_PHASES:
            hrw.set_stage(phase)
            self.assertEqual(
                self._job_stage(), hjs.WORKER_PHASE_TO_JOB_STAGE[phase],
                f"set_stage({phase!r}) wrote an unmapped stage")

    def test_token_refresh_rewrite_does_not_reemit_setup_parallel(self):
        # A3.8: a token refresh during setup_parallel must rewrite the
        # job state with the mapped stage, not the raw phase.
        hrw.set_stage("setup_parallel")
        provider = SimpleNamespace(token_id="tok-refresh-1")
        hrw._request_token_refresh(provider)
        state = hjs.read_job_state(self.root)
        self.assertEqual(state["stage"], "RESTORING_MODELS")
        self.assertIn("token_refresh_requested", state)

    def test_emit_coldstart_metrics_writes_mapped_stage_cold_path(self):
        hrw.set_stage("comfyui_start")
        hrw.emit_coldstart_metrics(time.monotonic() - 5, RUN_ID, "A",
                                   vram_mib=81920)
        state = hjs.read_job_state(self.root)
        self.assertEqual(state["stage"], "RESTORING_MODELS")
        self.assertIn("coldstart_metrics", state)

    def test_emit_coldstart_metrics_writes_mapped_stage_warm_path(self):
        hrw.set_stage("object_info_validation")
        hrw.emit_coldstart_metrics(time.monotonic() - 5, RUN_ID, "A",
                                   vram_mib=81920, warm_session_reused=True)
        state = hjs.read_job_state(self.root)
        self.assertEqual(state["stage"], "RESTORING_MODELS")
        self.assertTrue(state["coldstart_metrics"]["warm_session_reused"])

    def test_upload_phases_declared_as_inference_completed(self):
        # The mapping already declares all three upload phases as
        # INFERENCE_COMPLETED; the worker's direct writes must agree.
        for phase in ("remote_upload", "remote_upload_verified",
                      "remote_upload_failed"):
            self.assertEqual(
                hjs.WORKER_PHASE_TO_JOB_STAGE[phase],
                "INFERENCE_COMPLETED")


class ControllerPollContractTest(unittest.TestCase):
    """The controller accepts every stage the worker can legitimately
    write, advances the manifest only under the pre-existing P3
    conditions, and still refuses truly unknown stages."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        rm.create_run_manifest(self.root, TASK, RUN_ID)
        self._saved = (hrw.ROOT, hrw._JOB_RUN_ID, hrw._JOB_VARIANT,
                       hrw._LAST_STAGE, hrw._LAST_REFRESH_REQUEST_TOKEN_ID)
        hrw.ROOT = self.root
        hrw._JOB_RUN_ID = RUN_ID
        hrw._JOB_VARIANT = "A"
        hrw._LAST_STAGE = ""
        hrw._LAST_REFRESH_REQUEST_TOKEN_ID = None

    def tearDown(self):
        (hrw.ROOT, hrw._JOB_RUN_ID, hrw._JOB_VARIANT, hrw._LAST_STAGE,
         hrw._LAST_REFRESH_REQUEST_TOKEN_ID) = self._saved

    def _poll_manifest(self, state):
        with mock.patch.object(hc, "ROOT", self.root), \
                mock.patch.object(hc, "record", return_value=None):
            hc._manifest_worker_state(RUN_ID, state)

    def _stage(self):
        return rm.load_run_manifest(self.root, RUN_ID)["task_state"]

    def test_setup_parallel_chain_continues_polling(self):
        # Defect A end to end: the state the worker actually writes
        # when it enters setup_parallel must poll cleanly.
        hrw.set_stage("setup_parallel")
        written = hjs.read_job_state(self.root)
        self.assertEqual(written["stage"], "RESTORING_MODELS")
        states = [written, _state(stage="VIDEO_READY", output=DONE)]
        with mock.patch.object(hc, "poll_worker", side_effect=states):
            final = hc.wait_for_worker(SESSION, RUN_ID, "A",
                                       deadline_monotonic=1e9,
                                       poll_interval_sec=0)
        self.assertEqual(final["stage"], "VIDEO_READY")

    def test_setup_mapping_does_not_advance_manifest(self):
        # A3.4/5: the mapping must not push the manifest to MODELS_READY.
        self._poll_manifest({"stage": "RESTORING_MODELS"})
        self.assertEqual(self._stage(), "TASK_AUTHORIZED")

    def test_coldstart_backfill_preserved(self):
        # P3 behaviour unchanged: coldstart evidence still backfills.
        self._poll_manifest({"stage": "RESTORING_MODELS",
                             "coldstart_metrics": dict(REPORT)})
        self.assertEqual(self._stage(), "MODELS_READY")

    def test_later_stage_backfill_preserved(self):
        self._poll_manifest({"stage": "INFERENCE_RUNNING"})
        self.assertEqual(self._stage(), "MODELS_READY")

    def test_video_ready_and_failed_semantics_unchanged(self):
        with mock.patch.object(hc, "poll_worker",
                               return_value=_state(stage="VIDEO_READY",
                                                   output=DONE)):
            final = hc.wait_for_worker(SESSION, RUN_ID, "A",
                                       deadline_monotonic=1e9,
                                       poll_interval_sec=0)
        self.assertEqual(final["stage"], "VIDEO_READY")
        err = {"stage": "restore_weights", "error_type": "RuntimeError",
               "error": "boom"}
        with mock.patch.object(hc, "poll_worker",
                               return_value=_state(stage="FAILED",
                                                   error=err)):
            with self.assertRaisesRegex(RuntimeError, "Remote worker failed"):
                hc.wait_for_worker(SESSION, RUN_ID, "A",
                                   deadline_monotonic=1e9,
                                   poll_interval_sec=0)

    def test_truly_unknown_stage_still_raises(self):
        weird = _state(stage="SOME_NEW_STAGE")
        with mock.patch.object(hc, "poll_worker", return_value=weird):
            with self.assertRaisesRegex(RuntimeError,
                                        "Unknown worker job stage"):
                hc.wait_for_worker(SESSION, RUN_ID, "A",
                                   deadline_monotonic=1e9,
                                   poll_interval_sec=0)

    def test_upload_evidence_states_accepted_and_recorded(self):
        # The post-fix upload states (INFERENCE_COMPLETED + evidence)
        # must poll cleanly and record the Drive evidence.
        failed = {"error_type": "RuntimeError", "error": "upload boom"}
        states = [_state(stage="INFERENCE_COMPLETED",
                         remote_upload_failed=failed),
                  _state(stage="VIDEO_READY", output=DONE)]
        with mock.patch.object(hc, "poll_worker", side_effect=states):
            final = hc.wait_for_worker(SESSION, RUN_ID, "A",
                                       deadline_monotonic=1e9,
                                       poll_interval_sec=0)
        self.assertEqual(final["stage"], "VIDEO_READY")
        evidence = {"drive_file_id": "drive-file-1",
                    "drive_output_folder_id": "drive-folder-1",
                    "size": 1234, "sha256": "b" * 64,
                    "upload_started_at_utc": "2026-11-20T00:00:00Z",
                    "upload_finished_at_utc": "2026-11-20T00:00:10Z"}
        self._poll_manifest({"stage": "INFERENCE_COMPLETED",
                             "remote_upload": evidence})
        artifacts = rm.load_run_manifest(self.root, RUN_ID)["artifacts"]
        self.assertEqual(artifacts["delivery_state"],
                         "REMOTE_UPLOAD_VERIFIED")
        self.assertEqual(artifacts["drive_file_id"], "drive-file-1")


if __name__ == "__main__":
    unittest.main()

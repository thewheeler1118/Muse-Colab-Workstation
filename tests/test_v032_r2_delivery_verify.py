"""TASK-RELEASE-V032 R2: tamper-proof delivery re-verification + 0-CU
local self-healing (restore from the Drive archive).

Strictly 0.00 CU: the Drive service, the download bytes and
validate_final_video are all fakes/mocks. No network, no GPU, no
real Drive file ids (every id below is a synthetic fake).

Fail-first tests for the R2 gaps in h3_controller._verify_completed_delivery:
  * a remote size/md5 mismatch (tampered bytes) must be "lost", even
    when the local MP4 is still present;
  * a missing local MP4 must NEVER be reported as verified with
    local_video_validated=True in read-only mode (restore_local=False);
  * with restore_local=True the remote file must be downloaded to a
    temp file, hash-checked against the trusted manifest identity and
    fully validated (validate_final_video) before it atomically lands;
  * a restored file whose frames/ffprobe do not match must be "lost"
    and must not land at the destination;
  * the trusted output identity (output_size / output_md5 /
    output_sha256) must be persisted to run_manifest.json["artifacts"]
    when a video is first locally accepted and archived to Drive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h3_controller as hc
import h3_run_manifest as rm

TASK = "h3-t032-r2-deliver"
RUN_ID = "h3-v032-r2-01"
FILE_ID = "fake-drive-file-r2-01"
FOLDER_ID = "fake-drive-folder-r2-01"

# Deterministic fake MP4 bytes; digests are computed from them, never
# hard-coded, so a tamper test changes exactly one byte.
GOOD_BYTES = b"FAKE-MP4-R2-" + b"0123456789abcdef" * 8
GOOD_MD5 = hashlib.md5(GOOD_BYTES, usedforsecurity=False).hexdigest()
GOOD_SHA256 = hashlib.sha256(GOOD_BYTES).hexdigest()
GOOD_SIZE = len(GOOD_BYTES)
BAD_BYTES = b"X" + GOOD_BYTES[1:]
BAD_MD5 = hashlib.md5(BAD_BYTES, usedforsecurity=False).hexdigest()

FAKE_QA_INFO = {
    "sha256": GOOD_SHA256, "size": GOOD_SIZE, "frame_count": 274,
    "width": 1080, "height": 1920, "fps": 24.0,
}


def _meta(*, size=GOOD_SIZE, md5=GOOD_MD5, sha256=GOOD_SHA256,
          trashed=False, file_id=FILE_ID):
    return {"id": file_id, "name": "A.mp4", "size": str(size),
            "md5Checksum": md5, "sha256Checksum": sha256,
            "trashed": trashed, "parents": [FOLDER_ID]}


def _fake_drive(meta=None, content=GOOD_BYTES, meta_error=None):
    """Fake Drive service: files().get() -> metadata, files().get_media()
    -> bytes. Both go through the same files() mock, as in production."""
    svc = mock.MagicMock()
    files = svc.files.return_value
    if meta_error is not None:
        files.get.return_value.execute.side_effect = meta_error
    else:
        files.get.return_value.execute.return_value = meta
    files.get_media.return_value.execute.return_value = content
    return svc


def _completed_manifest(root: Path, run_id: str, artifacts: dict) -> dict:
    rm.create_run_manifest(root, TASK, run_id, variant="A", budget={})
    rm.update_run(root, run_id, artifacts=dict(artifacts))
    for stage in ("WORKER_STARTED", "MODELS_READY", "VIDEO_VALIDATED",
                  "GPU_STOP_CONFIRMED"):
        kw = {"session_state": "STOPPED"} if stage == "GPU_STOP_CONFIRMED" else {}
        rm.update_run(root, run_id, task_state=stage,
                      session_id="muse-h3-stopped01", **kw)
    rm.update_run(root, run_id, task_state="DRIVE_VERIFIED",
                  session_state="STOPPED")
    rm.update_run(root, run_id, task_state="TASK_COMPLETED",
                  session_state="STOPPED")
    return rm.load_run_manifest(root, run_id)


def _trusted_artifacts(**over):
    base = {
        "drive_file_id": FILE_ID,
        "drive_output_folder_id": FOLDER_ID,
        "delivery_state": "DELIVERED",
        "output_size": GOOD_SIZE,
        "output_md5": GOOD_MD5,
        "output_sha256": GOOD_SHA256,
        "local_sha256": GOOD_SHA256,
        "local_frames": 274,
    }
    base.update(over)
    return base


class VerifyCompletedDeliveryR2Test(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        for sub in ("runs", "output", "logs", "model_cache"):
            (self.root / sub).mkdir(parents=True)
        self.out_root = self.root / "output"
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.out_root),
            mock.patch.object(hc, "RUNS", self.root / "runs"),
            mock.patch.object(hc.time, "sleep", lambda s: None),
            mock.patch("h3_video_qa.validate_final_video",
                       return_value=dict(FAKE_QA_INFO)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.td.cleanup)

    def _drive(self, meta=None, content=GOOD_BYTES, meta_error=None):
        svc = _fake_drive(meta=meta, content=content, meta_error=meta_error)
        mock.patch("drive_depot.drive_service", return_value=svc).start()
        return svc

    def _manifest(self, artifacts):
        return _completed_manifest(self.root, RUN_ID, artifacts)

    def _write_local(self, data=GOOD_BYTES):
        path = self.out_root / RUN_ID / "A.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    # -- happy path ----------------------------------------------------
    def test_verified_when_local_present_and_remote_matches_trusted(self):
        svc = self._drive(meta=_meta())
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, evidence = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "verified")
        self.assertTrue(evidence["local_video_validated"])
        self.assertTrue(evidence["drive_upload_verified"])
        self.assertEqual(evidence["drive_file_id"], FILE_ID)
        self.assertEqual(evidence["frames"], 274)
        self.assertEqual(evidence["sha256"], GOOD_SHA256)
        svc.files.return_value.get_media.assert_not_called()

    def test_remote_md5_uppercase_still_matches_after_normalisation(self):
        self._drive(meta=_meta(md5=GOOD_MD5.upper()))
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "verified")

    def test_legacy_manifest_without_output_fields_verifies_via_remote_upload(self):
        self._drive(meta=_meta())
        self._write_local()
        manifest = self._manifest({
            "drive_file_id": FILE_ID,
            "drive_output_folder_id": FOLDER_ID,
            "delivery_state": "DELIVERED",
            "remote_upload": {"size": GOOD_SIZE, "md5": GOOD_MD5,
                              "sha256": GOOD_SHA256},
            "local_sha256": GOOD_SHA256,
            "local_frames": 274,
        })
        status, evidence = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "verified")
        self.assertTrue(evidence["local_video_validated"])

    # -- remote tamper -> "lost" ---------------------------------------
    def test_remote_size_mismatch_is_lost_and_never_downloads(self):
        svc = self._drive(meta=_meta(size=GOOD_SIZE + 1))
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, reason = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")
        self.assertIn("size", str(reason).lower())
        svc.files.return_value.get_media.assert_not_called()

    def test_remote_md5_mismatch_is_lost(self):
        self._drive(meta=_meta(md5=BAD_MD5))
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, reason = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")
        self.assertIn("md5", str(reason).lower())

    def test_remote_md5_missing_when_trusted_md5_recorded_is_lost(self):
        meta = _meta()
        del meta["md5Checksum"]
        self._drive(meta=meta)
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")

    def test_trashed_remote_is_lost(self):
        self._drive(meta=_meta(trashed=True))
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")

    def test_malformed_recorded_output_md5_is_lost(self):
        self._drive(meta=_meta())
        self._write_local()
        manifest = self._manifest(_trusted_artifacts(output_md5="not-a-hex-md5"))
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")

    def test_local_tamper_is_lost_even_when_remote_matches_trusted(self):
        # Local bytes differ from both the remote file and the trusted
        # manifest identity: the local copy must not be trusted.
        self._drive(meta=_meta())
        self._write_local(BAD_BYTES)
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")

    # -- Drive unreachable -> "unverifiable", never "lost" -------------
    def test_transient_drive_lookup_is_unverifiable(self):
        self._drive(meta_error=ConnectionError("connection reset by peer"))
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "unverifiable")

    def test_drive_404_is_lost(self):
        self._drive(meta_error=RuntimeError("404: file notFound"))
        self._write_local()
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "lost")

    # -- local missing: read-only probe must stay honest ---------------
    def test_local_missing_readonly_never_claims_verified(self):
        svc = self._drive(meta=_meta())
        manifest = self._manifest(_trusted_artifacts())
        status, reason = hc._verify_completed_delivery(manifest)
        self.assertEqual(status, "unverifiable")
        self.assertNotEqual(status, "verified")
        self.assertIn("local", str(reason).lower())
        svc.files.return_value.get_media.assert_not_called()
        self.assertFalse((self.out_root / RUN_ID / "A.mp4").exists())

    def test_local_missing_readonly_explicit_restore_false_is_honest(self):
        svc = self._drive(meta=_meta())
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(manifest, restore_local=False)
        self.assertEqual(status, "unverifiable")
        svc.files.return_value.get_media.assert_not_called()

    # -- local missing: restore_local=True self-heals at 0 CU ----------
    def test_local_missing_restore_downloads_validates_and_lands(self):
        svc = self._drive(meta=_meta(), content=GOOD_BYTES)
        manifest = self._manifest(_trusted_artifacts())
        status, evidence = hc._verify_completed_delivery(
            manifest, restore_local=True, drive=svc)
        self.assertEqual(status, "verified")
        self.assertTrue(evidence["local_video_validated"])
        self.assertTrue(evidence.get("restored_from_drive"))
        dest = self.out_root / RUN_ID / "A.mp4"
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), GOOD_BYTES)
        # No temp file is left behind in the run output dir.
        leftovers = [p for p in (self.out_root / RUN_ID).iterdir()
                     if p.name.startswith(".recover_")]
        self.assertEqual(leftovers, [])
        # The trusted identity is (re-)persisted in the manifest.
        after = rm.load_run_manifest(self.root, RUN_ID)
        self.assertEqual(after["artifacts"]["output_size"], GOOD_SIZE)
        self.assertEqual(after["artifacts"]["output_md5"], GOOD_MD5)
        self.assertEqual(after["artifacts"]["output_sha256"], GOOD_SHA256)
        self.assertEqual(after["artifacts"]["local_sha256"], GOOD_SHA256)

    def test_restore_rejects_downloaded_bytes_whose_hash_differs(self):
        # Metadata still claims the trusted md5, but the downloaded
        # bytes are different (transport corruption / tampered object):
        # the file must be "lost" and must never land locally.
        svc = self._drive(meta=_meta(md5=GOOD_MD5), content=BAD_BYTES)
        manifest = self._manifest(_trusted_artifacts())
        status, reason = hc._verify_completed_delivery(
            manifest, restore_local=True, drive=svc)
        self.assertEqual(status, "lost")
        self.assertIn("md5", str(reason).lower())
        dest = self.out_root / RUN_ID / "A.mp4"
        self.assertFalse(dest.exists())
        self.assertEqual(list((self.out_root / RUN_ID).glob(".recover_*")), [])

    def test_restore_rejects_bad_frames_after_download(self):
        svc = self._drive(meta=_meta(), content=GOOD_BYTES)
        manifest = self._manifest(_trusted_artifacts())
        with mock.patch(
                "h3_video_qa.validate_final_video",
                side_effect=RuntimeError(
                    "Final video technical QA failed: 1080x1920 24.0fps "
                    "100 frames, expected 1080x1920 24fps 274 frames")):
            status, _ = hc._verify_completed_delivery(
                manifest, restore_local=True, drive=svc)
        self.assertEqual(status, "lost")
        self.assertFalse((self.out_root / RUN_ID / "A.mp4").exists())

    def test_restore_transient_download_is_unverifiable_not_lost(self):
        svc = _fake_drive(meta=_meta())
        svc.files.return_value.get_media.return_value.execute.side_effect = (
            TimeoutError("read timed out"))
        mock.patch("drive_depot.drive_service", return_value=svc).start()
        manifest = self._manifest(_trusted_artifacts())
        status, _ = hc._verify_completed_delivery(
            manifest, restore_local=True, drive=svc)
        self.assertEqual(status, "unverifiable")
        self.assertFalse((self.out_root / RUN_ID / "A.mp4").exists())


class TrustedOutputPersistenceTest(unittest.TestCase):
    """deliver_variant_to_drive persists the trusted output identity
    (output_size / output_md5 / output_sha256) at archive time, keeping
    the legacy local_sha256 field alongside it."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        for sub in ("runs", "output", "logs", "model_cache"):
            (self.root / sub).mkdir(parents=True)
        self.out_root = self.root / "output"
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.out_root),
            mock.patch.object(hc, "RUNS", self.root / "runs"),
            mock.patch.object(hc.time, "sleep", lambda s: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.td.cleanup)
        rm.create_run_manifest(self.root, TASK, RUN_ID, variant="A", budget={})
        local = self.out_root / RUN_ID / "A.mp4"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(GOOD_BYTES)

    def _archive_ok(self):
        def fn(variant, run_id):
            return {"output_folder_id": FOLDER_ID,
                    "files": {"A.mp4": {"drive_file_id": FILE_ID,
                                        "size": GOOD_SIZE,
                                        "server_md5": GOOD_MD5,
                                        "sha256": GOOD_SHA256,
                                        "parent": FOLDER_ID}}}
        return fn

    def test_archive_persists_trusted_output_identity(self):
        video_info = {"frame_count": 274, "width": 1080, "height": 1920,
                      "fps": 24.0, "sha256": GOOD_SHA256}
        evidence = hc.deliver_variant_to_drive(
            "A", RUN_ID, video_info, archive_fn=self._archive_ok())
        self.assertTrue(evidence["local_video_validated"])
        self.assertEqual(evidence["drive_file_id"], FILE_ID)
        self.assertEqual(evidence["output_size"], GOOD_SIZE)
        self.assertEqual(evidence["output_md5"], GOOD_MD5)
        self.assertEqual(evidence["output_sha256"], GOOD_SHA256)
        after = rm.load_run_manifest(self.root, RUN_ID)
        artifacts = after["artifacts"]
        self.assertEqual(artifacts["output_size"], GOOD_SIZE)
        self.assertEqual(artifacts["output_md5"], GOOD_MD5)
        self.assertEqual(artifacts["output_sha256"], GOOD_SHA256)
        # Backward compatibility: the legacy field is still written.
        self.assertEqual(artifacts["local_sha256"], GOOD_SHA256)
        self.assertEqual(artifacts["delivery_state"], "DURABLE_UPLOAD_VERIFIED")


class DeliverRestoreFlagTest(unittest.TestCase):
    """--deliver passes its restore flag through to the verifier, so a
    --deliver --restore-local run can self-heal a missing local MP4
    while a plain --deliver run stays read-only."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        for sub in ("runs", "output", "logs", "model_cache"):
            (self.root / sub).mkdir(parents=True)
        self.spec_path = self.root / "spec.json"
        spec = {"task_id": TASK, "output_prefix": "A",
                "ref_image_1": {"sha256": "ab" * 32,
                                "local_path": "assets/original_poster.jpg"},
                "prompt": "test prompt", "seed": 42,
                "width": 736, "height": 1312, "fps": 24,
                "animation_frames": 226, "tail_card_frames": 0, "steps": 25,
                "output_width": 1080, "output_height": 1920}
        self.spec_path.write_text(json.dumps(spec))
        self._patches = [
            mock.patch.object(hc, "ROOT", self.root),
            mock.patch.object(hc, "OUTPUT_ROOT", self.root / "output"),
            mock.patch.object(hc, "RUNS", self.root / "runs"),
            mock.patch.object(hc.time, "sleep", lambda s: None),
            mock.patch.object(hc, "inspect",
                              return_value={"cu_balance": 144.19, "rate": 0.0,
                                            "sessions": []}),
        ]
        for p in self._patches:
            p.start()
        self.verify = mock.patch.object(
            hc, "_verify_completed_delivery",
            return_value=("verified", {"drive_file_id": FILE_ID,
                                       "state": "DELIVERY_COMPLETED"})).start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.td.cleanup)
        _completed_manifest(self.root, "run-1", _trusted_artifacts())

    def _args(self, **over):
        args = argparse.Namespace(
            action="deliver", production_task_id=TASK,
            task_spec=str(self.spec_path), variant=None, run_id="",
            max_task_attempts=2, task_deadline_utc="",
            execute=True, confirm_gpu=True, authorize_task_gpu=True,
            allow_g4=True, allow_a100_highmem=True, fresh_inference=True,
            project_budget_cu=30.0, per_session_budget_cu=25.0,
            authorize_new_task=True, prior_unbilled_reserve_cu=0.14,
            max_wall_sec=7200, poll_interval_sec=60,
            gpu_policy="G4_THEN_A100_HIGHMEM", comfy_ref="c" * 40,
            workflow_api=str(self.root / "workflow_api.json"),
            cache_manifest=str(self.root / "model_cache" / "verified_manifest.json"),
            drive_source="download",
            drive_cache_dir="/content/drive/MyDrive/Muse-H3-Model-Depot",
            colab_auth="adc", budget_cu=0.0, max_rate_cu_h=14.0,
            approve_interactive_drive_mount=False, auto_recover=False,
            restore_local=False)
        for k, v in over.items():
            setattr(args, k, v)
        return args

    def test_deliver_passes_restore_local_true_to_verifier(self):
        result = hc.deliver_task(self._args(restore_local=True))
        self.assertTrue(result["delivered"])
        self.assertEqual(result["outcome"], "reused_completed")
        _, kwargs = self.verify.call_args
        self.assertTrue(kwargs.get("restore_local"))

    def test_deliver_defaults_to_readonly_verifier(self):
        result = hc.deliver_task(self._args())
        self.assertTrue(result["delivered"])
        _, kwargs = self.verify.call_args
        self.assertFalse(kwargs.get("restore_local"))


if __name__ == "__main__":
    unittest.main()

"""v0.3.0 M3 unit & fault-injection tests: host disconnect self-healing,
Drive 0-GPU archive recovery, and preflight token freshness.

Muse-original tests for the Muse-original M3 reimplementation (Issue #16).
Strictly 0.00 CU: all Colab CLI, Drive API, and ADC calls are mocked.
"""

from __future__ import annotations

import datetime
import json
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_depot  # noqa: E402
import h3_controller as hc  # noqa: E402
import h3_drive_token as dt  # noqa: E402
import h3_run_manifest as rm  # noqa: E402


class TransientRetryTest(unittest.TestCase):
    def test_succeeds_after_network_errors(self):
        calls = {"n": 0}
        sleeps = []

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("connection reset by peer")
            if calls["n"] == 2:
                raise ssl.SSLError("EOF occurred in violation of protocol")
            if calls["n"] == 3:
                raise TimeoutError("read timed out")
            return {"ok": True}

        res = hc._call_with_transient_retry(
            flaky, max_attempts=5, delays=(5, 10, 20, 30),
            sleep_fn=lambda s: sleeps.append(s))
        self.assertEqual(res, {"ok": True})
        self.assertEqual(calls["n"], 4)
        self.assertEqual(sleeps, [5, 10, 20])

    def test_message_based_transient_is_retried(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("read timed out")
            return "done"

        res = hc._call_with_transient_retry(
            flaky, max_attempts=5, delays=(5, 10, 20, 30),
            sleep_fn=lambda s: None)
        self.assertEqual(res, "done")
        self.assertEqual(calls["n"], 2)

    def test_fatal_errors_are_not_retried(self):
        for fatal in (ValueError("bad input"), RuntimeError("HTTP 403 Forbidden")):
            calls = {"n": 0}
            sleeps = []

            def fail():
                calls["n"] += 1
                raise fatal

            with self.assertRaises(type(fatal)):
                hc._call_with_transient_retry(
                    fail, max_attempts=5, delays=(5, 10, 20, 30),
                    sleep_fn=lambda s: sleeps.append(s))
            self.assertEqual(calls["n"], 1)
            self.assertEqual(sleeps, [])

    def test_exhaustion_raises_last_error(self):
        sleeps = []

        def always_timeout():
            raise TimeoutError("timed out")

        with self.assertRaises(TimeoutError):
            hc._call_with_transient_retry(
                always_timeout, max_attempts=5, delays=(5, 10, 20, 30),
                sleep_fn=lambda s: sleeps.append(s))
        self.assertEqual(sleeps, [5, 10, 20, 30])

    def test_on_retry_hook_reports_attempt_and_delay(self):
        seen = []

        def flaky():
            raise ConnectionResetError("reset")

        with self.assertRaises(ConnectionResetError):
            hc._call_with_transient_retry(
                flaky, max_attempts=3, delays=(5, 10),
                sleep_fn=lambda s: None,
                on_retry=lambda n, d, e: seen.append((n, d)))
        self.assertEqual(seen, [(1, 5), (2, 10)])


class DriveArchiveRecoveryTest(unittest.TestCase):
    def _create_manifest(self, root: Path, run_id: str, artifacts: dict | None = None) -> dict:
        rm.create_run_manifest(root, production_task_id="task-m3", run_id=run_id,
                               variant="A", code_commit="abc1234")
        rm.update_run(root, run_id, task_state="INFERENCE_COMPLETED",
                      session_id="muse-h3-stopped01", session_state="STOPPED",
                      artifacts=artifacts or {})
        return rm.load_run_manifest(root, run_id)

    def test_recover_via_manifest_file_id_0gpu(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_root = root / "output"
            run_id = "h3-m3-rec-01"
            manifest = self._create_manifest(root, run_id, artifacts={
                "drive_file_id": "fid-archive-1",
                "drive_output_folder_id": "fld-run-1",
                "delivery_state": "REMOTE_UPLOAD_VERIFIED",
            })
            fake_drive = mock.MagicMock()
            fake_drive.files().get().execute.return_value = {
                "id": "fid-archive-1", "name": "A.mp4", "size": "12",
                "trashed": False, "md5Checksum": "a" * 32, "parents": ["fld-run-1"],
            }
            fake_drive.files().get_media().execute.return_value = b"FAKE_MP4_DATA"
            fake_qa = {"sha256": "c" * 64, "frame_count": 274,
                       "width": 1080, "height": 1920, "fps": 24.0}
            with mock.patch.object(hc, "ROOT", root), \
                    mock.patch.object(hc, "OUTPUT_ROOT", out_root), \
                    mock.patch.object(hc, "RUNS", root / "logs"), \
                    mock.patch("h3_video_qa.validate_final_video", return_value=fake_qa):
                res = hc.recover_from_drive_archive(run_id, "A", manifest, drive=fake_drive)
            self.assertIsNotNone(res)
            self.assertTrue(res["resumed"])
            self.assertEqual(res["outcome"], "recovered_from_drive_archive_0gpu")
            self.assertEqual(res["drive_file_id"], "fid-archive-1")
            self.assertTrue((out_root / run_id / "A.mp4").is_file())
            self.assertEqual((out_root / run_id / "A.mp4").read_bytes(), b"FAKE_MP4_DATA")
            after = rm.load_run_manifest(root, run_id)
            self.assertEqual(after["task_state"], "TASK_COMPLETED")
            self.assertEqual(after["session_state"], "STOPPED")
            self.assertEqual(after["artifacts"]["delivery_state"], "DELIVERED")
            self.assertEqual(after["artifacts"]["drive_file_id"], "fid-archive-1")

    def test_recover_via_folder_lookup_when_no_file_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_root = root / "output"
            run_id = "h3-m3-rec-02"
            manifest = self._create_manifest(root, run_id, artifacts={})
            fake_drive = mock.MagicMock()
            fake_drive.files().get_media().execute.return_value = b"RECOVERED_MP4"

            def fake_list_named(svc, parent_id, name):
                if parent_id == "root" and name == "Muse-H3-Video-Outputs":
                    return [{"id": "root-out", "mimeType": "application/vnd.google-apps.folder",
                             "trashed": False}]
                if parent_id == "root-out" and name == run_id:
                    return [{"id": "run-fld", "mimeType": "application/vnd.google-apps.folder",
                             "trashed": False}]
                if parent_id == "run-fld" and name == "A.mp4":
                    return [{"id": "fid-found-2", "trashed": False}]
                return []

            fake_qa = {"sha256": "d" * 64, "frame_count": 274}
            with mock.patch.object(hc, "ROOT", root), \
                    mock.patch.object(hc, "OUTPUT_ROOT", out_root), \
                    mock.patch.object(hc, "RUNS", root / "logs"), \
                    mock.patch.object(drive_depot, "list_named", side_effect=fake_list_named), \
                    mock.patch("h3_video_qa.validate_final_video", return_value=fake_qa):
                res = hc.recover_from_drive_archive(run_id, "A", manifest, drive=fake_drive)
            self.assertIsNotNone(res)
            self.assertEqual(res["drive_file_id"], "fid-found-2")
            self.assertEqual(rm.load_run_manifest(root, run_id)["task_state"], "TASK_COMPLETED")

    def test_recover_returns_none_on_corrupt_mp4_without_mutating_dest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_root = root / "output"
            run_id = "h3-m3-bad"
            manifest = self._create_manifest(root, run_id,
                                             artifacts={"drive_file_id": "fid-bad"})
            fake_drive = mock.MagicMock()
            fake_drive.files().get().execute.return_value = {
                "id": "fid-bad", "name": "A.mp4", "trashed": False, "parents": ["fld-1"],
            }
            fake_drive.files().get_media().execute.return_value = b"CORRUPT"
            with mock.patch.object(hc, "ROOT", root), \
                    mock.patch.object(hc, "OUTPUT_ROOT", out_root), \
                    mock.patch.object(hc, "RUNS", root / "logs"), \
                    mock.patch("h3_video_qa.validate_final_video",
                              side_effect=RuntimeError("ffprobe failed")):
                res = hc.recover_from_drive_archive(run_id, "A", manifest, drive=fake_drive)
            self.assertIsNone(res)
            self.assertFalse((out_root / run_id / "A.mp4").exists())
            self.assertNotEqual(rm.load_run_manifest(root, run_id)["task_state"], "TASK_COMPLETED")

    def test_recover_returns_none_when_drive_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = self._create_manifest(root, "h3-m3-down",
                                             artifacts={"drive_file_id": "fid-1"})
            with mock.patch.object(hc, "ROOT", root), \
                    mock.patch("drive_depot.drive_service",
                              side_effect=RuntimeError("ADC unavailable")):
                res = hc.recover_from_drive_archive("h3-m3-down", "A", manifest)
            self.assertIsNone(res)


class ResumeStoppedSessionRecoveryTest(unittest.TestCase):
    """_resume_stopped_session tries the 0-GPU archive recovery before
    the metadata-only server verify, and falls back to it when the
    download fails."""

    def _setup(self, td, run_id, artifacts):
        root = Path(td)
        rm.create_run_manifest(root, production_task_id="task-m3", run_id=run_id,
                               variant="A", code_commit="abc1234")
        rm.update_run(root, run_id, task_state="INFERENCE_COMPLETED",
                      session_id="muse-h3-stopped01", session_state="STOPPED",
                      artifacts=artifacts)
        return root, rm.load_run_manifest(root, run_id)

    def test_stopped_session_recovers_from_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root, manifest = self._setup(td, "h3-m3-res-01", {
                "drive_file_id": "fid-archive-1",
                "drive_output_folder_id": "fld-run-1",
                "delivery_state": "REMOTE_UPLOAD_VERIFIED",
            })
            fake_drive = mock.MagicMock()
            fake_drive.files().get().execute.return_value = {
                "id": "fid-archive-1", "name": "A.mp4", "size": "12",
                "trashed": False, "md5Checksum": "a" * 32, "parents": ["fld-run-1"],
            }
            fake_drive.files().get_media().execute.return_value = b"FAKE_MP4_DATA"
            fake_qa = {"sha256": "c" * 64, "frame_count": 274}
            args = mock.Mock()
            with mock.patch.object(hc, "ROOT", root), \
                    mock.patch.object(hc, "OUTPUT_ROOT", root / "output"), \
                    mock.patch.object(hc, "RUNS", root / "logs"), \
                    mock.patch.object(drive_depot, "drive_service", return_value=fake_drive), \
                    mock.patch("h3_video_qa.validate_final_video", return_value=fake_qa):
                res = hc._resume_stopped_session(args, manifest)
            self.assertTrue(res["resumed"])
            self.assertEqual(res["outcome"], "recovered_from_drive_archive_0gpu")
            self.assertEqual(rm.load_run_manifest(root, "h3-m3-res-01")["task_state"],
                             "TASK_COMPLETED")

    def test_download_failure_falls_back_to_server_verify(self):
        with tempfile.TemporaryDirectory() as td:
            root, manifest = self._setup(td, "h3-m3-res-02", {
                "drive_file_id": "fid-verify-2",
                "drive_output_folder_id": "fld-run-2",
                "delivery_state": "REMOTE_UPLOAD_VERIFIED",
            })
            fake_drive = mock.MagicMock()
            fake_drive.files().get().execute.return_value = {
                "id": "fid-verify-2", "name": "A.mp4", "size": "12",
                "trashed": False, "md5Checksum": "a" * 32, "parents": ["fld-run-2"],
            }
            # The archive download fails (transient transport error is
            # retried, then the recovery gives up); the metadata-only
            # server verify that follows still completes the run.
            fake_drive.files().get_media().execute.side_effect = TimeoutError("read timed out")
            args = mock.Mock()
            with mock.patch.object(hc, "ROOT", root), \
                    mock.patch.object(hc, "OUTPUT_ROOT", root / "output"), \
                    mock.patch.object(hc, "RUNS", root / "logs"), \
                    mock.patch.object(drive_depot, "drive_service", return_value=fake_drive), \
                    mock.patch.object(hc, "_call_with_transient_retry",
                                     wraps=hc._call_with_transient_retry) as spy:
                # Keep the retry fast: the download's 5 attempts would
                # sleep 5+10+20+30 s in real time.
                orig = hc._call_with_transient_retry

                def _fast(fn, **kw):
                    kw["sleep_fn"] = lambda s: None
                    return orig(fn, **kw)

                spy.side_effect = _fast
                res = hc._resume_stopped_session(args, manifest)
            self.assertTrue(res["resumed"])
            self.assertEqual(res["outcome"],
                             "session stopped; Drive file already verified server-side")
            self.assertEqual(rm.load_run_manifest(root, "h3-m3-res-02")["task_state"],
                             "TASK_COMPLETED")


class TokenFreshnessTest(unittest.TestCase):
    @staticmethod
    def _fake_google_auth_modules(creds) -> dict[str, ModuleType]:
        google_mod = ModuleType("google")
        auth_mod = ModuleType("google.auth")
        transport_mod = ModuleType("google.auth.transport")
        requests_mod = ModuleType("google.auth.transport.requests")

        auth_mod.default = lambda scopes=None: (creds, "proj")  # type: ignore[attr-defined]
        requests_mod.Request = lambda: object()  # type: ignore[attr-defined]
        transport_mod.requests = requests_mod  # type: ignore[attr-defined]
        auth_mod.transport = transport_mod  # type: ignore[attr-defined]
        google_mod.auth = auth_mod  # type: ignore[attr-defined]
        return {
            "google": google_mod,
            "google.auth": auth_mod,
            "google.auth.transport": transport_mod,
            "google.auth.transport.requests": requests_mod,
        }

    def test_refreshes_when_below_threshold(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        soon = (now + datetime.timedelta(seconds=120)).replace(tzinfo=None)
        later = (now + datetime.timedelta(seconds=3600)).replace(tzinfo=None)

        class FakeCreds:
            service_account_email = None
            valid = True
            token = "synthetic-token"
            expiry = soon
            refreshed_calls = 0

            def refresh(self, req):
                self.refreshed_calls += 1
                self.token = "synthetic-refreshed"
                self.expiry = later

        creds = FakeCreds()
        with mock.patch.dict(sys.modules, self._fake_google_auth_modules(creds)):
            res = dt.ensure_token_freshness(min_remaining_sec=900)
        self.assertTrue(res["refreshed"])
        self.assertEqual(creds.refreshed_calls, 1)
        self.assertGreaterEqual(res["remaining_sec"], 3500)
        self.assertNotIn("synthetic-refreshed", json.dumps(res))
        self.assertNotIn("synthetic-token", json.dumps(res))

    def test_skips_refresh_when_fresh(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        later = (now + datetime.timedelta(seconds=2400)).replace(tzinfo=None)

        class FakeCreds:
            service_account_email = None
            valid = True
            token = "synthetic-token"
            expiry = later
            refreshed_calls = 0

            def refresh(self, req):
                self.refreshed_calls += 1

        creds = FakeCreds()
        with mock.patch.dict(sys.modules, self._fake_google_auth_modules(creds)):
            res = dt.ensure_token_freshness(min_remaining_sec=900)
        self.assertFalse(res["refreshed"])
        self.assertEqual(creds.refreshed_calls, 0)
        self.assertGreaterEqual(res["remaining_sec"], 2300)

    def test_service_account_refused(self):
        class FakeCreds:
            service_account_email = "sa@project.iam.gserviceaccount.com"
            valid = True
            token = "synthetic-token"
            expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

            def refresh(self, req):
                raise AssertionError("must not refresh a service account")

        with mock.patch.dict(sys.modules, self._fake_google_auth_modules(FakeCreds())):
            with self.assertRaisesRegex(RuntimeError, "Service-account"):
                dt.ensure_token_freshness()

    def test_refresh_without_token_fails_closed(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        soon = (now + datetime.timedelta(seconds=10)).replace(tzinfo=None)

        class FakeCreds:
            service_account_email = None
            valid = False
            token = None
            expiry = soon

            def refresh(self, req):
                self.valid = True  # refreshed, but still no token material

        with mock.patch.dict(sys.modules, self._fake_google_auth_modules(FakeCreds())):
            with self.assertRaisesRegex(RuntimeError, "did not yield an access token"):
                dt.ensure_token_freshness()


class PollWorkerRetryTest(unittest.TestCase):
    def test_poll_retries_transient_exec_failure(self):
        calls = {"n": 0}

        def flaky_exec(session, path, timeout=None, env=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise TimeoutError("read timed out")
            return {"returncode": 0,
                    "stdout": 'MUSE_H3_JOB_STATE_JSON:{"run_id": "r1", "variant": "A", '
                              '"stage": "VIDEO_READY"}',
                    "stderr": ""}

        with mock.patch.object(hc, "exec_script", side_effect=flaky_exec), \
                mock.patch.object(hc.time, "sleep", lambda s: None):
            state = hc.poll_worker("muse-h3-test")
        self.assertEqual(state["stage"], "VIDEO_READY")
        self.assertEqual(calls["n"], 3)

    def test_poll_does_not_retry_fatal_exec_failure(self):
        calls = {"n": 0}

        def fatal_exec(session, path, timeout=None, env=None):
            calls["n"] += 1
            raise RuntimeError("HTTP 403 Forbidden")

        with mock.patch.object(hc, "exec_script", side_effect=fatal_exec), \
                mock.patch.object(hc.time, "sleep", lambda s: None):
            with self.assertRaises(hc.WorkerPollError):
                hc.poll_worker("muse-h3-test")
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()

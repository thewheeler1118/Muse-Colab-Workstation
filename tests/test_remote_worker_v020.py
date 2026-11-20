"""v0.2.0 F3 + F4 tests: dual-track parallel setup, wheelhouse-first
install, independent NVMe model store + high-VRAM flags.

Offline only (0 CU): fake transport, fake pip, fake Drive downloads,
fake GPU probe, tmp filesystems. No GPU, no Drive, no network.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import unittest

import h3_remote_worker as hrw
from h3_contract import MODEL_FILES
from h3_remote_worker import (
    SetupTrackFailed,
    _SetupAborted,
    _abort_progress,
    _cmd_failure_get,
    _request_token_refresh,
    _runtime_python_tag,
    _safe_extract_tar,
    _select_wheelhouse_entry,
    comfy_vram_flags,
    detect_gpu_vram_mib,
    ensure_comfy,
    link_models_into_comfy,
    nvme_models_dir,
    restore_weights,
    run_cmd,
    run_setup_parallel,
    worker_failure_info,
)


def _fake_manifest():
    return {
        "status": "READY",
        "verification_method": "drive_v3_server_api",
        "account_verified": True,
        "verified_at_utc": "2026-01-01T00:00:00Z",
        "files": [
            {
                "path": p,
                "size": 1024,
                "sha256": "a" * 64,
                "drive_file_id": "fake-id-" + p,
                "server_verified": True,
                "hf_revision": "rev",
            }
            for p in sorted(MODEL_FILES)
        ],
    }


def _real_manifest_and_content():
    """Manifest + bytes for the four MODEL_FILES paths, with real
    sizes and SHA-256s, so link/restore tests verify real content."""
    content = {}
    files = []
    for p in sorted(MODEL_FILES):
        data = (f"weights:{p}:".encode() * 8)[:256]
        content[p] = data
        files.append({
            "path": p,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "drive_file_id": "fake-id-" + p,
            "server_verified": True,
            "hf_revision": "rev",
        })
    manifest = {
        "status": "READY",
        "verification_method": "drive_v3_server_api",
        "account_verified": True,
        "verified_at_utc": "2026-01-01T00:00:00Z",
        "files": files,
    }
    return manifest, content


def _big_disk_usage():
    return type("usage", (), {"total": 200 * 1024 ** 3,
                             "used": 10 * 1024 ** 3,
                             "free": 190 * 1024 ** 3})()

class RunSetupParallelTest(unittest.TestCase):
    def test_both_tracks_run_concurrently(self):
        # Deterministic proof, not timing: both tracks rendezvous at a
        # barrier. Sequential execution would break the barrier.
        barrier = threading.Barrier(2, timeout=10)
        passed = []

        def track():
            barrier.wait(timeout=10)
            passed.append(True)

        run_setup_parallel([("ensure_comfy", track), ("restore_weights", track)])
        self.assertEqual(passed, [True, True])

    def test_success_returns_none(self):
        self.assertIsNone(
            run_setup_parallel([("ensure_comfy", lambda: None),
                                ("restore_weights", lambda: None)]))

    def test_ensure_failure_reports_ensure_stage_and_aborts_other(self):
        abort = threading.Event()
        observed = []

        def survivor():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if abort.is_set():
                    observed.append(True)
                    return
                time.sleep(0.01)
            observed.append(False)

        def failing():
            raise RuntimeError("comfy boom")

        with self.assertRaises(SetupTrackFailed) as cm:
            run_setup_parallel([("ensure_comfy", failing),
                                ("restore_weights", survivor)],
                               abort_event=abort)
        self.assertEqual(cm.exception.stage, "ensure_comfy")
        self.assertIsInstance(cm.exception.exc, RuntimeError)
        self.assertEqual(observed, [True])

    def test_restore_failure_reports_restore_stage(self):
        abort = threading.Event()
        with self.assertRaises(SetupTrackFailed) as cm:
            run_setup_parallel([
                ("ensure_comfy", lambda: time.sleep(0.05)),
                ("restore_weights", lambda: (_ for _ in ()).throw(
                    RuntimeError("weights boom")))],
                abort_event=abort)
        self.assertEqual(cm.exception.stage, "restore_weights")

    def test_earliest_failure_wins(self):
        abort = threading.Event()

        def slow_fail():
            time.sleep(0.2)
            raise RuntimeError("slow")

        def fast_fail():
            raise RuntimeError("fast")

        with self.assertRaises(SetupTrackFailed) as cm:
            run_setup_parallel([("ensure_comfy", slow_fail),
                                ("restore_weights", fast_fail)],
                               abort_event=abort)
        self.assertEqual(cm.exception.stage, "restore_weights")
        self.assertEqual(str(cm.exception.exc), "fast")

    def test_failure_carries_cmd_record(self):
        abort = threading.Event()

        def failing_cmd():
            run_cmd([sys.executable, "-c",
                     "import sys; sys.stderr.write('fatal: track boom\\n'); "
                     "raise SystemExit(3)"])

        with self.assertRaises(SetupTrackFailed) as cm:
            run_setup_parallel([("ensure_comfy", failing_cmd),
                                ("restore_weights", lambda: None)],
                               abort_event=abort)
        self.assertEqual(cm.exception.stage, "ensure_comfy")
        record = cm.exception.cmd_failure
        self.assertIsNotNone(record)
        self.assertEqual(record["returncode"], 3)
        self.assertIn("fatal: track boom", record["stderr"])
        # And worker_failure_info carries it to the structured line.
        info = worker_failure_info(cm.exception.stage, cm.exception)
        self.assertEqual(info["cmd_failure"]["returncode"], 3)


class TokenRefreshThreadSafetyTest(unittest.TestCase):
    def test_concurrent_refreshes_ask_once_per_token(self):
        writes = []

        def fake_write_job_state(*args, **kwargs):
            writes.append(kwargs.get("extra") or args)

        provider = mock.Mock()
        provider.token_id = "abc123"
        old_run, old_variant = hrw._JOB_RUN_ID, hrw._JOB_VARIANT
        old_last = hrw._LAST_REFRESH_REQUEST_TOKEN_ID
        hrw._JOB_RUN_ID, hrw._JOB_VARIANT = "run-x", "v1"
        hrw._LAST_REFRESH_REQUEST_TOKEN_ID = None
        try:
            with mock.patch.object(hrw, "write_job_state", fake_write_job_state):
                threads = [threading.Thread(
                    target=_request_token_refresh, args=(provider,))
                    for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(len(writes), 1)
            # A different token id is a new request.
            provider.token_id = "def456"
            with mock.patch.object(hrw, "write_job_state", fake_write_job_state):
                _request_token_refresh(provider)
            self.assertEqual(len(writes), 2)
        finally:
            hrw._JOB_RUN_ID, hrw._JOB_VARIANT = old_run, old_variant
            hrw._LAST_REFRESH_REQUEST_TOKEN_ID = old_last


class CmdFailureThreadLocalTest(unittest.TestCase):
    def test_concurrent_cmd_failures_keep_own_records(self):
        barrier = threading.Barrier(2, timeout=10)
        snapshots = {}

        def failing(marker, key):
            barrier.wait(timeout=10)
            try:
                run_cmd([sys.executable, "-c",
                         f"import sys,time; time.sleep(0.05); "
                         f"sys.stderr.write('fatal: {marker}\\n'); "
                         "raise SystemExit(3)"])
            except subprocess.CalledProcessError:
                snapshots[key] = _cmd_failure_get()

        threads = [
            threading.Thread(target=failing, args=("MARK-A", "a")),
            threading.Thread(target=failing, args=("MARK-B", "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn("MARK-A", snapshots["a"]["stderr"])
        self.assertNotIn("MARK-B", snapshots["a"]["stderr"])
        self.assertIn("MARK-B", snapshots["b"]["stderr"])
        self.assertNotIn("MARK-A", snapshots["b"]["stderr"])

    def test_worker_failure_info_prefers_carried_record(self):
        hrw._cmd_failure_set(None)
        exc = SetupTrackFailed("ensure_comfy", RuntimeError("x"),
                               {"returncode": 7, "cmd": "pip"})
        info = worker_failure_info("ensure_comfy", exc)
        self.assertEqual(info["cmd_failure"]["returncode"], 7)


class WheelhouseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "muse_h3"
        self.root.mkdir()
        self.patch_root = mock.patch.object(hrw, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.tag = _runtime_python_tag()
        self.commit = "deadbeef"
        self.archive_name = f"comfyui-wheelhouse-{self.commit}-{self.tag}.tar.gz"
        self.tar_path = Path(self.tmp.name) / self.archive_name
        self._build_tar(meta_commit=self.commit, meta_tag=self.tag)
        self.entry = {
            "file_id": "drive-file-1",
            "size": self.tar_path.stat().st_size,
            "sha256": "b" * 64,
            "archive": self.archive_name,
            "commit": self.commit,
            "python_tag": self.tag,
        }

    def _build_tar(self, meta_commit, meta_tag, members=None):
        meta = {"commit": meta_commit, "python_tag": meta_tag,
                "archive_name": self.archive_name, "entries": []}
        with tarfile.open(self.tar_path, "w:gz") as tf:
            meta_bytes = json.dumps(meta).encode()
            info = tarfile.TarInfo("wheelhouse/WHEELHOUSE_META.json")
            info.size = len(meta_bytes)
            tf.addfile(info, io.BytesIO(meta_bytes))
            for name, data in (members or [("wheelhouse/fake-1.0-py3-none-any.whl", b"")]):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    def _fake_downloader(self, received):
        tar_path = self.tar_path

        class FakeDownloader:
            def __init__(self, provider):
                self.provider = provider

            def download_file(self, file_id, size, sha256, dest, **kwargs):
                received.append(kwargs)
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(tar_path.read_bytes())

        return FakeDownloader

    def _run_ensure(self, drive_download, pip_calls, pip_fail_offline=False):
        comfy = self.root / "ComfyUI"
        comfy.mkdir()
        (comfy / ".git").mkdir()
        (comfy / "requirements.txt").write_text("fake-pkg==1.0\n")
        provider = mock.Mock()

        def fake_run_cmd(args, timeout=1200, cwd=None):
            pip_calls.append(list(args))
            if pip_fail_offline and "--no-index" in args:
                raise subprocess.CalledProcessError(1, args, "", "offline fail")
            return mock.Mock()

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch("h3_drive_download.DriveDownloader",
                           self._fake_downloader([])), \
                mock.patch.object(hrw, "run_cmd", fake_run_cmd), \
                mock.patch.object(hrw, "verify_comfyui_commit", return_value=True), \
                mock.patch.object(hrw, "comfyui_commit", return_value="ref-1"), \
                mock.patch.object(hrw, "pip_install_needed", return_value=True):
            ensure_comfy(comfy, "ref-1", drive_download=drive_download,
                         token_provider=provider)
        return out.getvalue()


class WheelhouseInstallTest(WheelhouseTestBase):
    def test_offline_install_used_when_entry_matches(self):
        pip_calls = []
        out = self._run_ensure({"wheelhouses": [self.entry]}, pip_calls)
        offline = [c for c in pip_calls if "--no-index" in c]
        self.assertEqual(len(offline), 1)
        self.assertIn("--find-links", offline[0])
        wheels_dir = Path(offline[0][offline[0].index("--find-links") + 1])
        self.assertTrue((wheels_dir / "fake-1.0-py3-none-any.whl").exists())
        self.assertIn("WHEELHOUSE_INSTALL_OK", out)
        self.assertNotIn("WHEELHOUSE_FALLBACK_PYPI", out)

    def test_offline_pip_failure_falls_back_to_pypi(self):
        pip_calls = []
        out = self._run_ensure({"wheelhouses": [self.entry]}, pip_calls,
                               pip_fail_offline=True)
        self.assertEqual(len(pip_calls), 2)
        self.assertIn("--no-index", pip_calls[0])
        self.assertNotIn("--no-index", pip_calls[1])
        self.assertIn("WHEELHOUSE_FALLBACK_PYPI", out)
        payload = json.loads(out.split("WHEELHOUSE_FALLBACK_PYPI", 1)[1].strip())
        self.assertIn("CalledProcessError", payload["reason"])

    def test_meta_commit_mismatch_falls_back(self):
        self._build_tar(meta_commit="other", meta_tag=self.tag)
        pip_calls = []
        out = self._run_ensure({"wheelhouses": [self.entry]}, pip_calls)
        self.assertEqual(len(pip_calls), 1)
        self.assertNotIn("--no-index", pip_calls[0])
        self.assertIn("WHEELHOUSE_FALLBACK_PYPI", out)

    def test_tar_traversal_member_rejected_and_falls_back(self):
        self._build_tar(meta_commit=self.commit, meta_tag=self.tag,
                        members=[("wheelhouse/fake-1.0-py3-none-any.whl", b""),
                                 ("../evil.txt", b"evil")])
        pip_calls = []
        out = self._run_ensure({"wheelhouses": [self.entry]}, pip_calls)
        self.assertNotIn("--no-index", pip_calls[0])
        self.assertIn("WHEELHOUSE_FALLBACK_PYPI", out)
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((Path(self.tmp.name) / "evil.txt").exists())

    def test_no_entry_plain_pypi_no_event(self):
        pip_calls = []
        out = self._run_ensure({}, pip_calls)
        self.assertEqual(len(pip_calls), 1)
        self.assertNotIn("--no-index", pip_calls[0])
        self.assertNotIn("WHEELHOUSE_FALLBACK_PYPI", out)
        self.assertNotIn("WHEELHOUSE_INSTALL_OK", out)

    def test_python_tag_mismatch_falls_back(self):
        entry = dict(self.entry, python_tag="py999")
        pip_calls = []
        out = self._run_ensure({"wheelhouses": [entry]}, pip_calls)
        self.assertNotIn("--no-index", pip_calls[0])
        self.assertIn("WHEELHOUSE_FALLBACK_PYPI", out)
        payload = json.loads(out.split("WHEELHOUSE_FALLBACK_PYPI", 1)[1].strip())
        self.assertIn("no wheelhouse entry for", payload["reason"])
        self.assertIn(self.tag, payload["reason"])

    def test_select_entry_prefers_matching_tag(self):
        entry, reason = _select_wheelhouse_entry(
            {"wheelhouses": [dict(self.entry, python_tag="py999"), self.entry]})
        self.assertEqual(entry, self.entry)
        self.assertIsNone(reason)

    def test_select_entry_none_without_entries(self):
        entry, reason = _select_wheelhouse_entry({})
        self.assertIsNone(entry)
        self.assertIn("no wheelhouse entry", reason)

    def test_safe_extract_rejects_symlink(self):
        bad = Path(self.tmp.name) / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tf:
            info = tarfile.TarInfo("wheelhouse/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        dest = Path(self.tmp.name) / "out"
        with self.assertRaises(ValueError):
            _safe_extract_tar(bad, dest)

    def test_runtime_python_tag_matches_interpreter(self):
        self.assertEqual(_runtime_python_tag(),
                         f"py{sys.version_info.major}{sys.version_info.minor}")


class AbortProgressTest(unittest.TestCase):
    def test_wrapper_raises_when_event_set(self):
        event = threading.Event()
        event.set()
        wrapped = _abort_progress(None, event)
        with self.assertRaises(_SetupAborted):
            wrapped(1, 10)

    def test_wrapper_delegates_when_unset(self):
        seen = []
        event = threading.Event()
        wrapped = _abort_progress(lambda d, t: seen.append((d, t)), event)
        wrapped(3, 10)
        self.assertEqual(seen, [(3, 10)])

    def test_wrapper_returns_original_when_no_event(self):
        cb = lambda d, t: None
        self.assertIs(_abort_progress(cb, None), cb)
        self.assertIsNone(_abort_progress(None, None))

    def test_restore_weights_drive_path_aborts_via_progress(self):
        # The fake downloader invokes the progress callback once; with
        # the abort preset, the wrapped callback raises _SetupAborted
        # instead of letting the download proceed.
        import shutil as _shutil

        class NotifyingDownloader:
            def __init__(self, *a, **k):
                pass

            def download_file(self, file_id, size, sha256, dest, **kw):
                # The sibling fails mid-download: set the event, then
                # invoke the callback — the wrapped callback must raise
                # _SetupAborted instead of letting the download proceed.
                abort.set()
                kw["on_progress"](1, size)

        manifest = _fake_manifest()
        drive_download = {
            "files": [{"file_id": f["drive_file_id"], "path": f["path"],
                       "size": f["size"], "sha256": f["sha256"]}
                      for f in manifest["files"]],
            "dest_dir": "/nonexistent",
        }
        usage = type("usage", (), {"total": 200 * 1024 ** 3,
                                   "used": 10 * 1024 ** 3,
                                   "free": 190 * 1024 ** 3})()
        abort = threading.Event()
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("h3_drive_download.DriveDownloader",
                            NotifyingDownloader), \
                    mock.patch.object(_shutil, "disk_usage",
                                      return_value=usage):
                with self.assertRaises(_SetupAborted):
                    restore_weights(Path(d) / "ComfyUI", Path("/nonexistent"),
                                    manifest, drive_download=drive_download,
                                    token_provider=mock.Mock(),
                                    abort_event=abort)


class RestoreWeightsAbortTest(unittest.TestCase):
    def test_preset_abort_event_fails_fast_in_mount_path(self):
        abort = threading.Event()
        abort.set()
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache"
            cache.mkdir()
            with self.assertRaises(RuntimeError) as cm:
                restore_weights(Path(d) / "ComfyUI", cache, _fake_manifest(),
                                abort_event=abort)
            self.assertIn("aborted", str(cm.exception))

    def test_unset_abort_event_does_not_fire(self):
        # Without an abort the mount path proceeds to its normal checks
        # and fails on something else (here the disk-capacity gate,
        # which is environment-dependent) — proving the abort check
        # itself did not fire.
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "cache"
            cache.mkdir()
            with self.assertRaises(RuntimeError) as cm:
                restore_weights(Path(d) / "ComfyUI", cache, _fake_manifest(),
                                abort_event=threading.Event())
            self.assertNotIn("aborted", str(cm.exception))


class NvmeStoreTest(unittest.TestCase):
    """v0.2.0 F4: the NVMe model store + link_models_into_comfy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / "nvme"
        self.comfy = self.base / "ComfyUI"
        self.manifest, self.content = _real_manifest_and_content()

    def _write_store(self, corrupt=None, missing=None):
        for path, data in self.content.items():
            if path == missing:
                continue
            f = self.store / path
            f.parent.mkdir(parents=True, exist_ok=True)
            # Corrupt bytes keep the size: the checksum check (not
            # the size check) must be the one that fires.
            bad = bytes([data[0] ^ 0xFF]) + data[1:] if data else b"x"
            f.write_bytes(bad if path == corrupt else data)

    def test_nvme_models_dir_default_and_env_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("H3_NVME_MODELS_DIR", None)
            self.assertEqual(nvme_models_dir(),
                             Path("/content/nvme_models/h3"))
            with mock.patch.dict(os.environ,
                                {"H3_NVME_MODELS_DIR": "/tmp/store-x"}):
                self.assertEqual(nvme_models_dir(), Path("/tmp/store-x"))
            with mock.patch.dict(os.environ,
                                {"H3_NVME_MODELS_DIR": "   "}):
                self.assertEqual(nvme_models_dir(),
                                 Path("/content/nvme_models/h3"))

    def test_link_verified_weights(self):
        self._write_store()
        link_models_into_comfy(self.store, self.comfy, self.manifest)
        for path, data in self.content.items():
            dst = self.comfy / "models" / path
            self.assertTrue(dst.is_symlink(), path)
            self.assertEqual(os.readlink(dst), str(self.store / path))
            self.assertEqual(dst.read_bytes(), data)

    def test_link_rejects_missing_store_file(self):
        missing = sorted(self.content)[0]
        self._write_store(missing=missing)
        with self.assertRaises(RuntimeError) as cm:
            link_models_into_comfy(self.store, self.comfy, self.manifest)
        self.assertIn("missing", str(cm.exception))
        self.assertFalse((self.comfy / "models" / missing).exists())

    def test_link_rejects_size_mismatch(self):
        path = sorted(self.content)[0]
        self._write_store()
        (self.store / path).write_bytes(b"short")
        with self.assertRaises(RuntimeError) as cm:
            link_models_into_comfy(self.store, self.comfy, self.manifest)
        self.assertIn("size", str(cm.exception))

    def test_link_verify_rejects_bad_checksum(self):
        path = sorted(self.content)[0]
        self._write_store(corrupt=path)
        with self.assertRaises(RuntimeError) as cm:
            link_models_into_comfy(self.store, self.comfy, self.manifest)
        self.assertIn("checksum", str(cm.exception))

    def test_link_verify_false_trusts_caller(self):
        # Documented contract: verify=False only for callers that
        # verified the store files in this same run. The link step
        # then links without hashing.
        path = sorted(self.content)[0]
        self._write_store(corrupt=path)
        link_models_into_comfy(self.store, self.comfy, self.manifest,
                               verify=False)
        self.assertTrue((self.comfy / "models" / path).is_symlink())

    def test_relink_is_idempotent(self):
        self._write_store()
        link_models_into_comfy(self.store, self.comfy, self.manifest)
        first = {p: os.readlink(self.comfy / "models" / p)
                 for p in self.content}
        link_models_into_comfy(self.store, self.comfy, self.manifest)
        second = {p: os.readlink(self.comfy / "models" / p)
                  for p in self.content}
        self.assertEqual(first, second)

    def test_stale_link_replaced(self):
        self._write_store()
        path = sorted(self.content)[0]
        dst = self.comfy / "models" / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        decoy = self.base / "decoy.bin"
        decoy.write_bytes(b"decoy")
        os.symlink(str(decoy), dst)
        link_models_into_comfy(self.store, self.comfy, self.manifest)
        self.assertEqual(os.readlink(dst), str(self.store / path))
        self.assertEqual(dst.read_bytes(), self.content[path])

    def test_real_file_with_right_content_left_alone(self):
        self._write_store()
        path = sorted(self.content)[0]
        dst = self.comfy / "models" / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(self.content[path])
        link_models_into_comfy(self.store, self.comfy, self.manifest)
        self.assertFalse(dst.is_symlink())
        self.assertEqual(dst.read_bytes(), self.content[path])

    def test_real_file_with_wrong_content_fails_closed(self):
        self._write_store()
        path = sorted(self.content)[0]
        dst = self.comfy / "models" / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"wrong-content-here")
        with self.assertRaises(RuntimeError) as cm:
            link_models_into_comfy(self.store, self.comfy, self.manifest)
        self.assertIn("wrong content", str(cm.exception))
        # The real file is untouched.
        self.assertEqual(dst.read_bytes(), b"wrong-content-here")
        self.assertFalse(dst.is_symlink())

    def test_manifest_with_escaping_path_rejected(self):
        bad = dict(self.manifest)
        bad["files"] = [dict(f) for f in self.manifest["files"]]
        bad["files"][0]["path"] = "../evil.safetensors"
        with self.assertRaises(Exception):
            link_models_into_comfy(self.store, self.comfy, bad)


class VramFlagsTest(unittest.TestCase):
    """v0.2.0 F4: detect_gpu_vram_mib + comfy_vram_flags."""

    def _fake_run(self, stdout, returncode=0):
        proc = mock.Mock()
        proc.stdout = stdout
        proc.returncode = returncode
        return mock.patch.object(hrw.subprocess, "run",
                                 return_value=proc)

    def test_detect_parses_nvidia_smi(self):
        with self._fake_run("81920\n"):
            self.assertEqual(detect_gpu_vram_mib(), 81920)

    def test_detect_returns_none_on_failure(self):
        with self._fake_run("", returncode=1):
            self.assertIsNone(detect_gpu_vram_mib())
        with mock.patch.object(hrw.subprocess, "run",
                               side_effect=FileNotFoundError("no nvidia-smi")):
            self.assertIsNone(detect_gpu_vram_mib())
        with self._fake_run("N/A\n"):
            self.assertIsNone(detect_gpu_vram_mib())
        with self._fake_run(""):
            self.assertIsNone(detect_gpu_vram_mib())

    def test_flags_for_high_vram(self):
        self.assertEqual(comfy_vram_flags(81920),
                         ["--highvram", "--disable-mmap"])
        self.assertEqual(comfy_vram_flags(76800),
                         ["--highvram", "--disable-mmap"])
        self.assertEqual(comfy_vram_flags(76799), [])

    def test_flags_none_when_no_gpu_detected(self):
        with mock.patch.object(hrw, "detect_gpu_vram_mib", return_value=None):
            self.assertEqual(comfy_vram_flags(), [])

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ,
                             {"H3_COMFY_VRAM_FLAGS": "--lowvram --foo"}):
            self.assertEqual(comfy_vram_flags(81920),
                             ["--lowvram", "--foo"])
        with mock.patch.dict(os.environ, {"H3_COMFY_VRAM_FLAGS": ""}):
            self.assertEqual(comfy_vram_flags(81920), [])


class RestoreWeightsNvmeTest(unittest.TestCase):
    """v0.2.0 F4: restore_weights writes the store, then links."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.comfy = self.base / "ComfyUI"
        self.store = self.base / "nvme"
        self.manifest, self.content = _real_manifest_and_content()
        self.drive_download = {
            "files": [{"file_id": f["drive_file_id"], "path": f["path"],
                       "size": f["size"], "sha256": f["sha256"]}
                      for f in self.manifest["files"]],
            "dest_dir": "/nonexistent",
        }

    def _assert_store_and_links(self):
        for path, data in self.content.items():
            self.assertEqual((self.store / path).read_bytes(), data)
            dst = self.comfy / "models" / path
            self.assertTrue(dst.is_symlink(), path)
            self.assertEqual(dst.read_bytes(), data)

    def test_drive_path_restores_into_store_and_links(self):
        content = self.content

        class WritingDownloader:
            def __init__(self, *a, **k):
                pass

            def download_file(self, file_id, size, sha256, dest, **kw):
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                path = next(f["path"] for f in
                            self_files if f["drive_file_id"] == file_id)
                dest.write_bytes(content[path])
                return {"drive_file_id": file_id, "size": size,
                        "sha256": sha256, "path": str(dest)}

        self_files = self.manifest["files"]
        with mock.patch("h3_drive_download.DriveDownloader",
                        WritingDownloader), \
                mock.patch.object(shutil, "disk_usage",
                                  return_value=_big_disk_usage()):
            restore_weights(self.comfy, Path("/nonexistent"), self.manifest,
                            drive_download=self.drive_download,
                            token_provider=mock.Mock(),
                            nvme_dir=self.store)
        self._assert_store_and_links()

    def test_mount_path_copies_into_store_and_links(self):
        cache = self.base / "cache"
        for path, data in self.content.items():
            f = cache / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        with mock.patch.object(shutil, "disk_usage",
                               return_value=_big_disk_usage()):
            restore_weights(self.comfy, cache, self.manifest,
                            nvme_dir=self.store)
        self._assert_store_and_links()

    def test_reuse_via_store_skips_download(self):
        for path, data in self.content.items():
            f = self.store / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        calls = []

        class CountingDownloader:
            def __init__(self, *a, **k):
                pass

            def download_file(self, *a, **k):
                calls.append(1)
                raise AssertionError("must not download when store is valid")

        with mock.patch("h3_drive_download.DriveDownloader",
                        CountingDownloader), \
                mock.patch.object(shutil, "disk_usage",
                                  return_value=_big_disk_usage()):
            restore_weights(self.comfy, Path("/nonexistent"), self.manifest,
                            drive_download=self.drive_download,
                            token_provider=mock.Mock(),
                            nvme_dir=self.store)
        self.assertEqual(calls, [])
        self._assert_store_and_links()

if __name__ == "__main__":
    unittest.main()

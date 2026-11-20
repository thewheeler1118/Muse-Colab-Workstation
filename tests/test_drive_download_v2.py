"""v0.2.0 F1: zero-copy high-concurrency Drive download (Muse-original).

Tests the preallocated single-.part download: os.truncate + lock-free
multi-threaded os.pwrite (peak disk <= 1.0x), 12 default workers with
env override, process-wide connection semaphore, ordered in-flight
streaming SHA-256 (out-of-order blocks read back via os.pread, never
buffered whole), atomic .part.bitmap.json resume, abort_event
cancellation, and single-flight FileTokenProvider.refresh.

All offline: a self-contained fake Drive transport stands in for the
network. No GPU, no Drive, no credentials.
"""

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import h3_drive_download as dd
from h3_drive_download import (
    DriveAuthError,
    DriveDownloadAbortedError,
    DriveDownloader,
    FileTokenProvider,
    TokenUnavailable,
    token_id_for,
)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeResp:
    """Minimal urlopen response: read(n) slices content, optionally
    sleeping per read so abort / in-flight timing is observable."""

    def __init__(self, status, content, headers=None, read_delay=0.0):
        self.status = status
        self._content = content
        self.headers = headers or {}
        self._off = 0
        self._read_delay = read_delay

    def read(self, n=-1):
        if self._read_delay:
            time.sleep(self._read_delay)
        if n is None or n < 0:
            chunk, self._off = self._content[self._off:], len(self._content)
            return chunk
        chunk = self._content[self._off:self._off + n]
        self._off += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDriveV2:
    """Scripted fake Drive server behind h3_drive_download.urlopen.

    - metadata: {file_id: {"size", "md5", "sha256"}}
    - media_content: {file_id: bytes}
    - media_plan: {file_id: [action]}; action ("resp", status, content,
      headers) or ("raise", exc); exhausted plans fall through to the
      ranged slice of media_content.
    - range_delay: {range-start: seconds} — sleeps before answering
      that range, to force out-of-order block completion.
    - read_delay: seconds slept on every FakeResp.read.
    - Tracks calls, per-range fetches, and max simultaneous urlopen
      entries (for the global connection-cap test).
    """

    def __init__(self):
        self.metadata = {}
        self.media_content = {}
        self.media_plan = {}
        self.range_delay = {}
        self.read_delay = 0.0
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, request, timeout=None):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            url = request.full_url
            headers = {k.lower(): v for k, v in
                       dict(getattr(request, "headers", {}) or {}).items()}
            self.calls.append((url, headers))
            if "fields=id" in url:
                file_id = url.split("/files/")[1].split("?")[0]
                meta = self.metadata[file_id]
                body = json.dumps({
                    "id": file_id, "size": str(meta["size"]),
                    "md5Checksum": meta.get("md5", ""),
                    "sha256Checksum": meta.get("sha256", ""),
                    "trashed": False}).encode()
                return FakeResp(200, body, {"Content-Type": "application/json"})
            if "alt=media" in url:
                file_id = url.split("/files/")[1].split("?")[0]
                plan = self.media_plan.get(file_id) or []
                if plan:
                    action = plan.pop(0)
                    if action[0] == "raise":
                        raise action[1]
                    _, status, content, resp_headers = action
                    return FakeResp(status, content, resp_headers,
                                    read_delay=self.read_delay)
                content = self.media_content[file_id]
                range_h = headers.get("range", "")
                start, end = 0, len(content) - 1
                if range_h.startswith("bytes="):
                    spec = range_h.split("=", 1)[1]
                    start_s, end_s = spec.split("-", 1)
                    start = int(start_s or 0)
                    end = int(end_s) if end_s else len(content) - 1
                    delay = self.range_delay.get(start, 0.0)
                    if delay:
                        time.sleep(delay)
                return FakeResp(206 if range_h else 200,
                                content[start:end + 1],
                                {"Content-Range": f"bytes {start}-{end}/{len(content)}"},
                                read_delay=self.read_delay)
            raise AssertionError("unexpected fake-drive URL: " + url)
        finally:
            with self._lock:
                self.in_flight -= 1

    def media_ranges(self, file_id):
        return [h.get("range") for u, h in self.calls
                if "alt=media" in u and file_id in u]


class FakeProvider:
    """Token provider stub: token A, refresh swaps to token B once."""

    def __init__(self):
        self._token = "SECRET-TOKEN-A"
        self.refresh_calls = 0

    @property
    def token_id(self):
        return token_id_for(self._token)

    def seconds_remaining(self):
        return 3600.0

    def get_token(self):
        return self._token

    def refresh(self, failed_token_id=None):
        self.refresh_calls += 1
        self._token = "SECRET-TOKEN-B"
        return self._token


class F1TestBase(unittest.TestCase):
    FILE_ID = "fid-f1"
    BIG_ID = "fid-f1-big"
    CONTENT = bytes(range(256)) * 16  # 4096 bytes, 4 blocks at 4 workers

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.td, ignore_errors=True))
        self.drive = FakeDriveV2()
        for fid, content in ((self.FILE_ID, b"0123456789"),
                             (self.BIG_ID, self.CONTENT)):
            self.drive.metadata[fid] = {
                "size": len(content),
                "md5": hashlib.md5(content).hexdigest(),
                "sha256": sha(content)}
            self.drive.media_content[fid] = content
        self.provider = FakeProvider()
        self.urlopen_patch = patch.object(dd, "urlopen", self.drive)
        self.urlopen_patch.start()
        self.addCleanup(self.urlopen_patch.stop)
        self.sleep_patch = patch.object(dd, "_sleep", lambda s: None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.min_patch = patch.object(dd, "CONCURRENT_MIN_SIZE", 1024)
        self.min_patch.start()
        self.addCleanup(self.min_patch.stop)

    def _dest(self, name="big.bin"):
        return Path(self.td) / name

    def _downloader(self, **kw):
        kw.setdefault("concurrent_workers", 4)
        kw.setdefault("backoff_base_sec", 0)
        return DriveDownloader(self.provider, **kw)

    def _download_big(self, **kw):
        return self._downloader(**kw).download_file(
            self.BIG_ID, len(self.CONTENT), sha(self.CONTENT),
            self._dest("big.bin"))

    def _part(self, name="big.bin"):
        return Path(str(self._dest(name)) + ".part")

    def _bitmap(self, name="big.bin"):
        return Path(str(self._dest(name)) + ".part.bitmap.json")


class PwriteBasicsTest(F1TestBase):
    def test_pwrite_no_shard_files_and_correct_bytes(self):
        """No .shard file is ever created; bytes + SHA-256 are exact."""
        seen_shards = []

        orig_call = self.drive.__call__

        def _watching(request, timeout=None):
            for entry in os.listdir(self.td):
                if ".shard" in entry:
                    seen_shards.append(entry)
            return orig_call(request, timeout)

        with patch.object(dd, "urlopen", _watching):
            out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertEqual(out["sha256"], sha(self.CONTENT))
        self.assertEqual(seen_shards, [])
        self.assertFalse(self._part().exists())
        self.assertFalse(self._bitmap().exists())

    def test_pwrite_uses_pwrite_not_sequential_write(self):
        """os.pwrite is the write path for concurrent blocks."""
        pwrites = []
        real_pwrite = os.pwrite

        def _recording(fd, data, offset):
            pwrites.append((len(data), offset))
            return real_pwrite(fd, data, offset)

        with patch.object(os, "pwrite", _recording):
            self._download_big()
        self.assertTrue(pwrites)
        # Four blocks at offsets 0/1024/2048/3072, each exactly 1024.
        self.assertEqual(sorted(o for _, o in pwrites), [0, 1024, 2048, 3072])
        self.assertTrue(all(n == 1024 for n, _ in pwrites))

    def test_pwrite_preallocated_part_full_size_during_download(self):
        """The .part file exists at the full size while blocks are
        still in flight; peak disk is the file itself, no extra copy."""
        self.drive.read_delay = 0.08
        seen_sizes = []
        stop = threading.Event()

        def _watch():
            while not stop.is_set():
                if self._part().exists():
                    seen_sizes.append(self._part().stat().st_size)
                    # Only .part and its bitmap may exist mid-download.
                    self.assertEqual(
                        sorted(os.listdir(self.td)),
                        sorted([p.name for p in
                                (self._part(), self._bitmap())
                                if p.exists()]))
                time.sleep(0.02)

        watcher = threading.Thread(target=_watch)
        watcher.start()
        try:
            out = self._download_big()
        finally:
            stop.set()
            watcher.join()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertIn(len(self.CONTENT), seen_sizes)


class WorkerConfigTest(unittest.TestCase):
    def test_default_workers_twelve_and_env_override(self):
        self.assertEqual(dd.DEFAULT_CONCURRENT_WORKERS, 12)
        provider = FakeProvider()
        self.assertEqual(DriveDownloader(provider).concurrent_workers, 12)
        with patch.dict(os.environ, {"H3_DRIVE_CONCURRENT_WORKERS": "3"}):
            self.assertEqual(DriveDownloader(provider).concurrent_workers, 3)
        with patch.dict(os.environ, {"H3_DRIVE_CONCURRENT_WORKERS": "99"}):
            self.assertEqual(DriveDownloader(provider).concurrent_workers, 16)
        with patch.dict(os.environ, {"H3_DRIVE_CONCURRENT_WORKERS": "abc"}):
            self.assertEqual(DriveDownloader(provider).concurrent_workers, 12)
        # Explicit arg wins over env.
        with patch.dict(os.environ, {"H3_DRIVE_CONCURRENT_WORKERS": "3"}):
            self.assertEqual(
                DriveDownloader(provider, concurrent_workers=5).concurrent_workers, 5)
        with self.assertRaises(ValueError):
            DriveDownloader(provider, concurrent_workers=0)

    def test_global_connection_cap(self):
        self.assertEqual(dd.DEFAULT_MAX_GLOBAL_CONNECTIONS, 16)


class GlobalCapTest(F1TestBase):
    def test_connection_semaphore_bounds_simultaneous_requests(self):
        """With a 2-permit semaphore, at most 2 urlopen calls are ever
        in flight at once, and the download still completes."""
        self.drive.read_delay = 0.05
        cap = threading.BoundedSemaphore(2)
        with patch.object(dd, "_CONNECTION_SEMAPHORE", cap):
            out = self._download_big(concurrent_workers=12)
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertLessEqual(self.drive.max_in_flight, 2)


class StreamingHashTest(F1TestBase):
    def test_out_of_order_completion_still_hashes_correctly(self):
        """Block 0 is slowest, so blocks 1-3 finish first; the ordered
        streaming hash still verifies without a second full read."""
        self.drive.range_delay = {0: 0.15}
        out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertEqual(out["sha256"], sha(self.CONTENT))

    def test_streaming_hash_replaces_second_full_read(self):
        """A successful concurrent download never re-reads the whole
        file through sha256_file: the streaming digest is the proof."""
        import h3_contract

        calls = []
        real = h3_contract.sha256_file

        def _counting(path):
            calls.append(str(path))
            return real(path)

        with patch.object(h3_contract, "sha256_file", _counting):
            out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertEqual(calls, [])


class BitmapResumeTest(F1TestBase):
    def _seed_part_with_blocks(self, done_indices):
        """Pre-create a full-size .part with correct bytes for the
        given blocks, plus a valid bitmap naming them done."""
        part = self._part()
        fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        try:
            os.ftruncate(fd, len(self.CONTENT))
            for i in done_indices:
                start = i * 1024
                os.pwrite(fd, self.CONTENT[start:start + 1024], start)
        finally:
            os.close(fd)
        dd._save_bitmap_atomic(part, len(self.CONTENT), sha(self.CONTENT),
                               4, set(done_indices))

    def test_bitmap_resume_fetches_only_missing_blocks(self):
        self._seed_part_with_blocks([0, 2])
        out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        ranges = sorted(self.drive.media_ranges(self.BIG_ID))
        self.assertEqual(ranges, ["bytes=1024-2047", "bytes=3072-4095"])
        self.assertFalse(self._bitmap().exists())

    def test_bitmap_wrong_sha_starts_fresh(self):
        self._seed_part_with_blocks([0, 2])
        # Corrupt the bitmap's pinned hash: it must be ignored.
        doc = json.loads(self._bitmap().read_text())
        doc["sha256"] = sha(b"X" * len(self.CONTENT))
        self._bitmap().write_text(json.dumps(doc))
        out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        ranges = sorted(self.drive.media_ranges(self.BIG_ID))
        self.assertEqual(
            ranges, ["bytes=0-1023", "bytes=1024-2047",
                     "bytes=2048-3071", "bytes=3072-4095"])

    def test_bitmap_wrong_workers_starts_fresh(self):
        self._seed_part_with_blocks([0, 2])
        doc = json.loads(self._bitmap().read_text())
        doc["workers"] = 2  # block boundaries would differ
        self._bitmap().write_text(json.dumps(doc))
        out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertEqual(len(self.drive.media_ranges(self.BIG_ID)), 4)


class AbortTest(F1TestBase):
    def test_abort_event_leaves_part_and_bitmap_then_resumes(self):
        """Abort mid-download: DriveDownloadAbortedError, part +
        bitmap survive, and a later run resumes to a correct file."""
        self.drive.read_delay = 0.08
        abort = threading.Event()

        def _fire():
            time.sleep(0.05)
            abort.set()

        timer = threading.Thread(target=_fire)
        timer.start()
        try:
            with self.assertRaises(DriveDownloadAbortedError):
                self._downloader(chunk_size=256).download_file(
                    self.BIG_ID, len(self.CONTENT), sha(self.CONTENT),
                    self._dest("big.bin"), abort_event=abort)
        finally:
            timer.join()
        self.assertTrue(self._part().exists())
        self.assertEqual(self._part().stat().st_size, len(self.CONTENT))
        self.assertTrue(self._bitmap().exists())
        self.assertFalse(self._dest("big.bin").exists())
        # Resume with a fresh event: succeeds, correct bytes.
        out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        self.assertFalse(self._bitmap().exists())

    def test_abort_before_start_raises_without_requests(self):
        abort = threading.Event()
        abort.set()
        with self.assertRaises(DriveDownloadAbortedError):
            self._downloader().download_file(
                self.BIG_ID, len(self.CONTENT), sha(self.CONTENT),
                self._dest("big.bin"), abort_event=abort)
        self.assertEqual(
            [u for u, _ in self.drive.calls if "alt=media" in u], [])


class SingleFlightRefreshTest(unittest.TestCase):
    def _write_token_file(self, path, token):
        doc = {"access_token": token,
               "expires_at_utc": "2030-01-01T00:00:00Z",
               "token_id": token_id_for(token)}
        Path(path).write_text(json.dumps(doc), encoding="utf-8")

    def test_concurrent_refresh_reads_token_file_once(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        token_path = Path(td) / "token.json"
        self._write_token_file(token_path, "TOKEN-ONE")
        provider = FileTokenProvider(token_path)
        provider.load_initial()
        old_id = provider.token_id
        self._write_token_file(token_path, "TOKEN-TWO")

        reads = []
        real_read = dd.read_token_file

        def _counting(path, delete=True):
            reads.append(str(path))
            return real_read(path, delete=delete)

        results = []
        barrier = threading.Barrier(8)

        def _refresh():
            barrier.wait(timeout=5)
            results.append(provider.refresh(failed_token_id=old_id))

        with patch.object(dd, "read_token_file", _counting):
            threads = [threading.Thread(target=_refresh) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual(len(reads), 1)
        self.assertEqual(results, ["TOKEN-TWO"] * 8)
        self.assertEqual(provider.token_id, token_id_for("TOKEN-TWO"))

    def test_refresh_with_stale_failed_id_returns_current_without_read(self):
        """Once a token has been refreshed past the failed one, a
        second refresh naming the old (failed) token id returns the
        current token without touching the token file again."""
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        token_path = Path(td) / "token.json"
        self._write_token_file(token_path, "TOKEN-ONE")
        provider = FileTokenProvider(token_path)
        provider.load_initial()
        old_id = provider.token_id
        self._write_token_file(token_path, "TOKEN-TWO")
        # First refresh: reads the fresh file, current becomes TWO.
        self.assertEqual(provider.refresh(failed_token_id=old_id), "TOKEN-TWO")
        # Second refresh naming the same failed (now stale) id: the
        # failed token is no longer current, so no file read happens.
        reads = []
        real_read = dd.read_token_file

        def _counting(path, delete=True):
            reads.append(str(path))
            return real_read(path, delete=delete)

        with patch.object(dd, "read_token_file", _counting):
            self.assertEqual(
                provider.refresh(failed_token_id=old_id), "TOKEN-TWO")
        self.assertEqual(reads, [])
        self.assertEqual(provider.token_id, token_id_for("TOKEN-TWO"))

    def test_refresh_without_fresh_file_raises(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        token_path = Path(td) / "token.json"
        self._write_token_file(token_path, "TOKEN-ONE")
        provider = FileTokenProvider(token_path)
        provider.load_initial()
        with self.assertRaises(TokenUnavailable):
            provider.refresh()


class FallbackTest(F1TestBase):
    def test_range_ignored_falls_back_and_cleans_part(self):
        """Server answers 200 to Range: concurrent attempt aborts,
        single stream finishes, no .part/.bitmap left behind."""
        self.drive.media_plan[self.BIG_ID] = [
            ("resp", 200, self.CONTENT, {})] * 12
        out = self._download_big()
        self.assertEqual(Path(out["path"]).read_bytes(), self.CONTENT)
        ranges = self.drive.media_ranges(self.BIG_ID)
        self.assertTrue(any(r and r.startswith("bytes=") for r in ranges))
        self.assertIn(None, ranges)  # fallback single-stream request
        self.assertFalse(self._part().exists())
        self.assertFalse(self._bitmap().exists())


if __name__ == "__main__":
    unittest.main()

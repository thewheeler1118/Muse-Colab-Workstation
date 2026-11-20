"""Noninteractive Google Drive download for the H3 production path.

TASK-006 P0-B (Issue #44): the paid path must not depend on interactive
``colab drivemount``. This module downloads pinned model files (and the
ComfyUI source archive) from Drive with a short-lived, read-only access
token, using only the standard library so the remote Colab worker can
import it with no extra dependency.

Security rules (hard):
- This module never holds, mints, or stores a refresh token, ADC, or
  any long-lived credential. It only ever uses an access token handed
  to it through a token provider.
- A token value is NEVER written to a log, manifest, audit record,
  exception message, or return value. Only ``token_id`` (the first 16
  hex chars of SHA-256(token)) may appear in diagnostics; it is a
  non-sensitive identifier for correlating mint/renewal events.
- A token file is read and then deleted immediately by the reader
  (``read_token_file``), except for the explicit ``delete=False`` probe
  path, which is documented at its call site.
- Tokens are never passed on the command line (``/proc/<pid>/cmdline``
  is world-readable on the VM).

Download rules:
- Pinned identity: Drive file id + expected byte count + expected
  SHA-256 (and MD5 when the server reports one). The server metadata
  is re-checked before AND after the transfer; a file that changed
  mid-transfer fails instead of being silently accepted.
- HTTP Range resume with a ``.part`` file, atomic replace on success,
  full SHA-256 verification before the file is ever handed to a model
  loader. A ``.part`` file is never a verified model.
- Error classification: 429/5xx are transient (bounded backoff);
  401 triggers exactly one token refresh, then fails closed;
  403 fails closed immediately; other 4xx fail immediately.

v0.2.0 F1 (TASK-VERSION-ROADMAP-011, Muse-original reimplementation
of the upstream v0.2.0 behaviour spec — no upstream code copied):
large fresh downloads no longer shard into ``.part.shardN`` temp
files plus a merge. Instead one ``.part`` file is preallocated with
``os.truncate`` and filled by lock-free multi-threaded ``os.pwrite``
(peak disk ``<= 1.0x`` the file size). Completion is tracked per
worker-range block in an atomically written
``.part.bitmap.json`` so an interrupted download resumes only its
missing blocks. Integrity is checked in flight by an ordered
streaming SHA-256: blocks hash in index order, and a block that
finishes out of order is read back from disk with ``os.pread`` when
its turn comes instead of being buffered whole in memory. An
``abort_event`` cancels the transfer
(``DriveDownloadAbortedError``) leaving ``.part`` + bitmap for
resume, and token refresh is single-flight across threads.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Rebindable for tests: the fake transport patches h3_drive_download.urlopen.
urlopen = urllib.request.urlopen

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Token lifetime margin: ask for a renewal while this much life remains.
TOKEN_REFRESH_MARGIN_SEC = 600
# Hard cap on renewals per run; the controller enforces the same cap.
MAX_TOKEN_RENEWALS = 6

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB streaming writes (TASK-007)
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_SEC = 2.0
DEFAULT_TIMEOUT_SEC = 300
# Files at or above this size get a concurrent download when starting
# fresh (TASK-007): a 40 GiB restore over one TCP stream is the single
# biggest cold-start risk against a ~60-minute session budget. Below
# it the single stream is simpler and fast enough.
CONCURRENT_MIN_SIZE = 256 * 1024 * 1024


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Read an int env override, clamped to [lo, hi]; default on any
    bad value. Used for the F1 worker / connection caps so operators
    can tune without a code change."""
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


# v0.2.0 F1: 12 workers by default (was 4), 1..16 via
# H3_DRIVE_CONCURRENT_WORKERS. The effective per-downloader count is
# resolved in DriveDownloader.__init__ (explicit arg wins, else env,
# else this default) so tests can pin either without reloading.
DEFAULT_CONCURRENT_WORKERS = 12
# v0.2.0 F1: process-wide cap on simultaneous Drive connections, 16
# by default via H3_DRIVE_MAX_GLOBAL_CONNECTIONS (1..64). The
# semaphore bounds connection establishment across every
# DriveDownloader in this process; each HTTP call holds one permit
# only while urlopen() runs.
DEFAULT_MAX_GLOBAL_CONNECTIONS = 16
_CONNECTION_SEMAPHORE = threading.BoundedSemaphore(
    _env_int("H3_DRIVE_MAX_GLOBAL_CONNECTIONS",
             DEFAULT_MAX_GLOBAL_CONNECTIONS, 1, 64))


@contextlib.contextmanager
def _drive_connection():
    """Hold one process-wide Drive connection permit. Reads the
    module-global semaphore at call time so tests can swap it."""
    _CONNECTION_SEMAPHORE.acquire()
    try:
        yield
    finally:
        _CONNECTION_SEMAPHORE.release()


def _urlopen(req, timeout):
    """urlopen behind the global connection semaphore. Tests patch
    the module-global ``urlopen``; this helper resolves it at call
    time, so the patch still applies."""
    with _drive_connection():
        return urlopen(req, timeout=timeout)


def resolve_concurrent_workers(explicit: int | None = None) -> int:
    """Effective concurrent-worker count: explicit arg wins, else the
    H3_DRIVE_CONCURRENT_WORKERS env override (1..16), else the
    default. Raises ValueError on an explicit value below 1."""
    if explicit is not None:
        if explicit < 1:
            raise ValueError("concurrent_workers must be >= 1")
        return explicit
    return _env_int("H3_DRIVE_CONCURRENT_WORKERS",
                    DEFAULT_CONCURRENT_WORKERS, 1, 16)


# A slow-but-alive download is not killed; the read timeout only bounds
# a single stalled read inside urlopen.
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


class DriveDownloadError(RuntimeError):
    """Base failure for a Drive download. Never carries a token value."""


class DriveAuthError(DriveDownloadError):
    """401 after a refresh, or 403: fail closed, never retry the token."""


class DriveChangedError(DriveDownloadError):
    """Server metadata changed between the pre/post checks: the remote
    file is not the pinned file any more. Never accept the bytes."""


class DriveRetryExhaustedError(DriveDownloadError):
    """Transient failures outlived the bounded retry budget."""


class DriveDownloadAbortedError(DriveDownloadError):
    """An external ``abort_event`` cancelled the transfer. The
    ``.part`` file (and its bitmap, for concurrent downloads) is left
    in place so the next attempt resumes instead of restarting."""


class _NoRangeSupport(Exception):
    """Internal: a ranged request was answered 200, so the server does
    not honour Range for this file. Signals the caller to fall back
    to the single-stream path (never an error by itself)."""


class _ProgressAborted(Exception):
    """Internal: a block stopped early because the progress callback
    raised (e.g. the worker's budget projection). The recorded
    callback error is what surfaces, never this marker."""


class TokenUnavailable(RuntimeError):
    """The token provider has no usable access token right now."""


def token_id_for(token: str) -> str:
    """Non-sensitive identifier for a token value (audit correlation
    only). The id cannot be used to reconstruct or use the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _parse_utc(value: str) -> float:
    """Parse an ISO-8601 UTC timestamp to epoch seconds."""
    from datetime import datetime, timezone

    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()


def validate_token_doc(doc: dict) -> dict:
    """Shape-check a token document. Returns the doc unchanged.
    Raises ValueError on a bad shape. Never logs the token value."""
    if not isinstance(doc, dict):
        raise ValueError("token document is not an object")
    token = doc.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("token document has no access_token")
    expires = doc.get("expires_at_utc")
    if not isinstance(expires, str) or not expires:
        raise ValueError("token document has no expires_at_utc")
    _parse_utc(expires)  # raises ValueError on a bad timestamp
    token_id = doc.get("token_id")
    if not isinstance(token_id, str) or not token_id:
        raise ValueError("token document has no token_id")
    if token_id != token_id_for(token):
        raise ValueError("token document token_id does not match its token")
    return doc


def read_token_file(path, delete: bool = True) -> dict:
    """Read a 0600 token file and delete it immediately after a
    successful read (default). The caller owns the lifetime decision:
    the download probe passes delete=False so the worker's own token
    file survives the probe; the worker always reads with delete=True.
    Raises FileNotFoundError when no token file is present (the
    controller has not delivered one yet) and ValueError on a bad
    shape. Never logs the token value."""
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate_token_doc(doc)
    if delete:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return doc


class FileTokenProvider:
    """Thread-safe access-token holder for the remote worker.

    The token is loaded from a 0600 token file delivered by the
    controller (``colab upload``). ``get_token()`` returns the current
    token while it has more than ``margin_sec`` of life left; when it
    is about to expire it tries to pick up a freshly delivered token
    file at the same path (the controller uploads one after the worker
    requests renewal through its job state). ``refresh()`` forces a
    re-read of the token file (used exactly once after a 401).
    The provider never logs a token value.
    """

    def __init__(self, token_path, margin_sec: int = TOKEN_REFRESH_MARGIN_SEC):
        self.token_path = Path(token_path)
        self.margin_sec = margin_sec
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None
        self._token_id: str | None = None
        # v0.2.0 F1 single-flight refresh: while one thread is
        # re-reading the token file, other threads wait on this event
        # instead of starting a second refresh. None when idle.
        self._refreshing: threading.Event | None = None

    @property
    def token_id(self) -> str | None:
        with self._lock:
            return self._token_id

    def _load_file_locked(self) -> bool:
        try:
            doc = read_token_file(self.token_path, delete=True)
        except FileNotFoundError:
            return False
        self._token = doc["access_token"]
        self._expires_at = _parse_utc(doc["expires_at_utc"])
        self._token_id = doc["token_id"]
        return True

    def load_initial(self) -> dict:
        """Load the first token file. Raises TokenUnavailable when no
        token file has been delivered yet."""
        with self._lock:
            if not self._load_file_locked():
                raise TokenUnavailable(
                    "no Drive token file delivered at " + str(self.token_path))
            return {"token_id": self._token_id, "expires_at": self._expires_at}

    def get_token(self) -> str:
        """Current usable token, or raise TokenUnavailable. Picks up a
        freshly delivered token file when the current one is inside the
        renewal margin."""
        with self._lock:
            now = time.time()
            if self._token is not None and self._expires_at is not None:
                if self._expires_at - now > self.margin_sec:
                    return self._token
            # Inside the margin (or no token yet): try a fresh file.
            if self._load_file_locked():
                return self._token
            if self._token is not None and self._expires_at is not None \
                    and self._expires_at > now:
                # No fresh file, but the old token is still alive: use it
                # once more rather than failing the in-flight chunk.
                return self._token
            raise TokenUnavailable("no usable Drive access token")

    def refresh(self, failed_token_id: str | None = None) -> str:
        """Force a re-read of the token file (after a 401). Raises
        TokenUnavailable when no fresh token file is present.

        v0.2.0 F1 single-flight: when several download threads hit
        401 at once, only the first re-reads the file; the rest wait
        for that refresh to finish and then use its token. When
        ``failed_token_id`` is given and it no longer matches the
        current token, another thread already refreshed past the
        failure, so this call returns the current token without
        touching the file again. The wait is bounded (30 s) and never
        holds ``self._lock``."""
        with self._lock:
            if failed_token_id is not None and self._token_id is not None \
                    and failed_token_id != self._token_id:
                return self._token
            if self._refreshing is None:
                event = threading.Event()
                self._refreshing = event
                refresher = True
            else:
                event = self._refreshing
                refresher = False
        if not refresher:
            event.wait(timeout=30)
            with self._lock:
                if self._token is not None:
                    return self._token
                raise TokenUnavailable(
                    "no fresh Drive token file for refresh")
        try:
            with self._lock:
                if not self._load_file_locked():
                    raise TokenUnavailable(
                        "no fresh Drive token file for refresh")
                return self._token
        finally:
            with self._lock:
                self._refreshing = None
            event.set()

    def seconds_remaining(self) -> float | None:
        with self._lock:
            if self._expires_at is None:
                return None
            return self._expires_at - time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _backoff_delay(attempt: int, base: float) -> float:
    # Exponential backoff with jitter; the attempt number is 1-based.
    return base * (2 ** (attempt - 1)) + random.uniform(0, 1.0)


# -- v0.2.0 F1: bitmap resume + ordered streaming hash (module level) --

BITMAP_VERSION = 1


def _bitmap_path(part: Path) -> Path:
    return Path(str(part) + ".bitmap.json")


def _discard_bitmap(part: Path) -> None:
    try:
        _bitmap_path(part).unlink(missing_ok=True)
    except OSError:
        pass


def _save_bitmap_atomic(part: Path, expected_size: int,
                        expected_sha256: str, workers: int,
                        done: set[int]) -> None:
    """Atomically record which worker-range blocks are complete.
    Written (temp file + os.replace) after every block completes, so
    a crash never leaves a torn bitmap behind."""
    doc = {"version": BITMAP_VERSION, "size": expected_size,
           "sha256": expected_sha256.lower(), "workers": workers,
           "done": sorted(done)}
    tmp = Path(str(_bitmap_path(part)) + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, _bitmap_path(part))


def _load_bitmap(part: Path, expected_size: int, expected_sha256: str,
                 workers: int) -> set[int] | None:
    """Load a valid resume bitmap, or None. Validates size, pinned
    SHA-256, worker count (block boundaries depend on it) and that
    every recorded block index exists; never raises on bad content —
    a corrupt or mismatched bitmap simply means 'start fresh'."""
    try:
        if not part.is_file() or part.stat().st_size != expected_size:
            return None
        doc = json.loads(_bitmap_path(part).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("version") != BITMAP_VERSION:
        return None
    if doc.get("size") != expected_size:
        return None
    if str(doc.get("sha256", "")).lower() != expected_sha256.lower():
        return None
    if doc.get("workers") != workers:
        return None
    done = doc.get("done")
    if not isinstance(done, list):
        return None
    if any(not isinstance(i, int) for i in done):
        return None
    block_count = (expected_size + workers - 1) // workers
    if any(i < 0 or i >= block_count for i in done):
        return None
    return set(done)


class _OrderedBlockHasher:
    """Ordered in-flight SHA-256 over downloaded blocks, bounded
    memory: this class never holds block bytes. Blocks are pwritten
    to disk as they arrive; when a block completes out of order, its
    bytes are read back from disk with ``os.pread`` when its turn
    comes (spillback) instead of being buffered whole."""

    def __init__(self, part_fd: int, ranges, chunk_size: int):
        self._fd = part_fd
        self._ranges = list(ranges)
        self._chunk_size = chunk_size
        self._hasher = hashlib.sha256()
        self._next = 0
        self._done: set[int] = set()
        self._lock = threading.Lock()

    def block_completed(self, index: int) -> None:
        """Record that ``index`` is fully on disk, then hash every
        contiguous completed block starting at the next unhashed one."""
        with self._lock:
            self._done.add(index)
            while self._next in self._done:
                start, end = self._ranges[self._next]
                remaining = end - start + 1
                offset = start
                while remaining > 0:
                    step = min(self._chunk_size, remaining)
                    data = os.pread(self._fd, step, offset)
                    if not data:
                        raise DriveDownloadError(
                            "short pread while hashing block "
                            f"{self._next}")
                    self._hasher.update(data)
                    offset += len(data)
                    remaining -= len(data)
                self._done.discard(self._next)
                self._next += 1

    def hexdigest(self) -> str:
        with self._lock:
            if self._next != len(self._ranges):
                raise DriveDownloadError(
                    "streaming hash incomplete: hashed "
                    f"{self._next} of {len(self._ranges)} blocks")
            return self._hasher.hexdigest()


class DriveDownloader:
    """Range-resuming Drive file downloader over a token provider.

    ``token_provider`` must expose ``get_token() -> str`` and
    ``refresh() -> str`` (see FileTokenProvider). ``on_progress`` is
    called as ``on_progress(bytes_done, total)`` after each chunk; the
    worker uses it to request a token renewal while life remains.
    ``on_token_expiring`` is called as ``on_token_expiring(token_id,
    seconds_remaining)`` when the provider reports less than the
    renewal margin during a download.
    ``concurrent_workers`` bounds the concurrent download of large
    fresh files (``>= CONCURRENT_MIN_SIZE``); it is separate from
    ``max_workers``, which bounds multi-file ``download_all``.
    """

    def __init__(self, token_provider, chunk_size: int = DEFAULT_CHUNK_SIZE,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 backoff_base_sec: float = DEFAULT_BACKOFF_BASE_SEC,
                 max_workers: int = 1,
                 concurrent_workers: int | None = None,
                 timeout_sec: int = DEFAULT_TIMEOUT_SEC):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.token_provider = token_provider
        self.chunk_size = chunk_size
        self.max_attempts = max_attempts
        self.backoff_base_sec = backoff_base_sec
        self.max_workers = max_workers
        # v0.2.0 F1: None means env override (H3_DRIVE_CONCURRENT_WORKERS)
        # else the 12-worker default.
        self.concurrent_workers = resolve_concurrent_workers(concurrent_workers)
        self.timeout_sec = timeout_sec

    # -- HTTP plumbing (stdlib only; urlopen is module-level for tests) --

    def _request(self, url: str, headers: dict | None = None,
                 method: str = "GET"):
        token = self.token_provider.get_token()
        req_headers = {"Authorization": "Bearer " + token}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers, method=method)
        return _urlopen(req, self.timeout_sec)

    def get_metadata(self, file_id: str) -> dict:
        """Server-side metadata for one Drive file. Raises
        DriveDownloadError on any non-200 answer; a 404 may mean
        permissions rather than absence."""
        url = (f"{DRIVE_API_BASE}/files/{file_id}"
               "?fields=id,name,size,md5Checksum,sha256Checksum,trashed"
               "&supportsAllDrives=true")
        try:
            with self._request(url) as resp:
                status = getattr(resp, "status", 200)
                if status != 200:
                    raise DriveDownloadError(
                        f"Drive metadata HTTP {status} for file {file_id}")
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise DriveDownloadError(
                f"Drive metadata HTTP {e.code} for file {file_id}") from e
        except urllib.error.URLError as e:
            raise DriveDownloadError(
                f"Drive metadata transport error for file {file_id}: "
                f"{type(e).__name__}") from e

    def _check_metadata(self, meta: dict, file_id: str, expected_size: int,
                        expected_sha256: str, expected_md5: str | None) -> None:
        if meta.get("trashed"):
            raise DriveChangedError(f"Drive file is trashed: {file_id}")
        size = meta.get("size")
        if size is None or int(size) != int(expected_size):
            raise DriveChangedError(
                f"Drive file size mismatch for {file_id}: server "
                f"{size!r} != pinned {expected_size}")
        server_sha = (meta.get("sha256Checksum") or "").lower()
        if server_sha and server_sha != expected_sha256.lower():
            raise DriveChangedError(
                f"Drive server SHA-256 mismatch for {file_id}")
        if expected_md5:
            server_md5 = (meta.get("md5Checksum") or "").lower()
            if server_md5 and server_md5 != expected_md5.lower():
                raise DriveChangedError(
                    f"Drive server MD5 mismatch for {file_id}")

    # -- single file --

    def download_file(self, file_id: str, expected_size: int,
                      expected_sha256: str, dest,
                      expected_md5: str | None = None,
                      on_progress=None, on_token_expiring=None,
                      abort_event: threading.Event | None = None) -> dict:
        """Download one pinned Drive file to ``dest``.

        Returns ``{"drive_file_id", "size", "sha256", "path"}``. The
        destination is atomically replaced only after the full SHA-256
        verifies; a ``.part`` file is left behind on failure for resume.
        Raises DriveDownloadError (or a subclass) on any failure; the
        exception message never contains a token value. When
        ``abort_event`` is set the transfer raises
        DriveDownloadAbortedError and leaves ``.part`` (+ bitmap) for
        resume instead of deleting them.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "", re.I):
            raise ValueError("Pinned SHA-256 required for " + str(file_id))
        if not expected_size or int(expected_size) <= 0:
            raise ValueError("Pinned byte count required for " + str(file_id))
        expected_size = int(expected_size)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(dest) + ".part")

        # Pre-check: the server must agree this is the pinned file.
        meta = self.get_metadata(file_id)
        self._check_metadata(meta, file_id, expected_size, expected_sha256,
                             expected_md5)

        # Reuse a complete, already-verified local copy.
        if dest.is_file() and dest.stat().st_size == expected_size:
            from h3_contract import sha256_file

            if sha256_file(dest) == expected_sha256.lower():
                return {"drive_file_id": file_id, "size": expected_size,
                        "sha256": expected_sha256.lower(), "path": str(dest)}
            dest.unlink()  # stale or corrupt local copy; never trust it

        # v0.2.0 F1 bitmap resume: a preallocated .part with a valid
        # bitmap resumes only its missing blocks (concurrent path).
        # Without a valid bitmap the old prefix rules apply below.
        bitmap = _load_bitmap(part, expected_size, expected_sha256,
                              self.concurrent_workers)
        streaming_sha256: str | None = None
        if bitmap is not None:
            concurrent = self._download_concurrent(
                file_id, expected_size, expected_sha256, part, on_progress,
                on_token_expiring, abort_event, resume_bitmap=bitmap)
            if concurrent is not False:
                streaming_sha256 = concurrent["streaming_sha256"]
                start = expected_size
            else:
                # Fallback: the concurrent attempt cleaned its part +
                # bitmap, so the single stream below starts fresh.
                start = 0
        else:
            start = part.stat().st_size if part.is_file() else 0
            if start > expected_size:
                part.unlink()
                _discard_bitmap(part)
                start = 0
            if start == expected_size:
                from h3_contract import sha256_file

                if sha256_file(part) == expected_sha256.lower():
                    os.replace(part, dest)
                    _discard_bitmap(part)
                    return {"drive_file_id": file_id, "size": expected_size,
                            "sha256": expected_sha256.lower(),
                            "path": str(dest)}
                part.unlink()
                _discard_bitmap(part)
                start = 0

        last_error: Exception | None = None
        refreshed_once = False
        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        # v0.2.0 F1: large fresh downloads go concurrent (pwrite) first.
        # The concurrent path returns its streaming SHA-256 on success;
        # the standard final verification below still runs the
        # post-metadata check + atomic replace. False means "fall back
        # to the single stream".
        if start == 0 and expected_size >= CONCURRENT_MIN_SIZE \
                and self.concurrent_workers > 1:
            concurrent = self._download_concurrent(
                file_id, expected_size, expected_sha256, part, on_progress,
                on_token_expiring, abort_event)
            if concurrent is not False:
                streaming_sha256 = concurrent["streaming_sha256"]
                start = expected_size
        for attempt in range(1, self.max_attempts + 1):
            if start >= expected_size:
                # Concurrent path already merged the whole file into
                # .part; skip straight to final verification.
                break
            # Token-expiry signal: let the caller request a renewal
            # while the current token still has life left.
            if on_token_expiring is not None:
                remaining = None
                seconds_remaining = getattr(
                    self.token_provider, "seconds_remaining", None)
                if callable(seconds_remaining):
                    remaining = seconds_remaining()
                if remaining is not None and remaining < TOKEN_REFRESH_MARGIN_SEC:
                    token_id = getattr(self.token_provider, "token_id", None)
                    on_token_expiring(token_id, remaining)
            headers = {}
            if start > 0:
                headers["Range"] = f"bytes={start}-"
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                # _request adds Authorization; build the authorised
                # request directly so Range headers are preserved.
                token = self.token_provider.get_token()
                req.add_header("Authorization", "Bearer " + token)
                with _urlopen(req, self.timeout_sec) as resp:
                    status = getattr(resp, "status", 200)
                    if status == 401:
                        if not refreshed_once:
                            refreshed_once = True
                            try:
                                self.token_provider.refresh()
                            except TokenUnavailable as e:
                                raise DriveAuthError(
                                    "Drive download 401 and no fresh "
                                    f"token available for {file_id}") from e
                            continue
                        raise DriveAuthError(
                            "Drive download 401 after token refresh for "
                            + str(file_id))
                    if status == 403:
                        raise DriveAuthError(
                            "Drive download 403 (revoked or insufficient "
                            f"permission) for {file_id}")
                    if status == 416:
                        # Range unsatisfiable: the part may already be
                        # complete; verify before deciding.
                        from h3_contract import sha256_file

                        if part.is_file() and part.stat().st_size == expected_size \
                                and sha256_file(part) == expected_sha256.lower():
                            os.replace(part, dest)
                            return {"drive_file_id": file_id,
                                    "size": expected_size,
                                    "sha256": expected_sha256.lower(),
                                    "path": str(dest)}
                        part.unlink(missing_ok=True)
                        start = 0
                        last_error = DriveDownloadError(
                            "HTTP 416 with incomplete part for " + str(file_id))
                        continue
                    if status == 200 and start > 0:
                        # Server ignored Range; restart from zero, do
                        # not append to a stale prefix.
                        part.unlink(missing_ok=True)
                        start = 0
                        last_error = DriveDownloadError(
                            "Server ignored Range for " + str(file_id))
                        continue
                    if status not in (200, 206):
                        if status in TRANSIENT_STATUSES:
                            last_error = DriveDownloadError(
                                f"Drive download HTTP {status} for {file_id}")
                            if attempt < self.max_attempts:
                                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                                start = part.stat().st_size if part.is_file() else 0
                                continue
                            raise DriveRetryExhaustedError(
                                "Drive download retries exhausted for "
                                f"{file_id}: {last_error}") from last_error
                        raise DriveDownloadError(
                            f"Drive download HTTP {status} for {file_id}")
                    if status == 206:
                        cr = resp.headers.get("Content-Range", "")
                        m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr or "")
                        if not m or int(m.group(1)) != start \
                                or int(m.group(3)) != expected_size:
                            raise DriveChangedError(
                                "Unexpected Content-Range for " + str(file_id))
                    mode = "ab" if start > 0 else "wb"
                    written = start
                    with part.open(mode) as f:
                        while True:
                            if abort_event is not None and abort_event.is_set():
                                raise DriveDownloadAbortedError(
                                    "Drive download aborted for "
                                    + str(file_id))
                            chunk = resp.read(self.chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            written += len(chunk)
                            if written > expected_size:
                                raise DriveDownloadError(
                                    "Drive download overran pinned size for "
                                    + str(file_id))
                            if on_progress is not None:
                                on_progress(written, expected_size)
                    start = written
                    if written == expected_size:
                        break
                    last_error = DriveDownloadError(
                        "Drive download short read for " + str(file_id))
            except (DriveDownloadError, DriveAuthError, DriveChangedError,
                    ValueError):
                raise
            except urllib.error.HTTPError as e:
                # Raised by urlopen itself (not by our status branch)
                # when the server answers an error status.
                if e.code == 401 and not refreshed_once:
                    refreshed_once = True
                    try:
                        self.token_provider.refresh()
                    except TokenUnavailable as exc:
                        raise DriveAuthError(
                            "Drive download 401 and no fresh token "
                            f"available for {file_id}") from exc
                    continue
                if e.code == 401:
                    raise DriveAuthError(
                        "Drive download 401 after token refresh for "
                        + str(file_id)) from e
                if e.code == 403:
                    raise DriveAuthError(
                        "Drive download 403 (revoked or insufficient "
                        f"permission) for {file_id}") from e
                if e.code in TRANSIENT_STATUSES:
                    last_error = DriveDownloadError(
                        f"Drive download HTTP {e.code} for {file_id}")
                    if attempt < self.max_attempts:
                        _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                        start = part.stat().st_size if part.is_file() else 0
                        continue
                    raise DriveRetryExhaustedError(
                        "Drive download retries exhausted for "
                        f"{file_id}: {last_error}") from e
                raise DriveDownloadError(
                    f"Drive download HTTP {e.code} for {file_id}") from e
            except Exception as e:  # transport errors are transient
                last_error = e
            if attempt < self.max_attempts:
                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                start = part.stat().st_size if part.is_file() else 0

        # Final verification: size + full SHA-256, then atomic replace.
        # The part is only promoted when it proves to be the pinned file.
        # A concurrent download already proved the bytes with its
        # ordered streaming SHA-256 (same bytes, same hash, no second
        # full read); otherwise the part is hashed here.
        from h3_contract import sha256_file

        verified = False
        if streaming_sha256 is not None:
            verified = streaming_sha256 == expected_sha256.lower()
        elif part.is_file() and part.stat().st_size == expected_size:
            verified = sha256_file(part) == expected_sha256.lower()
        if part.is_file() and part.stat().st_size == expected_size and verified:
            # Post-check: the server file must still be the pinned one;
            # a file that changed mid-transfer is refused even if the
            # downloaded bytes happen to hash correctly.
            post = self.get_metadata(file_id)
            self._check_metadata(post, file_id, expected_size,
                                 expected_sha256, expected_md5)
            os.replace(part, dest)
            _discard_bitmap(part)
            return {"drive_file_id": file_id, "size": expected_size,
                    "sha256": expected_sha256.lower(), "path": str(dest)}
        if part.is_file():
            part.unlink(missing_ok=True)
        _discard_bitmap(part)
        detail = (str(last_error)[:120] if last_error else "unknown")
        raise DriveRetryExhaustedError(
            "Drive download failed checksum/size for " + str(file_id)
            + ": " + detail)

    # -- concurrent download, v0.2.0 F1: preallocated pwrite, no shards --

    def _block_ranges(self, expected_size: int) -> list:
        """Split the file into ``concurrent_workers`` contiguous ranges
        (one block per worker). The last block may be shorter."""
        n = self.concurrent_workers
        block_size = (expected_size + n - 1) // n
        ranges = []
        for i in range(n):
            start = i * block_size
            end = min((i + 1) * block_size, expected_size) - 1
            ranges.append((start, end))
        return ranges

    def _download_block(self, url: str, file_id: str, index: int,
                        start: int, end: int, expected_size: int,
                        part_fd: int, report, abort: threading.Event,
                        external_abort: threading.Event | None) -> int:
        """Download one byte range ``[start, end]`` with ``os.pwrite``
        at its offset into the preallocated ``.part`` file.

        Each attempt rewrites the block from scratch (the previous
        attempt's bytes are overwritten in place, never appended).
        Returns ``index`` on success. Raises ``_NoRangeSupport`` when
        the server answers 200 (Range ignored), ``DriveAuthError`` on
        403 (fail closed, never falls back), ``DriveRetryExhaustedError``
        when retries run out, ``_ProgressAborted`` when the shared
        abort flag is set (the progress callback raised; the recorded
        callback error is what surfaces), and
        ``DriveDownloadAbortedError`` when the caller's abort event
        fires. Token refresh stays inside the thread-safe provider
        and is single-flight (see FileTokenProvider.refresh).
        """
        length = end - start + 1
        last_error: Exception | None = None
        refreshed_once = False
        for attempt in range(1, self.max_attempts + 1):
            if abort.is_set():
                raise _ProgressAborted()
            if external_abort is not None and external_abort.is_set():
                raise DriveDownloadAbortedError(
                    "Drive download aborted for " + str(file_id))
            try:
                token = self.token_provider.get_token()
                failed_id = token_id_for(token)
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer " + token,
                                  "Range": f"bytes={start}-{end}"},
                    method="GET")
                with _urlopen(req, self.timeout_sec) as resp:
                    status = getattr(resp, "status", 200)
                    if status == 200:
                        raise _NoRangeSupport(
                            "server answered 200 to a Range request for "
                            + str(file_id))
                    if status == 401:
                        if not refreshed_once:
                            refreshed_once = True
                            try:
                                self.token_provider.refresh(
                                    failed_token_id=failed_id)
                            except TokenUnavailable as e:
                                raise DriveAuthError(
                                    "Drive download 401 and no fresh "
                                    f"token available for {file_id}") from e
                            continue
                        raise DriveAuthError(
                            "Drive download 401 after token refresh for "
                            + str(file_id))
                    if status == 403:
                        raise DriveAuthError(
                            "Drive download 403 (revoked or insufficient "
                            f"permission) for {file_id}")
                    if status != 206:
                        if status in TRANSIENT_STATUSES:
                            last_error = DriveDownloadError(
                                f"Drive download HTTP {status} for {file_id}")
                            if attempt < self.max_attempts:
                                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                                continue
                            raise DriveRetryExhaustedError(
                                "Drive download retries exhausted for "
                                f"{file_id}: {last_error}") from last_error
                        raise DriveDownloadError(
                            f"Drive download HTTP {status} for {file_id}")
                    cr = resp.headers.get("Content-Range", "")
                    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr or "")
                    if not m or int(m.group(1)) != start \
                            or int(m.group(2)) != end \
                            or int(m.group(3)) != expected_size:
                        raise DriveChangedError(
                            "Unexpected Content-Range for " + str(file_id))
                    written = 0
                    while True:
                        if abort.is_set():
                            raise _ProgressAborted()
                        if external_abort is not None \
                                and external_abort.is_set():
                            raise DriveDownloadAbortedError(
                                "Drive download aborted for "
                                + str(file_id))
                        chunk = resp.read(self.chunk_size)
                        if not chunk:
                            break
                        # Lock-free: distinct offsets never overlap.
                        os.pwrite(part_fd, chunk, start + written)
                        written += len(chunk)
                        if written > length:
                            raise DriveDownloadError(
                                "Drive block overran its range for "
                                + str(file_id))
                        report(len(chunk))
                    if written == length:
                        return index
                    last_error = DriveDownloadError(
                        "Drive block short read for " + str(file_id))
            except (DriveAuthError, DriveChangedError, _NoRangeSupport,
                    _ProgressAborted, DriveDownloadAbortedError):
                raise
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed_once:
                    refreshed_once = True
                    try:
                        self.token_provider.refresh()
                    except TokenUnavailable as exc:
                        raise DriveAuthError(
                            "Drive download 401 and no fresh token "
                            f"available for {file_id}") from exc
                    continue
                if e.code == 401:
                    raise DriveAuthError(
                        "Drive download 401 after token refresh for "
                        + str(file_id)) from e
                if e.code == 403:
                    raise DriveAuthError(
                        "Drive download 403 (revoked or insufficient "
                        f"permission) for {file_id}") from e
                if e.code in TRANSIENT_STATUSES:
                    last_error = DriveDownloadError(
                        f"Drive download HTTP {e.code} for {file_id}")
                    if attempt < self.max_attempts:
                        _sleep(_backoff_delay(attempt, self.backoff_base_sec))
                        continue
                    raise DriveRetryExhaustedError(
                        "Drive download retries exhausted for "
                        f"{file_id}: {last_error}") from e
                raise DriveDownloadError(
                    f"Drive download HTTP {e.code} for {file_id}") from e
            except Exception as e:  # transport errors are transient
                last_error = e
            if attempt < self.max_attempts:
                _sleep(_backoff_delay(attempt, self.backoff_base_sec))
        raise DriveRetryExhaustedError(
            "Drive block retries exhausted for " + str(file_id)
            + ": " + (str(last_error)[:120] if last_error else "unknown"))

    def _download_concurrent(self, file_id: str, expected_size: int,
                             expected_sha256: str, part: Path, on_progress,
                             on_token_expiring,
                             abort_event: threading.Event | None = None,
                             resume_bitmap: set[int] | None = None):
        """Concurrent download into one preallocated ``.part`` file
        (v0.2.0 F1): ``os.truncate`` to the full size, then
        ``concurrent_workers`` threads each ``os.pwrite`` their block
        at its offset. Peak disk is ``<= 1.0x`` the file size — no
        shard temp files, no merge copy.

        Returns ``{"streaming_sha256": hex}`` when every block is on
        disk and the ordered streaming hash is complete; the caller
        still runs the post-metadata check + atomic replace. Returns
        False to fall back to the single-stream path: the server
        ignored Range, or a block exhausted its retries. On fallback
        the preallocated part + bitmap are removed so the single
        stream starts clean. A 403 (DriveAuthError), a metadata
        disagreement (DriveChangedError), an ``on_progress`` exception
        or an external abort is fatal: the callback error / abort is
        re-raised after the pool finishes, and no fallback is
        attempted. On abort the part + bitmap are left in place for
        resume; a valid bitmap lets the next attempt fetch only the
        missing blocks.
        """
        # Token-expiry signal once, before the pool starts.
        if on_token_expiring is not None:
            remaining = None
            seconds_remaining = getattr(
                self.token_provider, "seconds_remaining", None)
            if callable(seconds_remaining):
                remaining = seconds_remaining()
            if remaining is not None and remaining < TOKEN_REFRESH_MARGIN_SEC:
                token_id = getattr(self.token_provider, "token_id", None)
                on_token_expiring(token_id, remaining)
        if abort_event is not None and abort_event.is_set():
            raise DriveDownloadAbortedError(
                "Drive download aborted for " + str(file_id))

        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        ranges = self._block_ranges(expected_size)
        n = len(ranges)
        done_blocks: set[int] = set(resume_bitmap) if resume_bitmap else set()

        # Fresh: preallocate the whole file up front. Resume: the part
        # was already validated at full size by _load_bitmap.
        if resume_bitmap is None:
            fd = os.open(part, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            try:
                os.ftruncate(fd, expected_size)
            except OSError:
                os.close(fd)
                raise
            _save_bitmap_atomic(part, expected_size, expected_sha256,
                                self.concurrent_workers, set())
        else:
            fd = os.open(part, os.O_RDWR)

        hasher = _OrderedBlockHasher(fd, ranges, self.chunk_size)
        # Re-commit already-downloaded blocks to the hash state, in
        # order (reads them back from disk; no memory buffering).
        for index in sorted(done_blocks):
            hasher.block_completed(index)

        done = [sum(ranges[i][1] - ranges[i][0] + 1 for i in done_blocks)]
        progress_lock = threading.Lock()
        progress_error: list = []
        abort = threading.Event()
        bitmap_lock = threading.Lock()

        def _report(nbytes: int) -> None:
            # Held across the callback so concurrent blocks report a
            # monotonic cumulative total.
            with progress_lock:
                done[0] += nbytes
                total = done[0]
                if on_progress is not None:
                    try:
                        on_progress(total, expected_size)
                    except Exception as exc:  # recorded; raised after the pool
                        if not progress_error:
                            progress_error.append(exc)
                        abort.set()

        def _mark_done(index: int) -> None:
            with bitmap_lock:
                done_blocks.add(index)
                _save_bitmap_atomic(part, expected_size, expected_sha256,
                                    self.concurrent_workers, done_blocks)

        errors: list = []
        fallback = False
        try:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(
                    self._download_block, url, file_id, i, s, e,
                    expected_size, fd, _report, abort, abort_event)
                    for i, (s, e) in enumerate(ranges)
                    if i not in done_blocks]
                for fut in futs:
                    try:
                        index = fut.result()
                        hasher.block_completed(index)
                        _mark_done(index)
                    except Exception as exc:  # collected; classified below
                        errors.append(exc)
            if progress_error:
                raise progress_error[0]
            for exc in errors:
                if isinstance(exc, (DriveAuthError, DriveChangedError,
                                    DriveDownloadAbortedError)):
                    raise exc
            if errors:
                fallback = True
            else:
                digest = hasher.hexdigest()
                # Durability before declaring success: the bitmap's
                # claim that every block is on disk must be true.
                os.fsync(fd)
                return {"streaming_sha256": digest}
        finally:
            os.close(fd)
        if fallback:
            part.unlink(missing_ok=True)
            _discard_bitmap(part)
            return False

    def probe_transport(self, file_id: str, probe_bytes: int = 1024 * 1024) -> dict:
        """Cheap auth + transport probe: metadata GET, then a single
        Range GET of the first ``probe_bytes`` bytes. Validates the
        206 status, the Content-Range header, and the exact byte
        count. Proves the token works and the server honours Range;
        it does NOT prove full-file integrity (the real restore does
        that with a full SHA-256). Raises DriveDownloadError on any
        failure. Never returns token material."""
        meta = self.get_metadata(file_id)
        total = meta.get("size")
        if total is None or int(total) <= 0:
            raise DriveDownloadError(
                f"Drive probe metadata missing size for {file_id}")
        want = min(int(probe_bytes), int(total))
        url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media&supportsAllDrives=true"
        token = self.token_provider.get_token()
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token,
                          "Range": f"bytes=0-{want - 1}"}, method="GET")
        try:
            with _urlopen(req, self.timeout_sec) as resp:
                status = getattr(resp, "status", 200)
                if status != 206:
                    raise DriveDownloadError(
                        f"Drive probe expected HTTP 206, got {status} "
                        f"for {file_id}")
                cr = resp.headers.get("Content-Range", "")
                m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", cr or "")
                if not m or int(m.group(1)) != 0 or int(m.group(3)) != int(total):
                    raise DriveChangedError(
                        "Drive probe unexpected Content-Range for "
                        + str(file_id))
                got = 0
                while True:
                    chunk = resp.read(self.chunk_size)
                    if not chunk:
                        break
                    got += len(chunk)
                if got != want:
                    raise DriveDownloadError(
                        f"Drive probe short read for {file_id}: got "
                        f"{got}, wanted {want}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DriveAuthError(
                    "Drive probe 401 for " + str(file_id)) from e
            if e.code == 403:
                raise DriveAuthError(
                    "Drive probe 403 for " + str(file_id)) from e
            raise DriveDownloadError(
                f"Drive probe HTTP {e.code} for {file_id}") from e
        return {"drive_file_id": file_id, "size": int(total),
                "probe_bytes": want}

    # -- many files --

    def download_all(self, files, dest_dir, on_progress=None,
                     on_token_expiring=None) -> list:
        """Download a list of pinned files.

        Each entry: ``{"file_id", "path", "size", "sha256",
        "md5" (optional)}``. ``path`` is relative to ``dest_dir``.
        Files download with bounded concurrency (``max_workers``;
        default 1 = sequential). Returns per-file result dicts in
        input order. On failure the first error is raised; files that
        already verified stay on disk and are reused on retry."""
        dest_dir = Path(dest_dir)

        def _one(entry):
            rel = entry["path"]
            return self.download_file(
                entry["file_id"], int(entry["size"]), entry["sha256"],
                dest_dir / rel, expected_md5=entry.get("md5"),
                on_progress=on_progress,
                on_token_expiring=on_token_expiring)

        if self.max_workers == 1:
            return [_one(entry) for entry in files]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(_one, files))

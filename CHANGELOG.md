# CHANGELOG

## v0.3.0 — 2026-11-20 (TASK-VERSION-ROADMAP-011 / TASK-VERSION-INTEGRATION-012)

Integrated release (PR #18, single Merge Commit into `main`): the v0.2.0
fast cold start (F1–F5), v0.2.1 zero-compute optimisations (P1–P3) and
v0.3.0 self-healing + warm pool (M1–M4) stages, on top of the v0.1.0
Muse Adapter. **v0.2.0 and v0.2.1 were development stages only and were
never released separately** — their work is folded into this release
and no retroactive v0.2.0 / v0.2.1 Release will be created.

Release status: `OFFLINE_VERIFIED` (1046 offline tests, 0 failures;
GitHub Actions 6/6 on the release HEAD) — `LIVE_E2E_NOT_VERIFIED`
(no paid GPU session was started, no CU spent; Remote Seed, the
Python 3.13 wheelhouse and warm-pool reuse were never run live) —
`PRODUCTION_NOT_UPGRADED` (the production directory was never touched;
upgrading production needs separate approval).

### M4 — controlled warm session pool, default OFF (2026-11-20)

Muse-original reimplementation of the upstream v0.3.0 M4 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). The pool is OFF by default:
`--keep-warm-ttl` accepts 0 (disabled) or 60–900 s, validated in the
controller, the wrapper, and the task dict. A healthy run may arm a
warm lease instead of stopping: the arm expiry is the minimum of
now + TTL, session start + 14400 s, and the session budget deadline;
the lease records `comfy_ref`, the model-manifest SHA-256, the spent
CU, and the rate, and the run manifest moves to session
`WARM_POOLED` with `warm_lease_armed: true`. Any arm failure falls
back to the normal stop — never a live session left unaccounted for.
A later run reuses a pooled session only after all five gates pass:
matching `comfy_ref` and manifest SHA-256, lifetime headroom
(elapsed + 2400 s ≤ 14400 s), session budget
(spent + new-task estimate + 0.75 CU reserve ≤ 25.0 CU), a live
`query_session_state` == ACTIVE, and a fresh `MUSE_WARM_HEALTH_OK`
from `h3_warm_health_probe.py` (`/object_info` must expose the four
MiniMax-H3 node types). Reuse dispatches a lightweight Stage-2
bundle (`_dispatch_warm_stage2`: worker, launcher, task, refs,
workflow, fail-soft upload block — no Stage-1 probe, the models are
already in the NVMe store); the worker skips setup when the resident
ComfyUI is alive with the pinned commit and all models present, and
scrubs ephemeral token files when a warm run ends. The manifest
`TASK_COMPLETED` gate accepts `WARM_POOLED` + armed (never an
unarmed pooled session); `record_warm_session_stopped` moves a
pooled run to STOPPED idempotently. Cross-task reclamation lives in
Muse's own `h3_shutdown_executor.py --stop-warm` (+
`verify_shutdown.sh --stop-warm`): expired warm leases are overdue
as `warm_ttl_expired`, `--stop-warm` stops them as `manual_stop_warm`
regardless of task scope, and confirmed stops update the pooled
manifests. The upstream macOS guardian scripts were NOT copied (no
Mac runtime dependency under the North Star) and no guardian was
installed. Reliability metric D now requires session STOPPED +
`GPU_STOP_CONFIRMED`, and `compute_reliability` reports
`warm_session_hits` + `warm_pooled_active_runs`.

Tests: `tests/test_v030_m4_warm_infra.py` (21: registry normalize /
mark / claim / find / confirm-clears-warm, manifest gate + idempotent
stop recording, reliability warm counts + metric-D exclusion,
executor warm audit + stop-warm-only, probe success / missing node /
non-dict) and `tests/test_v030_m4_controller_worker.py` (22: TTL
boundaries, all five claim gates, worker warm helpers, ChainHarness
arm-pools / arm-failure-falls-back-to-stop / TTL-0-cold-stop /
reuse-without-allocating / unhealthy-probe-refuses-reuse). Full
offline suite: 1046 tests, 0 failures. Live GPU verification:
NOT_VERIFIED — no warm session was ever created.

### M3 — host disconnect self-healing & Drive 0-GPU recovery (2026-11-20)

Muse-original reimplementation of the upstream v0.3.0 M3 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `_call_with_transient_retry`
(5 attempts, 5/10/20/30 s backoff, injectable sleep/on_retry) retries
transient network/TLS failures only — type-based
(ConnectionError/TimeoutError/ssl.SSLError) plus the h3_budget
transport classifier and the TLS/timeout phrases; auth, quota,
permission and input errors raise on the first attempt. Wired into
host polling (`poll_worker`), inspection (`_inspect_live`, keeping
the legacy bounded loop for non-network CLI errors), the resume
session-state query, and the deliver gates (`_verify_completed_delivery`,
`inspect()`). `h3_drive_token.ensure_token_freshness(min_remaining_sec=900)`
refreshes the user ADC silently when expired/invalid/near expiry
(service accounts refused, same as `_mint_token`; never logs token
material) and is wired into the 0-CU preflight as `token_freshness`.
`recover_from_drive_archive` recovers a finished video at 0 CU when
the host disconnects after the GPU stopped: it resolves the Drive
file from the manifest `drive_file_id` (size/MD5 checked against the
remote-upload evidence) or from `Muse-H3-Video-Outputs/<run_id>/<variant>.mp4`
via `list_named`, downloads to a temp file, validates with
`validate_final_video`, atomically moves it into place, and advances
the manifest VIDEO_VALIDATED → ARTIFACT_DURABLE → DRIVE_VERIFIED →
TASK_COMPLETED; a failed validation never leaves a partial file.
`_resume_stopped_session` tries it before the metadata-only server
verify, and after a failed local-MP4 archive. Never re-infers, never
allocates a GPU.

Tests: `tests/test_v030_m3_self_healing.py` (17: retry semantics,
archive recovery via file id / folder lookup / corrupt / Drive down,
resume wiring incl. download-failure fallback to server verify,
token freshness incl. service-account refusal, poll-worker retry).
Full offline suite: 1003 tests, 0 failures. Live GPU verification:
NOT_VERIFIED.

### M2 — two-stage bundled bootstrap (2026-11-20)

Muse-original reimplementation of the upstream v0.3.0 M2 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `--bootstrap-mode bundled|legacy`
(default `bundled`; the wrapper validates it). Stage 1 uploads ONE
deterministic tar (contract files, refs, manifest, workflow, probe
spec + scripts, and 0600 downscoped tokens — never
`h3_remote_worker.py`, refused at build time) plus the Stage-1
extractor, then runs the Drive download probe, which must print
`MUSE_DRIVE_DOWNLOAD_PROBE_OK` before Stage 2 is built or uploaded.
Stage 2 uploads ONE tar (`h3_job_state.py`, `h3_remote_worker.py`,
`h3_worker_launcher.py`, `task.json`); the launch execs the Stage-2
extractor, which invokes the same worker launcher. Both extractors
validate every tar member before extracting anything (absolute paths,
`..`, symlinks/hardlinks, device nodes, escapes, oversize bundles
rejected), enforce 0600 on `*token*.json`, and unlink the tar in
`finally`. Local token files and tars are unlinked after use. Run
manifests persist `bootstrap` telemetry (`mode`,
`bootstrap_wall_sec`, `cli_round_trips` — 4 bundled calls; legacy
records mode + wall time only). The mount source always uses legacy.
The bundled path cut the bootstrap from ~23 CLI round trips to 4.

Tests: `tests/test_v030_m2_bundled_bootstrap.py` (18: deterministic
tar + modes + forbidden/traversal rejection, mode resolution, both
extractors incl. validate-before-extract and tar unlinking, and
fault-injection execution — bundled success with telemetry, probe
failure never builds Stage 2, legacy mode keeps the old uploads,
mount falls back to legacy). The three chain harnesses now read real
fixture files (the bundled path reads them to build its tar) and stub
the new `_mint_drive_token_doc` seam. Full offline suite: 986 tests,
0 failures. Live GPU verification: NOT_VERIFIED.

### M1 — GPU 503 elastic backoff & error classification (2026-11-20)

Muse-original reimplementation of the upstream v0.3.0 M1 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `h3_budget.py` gains
`classify_allocation_error()` (`AUTH_FATAL`, `QUOTA_EXCEEDED`,
`CAPACITY_TRANSIENT`, `TRANSPORT_TRANSIENT`, `UNKNOWN_FATAL`) and
`compute_backoff_delay()` (`15s / 45s / 90s ±20%` jitter, later rounds
reuse 90s). `h3_controller.py` wraps the existing inner attempt plan
(`G4`, `G4`, `A100_HIGH_MEM`) in outer `--allocation-rounds` rounds
(1..5; default `3` for `deliver` / `--auto-recover`, `1` for a raw
run): capacity failures exhaust the inner plan, then back off before
the next round unless the task deadline leaves less than
`MIN_ATTEMPT_WALL_SEC + DEADLINE_MARGIN_SEC` after the delay; fatal,
quota and unknown errors fail fast with no retry; transport errors
retry the same G4 once and never fall back to A100. Each backoff
records `GPU_ALLOCATION_BACKOFF` in the controller audit log and the
run-manifest audit log. A failed attempt's lease and allocating
watchdog record are marked stopped immediately (no phantom
`not_yet_due` leases). `scripts/run_h3_task.sh` passes
`--allocation-rounds` through.

Tests: `tests/test_v030_m1_backoff.py` (21: classification matrix,
backoff schedule/jitter/clamps, rounds resolution/defaults, and
fault-injection execution — round-1 503 exhaustion → 15.0s sleep →
round-2 success, auth/quota fail-fast, deadline abort, stale ledger
deadline ignored, failed leases stopped immediately). Full offline
suite: 968 tests, 0 failures. Live GPU verification: NOT_VERIFIED.

## v0.2.1 development stage — folded into v0.3.0, never released separately (TASK-VERSION-ROADMAP-011)

### P1 — Drive upload MD5 hex alignment (2026-11-20)

Muse-original reimplementation of the upstream v0.2.1 P1 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `h3_remote_worker.py`:
`_md5_b64` is replaced by `_md5_hex` — `validate_assembled_mp4`
now returns the 32-char lowercase hex MD5 that matches Google Drive
API v3 `md5Checksum` directly, and the worker evidence
(`upload_remote_archive`) carries hex instead of base64. The
defensive `_normalize_md5` in `h3_drive_upload.py` already accepted
both forms (PR #62 lineage); it was verified, not re-implemented:
hex case-insensitively, 24-char base64 16-byte digests decoded to
hex, anything else lower-cased — and `verify_uploaded` fails closed
when an expected MD5 has no server `md5Checksum`.

Tests: `NormalizeMd5Tests` (4), `VerifyTests` gains the missing-
server-MD5 fail-closed case plus the no-MD5-requested pass case
(`tests/test_drive_upload.py`); `Md5HexTest` (2) in
`tests/test_remote_worker_v020.py`. Full offline suite: 925 tests,
0 failures. Live GPU verification: NOT_VERIFIED.

### P2 — Headless wheelhouse exclusions + py313 (2026-11-20)

Muse-original reimplementation of the upstream v0.2.1 P2 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `h3_comfyui_cache.py`:

- `SUPPORTED_PYTHON_TAGS` gains `py313` (the tag normalizer was
  already generic — "3.13"/"cp313"/"py313" all normalize without a
  code change).
- Headless-optional ComfyUI UI asset packages
  (`comfyui-workflow-templates`, `comfyui-workflow-templates-*`,
  `comfyui-embedded-docs`) are excluded from the wheelhouse by
  `filter_requirements_lines`, per the upstream audit of the pinned
  ComfyUI commit `61e524bdba7a4e193b45c2697f1af83c1beb86d6`
  (`server.py` + `app/frontend_management.py`);
  `comfyui-frontend-package` is intentionally kept (it calls
  `sys.exit(-1)` on `ImportError`).
- `manylinux_2_28_x86_64` (required by `av>=17.0.0`) needs no code
  change: `_wheel_matches_pytag` already accepts any
  `manylinux.*_x86_64` tag generically; a regression test proves a
  `manylinux_2_28_x86_64` wheel validates for `py313`.
- `verify()` gains the remote-only branch: an entry whose local copy
  is gone (staging cleaned after publish) is verified from server
  metadata alone — size + MD5 + SHA-256 against the manifest —
  instead of failing on the missing local file.

`h3_remote_worker.py`: the same headless set lives worker-side
(`HEADLESS_EXCLUDED_PACKAGES`/`HEADLESS_EXCLUDED_PREFIXES`);
`log_requirements_status` records headless-optional packages under
`excluded` instead of `missing`, and `pip_install_needed` no longer
counts them; `_install_requirements` installs a filtered
`wheelhouse_requirements.txt` on the offline path (otherwise
`--no-index` fails on packages neither installed nor in the bundle
and silently falls back to PyPI), while the PyPI fallback uses the
original file.

Tests: filter exclusions incl. prefix variants + frontend-package
kept, `py313` archive name + entry selection, `verify()` remote-only
(happy path, bad server MD5, local-file path) in
`tests/test_comfyui_wheelhouse.py`; excluded bucket + no-pip-needed
in `tests/test_h3_comfyui_cache.py`; filtered offline install +
fallback-to-original in `tests/test_remote_worker_v020.py`. Full
offline suite: 934 tests, 0 failures. The actual `py313` wheelhouse
bundle was NOT built or published (live network + Drive write, needs
separate authorization). Live GPU verification: NOT_VERIFIED.

### P3 — Monotonic MODELS_READY backfill + reliability calibration (2026-11-20)

Muse-original reimplementation of the upstream v0.2.1 P3 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `h3_controller.py`:

- `_backfill_models_ready_if_needed` backfills `MODELS_READY`
  monotonically (Muse's own `TASK_STAGES` order from
  `h3_run_manifest.py`; never a backward move, never raises) when a
  poll observes `INFERENCE_RUNNING` / `INFERENCE_COMPLETED` /
  `VIDEO_READY` or `coldstart_metrics` before a separate
  `MODELS_READY` poll arrived — the worker crosses
  MODELS_READY → INFERENCE_RUNNING in milliseconds while the
  controller polls every 60s.
- `_record_coldstart_metrics` backfills before persisting and records
  a `COLDSTART_METRICS` audit event in the run's audit log (via
  `record_audit_event` — the controller's `record()` writes a
  different log and does not count).

`h3_reliability.py`: `_evaluate_run_f` condition (2) now uses
`_has_models_ready_evidence` — an explicit `MODELS_READY` stage, a
`COLDSTART_METRICS` audit event, or the monotonic
`WORKER_STARTED -> INFERENCE_COMPLETED -> VIDEO_VALIDATED` chain
(first timestamps in order); incomplete or out-of-order chains still
fail closed.

Tests: `ModelsReadyBackfillTest` (6) in
`tests/test_controller_wheelhouse_telemetry.py`;
`tests/test_reliability_p3.py` (7: evidence legs, incomplete and
out-of-order chains, metric-F integration for a backfilled run);
`tests/test_fault_injection.py` baseline updated — the full chain
now records the backfilled `MODELS_READY` (intended behaviour
change, documented in Issue #13). Full offline suite: 947 tests,
0 failures. Live GPU verification: NOT_VERIFIED.

## v0.2.0 development stage — folded into v0.3.0, never released separately (TASK-VERSION-ROADMAP-011)

### F1 — Zero-copy high-concurrency Drive download (2026-11-20)

Muse-original reimplementation of the upstream v0.2.0 F1 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md` in the adapter PR). `h3_drive_download.py`:

- Large fresh downloads no longer shard into `.part.shardN` temp
  files plus a merge. One `.part` file is preallocated with
  `os.truncate` and filled by lock-free multi-threaded `os.pwrite`
  (peak disk `<= 1.0x` the file size).
- `DEFAULT_CONCURRENT_WORKERS` 4 → 12, configurable via
  `H3_DRIVE_CONCURRENT_WORKERS` (1..16); process-wide Drive
  connection semaphore `DEFAULT_MAX_GLOBAL_CONNECTIONS = 16` via
  `H3_DRIVE_MAX_GLOBAL_CONNECTIONS`.
- Ordered in-flight streaming SHA-256: blocks hash in index order;
  a block that finishes out of order is read back with `os.pread`
  when its turn comes instead of being buffered whole. A successful
  concurrent download no longer re-reads the whole file for a second
  full hash — the streaming digest is the proof (post-metadata check
  and atomic replace unchanged).
- Atomic `.part.bitmap.json` block-level resume: an interrupted
  download refetches only its missing blocks.
- `abort_event` / `DriveDownloadAbortedError`: cancellation leaves
  `.part` + bitmap for resume.
- Single-flight `FileTokenProvider.refresh(failed_token_id=None)`:
  concurrent 401s trigger one token-file re-read, not one per thread.

Tests: new `tests/test_drive_download_v2.py` (17 tests, offline fake
transport). Updated `tests/test_drive_download.py`: default-workers
constant 12; the TASK-007.1 shard-merge test was retired because its
implementation no longer exists (replaced by the pwrite tests — an
intended behaviour change, recorded in the test file, not a silent
deletion). Full offline suite: 760 tests, 0 failures. Live GPU
verification: NOT_VERIFIED (no GPU run under this change).


TASK-012 §2 fix (2026-11-20, integration stage): the global
connection cap is now defined as simultaneously ACTIVE HTTP download
streams, not connections merely established. `_urlopen` returns a
`_PermitResponse` that holds one permit from connection
establishment until the Response is fully read and closed; the
previous helper released the permit as soon as `urlopen()` returned,
so a slow stream stopped counting against the cap while still
reading. The fix covers Range downloads, the single-stream path,
metadata requests and the probe. Release is idempotent (bounded
semaphore, lock-guarded), a failed `urlopen()` releases immediately,
and no code path acquires a second permit while holding one, so no
deadlock. 429/5xx backoff, token refresh and SHA-256 verification
are untouched. Tests: new `ConnectionPermitLifetimeTest` (6 tests in
`tests/test_drive_download_v2.py`): permit held during a slow read,
released on exception exit, released when `urlopen()` raises,
double close releases once, metadata request holds its permit, and
the cap bounds concurrent streams across three Downloaders.

### F2 — Offline ComfyUI wheelhouse cache (2026-11-20)

Muse-original reimplementation of the upstream v0.2.0 F2 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md` in the adapter PR). `h3_comfyui_cache.py`:

- Drive SDK imports are lazy: the pure-stdlib wheelhouse helpers
  (`normalize_pkg_name`, `normalize_python_tag`,
  `filter_requirements_lines`, `filter_requirements_file`,
  `validate_wheelhouse_entry`, `select_wheelhouse_entry`,
  `build_wheelhouse_task_descriptor`, `verify_local_wheelhouse`)
  work with no Drive dependency (proven by a test that blocks all
  `google*` imports).
- `build-wheelhouse` / `publish-wheelhouse` (publish gated by
  `--allow-drive-write`) package deterministic
  `comfyui-wheelhouse-<commit>-<pytag>.tar.gz`
  (`wheelhouse/*.whl` + `wheelhouse/WHEELHOUSE_META.json`, mtime 0,
  rebuilds byte-identical) for Colab Linux x86_64 (`py311`/`py312`),
  excluding preinstalled `torch`, `torchvision`, `torchaudio`,
  `triton`, `nvidia-*`. A required package with no matching local
  wheel fails closed with its name listed.
- `verify_wheelhouse_on_drive` discovers commit-matching bundles in
  `Muse-H3-ComfyUI-Source` alongside the source tarball (the Muse
  equivalent of the upstream `--sync-shared-caches` discovery; Muse
  has no `setup_isolated_account.py` script, so there was nothing to
  update there — recorded NOT-APPLICABLE in the ledger).
- `h3_controller.py` wires optional `drive_download["wheelhouses"]`
  (one-entry list when a wheelhouse manifest exists, key absent
  otherwise). Worker-side install from the wheelhouse is F3, not F2.

Tests: new `tests/test_comfyui_wheelhouse.py` (19 tests, offline).
Full offline suite: 763 tests, 0 failures. Live GPU verification:
NOT_VERIFIED (no GPU run under this change).

### F3 — Dual-track parallel setup + wheelhouse-first install (2026-11-20)

Muse-original reimplementation of the upstream v0.2.0 F3 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `h3_remote_worker.py`:

- `ensure_comfy()` and `restore_weights()` now run concurrently via
  `ThreadPoolExecutor(max_workers=2)` with a shared `abort_event`
  (`run_setup_parallel`). The first failure sets the event; the
  surviving track's in-flight Drive download aborts through its
  progress callback (`_abort_progress` raises `_SetupAborted`) and
  the failure is reported with the stage that actually failed
  (`SetupTrackFailed`, carried into `WORKER_FAILED_JSON`).
- Thread-safe token refresh: `_request_token_refresh` serializes its
  once-per-token-id throttle with a lock, so two tracks expiring at
  once ask the controller once.
- Thread-local command failure tracking: the `run_cmd` failure record
  is per-thread now (`_cmd_failure_get`); the failing track's record
  travels with the failure to the main thread. Existing tests updated
  to the new accessor (same behaviour: set on failure, cleared on
  success).
- Wheelhouse-first install: when `drive_download["wheelhouses"]`
  carries an entry for this interpreter, the worker downloads the
  bundle with the standard Drive transport, extracts it with
  tar-member validation (absolute paths, `..`, symlinks/hardlinks,
  device nodes, escape rejected), verifies `WHEELHOUSE_META.json`
  against the entry (`commit` + `python_tag`), and installs with
  `pip install --no-index --find-links`. Any offline-path failure
  falls back to plain PyPI and emits structured
  `WHEELHOUSE_FALLBACK_PYPI`; a failed plain install still raises.

Tests: new `tests/test_remote_worker_v020.py` (25 tests, offline fake
transport). Full offline suite: 818 tests, 0 failures. Live GPU
verification: NOT_VERIFIED (no GPU run under this change).


### F4 — Independent NVMe model store + high-VRAM flags (2026-11-20)

Muse-original reimplementation of the upstream v0.2.0 F4 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `h3_remote_worker.py`:

- Weights now live in one NVMe store, `/content/nvme_models/h3`
  (overridable via `H3_NVME_MODELS_DIR`), and are atomically
  symlinked into `comfy/models` by `link_models_into_comfy()` (temp
  symlink + `os.replace`). A re-run reuses the store instead of
  re-downloading; a ComfyUI reinstall never touches the weights.
- The link step only links verified weights: store file must resolve
  inside the store (`require_inside`), exist, be readable, and match
  the pinned size and SHA-256 (`verify=True` by default). An existing
  correct link or correct real file is left alone; a stale link is
  atomically replaced; a real file with wrong content fails closed.
  `restore_weights` verifies each store file in-run (reuse-check hash
  or the downloader's streaming SHA-256), so it links with
  `verify=False` under a documented contract. The destination
  containment check is lexical (`_lexical_inside`): `require_inside`
  would follow an existing link into the store and fail every
  re-link.
- `restore_weights` gains an explicit `nvme_dir` parameter (tests
  isolate the store; production uses the env/default store).
- GPU VRAM auto-detection (`detect_gpu_vram_mib`, silent `nvidia-smi`
  probe, None on any failure) appends `--highvram --disable-mmap`
  for VRAM >= 76800 MiB, overridable via `H3_COMFY_VRAM_FLAGS`
  (split on whitespace; empty means no flags). The launch prints
  `COMFY_VRAM_FLAGS`.

Tests: `NvmeStoreTest` (11), `VramFlagsTest` (5) and
`RestoreWeightsNvmeTest` (3) in `tests/test_remote_worker_v020.py`
(19 new tests, offline fake filesystem/GPU probe); pre-existing
`WorkerDownloadTests` in `tests/test_drive_download.py` now pass an
explicit tmp store so they cannot leak fixtures into the global
default store. Full offline suite: 878 tests, 0 failures. Live GPU
verification: NOT_VERIFIED (no GPU run under this change).

### F5 — In-cloud remote seeding + cold-start telemetry (2026-11-20)

Muse-original reimplementation of the upstream v0.2.0 F5 behaviour
spec (no upstream code copied — upstream has no license; see
`docs/upstream/PORTING_LEDGER.md`). `drive_depot.py`:

- New `remote-seed` action, triple-gated: `--allow-drive-write`,
  `--allow-large-transfer`, and `--confirm-remote-seed`. It launches
  ONE temporary Colab session (G4, cheapest, via `allocation_command`
  + the watchdog's `record_session` / `confirm_session_active`) with a
  short-lived `drive.file` token (`mint_file_token`, delivered as a
  0600 file, never in the script, CLI args, or logs). The session
  downloads the locked `config/pinned_hf_files.json` weights in-cloud
  (8-way Range, SHA-256 verified), uploads them to the Drive Depot
  (reusing an identical file, never overwriting a differing one), and
  the local side verifies every entry server-side (`verify_drive_entry`)
  before the shared `_commit_ready_manifest` publishes the READY
  document (extracted from `seed()`'s tail, behaviour unchanged).
- The session stop runs in `finally` on every path and is tristate
  verified (`stop_and_verify` mirrors
  `h3_controller.stop_and_verify_session`); an unconfirmed stop raises
  `REMOTE_SEED_ORPHAN_RISK` and the seed never reports success. A
  failed `new` that still produced a session stops it before raising.
  The runner is injectable, so the whole flow is testable offline.

Cold-start telemetry:

- `h3_remote_worker.py`: the setup tracks record per-track wall time
  (`_SETUP_TRACK_TIMINGS`), the wheelhouse outcome (`_WHEELHOUSE_HIT`)
  and per-file reuse/download counts (`_RESTORE_COUNTS`). After setup
  succeeds, `main()` emits `COLDSTART_METRICS_JSON` (setup_seconds,
  comfy_install_seconds, restore_seconds, wheelhouse_hit,
  models_reused, models_downloaded, vram_mib) and persists the same
  report in `MUSE_H3_JOB_STATE.json`. VRAM is now detected once and
  shared by the flags and the report.
- `h3_run_manifest.py`: `validate_coldstart_metrics` (fail-closed
  shape check) + `update_run(..., coldstart_metrics=...)` stores the
  validated report; a later report replaces an earlier one.
- `h3_controller.py`: `parse_coldstart_metrics` extracts the report
  from exec/log text (last line wins, corrupt lines skipped);
  `_record_coldstart_metrics` persists the polled job state's report
  from `_manifest_worker_state` (same path as the remote-upload
  evidence), best-effort, never breaking a run.
- `h3_reliability.py`: the report gains a `coldstart` section —
  setup_seconds P50/P90 (`_percentile`, linear interpolation) and
  `wheelhouse_hit_rate` (Wilson rate over runs reporting a boolean
  hit; `None` means pip was skipped, not a miss). UNKNOWN when no run
  reports metrics.

Tests: `tests/test_drive_depot_remote_seed.py` (12: triple-flag CLI
gate, fake runner happy path, exec failure still stops, unconfirmed
stop is an orphan risk, report mismatch rejected before commit,
token never in the script), worker `ColdstartMetricsTest` (5) +
`RestoreCountsTest` (1) in `tests/test_remote_worker_v020.py`,
`tests/test_controller_wheelhouse_telemetry.py` (15: parse, validate,
manifest persistence, poll wiring),
`tests/test_reliability_coldstart.py` (6: P50/P90 + hit rate + helper).
Full offline suite: 917 tests, 0 failures. Live GPU verification:
NOT_VERIFIED. No live remote seed was started.

### Muse Adapter (Stage 1)


Muse-original adapter layer over the unchanged v0.1.0 production
core (`adapters/muse/`): TaskSpec mapping, explicit-authorization →
wrapper args, read-only restart check, redacted status reports, and
explicit production-dir / task-file location. No production module
was changed; budget constants (30 / 25 / 0.75 CU, 14400 s) are
asserted, not redefined. Antigravity compatibility, upstream lock,
porting ledger and version matrix: `adapters/antigravity/
COMPATIBILITY.md`, `docs/upstream/UPSTREAM_LOCK.json`,
`docs/upstream/PORTING_LEDGER.md`,
`docs/upstream/COMPATIBILITY_MATRIX.md`. License verdict: no license
was found upstream, so no Antigravity source is copied verbatim;
porting proceeds as specification + Muse-original reimplementation.

## v0.1.0 — 2026-11-12 — initial migration (TASK-001, local repo)

Initial private migration of the verified Muse Colab H3 production
baseline from `~/workspace/Muse_Colab_H3_Drive_v3/`. No production logic
was rewritten; all production files are verbatim copies (SHA-256
recorded in `SOURCE_MANIFEST.json`).

### Migrated

- 21 flat-root production modules + `workflow_api.json` +
  `requirements-local.txt` (verbatim).
- `reference_v2/colab_manager.py` **and** `reference_v2/generate_video.py`
  (verbatim). Correction found by the test run: an early grep that
  excluded `reference_v2/` lines wrongly concluded `generate_video.py`
  was unimported; full discovery failed with
  `ModuleNotFoundError: No module named 'generate_video'`, proving the
  transitive import (`colab_manager.py:39` imports 10 names from it).
  The file was then migrated verbatim and the suite re-run.
  `reference_v2/config.yaml` stays excluded: no production code path
  loads it (the loader takes an explicit path and is never called by the
  H3 controller or tests), and it holds a hardcoded Mac user path plus
  SVD settings unrelated to this job.
- 8 migrated test suites (173 tests) + live-only
  `tests/live_drive_resume_check.py` (naturally excluded from discovery).
- Project docs verbatim: v3 README/progress, deployment status, both
  MUSE prompt docs, storyboard references, Drive remote-download design
  doc, P0 archive prompt docs, pinned HF files, ComfyUI source manifest,
  depot config example, UI-format source workflow.
- Real CLI patch diffs (`patches/google-colab-cli-0.7.5/`), generated
  against the pristine 0.7.5 wheel and verified with `git apply --check`
  plus a byte-identical round-trip.

### Added in this migration

- Configs mirroring code constants (`config/*.yaml`, `*.example.json`),
  asserted by new `tests/test_config_contract.py` (4 tests).
- New regression tests `tests/test_savevideo_history.py` (8 tests) for
  `find_rendered_video` (PreviewVideo `{images, animated}` list/bool,
  legacy `{videos}`, bare `{animated}`, images-without-animated,
  suffix/size gates, missing file, documented no-ffprobe gap).
- `jobs/minimax_h3_ab/` (job.yaml, `video_contract.py` facade — imports
  verified — README with the MiniMax_H3_Clean non-presence note).
- `scripts/` wrappers (all verified against real controller/CLI
  behaviour; `run_h3_ab.sh` refuses without `--confirm-live`) plus
  `verify_manifest.py`, `verify_patch.py`, `check_workflow_schema.py`,
  `check_docs_index.py`.
- `docs/`: architecture, 19-stage execution flow (with one UNVERIFIED
  stage — see below), module inventory, code provenance, 4 runbooks,
  24 incident records, 5 ADRs, 9 issue drafts, audits/research notes.
- Offline-only CI (`.github/workflows/ci.yml`): py_compile, test suite,
  workflow schema check, patch verification, secret scan, docs-index
  check. No GPU, no Drive, no credentials.
- `requirements.lock` (72 pins from the verified source venv,
  Python 3.12.3) and `pyproject.toml`.

### Test results (2026-11-12, source venv python, new repo root)

- Source baseline: `python -m unittest discover -s tests -q` in the
  source project → **173 tests, OK**.
- New repo full discovery: **185 tests ran, 183 passed, 2 errors**.
  The 2 errors are the asset-gated poster tests, failing verbatim with
  `ValueError: Original poster missing or SHA256 mismatch:
  <repo>/assets/original_poster.jpg`:
  - `test_contract.ContractTest.test_poster_hash`
  - `test_h3_production.ProductionTest.test_poster_sha256_required`
  Both pass in the source project only because the poster binary is
  present there; the binary is intentionally excluded from this repo.
  This is recorded, not hidden.
- CI entry point `python tests/run_ci_tests.py`: **183 ran, 0 failures,
  0 errors, 2 asset-gated skips** (printed).
- `python h3_controller.py plan` verified (prints `no_gpu_created: true`,
  project budget 30.0, per-session 25.0). `--help` verified.
- `scripts/verify_patch.py`: OK. `scripts/check_workflow_schema.py`: OK.
  `python -m py_compile` over all repo Python files: OK.
  `jobs/minimax_h3_ab/video_contract.py` import smoke test: OK
  (736/1312/24, 226/274 frames, seeds 42/43, Turbo off).

### Secret scan (2026-11-12, new repo)

- Pattern scan (Google/GitHub token shapes, private keys,
  refresh_token/client_secret with values): **clean** after tuning —
  the only hits were (a) the redaction regexes in
  `reference_v2/colab_manager.py` (the scrubber itself, allow-listed in
  CI), (b) the CI workflow's own search-pattern literals (excluded by
  filename, documented in the workflow), and (c) a loose JWT-shape
  pattern false-positiving on a dotted model filename in
  `config/pinned_hf_files.json` (pattern removed from CI, documented).
- Broad word scan (`token|secret|password|api_key`, file:line only):
  all hits inspected — code words only (`name_tokens`, `token_hex`
  run-id suffix, `psnr_avg` token parsing, `atomic_secret_json`,
  comments stating tokens are never logged). No credential values found.
- No model weights, poster binary, or MP4 in the repo.

### Unverified / PLANNED (not claimed as done)

- **DRIVE_OUTPUT_VERIFIED stage is UNVERIFIED**: `archive_variant_to_drive`
  is defined but has no call site in `run_variant` (verified by grep).
  Drive delivery is not verified by the current live flow.
- **Variant B pending re-run** (run `h3-20261112T093207Z-7ab515`
  interrupted by VM restart; orphan A100 stopped; no MP4).
- `workstation/` package layout is PLANNED only (no implementation).
- `prompts_ab.py` / `workflow_builder.py` (MiniMax_H3_Clean) are NOT in
  this repo — the files were never obtained (P2 evaluation issue).
- Drivemount 600s fix: mock-verified; live mounts so far stayed inside
  the old 120s authorization window (incident 07).
- `find_rendered_video` has no ffprobe gate and no RUN_ID-prefix
  fallback (P0 issue; gap documented by a passing test).
- VM-restart orphan recovery is manual (incident 12).

### Safety

- No Colab GPU created, no CU spent, no Drive reads/writes, no `colab`
  commands run by this migration. No remote set, nothing pushed.
- GitHub repo creation is NOT part of this local build (parent agent
  verifies identity/permissions and creates the private repo).

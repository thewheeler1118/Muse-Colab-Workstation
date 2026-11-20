# CHANGELOG

## Unreleased — v0.2.0 development (TASK-VERSION-ROADMAP-011)

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

# Porting ledger — Antigravity → Muse

Locked refs: `docs/upstream/UPSTREAM_LOCK.json`.
Matrix: `docs/upstream/COMPATIBILITY_MATRIX.md`.

**License rule (applies to every row below):** no license was found
in the upstream repository (no `LICENSE` file, no grant in README /
AGENTS / CHANGELOG, GitHub license API `null`, checked 2026-11-20).
`verbatim_allowed` is therefore `false` for every upstream module
until an explicit license or written permission is recorded. Porting
means behavioral specification + Muse-original reimplementation, with
tests. A row may only change to verbatim-allowed when the grant is
recorded here with its source.

**Never merged, never mirrored:** the upstream repo is fetched
read-only into a separate directory; no `git merge` of an upstream
branch, no `.git` copy, no mirror push, no overwrite of the Muse
worktree.

## Stage 1 — Muse adapter (Muse-original, no upstream source)

| Target module | Source | Verbatim copy? | Adaptation | Tests | Security review |
|---|---|---|---|---|---|
| `adapters/muse/__init__.py` | Muse-original | n/a | — | `tests/test_muse_adapter.py` | No credentials; re-exports only |
| `adapters/muse/adapter.py` | Muse-original | n/a | Wraps existing `TaskSpec` / `h3_budget` / wrapper script; no new GPU path | `tests/test_muse_adapter.py` | Authorization refuses without explicit `confirm_live`; budget constants asserted, not redefined |
| `adapters/muse/paths.py` | Muse-original | n/a | Explicit production-dir location; symlink + `config/tasks/` refusal | `tests/test_muse_adapter.py` | Never writes to production dir; never returns credential paths |
| `adapters/muse/redact.py` | Muse-original | n/a | Runtime counterpart of `scripts/secret_scan.py` patterns | `tests/test_muse_adapter.py` | Fake values only in tests; redaction never echoes matches |
| `adapters/muse/README.md` | Muse-original | n/a | Design doc | — (doc) | — |
| `adapters/antigravity/COMPATIBILITY.md` | Muse-original | n/a | Records hook non-equivalence + license verdict | — (doc) | — |
| `docs/upstream/UPSTREAM_LOCK.json` | Verified via `git ls-remote` + GitHub API | n/a | Tag-object vs commit SHAs distinguished | Manual verification 2026-11-20 | Contains SHAs + public repo names only |
| `docs/upstream/PORTING_LEDGER.md` | This file | n/a | — | — | Contains blob SHAs only, no source text |
| `docs/upstream/COMPATIBILITY_MATRIX.md` | Muse-original | n/a | — | — | — |

## Retained Muse-only modules (must NOT be replaced by upstream)

| Target module | Muse blob SHA (v0.1.0) | Source | Verbatim copy? | Reason | Tests | Security review |
|---|---|---|---|---|---|---|
| `scripts/manifest_policy.py` | `fe224d0e42877661d97c0a0b2ee3c270ead8b71b` | Muse (TASK-SECURITY-009 P0-A) | n/a — Muse-original | Git-tracked list + sensitive-path deny gate + symlink refusal; upstream has no equivalent file | `tests/test_manifest_security.py` | PASS (existing suite) |
| `tests/test_manifest_security.py` | `d1da277d0abccf76cee1b2db312bc7badc188044` | Muse (TASK-SECURITY-009 P0-A) | n/a — Muse-original | Regression tests for the deny gate | Self (in 744-test suite) | Fake values only |

## Upstream new files — v0.2.0 (`46a6a6d6d436ab9b76c91fb5a3cdd1c3511df64b`)

| Source path | Source blob SHA | Target path | Verbatim copy? | Must adapt? / Reason | Tests | Security review |
|---|---|---|---|---|---|---|
| `.agents/hooks.json` | `2d09d06f12566c1ee7b96062dd23ddf1e6fd2d0f` | — (none) | No | REJECTED as Muse config — Muse does not load `.agents/hooks.json`; enforcement stays in wrapper/controller/permissions | — | Reviewed: hook file itself is not secret-bearing, but claiming it enforces on Muse would be false |
| `.agents/pre_tool_guard.py` | `ba13ea7614ef83b250560cce5e6fe94ea6dd3d77` | — (none) | No | REJECTED as a copied guard — Muse equivalent is `run_h3_task.sh` gates + `secret_scan.py` + `redact.py`; reimplement behavior only if a gap is proven | — | Reviewed |
| `.agents/skills/colab-h3-video/SKILL.md` | `10ff20435a054a37f7621dcffec72525f803f43b` | — (none) | No | REJECTED — Muse skills live in the Muse skill system, not `.agents/skills/` | — | Reviewed |
| `config/tasks/shao_nian_v1_A.json` | `1192d110a873af97602b297818c76a603f01e1dd` | — (none) | No | REJECTED — real user TaskSpec, never copied | — | Reviewed: real task data |
| `config/tasks/shao_nian_v1_B.json` | `21650c3b4580e20239cefd2a93274ce7f5bc56e3` | — (none) | No | REJECTED — real user TaskSpec, never copied | — | Reviewed: real task data |
| `docs/runbooks/antigravity-cross-project-integration.md` | `3a744f104ffb173f57bd94eaeca7740d8b1ed088` | — (none) | No | REJECTED — cross-project doc references Antigravity runtime paths; Muse integration is documented in `adapters/antigravity/COMPATIBILITY.md` instead | — | Reviewed |
| `scripts/_colab_env.sh` | `bf5cc966170c76952f33611b2d38b2050d56b202` | TBD (F-stage) | No | SPEC PENDING — shell env helper; adapt to Muse VM paths only if needed, never with local absolute paths | TBD | PENDING |
| `scripts/install_macos_guardian.sh` | `73fefd8ea88680b57b7f6bfa4a67aaeb1a4de931` | — (none) | No | REJECTED — macOS guardian; North Star forbids Mac as a runtime dependency | — | Reviewed |
| `scripts/run_shutdown_guardian.py` | `17e1448ec1745da5a8274c7e3e42c529891222f4` | — (none) | No | REJECTED as-is — shutdown enforcement is `h3_shutdown_executor.py` + lease registry; a separate guardian/cron needs its own user approval | — | Reviewed |
| `scripts/setup_isolated_account.py` | `e29699f03b16c696bb0402470ada0b47f0155fb6` | — (none) | No | NOT-APPLICABLE (decided F2, 2026-11-20): Muse has no such script; its discover/verify role is covered by `verify_wheelhouse_on_drive` in `h3_comfyui_cache.py` + controller wiring (PR #6). No new isolated-account script is created — it would be a new subsystem, not a port, and it touches credentials by design. | — | Reviewed |
| `tests/test_comfyui_wheelhouse.py` | `89ff7f3d92118a74f86a932508975a646c640f27` | `tests/test_comfyui_wheelhouse.py` (Muse-original) | No | IMPLEMENTED (F2, PR #6, commit `fd61ece`): test intent reimplemented from the behaviour spec; no upstream test code copied | `tests/test_comfyui_wheelhouse.py` (19 tests, in 763-test suite) | PASS — offline only, fake wheels, no credentials |
| `tests/test_controller_wheelhouse_telemetry.py` | `994e4ba816a3bd909953f596c38f9d00fe97cfc1` | TBD (F5) | No | SPEC PENDING — telemetry wiring is F5's area (cold-start metrics); F2's controller wiring is covered by `tests/test_comfyui_wheelhouse.py` | TBD | PENDING |
| `tests/test_drive_depot_remote_seed.py` | `8063669e320785ae63c522fbfdef2ca5f3dcbca4` | TBD (F5) | No | SPEC PENDING — Remote Seed stays optional; no live GPU test without separate approval | TBD | PENDING |
| `tests/test_drive_download_v2.py` | `8ea33c16390b0237f1a3231a111cb362c8e3cf23` | `tests/test_drive_download_v2.py` (Muse-original) | No | IMPLEMENTED (F1, PR #4, commit `2f3c425`): test intent reimplemented from the behaviour spec; no upstream test code copied. The TASK-007.1 shard-merge test in `tests/test_drive_download.py` was retired (its implementation no longer exists) — an intended behaviour change, recorded in the test file. | `tests/test_drive_download_v2.py` (17 tests, in 760-test suite) | PASS — offline only, fake transport, no credentials |
| `tests/test_reliability_coldstart.py` | `3cb19ac48697ccb840687a02d8419930d06a568b` | TBD (F5) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_remote_worker_v020.py` | `38ea495e40be5f52569d337512df420e5f5bfd84` | `tests/test_remote_worker_v020.py` (Muse-original) | No | IMPLEMENTED (F3, PR #8, commit `2364408`; F4, integration branch, Issue #9; F5, integration branch, Issue #10): test intent reimplemented from the behaviour spec; no upstream test code copied | `tests/test_remote_worker_v020.py` (54 tests: 25 F3 + 19 F4 + 6 F5 + 2 P1 + 2 P2, in 968-test suite) | PASS — offline only, fake transport/pip/filesystem/GPU probe, no credentials |

## Upstream new files — v0.3.0 (`2fa7ba48c55614ccca157880e6b90d20f468f871`)

| Source path | Source blob SHA | Target path | Verbatim copy? | Must adapt? / Reason | Tests | Security review |
|---|---|---|---|---|---|---|
| `h3_bootstrap_stage1.py` | `51534a9fbc0f3c56dd198f19a8ff4d49408b3468` | TBD (M2) | No | SPEC PENDING — two-stage bundled bootstrap; strict archive-entry rejection (no path traversal, no symlinks) is a hard requirement | TBD | PENDING — archive safety is a gate |
| `h3_bootstrap_stage2.py` | `b15bf9a83b7e703ee536f455561a7b0265af73cb` | TBD (M2) | No | SPEC PENDING — same as above | TBD | PENDING |
| `h3_warm_health_probe.py` | `42eb6199958bc3a21ae6d144fa5de2cd497bdf6b` | TBD (M4) | No | SPEC PENDING — warm-pool health probe; pool defaults OFF, bounds 25 CU / 14400 s / TTL | TBD | PENDING |
| `tests/test_v030_m1_backoff.py` | `159b15da0abbba3214b0eb2ff89eb428d0066e44` | TBD (M1) | No | SPEC PENDING — Fatal/Auth/Quota must never retry infinitely | TBD | PENDING |
| `tests/test_v030_m2_bundled_bootstrap.py` | `02a017361e86ae8f4b42fc8323a1e166d5fb12a9` | TBD (M2) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_v030_m3_self_healing.py` | `933e05518b47bb5a6dc0e8e01fcbd9d00e005f25` | TBD (M3) | No | SPEC PENDING — finished video recovers first, never blind re-inference | TBD | PENDING |
| `tests/test_v030_m4_controller_worker.py` | `648995bd8226fe61eb2220ca7a39e9f42c60ca62` | TBD (M4) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_v030_m4_warm_infra.py` | `f5b009b583c95c9aafb7758faa79ee6cb043d5eb` | TBD (M4) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_v030_scripts_guard.py` | `8b33b0a814d0dc414bf209f18d89d46d336975a4` | TBD (M4) | No | SPEC PENDING | TBD | PENDING |

## Shared modules that differ (blob SHA, Muse v0.1.0 → upstream v0.3.0)

Sample, verified 2026-11-20. The full per-file diff analysis is
complete (Stage 2, 2026-11-20): `upstream-analysis.md` +
`porting-inventory.json` (73 entries, all `verbatim_allowed:
false`) in the task work area
(`~/workspace/version-roadmap-011/upstream/analysis/`, raw diffs
under `raw/`). Per-version scale: v0.2.0 +8,150/−1,472 (60 files),
v0.2.1 +8,606/−1,502 (63 files), v0.3.0 +13,253/−1,839 (73 files).
Each shared module gets its own ledger row when its feature PR opens.

| Module | Muse blob (v0.1.0) | Upstream blob (v0.3.0) | Same? | Note |
|---|---|---|---|---|
| `h3_task_spec.py` | `d0e2ec038e7d3d92efd7ae16a48bd49342f0e104` | `d0e2ec038e7d3d92efd7ae16a48bd49342f0e104` | Yes | TaskSpec unchanged upstream — adapter builds on it as-is |
| `h3_controller.py` | `452368875880e962dd6bdb3778d141a05065ab33` | `10607ea417de19a10654d379b2e2b65f574f442d` | No | Largest shared diff; per-feature rows open with F/M PRs |
| `h3_remote_worker.py` | `a1e219387e1c57ad4f89fa83149d10978cd06668` | `c85af91916831cb0dbba7b98a6bf4e245883b85e` | No | Per-feature rows open with F/M PRs |
| `h3_drive_download.py` | `682413b10bcb4d3a8be1165ff60ed3f71e1d8d69` | `4a260cbbe288a34f36c1d5f604e4853e12a83800` | No | F1 area |
| `h3_comfyui_cache.py` | `b6f62ba72b21a69e302ca4cff64c799f51fb6668` | `43a842dcaa6fb390afb8651d700d40a595d5c62d` | No | F2 area |
| `drive_depot.py` | `b03cc1546f9541ef19872b4fdb76317ef9d073be` | `21f37a2c4579874d645bb03aeed40bcf923d4642` | No | F5 (Remote Seed) area |
| `h3_reliability.py` | `be0734b2857472043d2200647adecfdb69f6fd1b` | `fa24f1c3f16bc1f064eba85fe5e31a10160c9e0b` | No | F5/P3 area |
| `h3_budget.py` | `d4208b3a08130aee090e047185f6ec31c4440f9f` | `9d5b9dceb8ac82fa52bbf536761601d822624f7f` | No | Upstream budget module differs — Muse keeps 30 / 25 / 0.75 / 14400; any change needs its own PR + config mirrors, never a silent port |
| `tests/test_drive_depot.py` | `a0f365dcbe388df332e5cf49fd9f11034f2fa5a2` | `a0f365dcbe388df332e5cf49fd9f11034f2fa5a2` | Yes | Unchanged upstream |

## Implemented features (Stage 3, v0.2.0)

Each row is a Muse-original reimplementation from the upstream
behaviour spec (CHANGELOG text only). No upstream source was copied
in any of them.

| Feature | Muse PR / commit | Muse modules changed | Tests | CI | Live verification |
|---|---|---|---|---|---|
| F1 zero-copy high-concurrency Drive download (pwrite + bitmap resume) | PR #4 / `2f3c425` (Issue #3) | `h3_drive_download.py` (preallocated single `.part`, `os.pwrite`, 12 workers, global connection cap 16, ordered streaming SHA-256, `.part.bitmap.json` resume, abort, single-flight refresh); `tests/test_drive_download.py` (constants + retired shard-merge test) | `tests/test_drive_download_v2.py` (17 new tests) | Run 44662119661, 6/6 pass (2026-11-20) | NOT_VERIFIED |
| F2 offline ComfyUI wheelhouse cache | PR #6 / `fd61ece` (Issue #5) | `h3_comfyui_cache.py` (lazy Drive imports, pure-stdlib helpers, deterministic `comfyui-wheelhouse-<commit>-<pytag>.tar.gz` build/publish/verify, `verify_wheelhouse_on_drive`); `h3_controller.py` (optional `drive_download["wheelhouses"]`) | `tests/test_comfyui_wheelhouse.py` (19 new tests) | CI run pending at PR creation (local suite 763 tests, 0 failures) | NOT_VERIFIED |
| F3 dual-track parallel setup + wheelhouse-first install | PR #8 / `2364408` (Issue #7) | `h3_remote_worker.py` (run_setup_parallel ThreadPoolExecutor max_workers=2, shared abort_event via progress callback, thread-safe token refresh, thread-local cmd-failure record, SetupTrackFailed attribution; wheelhouse-first pip install with WHEELHOUSE_FALLBACK_PYPI, safe tar extract + META verify); `tests/test_h3_comfyui_cache.py` (TestGitFailureRecord updated to _cmd_failure_get) | `tests/test_remote_worker_v020.py` (25 new tests) | CI run pending at PR creation (local suite 818 tests, 0 failures) | NOT_VERIFIED |
| F4 independent NVMe model store + high-VRAM flags | integration/v0.3.0, Issue #9 (no separate PR; merged via the final v0.3.0 integration PR) | `h3_remote_worker.py` (nvme_models_dir H3_NVME_MODELS_DIR, link_models_into_comfy atomic symlink-in with require_inside + readability + size/SHA-256 checks, stale-link replace, wrong-real-file fail-closed, _lexical_inside dst check; restore_weights restores into the store then links with verify=False under an in-run verification contract; detect_gpu_vram_mib silent nvidia-smi probe, comfy_vram_flags --highvram --disable-mmap at >= 76800 MiB, H3_COMFY_VRAM_FLAGS override; COMFY_VRAM_FLAGS print on launch) | `tests/test_remote_worker_v020.py` (19 new tests: NvmeStoreTest 11, VramFlagsTest 5, RestoreWeightsNvmeTest 3) | local suite 878 tests, 0 failures (integration branch) | NOT_VERIFIED |
| F5 in-cloud remote seeding + cold-start telemetry | integration/v0.3.0, Issue #10 (no separate PR; merged via the final v0.3.0 integration PR) | `drive_depot.py` (remote-seed action triple-gated --allow-drive-write/--allow-large-transfer/--confirm-remote-seed; RemoteSeedRunner via allocation_command G4 + watchdog record/confirm + _colab_run_cli; short-lived drive.file token as 0600 file; generated in-cloud script: 8-way Range download + SHA-256 + Drive upload with reuse-never-overwrite; _commit_ready_manifest shared with seed(); stop + tristate STOPPED verify in finally, REMOTE_SEED_ORPHAN_RISK on unconfirmed stop); `h3_remote_worker.py` (_SETUP_TRACK_TIMINGS/_WHEELHOUSE_HIT/_RESTORE_COUNTS, build/emit_coldstart_metrics, COLDSTART_METRICS_JSON + job-state persistence); `h3_run_manifest.py` (validate_coldstart_metrics, update_run coldstart_metrics); `h3_controller.py` (parse_coldstart_metrics, _record_coldstart_metrics via _manifest_worker_state); `h3_reliability.py` (coldstart section: setup_seconds P50/P90 + wheelhouse_hit_rate) | `tests/test_drive_depot_remote_seed.py` (12 new tests), `tests/test_remote_worker_v020.py` (6 new F5 tests), `tests/test_controller_wheelhouse_telemetry.py` (15 new tests), `tests/test_reliability_coldstart.py` (6 new tests) | local suite 917 tests, 0 failures (integration branch) | NOT_VERIFIED |
| P1 Drive upload MD5 hex alignment | integration/v0.3.0, Issue #11 (no separate PR; merged via the final v0.3.0 integration PR) | `h3_remote_worker.py` (_md5_b64 replaced by _md5_hex; validate_assembled_mp4 and the upload_remote_archive evidence now carry 32-char lowercase hex MD5 matching Drive v3 md5Checksum; unused base64 import dropped); `h3_drive_upload.py` (_normalize_md5 verified, not re-implemented: hex case-insensitively + 24-char base64 16-byte digests decoded to hex, fails closed on missing server md5Checksum; docstring updated) | `tests/test_drive_upload.py` (NormalizeMd5Tests 4 + missing-server-MD5 fail-closed + no-MD5-requested pass; WorkerUploadTests evidence now expects hex), `tests/test_remote_worker_v020.py` (Md5HexTest 2) | local suite 925 tests, 0 failures (integration branch) | NOT_VERIFIED |
| P2 headless wheelhouse exclusions + py313 | integration/v0.3.0, Issue #12 (no separate PR; merged via the final v0.3.0 integration PR) | `h3_comfyui_cache.py` (SUPPORTED_PYTHON_TAGS += py313; HEADLESS_OPTIONAL_PACKAGES/PREFIXES filtered in filter_requirements_lines, comfyui-frontend-package kept; verify() remote-only server-metadata branch); `h3_remote_worker.py` (HEADLESS_EXCLUDED_PACKAGES/PREFIXES, log_requirements_status excluded bucket, pip_install_needed threading, _filter_requirements_for_wheelhouse + filtered offline install in _install_requirements, original file for the PyPI fallback) | `tests/test_comfyui_wheelhouse.py` (filter exclusions, py313 archive/entry, verify() remote-only x3), `tests/test_h3_comfyui_cache.py` (excluded bucket, only-headless means no pip), `tests/test_remote_worker_v020.py` (filtered offline install, fallback to original) | local suite 934 tests, 0 failures (integration branch) | NOT_VERIFIED (no py313 bundle built or published) |
| P3 monotonic MODELS_READY backfill + reliability calibration | integration/v0.3.0, Issue #13 (no separate PR; merged via the final v0.3.0 integration PR) | `h3_controller.py` (_STAGES_IMPLYING_MODELS_READY, _backfill_models_ready_if_needed via TASK_STAGES order, called from _manifest_worker_state on later stages or coldstart_metrics and from _record_coldstart_metrics, which also records a COLDSTART_METRICS audit event via record_audit_event); `h3_reliability.py` (_has_models_ready_evidence: explicit stage, COLDSTART_METRICS audit event, or monotonic WORKER_STARTED -> INFERENCE_COMPLETED -> VIDEO_VALIDATED chain; _evaluate_run_f condition (2) uses it) | `tests/test_controller_wheelhouse_telemetry.py` (ModelsReadyBackfillTest 6), `tests/test_reliability_p3.py` (7 new tests), `tests/test_fault_injection.py` (baseline chain now records the backfilled MODELS_READY) | local suite 947 tests, 0 failures (integration branch) | NOT_VERIFIED |
| M1 GPU 503 elastic backoff & error classification | integration/v0.3.0, Issue #14 (no separate PR; merged via the final v0.3.0 integration PR) | `h3_budget.py` (classify_allocation_error: AUTH_FATAL / QUOTA_EXCEEDED / CAPACITY_TRANSIENT / TRANSPORT_TRANSIENT / UNKNOWN_FATAL; compute_backoff_delay 15s/45s/90s ±20% jitter, later rounds reuse 90s); `h3_controller.py` (resolve_allocation_rounds: default 3 for deliver/--auto-recover, 1 for raw run, 1..5; outer rounds wrap the inner GPU_ATTEMPT_PLAN; capacity exhausts the inner plan then backs off unless the task deadline leaves less than MIN_ATTEMPT_WALL_SEC + DEADLINE_MARGIN_SEC after the delay; fatal/quota/unknown fail fast; transport retries the same G4 once, never A100; GPU_ALLOCATION_BACKOFF in both audit logs; failed attempts mark lease + watchdog record stopped immediately; sleep/jitter injectable via args); `scripts/run_h3_task.sh` (--allocation-rounds passthrough, 1..5 validated) | `tests/test_v030_m1_backoff.py` (21 new tests) | local suite 968 tests, 0 failures (integration branch) | NOT_VERIFIED |

## Legacy repository — `LEGACY_REPOSITORY_NOT_ACCESSIBLE`

Recorded 2026-11-20 (user decision, TASK-VERSION-ROADMAP-011
review): `thewheeler1118/colab-workstation` returns 404 from the
GitHub API (re-verified 2026-11-20). Recorded as
`LEGACY_REPOSITORY_NOT_ACCESSIBLE`. No deletion reason is assumed,
no recovery is attempted, and the old repository is not a
development source for this project — all work continues from
`thewheeler1118/Muse-Colab-Workstation` (`61aa33a9` baseline).

## Security review status key

- `PASS` — reviewed, no issue, with evidence.
- `PENDING` — not yet reviewed; the feature PR must review it before merge.
- `REJECTED` — will not be ported; reason recorded in the row.
- `Reviewed` — read for the reject/retain decision; no code copied.

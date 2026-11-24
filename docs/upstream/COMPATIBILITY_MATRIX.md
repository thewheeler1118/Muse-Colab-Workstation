# Antigravity → Muse version porting matrix

Locked refs: `docs/upstream/UPSTREAM_LOCK.json`.
Ledger: `docs/upstream/PORTING_LEDGER.md`.
License verdict: **no license found upstream — verbatim copying is
NOT permitted** (see `adapters/antigravity/COMPATIBILITY.md`).
Porting mode is therefore behavioral specification + Muse-original
reimplementation, per module, with tests.

## Version map

| Muse | Upstream | Upstream commit | Files (upstream) | Status |
|---|---|---|---|---|
| v0.1.0 | — (baseline) | `61aa33a9` (own root commit) | 209 | RELEASED 2026-11-20 — [Release](https://github.com/thewheeler1118/Muse-Colab-Workstation/releases/tag/v0.1.0), tag `v0.1.0`, CI 6/6 on run 43782755112 |
| v0.1.0 + adapter (Stage 1) | — | `61aa33a9` + `feat/muse-adapter` | 209 + adapter files | MERGED — in main since the v0.3.0 release (PR #18); adapter unchanged by v0.3.1 |
| v0.2.0 (integrated) | v0.2.0 | `46a6a6d6` | 223 | INTEGRATED — F1–F5 merged via PR #18 (`0e3c114`, 2026-11-20); OFFLINE_VERIFIED (1046 tests at v0.3.0); live NOT_OBSERVED |
| v0.2.1 (integrated) | v0.2.1 | `f043ebd0` | 223 | INTEGRATED — P1–P3 merged via PR #18; OFFLINE_VERIFIED; live NOT_OBSERVED |
| v0.3.0 (integrated) | v0.3.0 | `2fa7ba48` | 232 | INTEGRATED — M1–M4 merged via PR #18; OFFLINE_VERIFIED; live NOT_OBSERVED (only in-repo live record is the FAILED v0.3.0 E2E run) |
| v0.3.1 (Muse) | v0.3.1 | `418797a4` | 235 | REFERENCE ONLY — R5 registers the tag (P0-1/P0-2/P1-3~P1-6); P0-2 adapted behaviourally (R5: `init` mapping + `read_job_state` tightening + Muse-original AST gate), the rest not imported; `LICENSE` Apache-2.0 at tag (see `PORTING_LEDGER.md`) |

## File inventory (verified 2026-11-20, names only)

Files present upstream but absent in Muse v0.1.0:

**v0.2.0 (+14 vs Muse):**

- `.agents/hooks.json`, `.agents/pre_tool_guard.py`,
  `.agents/skills/colab-h3-video/SKILL.md` — NOT copied as Muse
  config (see COMPATIBILITY.md).
- `config/tasks/shao_nian_v1_A.json`,
  `config/tasks/shao_nian_v1_B.json` — real TaskSpecs, NEVER copied.
- `docs/runbooks/antigravity-cross-project-integration.md`
- `scripts/_colab_env.sh`, `scripts/install_macos_guardian.sh`,
  `scripts/run_shutdown_guardian.py`,
  `scripts/setup_isolated_account.py` — macOS guardian scripts are
  out of scope under the North Star (no Mac runtime dependency).
- `tests/test_comfyui_wheelhouse.py`,
  `tests/test_controller_wheelhouse_telemetry.py`,
  `tests/test_drive_depot_remote_seed.py`,
  `tests/test_drive_download_v2.py`,
  `tests/test_reliability_coldstart.py`,
  `tests/test_remote_worker_v020.py`

**v0.3.0 (additional +9 vs v0.2.0):**

- `h3_bootstrap_stage1.py`, `h3_bootstrap_stage2.py`,
  `h3_warm_health_probe.py`
- `tests/test_v030_m1_backoff.py`,
  `tests/test_v030_m2_bundled_bootstrap.py`,
  `tests/test_v030_m3_self_healing.py`,
  `tests/test_v030_m4_controller_worker.py`,
  `tests/test_v030_m4_warm_infra.py`,
  `tests/test_v030_scripts_guard.py`

**v0.3.1 (additional +3 vs v0.3.0):**

- `LICENSE` (standard Apache-2.0, `Copyright 2026 SkyOneBranch`)
- `tests/test_stage_contract_ast.py` (P0-2 static AST gate — behaviour
  reference for R5's Muse-original gate; no code copied)
- `tests/test_v031_hardening.py` (v0.3.1 hardening tests — reference only)

Files present in Muse v0.1.0 but absent upstream (must be retained):

- `scripts/manifest_policy.py`
- `tests/test_manifest_security.py`

## Per-version diff scale vs Muse v0.1.0 (verified 2026-11-20)

Detailed analysis: `upstream-analysis.md` + `porting-inventory.json`
(73 entries) in the task work area
(`~/workspace/version-roadmap-011/upstream/analysis/`, with raw
diffs under `raw/`). Step diffs inside upstream: v0.2.0→v0.2.1 =
14 files (+495/−69); v0.2.1→v0.3.0 = 28 files (+4,767/−457).
Nothing changed-then-reverted: the 73-file union equals the v0.3.0
set. Test counts are `def test` greps (Muse baseline 746; upstream
v0.3.0 counts 874 by this method vs the 872 figure in the task
brief — a counting-method difference, not a finding).

| Upstream tag | Added | Deleted | Modified | Changed | Tree files | Ins / Del vs Muse | `def test` count |
|---|---|---|---|---|---|---|---|
| v0.2.0 | 16 | 2 | 42 | 60 | 223 | +8,150 / −1,472 | 810 |
| v0.2.1 | 16 | 2 | 45 | 63 | 223 | +8,606 / −1,502 | 816 |
| v0.3.0 | 25 | 2 | 46 | 73 | 232 | +13,253 / −1,839 | 874 |

## Feature map (from each tag's CHANGELOG + diffstats)

| Feature | Upstream version | Key files + diffstat vs Muse v0.1.0 | Muse status |
|---|---|---|---|
| F1 high-concurrency zero-merge download | v0.2.0 | `h3_drive_download.py` (+902/−350, frozen after v0.2.0); `tests/test_drive_download_v2.py`, `tests/test_drive_download.py` | IMPLEMENTED — PR #4 branch work (`2f3c425`) re-landed via PR #18 (`0e3c114`); CI run 44662119661 6/6 pass, 760 tests 0 failures; live NOT_OBSERVED |
| F2 ComfyUI offline wheelhouse | v0.2.0 | `h3_comfyui_cache.py` (+833/−45); `scripts/setup_isolated_account.py`; controller wiring; `tests/test_comfyui_wheelhouse.py`, `tests/test_controller_wheelhouse_telemetry.py` | IMPLEMENTED — PR #6 branch work (`fd61ece`) re-landed via PR #18 (`0e3c114`); 763 tests 0 failures locally; `setup_isolated_account.py` NOT-APPLICABLE (no Muse equivalent); live NOT_OBSERVED |
| F3 parallel install + model restore | v0.2.0 | `h3_remote_worker.py` (+756/−133, shared with F4); `tests/test_remote_worker_v020.py` (CHANGELOG says `test_remote_worker_v2.py`, shipped file is `…_v020.py`) | IMPLEMENTED — PR #8 branch work re-landed via PR #18 (`0e3c114`); OFFLINE_VERIFIED (818 tests at PR time, 1046 at v0.3.0); live NOT_OBSERVED (observed failing live in run `h3-20261120T071209Z-4ea108`, fixed by P0-A in PR #20) |
| F4 standalone NVMe model storage + VRAM optimization | v0.2.0 | `h3_remote_worker.py` (+756/−133, shared with F3); `tests/test_remote_worker_v020.py` | IMPLEMENTED — merged via PR #18 (Issue #9); OFFLINE_VERIFIED (878 tests at integration); live NOT_OBSERVED |
| F5 Remote Seed + cold-start telemetry | v0.2.0 | `drive_depot.py` (+572/−6); `h3_run_manifest.py`; `scripts/reliability_report.py`; `tests/test_drive_depot_remote_seed.py`, `tests/test_reliability_coldstart.py`; triple-flag gated | IMPLEMENTED — merged via PR #18 (Issue #10); OFFLINE_VERIFIED (917 tests); Remote Seed stays optional (triple-gated), no live GPU without separate approval; live NOT_OBSERVED |
| P1 MD5 normalization (compare first) | v0.2.1 | `h3_remote_worker.py`, `h3_drive_upload.py`, `tests/test_drive_upload.py` | IMPLEMENTED — merged via PR #18 (Issue #11); Muse `_normalize_md5` verified (PR #62 lineage), worker `_md5_hex` added; OFFLINE_VERIFIED (925 tests); live NOT_OBSERVED |
| P2 Python 3.13 + slim offline wheelhouse | v0.2.1 | `h3_comfyui_cache.py` (+36/−7); `h3_remote_worker.py`; `py313` handled via generic tag normalization, no hardcoded list | IMPLEMENTED — merged via PR #18 (Issue #12); OFFLINE_VERIFIED (934 tests); no py313 bundle built or published; live NOT_OBSERVED |
| P3 monotonic `MODELS_READY` backfill + reliability stats fix | v0.2.1 | `h3_controller.py`, `h3_reliability.py`, `tests/test_reliability.py`, `tests/test_fault_injection.py` | IMPLEMENTED — merged via PR #18 (Issue #13); OFFLINE_VERIFIED (947 tests); live NOT_OBSERVED |
| M1 GPU 503 capacity backoff + error classification | v0.3.0 | `h3_budget.py` (+75/−0); `h3_controller.py`; `scripts/run_h3_task.sh`; `tests/test_v030_m1_backoff.py`; classes `AUTH_FATAL` / `QUOTA_EXCEEDED` / `CAPACITY_TRANSIENT` (503/429/502/504/412) / `TRANSPORT_TRANSIENT` / `UNKNOWN_FATAL`; backoff 15s/45s/90s ±20% | IMPLEMENTED — merged via PR #18 (Issue #14); OFFLINE_VERIFIED (968 tests); Fatal/Auth/Quota fail fast; live NOT_OBSERVED (capacity behaviour observed in the failed v0.3.0 E2E run, not verified) |
| M2 two-stage bundled bootstrap | v0.3.0 | `h3_bootstrap_stage1.py`, `h3_bootstrap_stage2.py` (new); `tests/test_v030_m2_bundled_bootstrap.py`; tar members validated pre-extraction (absolute paths, `..`, symlinks/hardlinks, device nodes, escape from `/content/muse_h3` rejected); 0600 downscoped tokens; `MUSE_DRIVE_DOWNLOAD_PROBE_OK` gate | IMPLEMENTED — merged via PR #18 (Issue #15); OFFLINE_VERIFIED (986 tests); strict archive-entry rejection enforced; live NOT_OBSERVED |
| M3 network-drop self-healing + Drive 0-GPU finished-video recovery | v0.3.0 | `h3_controller.py`; `h3_drive_token.py` (+47/−0, `ensure_token_freshness(min_remaining_sec=900)`); `tests/test_v030_m3_self_healing.py` | IMPLEMENTED — merged via PR #18 (Issue #16); OFFLINE_VERIFIED (1003 tests); finished video recovers first, never blind re-inference; live NOT_OBSERVED |
| M4 controlled warm pool | v0.3.0 | `h3_controller.py` (largest v0.3.0 change, +1,264/−279); `h3_gpu_lease_registry.py` (+165/−1); `h3_shutdown_executor.py`; `h3_warm_health_probe.py` (new); `scripts/run_shutdown_guardian.py`; `scripts/verify_shutdown.sh`; 3 test suites; default OFF (`--keep-warm-ttl` default 0, allowed 0 or 60–900s); caps 14400s / 25.0 CU / 30.0 CU / 0.75 CU reserve; reuse requires `MUSE_WARM_HEALTH_OK` | IMPLEMENTED — merged via PR #18 (Issue #17, closed 2026-11-20); OFFLINE_VERIFIED (1046 tests); default OFF; no live warm pool without separate approval; live NOT_OBSERVED |

Note on upstream performance claims: the v0.2.1 CHANGELOG cites a
live A100 run (489s→230.9s, 4.44→3.70 CU). That is upstream's claim,
not Muse-verified; treat it as TARGET / claimed MEASURED until a Muse
GPU run with a run id proves it.

## Verification status key

- `OFFLINE_VERIFIED` — offline tests + CI green, no GPU.
- `LIVE_E2E_VERIFIED` — real GPU end-to-end with a run id.
- `NOT_OBSERVED` — no in-repo live run record for the feature
  (neither verified nor refuted live). No v0.2.0–v0.3.1 feature is
  `LIVE_E2E_VERIFIED` in this repo: no successful live E2E run is
  recorded in-repo at this baseline (CHANGELOG v0.3.1:
  `LIVE_E2E_NOT_YET_RETESTED`). The only in-repo live E2E record
  is the FAILED v0.3.0 run `h3-20261120T071209Z-4ea108`
  (TASK-LIVE-E2E-015), which observed M1/F3 failing live. The ≥95%
  reliability target is a TARGET, never a proven result, until a
  real GPU run proves it.

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
| v0.1.0 + adapter (Stage 1) | — | `61aa33a9` + `feat/muse-adapter` | 209 + adapter files | IN PROGRESS — this branch |
| v0.2.0 (planned) | v0.2.0 | `46a6a6d6` | 223 | NOT STARTED — F1–F5, each its own PR |
| v0.2.1 (planned) | v0.2.1 | `f043ebd0` | 223 | NOT STARTED — P1–P3 |
| v0.3.0 (planned) | v0.3.0 | `2fa7ba48` | 232 | NOT STARTED — M1–M4 |

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

Files present in Muse v0.1.0 but absent upstream (must be retained):

- `scripts/manifest_policy.py`
- `tests/test_manifest_security.py`

## Feature map (per-feature file mapping: detailed analysis in progress)

| Feature | Upstream version | Upstream new tests (known) | Muse status |
|---|---|---|---|
| F1 high-concurrency zero-merge download | v0.2.0 | `test_drive_download_v2.py`, `test_remote_worker_v020.py` | SPEC PENDING — detailed diff analysis in progress |
| F2 ComfyUI offline wheelhouse | v0.2.0 | `test_comfyui_wheelhouse.py`, `test_controller_wheelhouse_telemetry.py` | SPEC PENDING |
| F3 parallel install + model restore | v0.2.0 | `test_remote_worker_v020.py` | SPEC PENDING |
| F4 standalone NVMe model storage + VRAM optimization | v0.2.0 | `test_remote_worker_v020.py` | SPEC PENDING |
| F5 Remote Seed + cold-start telemetry | v0.2.0 | `test_drive_depot_remote_seed.py`, `test_reliability_coldstart.py` | SPEC PENDING — Remote Seed stays optional, no live GPU without separate approval |
| P1 MD5 normalization (compare first) | v0.2.1 | — | SPEC PENDING — Muse already has MD5 normalization (PR #62); expect test-only work |
| P2 Python 3.13 + slim offline wheelhouse | v0.2.1 | — | SPEC PENDING |
| P3 monotonic `MODELS_READY` backfill + reliability stats fix | v0.2.1 | — | SPEC PENDING |
| M1 GPU 503 capacity backoff + error classification | v0.3.0 | `test_v030_m1_backoff.py` | SPEC PENDING — Fatal/Auth/Quota must never retry infinitely |
| M2 two-stage bundled bootstrap | v0.3.0 | `test_v030_m2_bundled_bootstrap.py` | SPEC PENDING — strict archive-entry rejection required |
| M3 network-drop self-healing + Drive 0-GPU finished-video recovery | v0.3.0 | `test_v030_m3_self_healing.py` | SPEC PENDING — finished video recovers first, never blind re-inference |
| M4 controlled warm pool | v0.3.0 | `test_v030_m4_controller_worker.py`, `test_v030_m4_warm_infra.py`, `test_v030_scripts_guard.py` | SPEC PENDING — default OFF, 25 CU / 14400 s / TTL bounds, guardian unverified until proven |

## Verification status key

- `OFFLINE_VERIFIED` — offline tests + CI green, no GPU.
- `LIVE_E2E_VERIFIED` — real GPU end-to-end with a run id.
- `NOT_VERIFIED` — everything else. No v0.2.0–v0.3.0 feature is
  `LIVE_E2E_VERIFIED` in this repo; the ≥95% reliability target is a
  TARGET, never a proven result, until a real GPU run proves it.

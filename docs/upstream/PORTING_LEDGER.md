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
| `scripts/setup_isolated_account.py` | `e29699f03b16c696bb0402470ada0b47f0155fb6` | TBD (F-stage) | No | SPEC PENDING — isolated-account setup touches credentials by design; any Muse version needs its own security review before any work | TBD | PENDING — high-risk area |
| `tests/test_comfyui_wheelhouse.py` | `89ff7f3d92118a74f86a932508975a646c640f27` | TBD (F2) | No | SPEC PENDING — test intent may be reimplemented in Muse-original tests; test files are not copied verbatim without a license | TBD | PENDING |
| `tests/test_controller_wheelhouse_telemetry.py` | `994e4ba816a3bd909953f596c38f9d00fe97cfc1` | TBD (F2/F5) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_drive_depot_remote_seed.py` | `8063669e320785ae63c522fbfdef2ca5f3dcbca4` | TBD (F5) | No | SPEC PENDING — Remote Seed stays optional; no live GPU test without separate approval | TBD | PENDING |
| `tests/test_drive_download_v2.py` | `8ea33c16390b0237f1a3231a111cb362c8e3cf23` | TBD (F1) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_reliability_coldstart.py` | `3cb19ac48697ccb840687a02d8419930d06a568b` | TBD (F5) | No | SPEC PENDING | TBD | PENDING |
| `tests/test_remote_worker_v020.py` | `38ea495e40be5f52569d337512df420e5f5bfd84` | TBD (F1/F3/F4) | No | SPEC PENDING | TBD | PENDING |

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

## Security review status key

- `PASS` — reviewed, no issue, with evidence.
- `PENDING` — not yet reviewed; the feature PR must review it before merge.
- `REJECTED` — will not be ported; reason recorded in the row.
- `Reviewed` — read for the reject/retain decision; no code copied.

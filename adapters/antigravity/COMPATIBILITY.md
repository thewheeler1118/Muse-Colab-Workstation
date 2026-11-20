# Antigravity compatibility — what transfers, what does not

Upstream reference: `SkyOneBranch/Antigravity-Colab-Workstation`
(see `docs/upstream/UPSTREAM_LOCK.json` for locked SHAs).

## License verdict — read first

**No license was found in the Antigravity repository.** Checked
2026-11-20 against `v0.3.0`
(`2fa7ba48c55614ccca157880e6b90d20f468f871`): no `LICENSE` /
`COPYING` / `NOTICE` file in the tree, no license grant in
`README.md`, `AGENTS.md`, or `CHANGELOG.md`, and the GitHub license
API returns `license: null` for the repository.

Consequence (task constraint 12): **no Antigravity source file may
be copied verbatim into this repository.** Porting proceeds as
behavioral specification + Muse-original reimplementation, with every
module recorded in `docs/upstream/PORTING_LEDGER.md` as
`verbatim_allowed: false`. If the upstream author adds an explicit
open-source license (or grants written permission), the ledger can be
revised — per module, with the grant recorded — and only then may a
verbatim copy be considered.

## Antigravity `.agents/hooks.json` is NOT a Muse hook

Antigravity ships `.agents/hooks.json` +
`.agents/pre_tool_guard.py` (a `PreToolUse` guard that blocks secret
inspection and unguarded `colab` CLI calls while permitting audited
wrapper commands). That file is an Antigravity-agent configuration.
Muse does not load `.agents/hooks.json`, and copying it into this
repo and claiming it enforces anything would be false.

What Muse actually provides (verified against this platform, not
assumed from upstream):

| Capability | Antigravity mechanism | Muse equivalent (verified) |
|---|---|---|
| Block unguarded GPU creation | `PreToolUse` hook on `colab` CLI | `scripts/run_h3_task.sh` refuses without `--confirm-live`; `h3_controller.py` authorization gates; no bare `colab new` path in the adapter |
| Block secret inspection / leakage | `pre_tool_guard.py` + secret scan | `scripts/secret_scan.py` (CI) + `scripts/manifest_policy.py` deny gate + `adapters/muse/redact.py` at runtime |
| Background shutdown watch | `scripts/run_shutdown_guardian.py` + macOS install script | NOT ported as-is: the macOS guardian is out of scope under the North Star (no Mac runtime dependency). Shutdown enforcement is `h3_shutdown_executor.py` + the lease registry; an independent shutdown cron remains a separate user-approved decision, not an adapter default |
| Agent skill loading | `.agents/skills/colab-h3-video/SKILL.md` | Muse skills live in the Muse skill system (`~/workspace/custom-skills/`, system skills), not in `.agents/skills/`; no skill file is copied here |
| Scheduled / recurring work | (upstream cron/guardian scripts) | Muse cron/hook runtime (scheduled jobs), only when the user approves a specific job |

## Shared-core principle

The shared production core stays as-is (flat-root Python modules,
existing import style). Platform differences live only in
`adapters/muse/` (and this file). No H3 module branches on "Muse vs
Antigravity".

## Files that must never be copied from upstream

- `.agents/hooks.json` as a Muse-native hook (see above).
- `config/tasks/shao_nian_v1_A.json`, `config/tasks/shao_nian_v1_B.json`
  — real user TaskSpecs, not templates.
- `config/tasks/` contents generally (real run task data).
- The old manifest generator path (Muse keeps
  `scripts/manifest_policy.py` + `tests/test_manifest_security.py`).
- Any doc/config containing local absolute paths.
- OAuth credentials, run ledgers, logs, model weights, video.
- Anything conflicting with Muse's security policy (see repo
  `AGENTS.md` rules 1–7).

## Files that must be retained in Muse

- `scripts/manifest_policy.py` (git-tracked list + sensitive-path
  deny gate, symlink refusal).
- `tests/test_manifest_security.py` (regression tests for the above).
- Sensitive-directory refusal, git-tracked file range limit,
  symlink safety check — all in the two files above.

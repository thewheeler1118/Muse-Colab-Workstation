# Muse Adapter

Thin, Muse-original adaptation layer over the shared H3 production
core. It exists so that platform differences live in exactly one
place; the production modules (`h3_controller.py`,
`h3_remote_worker.py`, `h3_budget.py`, `h3_drive_*`,
`h3_shutdown_executor.py`, …) never branch on "which agent is
running me".

## What the adapter does

1. Maps a user's task to an existing `TaskSpec` — natural language
   is accepted only as the `prompt` (or `shots`) string; refs, seed,
   dimensions and frame counts must be explicit and are validated
   fail-closed by `TaskSpec.from_dict` (`adapters/muse/adapter.py:
   build_task_spec`).
2. Calls the standard preflight — the adapter never replaces it.
   Preflight lives in `h3_controller.preflight` and
   `h3_environment.verify_environment`; the adapter's job is to make
   sure it runs (via `scripts/preflight.sh` / the controller) before
   any live call, at 0 CU.
3. Maps an explicit authorization to managed wrapper parameters —
   `Authorization` → `scripts/run_h3_task.sh` args
   (`--confirm-live`, `--production-task-id`, `--task-spec`,
   `--deliver`, `--max-task-attempts 1–3`, optional
   `--authorize-new-task` / `--task-deadline-utc` / `--run-id`).
   Without `confirm_live=True` the mapping refuses.
4. Handles Muse conversation messages and status reports
   (`status_report`): budget constants, run id, delivered MP4
   reference, shutdown result. All free text is redacted first.
5. Locates the correct production directory and external task files
   (`adapters/muse/paths.py`): the production directory must be
   supplied explicitly and must contain `RELEASE_COMMIT`; the
   adapter never guesses it and never writes to it. Task-spec files
   must be external JSON files, not symlinks, and never under the
   repo's `config/tasks/` (denied by manifest policy).
6. Reports budget, run id, MP4 and shutdown results (see 4).
7. On Muse agent restart, checks existing task state FIRST
   (`check_existing_task_state`): read-only scan of
   `model_cache/run_manifests/*.json` for the task id. It launches
   nothing and resumes nothing; the newest found state must be
   reviewed with the user before any new attempt.
8. Records approvals and audit results, but never writes a
   credential value to a log (`adapters/muse/redact.py`).

## What the adapter does NOT do

- Call bare `colab new` to create a GPU.
- Rewrite the H3 worker.
- Implement a second GPU budget ledger (constants are asserted from
  `h3_budget.py`: 30.00 CU/task, 25.00 CU/session, 0.75 CU cleanup
  reserve, 14400 s max session).
- Implement a second Drive upload protocol.
- Replace `h3_shutdown_executor.py`.
- Bypass the controller's production authorization checks.

## Enforcement lives outside the prompt

Muse has no equivalent of Antigravity's `.agents/hooks.json`
PreToolUse hook file, and this adapter does not pretend otherwise
(see `adapters/antigravity/COMPATIBILITY.md`). The real gates are:

- `scripts/run_h3_task.sh` — refuses without `--confirm-live`;
- `h3_controller.py` — budget / authorization gates, fail-closed;
- `scripts/manifest_policy.py` + `scripts/secret_scan.py` —
  sensitive paths refused, credentials never in git;
- file permissions on the production directory and credential files.

A conversation prompt alone enforces nothing.

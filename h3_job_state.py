"""TASK-005 §4.2 (Issue #27): durable remote job state.

The worker writes this file on the Colab VM after every phase, so the
controller can poll a short-lived `colab exec` probe instead of holding
one long blocking exec as the only control channel (the 2026-11-12 B run
died exactly that way: the VM restarted, the blocking exec died with it,
and nobody could tell where the worker had got to).

The file is the worker's own claim about its stage; the controller never
infers a stage from log text. The local MP4 is still independently
verified (remote ffprobe check + local ffprobe validation) before any
success is reported — the state file says *where the worker is*, not
whether the video is good.

Atomic write (tmp + os.replace) so a probe never reads a torn file.
0600: the file carries run ids and paths only, never credentials, but
there is no reason to leave it world-readable on a shared VM.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

JOB_STATE_FILENAME = "MUSE_H3_JOB_STATE.json"

# Worker-internal stages, in order. The controller maps these onto the
# manifest stage chain (see h3_controller.JOB_STAGE_TO_MANIFEST_STAGE);
# only VIDEO_READY carries the done info, only FAILED carries an error.
JOB_STAGES = (
    "JOB_STARTED",
    "RESTORING_MODELS",
    "MODELS_READY",
    "INFERENCE_RUNNING",
    "INFERENCE_COMPLETED",
    "VIDEO_READY",
    "FAILED",
)

# The worker's own phase names (set_stage) map onto the job stages above.
# MODELS_READY is NOT set by the mapping: it is written explicitly only
# after object_info validation passes. INFERENCE_COMPLETED is written
# explicitly after find_rendered_video/rescue succeeds.
# "init" is the worker's _LAST_STAGE before the first set_stage call
# (TASK-RELEASE-V032 R5, closing the CHANGELOG v0.3.1 §5.2 residual
# note): a token refresh before the first set_stage rewrites the job
# state with the mapped stage JOB_STARTED instead of leaking the raw
# phase "init", which the controller would refuse as unknown. This
# mirrors upstream v0.3.1 P0-2, which maps "init" the same way
# (upstream h3_job_state.py, tag v0.3.1); Muse has no
# "dual_track_setup" / "link_models" phases, so those upstream
# entries deliberately have no Muse counterpart.
WORKER_PHASE_TO_JOB_STAGE = {
    "init": "JOB_STARTED",
    # TASK-HOTFIX-016 (Issue #19) P0-A: the v0.3.0 F3 dual-track setup
    # phase. It is setup work, not a finished model restore, so it maps
    # to RESTORING_MODELS — exactly how upstream Antigravity's
    # equivalent phase ("dual_track_setup") is normalised to
    # RESTORING_MODELS by its _job_stage_for(). Without this entry the
    # raw phase leaked into the job state and the controller refused the
    # run as an unknown stage (TASK-LIVE-E2E-015 defect A).
    "setup_parallel": "RESTORING_MODELS",
    "ensure_comfy": "JOB_STARTED",
    "restore_weights": "RESTORING_MODELS",
    "comfyui_start": "RESTORING_MODELS",
    "object_info_validation": "RESTORING_MODELS",
    "inference_submit": "INFERENCE_RUNNING",
    "find_rendered_video": "INFERENCE_RUNNING",
    "rescue_rendered_video": "INFERENCE_RUNNING",
    "assemble_mp4": "INFERENCE_COMPLETED",
    # TASK-006 P1-A: the worker-side upload runs after assembly; the
    # inference is already complete, so all three upload stages map to
    # INFERENCE_COMPLETED on the job-state axis. The upload evidence
    # itself travels in the job state's `extra` field.
    "remote_upload": "INFERENCE_COMPLETED",
    "remote_upload_verified": "INFERENCE_COMPLETED",
    "remote_upload_failed": "INFERENCE_COMPLETED",
    "done": "VIDEO_READY",
}


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_job_state(root, run_id: str, variant: str, stage: str,
                    pid: int | None = None, output: dict | None = None,
                    error: dict | None = None, extra: dict | None = None) -> dict:
    """Atomically write the durable job state file. Never raises for a
    bad stage name: an unknown stage is recorded verbatim so a probe can
    report it instead of the worker dying on its own bookkeeping."""
    root = Path(root)
    state = {
        "run_id": run_id,
        "variant": variant,
        "stage": stage,
        "updated_at_utc": _utc_now(),
        "heartbeat_utc": _utc_now(),
        "pid": pid,
    }
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    if extra:
        state.update(extra)
    path = root / JOB_STATE_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return state


def read_job_state(root) -> dict:
    """Read and validate the job state file. Raises FileNotFoundError
    when the worker has not written one yet, ValueError on a corrupt,
    incomplete, or unknown-stage file — both are poll failures for the
    controller, never silent 'no worker' answers.

    TASK-RELEASE-V032 R5: a stage outside JOB_STAGES is rejected
    explicitly. The worker can only write a canonical stage (every
    set_stage/write_job_state site is pinned by the static AST gate
    in tests/test_v032_r5_stage_ast_gate.py), so an unknown stage on
    disk means a stale pre-hotfix file, a foreign writer, or a
    tampered file — never a legitimate worker state. write_job_state
    deliberately stays non-validating (worker bookkeeping must never
    kill an inference); the rejection happens here, at the read
    boundary, mirroring upstream v0.3.1 P0-2."""
    path = Path(root) / JOB_STATE_FILENAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise ValueError(f"Job state file is corrupt or unreadable: {e}") from e
    if not isinstance(state, dict):
        raise ValueError("Job state file is not a JSON object")
    for key in ("run_id", "variant", "stage"):
        if not state.get(key):
            raise ValueError(f"Job state file is missing required field: {key}")
    if state["stage"] not in JOB_STAGES:
        raise ValueError(
            f"Job state file carries an unknown stage: {state['stage']!r} "
            f"(not in JOB_STAGES {list(JOB_STAGES)})"
        )
    return state

"""TASK-005 §12 (Issue #40) + TASK-006 P1-C (Issue #50): reliability
metrics from durable evidence only.

The six rates, computed from run manifests, their audit logs, and
budget ledgers under a root directory. No rate is ever claimed from
unit tests, Mock tests, or fault-injection runs: those prove code paths,
not production reliability.

Definitions
-----------
- A first_attempt_success_rate: terminal runs reaching TASK_COMPLETED
  with recovery_generation == 0 / all terminal runs started.
- B eventual_task_success_rate: terminal runs reaching TASK_COMPLETED
  (including via resume or archive-only recovery) / all terminal runs
  started. This is the 95% target metric.
- C artifact_preservation_rate: terminal runs whose validated local MP4
  survived (delivery_state at least COMPLETE_LOCAL, and — when an
  output root is given — the local file still exists with the recorded
  SHA-256) / terminal runs that produced a validated local MP4.
- D verified_shutdown_rate: terminal runs with a session allocated
  whose audit log records a GPU_STOP_CONFIRMED stage / terminal runs
  with a session allocated. The audit event is the evidence: reaching
  DRIVE_VERIFIED via the stopped-session recovery path does NOT record
  GPU_STOP_CONFIRMED, and must not count.
- E budget_compliance_rate: runs whose task ledger reports
  max_observed_spent_cu <= project_budget_cu (30.00 CU) / runs whose
  task ledger exists. The ledger is per-task, so runs inherit their
  task's verdict; runs with no readable ledger are excluded from the
  denominator and listed separately.
- F deadline_bound_e2e_success_rate (TASK-006 P1-C, TASK-008): production
  TASKS (not runs) that complete end to end within their deadline /
  tasks authorised with a budget and a deadline whose deadline has
  passed or whose runs are all terminal. A task counts when (a) AT
  LEAST ONE run meets all seven conditions — (1) GPU allocation
  (session_id + GPU_ALLOCATED stage), (2) model restore+load
  (MODELS_READY stage), (3) spec-conforming MP4 (VIDEO_VALIDATED
  stage + local_sha256 + local_frames == the run's TaskSpec total
  frames, falling back to TOTAL_FRAMES for pre-TaskSpec manifests),
  (4) MP4 independently verified in persistent storage
  (DRIVE_VERIFIED stage + delivery_state DELIVERED + drive_file_id),
  (5) GPU confirmed stopped (GPU_STOP_CONFIRMED stage +
  session_state STOPPED), (6) CU within the authorised budget (ledger
  max_observed_spent_cu <= project_budget_cu), (7) final manifest +
  audit log complete (task_state TASK_COMPLETED + TASK_COMPLETED
  stage) — AND that run's TASK_COMPLETED stage timestamp is at or
  before the deadline, (b) EVERY other run that ever allocated a
  session_id is confirmed STOPPED with shutdown evidence (no orphan
  billable GPU), and (c) the ledger spend is within budget. A task
  that needed a retry still counts as a task success; Metric A
  (first-run success) keeps distinguishing first-attempt from
  retry success. Stuck/over-deadline tasks, lost
  artifacts, unconfirmed shutdowns, and budget violations are IN the
  denominator as failures, never silently removed. A task still
  in-flight before its deadline is excluded and listed separately;
  a task whose ledger has no deadline (including the historical
  h3-ab-20261111 ledger) stays OUT of the denominator and is listed
  separately. A task authorised with a deadline that never started
  a run is a denominator failure once the deadline has passed (no
  completion was recorded); before the deadline it is in-flight.

Rules
-----
- In-flight (non-terminal) runs are excluded from every denominator and
  reported separately: an unfinished run is neither a success nor a
  failure yet.
- denominator == 0 -> rate None, status UNKNOWN, with the reason named.
- 0 < denominator < 30 -> status INSUFFICIENT_SAMPLE. 30 is a
  conventional minimum for quoting a proportion as a reliability claim,
  not a statistical guarantee; the rate and its Wilson interval are
  still reported.
- denominator >= 30 -> status MEASURED.
- target_95_verified is True only when B is measured and its 95%
  Wilson lower bound is >= 0.95. With zero failures that needs about
  73 completed runs (60/60 only reaches a 0.9398 lower bound); any
  failure needs more. The module reports the interval; it never rounds
  a small-sample rate up to a claim. Metric F carries its own
  `target_verified` inside its dict, computed the same way (MEASURED
  and Wilson lower >= 0.95); the top-level `target_95_verified` keeps
  its metric-B meaning.
- Metric F is computed at task scope: a `since`/`until` time filter
  scopes the runs, so a task with no runs inside the window cannot be
  judged and is listed separately (`f_time_window_excluded_tasks`)
  instead of being counted as a failure.
- Metric F cross-checks the GPU lease registry
  (`model_cache/h3_gpu_leases.json`, TASK-RELEASE-V032 R3): for the
  same production_task_id (and its run_ids), any lease with real
  allocation evidence that is not confirmed STOPPED fails F — that
  includes warm_idle / WARM_POOLED sessions not yet stopped and
  recycled, and leases whose session_id was never written back to any
  manifest. The only carve-out is a lease already confirmed STOPPED:
  the P0-B pre-registration path marks a failed `colab new` STOPPED
  once the live-list check confirms no remote session exists, and
  that is not a leak. A missing or unreadable registry file is
  fail-safe, not a silent pass: F is judged from manifest/audit
  evidence only and the absence is recorded on every verdict
  (`lease_evidence`: "missing" / "corrupt") and on the metric
  (`lease_cross_check`).
- Every ratio metric carries both the legacy `confidence_interval_95`
  list and a `wilson_95_ci` dict {"lower", "upper"} (None when the
  denominator is 0): the same two-sided 95% Wilson interval, in both
  shapes, so existing readers keep working. The report also carries
  an `a_vs_f_comparison` side-by-side summary of metrics A and F
  (different denominators — runs vs tasks — never pooled), a
  `startup_mode_breakdown` (cold_start vs warm_reuse vs unknown, each
  with first-attempt and eventual rates), and the honest F 95%
  conclusion as `f_95_verified` / `f_95_status`
  ("F_95_VERIFIED" / "F_95_NOT_VERIFIED") / `f_95_evidence`. With zero
  failures a two-sided 95% Wilson lower bound >= 0.95 needs 73/73
  successes (73/73 -> 0.95; 60/60 reaches only 0.9398).

Current honest baseline (2026-11-13): the only real production runs
(A first-attempt success, B interrupted by a VM restart) predate the
TASK-005 manifests, so the production sample for the new architecture
is n = 0 and all six rates are UNKNOWN.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import h3_run_manifest as rm
from h3_budget import PROJECT_BUDGET_CU, ledger_path_for_task
from h3_contract import TOTAL_FRAMES

WILSON_Z = 1.96
TARGET_RATE = 0.95
MIN_SAMPLE_FOR_MEASURED = 30

# Delivery states at or after which a validated local MP4 exists.
_DELIVERY_AT_LEAST_LOCAL = {
    "COMPLETE_LOCAL",
    "DURABLE_UPLOAD_STARTED",
    "DURABLE_UPLOAD_VERIFIED",
    "DELIVERED",
}


def wilson_interval(k: int, n: int, z: float = WILSON_Z) -> list[float] | None:
    """95% Wilson score interval for k successes in n trials, or None."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, (centre - margin) / denom), 4),
            round(min(1.0, (centre + margin) / denom), 4)]


def wilson_95_ci(k: int, n: int, z: float = WILSON_Z) -> dict | None:
    """Two-sided 95% Wilson score interval as {"lower", "upper"}.

    The same interval `wilson_interval` returns as a list, in dict
    form (TASK-RELEASE-V032 R3). None when n <= 0: no sample, no
    interval. Known anchors: 73/73 -> {"lower": 0.95, "upper": 1.0}
    (the zero-failure threshold for a 0.95 lower bound); 60/60 ->
    lower 0.9398; 0/10 -> {"lower": 0.0, "upper": 0.2775}.
    """
    interval = wilson_interval(k, n, z)
    if interval is None:
        return None
    return {"lower": interval[0], "upper": interval[1]}


def _metric(k: int, n: int, definition: str,
            unknown_reason: str | None = None) -> dict:
    if n == 0:
        return {"definition": definition, "rate": None, "numerator": k,
                "denominator": 0, "confidence_interval_95": None,
                "wilson_95_ci": None,
                "status": "UNKNOWN",
                "unknown_reason": unknown_reason or "empty sample"}
    interval = wilson_interval(k, n)
    status = ("MEASURED" if n >= MIN_SAMPLE_FOR_MEASURED
              else "INSUFFICIENT_SAMPLE")
    return {"definition": definition, "rate": round(k / n, 4),
            "numerator": k, "denominator": n,
            "confidence_interval_95": interval,
            "wilson_95_ci": wilson_95_ci(k, n), "status": status,
            "unknown_reason": None}


def _percentile(sorted_values: list, q: float) -> float | None:
    """Linear-interpolation percentile over an already-sorted list
    (v0.2.0 F5 cold-start latencies). Returns None for an empty list.
    ``q`` is a fraction in [0, 1]."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return round(float(sorted_values[0]), 3)
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return round(float(sorted_values[lo]) + frac * float(sorted_values[hi] - sorted_values[lo]), 3)


def _audit_stages(root: Path, run_id: str) -> set[str]:
    try:
        return {e.get("detail", {}).get("to")
                for e in rm.read_audit_log(root, run_id)
                if e.get("event") == "STAGE"}
    except Exception:
        return set()


def _load_manifests(root: Path, production_task_id: str | None = None,
                    since: str | None = None,
                    until: str | None = None) -> list[dict]:
    mdir = rm.manifest_dir(root)
    if not mdir.is_dir():
        return []
    out = []
    for path in sorted(mdir.glob("*.json")):
        if path.name.endswith(".audit.jsonl"):
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(manifest, dict) or not manifest.get("run_id"):
            continue
        if production_task_id and \
                manifest.get("production_task_id") != production_task_id:
            continue
        created = manifest.get("created_at_utc") or ""
        if since and created < since:
            continue
        if until and created > until:
            continue
        out.append(manifest)
    return out


def _ledger_compliant(root: Path, task_id: str) -> bool | None:
    """True/False from the task ledger; None when it is missing/unreadable."""
    try:
        path = ledger_path_for_task(root, task_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        budget = float(data.get("project_budget_cu", PROJECT_BUDGET_CU))
        spent = float(data.get("max_observed_spent_cu"))
        return spent <= budget
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Metric F (TASK-006 P1-C, Issue #50): deadline-bound end-to-end success.
# ---------------------------------------------------------------------------

def _parse_utc(value) -> datetime | None:
    """Parse a `YYYY-MM-DDTHH:MM:SSZ` timestamp; None when malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_ledger(root: Path, task_id: str) -> dict | None:
    try:
        path = ledger_path_for_task(root, task_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _discover_task_ids(root: Path, manifests: list[dict],
                       task_filter: str | None) -> list[str]:
    """Every task id known from a manifest or a ledger file in scope."""
    if task_filter:
        return [task_filter]
    ids = {m.get("production_task_id") for m in manifests
           if m.get("production_task_id")}
    try:
        cache = root / "model_cache"
        prefix, suffix = "h3_production_budget_", ".json"
        for path in cache.glob(prefix + "*" + suffix):
            task_id = path.name[len(prefix):-len(suffix)]
            if task_id:
                ids.add(task_id)
    except Exception:
        pass
    return sorted(ids)


def _stage_times(root: Path, run_id: str) -> dict[str, list[str]]:
    """STAGE -> sorted ts_utc list, from the run's audit log."""
    out: dict[str, list[str]] = {}
    try:
        for e in rm.read_audit_log(root, run_id):
            if e.get("event") != "STAGE":
                continue
            stage = (e.get("detail") or {}).get("to")
            ts = e.get("ts_utc") or ""
            if stage and ts:
                out.setdefault(stage, []).append(ts)
    except Exception:
        pass
    for times in out.values():
        times.sort()
    return out


def _expected_frames(manifest: dict) -> int:
    """Expected final-video frames for one run: the run's recorded
    TaskSpec total frames (animation + tail) when present; the frozen
    A/B total for pre-TaskSpec manifests."""
    artifacts = manifest.get("artifacts") or {}
    task_spec = artifacts.get("task_spec")
    if isinstance(task_spec, dict):
        try:
            return int(task_spec["animation_frames"]) + int(task_spec["tail_card_frames"])
        except (KeyError, TypeError, ValueError):
            return TOTAL_FRAMES
    return TOTAL_FRAMES


def _has_models_ready_evidence(root: Path, run_id: str,
                               stages: dict[str, list[str]]) -> bool:
    """True when durable audit evidence proves models were restored and loaded
    (v0.2.1 P3): explicit MODELS_READY stage, OR a COLDSTART_METRICS audit
    event (emitted after ComfyUI /object_info passes), OR monotonic stage
    progression WORKER_STARTED -> INFERENCE_COMPLETED -> VIDEO_VALIDATED
    (which can only occur when a fast worker crossed MODELS_READY between
    60s controller polls)."""
    if "MODELS_READY" in stages:
        return True
    try:
        if any(e.get("event") == "COLDSTART_METRICS"
               for e in rm.read_audit_log(root, run_id)):
            return True
    except Exception:
        pass
    ws = stages.get("WORKER_STARTED")
    ic = stages.get("INFERENCE_COMPLETED")
    vv = stages.get("VIDEO_VALIDATED")
    if ws and ic and vv and ws[0] <= ic[0] <= vv[0]:
        return True
    return False


def _evaluate_run_f(root: Path, manifest: dict) -> list[str]:
    """Failed F conditions (1-5, 7) for one run; empty = the run meets them."""
    failed: list[str] = []
    run_id = manifest.get("run_id") or ""
    stages = _stage_times(root, run_id)
    artifacts = manifest.get("artifacts") or {}
    if not manifest.get("session_id") or "GPU_ALLOCATED" not in stages:
        failed.append("allocation: no session_id or no GPU_ALLOCATED stage")
    if not _has_models_ready_evidence(root, run_id, stages):
        failed.append("models: no MODELS_READY stage")
    if ("VIDEO_VALIDATED" not in stages
            or not artifacts.get("local_sha256")
            or artifacts.get("local_frames") != _expected_frames(manifest)):
        failed.append("inference: no VIDEO_VALIDATED stage or local MP4 "
                      "not spec-conforming (local_sha256 / local_frames)")
    if ("DRIVE_VERIFIED" not in stages
            or artifacts.get("delivery_state") != "DELIVERED"
            or not artifacts.get("drive_file_id")):
        failed.append("persistence: no DRIVE_VERIFIED stage or "
                      "delivery_state != DELIVERED or no drive_file_id")
    if ("GPU_STOP_CONFIRMED" not in stages
            or manifest.get("session_state") != "STOPPED"):
        failed.append("shutdown: no GPU_STOP_CONFIRMED stage or "
                      "session_state != STOPPED")
    if (manifest.get("task_state") != "TASK_COMPLETED"
            or "TASK_COMPLETED" not in stages):
        failed.append("completion: task_state != TASK_COMPLETED or "
                      "no TASK_COMPLETED stage")
    return failed


# ---------------------------------------------------------------------------
# Metric F lease cross-check (TASK-RELEASE-V032 R3).
#
# The run manifest is not the only allocation evidence: the GPU lease
# registry (h3_gpu_lease_registry.REGISTRY_PATH) records every session
# the controller pre-registered BEFORE `colab new`, keyed by
# session_id, with run_id / production_task_id and a
# last_confirmed_state that only becomes STOPPED on a confirmed stop
# verdict. A manifest whose session_id was never written back (the
# pre-registration write failed, or the run died in the allocation
# window) still leaves a lease behind — the registry is what catches
# that GPU. There is no literal `pending_session_id` field anywhere in
# this codebase: the pre-registration evidence IS the lease entry
# itself (session_id pre-registered, deadline_final False,
# last_confirmed_state ACTIVE until the outcome is known), plus the
# manifest's session_id written by the same pre-registration step
# (TASK-HOTFIX-016 P0-B).
# ---------------------------------------------------------------------------

LEASE_REGISTRY_NAME = "h3_gpu_leases.json"


def _load_gpu_leases(root: Path) -> tuple[list[dict], dict]:
    """Read the lease registry under `root`.

    Returns (leases, status); status is {"registry": "present" |
    "missing" | "corrupt", "detail": str | None, "leases_total": int}.
    Missing or unreadable is fail-safe, never a crash: the caller
    judges F from manifest/audit evidence only and records the gap.
    """
    path = root / "model_cache" / LEASE_REGISTRY_NAME
    if not path.is_file():
        return [], {"registry": "missing",
                    "detail": ("no lease evidence: "
                               "model_cache/h3_gpu_leases.json not found; "
                               "F judged from manifest/audit evidence only"),
                    "leases_total": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("leases"), list):
            raise ValueError(
                "lease registry is not a lease document (leases list missing)")
        leases = [x for x in data["leases"] if isinstance(x, dict)]
        return leases, {"registry": "present", "detail": None,
                        "leases_total": len(leases)}
    except Exception as e:  # noqa: BLE001 - fail-safe, recorded by caller
        return [], {"registry": "corrupt",
                    "detail": (f"no lease evidence: lease registry unreadable "
                               f"({type(e).__name__}); F judged from "
                               "manifest/audit evidence only"),
                    "leases_total": 0}


def _lease_is_outstanding(lease: dict) -> bool:
    """True unless the lease is confirmed STOPPED.

    Only a confirmed STOPPED verdict clears allocation evidence. A
    missing/unknown state is outstanding (fail-closed): it is
    unconfirmed, not proven absent. This is the exact split the P0-B
    pre-registration path relies on — a failed `colab new` whose
    live-list check confirmed no remote session is marked STOPPED (not
    a leak), while anything still ACTIVE — including warm_idle /
    WARM_POOLED sessions not yet stopped and recycled — is a live,
    possibly billable GPU.
    """
    return lease.get("last_confirmed_state") != "STOPPED"


def _leases_for_task(leases: list[dict], task_id: str,
                     run_ids: list[str], session_ids: set[str]) -> list[dict]:
    """Leases belonging to one task: by production_task_id, by run_id
    (including warm_run_ids, which survive a warm-pool claim that
    rewrites run_id / production_task_id), or by a session_id that
    appears on one of the task's manifests."""
    run_id_set = set(run_ids)
    matched: list[dict] = []
    for lease in leases:
        lease_runs = {lease.get("run_id")}
        lease_runs |= set(lease.get("warm_run_ids") or [])
        if (lease.get("production_task_id") == task_id
                or lease_runs & run_id_set
                or (lease.get("session_id")
                    and lease.get("session_id") in session_ids)):
            matched.append(lease)
    return matched


def _lease_failure_reason(lease: dict, manifest_session_ids: set[str]) -> str:
    """One failed_condition naming the outstanding lease and why it
    counts: real allocation evidence, not confirmed STOPPED."""
    sid = lease.get("session_id") or "<unknown>"
    run_id = lease.get("run_id") or "<unknown>"
    state = lease.get("last_confirmed_state")
    warm = lease.get("warm_idle") is True
    if sid not in manifest_session_ids:
        where = (f"lease-only evidence: session {sid} was never written "
                 f"back to any manifest session_id for this task")
    else:
        where = f"session {sid}"
    warm_note = " (warm_idle: not yet stopped/recycled)" if warm else ""
    return (f"lease: {where} (run {run_id}) has real allocation evidence "
            f"but is not confirmed STOPPED "
            f"(last_confirmed_state={state}, warm_idle={warm}, "
            f"deadline_final={lease.get('deadline_final')}){warm_note}")


def _evaluate_task_f(root: Path, task_id: str, runs: list[dict],
                     ledger: dict, now_dt: datetime,
                     time_filter_active: bool) -> dict:
    """One task's F verdict. `in_denominator` False means the task is
    listed separately (in-flight / no deadline) and is NOT counted."""
    deadline_raw = ledger.get("deadline_utc")
    deadline = _parse_utc(deadline_raw)
    verdict = {"production_task_id": task_id,
               "deadline_utc": deadline_raw,
               "verdict": "failure", "failed_conditions": [],
               "in_denominator": False, "completed_at_utc": None,
               "run_ids": sorted(m.get("run_id") or "" for m in runs)}
    if deadline is None:
        verdict["excluded_reason"] = ("no deadline recorded on the ledger; "
                                      "the task is not in the F denominator")
        return verdict
    deadline_passed = now_dt >= deadline
    non_terminal = [m for m in runs
                    if m.get("task_state") not in rm.TERMINAL_STATES]
    if not runs and time_filter_active:
        # A time filter scopes the runs: runs may exist outside the
        # window, so a zero-run task cannot be judged here.
        verdict["excluded_reason"] = ("no runs inside the requested time "
                                      "window; cannot judge")
        return verdict
    if not runs and not deadline_passed:
        verdict["excluded_reason"] = ("no runs started and the deadline "
                                      "has not passed; in flight")
        verdict["in_flight"] = True
        return verdict
    if non_terminal and not deadline_passed:
        verdict["excluded_reason"] = ("runs still in flight and the "
                                      "deadline has not passed")
        verdict["in_flight"] = True
        verdict["in_flight_run_ids"] = sorted(
            m.get("run_id") or "" for m in non_terminal)
        return verdict
    verdict["in_denominator"] = True
    failed: list[str] = []
    if not runs:
        failed.append("no runs started before the deadline")
    # (a) TASK-008: at least ONE run fully meets F conditions (1)-(5)
    # and (7) and reached TASK_COMPLETED before the deadline. A task
    # that needed a retry still counts as a task success; Metric A
    # keeps distinguishing first-attempt from retry success.
    successful: list[tuple[dict, str]] = []
    for m in runs:
        if m.get("task_state") != "TASK_COMPLETED":
            continue
        if _evaluate_run_f(root, m):
            continue
        times = _stage_times(root, m.get("run_id") or "").get("TASK_COMPLETED", [])
        if times:
            successful.append((m, times[0]))
    if not successful:
        failed.append("completion: no run fully meets F conditions "
                      "(1)-(5),(7) with TASK_COMPLETED")
        verdict["completed_at_utc"] = None
    else:
        completed_at = min(t for _, t in successful)
        verdict["completed_at_utc"] = completed_at
        verdict["successful_run_ids"] = sorted(
            m.get("run_id") or "" for m, _ in successful)
        if (_parse_utc(completed_at) or datetime.max.replace(tzinfo=timezone.utc)) > deadline:
            failed.append(f"completion: earliest fully-successful run "
                          f"completed at {completed_at} after deadline {deadline_raw}")
    # (b) Every other run that ever allocated a session_id must be
    # confirmed STOPPED with shutdown evidence — no orphan billable
    # GPU may survive a delivered task. Runs that never allocated
    # (no session_id) have nothing to leak.
    successful_ids = {m.get("run_id") for m, _ in successful}
    for m in runs:
        run_id = m.get("run_id") or ""
        if run_id in successful_ids:
            continue
        if not m.get("session_id"):
            continue
        stages = _stage_times(root, run_id)
        if m.get("session_state") != "STOPPED" or "GPU_STOP_CONFIRMED" not in stages:
            failed.append(f"shutdown: run {run_id} allocated a session "
                          f"but is not confirmed STOPPED "
                          f"(session_state={m.get('session_state')}, "
                          f"GPU_STOP_CONFIRMED={'GPU_STOP_CONFIRMED' in stages})")
    try:
        spent = float(ledger.get("max_observed_spent_cu"))
        budget = float(ledger.get("project_budget_cu", PROJECT_BUDGET_CU))
        if spent > budget:
            failed.append(f"budget: max_observed_spent_cu {spent} > "
                          f"project_budget_cu {budget}")
    except (TypeError, ValueError):
        failed.append("budget: ledger spend unreadable")
    # (b2) TASK-RELEASE-V032 R3: lease-registry cross-check. The
    # manifest-only check (b) above can only see sessions a manifest
    # names; the registry also sees pre-registered sessions whose
    # session_id was never written back, warm-pooled sessions, and
    # leases whose run_id matches no manifest. Any task lease with
    # real allocation evidence but no confirmed STOPPED fails F.
    # A missing/corrupt registry adds no failure (fail-safe: judged
    # from manifest/audit evidence only) but is recorded on the
    # verdict so the gap is visible, never silent.
    leases, lease_status = _load_gpu_leases(root)
    run_ids = [m.get("run_id") or "" for m in runs]
    manifest_session_ids = {m.get("session_id") for m in runs
                            if m.get("session_id")}
    task_leases = _leases_for_task(leases, task_id, run_ids,
                                   manifest_session_ids)
    outstanding = [x for x in task_leases if _lease_is_outstanding(x)]
    verdict["lease_evidence"] = lease_status["registry"]
    verdict["lease_checked"] = len(task_leases)
    verdict["lease_outstanding"] = [
        {"session_id": x.get("session_id"), "run_id": x.get("run_id"),
         "last_confirmed_state": x.get("last_confirmed_state"),
         "warm_idle": bool(x.get("warm_idle"))} for x in outstanding]
    if lease_status["registry"] == "present":
        verdict["lease_cross_check"] = (
            f"checked {len(task_leases)} lease(s) for this task; "
            f"{len(outstanding)} outstanding (not confirmed STOPPED)")
    else:
        verdict["lease_cross_check"] = (
            f"unavailable ({lease_status['registry']}): "
            f"{lease_status['detail']}")
    for lease in outstanding:
        failed.append(_lease_failure_reason(lease, manifest_session_ids))
    verdict["failed_conditions"] = failed
    verdict["verdict"] = "success" if not failed else "failure"
    return verdict


def _deadline_bound_e2e(root: Path, manifests: list[dict],
                        now_dt: datetime,
                        task_filter: str | None = None,
                        time_filter_active: bool = False) -> dict:
    """Metric F plus the per-task verdicts and the excluded-task lists."""
    verdicts: list[dict] = []
    in_flight_tasks: list[str] = []
    deadline_unknown_tasks: list[str] = []
    window_excluded_tasks: list[str] = []
    for task_id in _discover_task_ids(root, manifests, task_filter):
        ledger = _load_ledger(root, task_id)
        if ledger is None or not ledger.get("authorized"):
            # No authorisation evidence: not a production task in the
            # F sense. A manifest without a ledger is listed as unknown
            # rather than silently dropped.
            if any(m.get("production_task_id") == task_id for m in manifests):
                deadline_unknown_tasks.append(task_id)
            continue
        runs = [m for m in manifests
                if m.get("production_task_id") == task_id]
        verdict = _evaluate_task_f(root, task_id, runs, ledger, now_dt,
                                   time_filter_active)
        if not verdict["in_denominator"]:
            if verdict.get("in_flight"):
                in_flight_tasks.append(task_id)
            elif verdict.get("excluded_reason", "").startswith("no runs inside"):
                window_excluded_tasks.append(task_id)
            else:
                deadline_unknown_tasks.append(task_id)
            continue
        verdicts.append(verdict)
    successes = sum(1 for v in verdicts if v["verdict"] == "success")
    metric = _metric(
        successes, len(verdicts),
        "tasks completing allocation + restore + inference + verified "
        "durable MP4 + confirmed stop within budget and deadline / "
        "tasks authorised with a budget and a deadline whose deadline "
        "passed or whose runs are all terminal",
        unknown_reason="no task in scope was authorised with a budget "
                       "and a deadline")
    metric["target_verified"] = bool(
        metric["status"] == "MEASURED"
        and metric["confidence_interval_95"] is not None
        and metric["confidence_interval_95"][0] >= TARGET_RATE)
    metric["verdicts"] = verdicts
    # R3: metric-level lease cross-check summary. The registry is one
    # file for the whole root, so its status is read once here; the
    # per-task counts come from the verdicts above (in-denominator
    # tasks only).
    leases, lease_status = _load_gpu_leases(root)
    leases_checked = sum(v.get("lease_checked", 0) for v in verdicts)
    outstanding_all: list[dict] = []
    for v in verdicts:
        for o in v.get("lease_outstanding", []):
            outstanding_all.append({"production_task_id":
                                    v["production_task_id"], **o})
    if lease_status["detail"] is not None:
        lease_detail = lease_status["detail"]
    else:
        lease_detail = (
            f"{lease_status['leases_total']} lease(s) in registry; "
            f"{leases_checked} matched tasks in scope; "
            f"{len(outstanding_all)} outstanding (not confirmed STOPPED)")
    metric["lease_cross_check"] = {
        "registry": lease_status["registry"],
        "detail": lease_detail,
        "leases_total": lease_status["leases_total"],
        "leases_checked": leases_checked,
        "outstanding_leases": outstanding_all,
    }
    return {"metric": metric,
            "in_flight_tasks": sorted(in_flight_tasks),
            "deadline_unknown_tasks": sorted(deadline_unknown_tasks),
            "time_window_excluded_tasks": sorted(window_excluded_tasks)}


def _startup_mode(manifest: dict) -> str:
    """cold_start / warm_reuse / unknown for one run (R3).

    warm: coldstart_metrics or artifacts record warm_session_reused
    True (the worker report carries the key only when warm; the
    controller also stamps artifacts on the warm path). cold: a
    coldstart report exists and says nothing about warm reuse.
    unknown: no report and no warm flag — the mode cannot be proven,
    so it is not guessed as cold.
    """
    cold = manifest.get("coldstart_metrics") or {}
    arts = manifest.get("artifacts") or {}
    if cold.get("warm_session_reused") is True \
            or arts.get("warm_session_reused") is True:
        return "warm_reuse"
    if isinstance(manifest.get("coldstart_metrics"), dict):
        return "cold_start"
    return "unknown"


def _startup_mode_breakdown(terminal: list[dict]) -> dict:
    """Per-startup-mode first-attempt and eventual rates over terminal
    runs, each a full _metric dict (with wilson_95_ci)."""
    out: dict[str, dict] = {}
    for mode in ("cold_start", "warm_reuse", "unknown"):
        runs = [m for m in terminal if _startup_mode(m) == mode]
        completed = [m for m in runs
                     if m.get("task_state") == "TASK_COMPLETED"]
        first = sum(1 for m in completed
                    if m.get("recovery_generation", 0) == 0)
        out[mode] = {
            "runs": len(runs),
            "completed": len(completed),
            "first_attempt_success_rate": _metric(
                first, len(runs),
                "TASK_COMPLETED with recovery_generation == 0 / terminal "
                f"runs in startup mode {mode}",
                unknown_reason=f"no terminal {mode} runs in scope"),
            "eventual_task_success_rate": _metric(
                len(completed), len(runs),
                "TASK_COMPLETED / terminal runs in startup mode "
                f"{mode}",
                unknown_reason=f"no terminal {mode} runs in scope"),
        }
    return out


def compute_reliability(root: str | Path, production_task_id: str | None = None,
                        since: str | None = None, until: str | None = None,
                        output_root: str | Path | None = None,
                        now_utc: str | None = None) -> dict:
    """Compute the six rates over the manifests under `root`.

    `output_root`, when given, is the directory holding
    `<run_id>/<variant>.mp4` files; metric C then also verifies the
    local file still exists with the recorded SHA-256. Without it, C
    is judged from manifest states only. `now_utc` (ISO
    `YYYY-MM-DDTHH:MM:SSZ`) is the "now" for metric F's deadline
    comparison; it defaults to the current UTC time and exists so
    tests can fix the clock.
    """
    root = Path(root)
    output_root = Path(output_root) if output_root else None
    now_dt = _parse_utc(now_utc) or datetime.now(timezone.utc)
    manifests = _load_manifests(root, production_task_id, since, until)
    terminal = [m for m in manifests
                if m.get("task_state") in rm.TERMINAL_STATES]
    in_flight = [m for m in manifests
                 if m.get("task_state") not in rm.TERMINAL_STATES]
    created = sorted(m.get("created_at_utc") or ""
                     for m in manifests if m.get("created_at_utc"))

    completed = [m for m in terminal
                 if m.get("task_state") == "TASK_COMPLETED"]
    failed = [m for m in terminal if m.get("task_state") == "FAILED"]
    awaiting = [m for m in terminal
                if m.get("task_state") == "COMPLETED_AWAITING_ARCHIVE"]

    metric_a = _metric(
        sum(1 for m in completed if m.get("recovery_generation", 0) == 0),
        len(terminal),
        "TASK_COMPLETED with recovery_generation == 0 / terminal runs",
        unknown_reason="no terminal runs in scope")
    metric_b = _metric(
        len(completed), len(terminal),
        "TASK_COMPLETED (incl. via resume / archive-only) / terminal runs",
        unknown_reason="no terminal runs in scope")

    produced = []
    preserved = []
    for m in terminal:
        artifacts = m.get("artifacts") or {}
        if artifacts.get("local_sha256") and \
                artifacts.get("delivery_state") in _DELIVERY_AT_LEAST_LOCAL:
            produced.append(m)
            ok = True
            if output_root is not None:
                variant = m.get("variant") or "A"
                local = (output_root / m["run_id"] / f"{variant}.mp4")
                try:
                    ok = local.is_file() and \
                        hashlib.sha256(local.read_bytes()).hexdigest() == \
                        artifacts["local_sha256"]
                except Exception:
                    ok = False
            if ok:
                preserved.append(m)
    metric_c = _metric(
        len(preserved), len(produced),
        "validated local MP4 survived / runs that produced a validated MP4",
        unknown_reason="no run in scope produced a validated local MP4")

    allocated = [m for m in terminal if m.get("session_id")]
    verified_shutdowns = sum(
        1 for m in allocated
        if m.get("session_state") == "STOPPED"
        and "GPU_STOP_CONFIRMED" in _audit_stages(root, m["run_id"]))
    metric_d = _metric(
        verified_shutdowns, len(allocated),
        "GPU_STOP_CONFIRMED in audit log and session_state STOPPED / "
        "terminal runs with a session",
        unknown_reason="no terminal run in scope allocated a GPU")

    e_numerator = 0
    e_denominator = 0
    e_unknown_runs: list[str] = []
    ledger_cache: dict[str, bool | None] = {}
    for m in terminal:
        task_id = m.get("production_task_id") or ""
        if task_id not in ledger_cache:
            ledger_cache[task_id] = _ledger_compliant(root, task_id)
        verdict = ledger_cache[task_id]
        if verdict is None:
            e_unknown_runs.append(m["run_id"])
        else:
            e_denominator += 1
            if verdict:
                e_numerator += 1
    metric_e = _metric(
        e_numerator, e_denominator,
        "task ledger max_observed_spent_cu <= project_budget_cu / "
        "terminal runs with a readable task ledger",
        unknown_reason="no terminal run in scope has a readable task ledger")

    metric_f_result = _deadline_bound_e2e(
        root, manifests, now_dt, task_filter=production_task_id,
        time_filter_active=bool(since or until))
    metric_f = metric_f_result["metric"]

    target_verified = bool(
        metric_b["status"] == "MEASURED"
        and metric_b["confidence_interval_95"] is not None
        and metric_b["confidence_interval_95"][0] >= TARGET_RATE)
    if metric_b["status"] == "UNKNOWN":
        target_evidence = ("No terminal production runs in scope; "
                           "the 95% target is unverified.")
    elif metric_b["status"] == "INSUFFICIENT_SAMPLE":
        target_evidence = (
            f"Only {metric_b['denominator']} terminal run(s) in scope; "
            f"Wilson 95% interval for eventual success is "
            f"{metric_b['confidence_interval_95']} — too small a sample "
            f"to verify a 95% rate.")
    elif target_verified:
        target_evidence = (
            f"Wilson 95% lower bound for eventual success is "
            f"{metric_b['confidence_interval_95'][0]} (>= 0.95) over "
            f"{metric_b['denominator']} terminal runs.")
    else:
        target_evidence = (
            f"Wilson 95% lower bound for eventual success is "
            f"{metric_b['confidence_interval_95'][0]} (< 0.95) over "
            f"{metric_b['denominator']} terminal runs; "
            f"the 95% target is not verified.")

    # R3: the honest F 95% conclusion, mirroring target_95_* for B.
    # F_95_VERIFIED only when F is MEASURED and its Wilson lower bound
    # is >= 0.95; anything else stays F_95_NOT_VERIFIED — an
    # unverified F is never reported as verified.
    f_target_verified = bool(metric_f["target_verified"])
    f_95_status = ("F_95_VERIFIED" if f_target_verified
                   else "F_95_NOT_VERIFIED")
    if metric_f["status"] == "UNKNOWN":
        f_95_evidence = (
            "No task in scope was authorised with a budget and a "
            "deadline; the 95% deadline-bound target is unverified "
            "(F_95_NOT_VERIFIED).")
    elif metric_f["status"] == "INSUFFICIENT_SAMPLE":
        f_95_evidence = (
            f"Only {metric_f['denominator']} task(s) in scope; Wilson 95% "
            f"interval for deadline-bound success is "
            f"{metric_f['confidence_interval_95']} — too small a sample "
            "to verify a 95% rate (F_95_NOT_VERIFIED).")
    elif f_target_verified:
        f_95_evidence = (
            f"Wilson 95% lower bound for deadline-bound success is "
            f"{metric_f['confidence_interval_95'][0]} (>= 0.95) over "
            f"{metric_f['denominator']} tasks (F_95_VERIFIED).")
    else:
        f_95_evidence = (
            f"Wilson 95% lower bound for deadline-bound success is "
            f"{metric_f['confidence_interval_95'][0]} (< 0.95) over "
            f"{metric_f['denominator']} tasks; the 95% target is not "
            "verified (F_95_NOT_VERIFIED).")

    def _rate_summary(metric: dict) -> dict:
        summary = {"rate": metric["rate"],
                   "numerator": metric["numerator"],
                   "denominator": metric["denominator"],
                   "wilson_95_ci": metric["wilson_95_ci"],
                   "status": metric["status"]}
        if "target_verified" in metric:
            summary["target_verified"] = metric["target_verified"]
        return summary

    a_vs_f = {
        "first_attempt_success_rate": _rate_summary(metric_a),
        "deadline_bound_e2e_success_rate": _rate_summary(metric_f),
        "note": ("A counts terminal runs (first-attempt success); F counts "
                 "authorised tasks completing end to end within deadline. "
                 "Different denominators — compared side by side, never "
                 "pooled. With zero failures, a two-sided 95% Wilson lower "
                 "bound >= 0.95 needs 73/73 successes (73/73 -> lower bound "
                 "0.95; 60/60 reaches only 0.9398); any failure needs more."),
    }

    # v0.2.0 F5: cold-start telemetry. A run counts when its manifest
    # carries a validated coldstart_metrics report. Latencies are
    # P50/P90 over the reported setup_seconds; the wheelhouse hit rate
    # is a Wilson rate over the runs that reported wheelhouse_hit
    # (None means pip was skipped, not a miss — those runs are excluded
    # from the rate, never counted as failures).
    cold_reports = [m.get("coldstart_metrics") for m in manifests
                    if isinstance(m.get("coldstart_metrics"), dict)]
    setup_seconds = sorted(
        float(c["setup_seconds"]) for c in cold_reports
        if isinstance(c.get("setup_seconds"), (int, float)))
    wheel_hits = [c["wheelhouse_hit"] for c in cold_reports
                  if isinstance(c.get("wheelhouse_hit"), bool)]
    if cold_reports:
        coldstart = {
            "status": "MEASURED",
            "runs_with_metrics": len(cold_reports),
            "setup_seconds_p50": _percentile(setup_seconds, 0.5),
            "setup_seconds_p90": _percentile(setup_seconds, 0.9),
            "wheelhouse_hit_rate": _metric(
                sum(1 for h in wheel_hits if h), len(wheel_hits),
                "wheelhouse_hit == True / runs reporting wheelhouse_hit",
                unknown_reason="no run in scope reported wheelhouse_hit"),
        }
    else:
        coldstart = {
            "status": "UNKNOWN",
            "runs_with_metrics": 0,
            "setup_seconds_p50": None,
            "setup_seconds_p90": None,
            "wheelhouse_hit_rate": _metric(
                0, 0, "wheelhouse_hit == True / runs reporting wheelhouse_hit",
                unknown_reason="no run in scope reported cold-start metrics"),
        }

    warm_session_hits = sum(
        1 for m in manifests
        if (m.get("coldstart_metrics") or {}).get("warm_session_reused") is True
        or (m.get("artifacts") or {}).get("warm_session_reused") is True
    )
    warm_pooled_active_runs = sum(
        1 for m in manifests
        if m.get("session_state") == "WARM_POOLED"
    )

    return {
        "root": str(root),
        "sample_scope": {"production_task_id": production_task_id,
                         "since": since, "until": until},
        "sample_size": len(terminal),
        "runs_sampled": len(manifests),
        "warm_session_hits": warm_session_hits,
        "warm_pooled_active_runs": warm_pooled_active_runs,
        "observation_dates": [created[0], created[-1]] if created else None,
        "breakdown": {"completed": len(completed), "failed": len(failed),
                      "awaiting_archive": len(awaiting),
                      "in_flight": len(in_flight)},
        "in_flight_run_ids": sorted(m["run_id"] for m in in_flight),
        "metrics": {
            "first_attempt_success_rate": metric_a,
            "eventual_task_success_rate": metric_b,
            "artifact_preservation_rate": metric_c,
            "verified_shutdown_rate": metric_d,
            "budget_compliance_rate": metric_e,
            "deadline_bound_e2e_success_rate": metric_f,
        },
        "coldstart": coldstart,
        "a_vs_f_comparison": a_vs_f,
        "f_95_verified": f_target_verified,
        "f_95_status": f_95_status,
        "f_95_evidence": f_95_evidence,
        "startup_mode_breakdown": _startup_mode_breakdown(terminal),
        "target_95_verified": target_verified,
        "target_95_evidence": target_evidence,
        "e_unknown_ledger_runs": sorted(e_unknown_runs),
        "f_in_flight_tasks": metric_f_result["in_flight_tasks"],
        "f_deadline_unknown_tasks": metric_f_result["deadline_unknown_tasks"],
        "f_time_window_excluded_tasks": metric_f_result["time_window_excluded_tasks"],
    }

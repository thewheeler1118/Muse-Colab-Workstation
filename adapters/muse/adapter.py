"""Muse adapter: natural-language task -> TaskSpec -> audited wrapper.

Stdlib + the existing production core only. The adapter does NOT:

- call bare ``colab new`` to create a GPU;
- rewrite the H3 worker;
- implement a second GPU budget ledger;
- implement a second Drive upload protocol;
- replace ``h3_shutdown_executor.py``;
- bypass the controller's production authorization checks.

Enforcement lives in ``scripts/run_h3_task.sh`` (refuses without
``--confirm-live``), in ``h3_controller.py`` (budget / authorization
gates), and in file permissions / ``scripts/manifest_policy.py`` —
not in a copied upstream hook file. See
``adapters/antigravity/COMPATIBILITY.md`` for the verified Muse
capability table.

Budget constants are read from ``h3_budget.py`` and asserted, never
redefined here: 30.00 CU per task, 25.00 CU per session, 0.75 CU
cleanup reserve, 14400 s maximum session lifetime.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from h3_budget import (  # noqa: E402
    CLEANUP_RESERVE_CU,
    MAX_SESSION_SECONDS,
    PER_SESSION_BUDGET_CU,
    PROJECT_BUDGET_CU,
)
from h3_task_spec import TaskSpec, load_task_spec  # noqa: E402

from .paths import locate_task_spec_file  # noqa: E402
from .redact import redact_value  # noqa: E402

BUDGET_TASK_CU = PROJECT_BUDGET_CU
BUDGET_SESSION_CU = PER_SESSION_BUDGET_CU
BUDGET_CLEANUP_RESERVE_CU = CLEANUP_RESERVE_CU
BUDGET_MAX_SESSION_SECONDS = MAX_SESSION_SECONDS


def assert_budget_constants() -> dict:
    """Assert the production budget constants are unchanged.

    Returns the constants for status reports. Raises ``AssertionError``
    if any value drifted — a drift must be fixed in ``h3_budget.py``
    with its config mirrors, never papered over here.
    """
    assert PROJECT_BUDGET_CU == 30.0, f"task budget drifted: {PROJECT_BUDGET_CU!r}"
    assert PER_SESSION_BUDGET_CU == 25.0, (
        f"session budget drifted: {PER_SESSION_BUDGET_CU!r}"
    )
    assert CLEANUP_RESERVE_CU == 0.75, f"cleanup reserve drifted: {CLEANUP_RESERVE_CU!r}"
    assert MAX_SESSION_SECONDS == 14400, (
        f"max session seconds drifted: {MAX_SESSION_SECONDS!r}"
    )
    return {
        "project_budget_cu": PROJECT_BUDGET_CU,
        "per_session_budget_cu": PER_SESSION_BUDGET_CU,
        "cleanup_reserve_cu": CLEANUP_RESERVE_CU,
        "max_session_seconds": MAX_SESSION_SECONDS,
    }


@dataclass(frozen=True)
class Authorization:
    """An explicit, single-task authorization record.

    Every field maps to a managed wrapper parameter; nothing here
    invents a new authorization. ``confirm_live`` must be ``True``
    because the user explicitly approved THIS task — the adapter
    never defaults it to true, and ``to_wrapper_args()`` refuses
    without it.
    """

    production_task_id: str
    confirm_live: bool = False
    authorize_new_task: bool = False
    max_task_attempts: int = 2
    task_deadline_utc: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.production_task_id, str) or not self.production_task_id.strip():
            raise ValueError("production_task_id is required (never reused silently)")
        if self.max_task_attempts not in (1, 2, 3):
            raise ValueError("max_task_attempts must be 1, 2 or 3")


def to_wrapper_args(auth: Authorization, task_spec_path: Path | str) -> list[str]:
    """Map an explicit authorization to ``scripts/run_h3_task.sh`` args.

    Refuses (raises) unless ``confirm_live`` is explicitly true. The
    wrapper itself re-checks every gate; this mapping adds no new
    permission and removes none.
    """
    if not auth.confirm_live:
        raise PermissionError(
            "REFUSED: live GPU run needs explicit user approval for this task "
            "(confirm_live=True); the adapter never auto-confirms"
        )
    spec = locate_task_spec_file(task_spec_path)
    args = [
        "--confirm-live",
        "--production-task-id",
        auth.production_task_id,
        "--task-spec",
        str(spec),
        "--deliver",
        "--max-task-attempts",
        str(auth.max_task_attempts),
    ]
    if auth.authorize_new_task:
        args.append("--authorize-new-task")
    if auth.task_deadline_utc:
        args += ["--task-deadline-utc", auth.task_deadline_utc]
    if auth.run_id:
        args += ["--run-id", auth.run_id]
    return args


def build_task_spec(spec_dict: dict) -> TaskSpec:
    """Build a validated TaskSpec from explicit structured fields.

    Natural-language input from the user is accepted ONLY as the
    ``prompt`` (or ``shots``) string inside *spec_dict*; every other
    field (refs, seed, dimensions, frames) must be supplied explicitly
    and is validated fail-closed by ``TaskSpec.from_dict``. Unknown
    keys, a missing ref_image_1, a missing seed, or a frame count
    violating the MiniMax rule all raise ``ValueError`` here, at 0 CU.
    """
    if not isinstance(spec_dict, dict):
        raise ValueError("spec must be a JSON object (dict)")
    return TaskSpec.from_dict(spec_dict)


def load_validated_task_spec(path: Path | str) -> TaskSpec:
    """Load and validate a task-spec file through the production loader."""
    return load_task_spec(locate_task_spec_file(path))


def _parse_manifest_time(data: dict):
    """Reliable time for one manifest: (datetime|None, source).

    The only reliable times are ISO-8601 UTC timestamps inside the
    manifest itself — ``updated_at_utc`` (most recent activity) first,
    then ``created_at_utc``. File mtime is NOT reliable (copies and
    restores change it) and is never used: a manifest with no
    parseable timestamp sorts after every timestamped one and is
    flagged ``time_source="none"``.
    """
    import datetime

    for field in ("updated_at_utc", "created_at_utc"):
        raw = data.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        try:
            parsed = datetime.datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc), field
    return None, "none"


def check_existing_task_state(
    production_task_id: str,
    search_roots: list[Path | str] | None = None,
) -> dict:
    """Read-only restart check: find existing state for a task id.

    Scans ``model_cache/run_manifests/*.json`` under the given roots
    (repo checkout and/or production directory) for manifests whose
    ``production_task_id`` or ``task_id`` matches. Returns what was
    found; launches nothing, resumes nothing, creates no GPU, and
    never reads credential, ledger-balance, or log contents — only
    manifest identity fields (run_id, task id, status, output name).

    The result is tri-state (``state``):

    - ``FOUND`` — at least one matching manifest. Callers must
      present the newest found state to the user before any new
      attempt is authorized.
    - ``CONFIRMED_ABSENT`` — every search root was read cleanly and
      no manifest matches. Only this state allows a new GPU request
      to be considered (still only with explicit user approval).
    - ``UNKNOWN`` — absence cannot be confirmed: no valid search
      root was supplied, a root could not be read, a manifest file
      could not be read or parsed, or coverage was otherwise
      incomplete. ``allow_new_gpu_request`` is False: nothing may
      request a new GPU on the basis of this check until the
      coverage problem is resolved.

    With several matching manifests they are sorted newest-first by
    reliable time (``updated_at_utc``, else ``created_at_utc`` from
    the manifest content); manifests with no parseable timestamp sort
    last, flagged ``time_source="none"``.
    """
    if not production_task_id or not str(production_task_id).strip():
        raise ValueError("production_task_id is required for a restart check")
    roots = [Path(r) for r in (search_roots or [])]
    found: list[dict] = []
    coverage: list[dict] = []
    problems: list[str] = []
    if not roots:
        problems.append(
            "no search roots provided: no directory was searched, so "
            "absence cannot be confirmed"
        )
    for root in roots:
        if not root.is_dir():
            coverage.append(
                {"root": str(root), "status": "unusable",
                 "manifests_read": 0}
            )
            problems.append(f"search root is not a readable directory: {root}")
            continue
        manifest_dir = root / "model_cache" / "run_manifests"
        if manifest_dir.exists() and not manifest_dir.is_dir():
            coverage.append(
                {"root": str(root), "status": "unusable",
                 "manifests_read": 0}
            )
            problems.append(
                f"manifest path is not a directory: {manifest_dir}"
            )
            continue
        if not manifest_dir.is_dir():
            coverage.append(
                {"root": str(root), "status": "no_manifest_dir",
                 "manifests_read": 0}
            )
            continue
        read = 0
        for manifest_path in sorted(manifest_dir.glob("*.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                problems.append(
                    f"unreadable manifest {manifest_path.name}: {exc}"
                )
                continue
            if not isinstance(data, dict):
                problems.append(
                    f"manifest is not a JSON object: {manifest_path.name}"
                )
                continue
            read += 1
            task_id = data.get("production_task_id") or data.get("task_id")
            if task_id != production_task_id:
                continue
            sort_time, time_source = _parse_manifest_time(data)
            found.append(
                {
                    "run_id": data.get("run_id"),
                    "production_task_id": task_id,
                    "status": data.get("status"),
                    "manifest": manifest_path.name,
                    "sort_time": (
                        sort_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                        if sort_time is not None else None
                    ),
                    "time_source": time_source,
                }
            )
        coverage.append(
            {"root": str(root), "status": "searched", "manifests_read": read}
        )
    # Newest first by reliable time; untimestamped manifests last,
    # in filename order (deterministic either way).
    timed = sorted(
        (m for m in found if m["sort_time"] is not None),
        key=lambda m: m["manifest"],
    )
    timed.sort(key=lambda m: m["sort_time"], reverse=True)
    untimed = sorted(
        (m for m in found if m["sort_time"] is None),
        key=lambda m: m["manifest"],
    )
    found = timed + untimed
    if found:
        state = "FOUND"
        action = (
            "review the newest manifest with the user before authorizing "
            "any new attempt; never auto-resume a GPU session"
        )
    elif problems:
        state = "UNKNOWN"
        action = (
            "state is UNKNOWN — do NOT request a new GPU on the basis "
            "of this check; resolve first: " + "; ".join(problems)
        )
    else:
        state = "CONFIRMED_ABSENT"
        action = (
            "no existing state found for this task id (all search roots "
            "read clean); a new attempt still needs explicit user approval"
        )
    return {
        "production_task_id": production_task_id,
        "state": state,
        "manifests_found": len(found),
        "manifests": found,
        "coverage": coverage,
        "problems": problems,
        "allow_new_gpu_request": state == "CONFIRMED_ABSENT",
        "action": action,
    }


def status_report(
    *,
    production_task_id: str,
    run_id: str | None,
    budget: dict | None = None,
    mp4_drive_file: str | None = None,
    shutdown: dict | None = None,
    note: str = "",
) -> dict:
    """Build a redacted status report for a Muse conversation message.

    Contains only identifiers and outcomes: budget constants, run id,
    delivered MP4 reference, and shutdown result. The whole report is
    deep-redacted (``redact_value``): credential-shaped substrings
    anywhere — in the note, in nested ``shutdown`` / ``budget``
    dicts, in lists — are replaced, and any value under a
    credential-named key is replaced wholesale. No raw log text is
    ever embedded. ``token_id`` values stay visible by design (they
    are truncated hashes, not tokens).
    """
    report = {
        "production_task_id": production_task_id,
        "run_id": run_id,
        "budget": budget if budget is not None else assert_budget_constants(),
        "mp4_drive_file": mp4_drive_file,
        "shutdown": shutdown,
        "note": note,
    }
    return redact_value(report)


# Backwards-compatible alias used by early drafts of the adapter docs.
check_restart_state = check_existing_task_state

__all__ = [
    "Authorization",
    "BUDGET_CLEANUP_RESERVE_CU",
    "BUDGET_MAX_SESSION_SECONDS",
    "BUDGET_SESSION_CU",
    "BUDGET_TASK_CU",
    "assert_budget_constants",
    "build_task_spec",
    "check_existing_task_state",
    "check_restart_state",
    "load_validated_task_spec",
    "status_report",
    "to_wrapper_args",
]

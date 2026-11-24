"""TASK-RELEASE-V032 R5: static AST gate for the worker stage contract.

The worker may only write a canonical job stage (``JOB_STAGES``) into
``MUSE_H3_JOB_STATE.json``. The mapping ``WORKER_PHASE_TO_JOB_STAGE``
is the only translation, and ``read_job_state()`` rejects anything
outside ``JOB_STAGES`` at the read boundary. This file proves, by
parsing the worker source with ``ast`` (no execution, no GPU, no
Drive), that every stage-writing site in ``h3_remote_worker.py``
(plus ``h3_worker_launcher.py``, the only other job-state writer)
conforms — so a new phase added without a mapping entry fails CI
instead of failing a live run at poll time with
``Unknown worker job stage`` (the v0.3.0 defect A mechanism).

Soundness argument (what the gate actually proves):

- ``set_stage(...)`` call sites: the argument is either a string
  literal that is a key of ``WORKER_PHASE_TO_JOB_STAGE``, or the
  single documented dynamic path ``set_stage(e.stage)``, where ``e``
  is the ``SetupTrackFailed`` from ``run_setup_parallel``. That
  dynamic path is bounded separately: every track name passed to
  ``run_setup_parallel`` must itself be a mapping key, so
  ``e.stage`` can only ever be a mapped phase.
- ``_LAST_STAGE`` assignments: only the module-level ``"init"``
  literal (mapped to ``JOB_STARTED``) and ``set_stage``'s own
  ``_LAST_STAGE = name``. No other function may assign it.
- ``write_job_state(...)`` stage arguments: either a ``JOB_STAGES``
  literal, or a ``WORKER_PHASE_TO_JOB_STAGE.get(x, default)`` whose
  input ``x`` is ``name`` (``set_stage``'s parameter) or
  ``_LAST_STAGE`` — both proven above to hold only mapped phases —
  and whose ``default`` is either a ``JOB_STAGES`` literal or the
  same name as the input (the fail-closed raw fallback, unreachable
  for audited phases: an unmapped phase would leak verbatim and be
  refused loudly by ``read_job_state()``, never silently relabelled).
- No dynamically built stage name anywhere: any ``+`` concatenation
  or f-string in a stage argument position fails explicitly. There
  is no such construction in the worker today; the test exists so
  one can never slip in unnoticed.

Explicit exclusion (not a hole): ``worker_failure_info`` carries the
raw phase in the ``error["stage"]`` diagnostic field of a ``FAILED``
state (e.g. ``"restore_weights"``). That field is diagnostics, not
the job-stage contract — the state's ``stage`` field is still the
``"FAILED"`` literal — and the existing
``tests/test_v031_hotfix_stage_contract.py`` pins that behaviour.
This gate asserts the ``FAILED`` write keeps the literal stage and
documents the exclusion rather than flagging it.

Behaviour reference (no code copied): upstream v0.3.1 P0-2 added the
same kind of gate (``tests/test_stage_contract_ast.py``) with the
same ``"init" -> "JOB_STARTED"`` mapping and the same
``read_job_state`` tightening. This file is a Muse-original
implementation of that contract, adapted to the Muse worker (Muse
has no ``dual_track_setup`` / ``link_models`` phases and no
``_write_job_state_safe`` helper, so those upstream forms do not
appear here).
"""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
import h3_job_state as hjs  # noqa: E402
import h3_remote_worker as hrw  # noqa: E402

WORKER_SRC = ROOT / "h3_remote_worker.py"
LAUNCHER_SRC = ROOT / "h3_worker_launcher.py"

# The one dynamic set_stage() call site: SetupTrackFailed carries the
# failing track's name (see run_setup_parallel). Its value universe is
# bounded by the track-name test below.
DYNAMIC_STAGE_ATTR = ("e", "stage")


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_call_to(node, name):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _is_mapping_get(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "WORKER_PHASE_TO_JOB_STAGE"
    )


def _stage_arg_of_write_call(node):
    """Stage argument of a write_job_state() call: 4th positional or
    the ``stage=`` keyword. Returns the node (never None — a call
    without a stage argument is a gate failure, not a skip)."""
    if len(node.args) >= 4:
        return node.args[3]
    for kw in node.keywords:
        if kw.arg == "stage":
            return kw.value
    raise AssertionError(
        f"write_job_state() call at line {node.lineno} has no stage argument"
    )


def _contains_dynamic_string(node):
    """True if the subtree builds a string dynamically (``+`` or an
    f-string). Such a construction in a stage position is never
    allowed: it cannot be proven to land in the legal set."""
    for sub in ast.walk(node):
        if isinstance(sub, (ast.BinOp, ast.JoinedStr)):
            return True
    return False


class _StageSites:
    """All stage-writing sites collected from the worker (+ launcher)."""

    def __init__(self):
        self.worker_tree = _parse(WORKER_SRC)
        self.launcher_tree = _parse(LAUNCHER_SRC)
        self.set_stage_calls = [
            n for n in ast.walk(self.worker_tree) if _is_call_to(n, "set_stage")
        ]
        self.write_calls = [
            n
            for tree in (self.worker_tree, self.launcher_tree)
            for n in ast.walk(tree)
            if _is_call_to(n, "write_job_state")
        ]
        self.setup_parallel_calls = [
            n
            for n in ast.walk(self.worker_tree)
            if _is_call_to(n, "run_setup_parallel")
        ]

    def track_names(self):
        """First elements of the (stage_name, callable) track tuples."""
        names = []
        for call in self.setup_parallel_calls:
            assert call.args, (
                f"run_setup_parallel() at line {call.lineno} has no arguments"
            )
            tracks = call.args[0]
            assert isinstance(tracks, ast.List), (
                f"run_setup_parallel() tracks at line {call.lineno} are not "
                f"a list literal: {ast.dump(tracks)[:120]}"
            )
            for item in tracks.elts:
                assert isinstance(item, ast.Tuple) and len(item.elts) >= 1, (
                    f"run_setup_parallel() track at line {call.lineno} is not "
                    f"a (stage, callable) tuple: {ast.dump(item)[:120]}"
                )
                name = _const_str(item.elts[0])
                assert name is not None, (
                    f"run_setup_parallel() track name at line {call.lineno} "
                    f"is not a string literal: {ast.dump(item.elts[0])[:120]}"
                )
                names.append(name)
        return names


class MappingContractTest(unittest.TestCase):
    def test_mapping_values_are_job_stages_and_init_maps_to_job_started(self):
        self.assertIn("init", hjs.WORKER_PHASE_TO_JOB_STAGE,
                      "the worker's initial _LAST_STAGE 'init' must map")
        self.assertEqual(hjs.WORKER_PHASE_TO_JOB_STAGE["init"], "JOB_STARTED")
        for phase, stage in hjs.WORKER_PHASE_TO_JOB_STAGE.items():
            self.assertIn(stage, hjs.JOB_STAGES,
                          f"phase {phase!r} maps to {stage!r}, "
                          f"which is not a job stage")


class SetStageSitesTest(unittest.TestCase):
    def test_every_set_stage_call_is_a_mapped_literal_or_documented_dynamic(self):
        sites = _StageSites()
        self.assertGreater(len(sites.set_stage_calls), 0,
                           "no set_stage() call sites found — the gate "
                           "scanned nothing; check the worker path")
        for call in sites.set_stage_calls:
            self.assertEqual(len(call.args), 1,
                             f"set_stage() at line {call.lineno} does not "
                             f"take exactly one argument")
            arg = call.args[0]
            literal = _const_str(arg)
            if literal is not None:
                self.assertIn(
                    literal, hjs.WORKER_PHASE_TO_JOB_STAGE,
                    f"set_stage({literal!r}) at line {call.lineno} has no "
                    f"mapping entry — add it to WORKER_PHASE_TO_JOB_STAGE")
                continue
            if (isinstance(arg, ast.Attribute) and arg.attr == DYNAMIC_STAGE_ATTR[1]
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == DYNAMIC_STAGE_ATTR[0]):
                # The single documented dynamic path (see module
                # docstring); bounded by test_setup_track_names_bound...
                continue
            self.fail(
                f"set_stage() at line {call.lineno} takes an unprovable "
                f"stage argument ({ast.dump(arg)[:160]}): only a mapped "
                f"string literal or the documented set_stage(e.stage) "
                f"dynamic path is allowed")

    def test_setup_track_names_bound_the_dynamic_set_stage(self):
        sites = _StageSites()
        self.assertGreater(len(sites.setup_parallel_calls), 0,
                           "no run_setup_parallel() call found — the "
                           "dynamic set_stage(e.stage) path would be "
                           "unbounded; check the worker")
        names = sites.track_names()
        self.assertGreater(len(names), 0,
                           "run_setup_parallel() carries no tracks")
        for name in names:
            self.assertIn(
                name, hjs.WORKER_PHASE_TO_JOB_STAGE,
                f"setup track {name!r} has no mapping entry, so "
                f"set_stage(e.stage) could write an unmapped phase")

    def test_no_dynamically_built_stage_name_in_set_stage(self):
        sites = _StageSites()
        for call in sites.set_stage_calls:
            self.assertFalse(
                _contains_dynamic_string(call.args[0]),
                f"set_stage() at line {call.lineno} builds its stage "
                f"dynamically (concatenation / f-string) — dynamic stage "
                f"names must be handled explicitly, never built inline")


class LastStageAssignmentTest(unittest.TestCase):
    def test_last_stage_assignments_are_mapped_literals_or_set_stage_param(self):
        tree = _parse(WORKER_SRC)
        # Module level (outside any function): only mapped literals.
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_LAST_STAGE":
                        literal = _const_str(node.value)
                        self.assertIsNotNone(
                            literal,
                            f"module-level _LAST_STAGE assignment at line "
                            f"{node.lineno} is not a string literal")
                        self.assertIn(
                            literal, hjs.WORKER_PHASE_TO_JOB_STAGE,
                            f"module-level _LAST_STAGE = {literal!r} at line "
                            f"{node.lineno} has no mapping entry")
        # Inside functions: only set_stage may assign it, and only from
        # its own `name` parameter.
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for sub in ast.walk(node):
                if sub is node:
                    continue
                if isinstance(sub, ast.AugAssign) and isinstance(
                        sub.target, ast.Name) and sub.target.id == "_LAST_STAGE":
                    self.fail(f"_LAST_STAGE is augmented at line {sub.lineno} "
                              f"inside {node.name}() — never allowed")
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if isinstance(target, ast.Name) and target.id == "_LAST_STAGE":
                            if node.name == "set_stage":
                                self.assertIsInstance(
                                    sub.value, ast.Name,
                                    f"set_stage() assigns _LAST_STAGE from "
                                    f"a non-parameter at line {sub.lineno}")
                                self.assertEqual(
                                    sub.value.id, "name",
                                    f"set_stage() assigns _LAST_STAGE from "
                                    f"{sub.value.id!r}, not its `name` "
                                    f"parameter, at line {sub.lineno}")
                            else:
                                self.fail(
                                    f"_LAST_STAGE is assigned inside "
                                    f"{node.name}() at line {sub.lineno} — "
                                    f"only set_stage() may assign it")


class WriteJobStateSitesTest(unittest.TestCase):
    def _check_stage_arg(self, arg, where):
        literal = _const_str(arg)
        if literal is not None:
            self.assertIn(
                literal, hjs.JOB_STAGES,
                f"{where} writes stage literal {literal!r}, which is not "
                f"a job stage")
            return
        if _is_mapping_get(arg):
            # WORKER_PHASE_TO_JOB_STAGE.get(input, default)
            self.assertGreaterEqual(
                len(arg.args), 1,
                f"{where} mapping lookup has no input argument")
            source = arg.args[0]
            self.assertIsInstance(
                source, ast.Name,
                f"{where} mapping lookup input is not a plain name: "
                f"{ast.dump(source)[:120]}")
            self.assertIn(
                source.id, ("name", "_LAST_STAGE"),
                f"{where} mapping lookup input {source.id!r} is not "
                f"`name` or `_LAST_STAGE` — its value universe is not "
                f"proven mapped by this gate")
            # A lookup without an explicit default (.get(x)) would write
            # None for an unmapped phase — never allowed.
            self.assertEqual(
                len(arg.args), 2,
                f"{where} mapping lookup must carry an explicit default")
            default = arg.args[1]
            default_literal = _const_str(default)
            if default_literal is not None:
                self.assertIn(
                    default_literal, hjs.JOB_STAGES,
                    f"{where} mapping lookup default "
                    f"{default_literal!r} is not a job stage")
            else:
                # Fail-closed raw fallback: allowed only when it is
                # the same name as the input (i.e. .get(x, x)) —
                # proven unreachable for audited phases by the
                # set_stage / track-name / _LAST_STAGE tests, and
                # refused loudly by read_job_state() if ever hit.
                self.assertIsInstance(
                    default, ast.Name,
                    f"{where} mapping lookup default is neither a "
                    f"job-stage literal nor the input name: "
                    f"{ast.dump(default)[:120]}")
                self.assertEqual(
                    default.id, source.id,
                    f"{where} mapping lookup default {default.id!r} "
                    f"differs from its input {source.id!r} — only "
                    f".get(x, x) or a job-stage literal is allowed")
            return
        self.fail(
            f"{where} writes a stage through an unprovable expression "
            f"({ast.dump(arg)[:160]}): only a JOB_STAGES literal or a "
            f"WORKER_PHASE_TO_JOB_STAGE.get(...) lookup is allowed")

    def test_every_write_job_state_stage_is_legal(self):
        sites = _StageSites()
        self.assertGreater(len(sites.write_calls), 0,
                           "no write_job_state() call sites found — the "
                           "gate scanned nothing")
        for call in sites.write_calls:
            arg = _stage_arg_of_write_call(call)
            self._check_stage_arg(
                arg, f"write_job_state() at line {call.lineno}")

    def test_no_dynamically_built_stage_name_in_write_job_state(self):
        sites = _StageSites()
        for call in sites.write_calls:
            arg = _stage_arg_of_write_call(call)
            self.assertFalse(
                _contains_dynamic_string(arg),
                f"write_job_state() at line {call.lineno} builds its "
                f"stage dynamically (concatenation / f-string) — dynamic "
                f"stage names must be handled explicitly, never built inline")

    def test_failed_write_keeps_failed_literal_with_diagnostic_error_stage(self):
        # Exclusion, documented not silent: the FAILED state's `stage`
        # field is the literal, while the raw phase travels only in
        # error["stage"] (diagnostics, pinned by
        # test_v031_hotfix_stage_contract.py).
        sites = _StageSites()
        failed_writes = []
        for call in sites.write_calls:
            arg = _stage_arg_of_write_call(call)
            if _const_str(arg) == "FAILED":
                failed_writes.append(call)
        self.assertEqual(len(failed_writes), 1,
                         f"expected exactly one FAILED write, found "
                         f"{len(failed_writes)}")
        error_kw = [kw for kw in failed_writes[0].keywords
                    if kw.arg == "error"]
        self.assertEqual(len(error_kw), 1,
                         "the FAILED write must carry worker_failure_info")
        self.assertTrue(
            _is_call_to(error_kw[0].value, "worker_failure_info"),
            "the FAILED write's error must come from worker_failure_info")


class ControllerMapTest(unittest.TestCase):
    def test_controller_stage_map_keys_equal_job_stages(self):
        self.assertEqual(set(hc.JOB_STAGE_TO_MANIFEST_STAGE),
                         set(hjs.JOB_STAGES),
                         "every job stage the worker can write must have a "
                         "controller manifest branch, and vice versa")


class ReadJobStateStrictnessTest(unittest.TestCase):
    def _write_raw_state(self, root, stage):
        (root / hjs.JOB_STATE_FILENAME).write_text(
            json.dumps({"run_id": "h3-r5-gate", "variant": "A",
                        "stage": stage,
                        "updated_at_utc": "2026-11-24T00:00:00Z",
                        "heartbeat_utc": "2026-11-24T00:00:00Z"}),
            encoding="utf-8")

    def test_read_job_state_rejects_unknown_stage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for bad in ("setup_parallel", "init", "SOME_NEW_STAGE", ""):
                if bad == "":
                    continue  # empty stage is the missing-field case
                self._write_raw_state(root, bad)
                with self.assertRaises(ValueError, msg=f"stage {bad!r}"):
                    hjs.read_job_state(root)

    def test_read_job_state_accepts_every_job_stage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for stage in hjs.JOB_STAGES:
                hjs.write_job_state(root, "h3-r5-gate", "A", stage)
                self.assertEqual(hjs.read_job_state(root)["stage"], stage)


class InitMappingRuntimeTest(unittest.TestCase):
    def test_set_stage_init_writes_job_started(self):
        # Runtime proof of the new mapping: the worker's initial phase
        # no longer leaks raw into the job state.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            saved = (hrw.ROOT, hrw._JOB_RUN_ID, hrw._JOB_VARIANT,
                     hrw._LAST_STAGE)
            hrw.ROOT = root
            hrw._JOB_RUN_ID = "h3-r5-gate"
            hrw._JOB_VARIANT = "A"
            hrw._LAST_STAGE = "init"
            try:
                hrw.set_stage("init")
                self.assertEqual(
                    hjs.read_job_state(root)["stage"], "JOB_STARTED")
                # A token refresh before the first real set_stage (the
                # CHANGELOG v0.3.1 §5.2 residual case) rewrites the
                # mapped stage, not the raw phase.
                from types import SimpleNamespace
                hrw._LAST_STAGE = "init"
                hrw._request_token_refresh(
                    SimpleNamespace(token_id="tok-r5-init"))
                self.assertEqual(
                    hjs.read_job_state(root)["stage"], "JOB_STARTED")
            finally:
                (hrw.ROOT, hrw._JOB_RUN_ID, hrw._JOB_VARIANT,
                 hrw._LAST_STAGE) = saved


if __name__ == "__main__":
    unittest.main()

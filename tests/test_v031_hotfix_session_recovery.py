"""TASK-HOTFIX-016 (v0.3.1) Phase 3/4: P0-B session identity persistence
+ evidence-based resume + attempt counting.

Covers fault-injection tests T03-T18 (T01/T02 live in
tests/test_v031_hotfix_stage_contract.py). Every test drives the real
controller / watchdog / lease-registry code on a ChainHarness temp
ROOT; the only fakes are the live Colab layer (run_cli / inspect /
query_session_state) and the Drive depot, exactly as in
tests/test_fault_injection.py.

The crash window is modelled at the durable-state level: the "first
process" drives the allocation preamble (allocate -> record_session ->
_register_gpu_lease -> strict manifest write -> run_cli new) and then
stops dead, the way a VM restart stops a process — no finally runs,
no FAILED marking, no stop attempt. A second harness over the same
on-disk state (h.restart()) is the recovered controller. That is the
faithful model: a real VM death runs no cleanup code at all.
"""
import argparse
import datetime
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h3_controller as hc
import h3_run_manifest as rm
import h3_session_watchdog as hwd
from h3_gpu_lease_registry import LeaseRegistry
from h3_session_watchdog import SessionState
from tests.test_fault_injection import ChainHarness, RUN_ID, TASK, _args
from tests.test_fault_injection import _FIXTURE_DIR

OWNER = "owner-test-0001"
OTHER_OWNER = "owner-someone-else"
DEADLINE = "2030-01-01T00:00:00Z"
SHA = "ab" * 32


class _SimulatedVMDeath(BaseException):
    """A process death mid-call: no handler in the controller catches it,
    mirroring a VM restart (no finally, no cleanup)."""


def _fmt(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _future_iso(hours=6):
    return _fmt(datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=hours))


def _past_iso(hours=1):
    return _fmt(datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=hours))


def _spec_file(root, task=TASK):
    spec = {
        "task_id": task, "output_prefix": "A",
        "ref_image_1": {"sha256": SHA, "local_path": "assets/original_poster.jpg"},
        "prompt": "test prompt", "seed": 42,
        "width": 736, "height": 1312, "fps": 24,
        "animation_frames": 226, "tail_card_frames": 0, "steps": 25,
        "output_width": 1080, "output_height": 1920,
    }
    path = root / "spec.json"
    path.write_text(json.dumps(spec))
    return str(path)


def _deliver_args(root, task=TASK, **over):
    args = argparse.Namespace(
        action="deliver", production_task_id=task,
        task_spec=_spec_file(root, task), variant=None, run_id="",
        max_task_attempts=2, task_deadline_utc="",
        execute=True, confirm_gpu=True, authorize_task_gpu=True,
        allow_g4=True, allow_a100_highmem=True, fresh_inference=True,
        project_budget_cu=30.0, per_session_budget_cu=25.0,
        authorize_new_task=True, prior_unbilled_reserve_cu=0.14,
        max_wall_sec=7200, poll_interval_sec=60,
        gpu_policy="G4_THEN_A100_HIGHMEM", comfy_ref="c" * 40,
        workflow_api=str(_FIXTURE_DIR / "workflow_api.json"),
        cache_manifest=str(_FIXTURE_DIR / "verified_manifest.json"),
        drive_source="download",
        drive_cache_dir="/content/drive/MyDrive/Muse-H3-Model-Depot",
        colab_auth="adc", budget_cu=0.0, max_rate_cu_h=14.0,
        approve_interactive_drive_mount=False, auto_recover=False)
    for k, v in over.items():
        setattr(args, k, v)
    return args


def _write_ledger(root, task=TASK, spent=0.0, deadline=None, hard_stop=132.77):
    d = root / "model_cache"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"production_task_id": task, "project_budget_cu": 30.0,
           "hard_stop_balance_cu": hard_stop, "max_observed_spent_cu": spent,
           "prior_task_unbilled_reserve_cu": 0.14, "run_ids": [RUN_ID],
           "authorized": True}
    if deadline is not None:
        doc["deadline_utc"] = deadline
    (d / f"h3_production_budget_{task}.json").write_text(json.dumps(doc))
    return doc


class _RecoveryBase(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)
        self.registry = LeaseRegistry(
            mirror=None,
            path=self.h.root / "model_cache" / "h3_gpu_leases.json")
        self._real_load = hwd.load_managed_sessions
        self._patches = [
            mock.patch.object(hc, "_lease_registry", return_value=self.registry),
            mock.patch("h3_gpu_lease_registry.current_owner_id",
                       return_value=OWNER),
            mock.patch.object(
                hwd, "load_managed_sessions",
                lambda path=None: self._real_load(path=self.h.state_path)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.h.close)
        self.h.unpatch(hc, "_register_gpu_lease")
        # The real lease-stop confirmation (local tmp registry only);
        # the harness stubs it to a no-op.
        self.h.unpatch(hc, "_confirm_gpu_lease_stopped")

    def _restart(self):
        """A recovered controller over the same on-disk state."""
        h2 = self.h.restart()
        self.h = h2
        self.h.unpatch(hc, "_register_gpu_lease")
        self.h.unpatch(hc, "_confirm_gpu_lease_stopped")
        return h2

    def _preamble(self, gpu="G4", run_id=RUN_ID, task=TASK,
                  write_manifest=True):
        """Drive the allocation preamble the way _run_variant_inner does
        (allocate -> record_session -> _register_gpu_lease -> strict
        manifest write). Returns the session id. With
        write_manifest=False the durable state reproduces v0.3.0: the
        registries know the session, the manifest does not."""
        sid = hc.allocate_managed_session(gpu, run_id)
        hwd.record_session(sid, gpu, run_id, DEADLINE,
                           production_task_id=task, status="allocating")
        hc._register_gpu_lease(sid, gpu, run_id, task, DEADLINE, 25.0)
        if write_manifest:
            hc._manifest_update_strict(run_id, session_id=sid)
        return sid

    def _sessionless_state(self, sessions, task=TASK, create_manifest=True):
        """A v0.3.0-shaped interrupted run: manifest TASK_AUTHORIZED with
        no session id, but the lease + managed registries hold `sessions`
        (list of (gpu, session_id) already registered by the caller, or
        gpu names to register here). Returns the session ids."""
        h = self.h
        if create_manifest:
            h.construct_manifest("TASK_AUTHORIZED")
        sids = []
        for item in sessions:
            if isinstance(item, tuple):
                gpu, sid = item
            else:
                gpu = item
                sid = hc.allocate_managed_session(gpu, RUN_ID)
            hwd.record_session(sid, gpu, RUN_ID, DEADLINE,
                               production_task_id=task, status="allocating")
            hc._register_gpu_lease(sid, gpu, RUN_ID, task, DEADLINE, 25.0)
            sids.append(sid)
        return sids

    def _no_drive_archive(self):
        return mock.patch.object(hc, "recover_from_drive_archive",
                                 return_value=None)

    def _finish_mocks(self):
        """The downstream finish path (poll says VIDEO_READY, download +
        stop + Drive deliver stubbed) — identical in shape to
        ResumeLiveSessionTest._finish_mocks. The harness's own download
        fake returns metadata without writing the file, so the download
        is stubbed here with the file-writing fake (the resumed finish
        path validates the local MP4 before delivering)."""
        self._po(hc, "stop_and_verify_session",
                 return_value={"session": "muse-h3-test",
                               "stopped_confirmed": True})

        def _fake_download(session, variant, run_id, spec=None):
            mp4 = self.h.out_root / run_id / f"{variant}.mp4"
            mp4.parent.mkdir(parents=True, exist_ok=True)
            mp4.write_bytes(b"fake-mp4")
            return dict(self.h.video_info)

        self._po(hc, "download_and_validate_variant",
                 side_effect=_fake_download)
        self._po(hc, "deliver_variant_to_drive",
                 return_value={"state": "DELIVERY_COMPLETED", "variant": "A",
                               "run_id": RUN_ID, "drive_file_id": "drive-file-3"})

    def _po(self, target, attr, **kw):
        p = mock.patch.object(target, attr, **kw)
        p.start()
        self.addCleanup(p.stop)
        return p

    def _new_calls(self, harness=None):
        h = harness or self.h
        return [c for c in h.run_cli_calls if c and c[0] == "new"]


class PreRegistrationTest(_RecoveryBase):
    """T03: a pre-allocation persistence failure must refuse the GPU
    before `colab new` is ever invoked, and the attempt must stay
    traceable in the registries (T03b)."""

    def test_strict_write_failure_refuses_colab_new(self):
        h = self.h
        with mock.patch.object(
                hc, "_manifest_update_strict",
                side_effect=RuntimeError("simulated manifest disk failure")):
            with self.assertRaises(RuntimeError):
                h.run()
        self.assertEqual(self._new_calls(), [])
        m = h.manifest()
        self.assertEqual(m["task_state"], "FAILED")
        self.assertIn("simulated manifest disk failure",
                      m.get("last_error") or "")
        # The attempt was registered BEFORE the gate, so it is traceable
        # even though no GPU exists.
        self.assertEqual(len(self.registry.list_leases()), 1)
        self.assertEqual(len(h.managed_sessions()), 1)
        self.assertEqual(hc._attempts_used(TASK), 1)

    def test_orphan_allocating_record_resolves_on_reconcile(self):
        # T03b: the failed run's handler already resolved the
        # never-created session (lease + managed record marked stopped),
        # so a later reconcile finds nothing allocating and nothing
        # blocks a fresh allocation. The attempt still counts.
        h = self.h
        with mock.patch.object(
                hc, "_manifest_update_strict",
                side_effect=RuntimeError("simulated manifest disk failure")):
            with self.assertRaises(RuntimeError):
                h.run()
        sid = self.registry.list_leases()[0]["session_id"]
        self.assertEqual(
            h.managed_sessions()[0]["status"], "stopped")
        h2 = self._restart()
        h2.query_plan = [SessionState.STOPPED]
        res = hwd.reconcile_on_startup(colab_auth="adc",
                                       path=h2.state_path)
        self.assertEqual(res.get("failed_allocations"), [])
        self.assertEqual(res.get("managed_active_sessions"), [])
        self.assertEqual(hc._attempts_used(TASK), 1)


class CrashRecoveryTest(_RecoveryBase):
    """T04-T07: the allocation crash windows, modelled as a dead first
    process (no finally) + a recovered controller over the same disk."""

    def test_crash_after_preregistration_before_new(self):
        # T04: records exist, `new` never ran. Resume must close the run
        # as FAILED (no video anywhere) and count the attempt.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        sid = self._preamble("G4")
        # The crash: the first process simply stops here.
        h2 = self._restart()
        h2.query_plan = [SessionState.STOPPED]
        if h2.mp4.is_file():
            h2.mp4.unlink()
        with self._no_drive_archive():
            res = h2.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("nothing recoverable", res["reason"])
        m = h2.manifest()
        self.assertEqual(m["task_state"], "FAILED")
        self.assertEqual(m["session_id"], sid)
        self.assertEqual(hc._attempts_used(TASK), 1)
        self.assertEqual(self._new_calls(h2), [])

    def test_crash_during_new_leaves_no_untraceable_session(self):
        # T05: `new` was invoked and the process died inside it. The
        # manifest, lease and managed records must all name the same
        # session, and the recovered controller must resolve it by live
        # query (server says STOPPED: the call never completed).
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        sid = self._preamble("G4")
        with self.assertRaises(_SimulatedVMDeath):
            hc.run_cli(["new", "-s", sid, "-g", "G4"], timeout=420)
            raise _SimulatedVMDeath("VM restarted during `colab new`")
        self.assertEqual(self._new_calls(), [["new", "-s", sid, "-g", "G4"]])
        h2 = self._restart()
        h2.query_plan = [SessionState.STOPPED]
        if h2.mp4.is_file():
            h2.mp4.unlink()
        with self._no_drive_archive():
            res = h2.resume()
        self.assertFalse(res["resumed"])
        self.assertEqual(h2.manifest()["session_id"], sid)
        self.assertEqual(
            self.registry.list_leases()[0]["session_id"], sid)
        self.assertEqual(
            h2.managed_sessions()[0]["session_id"], sid)
        self.assertEqual(hc._attempts_used(TASK), 1)

    def test_cli_success_then_crash_before_manifest_update_recovers_live(self):
        # T06: `new` returned success but the process died before the
        # post-allocation manifest update. The pre-allocation write alone
        # must let the recovered controller adopt the LIVE session and
        # finish the run without a second GPU.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        sid = self._preamble("G4")
        hc.run_cli(["new", "-s", sid, "-g", "G4"], timeout=420)
        # The crash: no post-allocation manifest update ever runs.
        h2 = self._restart()
        h2.query_plan = [SessionState.ACTIVE]
        if h2.mp4.is_file():
            h2.mp4.unlink()
        self._finish_mocks()
        res = h2.resume()
        self.assertTrue(res["resumed"])
        m = h2.manifest()
        self.assertEqual(m["task_state"], "TASK_COMPLETED")
        self.assertEqual(m["session_id"], sid)
        self.assertEqual(self._new_calls(h2), [])
        self.assertEqual(hc._attempts_used(TASK), 1)

    def test_post_allocation_manifest_failure_is_not_silent(self):
        # T07: the post-allocation strict write fails after a successful
        # `new`. The run must FAIL (identity not silently lost) and the
        # session must be stopped, not left billing.
        h = self.h
        real_strict = hc._manifest_update_strict

        def _strict_once(run_id, **kw):
            # The pre-allocation write lands; the post-allocation one fails.
            if kw.get("task_state") == "GPU_ALLOCATED":
                raise RuntimeError("simulated manifest disk failure")
            return real_strict(run_id, **kw)

        with mock.patch.object(hc, "_manifest_update_strict",
                               side_effect=_strict_once):
            with self.assertRaises(RuntimeError):
                h.run()
        self.assertEqual(len(self._new_calls()), 1)
        m = h.manifest()
        self.assertEqual(m["task_state"], "FAILED")
        self.assertIn("simulated manifest disk failure",
                      m.get("last_error") or "")
        # The session the loop created was stopped by the outer finally.
        self.assertEqual(h.stop_cli_sessions, [m["session_id"]])


class SessionlessResumeTest(_RecoveryBase):
    """T08-T14: a manifest with no session id must be resumed from
    registry evidence — never by guessing, never by claiming a foreign
    session, never by treating UNKNOWN as STOPPED."""

    def test_multi_candidate_g4_g4_a100_all_stopped_closes_run(self):
        # T08: the real E2E shape — two failed G4 allocations plus an
        # A100 interrupted mid-allocation. Every candidate must be
        # identified by live query (3 queries, not first-or-last), the
        # run must close FAILED, and one run must count as one attempt.
        h = self.h
        sids = self._sessionless_state(["G4", "G4", "A100_HIGH_MEM"])
        h.query_plan = [SessionState.STOPPED] * 3
        if h.mp4.is_file():
            h.mp4.unlink()
        with self._no_drive_archive():
            res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("nothing recoverable", res["reason"])
        self.assertEqual(h.query_idx, 3)
        self.assertEqual(h.manifest()["task_state"], "FAILED")
        self.assertEqual(hc._attempts_used(TASK), 1)
        self.assertEqual(self._new_calls(), [])
        # No blind stop: the sessions were already STOPPED, so no CLI
        # stop is issued for them.
        self.assertEqual(h.stop_cli_sessions, [])
        # Every candidate was confirmed stopped in the registries.
        for sid in sids:
            self.assertEqual(
                [r for r in self.registry.list_leases()
                 if r["session_id"] == sid][0]["last_confirmed_state"],
                "STOPPED")

    def test_unique_active_session_is_adopted(self):
        # T09: exactly one ACTIVE candidate whose run/task/owner match
        # is adopted under the existing gates and the run finishes
        # without a second GPU.
        h = self.h
        sid = self._sessionless_state(["G4"])[0]
        h.query_plan = [SessionState.ACTIVE]
        if h.mp4.is_file():
            h.mp4.unlink()
        self._finish_mocks()
        res = h.resume()
        self.assertTrue(res["resumed"])
        m = h.manifest()
        self.assertEqual(m["task_state"], "TASK_COMPLETED")
        self.assertEqual(m["session_id"], sid)
        self.assertEqual(self._new_calls(), [])
        self.assertTrue(any(
            e.get("event") == "SESSION_RECOVERED_FROM_REGISTRY"
            for e in h.audit_events()))

    def test_unknown_session_blocks_new_gpu(self):
        # T10: the candidate's live state cannot be confirmed. Resume
        # refuses, and deliver refuses a second GPU as well.
        h = self.h
        self._sessionless_state(["G4"])
        h.query_plan = [SessionState.UNKNOWN]
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("UNKNOWN", res["reason"])
        self.assertEqual(self._new_calls(), [])
        self.assertEqual(h.stop_cli_sessions, [])
        h2 = self._restart()
        h2.query_plan = [SessionState.UNKNOWN]
        result = hc.deliver_task(_deliver_args(h2.root))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "blocked_live_session")
        self.assertEqual(self._new_calls(h2), [])
        self.assertEqual(h2.stop_cli_sessions, [])

    def test_all_stopped_no_video_closes_then_new_attempt_runs(self):
        # T11: all candidates STOPPED, no video anywhere. The interrupted
        # run closes FAILED (audited), and deliver may then start a fresh
        # attempt under the gates — one new GPU, attempt 2/2. Full
        # completion of the second attempt is NOT asserted here: the
        # harness's job-probe and download fakes are hard-coded to the
        # first run id (the second attempt's own completion path is the
        # baseline chain, covered by BaselineChainTest). What this test
        # proves is the recovery contract: close, then a new attempt
        # starts instead of blocking or double-counting.
        h = self.h
        self._sessionless_state(["G4", "G4", "A100_HIGH_MEM"])
        h.query_plan = [SessionState.STOPPED]
        if h.mp4.is_file():
            h.mp4.unlink()
        _write_ledger(h.root, deadline=_future_iso())
        ledger_before = (h.root / "model_cache"
                         / f"h3_production_budget_{TASK}.json").read_bytes()
        with self._no_drive_archive():
            result = hc.deliver_task(_deliver_args(h.root))
        self.assertEqual(h.manifest()["task_state"], "FAILED")
        steps = [s.get("step") for s in result.get("steps") or []]
        self.assertIn("resume_inflight", steps)
        self.assertIn("new_attempt", steps)
        self.assertNotIn(result["outcome"],
                         ("blocked_live_session", "resume_refused"))
        self.assertEqual(len(self._new_calls()), 1)
        self.assertEqual(hc._attempts_used(TASK), 2)
        # The recovery never resets the task ledger.
        self.assertEqual((h.root / "model_cache"
                          / f"h3_production_budget_{TASK}.json").read_bytes(),
                         ledger_before)

    def test_all_stopped_with_drive_archive_recovers_without_gpu(self):
        # T12: all candidates STOPPED and a Drive archive exists. The
        # 0-GPU recovery wins; no GPU is allocated.
        h = self.h
        self._sessionless_state(["G4"])
        h.query_plan = [SessionState.STOPPED]
        if h.mp4.is_file():
            h.mp4.unlink()
        # The stub stands in for the Drive layer only: it performs the
        # same manifest moves the real recover_from_drive_archive makes
        # (VIDEO_VALIDATED -> ARTIFACT_DURABLE -> TASK_COMPLETED) and
        # returns the real function's success shape.
        def _fake_recover(run_id, variant, manifest, spec=None, drive=None):
            rm.update_run(h.root, run_id, task_state="VIDEO_VALIDATED",
                          last_verified_stage="VIDEO_VALIDATED",
                          session_state="STOPPED",
                          artifacts={"local_sha256": h.sha,
                                     "local_frames": 274,
                                     "delivery_state": "COMPLETE_LOCAL"})
            rm.update_run(h.root, run_id, task_state="ARTIFACT_DURABLE",
                          session_state="STOPPED")
            rm.update_run(h.root, run_id, task_state="DRIVE_VERIFIED",
                          last_verified_stage="DRIVE_VERIFIED",
                          session_state="STOPPED",
                          artifacts={"drive_file_id": "fid-archive-1",
                                     "drive_output_folder_id": "fld-run-1",
                                     "local_sha256": h.sha,
                                     "local_frames": 274,
                                     "delivery_state": "DELIVERED"})
            rm.update_run(h.root, run_id, task_state="TASK_COMPLETED",
                          session_state="STOPPED")
            return {
                "resumed": True,
                "outcome": "recovered_from_drive_archive_0gpu",
                "run_id": run_id, "variant": variant,
                "drive_file_id": "fid-archive-1",
                "delivery": {"state": "DELIVERY_COMPLETED",
                             "drive_file_id": "fid-archive-1"}}

        with mock.patch.object(hc, "recover_from_drive_archive",
                               side_effect=_fake_recover):
            res = h.resume()
        self.assertTrue(res["resumed"])
        self.assertEqual(res["outcome"], "recovered_from_drive_archive_0gpu")
        self.assertEqual(h.manifest()["task_state"], "TASK_COMPLETED")
        self.assertEqual(self._new_calls(), [])

    def test_conflicting_active_candidates_refuse_recovery(self):
        # T13: two ACTIVE candidates for the same run. No automatic
        # choice is made; resume and deliver both refuse.
        h = self.h
        self._sessionless_state(["G4", "A100_HIGH_MEM"])
        h.query_plan = [SessionState.ACTIVE, SessionState.ACTIVE]
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("conflicting", res["reason"])
        self.assertEqual(self._new_calls(), [])
        self.assertEqual(h.stop_cli_sessions, [])
        h2 = self._restart()
        h2.query_plan = [SessionState.ACTIVE, SessionState.ACTIVE]
        result = hc.deliver_task(_deliver_args(h2.root))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "blocked_live_session")
        self.assertEqual(self._new_calls(h2), [])
        self.assertEqual(h2.stop_cli_sessions, [])

    def test_foreign_and_other_task_leases_never_claimed(self):
        # T14: a lease for this run under a different account, and a live
        # lease for another task, must never be adopted. The refusal names
        # the foreign account; the other task's session is never queried.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        foreign_sid = hc.allocate_managed_session("G4", RUN_ID)
        hwd.record_session(foreign_sid, "G4", RUN_ID, DEADLINE,
                           production_task_id=TASK, status="allocating")
        with mock.patch("h3_gpu_lease_registry.current_owner_id",
                        return_value=OTHER_OWNER):
            hc._register_gpu_lease(foreign_sid, "G4", RUN_ID, TASK,
                                   DEADLINE, 25.0)
        other_sid = hc.allocate_managed_session("A100_HIGH_MEM", "h3-other-run")
        hwd.record_session(other_sid, "A100_HIGH_MEM", "h3-other-run",
                           DEADLINE, production_task_id="other-task",
                           status="allocating")
        hc._register_gpu_lease(other_sid, "A100_HIGH_MEM", "h3-other-run",
                               "other-task", DEADLINE, 25.0)
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("different account", res["reason"])
        self.assertEqual(h.query_idx, 0)
        self.assertEqual(h.manifest().get("session_id") or "", "")
        self.assertEqual(self._new_calls(), [])
        self.assertEqual(h.stop_cli_sessions, [])
        # The foreign lease is still allocation evidence for this task's
        # run: attempt counting never undercounts an uncertain state.
        self.assertEqual(hc._attempts_used(TASK), 1)
        self.assertEqual(hc._attempts_used("other-task"), 1)
        self.assertEqual(h.stop_cli_sessions, [])


class AttemptCountingTest(_RecoveryBase):
    """T15: _attempts_used must count verified allocation evidence from
    the manifest OR the lease registry — never undercounting a run whose
    manifest lost its session id, never triple-counting one run's
    G4 -> G4 -> A100 rounds."""

    def test_two_runs_both_allocated_count_two(self):
        # The real E2E shape: run 1 has its session id in the manifest,
        # run 2 allocated but its manifest is empty. Both count.
        run1, run2 = "h3-v031-run1", "h3-v031-run2"
        rm.create_run_manifest(self.h.root, TASK, run1, variant="A",
                               budget={})
        rm.update_run(self.h.root, run1, task_state="GPU_ALLOCATED",
                      session_id="muse-h3-run1", session_state="ACTIVE")
        rm.create_run_manifest(self.h.root, TASK, run2, variant="A",
                               budget={})
        sid2 = hc.allocate_managed_session("A100_HIGH_MEM", run2)
        hwd.record_session(sid2, "A100_HIGH_MEM", run2, DEADLINE,
                           production_task_id=TASK, status="allocating")
        hc._register_gpu_lease(sid2, "A100_HIGH_MEM", run2, TASK,
                               DEADLINE, 25.0)
        self.assertEqual(hc._attempts_used(TASK), 2)

    def test_one_run_three_allocations_counts_one(self):
        rm.create_run_manifest(self.h.root, TASK, RUN_ID, variant="A",
                               budget={})
        for gpu in ("G4", "G4", "A100_HIGH_MEM"):
            sid = hc.allocate_managed_session(gpu, RUN_ID)
            hwd.record_session(sid, gpu, RUN_ID, DEADLINE,
                               production_task_id=TASK, status="allocating")
            hc._register_gpu_lease(sid, gpu, RUN_ID, TASK, DEADLINE, 25.0)
        self.assertEqual(hc._attempts_used(TASK), 1)

    def test_no_gpu_request_counts_zero(self):
        rm.create_run_manifest(self.h.root, TASK, RUN_ID, variant="A",
                               budget={})
        self.assertEqual(hc._attempts_used(TASK), 0)

    def test_unreadable_registry_fails_closed(self):
        rm.create_run_manifest(self.h.root, TASK, RUN_ID, variant="A",
                               budget={})
        self.registry.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            hc._attempts_used(TASK)


class DeliverGateTest(_RecoveryBase):
    """T16-T18: the existing deliver gates (deadline, budget incl.
    unbilled reserve, code-version) still refuse a new GPU — the
    sessionless resume path must not become a way around them."""

    def _failed_run(self):
        rm.create_run_manifest(self.h.root, TASK, RUN_ID, variant="A",
                               budget={})
        rm.update_run(self.h.root, RUN_ID, task_state="FAILED",
                      session_state="STOPPED",
                      last_error="RuntimeError: earlier attempt failed")

    def test_deadline_expired_refuses_new_gpu(self):
        self._failed_run()
        _write_ledger(self.h.root, deadline=_past_iso())
        result = hc.deliver_task(_deliver_args(self.h.root))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "deadline_passed")
        self.assertEqual(self._new_calls(), [])

    def test_insufficient_budget_incl_reserve_refuses_new_gpu(self):
        self._failed_run()
        _write_ledger(self.h.root, deadline=_future_iso(), hard_stop=149.5)
        self.h.unpatch(hc, "allowed_runtime_seconds")
        result = hc.deliver_task(_deliver_args(self.h.root))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "budget_insufficient")
        self.assertEqual(self._new_calls(), [])

    def test_code_version_mismatch_refuses_cross_version_takeover(self):
        # T18 / B6: a v0.3.0 run must not be force-taken-over by the
        # v0.3.1 controller. The version gate fires before any
        # sessionless recovery is even considered.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED", code_commit="oldcommit")
        self._sessionless_state(["G4"], create_manifest=False)
        with mock.patch.object(hc, "_current_source_commit",
                               return_value="newcommit"):
            res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("code mismatch", res["reason"])
        self.assertEqual(h.query_idx, 0)
        self.assertEqual(h.manifest().get("session_id") or "", "")
        self.assertEqual(self._new_calls(), [])


class _FakeMirror:
    """Minimal Drive lease-mirror stub (read-only) for O09: the mirror
    is the only evidence source for the run."""

    def __init__(self, doc):
        self._doc = doc

    def read_mirror(self):
        return self._doc


def _fake_drive_recover(h):
    """Stub for recover_from_drive_archive performing the real manifest
    moves (VIDEO_VALIDATED -> ARTIFACT_DURABLE -> DRIVE_VERIFIED ->
    TASK_COMPLETED); returns the real success shape. Same shape as the
    T12 stub, reused by O12."""

    def _recover(run_id, variant, manifest, spec=None, drive=None):
        rm.update_run(h.root, run_id, task_state="VIDEO_VALIDATED",
                      last_verified_stage="VIDEO_VALIDATED",
                      session_state="STOPPED",
                      artifacts={"local_sha256": h.sha,
                                 "local_frames": 274,
                                 "delivery_state": "COMPLETE_LOCAL"})
        rm.update_run(h.root, run_id, task_state="ARTIFACT_DURABLE",
                      session_state="STOPPED")
        rm.update_run(h.root, run_id, task_state="DRIVE_VERIFIED",
                      last_verified_stage="DRIVE_VERIFIED",
                      session_state="STOPPED",
                      artifacts={"drive_file_id": "fid-archive-1",
                                 "drive_output_folder_id": "fld-run-1",
                                 "local_sha256": h.sha,
                                 "local_frames": 274,
                                 "delivery_state": "DELIVERED"})
        rm.update_run(h.root, run_id, task_state="TASK_COMPLETED",
                      session_state="STOPPED")
        return {"resumed": True,
                "outcome": "recovered_from_drive_archive_0gpu",
                "run_id": run_id, "variant": variant,
                "drive_file_id": "fid-archive-1",
                "delivery": {"state": "DELIVERY_COMPLETED",
                             "drive_file_id": "fid-archive-1"}}

    return _recover


class OwnerVerificationTest(_RecoveryBase):
    """O01-O12 (TASK-HOTFIX-016B): session ownership must be PROVEN
    before any sessionless resume may claim a session.

    The v0.3.1 finder degraded a failed current_owner_id() lookup to
    owner=None and then admitted every record as a candidate whenever
    the owner check could not run — including foreign leases and
    ownerless records. These tests prove the hole first (O01/O02/O03/
    O07/O08/O09-ownerless/O11 fail against the unfixed code) and pin
    the fail-closed contract afterwards:

    - the current owner must be verifiable (O01/O02 -> OWNER_ID_UNVERIFIABLE);
    - a candidate needs a trusted lease/mirror record whose owner_id
      matches the current owner (O03/O08/O09-ownerless refuse);
    - a managed record never carries owner_id, so it can only
      corroborate a session a trusted source already proves (O06);
    - any explicit foreign owner, or two trusted sources disagreeing
      about one session, refuse the claim (O05/O07/O09-foreign);
    - a valid candidate next to foreign evidence is still adopted, and
      the foreign session is never touched (O10);
    - after an owner refusal, deliver opens no second GPU and leaves
      the ledger alone (O11);
    - the matching-owner ACTIVE / STOPPED recovery chains keep working
      (O04/O12)."""

    def _lease_doc(self):
        return json.loads(self.registry.path.read_text())

    def _save_lease_doc(self, doc):
        self.registry.path.write_text(json.dumps(doc))

    def _strip_lease_owner(self, sid):
        """Simulate a legacy/corrupt lease record carrying no owner_id."""
        doc = self._lease_doc()
        for lease in doc["leases"]:
            if lease.get("session_id") == sid:
                lease.pop("owner_id", None)
        self._save_lease_doc(doc)

    def _duplicate_lease_with_owner(self, sid, owner):
        """Simulate a merged/corrupt registry: two lease records for
        one session id, disagreeing about its owner."""
        doc = self._lease_doc()
        orig = next(l for l in doc["leases"]
                    if l.get("session_id") == sid)
        clone = dict(orig)
        clone["owner_id"] = owner
        doc["leases"].append(clone)
        self._save_lease_doc(doc)

    def _assert_refused_untouched(self, res, h=None):
        """A refused resume must not query, stop, allocate, or mutate
        the manifest: the run stays TASK_AUTHORIZED with no session id,
        exactly as the interrupted run left it."""
        h = h or self.h
        self.assertFalse(res["resumed"])
        self.assertEqual(h.query_idx, 0)
        self.assertEqual(self._new_calls(h), [])
        self.assertEqual(h.stop_cli_sessions, [])
        m = h.manifest()
        self.assertEqual(m["task_state"], "TASK_AUTHORIZED")
        self.assertEqual(m.get("session_id") or "", "")

    def test_owner_lookup_failure_refuses_recovery(self):
        # O01: current_owner_id() raises -> OWNER_ID_UNVERIFIABLE.
        h = self.h
        self._sessionless_state(["G4"])
        h.query_plan = [SessionState.ACTIVE]
        with mock.patch("h3_gpu_lease_registry.current_owner_id",
                        side_effect=RuntimeError("whoami down")):
            res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("OWNER_ID_UNVERIFIABLE", res["reason"])
        self._assert_refused_untouched(res)

    def test_owner_lookup_empty_identity_refuses_recovery(self):
        # O02: None or "" from current_owner_id() is not an identity.
        h = self.h
        self._sessionless_state(["G4"])
        h.query_plan = [SessionState.ACTIVE]
        for bad in (None, ""):
            with self.subTest(bad=bad):
                with mock.patch("h3_gpu_lease_registry.current_owner_id",
                                return_value=bad):
                    res = h.resume()
                self.assertFalse(res["resumed"])
                self.assertIn("OWNER_ID_UNVERIFIABLE", res["reason"])
                self._assert_refused_untouched(res)

    def test_ownerless_lease_never_claimed(self):
        # O03: a lease with session/run/task but no owner proof.
        h = self.h
        sid = self._sessionless_state(["G4"])[0]
        self._strip_lease_owner(sid)
        h.query_plan = [SessionState.ACTIVE]
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("ownership unverifiable", res["reason"])
        self._assert_refused_untouched(res)

    def test_matching_owner_active_session_still_recovers(self):
        # O04: positive control — a lease whose owner matches the
        # current account still recovers exactly as in T09.
        h = self.h
        sid = self._sessionless_state(["G4"])[0]
        lease = next(l for l in self.registry.list_leases()
                     if l["session_id"] == sid)
        self.assertEqual(lease.get("owner_id"), OWNER)
        h.query_plan = [SessionState.ACTIVE]
        if h.mp4.is_file():
            h.mp4.unlink()
        self._finish_mocks()
        res = h.resume()
        self.assertTrue(res["resumed"])
        m = h.manifest()
        self.assertEqual(m["task_state"], "TASK_COMPLETED")
        self.assertEqual(m["session_id"], sid)
        self.assertEqual(self._new_calls(), [])
        self.assertTrue(any(
            e.get("event") == "SESSION_RECOVERED_FROM_REGISTRY"
            for e in h.audit_events()))

    def test_foreign_owner_lease_never_claimed(self):
        # O05: a lease under a different account is foreign, never a
        # candidate; the refusal names the foreign account.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        sid = hc.allocate_managed_session("G4", RUN_ID)
        hwd.record_session(sid, "G4", RUN_ID, DEADLINE,
                           production_task_id=TASK, status="allocating")
        with mock.patch("h3_gpu_lease_registry.current_owner_id",
                        return_value=OTHER_OWNER):
            hc._register_gpu_lease(sid, "G4", RUN_ID, TASK, DEADLINE, 25.0)
        h.query_plan = [SessionState.ACTIVE]
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("different account", res["reason"])
        self._assert_refused_untouched(res)

    def test_matching_lease_owner_with_ownerless_managed_record_recovers(self):
        # O06: the managed file never carries owner_id — that absence
        # must not break recovery when the lease proves ownership.
        h = self.h
        sid = self._sessionless_state(["G4"])[0]
        managed = [s for s in h.managed_sessions()
                   if s["session_id"] == sid][0]
        self.assertNotIn("owner_id", managed)
        lease = next(l for l in self.registry.list_leases()
                     if l["session_id"] == sid)
        self.assertEqual(lease.get("owner_id"), OWNER)
        h.query_plan = [SessionState.ACTIVE]
        if h.mp4.is_file():
            h.mp4.unlink()
        self._finish_mocks()
        res = h.resume()
        self.assertTrue(res["resumed"])
        self.assertEqual(h.manifest()["session_id"], sid)
        self.assertEqual(self._new_calls(), [])

    def test_conflicting_owners_for_one_session_refuse_recovery(self):
        # O07: two trusted lease records disagree about one session's
        # owner. The session is disputed, not foreign and not ours.
        h = self.h
        sid = self._sessionless_state(["G4"])[0]
        self._duplicate_lease_with_owner(sid, OTHER_OWNER)
        h.query_plan = [SessionState.ACTIVE]
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("conflicting", res["reason"])
        self._assert_refused_untouched(res)

    def test_managed_only_record_never_claimed(self):
        # O08: only an ownerless managed record, no lease at all.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        sid = hc.allocate_managed_session("G4", RUN_ID)
        hwd.record_session(sid, "G4", RUN_ID, DEADLINE,
                           production_task_id=TASK, status="allocating")
        h.query_plan = [SessionState.ACTIVE]
        res = h.resume()
        self.assertFalse(res["resumed"])
        self.assertIn("ownership unverifiable", res["reason"])
        self._assert_refused_untouched(res)

    def test_mirror_only_evidence_without_matching_owner_refuses(self):
        # O09: the Drive mirror is the only evidence. A foreign owner
        # refuses as foreign; a missing owner refuses as unverifiable.
        h = self.h
        h.construct_manifest("TASK_AUTHORIZED")
        sid = hc.allocate_managed_session("G4", RUN_ID)
        foreign_doc = {"version": 1, "leases": [{
            "session_id": sid, "run_id": RUN_ID,
            "production_task_id": TASK, "gpu_model": "G4",
            "owner_id": OTHER_OWNER}]}
        self.registry.mirror = _FakeMirror(foreign_doc)
        h.query_plan = [SessionState.ACTIVE]
        with self.subTest(case="foreign"):
            res = h.resume()
            self.assertFalse(res["resumed"])
            self.assertIn("different account", res["reason"])
            self._assert_refused_untouched(res)
        ownerless_doc = {"version": 1, "leases": [{
            "session_id": sid, "run_id": RUN_ID,
            "production_task_id": TASK, "gpu_model": "G4"}]}
        self.registry.mirror = _FakeMirror(ownerless_doc)
        with self.subTest(case="ownerless"):
            res = h.resume()
            self.assertFalse(res["resumed"])
            self.assertIn("ownership unverifiable", res["reason"])
            self._assert_refused_untouched(res)

    def test_valid_candidate_with_foreign_evidence_claims_only_ours(self):
        # O10: a matching candidate plus a foreign lease for the same
        # run. The valid session may be adopted; the foreign session
        # must never be queried, stopped, or claimed.
        h = self.h
        ours = self._sessionless_state(["G4"])[0]
        foreign_sid = hc.allocate_managed_session("A100_HIGH_MEM", RUN_ID)
        hwd.record_session(foreign_sid, "A100_HIGH_MEM", RUN_ID, DEADLINE,
                           production_task_id=TASK, status="allocating")
        with mock.patch("h3_gpu_lease_registry.current_owner_id",
                        return_value=OTHER_OWNER):
            hc._register_gpu_lease(foreign_sid, "A100_HIGH_MEM", RUN_ID,
                                   TASK, DEADLINE, 25.0)
        queried = []

        def _recording_query(session_id, colab_auth=None):
            queried.append(session_id)
            return h._fake_query(session_id, colab_auth=colab_auth)

        h.query_plan = [SessionState.ACTIVE]
        if h.mp4.is_file():
            h.mp4.unlink()
        self._finish_mocks()
        with mock.patch.object(hwd, "query_session_state",
                               side_effect=_recording_query):
            res = h.resume()
        self.assertTrue(res["resumed"])
        self.assertEqual(h.manifest()["session_id"], ours)
        self.assertIn(ours, queried)
        self.assertNotIn(foreign_sid, queried)
        self.assertNotIn(foreign_sid, h.stop_cli_sessions)
        self.assertEqual(self._new_calls(), [])

    def test_deliver_after_owner_refusal_creates_no_gpu(self):
        # O11: after an owner refusal, deliver must not open a second
        # GPU and must not touch the task ledger.
        h = self.h
        self._sessionless_state(["G4"])
        _write_ledger(h.root, deadline=_future_iso())
        ledger_before = (h.root / "model_cache"
                         / f"h3_production_budget_{TASK}.json").read_bytes()
        h.query_plan = [SessionState.ACTIVE]
        with mock.patch("h3_gpu_lease_registry.current_owner_id",
                        side_effect=RuntimeError("whoami down")):
            result = hc.deliver_task(_deliver_args(h.root))
        self.assertFalse(result["delivered"])
        self.assertEqual(result["outcome"], "blocked_live_session")
        self.assertEqual(self._new_calls(), [])
        self.assertEqual(h.stop_cli_sessions, [])
        self.assertEqual((h.root / "model_cache"
                          / f"h3_production_budget_{TASK}.json").read_bytes(),
                         ledger_before)
        self.assertEqual(h.manifest()["task_state"], "TASK_AUTHORIZED")

    def test_stopped_candidates_with_matching_owner_still_recover(self):
        # O12: the STOPPED positive — matching-owner candidates plus a
        # Drive archive still recover with 0 GPU, as in T12.
        h = self.h
        sid = self._sessionless_state(["G4"])[0]
        lease = next(l for l in self.registry.list_leases()
                     if l["session_id"] == sid)
        self.assertEqual(lease.get("owner_id"), OWNER)
        h.query_plan = [SessionState.STOPPED]
        if h.mp4.is_file():
            h.mp4.unlink()
        with mock.patch.object(hc, "recover_from_drive_archive",
                               side_effect=_fake_drive_recover(h)):
            res = h.resume()
        self.assertTrue(res["resumed"])
        self.assertEqual(res["outcome"], "recovered_from_drive_archive_0gpu")
        self.assertEqual(h.manifest()["task_state"], "TASK_COMPLETED")
        self.assertEqual(self._new_calls(), [])


if __name__ == "__main__":
    unittest.main()

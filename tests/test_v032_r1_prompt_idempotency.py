"""TASK-RELEASE-V032 R1 (P0): ComfyUI /prompt submit idempotency, tri-state.

The old `_find_prompt_by_client_id` swallowed every lookup failure and
returned None, which `_submit_prompt` read as "absent" (never submitted)
and answered with an unchecked resubmit. If the first POST had already
landed but /queue (or /history) was transiently unreadable, the resubmit
queued the same workflow a second time: a duplicate inference.

R1 makes the lookup explicitly tri-state -- ("found", pid) /
("absent", None) / ("unknown", error_detail) -- and makes the submit
path obey it:

* "found"   -> recover, return the existing prompt_id, no resubmit.
* "unknown" -> bounded exponential-backoff reconciliation polling
  (ComfyUI liveness checked, attempt count AND total wall time capped);
  NEVER another POST /prompt while unknown; still unknown at the bound
  -> fail closed naming the client_id.
* "absent"  -> at most ONE checked resubmit (two POSTs total, ever).

Fault-injection tests only: get_json / post_json / time.sleep are all
mocked. No network, no GPU, no CU.
"""
import unittest
import urllib.error
from unittest import mock

import h3_remote_worker as w

CLIENT = "muse-h3-test-client"


def _timeout_exc():
    return urllib.error.URLError(TimeoutError("timed out"))


def _queued(pid, client_id=CLIENT):
    return {"queue_running": [[0, pid, {}, {"client_id": client_id}, []]],
            "queue_pending": []}


def _empty_queue():
    return {"queue_running": [], "queue_pending": []}


def _history_with(pid, client_id=CLIENT):
    return {pid: {"prompt": [1, pid, {}, {"client_id": client_id}, []],
                  "status": {"status_str": "success"}, "outputs": {}}}


class _Proc:
    """Fake ComfyUI subprocess handle: poll() None = alive, int = exited."""
    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc


class FindPromptTriStateTest(unittest.TestCase):
    def setUp(self):
        self.get_json = mock.patch.object(w, "get_json").start()
        self.addCleanup(mock.patch.stopall)

    def _find(self):
        return w._find_prompt_by_client_id("http://127.0.0.1:8188", CLIENT)

    def test_both_empty_is_absent(self):
        self.get_json.side_effect = [_empty_queue(), {}]
        self.assertEqual(self._find(), ("absent", None))

    def test_queue_timeout_is_unknown(self):
        # /queue unreadable: history cannot prove absence (the prompt
        # may be running/pending but not yet in /history).
        self.get_json.side_effect = [_timeout_exc()]
        state, detail = self._find()
        self.assertEqual(state, "unknown")
        self.assertIsInstance(detail, str)
        self.assertTrue(detail)
        self.assertEqual(self.get_json.call_count, 1)

    def test_history_timeout_is_unknown(self):
        # /queue clean but /history unreadable: not both-successful,
        # so this is NOT proof of absence.
        self.get_json.side_effect = [_empty_queue(), _timeout_exc()]
        state, detail = self._find()
        self.assertEqual(state, "unknown")
        self.assertIsInstance(detail, str)
        self.assertTrue(detail)

    def test_found_in_queue(self):
        self.get_json.side_effect = [_queued("pid-9"), {}]
        self.assertEqual(self._find(), ("found", "pid-9"))
        # Found in /queue: no /history read needed.
        self.assertEqual(self.get_json.call_count, 1)

    def test_found_in_history(self):
        self.get_json.side_effect = [_empty_queue(), _history_with("pid-7")]
        self.assertEqual(self._find(), ("found", "pid-7"))

    def test_other_client_id_only_is_absent(self):
        self.get_json.side_effect = [
            _queued("pid-other", client_id="someone-else"), {}]
        self.assertEqual(self._find(), ("absent", None))

    def test_malformed_queue_response_is_unknown(self):
        # A non-object /queue payload is not evidence of absence.
        self.get_json.side_effect = [None, {}]
        state, detail = self._find()
        self.assertEqual(state, "unknown")
        self.assertIsInstance(detail, str)

    def test_malformed_history_response_is_unknown(self):
        self.get_json.side_effect = [_empty_queue(), ["not", "a", "dict"]]
        state, detail = self._find()
        self.assertEqual(state, "unknown")
        self.assertIsInstance(detail, str)

    def test_history_http_502_is_unknown(self):
        self.get_json.side_effect = [
            _empty_queue(),
            urllib.error.HTTPError("http://x/history", 502,
                                   "Bad Gateway", {}, None)]
        state, _detail = self._find()
        self.assertEqual(state, "unknown")


class SubmitPromptReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.post_json = mock.patch.object(w, "post_json").start()
        self.get_json = mock.patch.object(w, "get_json").start()
        self.sleep = mock.patch.object(w.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def _submit(self, proc=None):
        return w._submit_prompt("http://127.0.0.1:8188", {"1": {}},
                                CLIENT, proc=proc)

    def test_first_post_timeout_queue_recovers_after_transient_timeouts(self):
        """Scenario A: first POST enqueued but its response timed out;
        /queue then times out twice and recovers. Exactly ONE real
        submit; the prompt is recovered, never resubmitted."""
        self.post_json.side_effect = _timeout_exc()
        self.get_json.side_effect = [
            _timeout_exc(),          # initial probe: /queue timeout -> unknown
            _timeout_exc(),          # reconcile probe 1: still unknown
            _queued("pid-9"),        # reconcile probe 2: found in /queue
        ]
        result = self._submit(proc=_Proc())
        self.assertEqual(result, {"prompt_id": "pid-9"})
        self.assertEqual(self.post_json.call_count, 1)
        self.assertEqual(self.get_json.call_count, 3)
        # One backoff sleep between the two reconciliation probes.
        self.assertEqual(self.sleep.call_count, 1)

    def test_first_post_timeout_persistent_queue_timeouts_fail_closed(self):
        """Scenario B: first POST enqueued but its response timed out;
        /queue NEVER recovers. No resubmit ever, bounded polling, then
        fail closed naming the client_id -- no infinite loop."""
        self.post_json.side_effect = _timeout_exc()
        self.get_json.side_effect = _timeout_exc()
        with self.assertRaises(RuntimeError) as ctx:
            self._submit(proc=_Proc())
        self.assertIn(CLIENT, str(ctx.exception))
        self.assertEqual(self.post_json.call_count, 1)
        # 1 initial probe + exactly PROMPT_RECONCILE_MAX_ATTEMPTS probes.
        self.assertEqual(
            self.get_json.call_count, 1 + w.PROMPT_RECONCILE_MAX_ATTEMPTS)
        # Exponential backoff sleeps, one fewer than the probe count.
        self.assertEqual(
            self.sleep.call_count, w.PROMPT_RECONCILE_MAX_ATTEMPTS - 1)
        self.assertEqual(
            [c.args[0] for c in self.sleep.call_args_list],
            [1.0, 2.0, 4.0, 8.0][:w.PROMPT_RECONCILE_MAX_ATTEMPTS - 1])

    def test_persistent_history_timeouts_fail_closed_without_resubmit(self):
        """Same as scenario B but the failure is /history: /queue is
        clean and empty, /history never answers. Still unknown, still
        exactly one POST, still bounded."""
        self.post_json.side_effect = _timeout_exc()

        def _fake_get(url, timeout=10):
            if url.endswith("/queue"):
                return _empty_queue()
            raise _timeout_exc()

        self.get_json.side_effect = _fake_get
        with self.assertRaises(RuntimeError) as ctx:
            self._submit(proc=_Proc())
        self.assertIn(CLIENT, str(ctx.exception))
        self.assertEqual(self.post_json.call_count, 1)
        # Every probe reads both /queue and /history.
        self.assertEqual(
            self.get_json.call_count, 2 * (1 + w.PROMPT_RECONCILE_MAX_ATTEMPTS))
        self.assertEqual(
            self.sleep.call_count, w.PROMPT_RECONCILE_MAX_ATTEMPTS - 1)

    def test_unknown_then_absent_permits_exactly_one_checked_resubmit(self):
        """Unknown is not a licence to resubmit blindly -- but once a
        later probe proves BOTH endpoints clean and empty, the single
        checked resubmit is allowed and succeeds."""
        self.post_json.side_effect = [
            _timeout_exc(),
            {"prompt_id": "pid-2"},
        ]
        self.get_json.side_effect = [
            _timeout_exc(),          # initial probe: /queue timeout -> unknown
            _empty_queue(), {},      # reconcile probe 1: both clean -> absent
        ]
        result = self._submit(proc=_Proc())
        self.assertEqual(result, {"prompt_id": "pid-2"})
        self.assertEqual(self.post_json.call_count, 2)
        self.assertEqual(self.get_json.call_count, 3)
        self.assertEqual(self.sleep.call_count, 0)

    def test_second_attempt_timeout_unknown_fails_closed_with_two_posts_max(self):
        """First timeout proves absent -> one resubmit. The resubmit
        also times out and its state stays unknown -> fail closed.
        Two POSTs total, never a third."""
        self.post_json.side_effect = _timeout_exc()
        self.get_json.side_effect = [
            _empty_queue(), {},      # probe after attempt 1: absent
            _timeout_exc(),          # probe after attempt 2: unknown ...
            _timeout_exc(),          # ... reconciliation never recovers
            _timeout_exc(),
            _timeout_exc(),
            _timeout_exc(),
            _timeout_exc(),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            self._submit(proc=_Proc())
        self.assertIn(CLIENT, str(ctx.exception))
        self.assertEqual(self.post_json.call_count, 2)
        self.assertEqual(
            self.get_json.call_count, 2 + 1 + w.PROMPT_RECONCILE_MAX_ATTEMPTS)

    def test_dead_comfyui_during_reconciliation_fails_fast(self):
        """ComfyUI already exited while the submit state is unknown:
        fail fast with the exit, do not burn the reconciliation window
        polling a dead server, and never resubmit."""
        self.post_json.side_effect = _timeout_exc()
        self.get_json.side_effect = _timeout_exc()
        with self.assertRaises(RuntimeError) as ctx:
            self._submit(proc=_Proc(rc=1))
        self.assertIn("ComfyUI process exited", str(ctx.exception))
        self.assertIn(CLIENT, str(ctx.exception))
        self.assertEqual(self.post_json.call_count, 1)
        self.assertEqual(self.get_json.call_count, 0)
        self.assertEqual(self.sleep.call_count, 0)

    def test_absent_on_second_timeout_fails_closed_no_third_submit(self):
        """Both attempts time out and both probes prove absent: the
        run still fails closed instead of attempting a third submit."""
        self.post_json.side_effect = _timeout_exc()

        def _fake_get(url, timeout=10):
            if url.endswith("/queue"):
                return _empty_queue()
            return {}

        self.get_json.side_effect = _fake_get
        with self.assertRaises(RuntimeError) as ctx:
            self._submit(proc=_Proc())
        self.assertIn("refusing a third", str(ctx.exception))
        self.assertIn(CLIENT, str(ctx.exception))
        self.assertEqual(self.post_json.call_count, 2)

    def test_found_on_initial_probe_recovers_without_reconciliation(self):
        self.post_json.side_effect = _timeout_exc()
        self.get_json.side_effect = [_queued("pid-9"), {}]
        result = self._submit(proc=_Proc())
        self.assertEqual(result, {"prompt_id": "pid-9"})
        self.assertEqual(self.post_json.call_count, 1)
        self.assertEqual(self.get_json.call_count, 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_reconcile_bounds_are_explicit_and_finite(self):
        """The reconciliation window is a named, finite budget -- not
        an open-ended loop: bounded attempts, bounded delay, bounded
        total wall time."""
        self.assertEqual(w.PROMPT_RECONCILE_MAX_ATTEMPTS, 5)
        self.assertEqual(w.PROMPT_RECONCILE_BASE_DELAY_SEC, 1.0)
        self.assertEqual(w.PROMPT_RECONCILE_MAX_DELAY_SEC, 8.0)
        self.assertEqual(w.PROMPT_RECONCILE_MAX_TOTAL_SEC, 60.0)


if __name__ == "__main__":
    unittest.main()

"""v0.3.2 R6 unit & fault-injection tests: G4-first launch rule.

- run_cli injects COLAB_CLI_ASSIGN_RETRIES=0 by default (the vendored
  CLI's internal assign retry defaults to 3; 0 makes a 503 return to
  the controller immediately) and passes through an operator-set value.
- A 503 (CAPACITY_TRANSIENT) on the first G4 attempt fast-forwards
  within the same round straight to A100_HIGH_MEM instead of burning
  a second, certainly-failing G4 attempt; multi-round elastic backoff
  (M1) still works across rounds.
- A TRANSPORT_TRANSIENT on the first G4 attempt still retries the
  SAME G4 once (attempt 1) and never fast-forwards to A100.

The three-step GPU_ATTEMPT_PLAN contract (G4, G4, A100_HIGH_MEM) is
unchanged: the transport retry still depends on attempt 1 being G4.

Strictly 0.00 CU: all Colab CLI, Drive, and sleep calls are
mocked/injected (ChainHarness + fake subprocess).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_controller as hc  # noqa: E402
from tests.test_fault_injection import ChainHarness, _args as _run_args  # noqa: E402

FAST_FORWARD_EVENT = "GPU_ALLOCATION_CAPACITY_FAST_FORWARD"
A100_PROBE = "NVIDIA A100 80GB PCIe, 81920"


def _new_gpus(harness):
    """GPU requested by each `colab new` call, in order."""
    gpus = []
    for call in harness.run_cli_calls:
        if call and call[0] == "new":
            gpus.append(call[call.index("--gpu") + 1])
    return gpus


class RunCliAssignRetriesTest(unittest.TestCase):
    def _capture_env(self):
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_run(*a, **k):
            captured["env"] = k.get("env")
            return FakeProc()

        with mock.patch.object(hc.subprocess, "run", fake_run), \
                mock.patch.object(hc, "resolve_colab_executable",
                                  return_value="/usr/bin/colab"):
            hc.run_cli(["usage"], timeout=10, check=False)
        return captured["env"]

    def test_default_assign_retries_is_zero(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLAB_CLI_ASSIGN_RETRIES", None)
            env = self._capture_env()
        self.assertEqual(env["COLAB_CLI_ASSIGN_RETRIES"], "0")
        # Pre-existing timeouts are untouched.
        self.assertEqual(env["REQUEST_TIMEOUT"], "120")
        self.assertEqual(env["COLAB_CLI_REQUEST_TIMEOUT"], "180")

    def test_operator_value_is_passed_through(self):
        with mock.patch.dict(
                os.environ, {"COLAB_CLI_ASSIGN_RETRIES": "2"}):
            env = self._capture_env()
        self.assertEqual(env["COLAB_CLI_ASSIGN_RETRIES"], "2")
        self.assertEqual(env["REQUEST_TIMEOUT"], "120")
        self.assertEqual(env["COLAB_CLI_REQUEST_TIMEOUT"], "180")


class CapacityFastForwardTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_g4_first_try_503_fast_forwards_to_a100_same_round(self):
        h = self.h
        # Successful allocation lands on A100 after the fast-forward,
        # so the fake GPU probe must report A100 hardware.
        h.probe_output = A100_PROBE
        # Round 1: G4 (503) -> fast-forward -> A100 (503) -> backoff
        # 15.0s. Round 2: G4 (503) -> fast-forward -> A100 succeeds.
        h.new_failures = [
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
            RuntimeError("HTTP 503 Service Unavailable"),
        ]
        sleeps = []
        args = _run_args(
            allocation_rounds=3,
            sleep_fn=lambda s: sleeps.append(s),
            jitter_fn=lambda: 1.0,
        )
        res = hc.run_variant(args)
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        # Exactly one G4 `new` per round: the second G4 attempt is
        # never spent on a known-empty G4 pool.
        self.assertEqual(_new_gpus(h), ["G4", "A100", "G4", "A100"])
        self.assertEqual(len(h.allocate_ids), 4)
        # M1 multi-round backoff still works: one 15.0s backoff.
        self.assertIn(15.0, sleeps)
        self.assertIn("GPU_ALLOCATION_BACKOFF", h.audit_jsonl_text())
        # The fast-forward itself is audited.
        self.assertIn(FAST_FORWARD_EVENT, h.audit_jsonl_text())

    def test_g4_first_try_503_single_round_uses_one_g4_only(self):
        h = self.h
        h.probe_output = A100_PROBE
        h.new_failures = [RuntimeError("HTTP 503 Service Unavailable")]
        res = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(_new_gpus(h), ["G4", "A100"])
        self.assertEqual(len(h.allocate_ids), 2)
        self.assertEqual(h.manifest().get("session_id"), h.allocate_ids[1])
        self.assertIn(FAST_FORWARD_EVENT, h.audit_jsonl_text())


class TransportRetryStillSameG4Test(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_g4_first_try_transport_transient_retries_g4_not_a100(self):
        h = self.h
        # Proxy/TLS blip on the first G4 attempt: attempt 1 must retry
        # the SAME G4 (the three-step plan keeps attempt 1 as G4 for
        # exactly this case) and must never cut to A100.
        h.new_failures = [RuntimeError("connection reset by peer")]
        res = h.run()
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(_new_gpus(h), ["G4", "G4"])
        self.assertEqual(len(h.allocate_ids), 2)
        self.assertEqual(h.manifest().get("session_id"), h.allocate_ids[1])
        self.assertNotIn(FAST_FORWARD_EVENT, h.audit_jsonl_text())
        self.assertIn("allocation_transport_retry", h.audit_jsonl_text())


if __name__ == "__main__":
    unittest.main()

"""v0.2.0 F5 tests: controller cold-start telemetry wiring.

Offline only (0 CU): the worker's COLDSTART_METRICS_JSON line is
parsed from fake exec output, and the report is persisted into real
run manifests on temp roots. No GPU, no Drive, no network.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import h3_controller as hc
import h3_run_manifest as rm

REPORT = {"setup_seconds": 42.5, "comfy_install_seconds": 12.5,
          "restore_seconds": 30.0, "wheelhouse_hit": True,
          "models_reused": 3, "models_downloaded": 1, "vram_mib": 81920}


class ParseColdstartMetricsTest(unittest.TestCase):
    def test_valid_line_parses(self):
        text = "setup...\nCOLDSTART_METRICS_JSON " + json.dumps(REPORT) + "\n"
        self.assertEqual(hc.parse_coldstart_metrics(text), REPORT)

    def test_last_line_wins(self):
        first = dict(REPORT, setup_seconds=1.0)
        text = ("COLDSTART_METRICS_JSON " + json.dumps(first) + "\n"
                "COLDSTART_METRICS_JSON " + json.dumps(REPORT) + "\n")
        self.assertEqual(hc.parse_coldstart_metrics(text), REPORT)

    def test_missing_line_is_none(self):
        self.assertIsNone(hc.parse_coldstart_metrics("no metrics here\n"))

    def test_corrupt_line_is_skipped(self):
        text = ("COLDSTART_METRICS_JSON {not json}\n"
                "COLDSTART_METRICS_JSON " + json.dumps(REPORT) + "\n")
        self.assertEqual(hc.parse_coldstart_metrics(text), REPORT)

    def test_invalid_shape_is_none(self):
        bad = dict(REPORT, setup_seconds=-1)
        text = "COLDSTART_METRICS_JSON " + json.dumps(bad) + "\n"
        self.assertIsNone(hc.parse_coldstart_metrics(text))


class ValidateColdstartMetricsTest(unittest.TestCase):
    def test_unknown_fields_are_dropped(self):
        report = dict(REPORT, future_field="x", token="never")
        validated = rm.validate_coldstart_metrics(report)
        self.assertNotIn("future_field", validated)
        self.assertNotIn("token", validated)
        self.assertEqual(validated["setup_seconds"], 42.5)

    def test_missing_setup_seconds_is_rejected(self):
        with self.assertRaises(ValueError):
            rm.validate_coldstart_metrics({"wheelhouse_hit": True})

    def test_non_bool_hit_is_rejected(self):
        with self.assertRaises(ValueError):
            rm.validate_coldstart_metrics(dict(REPORT, wheelhouse_hit="yes"))

    def test_none_counts_default_to_zero(self):
        report = {"setup_seconds": 5.0, "wheelhouse_hit": None}
        validated = rm.validate_coldstart_metrics(report)
        self.assertEqual(validated["models_reused"], 0)
        self.assertIsNone(validated["comfy_install_seconds"])
        self.assertIsNone(validated["vram_mib"])


class ManifestPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        rm.create_run_manifest(self.root, "h3-f5-task", "h3-f5-run-1")

    def test_update_run_persists_report(self):
        manifest, changed, _ = rm.update_run(
            self.root, "h3-f5-run-1", coldstart_metrics=dict(REPORT))
        self.assertTrue(changed)
        self.assertEqual(manifest["coldstart_metrics"], REPORT)
        loaded = rm.load_run_manifest(self.root, "h3-f5-run-1")
        self.assertEqual(loaded["coldstart_metrics"], REPORT)

    def test_invalid_report_raises_and_persists_nothing(self):
        with self.assertRaises(ValueError):
            rm.update_run(self.root, "h3-f5-run-1",
                          coldstart_metrics=dict(REPORT, setup_seconds=-2))
        loaded = rm.load_run_manifest(self.root, "h3-f5-run-1")
        self.assertNotIn("coldstart_metrics", loaded)

    def test_later_report_replaces_earlier(self):
        rm.update_run(self.root, "h3-f5-run-1",
                      coldstart_metrics=dict(REPORT, setup_seconds=10.0))
        manifest, _, _ = rm.update_run(
            self.root, "h3-f5-run-1",
            coldstart_metrics=dict(REPORT, setup_seconds=20.0))
        self.assertEqual(manifest["coldstart_metrics"]["setup_seconds"], 20.0)


class ControllerPollWiringTest(unittest.TestCase):
    """_manifest_worker_state persists the polled job state's report."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        rm.create_run_manifest(self.root, "h3-f5-task", "h3-f5-run-1")

    def _poll(self, state):
        with mock.patch.object(hc, "ROOT", self.root), \
                mock.patch.object(hc, "record", return_value=None):
            hc._manifest_worker_state("h3-f5-run-1", state)

    def test_poll_persists_coldstart_metrics(self):
        self._poll({"stage": "restore_weights",
                    "coldstart_metrics": dict(REPORT)})
        loaded = rm.load_run_manifest(self.root, "h3-f5-run-1")
        self.assertEqual(loaded["coldstart_metrics"], REPORT)

    def test_poll_without_metrics_leaves_manifest_alone(self):
        self._poll({"stage": "restore_weights"})
        loaded = rm.load_run_manifest(self.root, "h3-f5-run-1")
        self.assertNotIn("coldstart_metrics", loaded)

    def test_poll_with_corrupt_metrics_never_breaks_the_run(self):
        self._poll({"stage": "restore_weights",
                    "coldstart_metrics": {"setup_seconds": "not-a-number"}})
        loaded = rm.load_run_manifest(self.root, "h3-f5-run-1")
        self.assertNotIn("coldstart_metrics", loaded)


class ModelsReadyBackfillTest(unittest.TestCase):
    """v0.2.1 P3: MODELS_READY is backfilled monotonically when a poll
    observes a later stage or coldstart_metrics first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        rm.create_run_manifest(self.root, "h3-p3-task", "h3-p3-run-1")

    def _backfill(self):
        with mock.patch.object(hc, "ROOT", self.root), \
                mock.patch.object(hc, "record", return_value=None):
            return hc._backfill_models_ready_if_needed("h3-p3-run-1")

    def _poll(self, state):
        with mock.patch.object(hc, "ROOT", self.root), \
                mock.patch.object(hc, "record", return_value=None):
            hc._manifest_worker_state("h3-p3-run-1", state)

    def _stage(self):
        return rm.load_run_manifest(self.root, "h3-p3-run-1")["task_state"]

    def test_backfill_from_early_stage(self):
        self.assertTrue(self._backfill())
        self.assertEqual(self._stage(), "MODELS_READY")

    def test_backfill_skips_when_already_past(self):
        rm.update_run(self.root, "h3-p3-run-1",
                      task_state="INFERENCE_COMPLETED")
        self.assertFalse(self._backfill())
        self.assertEqual(self._stage(), "INFERENCE_COMPLETED")

    def test_backfill_missing_manifest_is_quiet(self):
        with mock.patch.object(hc, "ROOT", self.root), \
                mock.patch.object(hc, "record", return_value=None):
            self.assertFalse(hc._backfill_models_ready_if_needed("no-such-run"))

    def test_poll_with_later_stage_backfills(self):
        self._poll({"stage": "INFERENCE_RUNNING"})
        self.assertEqual(self._stage(), "MODELS_READY")

    def test_poll_with_coldstart_backfills_and_records_audit_event(self):
        self._poll({"stage": "restore_weights",
                    "coldstart_metrics": dict(REPORT)})
        self.assertEqual(self._stage(), "MODELS_READY")
        events = [e.get("event")
                  for e in rm.read_audit_log(self.root, "h3-p3-run-1")]
        self.assertIn("COLDSTART_METRICS", events)

    def test_poll_with_models_ready_stage_is_direct(self):
        self._poll({"stage": "MODELS_READY"})
        self.assertEqual(self._stage(), "MODELS_READY")


if __name__ == "__main__":
    unittest.main()

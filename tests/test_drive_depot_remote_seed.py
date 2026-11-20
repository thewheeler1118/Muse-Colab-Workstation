"""v0.2.0 F5 tests: in-cloud remote seeding (drive_depot.remote_seed).

Offline only (0 CU): a fake session runner stands in for the real
Colab session, a fake token function stands in for ADC minting, and
the Drive service is a mock. No GPU, no Drive, no network, no live
remote seed is ever started.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drive_depot as depot
from h3_contract import MODEL_FILES
from h3_drive_token import token_id_for

REVISION = "b" * 40
PIN = {
    rel: {"size": 100 + i, "sha256": ("%02x" % i) * 32}
    for i, rel in enumerate(sorted(MODEL_FILES))
}
# Fake token, built from fragments so no credential-shaped literal
# exists in the file (same precedent as the F3 fake tokens).
_FAKE_TOKEN_VALUE = "ya29." + "FAKE-TOKEN-VALUE-12345"
TOKEN = {"access_token": _FAKE_TOKEN_VALUE,
         "token_id": token_id_for(_FAKE_TOKEN_VALUE),
         "expires_at_utc": "2026-11-20T04:00:00Z"}


def _report(pin=PIN, revision=REVISION):
    return {
        "seeded": True,
        "files": [
            {"path": rel, "drive_file_id": "drive-" + str(i),
             "parent_id": "folder-" + rel.split("/", 1)[0],
             "size": pin[rel]["size"], "sha256": pin[rel]["sha256"],
             "server_md5": "c" * 32, "server_verified": False,
             "hf_revision": revision}
            for i, rel in enumerate(sorted(MODEL_FILES))
        ],
    }


class FakeRunner:
    """Stands in for the real Colab session runner."""

    def __init__(self, report=None, exec_error=None,
                 stop_confirmed=True):
        self.calls = []
        self.report = report if report is not None else _report()
        self.exec_error = exec_error
        self.stop_confirmed = stop_confirmed
        self.uploaded = {}

    def create(self, session):
        self.calls.append(("create", session))

    def upload(self, session, local_path, remote_name):
        self.calls.append(("upload", session, remote_name))
        self.uploaded[remote_name] = Path(local_path).read_text(encoding="utf-8")

    def exec_script(self, session, remote_path):
        self.calls.append(("exec", session, remote_path))
        if self.exec_error is not None:
            raise self.exec_error
        return {"returncode": 0,
                "stdout": "SEED_REPORT_JSON " + json.dumps(self.report),
                "stderr": ""}

    def stop_and_verify(self, session):
        self.calls.append(("stop", session))
        return {"session": session,
                "stopped_confirmed": self.stop_confirmed,
                "state": "STOPPED" if self.stop_confirmed else "UNKNOWN"}


class RemoteSeedGateTest(unittest.TestCase):
    def _run_cli(self, argv):
        with mock.patch.object(sys, "argv", ["drive_depot.py"] + argv), \
                mock.patch.object(depot, "drive_service",
                                  return_value=mock.Mock()), \
                mock.patch.object(depot, "account_matches",
                                  return_value=True), \
                mock.patch.object(depot, "read_json",
                                  return_value={"drive_root_folder_id": "root1"}), \
                mock.patch.object(depot, "remote_seed",
                                  return_value={"remote_seeded": True}) as seed_fn:
            return depot.cli(), seed_fn

    def test_missing_confirm_flag_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self._run_cli(["remote-seed", "--allow-drive-write",
                           "--allow-large-transfer",
                           "--hf-revision", REVISION])
        self.assertEqual(cm.exception.code, 2)

    def test_missing_drive_write_flag_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self._run_cli(["remote-seed", "--allow-large-transfer",
                           "--confirm-remote-seed",
                           "--hf-revision", REVISION])
        self.assertEqual(cm.exception.code, 2)

    def test_missing_large_transfer_flag_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self._run_cli(["remote-seed", "--allow-drive-write",
                           "--confirm-remote-seed",
                           "--hf-revision", REVISION])
        self.assertEqual(cm.exception.code, 2)

    def test_all_three_flags_reach_remote_seed(self):
        code, seed_fn = self._run_cli(
            ["remote-seed", "--allow-drive-write", "--allow-large-transfer",
             "--confirm-remote-seed", "--hf-revision", REVISION])
        self.assertEqual(code, 0)
        seed_fn.assert_called_once_with(mock.ANY, "root1", REVISION)


class RemoteSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def _patches(self):
        pins = {"files": PIN}
        return (
            mock.patch.object(depot, "load_pinned_metadata",
                              return_value=pins),
            mock.patch.object(depot, "ensure_folder",
                              side_effect=lambda s, r, name, allow_write=True:
                              "folder-" + name),
            mock.patch.object(depot, "verify_drive_entry",
                              return_value={}),
            mock.patch.object(depot, "publish_manifest_to_drive",
                              return_value={"drive_file_id": "ready1",
                                            "name": "READY_" + REVISION + ".json",
                                            "md5": "d" * 32, "size": 100}),
            mock.patch.object(depot, "read_json", return_value={}),
            mock.patch.object(depot, "atomic_json"),
        )

    def _run(self, runner, service=None):
        patches = self._patches()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return depot.remote_seed(
            service or mock.Mock(), "root1", REVISION, runner=runner,
            token_fn=lambda: dict(TOKEN), work_dir=self.work)

    def test_happy_path_seeds_and_stops(self):
        runner = FakeRunner()
        result = self._run(runner)
        self.assertTrue(result["remote_seeded"])
        self.assertEqual(result["files"], 4)
        self.assertTrue(result["stopped_confirmed"])
        self.assertEqual(result["session"][:len("muse-h3-seed-")],
                         "muse-h3-seed-")
        kinds = [c[0] for c in runner.calls]
        self.assertEqual(kinds, ["create", "upload", "upload", "exec", "stop"])
        # The token file went up, but the script never contains the token.
        self.assertIn("seed_token.json", runner.uploaded)
        self.assertIn(TOKEN["access_token"], runner.uploaded["seed_token.json"])
        self.assertNotIn(TOKEN["access_token"], runner.uploaded["remote_seed.py"])
        # The generated script carries the pinned files + folder ids.
        script = runner.uploaded["remote_seed.py"]
        for rel in sorted(MODEL_FILES):
            self.assertIn(rel, script)
        self.assertIn("folder-diffusion_models", script)

    def test_exec_failure_still_stops_and_raises(self):
        runner = FakeRunner(exec_error=RuntimeError("remote cell failed"))
        with self.assertRaises(RuntimeError) as cm:
            self._run(runner)
        self.assertIn("remote cell failed", str(cm.exception))
        self.assertEqual([c[0] for c in runner.calls][-1], "stop")

    def test_unconfirmed_stop_is_an_orphan_risk(self):
        runner = FakeRunner(stop_confirmed=False)
        with self.assertRaises(RuntimeError) as cm:
            self._run(runner)
        self.assertIn("REMOTE_SEED_ORPHAN_RISK", str(cm.exception))

    def test_report_mismatch_is_rejected_before_commit(self):
        bad = _report()
        bad["files"][0]["sha256"] = "f" * 64
        runner = FakeRunner(report=bad)
        with mock.patch.object(depot, "publish_manifest_to_drive") as pub:
            with self.assertRaises(RuntimeError):
                self._run(runner)
            pub.assert_not_called()
        self.assertEqual([c[0] for c in runner.calls][-1], "stop")

    def test_bad_revision_is_refused(self):
        with self.assertRaises(ValueError):
            depot.remote_seed(mock.Mock(), "root1", "not-a-revision",
                              runner=FakeRunner(), token_fn=lambda: dict(TOKEN),
                              work_dir=self.work)


class ParseSeedReportTest(unittest.TestCase):
    def test_last_line_wins(self):
        text = ("SEED_REPORT_JSON {\"seeded\": false}\n"
                "SEED_REPORT_JSON {\"seeded\": true, \"files\": []}\n")
        self.assertTrue(depot.parse_seed_report(text)["seeded"])

    def test_missing_line_raises(self):
        with self.assertRaises(RuntimeError):
            depot.parse_seed_report("no report here\n")

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            depot.parse_seed_report("SEED_REPORT_JSON {not json}\n")


if __name__ == "__main__":
    unittest.main()

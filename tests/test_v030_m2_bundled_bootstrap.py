"""v0.3.0 M2 unit & fault-injection tests: two-stage bundled bootstrap.

Muse-original tests for the Muse-original M2 reimplementation (Issue #15).
Strictly 0.00 CU: all Colab CLI, Drive, and sleep calls are mocked/injected.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_bootstrap_stage1 as stage1  # noqa: E402
import h3_bootstrap_stage2 as stage2  # noqa: E402
import h3_controller as hc  # noqa: E402
import h3_run_manifest as rm  # noqa: E402
from tests.test_fault_injection import (  # noqa: E402
    ChainHarness, RUN_ID, TASK, _args as _run_args, _FIXTURE_DIR)


def _make_tar(path: Path, members: dict[str, bytes], *, symlinks=None, devs=None) -> None:
    """Build a tar.gz at ``path`` from {name: bytes}; ``symlinks`` maps
    name -> target, ``devs`` is a list of device-node member names."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
        for name in (devs or []):
            info = tarfile.TarInfo(name)
            info.type = tarfile.CHRTYPE
            info.devmajor = 1
            info.devminor = 3
            tf.addfile(info)


class BuildBootstrapTarTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.a = self.root / "a.txt"
        self.a.write_bytes(b"hello")
        self.tok = self.root / "drive_token.json"
        self.tok.write_bytes(b'{"access_token": "x"}')

    def tearDown(self):
        self.td.cleanup()

    def test_deterministic_rebuild_is_byte_identical(self):
        out1 = self.root / "one.tar.gz"
        out2 = self.root / "two.tar.gz"
        entries = [(self.a, "a.txt", None), (self.tok, "drive_token.json", 0o600)]
        hc.build_bootstrap_tar(out1, entries)
        hc.build_bootstrap_tar(out2, entries)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_modes_preserved(self):
        out = self.root / "b.tar.gz"
        hc.build_bootstrap_tar(out, [(self.a, "a.txt", None),
                                     (self.tok, "drive_token.json", 0o600)])
        with tarfile.open(out, "r:gz") as tf:
            modes = {m.name: m.mode for m in tf.getmembers()}
        self.assertEqual(modes["a.txt"], 0o644)
        self.assertEqual(modes["drive_token.json"], 0o600)

    def test_forbidden_worker_refused_in_stage1(self):
        out = self.root / "c.tar.gz"
        with self.assertRaisesRegex(ValueError, "Forbidden bootstrap entry"):
            hc.build_bootstrap_tar(out, [(self.a, "h3_remote_worker.py", None)],
                                   forbidden=frozenset({"h3_remote_worker.py"}))

    def test_absolute_and_traversal_arcs_refused(self):
        out = self.root / "d.tar.gz"
        for bad in ("/etc/passwd", "../escape.txt", "sub/../../escape.txt"):
            with self.assertRaisesRegex(ValueError, "Unsafe bootstrap entry"):
                hc.build_bootstrap_tar(out, [(self.a, bad, None)])

    def test_missing_local_file_raises(self):
        out = self.root / "e.tar.gz"
        with self.assertRaises(FileNotFoundError):
            hc.build_bootstrap_tar(out, [(self.root / "nope.txt", "nope.txt", None)])


class ResolveBootstrapModeTest(unittest.TestCase):
    def test_default_is_bundled(self):
        self.assertEqual(hc.resolve_bootstrap_mode(SimpleNamespace()), "bundled")
        self.assertEqual(hc.resolve_bootstrap_mode(SimpleNamespace(bootstrap_mode=None)), "bundled")

    def test_explicit_modes(self):
        self.assertEqual(
            hc.resolve_bootstrap_mode(SimpleNamespace(bootstrap_mode="legacy")), "legacy")
        self.assertEqual(
            hc.resolve_bootstrap_mode(SimpleNamespace(bootstrap_mode="Bundled")), "bundled")

    def test_invalid_rejected(self):
        with self.assertRaises(ValueError):
            hc.resolve_bootstrap_mode(SimpleNamespace(bootstrap_mode="zip"))


class StageExtractTest(unittest.TestCase):
    """Both stage modules share the same safe-extract discipline."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.dest = self.root / "dest"

    def tearDown(self):
        self.td.cleanup()

    def test_happy_path_and_token_mode(self):
        for mod in (stage1, stage2):
            tar = self.root / f"{mod.__name__}.tar.gz"
            _make_tar(tar, {"h3_drive_download.py": b"print(1)",
                            "drive_token.json": b'{"access_token": "x"}'})
            extracted = mod.safe_extract_tar(tar, self.dest)
            self.assertEqual(extracted, ["h3_drive_download.py", "drive_token.json"])
            self.assertEqual((self.dest / "h3_drive_download.py").read_bytes(), b"print(1)")
            mode = stat.S_IMODE((self.dest / "drive_token.json").stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_unsafe_members_rejected_and_nothing_extracted(self):
        cases = [
            ({"../escape.txt": b"x"}, "traversal"),
            ({"/abs.txt": b"x"}, "absolute"),
            ({"ok.txt": b"ok", "../escape.txt": b"x"}, "traversal after a good member"),
        ]
        for members, _label in cases:
            for mod in (stage1, stage2):
                dest = self.root / f"dest-{mod.__name__}-{len(os.listdir(self.root))}"
                tar = self.root / f"bad-{mod.__name__}-{len(os.listdir(self.root))}.tar.gz"
                _make_tar(tar, members)
                with self.assertRaises(ValueError):
                    mod.safe_extract_tar(tar, dest)
                self.assertFalse((dest / "ok.txt").exists(),
                                 "validation runs before any extraction")

    def test_symlink_hardlink_and_device_rejected(self):
        for mod in (stage1, stage2):
            tar = self.root / f"link-{mod.__name__}.tar.gz"
            _make_tar(tar, {"real.txt": b"r"},
                      symlinks={"evil-link": "/etc/passwd"})
            with self.assertRaisesRegex(ValueError, "symlink/hardlink"):
                mod.safe_extract_tar(tar, self.dest)
            tar2 = self.root / f"dev-{mod.__name__}.tar.gz"
            _make_tar(tar2, {"real.txt": b"r"}, devs=["dev-node"])
            with self.assertRaisesRegex(ValueError, "unsupported type"):
                mod.safe_extract_tar(tar2, self.dest)

    def test_oversize_bundle_fails_closed(self):
        for mod in (stage1, stage2):
            tar = self.root / f"big-{mod.__name__}.tar.gz"
            _make_tar(tar, {"big.bin": b"x" * (mod._MAX_EXTRACT_BYTES + 1)})
            with self.assertRaisesRegex(ValueError, "exceed"):
                mod.safe_extract_tar(tar, self.dest)

    def test_stage1_main_unlinks_tar_and_runs_probe(self):
        tar = self.root / "s1.tar.gz"
        _make_tar(tar, {"h3_drive_download.py": b"# probe dep",
                        "drive_token.json": b'{"access_token": "x"}'})
        probe_calls = []

        class _FakeProbe:
            ROOT = None

            @staticmethod
            def main():
                probe_calls.append("probe")
                print("MUSE_DRIVE_DOWNLOAD_PROBE_OK fake")

        sys.modules["h3_drive_download_probe"] = _FakeProbe  # type: ignore[assignment]
        try:
            stage1.main(root=self.dest, tar_path=tar)
        finally:
            sys.modules.pop("h3_drive_download_probe", None)
        self.assertFalse(tar.exists(), "stage-1 tar is unlinked even on success")
        self.assertEqual(probe_calls, ["probe"])
        self.assertTrue((self.dest / "drive_token.json").exists())

    def test_stage2_main_unlinks_tar_and_launches_worker(self):
        tar = self.root / "s2.tar.gz"
        _make_tar(tar, {"h3_remote_worker.py": b"# worker",
                        "task.json": b'{"run_id": "r"}'})
        launch_calls = []

        class _FakeLauncher:
            ROOT = None

            @staticmethod
            def main():
                launch_calls.append("launch")
                print("MUSE_H3_LAUNCH_JSON:{}")

        sys.modules["h3_worker_launcher"] = _FakeLauncher  # type: ignore[assignment]
        try:
            stage2.main(root=self.dest, tar_path=tar)
        finally:
            sys.modules.pop("h3_worker_launcher", None)
        self.assertFalse(tar.exists(), "stage-2 tar is unlinked even on success")
        self.assertEqual(launch_calls, ["launch"])
        self.assertTrue((self.dest / "h3_remote_worker.py").exists())


class BundledExecutionTest(unittest.TestCase):
    def setUp(self):
        self.h = ChainHarness(self)

    def tearDown(self):
        self.h.close()

    def test_bundled_run_succeeds_with_telemetry(self):
        h = self.h
        res = hc.run_variant(_run_args())
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        manifest = h.manifest()
        boot = manifest.get("bootstrap") or {}
        self.assertEqual(boot.get("mode"), "bundled")
        self.assertEqual(boot.get("cli_round_trips"), 4)
        self.assertIsInstance(boot.get("bootstrap_wall_sec"), (int, float))
        # Stage-1 tar + Stage-1 exec, Stage-2 tar + Stage-2 exec.
        uploads = [c for c in h.run_cli_calls if c and c[0] == "upload"]
        self.assertTrue(any(c[-1] == "/content/bootstrap_stage1.tar.gz" for c in uploads))
        self.assertTrue(any(c[-1] == "/content/muse_h3/bootstrap_stage2.tar.gz" for c in uploads))
        exec_names = [name for _s, name in h.exec_calls]
        self.assertIn("h3_bootstrap_stage1.py", exec_names)
        self.assertIn("h3_bootstrap_stage2.py", exec_names)
        # No local tar or token files linger in the run dir.
        run_dir = h.runs_root / RUN_ID
        self.assertFalse(list(run_dir.glob("bootstrap_*.tar.gz")))
        self.assertFalse(list(run_dir.glob("drive_*token*.json")))

    def test_probe_failure_never_builds_stage2(self):
        h = self.h
        real_exec = hc.exec_script

        def _failing_probe(sess, path, timeout=None, env=None):
            if Path(path).name == "h3_bootstrap_stage1.py":
                return {"returncode": 0, "stdout": "", "stderr": "probe exploded"}
            return real_exec(sess, path, timeout=timeout, env=env)

        with mock.patch.object(hc, "exec_script", side_effect=_failing_probe):
            with self.assertRaisesRegex(RuntimeError, "Remote Drive download probe failed"):
                hc.run_variant(_run_args())
        uploads = [c for c in h.run_cli_calls if c and c[0] == "upload"]
        self.assertFalse(any(c[-1].endswith("bootstrap_stage2.tar.gz") for c in uploads),
                         "stage 2 must not be uploaded after a failed probe")
        self.assertFalse(any(name == "h3_bootstrap_stage2.py" for _s, name in h.exec_calls))

    def test_legacy_mode_keeps_old_uploads(self):
        h = self.h
        res = hc.run_variant(_run_args(bootstrap_mode="legacy"))
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        self.assertEqual(res["delivery"]["drive_file_id"], "drive-file-1")
        boot = (h.manifest().get("bootstrap") or {})
        self.assertEqual(boot.get("mode"), "legacy")
        self.assertNotIn("cli_round_trips", boot)
        uploads = [c for c in h.run_cli_calls if c and c[0] == "upload"]
        self.assertFalse(any("bootstrap_stage" in c[-1] for c in uploads),
                         "legacy mode uploads no bundle tars")
        self.assertTrue(any(c[-1].endswith("h3_remote_worker.py") for c in uploads))
        exec_names = [name for _s, name in h.exec_calls]
        self.assertIn("h3_drive_download_probe.py", exec_names)
        self.assertIn("h3_worker_launcher.py", exec_names)

    def test_mount_source_falls_back_to_legacy(self):
        h = self.h
        res = hc.run_variant(_run_args(bootstrap_mode="bundled", drive_source="mount"))
        self.assertEqual(h.manifest_state()[0], "TASK_COMPLETED")
        boot = (h.manifest().get("bootstrap") or {})
        self.assertEqual(boot.get("mode"), "legacy")
        exec_names = [name for _s, name in h.exec_calls]
        self.assertIn("h3_drive_probe.py", exec_names)
        self.assertNotIn("h3_bootstrap_stage1.py", exec_names)


if __name__ == "__main__":
    unittest.main()

"""v0.2.0 F2: offline ComfyUI wheelhouse cache (Muse-original).

Tests the pure-stdlib wheelhouse helpers, the deterministic bundle
build, local verification, the lazy Drive imports (helpers work with
the Drive SDK import-blocked), and the controller's optional
drive_download["wheelhouses"] wiring. All offline: local wheels,
local tarballs, stubbed Drive service. No GPU, no Drive, no
credentials.
"""

import hashlib
import importlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import h3_comfyui_cache as wc
from h3_comfyui_cache import (
    build_wheelhouse,
    build_wheelhouse_task_descriptor,
    filter_requirements_file,
    filter_requirements_lines,
    normalize_pkg_name,
    normalize_python_tag,
    select_wheelhouse_entry,
    validate_wheelhouse_entry,
    verify_local_wheelhouse,
    wheelhouse_archive_name,
)


def _fake_wheel(directory: Path, filename: str, content: bytes = b"FAKEWHEEL") -> Path:
    path = Path(directory) / filename
    path.write_bytes(content)
    return path


class NormalizeTest(unittest.TestCase):
    def test_normalize_pkg_name_pep503(self):
        self.assertEqual(normalize_pkg_name("Pillow"), "pillow")
        self.assertEqual(normalize_pkg_name("nvidia-cuda-runtime-cu12"),
                         "nvidia-cuda-runtime-cu12")
        self.assertEqual(normalize_pkg_name("Foo_Bar.Baz--Qux"), "foo-bar-baz-qux")
        self.assertEqual(normalize_pkg_name("  requests  "), "requests")

    def test_normalize_python_tag(self):
        self.assertEqual(normalize_python_tag("3.11"), "py311")
        self.assertEqual(normalize_python_tag("cp311"), "py311")
        self.assertEqual(normalize_python_tag("py311"), "py311")
        self.assertEqual(normalize_python_tag("3.12"), "py312")
        self.assertEqual(normalize_python_tag("cp312"), "py312")
        # Generic: a future tag normalizes without a code change.
        self.assertEqual(normalize_python_tag("3.13"), "py313")
        self.assertEqual(normalize_python_tag("py313"), "py313")
        self.assertEqual(normalize_python_tag("310"), "py310")
        for bad in ("", "2.7", "py27", "3", "banana"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_python_tag(bad)


class FilterRequirementsTest(unittest.TestCase):
    def test_filter_drops_comments_blanks_options_and_preinstalled(self):
        lines = [
            "# a comment",
            "",
            "   ",
            "pillow==10.4.0  # inline comment",
            "numpy>=2.0",
            "requests",
            "torch==2.5.0",
            "torchvision",
            "torchaudio[extra]>=2.0",
            "triton==3.0.0",
            "nvidia-cuda-runtime-cu12==12.4.0",
            "nvidia-cudnn-cu12",
            "av==14.0.0; python_version>='3.11'",
            "some-pkg @ https://example.com/some-pkg-1.0.tar.gz",
            "-r other-requirements.txt",
            "--index-url https://example.com/simple",
        ]
        self.assertEqual(filter_requirements_lines(lines), [
            "pillow==10.4.0",
            "numpy>=2.0",
            "requests",
            "av==14.0.0; python_version>='3.11'",
            "some-pkg @ https://example.com/some-pkg-1.0.tar.gz",
        ])

    def test_filter_drops_headless_optional_packages(self):
        # v0.2.1 P2: headless UI asset packages are excluded from the
        # wheelhouse; comfyui-frontend-package is KEPT (it exits on
        # ImportError).
        lines = [
            "comfyui-workflow-templates==1.0.0",
            "comfyui-workflow-templates-extra>=0.1",
            "comfyui-embedded-docs",
            "comfyui-frontend-package==1.26.2",
            "pillow==10.4.0",
        ]
        self.assertEqual(filter_requirements_lines(lines), [
            "comfyui-frontend-package==1.26.2",
            "pillow==10.4.0",
        ])

    def test_filter_requirements_file(self):
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "requirements.txt"
            req.write_text("torch\npillow\n", encoding="utf-8")
            self.assertEqual(filter_requirements_file(req), ["pillow"])
        with self.assertRaises(FileNotFoundError):
            filter_requirements_file(Path(d) / "missing.txt")


class ValidateWheelTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        self.dir = Path(self.td)

    def test_valid_wheels(self):
        entry = validate_wheelhouse_entry(
            _fake_wheel(self.dir, "pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl"),
            "py311")
        self.assertEqual(entry["name"], "pillow")
        self.assertEqual(entry["version"], "10.4.0")
        entry = validate_wheelhouse_entry(
            _fake_wheel(self.dir, "requests-2.32.3-py3-none-any.whl"), "py311")
        self.assertEqual(entry["name"], "requests")
        entry = validate_wheelhouse_entry(
            _fake_wheel(self.dir, "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl"),
            "py311")
        self.assertEqual(entry["name"], "numpy")

    def test_rejects_wrong_tag_platform_and_names(self):
        cases = [
            ("av-14.0.0-cp312-cp312-manylinux_2_28_x86_64.whl", "py311",
             "wrong python tag"),
            ("av-14.0.0-cp311-cp311-musllinux_1_2_x86_64.whl", "py311",
             "musllinux is not Colab glibc"),
            ("av-14.0.0-cp311-cp311-manylinux_2_28_aarch64.whl", "py311",
             "wrong architecture"),
            ("av-14.0.0-cp311-cp311-win_amd64.whl", "py311",
             "wrong platform"),
            ("notawheel.txt", "py311", "not a wheel"),
            ("badname.whl", "py311", "unparsable filename"),
        ]
        for filename, pytag, msg in cases:
            path = _fake_wheel(self.dir, filename)
            with self.assertRaises(ValueError, msg=msg):
                validate_wheelhouse_entry(path, pytag)
        with self.assertRaises(ValueError):
            validate_wheelhouse_entry(self.dir / "missing-1.0-py3-none-any.whl",
                                      "py311")

    def test_select_newest_matching(self):
        entries = [
            validate_wheelhouse_entry(
                _fake_wheel(self.dir, "pillow-10.3.0-cp311-cp311-manylinux_2_28_x86_64.whl"),
                "py311"),
            validate_wheelhouse_entry(
                _fake_wheel(self.dir, "pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl"),
                "py311"),
            validate_wheelhouse_entry(
                _fake_wheel(self.dir, "pillow-11.0.0-cp312-cp312-manylinux_2_28_x86_64.whl"),
                "py312"),
        ]
        best = select_wheelhouse_entry(entries, "Pillow", "py311")
        self.assertEqual(best["version"], "10.4.0")
        # The cp312 wheel only matches a py312 request.
        best312 = select_wheelhouse_entry(entries, "pillow", "py312")
        self.assertEqual(best312["version"], "11.0.0")
        self.assertIsNone(select_wheelhouse_entry(entries, "scipy", "py311"))


class DescriptorAndVerifyTest(unittest.TestCase):
    def _descriptor(self):
        entries = [
            {"name": "pillow", "version": "10.4.0",
             "filename": "pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl",
             "sha256": hashlib.sha256(b"A").hexdigest(), "size_bytes": 1},
            {"name": "numpy", "version": "2.1.0",
             "filename": "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
             "sha256": hashlib.sha256(b"B").hexdigest(), "size_bytes": 1},
        ]
        return build_wheelhouse_task_descriptor("abc123", "py311", entries)

    def test_descriptor_sorted_and_named(self):
        desc = self._descriptor()
        self.assertEqual(desc["archive_name"],
                         "comfyui-wheelhouse-abc123-py311.tar.gz")
        self.assertEqual(desc["commit"], "abc123")
        self.assertEqual(desc["python_tag"], "py311")
        self.assertEqual([e["filename"] for e in desc["entries"]],
                         sorted(e["filename"] for e in desc["entries"]))
        # Deterministic: no wall-clock field inside the bundle.
        self.assertNotIn("created_utc", desc)

    def test_verify_local_wheelhouse(self):
        with tempfile.TemporaryDirectory() as d:
            wh = Path(d) / "wheelhouse"
            wh.mkdir()
            (wh / "pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl").write_bytes(b"A")
            (wh / "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl").write_bytes(b"B")
            desc = self._descriptor()
            out = verify_local_wheelhouse(wh, desc)
            self.assertEqual(out["entries_checked"], 2)
            # Tampered bytes fail.
            (wh / "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl").write_bytes(b"X")
            with self.assertRaises(RuntimeError):
                verify_local_wheelhouse(wh, desc)
            (wh / "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl").write_bytes(b"B")
            # An unlisted wheel fails.
            (wh / "extra-1.0-py3-none-any.whl").write_bytes(b"E")
            with self.assertRaises(RuntimeError):
                verify_local_wheelhouse(wh, desc)
            (wh / "extra-1.0-py3-none-any.whl").unlink()
            # A missing entry fails.
            (wh / "pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl").unlink()
            with self.assertRaises(RuntimeError):
                verify_local_wheelhouse(wh, desc)


class BuildWheelhouseTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, ignore_errors=True))
        self.dir = Path(self.td)
        self.wheels = self.dir / "wheels"
        self.wheels.mkdir()
        _fake_wheel(self.wheels, "pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl", b"PILLOW")
        _fake_wheel(self.wheels, "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl", b"NUMPY")
        _fake_wheel(self.wheels, "requests-2.32.3-py3-none-any.whl", b"REQS")
        _fake_wheel(self.wheels, "torch-2.5.0-cp311-cp311-manylinux_2_28_x86_64.whl", b"TORCH")
        self.req = self.dir / "requirements.txt"
        self.req.write_text(
            "# ComfyUI requirements (test fixture)\n"
            "pillow==10.4.0\nnumpy>=2.0\nrequests\ntorch==2.5.0\n",
            encoding="utf-8")

    def _list_tar(self, path):
        with tarfile.open(path, "r:gz") as tf:
            return [(m.name, m.mtime, m.uid, m.gid, m.uname, m.size)
                    for m in tf.getmembers()]

    def test_build_deterministic_and_complete(self):
        out1 = self.dir / "one.tar.gz"
        out2 = self.dir / "two.tar.gz"
        r1 = build_wheelhouse(self.req, "abc123", "py311", self.wheels, out1)
        r2 = build_wheelhouse(self.req, "abc123", "py311", self.wheels, out2)
        self.assertEqual(r1["archive_name"],
                         "comfyui-wheelhouse-abc123-py311.tar.gz")
        self.assertEqual(r1["entries"], 3)  # torch excluded, not missing
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        members = self._list_tar(out1)
        names = [m[0] for m in members]
        self.assertEqual(names, [
            "wheelhouse/numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
            "wheelhouse/pillow-10.4.0-cp311-cp311-manylinux_2_28_x86_64.whl",
            "wheelhouse/requests-2.32.3-py3-none-any.whl",
            "wheelhouse/WHEELHOUSE_META.json",
        ])
        self.assertTrue(all(m[1] == 0 for m in members))  # mtime 0
        self.assertTrue(all(m[2] == 0 and m[3] == 0 for m in members))
        with tarfile.open(out1, "r:gz") as tf:
            meta = json.loads(tf.extractfile(
                "wheelhouse/WHEELHOUSE_META.json").read())
            self.assertEqual(meta["commit"], "abc123")
            self.assertEqual(meta["python_tag"], "py311")
            self.assertEqual([e["name"] for e in meta["entries"]],
                             ["numpy", "pillow", "requests"])
            for entry in meta["entries"]:
                data = tf.extractfile(
                    "wheelhouse/" + entry["filename"]).read()
                self.assertEqual(hashlib.sha256(data).hexdigest(),
                                 entry["sha256"])

    def test_build_missing_wheel_fails_closed_listing_names(self):
        self.req.write_text("pillow\nscipy>=1.0\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as cm:
            build_wheelhouse(self.req, "abc123", "py311", self.wheels,
                             self.dir / "x.tar.gz")
        self.assertIn("scipy", str(cm.exception))
        self.assertFalse((self.dir / "x.tar.gz").exists())

    def test_build_unsupported_pytag_and_missing_dirs(self):
        # v0.2.1 P2: py313 is now supported; py310 is the unsupported tag.
        with self.assertRaises(ValueError):
            build_wheelhouse(self.req, "abc123", "py310", self.wheels,
                             self.dir / "x.tar.gz")
        with self.assertRaises(RuntimeError):
            build_wheelhouse(self.req, "abc123", "py311",
                             self.dir / "no-such-dir", self.dir / "x.tar.gz")
        with self.assertRaises(FileNotFoundError):
            build_wheelhouse(self.dir / "no-req.txt", "abc123", "py311",
                             self.wheels, self.dir / "x.tar.gz")

    def test_build_all_preinstalled_gives_empty_bundle(self):
        self.req.write_text("torch\ntorchvision\n", encoding="utf-8")
        out = self.dir / "empty.tar.gz"
        result = build_wheelhouse(self.req, "abc123", "py311", self.wheels, out)
        self.assertEqual(result["entries"], 0)
        names = [m[0] for m in self._list_tar(out)]
        self.assertEqual(names, ["wheelhouse/WHEELHOUSE_META.json"])


class LazyImportTest(unittest.TestCase):
    def test_pure_helpers_work_with_drive_sdk_blocked(self):
        """Block every google* import, reload the cache module fresh,
        and prove the pure helpers still work (v0.2.0 F2 lazy imports)."""
        import sys
        import importlib.abc

        class _BlockGoogle(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "google" or fullname.startswith("google."):
                    raise ImportError("blocked for test: " + fullname)
                return None

        blocker = _BlockGoogle()
        sys.meta_path.insert(0, blocker)
        try:
            importlib.reload(wc)
            self.assertEqual(wc.normalize_pkg_name("Foo_Bar"), "foo-bar")
            self.assertEqual(wc.normalize_python_tag("3.11"), "py311")
            self.assertEqual(
                wc.filter_requirements_lines(["torch", "pillow"]), ["pillow"])
            entry = wc.validate_wheelhouse_entry(
                _fake_wheel(Path(tempfile.mkdtemp()),
                            "requests-2.32.3-py3-none-any.whl"), "py311")
            self.assertEqual(entry["name"], "requests")
        finally:
            sys.meta_path.remove(blocker)
            importlib.reload(wc)


class ControllerWiringTest(unittest.TestCase):
    def setUp(self):
        import h3_controller as hc
        from h3_contract import MODEL_FILES

        self.hc = hc
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "model_cache").mkdir()
        manifest = {"files": [
            {"path": path, "drive_file_id": f"fid-{i}",
             "sha256": hashlib.sha256(str(i).encode()).hexdigest(),
             "size": 1000 + i, "server_md5": "00" * 16}
            for i, path in enumerate(MODEL_FILES)]}
        (self.root / "model_cache" / "cache_manifest.json").write_text(
            json.dumps(manifest))
        self.cache_manifest = str(
            self.root / "model_cache" / "cache_manifest.json")
        self.root_patch = mock.patch.object(hc, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        # _comfyui_download_task reads the source manifest: absent here.
        self.assertIsNone(hc._comfyui_download_task())

    def _write_wheelhouse_manifest(self, with_drive_ids=True):
        files = {}
        if with_drive_ids:
            files = {"comfyui-wheelhouse-abc123-py311.tar.gz": {
                "id": "fid-wheel", "size": 1234,
                "sha256": hashlib.sha256(b"W").hexdigest(),
                "server_md5": "11" * 16}}
        manifest = {
            "commit": "abc123", "python_tag": "py311",
            "archive_name": "comfyui-wheelhouse-abc123-py311.tar.gz",
            "sha256": hashlib.sha256(b"W").hexdigest(), "size_bytes": 1234,
            "drive_folder_id": "folder-1", "drive_files": files}
        (self.root / "model_cache"
         / "comfyui_wheelhouse_manifest.json").write_text(json.dumps(manifest))

    def test_no_wheelhouse_manifest_means_no_key(self):
        block = self.hc._build_drive_download_block(self.cache_manifest)
        self.assertNotIn("wheelhouses", block)
        self.assertIsNone(self.hc._wheelhouse_download_task())

    def test_wheelhouse_manifest_wires_optional_block(self):
        self._write_wheelhouse_manifest()
        task = self.hc._wheelhouse_download_task()
        self.assertEqual(task["file_id"], "fid-wheel")
        self.assertEqual(task["size"], 1234)
        self.assertEqual(task["archive"],
                         "comfyui-wheelhouse-abc123-py311.tar.gz")
        self.assertEqual(task["commit"], "abc123")
        self.assertEqual(task["python_tag"], "py311")
        block = self.hc._build_drive_download_block(self.cache_manifest)
        self.assertEqual(block["wheelhouses"], [task])

    def test_wheelhouse_manifest_without_drive_ids_means_no_key(self):
        self._write_wheelhouse_manifest(with_drive_ids=False)
        self.assertIsNone(self.hc._wheelhouse_download_task())
        block = self.hc._build_drive_download_block(self.cache_manifest)
        self.assertNotIn("wheelhouses", block)


class Py313AndRemoteVerifyTest(unittest.TestCase):
    """v0.2.1 P2: py313 wheelhouse support + verify() for remote-only
    (staging-cleaned) Drive entries."""

    def test_py313_archive_name_and_entry_selection(self):
        self.assertEqual(wheelhouse_archive_name("abc123", "py313"),
                         "comfyui-wheelhouse-abc123-py313.tar.gz")
        with tempfile.TemporaryDirectory() as d:
            entry = validate_wheelhouse_entry(
                _fake_wheel(Path(d),
                            "av-17.0.0-cp313-cp313-manylinux_2_28_x86_64.whl"),
                "py313")
            self.assertEqual(entry["name"], "av")
            self.assertEqual(entry["platform_tag"], "manylinux_2_28_x86_64")
            picked = select_wheelhouse_entry([entry], "av", "py313")
            self.assertIsNotNone(picked)
            self.assertIsNone(select_wheelhouse_entry([entry], "av", "py312"))

    def _manifest(self, tmp: Path, rel: str = "comfyui-abc123.tar.gz"):
        manifest = {"drive_folder_id": "folder-1",
                    "drive_files": {rel: {"id": "drive-file-1", "size": 100,
                                          "sha256": "a" * 64,
                                          "server_md5": "b" * 32}}}
        (tmp / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def test_verify_remote_only_entry_uses_server_metadata(self):
        import drive_api
        import drive_depot
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._manifest(tmp)
            meta = {"id": "drive-file-1", "size": "100",
                    "md5Checksum": "B" * 32, "sha256Checksum": "A" * 64,
                    "trashed": False}
            with mock.patch.object(wc, "LOCAL_MANIFEST",
                                   tmp / "manifest.json"), \
                    mock.patch.object(wc, "STAGING", tmp / "staging"), \
                    mock.patch.object(drive_depot, "drive_service",
                                      return_value=mock.Mock()), \
                    mock.patch.object(drive_depot, "account_matches",
                                      return_value=True), \
                    mock.patch.object(drive_api, "get_file_evidence",
                                      return_value=meta) as evidence, \
                    mock.patch.object(drive_api, "verify_uploaded_file") as local_verify:
                result = wc.verify("adc")
            evidence.assert_called_once()
            local_verify.assert_not_called()
            self.assertTrue(result["files"]["comfyui-abc123.tar.gz"]["server_verified"])
            self.assertEqual(result["files"]["comfyui-abc123.tar.gz"]["sha256"],
                             "a" * 64)

    def test_verify_remote_only_rejects_bad_server_md5(self):
        import drive_api
        import drive_depot
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._manifest(tmp)
            meta = {"id": "drive-file-1", "size": "100",
                    "md5Checksum": "c" * 32, "sha256Checksum": "A" * 64,
                    "trashed": False}
            with mock.patch.object(wc, "LOCAL_MANIFEST",
                                   tmp / "manifest.json"), \
                    mock.patch.object(wc, "STAGING", tmp / "staging"), \
                    mock.patch.object(drive_depot, "drive_service",
                                      return_value=mock.Mock()), \
                    mock.patch.object(drive_depot, "account_matches",
                                      return_value=True), \
                    mock.patch.object(drive_api, "get_file_evidence",
                                      return_value=meta):
                with self.assertRaises(RuntimeError):
                    wc.verify("adc")

    def test_verify_local_entry_still_uses_local_file(self):
        import drive_api
        import drive_depot
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._manifest(tmp)
            staging = tmp / "staging"
            staging.mkdir()
            (staging / "comfyui-abc123.tar.gz").write_bytes(b"x" * 100)
            proof = {"size": 100, "sha256": "a" * 64}
            with mock.patch.object(wc, "LOCAL_MANIFEST",
                                   tmp / "manifest.json"), \
                    mock.patch.object(wc, "STAGING", staging), \
                    mock.patch.object(drive_depot, "drive_service",
                                      return_value=mock.Mock()), \
                    mock.patch.object(drive_depot, "account_matches",
                                      return_value=True), \
                    mock.patch.object(drive_api, "verify_uploaded_file",
                                      return_value=proof) as local_verify, \
                    mock.patch.object(drive_api, "get_file_evidence") as evidence:
                result = wc.verify("adc")
            local_verify.assert_called_once()
            evidence.assert_not_called()
            self.assertTrue(result["files"]["comfyui-abc123.tar.gz"]["server_verified"])


class CliGateTest(unittest.TestCase):
    def test_publish_wheelhouse_requires_allow_drive_write(self):
        with mock.patch.object(sys, "argv", [
                "h3_comfyui_cache.py", "publish-wheelhouse",
                "--requirements", "r.txt", "--commit", "abc123",
                "--pytag", "py311", "--wheels-dir", "w",
                "--colab-auth", "adc"]):
            with self.assertRaises(SystemExit) as cm:
                wc.cli()
            self.assertEqual(cm.exception.code, 2)

    def test_build_wheelhouse_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            wheels = base / "wheels"
            wheels.mkdir()
            _fake_wheel(wheels, "requests-2.32.3-py3-none-any.whl", b"REQS")
            req = base / "requirements.txt"
            req.write_text("requests\n", encoding="utf-8")
            out = base / "bundle.tar.gz"
            with mock.patch.object(sys, "argv", [
                    "h3_comfyui_cache.py", "build-wheelhouse",
                    "--requirements", str(req), "--commit", "abc123",
                    "--pytag", "py311", "--wheels-dir", str(wheels),
                    "--out", str(out)]):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    self.assertEqual(wc.cli(), 0)
            printed = json.loads(stdout.getvalue())
            self.assertEqual(printed["archive"],
                             "comfyui-wheelhouse-abc123-py311.tar.gz")
            self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()

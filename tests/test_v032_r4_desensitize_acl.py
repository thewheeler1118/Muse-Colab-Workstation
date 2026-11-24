"""TASK-RELEASE-V032 R4: public-tree desensitization + read-only Drive ACL audit.

Red-first tests (written before the fix): every test here failed on the
v0.3.1 baseline — real Drive IDs were still committed, secret_scan had no
Drive-ID rule, drive_depot had no audit-acl, and the repo had no LICENSE.

All Drive-ID-shaped values in this file are built dynamically from short
fragments (never written as a literal), so this file itself contains no
real-shape Drive ID for the secret scan to flag. Nothing here touches a
GPU, Drive, or any credential: the ACL audit runs against a fake Drive
service, and the local-ID resolution runs against temp files.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))  # secret_scan lives in scripts/

import drive_depot as dd  # noqa: E402
import h3_controller as hc  # noqa: E402
import h3_contract as h3c  # noqa: E402
import secret_scan  # noqa: E402


def _fake_drive_id(seed="Ab"):
    """A synthetic 33-char Drive-shaped token (mixed case, starts with 1).

    Built dynamically so no real-shape literal appears in this file.
    It is NOT a real asset ID: the pattern is obviously synthetic.
    """
    return "1" + (seed * 16)[:32]


class FakeDriveService:
    """Minimal fake Drive v3 service: read-only surface only.

    Only files().get / files().list and permissions().list exist. If the
    code under test called any mutating method (create/update/delete) it
    would raise AttributeError here, which is the point: audit-acl must
    be provably read-only.
    """

    def __init__(self, perms_by_id, names_by_id=None, folders_by_name=None,
                 children_by_id=None):
        self.perms_by_id = perms_by_id
        self.names_by_id = names_by_id or {}
        self.folders_by_name = folders_by_name or {}
        self.children_by_id = children_by_id or {}
        self.calls = []

    def files(self):
        return _FakeFiles(self)

    def permissions(self):
        return _FakePermissions(self)


class _FakeFiles:
    def __init__(self, svc):
        self.svc = svc

    def get(self, fileId=None, fields=None, supportsAllDrives=None):
        self.svc.calls.append(("files.get", fileId))
        return _FakeCall({"id": fileId,
                          "name": self.svc.names_by_id.get(fileId, fileId),
                          "mimeType": "application/octet-stream",
                          "trashed": False})

    def list(self, q=None, spaces=None, pageSize=None, fields=None,
             supportsAllDrives=None, includeItemsFromAllDrives=None):
        self.svc.calls.append(("files.list", q))
        import re as _re
        m = _re.search(r"name = '([^']+)'", q or "")
        if m:
            found = self.svc.folders_by_name.get(m.group(1), [])
            return _FakeCall({"files": found})
        m = _re.search(r"'([^']+)' in parents", q or "")
        if m:
            return _FakeCall({"files": self.svc.children_by_id.get(m.group(1), [])})
        return _FakeCall({"files": []})


class _FakePermissions:
    def __init__(self, svc):
        self.svc = svc

    def list(self, fileId=None, fields=None, supportsAllDrives=None):
        self.svc.calls.append(("permissions.list", fileId))
        return _FakeCall({"permissions": self.svc.perms_by_id.get(fileId, [])})


class _FakeCall:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class SecretScanDriveIdTest(unittest.TestCase):
    def test_detects_real_shaped_drive_id(self):
        line = "drive_file_id: " + _fake_drive_id("Ab3dEf6h")
        self.assertTrue(secret_scan.contains_real_drive_id(line))

    def test_allows_example_placeholders(self):
        for value in h3c.DRIVE_ID_DEFAULTS.values():
            self.assertFalse(secret_scan.contains_real_drive_id(
                f'drive_file_id: "{value}"'), value)

    def test_allows_test_fake_ids(self):
        for fake in ("abc", "id1", "fid-0", "root123", "tok-1234",
                     "folder1", "m1"):
            self.assertFalse(secret_scan.contains_real_drive_id(fake), fake)

    def test_allows_incident_filename(self):
        # docs/incidents/NN-<slug>.md filenames start with "1" but are
        # local files, not Drive IDs (no uppercase letter in the token).
        line = "| `docs/incidents/15-git-clone-exit-128-stderr-lost.md` | 959 |"
        self.assertFalse(secret_scan.contains_real_drive_id(line))

    def test_allows_hashes(self):
        # 40/64-char hex hashes (incl. ones starting with "1") are not
        # Drive IDs: hex-only, and longer than the 33-char token shape.
        self.assertFalse(secret_scan.contains_real_drive_id("1" + "a" * 63))
        self.assertFalse(secret_scan.contains_real_drive_id("1" + "a" * 39))
        self.assertFalse(secret_scan.contains_real_drive_id(
            "61e524bdba7a4e193b45c2697f1af83c1beb86d6"))

    def test_no_real_drive_ids_in_tracked_files(self):
        # The whole public tree must be free of real-shape Drive IDs.
        # SOURCE_MANIFEST.json is included: it holds hashes, not IDs.
        r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                           text=True, check=True)
        offenders = []
        for rel in r.stdout.splitlines():
            p = ROOT / rel
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if secret_scan.contains_real_drive_id(line):
                    offenders.append(f"{rel}:{i}")
        self.assertEqual(offenders, [])


class LocalDriveIdsTest(unittest.TestCase):
    def test_constants_are_example_placeholders_offline(self):
        self.assertEqual(h3c.POSTER_DRIVE_ID, "EXAMPLE_DRIVE_POSTER_ID")
        self.assertEqual(h3c.STORY_DRIVE_ID, "EXAMPLE_DRIVE_STORY_ID")

    def test_env_var_wins(self):
        fake = _fake_drive_id("Zz9q")
        with mock.patch.dict(os.environ, {"H3_DRIVE_ID_POSTER": fake}):
            self.assertEqual(h3c.resolve_drive_id("POSTER"), fake)

    def test_local_file_used_when_no_env(self):
        fake = _fake_drive_id("Qw7x")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "local_drive_ids.json"
            p.write_text(json.dumps({"POSTER": fake}))
            os.chmod(p, 0o600)
            with mock.patch.object(h3c, "LOCAL_DRIVE_IDS_PATH", p):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("H3_DRIVE_ID_POSTER", None)
                    self.assertEqual(h3c.resolve_drive_id("POSTER"), fake)

    def test_overpermissive_local_file_ignored(self):
        fake = _fake_drive_id("Qw7x")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "local_drive_ids.json"
            p.write_text(json.dumps({"POSTER": fake}))
            os.chmod(p, 0o644)
            with mock.patch.object(h3c, "LOCAL_DRIVE_IDS_PATH", p):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("H3_DRIVE_ID_POSTER", None)
                    self.assertEqual(h3c.resolve_drive_id("POSTER"),
                                     "EXAMPLE_DRIVE_POSTER_ID")

    def test_missing_file_returns_example_default(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(h3c, "LOCAL_DRIVE_IDS_PATH",
                                   Path(td) / "does-not-exist.json"):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("H3_DRIVE_ID_POSTER", None)
                    self.assertEqual(h3c.resolve_drive_id("POSTER"),
                                     "EXAMPLE_DRIVE_POSTER_ID")

    def test_malformed_local_file_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "local_drive_ids.json"
            p.write_text("{not json")
            os.chmod(p, 0o600)
            with mock.patch.object(h3c, "LOCAL_DRIVE_IDS_PATH", p):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("H3_DRIVE_ID_POSTER", None)
                    self.assertEqual(h3c.resolve_drive_id("POSTER"),
                                     "EXAMPLE_DRIVE_POSTER_ID")

    def test_local_drive_ids_example_is_tracked_and_placeholder_only(self):
        example = ROOT / "model_cache" / "local_drive_ids.example.json"
        self.assertTrue(example.is_file())
        data = json.loads(example.read_text())
        self.assertIn("POSTER", data)
        self.assertEqual({k for k in data if not k.startswith("_")},
                         set(h3c.DRIVE_ID_DEFAULTS))
        for key, value in data.items():
            if key.startswith("_"):
                continue
            self.assertTrue(str(value).startswith("EXAMPLE_DRIVE_"), value)


class AuditAclTest(unittest.TestCase):
    def _private_service(self):
        ids = [_fake_drive_id("Ab"), _fake_drive_id("Cd")]
        perms = {i: [{"id": "p1", "type": "user", "role": "owner"}]
                 for i in ids}
        return FakeDriveService(perms), ids

    def test_private_targets_report_acl_private_verified(self):
        svc, ids = self._private_service()
        report = dd.audit_acl(svc, targets=[("depot_root", ids[0]),
                                            ("model:diffusion", ids[1])])
        self.assertEqual(report["status"], "ACL_PRIVATE_VERIFIED")
        self.assertEqual(report["exposed"], [])
        self.assertEqual(len(report["checked"]), 2)

    def test_anyone_exposure_raises_fail_closed(self):
        ids = [_fake_drive_id("Ab"), _fake_drive_id("Cd")]
        perms = {ids[0]: [{"id": "p1", "type": "user", "role": "owner"}],
                 ids[1]: [{"id": "any", "type": "anyone", "role": "reader"}]}
        svc = FakeDriveService(perms)
        with self.assertRaisesRegex(RuntimeError, "ACL_EXPOSED"):
            dd.audit_acl(svc, targets=[("depot_root", ids[0]),
                                       ("model:diffusion", ids[1])])

    def test_run_audit_acl_exit_codes(self):
        svc, ids = self._private_service()
        self.assertEqual(dd.run_audit_acl(
            svc, targets=[("depot_root", ids[0])]), 0)
        exposed = FakeDriveService(
            {ids[0]: [{"id": "any", "type": "anyone", "role": "reader"}]})
        self.assertEqual(dd.run_audit_acl(
            exposed, targets=[("depot_root", ids[0])]), 2)

    def test_no_targets_is_unavailable_not_verified(self):
        svc = FakeDriveService({})
        with self.assertRaises(dd.AclAuditUnavailable):
            dd.audit_acl(svc, targets=[])
        self.assertEqual(dd.run_audit_acl(svc, targets=[]), 2)

    def test_audit_is_read_only_surface(self):
        # The fake service only implements files().get/list and
        # permissions().list; any mutating call would raise AttributeError.
        svc, ids = self._private_service()
        dd.audit_acl(svc, targets=[("depot_root", ids[0])])
        kinds = {c[0] for c in svc.calls}
        self.assertEqual(kinds, {"files.get", "permissions.list"})

    def test_audit_targets_resolve_from_local_config(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            root_id = _fake_drive_id("Rt")
            model_id = _fake_drive_id("Md")
            (td / "depot_config.json").write_text(json.dumps(
                {"drive_root_folder_id": root_id,
                 "remote_manifest": {"drive_file_id": _fake_drive_id("Rm")}}))
            (td / "verified_manifest.json").write_text(json.dumps(
                {"files": [{"path": "diffusion_models/x.safetensors",
                            "drive_file_id": model_id}]}))
            svc = FakeDriveService({})
            with (mock.patch.object(dd, "CFG", td / "depot_config.json"),
                  mock.patch.object(dd, "READY", td / "verified_manifest.json")):
                targets = dd.audit_targets(svc)
            labels = [label for label, _ in targets]
            self.assertIn("depot_root", labels)
            self.assertIn("ready_manifest", labels)
            self.assertIn("model:diffusion_models/x.safetensors", labels)


class LicenseTest(unittest.TestCase):
    def test_license_exists_and_is_apache_2(self):
        lic = ROOT / "LICENSE"
        self.assertTrue(lic.is_file(), "repo root LICENSE missing")
        text = lic.read_text(encoding="utf-8")
        self.assertIn("Apache License", text)
        self.assertIn("Version 2.0, January 2004", text)
        self.assertIn("END OF TERMS AND CONDITIONS", text)
        # Canonical Apache-2.0 phrasing (present in the apache.org text).
        self.assertIn("bind by name", text)
        self.assertIn("Grant of Copyright License", text)
        self.assertIn("Grant of Patent License", text)
        self.assertIn("WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND", text)

    def test_license_copyright_notice_names_upstream_and_muse(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("SkyOneBranch", text)
        self.assertIn("Muse-Colab-Workstation", text)


class VerifyDriveWiringTest(unittest.TestCase):
    def test_verify_drive_sh_runs_audit_acl_when_live(self):
        text = (ROOT / "scripts" / "verify_drive.sh").read_text()
        self.assertIn("audit-acl", text)


class PreflightAclWiringTest(unittest.TestCase):
    """drive_acl_audit is a preflight check: skipped (pass, explicit)
    with no local depot config, fail-closed when the audit exposes."""

    def _args(self, td, spec_path):
        return SimpleNamespace(
            run_id="test-run", task_spec=str(spec_path), variant=None,
            production_task_id="h3-preflight-test", drive_source="download",
            cache_manifest=str(Path(td) / "manifest.json"),
            workflow_api=str(ROOT / "workflow_api.json"),
            comfy_ref=hc.COMFY_REF_DEFAULT,
            gpu_policy="G4_THEN_A100_HIGHMEM",
            project_budget_cu=30.0, per_session_budget_cu=25.0)

    def _scaffold_root(self, td):
        root_dir = Path(td) / "root"
        required = ["h3_controller.py", "h3_contract.py", "h3_remote_worker.py",
                    "h3_gpu_probe.py", "h3_drive_probe.py", "h3_init_remote.py",
                    "assemble.py", "build_api_candidate.py",
                    "drive_depot.py", "drive_api.py", "drive_resumable.py",
                    "auth_support.py", "workflow_api.json",
                    "model_cache/verified_manifest.json", "h3_budget.py",
                    "h3_session_watchdog.py", "h3_video_qa.py",
                    "h3_mount_timeline.py", "h3_mount_signature_probe.py",
                    "h3_comfyui_cache.py", "V3_DRIVEMOUNT_TIMEOUT_PATCH.md"]
        for rel in required:
            target = root_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = ROOT / rel
            if src.exists():
                target.symlink_to(src)
            else:
                target.write_text("")
        return root_dir

    def _run_preflight(self, args, root_dir, td, extra_patches=()):
        import subprocess

        patches = [
            mock.patch.object(hc, "ROOT", root_dir),
            # Hermetic budget gate: preflight's _budget reads the module
            # global hc.LEDGER_PATH, which an earlier suite test rebinds
            # to its temp dir (pre-existing suite-ordering leak via
            # _run_variant_inner/main: they reassign the global from the
            # then-current ROOT). Point it at a path that does not exist
            # so this test exercises the "ledger not yet created" branch
            # instead of inheriting that stale temp ledger.
            mock.patch.object(hc, "LEDGER_PATH",
                              Path(td) / "budget-ledger-absent.json"),
            mock.patch.object(hc, "audit_user_adc_file", return_value="adc ok"),
            mock.patch("drive_depot.drive_service", return_value=mock.MagicMock()),
            mock.patch("drive_depot.account_matches", return_value=True),
            mock.patch("drive_depot.verify_ready", return_value={"status": "READY"}),
            mock.patch.object(hc, "verify_drive_download_preflight",
                              return_value={"token_id": "tok-1234",
                                            "probe_file_id": "fid-0"}),
            mock.patch.object(hc, "verify_drive_file_token_archive",
                              return_value={"archive_folder": "Muse-H3-Video-Outputs",
                                            "token_id": "tok-5678"}),
            mock.patch.object(hc, "run_cli",
                              return_value={"returncode": 0, "stdout": "0.7.5",
                                            "stderr": ""}),
            mock.patch.object(hc, "cli_post_body_patch_present", return_value=True),
            mock.patch.object(hc, "cli_drivemount_timeout_patch_present", return_value=True),
            mock.patch("h3_environment.verify_environment",
                       return_value={"ok": True, "cli_version": "0.7.5"}),
            mock.patch.object(subprocess, "run",
                              return_value=SimpleNamespace(stdout="TTY_OK", stderr="")),
            mock.patch.object(hc, "read_live_usage", return_value={"balance": 144.19}),
            mock.patch.object(hc, "inspect",
                              return_value={"sessions": [], "cu_balance": 144.19,
                                            "rate": 10.05}),
        ]
        patches.extend(extra_patches)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        return hc.preflight(args)

    def _valid_spec(self, td):
        # Minimal valid spec (same shape as test_task_spec._valid_spec_dict).
        ref1 = Path(td) / "ref1.jpg"
        ref1.write_bytes(b"ref-one")
        ref2 = Path(td) / "ref2.jpg"
        ref2.write_bytes(b"ref-two")
        spec = {
            "task_id": "h3-preflight-test",
            "output_prefix": "test",
            "ref_image_1": {"local_path": str(ref1),
                            "sha256": hashlib.sha256(b"ref-one").hexdigest()},
            "ref_image_2": {"local_path": str(ref2),
                            "sha256": hashlib.sha256(b"ref-two").hexdigest()},
            "prompt": "test prompt",
            "seed": 42,
        }
        spec_path = Path(td) / "spec.json"
        spec_path.write_text(json.dumps(spec))
        (Path(td) / "manifest.json").write_text(json.dumps({
            "status": "READY", "verification_method": "drive_v3_server_api",
            "account_verified": True, "verified_at_utc": "2026-11-10T01:00:00Z",
            "files": [{"path": p, "drive_file_id": "abc", "size": 100,
                       "sha256": "a" * 64, "server_verified": True,
                       "hf_revision": "fixed"} for p in sorted(h3c.MODEL_FILES)]}))
        return spec_path

    def test_preflight_acl_check_skipped_offline_without_depot_config(self):
        with tempfile.TemporaryDirectory() as td:
            spec_path = self._valid_spec(td)
            result = self._run_preflight(self._args(td, spec_path),
                                         self._scaffold_root(td), td)
            self.assertTrue(result["passed"], result.get("failed"))
            self.assertIn("drive_acl_audit", result["checks"])
            self.assertTrue(result["checks"]["drive_acl_audit"]["pass"])
            self.assertIn("SKIPPED",
                          result["checks"]["drive_acl_audit"]["detail"])

    def test_preflight_acl_exposure_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            spec_path = self._valid_spec(td)
            cfg = Path(td) / "depot_config.json"
            cfg.write_text("{}")
            extra = [
                mock.patch.object(dd, "CFG", cfg),
                mock.patch.object(dd, "audit_acl",
                                  side_effect=RuntimeError(
                                      "ACL_EXPOSED: test-target")),
            ]
            with self.assertRaisesRegex(RuntimeError, "drive_acl_audit"):
                self._run_preflight(self._args(td, spec_path),
                                    self._scaffold_root(td), td, extra)


if __name__ == "__main__":
    unittest.main()

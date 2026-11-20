"""Build and publish the pinned ComfyUI source cache to Google Drive.

The cache lives OUTSIDE the model depot, in a dedicated folder
``Muse-H3-ComfyUI-Source`` in the same authorized Google account. It holds exactly three files:

  comfyui-<commit>.tar.gz        git archive of the pinned commit
                                  (prefix comfyui/, plus a COMFYUI_COMMIT.txt
                                  marker carrying the commit id)
  comfyui-<commit>.tar.gz.sha256 hex SHA-256 of the archive
  comfyui-source-manifest.json   repo, commit, archive name, sha256, size,
                                  created_utc, source

The remote worker restores this archive instead of cloning from GitHub
whenever the cache is present, and verifies it (archive SHA-256 against
the task, then the embedded commit id). No model file and no READY
manifest is touched: the folder is separate from Muse-H3-Model-Depot.

Usage (from the project root):
  python3 h3_comfyui_cache.py build --repo comfyui-source --commit <sha>
      # local only: builds the archive + manifest under model_cache/
  python3 h3_comfyui_cache.py publish --repo comfyui-source --commit <sha> \
      --colab-auth adc --allow-drive-write
      # builds, creates the Drive folder, uploads the three files,
      # re-verifies each from server metadata, writes
      # model_cache/comfyui_source_manifest.json (0600, with Drive ids)
  python3 h3_comfyui_cache.py verify --colab-auth adc
      # read-only: re-checks the Drive files against the local manifest

v0.2.0 F2 (TASK-VERSION-ROADMAP-011, Muse-original reimplementation
of the upstream v0.2.0 behaviour spec — no upstream code copied):
an offline wheelhouse cache next to the source archive. A wheelhouse
bundle is a deterministic tarball
``comfyui-wheelhouse-<commit>-<pytag>.tar.gz`` holding
``wheelhouse/*.whl`` + ``wheelhouse/WHEELHOUSE_META.json`` for one
pinned commit and one Python tag (py311/py312, Colab Linux x86_64),
with preinstalled packages (torch, torchvision, torchaudio, triton,
nvidia-*) excluded — the worker installs those from the Colab image,
everything else from the wheelhouse (worker-side install is F3).
The bundle is built from a local wheels directory, published to the
same Drive folder, and wired into the controller's drive_download
block as optional ``wheelhouses`` entries. Drive SDK imports are lazy
in this module so the pure-stdlib wheelhouse helpers work without
any Drive dependency.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# NOTE (v0.2.0 F2): no Drive imports at module top. drive_api /
# drive_depot are imported lazily inside the functions that touch
# Drive, so the pure-stdlib wheelhouse helpers below work with no
# Drive SDK installed. (drive_api itself is stdlib-only at top level;
# its google imports are lazy inside its functions — but this module
# does not depend on that; it simply never imports Drive code until
# a Drive operation is requested.)

COMFYUI_REPO_URL = "https://github.com/Comfy-Org/ComfyUI.git"
CACHE_FOLDER_NAME = "Muse-H3-ComfyUI-Source"
CACHE_DRIVE_DIR = "/content/drive/MyDrive/Muse-H3-ComfyUI-Source"
LOCAL_MANIFEST = ROOT / "model_cache" / "comfyui_source_manifest.json"
STAGING = ROOT / "model_cache" / "comfyui_source"

# -- v0.2.0 F2: offline wheelhouse cache --

# Python tags a wheelhouse bundle may be built for. The tag
# normalizer itself is generic (no hardcoded list); this tuple is
# the build-time allowlist for Colab (P2 adds py313).
SUPPORTED_PYTHON_TAGS = ("py311", "py312")
# Packages the Colab image already provides: never bundled, never
# installed from the wheelhouse. nvidia-* matches by prefix.
PREINSTALLED_PACKAGES = ("torch", "torchvision", "torchaudio", "triton")
PREINSTALLED_PREFIXES = ("nvidia-",)
WHEELHOUSE_LOCAL_MANIFEST = (
    ROOT / "model_cache" / "comfyui_wheelhouse_manifest.json")
WHEELHOUSE_STAGING = ROOT / "model_cache" / "comfyui_wheelhouse"
WHEELHOUSE_DIR_IN_ARCHIVE = "wheelhouse"
WHEELHOUSE_META_NAME = "WHEELHOUSE_META.json"


def archive_name(commit: str) -> str:
    return f"comfyui-{commit}.tar.gz"


def build_archive(repo_dir: Path, commit: str, out_path: Path) -> str:
    """git archive of <commit> (+ COMFYUI_COMMIT.txt marker), gzipped
    deterministically (mtime=0). Returns the SHA-256 of the .tar.gz."""
    from drive_api import file_digests  # lazy: Drive SDK not needed earlier

    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "archive", commit, "--prefix=comfyui/"],
        capture_output=True, check=True, timeout=600)
    raw_tar = proc.stdout
    if not raw_tar:
        raise RuntimeError("git archive produced no output for " + commit)
    with tempfile.TemporaryDirectory() as d:
        tar_path = Path(d) / "comfyui.tar"
        tar_path.write_bytes(raw_tar)
        with tarfile.open(tar_path, "a") as tf:
            data = (commit + "\n").encode()
            info = tarfile.TarInfo("comfyui/COMFYUI_COMMIT.txt")
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tar_path.open("rb") as fi, gzip.GzipFile(
                filename="", mode="wb", fileobj=out_path.open("wb"),
                compresslevel=9, mtime=0) as gz:
            for chunk in iter(lambda: fi.read(4 * 1024 * 1024), b""):
                gz.write(chunk)
    return file_digests(out_path)["sha256"]


def build_manifest(repo_url: str, commit: str, name: str, sha256: str,
                   size: int) -> dict:
    return {
        "repo": repo_url,
        "commit": commit,
        "archive_name": name,
        "sha256": sha256,
        "size_bytes": size,
        "created_utc": datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "git archive of the pinned ComfyUI commit, "
                  "plus COMFYUI_COMMIT.txt marker",
        "cache_folder": CACHE_FOLDER_NAME,
        "cache_drive_dir": CACHE_DRIVE_DIR,
    }


def verify_local_archive(repo_dir: Path, commit: str, archive_path: Path,
                         expected_sha256: str) -> dict:
    """Independent local check: SHA-256, extract, marker commit, and the
    extracted tree's files match the pinned commit's tree."""
    from drive_api import file_digests  # lazy: Drive SDK not needed earlier

    digests = file_digests(archive_path)
    if digests["sha256"] != expected_sha256:
        raise RuntimeError("local archive SHA-256 mismatch")
    with tempfile.TemporaryDirectory() as d:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(d, filter="data")
        marker = Path(d) / "comfyui" / "COMFYUI_COMMIT.txt"
        if not marker.is_file() or marker.read_text().strip() != commit:
            raise RuntimeError("local archive commit marker mismatch")
        for name in ("main.py", "requirements.txt"):
            if not (Path(d) / "comfyui" / name).is_file():
                raise RuntimeError("local archive missing " + name)
        # Tree identity: the extracted tree must equal the pinned commit's.
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", commit + "^{tree}"],
            capture_output=True, text=True, check=True, timeout=60)
        tree = proc.stdout.strip()
        listed = subprocess.run(
            ["git", "-C", str(repo_dir), "ls-tree", "-r", "--format=%(objectname) %(path)",
             commit],
            capture_output=True, text=True, check=True, timeout=60)
        expected = {}
        for line in listed.stdout.splitlines():
            oid, path = line.split(" ", 1)
            expected[path] = oid
        import hashlib as _h
        for path, oid in expected.items():
            data = (Path(d) / "comfyui" / path).read_bytes()
            blob = _h.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if blob != oid:
                raise RuntimeError("extracted file differs from pinned tree: " + path)
    return {"archive_sha256": digests["sha256"], "size_bytes": digests["size"],
            "tree": tree, "files_checked": len(expected)}


# -- v0.2.0 F2: pure-stdlib wheelhouse helpers (no Drive dependency) --

def normalize_pkg_name(name: str) -> str:
    """PEP 503 normalization: lowercase, runs of ``-_.`` become ``-``."""
    return re.sub(r"[-_.]+", "-", (name or "").strip().lower())


def normalize_python_tag(tag: str) -> str:
    """Normalize a Python tag to ``py<major><minor>`` (``"3.11"``,
    ``"cp311"``, ``"py311"`` all become ``"py311"``). Generic: any
    ``3.x`` normalizes, so a future ``py313`` needs no code change
    here — the build-time allowlist decides what may be built.
    Raises ValueError on anything that is not a ``3.x`` tag."""
    text = (tag or "").strip().lower().replace(".", "")
    for prefix in ("cp", "py"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if not re.fullmatch(r"3\d+", text):
        raise ValueError("not a Python 3.x tag: " + repr(tag))
    return "py" + text


def _requirement_name(line: str) -> str | None:
    """Normalized package name for one requirements line, or None for
    blank / comment / installer-option lines. Handles extras
    (``name[extra]``), version specifiers, environment markers and
    direct references (``name @ url``)."""
    text = line.split("#", 1)[0].strip()
    if not text or text.startswith("-"):
        return None
    if " @" in text:
        text = text.split(" @", 1)[0].strip()
    text = text.split("[", 1)[0]
    match = re.match(r"[A-Za-z0-9._-]+", text)
    if not match:
        return None
    return normalize_pkg_name(match.group(0))


def _is_preinstalled(normalized_name: str) -> bool:
    """True for packages the Colab image already provides."""
    if normalized_name in PREINSTALLED_PACKAGES:
        return True
    return any(normalized_name.startswith(p) for p in PREINSTALLED_PREFIXES)


def filter_requirements_lines(lines) -> list:
    """Keep the requirement lines a wheelhouse must cover: strip
    comments/blanks/installer options and drop preinstalled packages.
    Returns the kept lines (comment-stripped, whitespace-trimmed) in
    file order."""
    kept = []
    for line in lines:
        text = line.split("#", 1)[0].strip()
        if not text or text.startswith("-"):
            continue
        name = _requirement_name(line)
        if name is None or _is_preinstalled(name):
            continue
        kept.append(text)
    return kept


def filter_requirements_file(path) -> list:
    """Read a requirements file and return its filtered lines (see
    filter_requirements_lines). Raises FileNotFoundError when missing."""
    return filter_requirements_lines(Path(path).read_text(
        encoding="utf-8").splitlines())


_WHEEL_RE = re.compile(
    r"^(?P<distribution>[^-]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>[^-]+))?-(?P<python_tag>[^-]+)"
    r"-(?P<abi_tag>[^-]+)-(?P<platform_tag>[^-]+)\.whl$")


def _wheel_matches_pytag(python_tag: str, abi_tag: str, platform_tag: str,
                         pytag: str) -> bool:
    """True when a wheel's tags cover ``pytag`` on Colab Linux x86_64.
    Python tag components may list several tags (``cp311.cp312``);
    ``py3`` covers any 3.x, ``cp311`` covers ``py311``. Platform must
    be ``any`` (pure Python, abi ``none``) or a manylinux/linux x86_64
    tag — musllinux is rejected (Colab is glibc)."""
    want_cp = "cp" + pytag[2:]
    ok_python = False
    for component in python_tag.split("."):
        if component in (pytag, want_cp, "py3"):
            ok_python = True
            break
    if not ok_python:
        return False
    ok_platform = False
    for component in platform_tag.split("."):
        if component == "any" and abi_tag == "none":
            ok_platform = True
            break
        if re.fullmatch(r"(manylinux.*|linux)_x86_64", component):
            ok_platform = True
            break
    return ok_platform


def validate_wheelhouse_entry(path, pytag: str) -> dict:
    """Validate one wheel file for ``pytag``. Returns ``{name,
    version, filename, python_tag, abi_tag, platform_tag, path}``
    with the distribution name PEP 503-normalized. Raises ValueError
    on a missing file, a non-wheel name, an unparsable filename, or
    tags that do not cover ``pytag`` on Colab Linux x86_64."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("wheel file missing: " + str(path))
    if path.suffix != ".whl":
        raise ValueError("not a .whl file: " + str(path))
    match = _WHEEL_RE.match(path.name)
    if not match:
        raise ValueError("unparsable wheel filename: " + path.name)
    parts = match.groupdict()
    if not _wheel_matches_pytag(parts["python_tag"], parts["abi_tag"],
                                parts["platform_tag"], pytag):
        raise ValueError(
            "wheel tags do not cover " + pytag + " on Colab Linux x86_64: "
            + path.name)
    return {"name": normalize_pkg_name(parts["distribution"]),
            "version": parts["version"],
            "filename": path.name,
            "python_tag": parts["python_tag"],
            "abi_tag": parts["abi_tag"],
            "platform_tag": parts["platform_tag"],
            "path": str(path)}


def _version_key(version: str):
    """Comparable key for dotted versions: numeric components sort
    numerically, the rest lexically (good enough for picking the
    newest wheel of one package)."""
    key = []
    for part in str(version).split("."):
        match = re.match(r"(\d+)(.*)", part)
        if match:
            key.append((0, int(match.group(1)), match.group(2)))
        else:
            key.append((1, 0, part))
    return tuple(key)


def select_wheelhouse_entry(entries, name: str, pytag: str) -> dict | None:
    """Pick the newest validated entry for ``name`` covering ``pytag``,
    or None when there is none. ``entries`` are
    validate_wheelhouse_entry dicts."""
    want = normalize_pkg_name(name)
    best = None
    for entry in entries:
        if entry["name"] != want:
            continue
        if not _wheel_matches_pytag(entry["python_tag"], entry["abi_tag"],
                                    entry["platform_tag"], pytag):
            continue
        if best is None or _version_key(entry["version"]) > _version_key(
                best["version"]):
            best = entry
    return best


def wheelhouse_archive_name(commit: str, pytag: str) -> str:
    return f"comfyui-wheelhouse-{commit}-{pytag}.tar.gz"


def build_wheelhouse_task_descriptor(commit: str, pytag: str,
                                     entries: list) -> dict:
    """Descriptor written as WHEELHOUSE_META.json inside the bundle
    (and mirrored in the local manifest). ``entries`` carry name,
    version, filename, sha256 and size_bytes; they are sorted by
    filename so the descriptor is deterministic."""
    # No wall-clock timestamp here: the descriptor lives inside the
    # bundle, and the bundle must rebuild byte-identically. (The
    # local publish manifest _wheelhouse_manifest carries created_utc.)
    return {
        "commit": commit,
        "python_tag": pytag,
        "archive_name": wheelhouse_archive_name(commit, pytag),
        "entries": sorted(
            ({"name": e["name"], "version": e["version"],
              "filename": e["filename"], "sha256": e["sha256"],
              "size_bytes": e["size_bytes"]} for e in entries),
            key=lambda e: e["filename"]),
        "source": "offline wheelhouse: local .whl files for the pinned "
                  "ComfyUI requirements (preinstalled packages excluded)",
        "cache_folder": CACHE_FOLDER_NAME,
        "cache_drive_dir": CACHE_DRIVE_DIR,
    }


def verify_local_wheelhouse(wheelhouse_dir, descriptor: dict) -> dict:
    """Independent local check of an extracted wheelhouse directory:
    every descriptor entry exists under ``wheelhouse_dir`` with
    matching SHA-256 and size, and no extra ``.whl`` file is present
    (the bundle must be exactly what the descriptor says). Raises
    RuntimeError on any mismatch. Returns a summary dict."""
    from drive_api import file_digests  # lazy: local digests only

    wheelhouse_dir = Path(wheelhouse_dir)
    entries = descriptor.get("entries", [])
    checked = []
    for entry in entries:
        wheel_path = wheelhouse_dir / entry["filename"]
        if not wheel_path.is_file():
            raise RuntimeError(
                "wheelhouse missing entry: " + entry["filename"])
        digests = file_digests(wheel_path)
        if digests["sha256"] != entry["sha256"]:
            raise RuntimeError(
                "wheelhouse SHA-256 mismatch: " + entry["filename"])
        if digests["size"] != int(entry["size_bytes"]):
            raise RuntimeError(
                "wheelhouse size mismatch: " + entry["filename"])
        checked.append(entry["filename"])
    on_disk = sorted(p.name for p in wheelhouse_dir.glob("*.whl"))
    extra = [name for name in on_disk if name not in checked]
    if extra:
        raise RuntimeError("wheelhouse has unlisted wheels: " + ", ".join(extra))
    return {"entries_checked": len(checked), "files": checked}


def build_wheelhouse(requirements_path, commit: str, pytag: str,
                     wheels_dir, out_path: Path) -> dict:
    """Build a deterministic wheelhouse bundle from a local wheels
    directory. Every filtered requirement must have a matching wheel
    for ``pytag``; a missing wheel fails closed with the missing names
    listed (never silently skipped). The tarball holds
    ``wheelhouse/*.whl`` (sorted) + ``wheelhouse/WHEELHOUSE_META.json``,
    all members mtime=0, gzip mtime=0, so rebuilding gives identical
    bytes. Returns ``{archive_name, sha256, size_bytes, entries}``."""
    from drive_api import file_digests  # lazy: local digests only

    pytag = normalize_python_tag(pytag)
    if pytag not in SUPPORTED_PYTHON_TAGS:
        raise ValueError(
            "unsupported python tag " + pytag + "; supported: "
            + ", ".join(SUPPORTED_PYTHON_TAGS))
    required = []
    for line in filter_requirements_file(requirements_path):
        name = _requirement_name(line)
        if name is not None and name not in required:
            required.append(name)
    wheels_dir = Path(wheels_dir)
    if not wheels_dir.is_dir():
        raise RuntimeError("wheels dir missing: " + str(wheels_dir))
    candidates = []
    for wheel_path in sorted(wheels_dir.glob("*.whl")):
        try:
            candidates.append(validate_wheelhouse_entry(wheel_path, pytag))
        except ValueError:
            # A wheel for another tag (or an unparsable name) is not
            # this bundle's business; a required package with no wheel
            # at all fails closed below, with its name listed.
            continue
    chosen = []
    missing = []
    for name in required:
        entry = select_wheelhouse_entry(candidates, name, pytag)
        if entry is None:
            missing.append(name)
        else:
            chosen.append(entry)
    if missing:
        raise RuntimeError(
            "no wheel for " + pytag + ": " + ", ".join(missing))
    entries = []
    for entry in chosen:
        digests = file_digests(Path(entry["path"]))
        entries.append({**entry, "sha256": digests["sha256"],
                        "size_bytes": digests["size"]})
    descriptor = build_wheelhouse_task_descriptor(commit, pytag, entries)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_bytes = (json.dumps(descriptor, indent=2) + "\n").encode("utf-8")

    def _member(name: str, data: bytes) -> tuple:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mode = 0o644
        return info, io.BytesIO(data)

    with tempfile.TemporaryDirectory() as d:
        tar_path = Path(d) / "wheelhouse.tar"
        with tarfile.open(tar_path, "w") as tf:
            for entry in sorted(entries, key=lambda e: e["filename"]):
                wheel_path = Path(entry["path"])
                info, _ = _member(
                    WHEELHOUSE_DIR_IN_ARCHIVE + "/" + entry["filename"],
                    b"")
                info.size = wheel_path.stat().st_size
                with wheel_path.open("rb") as fi:
                    tf.addfile(info, fi)
            info, bio = _member(
                WHEELHOUSE_DIR_IN_ARCHIVE + "/" + WHEELHOUSE_META_NAME,
                meta_bytes)
            tf.addfile(info, bio)
        with tar_path.open("rb") as fi, gzip.GzipFile(
                filename="", mode="wb", fileobj=out_path.open("wb"),
                compresslevel=9, mtime=0) as gz:
            for chunk in iter(lambda: fi.read(4 * 1024 * 1024), b""):
                gz.write(chunk)
    digests = file_digests(out_path)
    return {"archive_name": wheelhouse_archive_name(commit, pytag),
            "sha256": digests["sha256"], "size_bytes": digests["size"],
            "entries": len(entries)}


def _wheelhouse_manifest(commit: str, pytag: str, name: str, sha256: str,
                         size: int) -> dict:
    return {
        "commit": commit,
        "python_tag": pytag,
        "archive_name": name,
        "sha256": sha256,
        "size_bytes": size,
        "created_utc": datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "offline wheelhouse bundle for the pinned ComfyUI "
                  "requirements (preinstalled packages excluded)",
        "cache_folder": CACHE_FOLDER_NAME,
        "cache_drive_dir": CACHE_DRIVE_DIR,
    }


def publish_wheelhouse(requirements_path, commit: str, pytag: str,
                       wheels_dir, colab_auth: str) -> dict:
    """Build the wheelhouse bundle locally, verify it by extraction,
    upload the archive + .sha256 sidecar + manifest to the same Drive
    folder as the source cache, re-verify each from server metadata,
    and write the local manifest with Drive ids (0600). Never touches
    the model depot or READY."""
    from drive_api import file_digests, verify_uploaded_file
    from drive_depot import (
        account_matches,
        atomic_json,
        drive_service,
        ensure_folder,
        upload_one,
    )

    service = drive_service()
    if not account_matches(service, colab_auth):
        raise RuntimeError("ACCOUNT_MISMATCH")
    WHEELHOUSE_STAGING.mkdir(parents=True, exist_ok=True)
    name = wheelhouse_archive_name(
        commit, normalize_python_tag(pytag))
    archive_path = WHEELHOUSE_STAGING / name
    built = build_wheelhouse(requirements_path, commit, pytag, wheels_dir,
                             archive_path)
    digests = file_digests(archive_path)
    # Independent local verification: extract and check every wheel.
    with tempfile.TemporaryDirectory() as d:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(d, filter="data")
        descriptor = json.loads(
            (Path(d) / WHEELHOUSE_DIR_IN_ARCHIVE
             / WHEELHOUSE_META_NAME).read_text(encoding="utf-8"))
        verify_local_wheelhouse(
            Path(d) / WHEELHOUSE_DIR_IN_ARCHIVE, descriptor)
    sha_path = WHEELHOUSE_STAGING / (name + ".sha256")
    sha_path.write_text(built["sha256"] + "  " + name + "\n")
    manifest = _wheelhouse_manifest(commit, normalize_python_tag(pytag),
                                    name, built["sha256"], digests["size"])
    manifest_path = WHEELHOUSE_STAGING / "comfyui-wheelhouse-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    folder_id = ensure_folder(service, None, CACHE_FOLDER_NAME,
                              allow_write=True)
    entries = {}
    for local_path, rel in ((archive_path, name),
                            (sha_path, name + ".sha256"),
                            (manifest_path, "comfyui-wheelhouse-manifest.json")):
        entry = upload_one(service, local_path, folder_id, rel, rel,
                           file_digests(local_path)["sha256"], commit)
        entries[rel] = entry
    for rel, entry in entries.items():
        verify_uploaded_file(service, entry["drive_file_id"],
                             WHEELHOUSE_STAGING / rel)
    manifest["drive_folder_id"] = folder_id
    manifest["drive_files"] = {
        rel: {"id": e["drive_file_id"], "size": e["size"],
              "sha256": e["sha256"], "server_md5": e["server_md5"]}
        for rel, e in entries.items()
    }
    atomic_json(WHEELHOUSE_LOCAL_MANIFEST, manifest)
    os.chmod(WHEELHOUSE_LOCAL_MANIFEST, 0o600)
    return manifest


def verify_wheelhouse_on_drive(service, commit: str, pytag: str) -> dict:
    """Discover a commit-matching wheelhouse bundle in the Drive cache
    folder (the Muse equivalent of the upstream --sync-shared-caches
    discovery; Muse has no setup_isolated_account.py script). Returns
    ``{found, drive_file_id, name, size, server_md5}`` for the archive
    plus whether its .sha256 sidecar is present. No bytes are
    downloaded; full SHA-256 verification happens when the worker
    downloads the bundle. Raises when the cache folder is missing or
    the listing is ambiguous."""
    from drive_depot import ensure_folder, list_named

    pytag = normalize_python_tag(pytag)
    folder_id = ensure_folder(service, None, CACHE_FOLDER_NAME,
                              allow_write=False)
    name = wheelhouse_archive_name(commit, pytag)
    found = list_named(service, folder_id, name)
    if len(found) > 1:
        raise RuntimeError("duplicate wheelhouse bundle on Drive: " + name)
    if not found:
        return {"found": False, "name": name, "drive_file_id": None,
                "size": None, "server_md5": None,
                "sha256_sidecar_present": False}
    entry = found[0]
    sidecar = list_named(service, folder_id, name + ".sha256")
    return {"found": True, "name": name,
            "drive_file_id": entry["id"],
            "size": int(entry["size"]) if entry.get("size") else None,
            "server_md5": entry.get("md5Checksum"),
            "sha256_sidecar_present": len(sidecar) == 1}


def verify_wheelhouse(colab_auth: str) -> dict:
    """Read-only: re-check the published wheelhouse files against the
    local manifest (same pattern as verify() for the source cache)."""
    from drive_api import verify_uploaded_file
    from drive_depot import account_matches, drive_service

    if not WHEELHOUSE_LOCAL_MANIFEST.exists():
        raise RuntimeError(
            "local wheelhouse manifest missing: "
            + str(WHEELHOUSE_LOCAL_MANIFEST))
    manifest = json.loads(WHEELHOUSE_LOCAL_MANIFEST.read_text())
    service = drive_service()
    if not account_matches(service, colab_auth):
        raise RuntimeError("ACCOUNT_MISMATCH")
    result = {"folder_id": manifest.get("drive_folder_id"), "files": {}}
    for rel, info in manifest.get("drive_files", {}).items():
        proof = verify_uploaded_file(
            service, info["id"], WHEELHOUSE_STAGING / rel)
        result["files"][rel] = {
            "drive_file_id": info["id"], "size": proof["size"],
            "sha256": proof["sha256"], "server_verified": True}
    return result


def publish(repo_dir: Path, commit: str, colab_auth: str) -> dict:
    """Build the archive locally, create the Drive folder, upload the three
    files, re-verify each from server metadata, and write the local
    manifest with Drive ids. Never touches the model depot or READY."""
    from drive_api import file_digests, verify_uploaded_file
    from drive_depot import (
        account_matches,
        atomic_json,
        drive_service,
        ensure_folder,
        upload_one,
    )

    if not (repo_dir / ".git").exists():
        raise RuntimeError("repo dir has no .git: " + str(repo_dir))
    head = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    if head != commit:
        raise RuntimeError("repo HEAD " + head + " != pinned commit " + commit)
    service = drive_service()
    if not account_matches(service, colab_auth):
        raise RuntimeError("ACCOUNT_MISMATCH")
    STAGING.mkdir(parents=True, exist_ok=True)
    name = archive_name(commit)
    archive_path = STAGING / name
    sha256 = build_archive(repo_dir, commit, archive_path)
    digests = file_digests(archive_path)
    verify_local_archive(repo_dir, commit, archive_path, sha256)
    sha_path = STAGING / (name + ".sha256")
    sha_path.write_text(sha256 + "  " + name + "\n")
    manifest = build_manifest(COMFYUI_REPO_URL, commit, name, sha256,
                              digests["size"])
    manifest_path = STAGING / "comfyui-source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    folder_id = ensure_folder(service, None, CACHE_FOLDER_NAME,
                              allow_write=True)
    entries = {}
    for local_path, rel in ((archive_path, name),
                            (sha_path, name + ".sha256"),
                            (manifest_path, "comfyui-source-manifest.json")):
        entry = upload_one(service, local_path, folder_id, rel, rel,
                           file_digests(local_path)["sha256"], commit)
        entries[rel] = entry
    # Independent re-verification from server metadata for every file.
    for rel, entry in entries.items():
        verify_uploaded_file(service, entry["drive_file_id"],
                             STAGING / rel)
    manifest["drive_folder_id"] = folder_id
    manifest["drive_files"] = {
        rel: {"id": e["drive_file_id"], "size": e["size"],
              "sha256": e["sha256"], "server_md5": e["server_md5"]}
        for rel, e in entries.items()
    }
    atomic_json(LOCAL_MANIFEST, manifest)
    os.chmod(LOCAL_MANIFEST, 0o600)
    return manifest


def verify(colab_auth: str) -> dict:
    """Read-only: re-check the Drive cache files against the local manifest."""
    from drive_api import verify_uploaded_file
    from drive_depot import account_matches, drive_service

    if not LOCAL_MANIFEST.exists():
        raise RuntimeError("local manifest missing: " + str(LOCAL_MANIFEST))
    manifest = json.loads(LOCAL_MANIFEST.read_text())
    service = drive_service()
    if not account_matches(service, colab_auth):
        raise RuntimeError("ACCOUNT_MISMATCH")
    result = {"folder_id": manifest.get("drive_folder_id"), "files": {}}
    for rel, info in manifest.get("drive_files", {}).items():
        proof = verify_uploaded_file(
            service, info["id"], STAGING / rel)
        result["files"][rel] = {
            "drive_file_id": info["id"], "size": proof["size"],
            "sha256": proof["sha256"], "server_verified": True}
    return result


def cli() -> int:
    p = argparse.ArgumentParser(description="Pinned ComfyUI source cache")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--repo", required=True)
    b.add_argument("--commit", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--repo", required=True)
    pub.add_argument("--commit", required=True)
    pub.add_argument("--colab-auth", required=True)
    pub.add_argument("--allow-drive-write", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--colab-auth", required=True)
    bw = sub.add_parser("build-wheelhouse")
    bw.add_argument("--requirements", required=True)
    bw.add_argument("--commit", required=True)
    bw.add_argument("--pytag", required=True)
    bw.add_argument("--wheels-dir", required=True)
    bw.add_argument("--out", required=False, default=None)
    pw = sub.add_parser("publish-wheelhouse")
    pw.add_argument("--requirements", required=True)
    pw.add_argument("--commit", required=True)
    pw.add_argument("--pytag", required=True)
    pw.add_argument("--wheels-dir", required=True)
    pw.add_argument("--colab-auth", required=True)
    pw.add_argument("--allow-drive-write", action="store_true")
    vw = sub.add_parser("verify-wheelhouse")
    vw.add_argument("--colab-auth", required=True)
    args = p.parse_args()
    if args.cmd == "build":
        from drive_api import file_digests  # lazy: local digests only

        repo = Path(args.repo)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()
        if head != args.commit:
            raise RuntimeError("repo HEAD " + head + " != pinned commit " + args.commit)
        STAGING.mkdir(parents=True, exist_ok=True)
        name = archive_name(args.commit)
        archive_path = STAGING / name
        sha256 = build_archive(repo, args.commit, archive_path)
        digests = file_digests(archive_path)
        checked = verify_local_archive(repo, args.commit, archive_path, sha256)
        (STAGING / (name + ".sha256")).write_text(sha256 + "  " + name + "\n")
        manifest = build_manifest(COMFYUI_REPO_URL, args.commit, name,
                                  sha256, digests["size"])
        (STAGING / "comfyui-source-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n")
        print(json.dumps({"archive": name, "sha256": sha256,
                          "size_bytes": digests["size"],
                          "tree": checked["tree"],
                          "files_checked": checked["files_checked"]},
                         indent=2))
        return 0
    if args.cmd == "publish":
        if not args.allow_drive_write:
            p.error("Mutating Drive requires --allow-drive-write")
        manifest = publish(Path(args.repo), args.commit, args.colab_auth)
        print(json.dumps({"folder": manifest["cache_folder"],
                          "drive_folder_id": manifest["drive_folder_id"],
                          "archive": manifest["archive_name"],
                          "sha256": manifest["sha256"],
                          "size_bytes": manifest["size_bytes"],
                          "local_manifest": str(LOCAL_MANIFEST)},
                         indent=2))
        return 0
    if args.cmd == "verify":
        result = verify(args.colab_auth)
        print(json.dumps(result, indent=2))
        return 0
    if args.cmd == "build-wheelhouse":
        out = Path(args.out) if args.out else (
            WHEELHOUSE_STAGING
            / wheelhouse_archive_name(args.commit,
                                      normalize_python_tag(args.pytag)))
        built = build_wheelhouse(Path(args.requirements), args.commit,
                                 args.pytag, Path(args.wheels_dir), out)
        print(json.dumps({"archive": built["archive_name"],
                          "sha256": built["sha256"],
                          "size_bytes": built["size_bytes"],
                          "entries": built["entries"],
                          "path": str(out)}, indent=2))
        return 0
    if args.cmd == "publish-wheelhouse":
        if not args.allow_drive_write:
            p.error("Mutating Drive requires --allow-drive-write")
        manifest = publish_wheelhouse(
            Path(args.requirements), args.commit, args.pytag,
            Path(args.wheels_dir), args.colab_auth)
        print(json.dumps({"folder": manifest["cache_folder"],
                          "drive_folder_id": manifest["drive_folder_id"],
                          "archive": manifest["archive_name"],
                          "sha256": manifest["sha256"],
                          "size_bytes": manifest["size_bytes"],
                          "local_manifest": str(WHEELHOUSE_LOCAL_MANIFEST)},
                         indent=2))
        return 0
    if args.cmd == "verify-wheelhouse":
        result = verify_wheelhouse(args.colab_auth)
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())

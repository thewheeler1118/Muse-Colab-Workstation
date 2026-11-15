"""Recompute SOURCE_MANIFEST.json hashes and report drift.

Stdlib only. Run from the repo root:
    python3 scripts/verify_manifest.py

Exits 0 only when:
- every file listed in SOURCE_MANIFEST.json exists with a matching
  SHA-256 and size, and is not a sensitive path or a symlink;
- every git-tracked file (except SOURCE_MANIFEST.json itself and
  bytecode) is listed in the manifest;
- no tracked file is a sensitive path (a protected path that slipped
  into the index fails even if it is not in the manifest).

File range and sensitive-path rules are shared with
scripts/regenerate_manifest.py via scripts/manifest_policy.py.

Non-git directory (e.g. the production dir): verifies only the files
listed in the manifest and says so explicitly. It never scans the
filesystem to rebuild the tracked set.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from manifest_policy import (  # noqa: E402
    MANIFEST_NAME,
    is_bytecode,
    is_sensitive,
    sha256_file,
    tracked_files,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / MANIFEST_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT),
                        help="repo root (default: parent of this script's repo)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    manifest = root / MANIFEST_NAME

    if not manifest.is_file():
        print("SOURCE_MANIFEST.json missing", file=sys.stderr)
        return 2
    data = json.loads(manifest.read_text())
    entries = data["files"]
    errors = []
    seen = set()
    for e in entries:
        rel = e["path"]
        seen.add(rel)
        category = is_sensitive(rel)
        if category is not None:
            errors.append(f"SENSITIVE PATH IN MANIFEST: {rel} ({category})")
            continue
        p = root / rel
        if p.is_symlink():
            errors.append(f"SYMLINK IN MANIFEST: {rel}")
            continue
        if not p.is_file():
            errors.append(f"MISSING: {rel}")
            continue
        actual_size = p.stat().st_size
        actual_sha = sha256_file(p)
        if actual_size != e["size_bytes"]:
            errors.append(f"SIZE MISMATCH: {rel} manifest={e['size_bytes']} actual={actual_size}")
        if actual_sha != e["sha256"]:
            errors.append(f"SHA MISMATCH: {rel}")

    tracked = tracked_files(root)
    if tracked is None:
        print("NOTE: not a git worktree -- verifying listed files only "
              "(unlisted-file check skipped; do not rebuild a manifest here).")
    else:
        for rel in sorted(tracked):
            if rel == MANIFEST_NAME or is_bytecode(rel):
                continue
            category = is_sensitive(rel)
            if category is not None:
                errors.append(f"SENSITIVE PATH TRACKED: {rel} ({category})")
                continue
            if rel not in seen:
                errors.append(f"UNLISTED FILE (tracked but not in manifest): {rel}")

    if errors:
        print(f"MANIFEST VERIFY FAILED ({len(errors)} problem(s), {len(entries)} entries):")
        for e in errors:
            print("  " + e)
        return 1
    print(f"MANIFEST OK: {len(entries)} files, all sizes and SHA-256 match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

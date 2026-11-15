"""Regenerate SOURCE_MANIFEST.json deterministically from git-tracked files.

Stdlib only. Run from the repo root after any tracked-file change:

    python3 scripts/regenerate_manifest.py
    python3 scripts/verify_manifest.py

Rules (mirror scripts/verify_manifest.py; shared policy in
scripts/manifest_policy.py):
- The manifest lists git-TRACKED files only (``git ls-files --cached``),
  sorted by path, with size_bytes and SHA-256. Untracked or ignored
  runtime state -- virtualenvs, credentials, real task specs, logs,
  runs, output, model cache -- can never enter the manifest.
- New files must be staged first (``git add`` the files this change
  really needs to commit); the generator never scans the worktree to
  find them. Do NOT restore a recursive worktree scan to include them.
- The manifest never lists itself (no self-hash cycle).
- A tracked sensitive path (or a symlink) REFUSES generation: the
  manifest is left unwritten and the exit code is non-zero. The error
  names only the relative path and a risk category, never contents.
- `role` is preserved from the previous manifest for paths it already
  knew; new paths get a role from ROLE_DEFAULTS (edit the table when a
  genuinely new kind of file appears -- do not hand-edit hashes).
- `generated_utc` is today's UTC date; `source` / `note` are preserved
  from the previous manifest (update them deliberately, in the same
  commit, when the baseline they describe actually changes).

In a non-git directory the generator refuses outright: it must never
fall back to scanning the filesystem to build a manifest.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from manifest_policy import (  # noqa: E402
    MANIFEST_NAME,
    SensitivePathError,
    check_tracked_entry,
    is_bytecode,
    sha256_file,
    tracked_files,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / MANIFEST_NAME

ROLE_DEFAULTS = [
    (".github/", "CI / templates"),
    ("patches/", "Vendored CLI patch"),
    ("tests/fixtures/", "Test fixture (pristine upstream source, read-only)"),
    ("tests/", "Tests"),
    ("scripts/", "Operator wrapper / offline check"),
    ("docs/", "Documentation / evidence"),
    ("config/", "Configuration (mirrors code constants; asserted by tests)"),
    ("requirements-ci.txt", "CI-only offline test dependencies (pins from requirements.lock)"),
    ("requirements-local.txt", "Direct dependency floors (local/CPU env only)"),
    ("requirements.lock", "Pinned dependencies (verified source venv)"),
]


def role_for(rel: str, old_roles: dict) -> str:
    if rel in old_roles:
        return old_roles[rel]
    for prefix, role in ROLE_DEFAULTS:
        if rel == prefix or rel.startswith(prefix):
            return role
    return "Supporting file"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT),
                        help="repo root (default: parent of this script's repo)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    manifest = root / MANIFEST_NAME

    tracked = tracked_files(root)
    if tracked is None:
        print(f"MANIFEST REFUSED: {root} is not a git worktree; "
              "refusing to scan the filesystem to build a manifest.",
              file=sys.stderr)
        return 2
    old = {}
    header = {}
    if manifest.is_file():
        data = json.loads(manifest.read_text())
        old = {e["path"]: e["role"] for e in data.get("files", [])}
        header = {k: v for k, v in data.items() if k != "files"}
    header.setdefault("source", "~/workspace/Muse_Colab_H3_Drive_v3/ (production baseline)")
    header.setdefault(
        "note",
        "Git-tracked files only (git ls-files --cached). Regenerate after any change; "
        "verify with scripts/verify_manifest.py. Excluded by policy and therefore absent: "
        "model weights, poster binary, MP4s, live ledgers/sessions/depot state, virtualenvs, "
        "credentials, real task specs, __pycache__. Tracked sensitive paths refuse generation.",
    )
    header["generated_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    files = []
    for rel in sorted(tracked):
        if rel == MANIFEST_NAME or is_bytecode(rel):
            continue
        try:
            p = check_tracked_entry(root, rel)
        except SensitivePathError as exc:
            print(f"MANIFEST REFUSED: {exc.rel} ({exc.category}) -- "
                  "tracked sensitive path; remove it from the index before regenerating.",
                  file=sys.stderr)
            return 1
        if not p.is_file():
            print(f"MANIFEST REFUSED: tracked file missing on disk: {rel}",
                  file=sys.stderr)
            return 1
        files.append(
            {
                "path": rel,
                "size_bytes": p.stat().st_size,
                "sha256": sha256_file(p),
                "role": role_for(rel, old),
            }
        )
    out = {**header, "files": files}
    manifest.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"MANIFEST REGENERATED: {len(files)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

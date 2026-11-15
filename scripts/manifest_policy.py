"""Shared manifest policy: git-tracked file list + sensitive-path deny gate.

Stdlib only. Used by scripts/regenerate_manifest.py and
scripts/verify_manifest.py so both tools enforce the SAME file range and
the SAME sensitive-path rules (a drift between them would be a leak).

Policy (TASK-SECURITY-009 P0-A):
- The manifest covers git-TRACKED files only (``git ls-files --cached``),
  never the whole worktree. Untracked or ignored runtime state
  (virtualenvs, credentials, real task specs, logs, runs, output) can
  never enter the manifest, even if present on disk.
- Sensitive paths are REFUSED, not listed, even if force-added to the
  index: a tracked sensitive file fails generation and verification.
  .gitignore alone is not trusted -- the deny gate is independent.
- Symlinks are refused: the generator never follows a link out of the
  repo to read an external file.
- Error output names only the relative path and a risk category, never
  file contents.

The deny gate is path-based and conservative on purpose: a false
positive (a legit file refused) is a loud, fixable failure; a false
negative is a leak into a public repo.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

# (path prefix, risk category). A tracked path equal to the prefix or
# under it is refused. model_cache/ is refused except for the one
# sanitized example file that is meant to be public.
SENSITIVE_PREFIXES = [
    (".venv/", "private-venv"),
    (".venv-colab-cli/", "private-venv"),
    ("venv/", "private-venv"),
    (".secrets/", "credentials-dir"),
    (".gcloud-colab/", "credentials-dir"),
    ("config/tasks/", "real-task-spec"),
    ("logs/", "runtime-state"),
    ("runs/", "runtime-state"),
    ("output/", "runtime-state"),
    ("outputs/", "runtime-state"),
    ("artifacts/", "runtime-state"),
    ("model_cache/", "model-cache"),
]

MODEL_CACHE_ALLOWED = {"model_cache/verified_manifest.example.json"}

# Credential-like basenames (same shape as scripts/secret_scan.py).
CREDENTIAL_BASENAME = re.compile(
    r"(?i)^(credentials?|tokens?|oauth|application_default_credentials)([._-]|$)"
)
CREDENTIAL_SUFFIXES = {".pem", ".key", ".p12"}

MANIFEST_NAME = "SOURCE_MANIFEST.json"


def is_sensitive(rel: str) -> str | None:
    """Return the risk category for a repo-relative path, or None.

    Only the path is inspected; file contents are never read here.
    """
    rel = rel.replace("\\", "/")
    if rel in MODEL_CACHE_ALLOWED:
        return None
    for prefix, category in SENSITIVE_PREFIXES:
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return category
    name = rel.rsplit("/", 1)[-1]
    if CREDENTIAL_BASENAME.match(name):
        return "credential-filename"
    if Path(name).suffix in CREDENTIAL_SUFFIXES:
        return "credential-filename"
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return "credential-filename"
    return None


def tracked_files(root: Path) -> list[str] | None:
    """Git-tracked files (index, so staged new files count), or None.

    Returns None when `root` is not a git worktree or git fails. Never
    falls back to a worktree scan -- that fallback was the leak.
    """
    try:
        r = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=root, capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError):
        return None
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split("\0") if p]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


class SensitivePathError(Exception):
    """A tracked path the manifest must never contain."""

    def __init__(self, rel: str, category: str):
        super().__init__(f"{rel} ({category})")
        self.rel = rel
        self.category = category


def check_tracked_entry(root: Path, rel: str) -> Path:
    """Validate one tracked path for manifest inclusion.

    Raises SensitivePathError for sensitive paths and for symlinks
    (never follow a link out of the repo). Returns the on-disk Path.
    """
    category = is_sensitive(rel)
    if category is not None:
        raise SensitivePathError(rel, category)
    p = root / rel
    if p.is_symlink():
        raise SensitivePathError(rel, "symlink")
    return p


def is_bytecode(rel: str) -> bool:
    """Bytecode is never manifest content (determinism, not a leak)."""
    return "__pycache__" in Path(rel).parts or rel.endswith(".pyc")

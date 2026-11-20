"""Path location for the Muse adapter.

Rules (TASK-VERSION-ROADMAP-011 Stage 1):

- The adapter locates the repo root (this checkout), the production
  directory, and external task-spec files. It never writes into the
  production directory and never treats a path inside the repo as a
  production path.
- Real task specs live outside the repo. ``config/tasks/`` inside the
  repo is denied by ``scripts/manifest_policy.py`` and is refused
  here too: a task-spec path under the repo's ``config/tasks/`` is
  rejected, even if the file exists.
- Symlinks are refused: a task-spec path must resolve inside the
  directory the user explicitly named, never escape it via a link.
- No credential, ledger, log, or run-manifest path is ever returned
  for reading by callers outside the adapter; this module only
  resolves repo root, production dir (read-only locate), and
  task-spec files.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo-relative paths that must never hold a real task spec.
_DENIED_TASK_SPEC_PREFIXES = ("config/tasks/",)

# Directory names that are never a production directory.
_PRODUCTION_DIR_DENY_NAMES = {".git", ".venv", ".venv-colab-cli", "model_cache"}


def locate_repo_root(start: Path | str | None = None) -> Path:
    """Return the repo root (the directory holding ``h3_controller.py``).

    Walks up from *start* (default: this file's directory). Raises
    ``FileNotFoundError`` if no ancestor holds the production core.
    """
    current = Path(start).resolve() if start is not None else Path(__file__).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "h3_controller.py").is_file() and (
            candidate / "pyproject.toml"
        ).is_file():
            return candidate
    raise FileNotFoundError("repo root (h3_controller.py + pyproject.toml) not found")


def resolve_production_dir(explicit: Path | str | None = None) -> Path:
    """Locate the production directory for read-only status checks.

    The production directory is only ever located from an explicit,
    user-supplied path. There is no default guess: guessing would let
    the adapter silently pick the wrong live directory. The directory
    must exist and must contain ``RELEASE_COMMIT``; the adapter never
    writes to it.
    """
    if explicit is None:
        raise ValueError(
            "production directory must be supplied explicitly; "
            "the adapter never guesses a live production path"
        )
    path = Path(explicit).expanduser()
    if not path.is_absolute():
        raise ValueError(f"production directory must be absolute: {explicit!r}")
    if path.name in _PRODUCTION_DIR_DENY_NAMES:
        raise ValueError(f"not a production directory: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"production directory not found: {path}")
    if not (path / "RELEASE_COMMIT").is_file():
        raise FileNotFoundError(
            f"production directory has no RELEASE_COMMIT: {path}"
        )
    return path.resolve()


def _refuse_if_under_denied(path: Path, root_resolved: Path,
                            addressed_as) -> None:
    """Raise if *path* lies under one of the denied repo prefixes."""
    try:
        rel = path.relative_to(root_resolved)
    except ValueError:
        return
    rel_posix = rel.as_posix()
    for prefix in _DENIED_TASK_SPEC_PREFIXES:
        if rel_posix == prefix.rstrip("/") or rel_posix.startswith(prefix):
            raise ValueError(
                "real task specs are denied under repo "
                f"config/tasks/: {addressed_as}"
            )


def locate_task_spec_file(path: Path | str, repo_root: Path | str | None = None) -> Path:
    """Validate and resolve an external task-spec JSON path.

    The file must exist, must be a regular file (not a symlink), must
    end in ``.json``, and must not live under the repo's
    ``config/tasks/`` (real task data is denied there by manifest
    policy). Returns the resolved absolute path.

    The repo root is always determined — ``repo_root`` when given,
    otherwise this checkout (``locate_repo_root()`` with no argument).
    There is no "no root" mode: skipping the root also skipped the
    ``config/tasks/`` denial on the ``to_wrapper_args()`` call path,
    which is how a real task file could have been accepted.

    The denial is checked on BOTH the resolved path and the
    as-addressed path (lexically normalized, symlinks NOT resolved).
    That closes the parent-directory symlink bypass in both
    directions: a ``config/tasks/`` component that is a symlink to
    somewhere outside the repo (resolved path escapes the denial),
    and a path addressed outside the repo whose parent is a symlink
    into ``config/tasks/`` (resolved path lands inside it).
    """
    root = locate_repo_root(repo_root)
    root_resolved = Path(root).resolve()
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"task-spec path must not be a symlink: {path}")
    if candidate.suffix.lower() != ".json":
        raise ValueError(f"task-spec path must end in .json: {path}")
    resolved = candidate.resolve()
    _refuse_if_under_denied(resolved, root_resolved, path)
    # As-addressed path: os.path.abspath normalizes "." / ".."
    # lexically without following symlinks, so a symlinked parent
    # cannot smuggle a config/tasks/ address past the check.
    addressed = Path(os.path.abspath(candidate))
    _refuse_if_under_denied(addressed, root_resolved, path)
    if not resolved.is_file():
        raise FileNotFoundError(f"task-spec file not found: {path}")
    if resolved.is_symlink():
        raise ValueError(f"task-spec path must not resolve via a symlink: {path}")
    return resolved

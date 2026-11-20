"""Remote Stage-2 bundled bootstrap extractor & worker launcher (v0.3.0 M2).

Executed via a single `colab exec -f` call after Stage 1 passes and
`/content/muse_h3/bootstrap_stage2.tar.gz` has been uploaded (the
controller's `launch_worker(session, bundled=True)` path). The bundle
carries `h3_job_state.py`, `h3_remote_worker.py`,
`h3_worker_launcher.py` and `task.json`. After extraction this module
invokes `h3_worker_launcher.main()`, which prints
`MUSE_H3_LAUNCH_JSON:...`.

Security invariants (same discipline as the F3 wheelhouse extractor;
the twin copy lives in `h3_bootstrap_stage1.py` and both must stay
in sync):
- Every tar member is validated BEFORE anything is extracted:
  absolute paths, `..` traversal, symlinks, hardlinks, device nodes,
  and any member resolving outside the destination are rejected.
- Total extracted bytes are capped; an oversize bundle fails closed.
- Mode `0o600` is enforced on every extracted `*token*.json` file.
- The bundle tar is unlinked in `finally`, even on failure.
- Token material is never printed or logged.
"""

from __future__ import annotations

import os
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path("/content/muse_h3")
DEFAULT_TAR_PATH = ROOT / "bootstrap_stage2.tar.gz"

# The bundle holds scripts and task.json only — never model weights.
# Anything larger is a corrupt or hostile bundle.
_MAX_EXTRACT_BYTES = 64 * 1024 * 1024


def _is_token_file(name: str) -> bool:
    lower = name.lower()
    return "token" in lower and lower.endswith(".json")


def safe_extract_tar(tar_path: Path, dest_dir: Path) -> list[str]:
    """Safely extract regular files and directories from ``tar_path``
    into ``dest_dir``. All members are validated first; on any unsafe
    member nothing is extracted and ValueError is raised."""
    dest_resolved = dest_dir.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        members = tf.getmembers()
        total = 0
        for member in members:
            raw_name = member.name or ""
            posix = PurePosixPath(raw_name)
            if not raw_name or raw_name.startswith("/") or posix.is_absolute():
                raise ValueError(f"Unsafe tar member (absolute path): {raw_name!r}")
            if any(part == ".." for part in posix.parts):
                raise ValueError(f"Unsafe tar member (path traversal): {raw_name!r}")
            if member.issym() or member.islnk():
                raise ValueError(f"Unsafe tar member (symlink/hardlink): {raw_name!r}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Unsafe tar member (unsupported type): {raw_name!r}")
            target = (dest_resolved / Path(*posix.parts)).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"Unsafe tar member (escapes destination): {raw_name!r}")
            if member.isfile():
                total += member.size
                if total > _MAX_EXTRACT_BYTES:
                    raise ValueError(
                        f"Unsafe tar bundle (extracted bytes exceed {_MAX_EXTRACT_BYTES})")
        extracted: list[str] = []
        for member in members:
            posix = PurePosixPath(member.name)
            target = dest_resolved / Path(*posix.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                raise ValueError(f"Cannot read tar member: {member.name!r}")
            data = src.read()
            if _is_token_file(target.name):
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                os.chmod(target, 0o600)
            else:
                target.write_bytes(data)
            extracted.append(str(posix))
    return extracted


def main(root: Path | None = None, tar_path: Path | None = None) -> None:
    target_root = Path(root) if root is not None else ROOT
    bundle_path = Path(tar_path) if tar_path is not None else (target_root / "bootstrap_stage2.tar.gz")
    target_root.mkdir(parents=True, exist_ok=True)
    (target_root / "output").mkdir(parents=True, exist_ok=True)
    try:
        if not bundle_path.is_file():
            raise FileNotFoundError(f"Stage 2 bundle missing: {bundle_path}")
        safe_extract_tar(bundle_path, target_root)
    finally:
        try:
            if bundle_path.exists():
                bundle_path.unlink()
        except OSError:
            pass
    if str(target_root) not in sys.path:
        sys.path.insert(0, str(target_root))
    import h3_worker_launcher

    h3_worker_launcher.ROOT = target_root
    h3_worker_launcher.main()


if __name__ == "__main__":
    main()

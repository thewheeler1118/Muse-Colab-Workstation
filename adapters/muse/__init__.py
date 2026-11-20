"""Muse adapter for Muse-Colab-Workstation.

Thin, Muse-original adaptation layer over the shared H3 production
core. It never calls bare ``colab new``, never reimplements the GPU
budget ledger, the Drive upload protocol, or the shutdown executor,
and never bypasses the controller's production authorization checks.
All enforcement lives in the shell wrapper
(``scripts/run_h3_task.sh``), the Python controller
(``h3_controller.py``), and file permissions — not in a copied
upstream hook file (see ``adapters/antigravity/COMPATIBILITY.md``).
"""
from __future__ import annotations

from .adapter import (
    Authorization,
    build_task_spec,
    check_existing_task_state,
    status_report,
    to_wrapper_args,
)
from .paths import locate_repo_root, locate_task_spec_file, resolve_production_dir
from .redact import redact_text

__all__ = [
    "Authorization",
    "build_task_spec",
    "check_existing_task_state",
    "locate_repo_root",
    "locate_task_spec_file",
    "redact_text",
    "resolve_production_dir",
    "status_report",
    "to_wrapper_args",
]

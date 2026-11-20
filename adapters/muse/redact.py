"""Credential redaction for Muse adapter logs and status reports.

Stdlib only. The adapter records approvals and audit results, but
never writes a credential value to a log, a status report, or a
Git-tracked file. Redaction is deny-by-pattern: any substring that
looks like a credential is replaced, and the replacement never echoes
the matched value (not even partially).

Patterns mirror ``scripts/secret_scan.py`` (same shapes, same
intent); this module is the runtime counterpart to that static scan.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

_PATTERNS = [
    # Google OAuth access token.
    re.compile(r"ya29\.[A-Za-z0-9_\-.]{10,}"),
    # GitHub personal access tokens (classic + fine-grained).
    re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    # PEM private-key blocks (whole block, including headers).
    re.compile(
        r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"
        r".*?-----END (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Generic bearer / API-key assignments: `key = <long token>`.
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|passwd)\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9_\-./+]{8,}[\"']?"
    ),
    # Long base64-ish token-looking strings after `token`/`auth` words.
    re.compile(r"(?i)(token|auth)[\"'\s:=]+[A-Za-z0-9_\-./+]{20,}"),
]


def redact_text(text: str) -> str:
    """Return *text* with credential-shaped substrings replaced.

    The function is total (never raises on odd input) and idempotent:
    redacting an already-redacted string changes nothing.
    """
    if not isinstance(text, str):
        text = str(text)
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def redact_mapping(mapping: dict) -> dict:
    """Redact every string value in a flat dict (keys are kept)."""
    return {
        key: (redact_text(value) if isinstance(value, str) else value)
        for key, value in mapping.items()
    }

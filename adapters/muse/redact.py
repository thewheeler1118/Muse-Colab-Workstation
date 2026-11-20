"""Credential redaction for Muse adapter logs and status reports.

Stdlib only. The adapter records approvals and audit results, but
never writes a credential value to a log, a status report, or a
Git-tracked file. Redaction is deny-by-pattern AND deny-by-key:

- ``redact_text`` replaces any credential-shaped substring (the
  replacement never echoes the matched value, not even partially);
- ``redact_value`` walks strings, dicts, and lists recursively, so a
  token nested inside ``shutdown`` or ``budget`` detail is redacted
  exactly like one in a top-level note;
- a value under a credential-named key (``token``, ``secret``,
  ``password``, ``api_key``, ``authorization``, ...) is replaced
  wholesale — pattern matching alone cannot catch a short password
  or an unfamiliar token format, and a key that says "token" is
  enough to treat the value as one.

``token_id`` is exempt from key-based redaction: it is the first 16
hex of SHA-256(token), a non-sensitive identifier the production
core already logs everywhere. Patterns mirror
``scripts/secret_scan.py`` (same shapes, same intent); this module
is the runtime counterpart to that static scan.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Words that mark a dict key as credential-bearing. Matched
# case-insensitively against the whole key. "author" is excluded in
# _is_sensitive_key: it contains "auth" but is not a credential.
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|passwd|password|credential|private[_-]?key|"
    r"api[_-]?key|access[_-]?key|session[_-]?key|auth|bearer)",
    re.IGNORECASE,
)
_MAX_DEPTH = 8

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
    """Redact every value in a dict (keys are kept).

    Values are deep-redacted: nested dicts and lists are walked, and
    a value under a credential-named key is replaced wholesale.
    """
    return {key: redact_value(value) for key, value in mapping.items()}


def _is_sensitive_key(key) -> bool:
    """True when a dict key names a credential (case-insensitive).

    ``token_id`` is the one exemption: a truncated hash of a token,
    not the token itself, and safe to show by design.
    """
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered == "token_id":
        return False
    if "author" in lowered:
        return False
    return bool(_SENSITIVE_KEY_RE.search(lowered))


def redact_value(value, _depth: int = 0):
    """Recursively redact a value of any shape.

    - str: credential-shaped substrings replaced (``redact_text``);
    - dict: every value redacted; a value under a credential-named
      key is replaced wholesale, without inspecting it;
    - list / tuple: each element redacted (type preserved);
    - anything else (numbers, bool, None): returned unchanged.

    Depth is capped (``_MAX_DEPTH``): anything deeper is replaced
    wholesale. The cap also bounds self-referential structures,
    which would otherwise recurse forever. The function is total
    (never raises on odd input) and idempotent.
    """
    if _depth > _MAX_DEPTH:
        return _REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_sensitive_key(key)
                  else redact_value(item, _depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [redact_value(item, _depth + 1) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value

"""Redaction and hashing helpers for trace tool arguments."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "password",
    "credential",
    "secret",
)

# "token" is handled separately from SENSITIVE_KEY_PARTS, not folded into it
# as a plain substring. A blind substring match on "token" would also catch
# legitimate operational metrics like "token_usage"/"token_count" (see
# ResourceUsage.token_usage in schema/trace.py), which are not secrets and
# must remain raw for later phases (Phase 6 feature engineering needs the
# actual numeric value). Credential-shaped token keys ("token",
# "access_token", "session_token", "bearer_token", "id_token", ...) all have
# "token" as the *trailing* "_"-separated segment; metric-shaped keys
# ("token_usage", "token_count") have "token" as a *leading* segment
# followed by a metric suffix. Matching on the trailing segment distinguishes
# the two reliably. See Phase 4 patch note in docs/phase4_completion.md for
# the leak (bare/compound token fields were unredacted) this closes.


def _has_sensitive_token_segment(normalized: str) -> bool:
    segments = normalized.split("_")
    return segments[-1] == "token"


def is_sensitive_key(key: str) -> bool:
    """Return whether a tool-argument key is too sensitive to store raw."""

    normalized = key.lower().replace("-", "_").replace(" ", "_")
    if any(part in normalized for part in SENSITIVE_KEY_PARTS):
        return True
    return _has_sensitive_token_segment(normalized)


def hash_sensitive_value(value: Any, salt: str = "agentsentinel") -> str:
    """Return a deterministic one-way hash for a sensitive argument value."""

    payload = f"{salt}:{repr(value)}".encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def redact_mapping(
    payload: Mapping[str, Any],
    *,
    salt: str = "agentsentinel",
) -> tuple[dict[str, Any], bool]:
    """Return a copy with sensitive values hashed and a redaction flag.

    The shape of the argument mapping is preserved so downstream policy and
    audit code can still reason about which argument keys were present without
    storing raw secrets.
    """

    redacted = False
    clean: dict[str, Any] = {}

    for key, value in payload.items():
        if is_sensitive_key(key):
            clean[key] = hash_sensitive_value(value, salt=salt)
            redacted = True
        elif isinstance(value, Mapping):
            nested, nested_redacted = redact_mapping(value, salt=salt)
            clean[key] = nested
            redacted = redacted or nested_redacted
        elif isinstance(value, list):
            clean_list, list_redacted = _redact_list(value, salt=salt)
            clean[key] = clean_list
            redacted = redacted or list_redacted
        else:
            clean[key] = value

    return clean, redacted


def _redact_list(values: list[Any], *, salt: str) -> tuple[list[Any], bool]:
    redacted = False
    clean: list[Any] = []

    for value in values:
        if isinstance(value, Mapping):
            nested, nested_redacted = redact_mapping(value, salt=salt)
            clean.append(nested)
            redacted = redacted or nested_redacted
        elif isinstance(value, list):
            nested_list, nested_redacted = _redact_list(value, salt=salt)
            clean.append(nested_list)
            redacted = redacted or nested_redacted
        else:
            clean.append(value)

    return clean, redacted

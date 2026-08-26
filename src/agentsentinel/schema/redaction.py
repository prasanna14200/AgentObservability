"""Redaction and hashing helpers for trace tool arguments."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "refresh_token",
    "password",
    "credential",
    "secret",
)


def is_sensitive_key(key: str) -> bool:
    """Return whether a tool-argument key is too sensitive to store raw."""

    normalized = key.lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


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

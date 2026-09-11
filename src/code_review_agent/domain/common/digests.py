"""Canonical JSON serialization and SHA-256 content digests."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


def _validate_json(value: Any, *, path: str = "$") -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path.rstrip()}")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"JSON object key at {path.rstrip()} must be a string")
            normalized[key] = _validate_json(item, path=f"{path}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_validate_json(item, path=f"{path}[]") for item in value]
    raise TypeError(f"unsupported JSON value at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON text for a strict JSON-compatible value."""

    normalized = _validate_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_bytes(value: bytes) -> str:
    """Return a lowercase hexadecimal SHA-256 digest for bytes."""

    if not isinstance(value, bytes):
        raise TypeError("sha256_bytes requires bytes")
    return hashlib.sha256(value).hexdigest()


def sha256_digest(value: Any) -> str:
    """Return the SHA-256 digest of a value's canonical JSON representation."""

    return sha256_bytes(canonical_json(value).encode("utf-8"))

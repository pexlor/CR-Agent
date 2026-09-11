"""Stable, safe domain errors."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MappingProxyType

type SafeScalar = str | int | float | bool | None
_STABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class StableError(Exception):
    """An externally safe error carrying no third-party exception payload."""

    __slots__ = (
        "code",
        "category",
        "stage",
        "recoverable",
        "next_actions",
        "details",
        "_initialized",
    )

    _IMMUTABLE_FIELDS = frozenset(
        {
            "code",
            "category",
            "stage",
            "recoverable",
            "next_actions",
            "details",
            "_initialized",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_initialized", False) and name in self._IMMUTABLE_FIELDS:
            raise AttributeError("StableError is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        code: str,
        category: str,
        stage: str,
        recoverable: bool,
        next_actions: tuple[str, ...] | list[str] = (),
        details: Mapping[str, SafeScalar] | None = None,
    ) -> None:
        self._validate_name(code, "code")
        self._validate_name(category, "category")
        self._validate_name(stage, "stage")
        if type(recoverable) is not bool:
            raise TypeError("recoverable must be a bool")
        normalized_actions = tuple(next_actions)
        if any(
            not isinstance(action, str) or not action for action in normalized_actions
        ):
            raise TypeError("next_actions must contain non-empty strings")
        normalized_details = dict(details or {})
        for key, value in normalized_details.items():
            if not isinstance(key, str) or not key:
                raise TypeError("error detail keys must be non-empty strings")
            if type(value) not in (type(None), bool, int, float, str):
                raise TypeError("error details must contain scalar values only")
            if type(value) is float and not math.isfinite(value):
                raise ValueError("error detail numbers must be finite")

        Exception.__init__(self, code)
        self.code = code
        self.category = category
        self.stage = stage
        self.recoverable = recoverable
        self.next_actions = normalized_actions
        self.details = MappingProxyType(normalized_details)
        self._initialized = True

    @staticmethod
    def _validate_name(value: str, field: str) -> None:
        if not isinstance(value, str) or not _STABLE_NAME.fullmatch(value):
            raise ValueError(f"{field} must be a lowercase stable name")

    def __str__(self) -> str:
        return self.code

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "stage": self.stage,
            "recoverable": self.recoverable,
            "next_actions": list(self.next_actions),
            "details": dict(sorted(self.details.items())),
        }

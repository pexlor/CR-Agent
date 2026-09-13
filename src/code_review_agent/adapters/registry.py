"""Frozen registries for trusted providers and output adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from code_review_agent.domain.common.digests import sha256_digest

T = TypeVar("T")


class RegistryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    kind: str
    entries: tuple[tuple[str, str], ...]
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.digest:
            object.__setattr__(
                self,
                "digest",
                sha256_digest({"kind": self.kind, "entries": list(self.entries)}),
            )


class ProviderRegistry[T]:
    def __init__(self, kind: str) -> None:
        if not kind:
            raise ValueError("registry kind is required")
        self.kind = kind
        self._entries: dict[tuple[str, str], T] = {}
        self._snapshot: RegistrySnapshot | None = None

    def register(self, provider_id: str, version: str, provider: T) -> None:
        if self._snapshot is not None:
            raise RegistryError("registry_frozen")
        if not provider_id or not version:
            raise RegistryError("registry_invalid_identity")
        key = (provider_id, version)
        if key in self._entries:
            if self._entries[key] is provider:
                return
            raise RegistryError("registry_conflict")
        self._entries[key] = provider

    def freeze(self) -> RegistrySnapshot:
        if self._snapshot is None:
            self._snapshot = RegistrySnapshot(self.kind, tuple(sorted(self._entries)))
        return self._snapshot

    def resolve_exact(self, provider_id: str, version: str) -> T:
        try:
            return self._entries[(provider_id, version)]
        except KeyError:
            raise RegistryError("registry_version_not_found") from None

    def list(self) -> tuple[tuple[str, str], ...]:
        return self.freeze().entries


class InputRegistry(ProviderRegistry[T]):
    def __init__(self) -> None:
        super().__init__("input")


class ModelRegistry(ProviderRegistry[T]):
    def __init__(self) -> None:
        super().__init__("model")


class ReviewToolRegistry(ProviderRegistry[T]):
    def __init__(self) -> None:
        super().__init__("review_tool")


class OutputRegistry(ProviderRegistry[T]):
    def __init__(self) -> None:
        super().__init__("output")

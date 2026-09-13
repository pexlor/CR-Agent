from __future__ import annotations

from dataclasses import dataclass

import pytest

from code_review_agent.adapters.credentials.keyring_store import KeyringCredentialStore
from code_review_agent.adapters.registry import ProviderRegistry, RegistryError
from code_review_agent.application.credential_service import CredentialService


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def test_credentials_are_stored_by_provider_and_alias_without_leaking_secret() -> None:
    backend = MemoryKeyring()
    service = CredentialService(KeyringCredentialStore(backend=backend))

    service.set(provider_id="github", alias="shared", secret="token-secret")

    assert service.exists(provider_id="github", alias="shared")
    assert service.get(provider_id="github", alias="shared") == "token-secret"
    assert "token-secret" not in repr(service)
    service.clear(provider_id="github", alias="shared")
    assert not service.exists(provider_id="github", alias="shared")


@dataclass(frozen=True)
class Provider:
    provider_id: str
    version: str
    origin: str


def test_registry_rejects_conflict_freezes_and_resolves_exact_version() -> None:
    registry = ProviderRegistry("input")
    value = Provider("github", "1.0.0", "https://github.com")
    registry.register(value.provider_id, value.version, value)

    with pytest.raises(RegistryError, match="registry_conflict"):
        registry.register(
            value.provider_id, value.version, Provider("github", "1.0.0", "other")
        )

    snapshot = registry.freeze()
    assert registry.resolve_exact("github", "1.0.0") is value
    assert snapshot.entries == (("github", "1.0.0"),)

    with pytest.raises(RegistryError, match="registry_frozen"):
        registry.register(
            "gitlab", "1.0.0", Provider("gitlab", "1.0.0", "https://gitlab.com")
        )

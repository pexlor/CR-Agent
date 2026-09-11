import pytest

from code_review_agent.domain.common.digests import (
    canonical_json,
    sha256_bytes,
    sha256_digest,
)


def test_canonical_json_is_order_and_whitespace_independent() -> None:
    left = {"b": [2, 3], "a": "值"}
    right = {"a": "值", "b": [2, 3]}

    assert canonical_json(left) == canonical_json(right)
    assert sha256_digest(left) == sha256_digest(right)


def test_sha256_digest_is_stable_and_bytes_digest_matches() -> None:
    payload = {"message": "hello", "enabled": True}
    encoded = canonical_json(payload).encode("utf-8")

    assert sha256_digest(payload) == sha256_bytes(encoded)
    assert len(sha256_digest(payload)) == 64


def test_canonical_json_rejects_non_json_values_and_nan() -> None:
    with pytest.raises(TypeError):
        canonical_json({"unsupported": object()})
    with pytest.raises(ValueError):
        canonical_json({"not_finite": float("nan")})

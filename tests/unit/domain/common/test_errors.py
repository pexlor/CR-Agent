import pytest

from code_review_agent.domain.common.errors import StableError


def test_stable_error_contains_only_safe_contract_fields() -> None:
    error = StableError(
        code="input_malformed",
        category="input",
        stage="normalization",
        recoverable=False,
        next_actions=("provide_valid_diff",),
        details={"field": "diff", "attempt": 1, "known": True},
    )

    assert error.to_dict() == {
        "code": "input_malformed",
        "category": "input",
        "stage": "normalization",
        "recoverable": False,
        "next_actions": ["provide_valid_diff"],
        "details": {"attempt": 1, "field": "diff", "known": True},
    }
    assert "input_malformed" in str(error)


def test_stable_error_rejects_exception_objects_and_nested_details() -> None:
    with pytest.raises(TypeError):
        StableError(
            code="internal_error",
            category="internal",
            stage="execution",
            recoverable=False,
            details={"cause": RuntimeError("secret response body")},
        )
    with pytest.raises(TypeError):
        StableError(
            code="internal_error",
            category="internal",
            stage="execution",
            recoverable=False,
            details={"nested": {"body": "secret"}},
        )


def test_stable_error_rejects_invalid_codes_and_mutation() -> None:
    with pytest.raises(ValueError):
        StableError(
            code="Bad Code",
            category="internal",
            stage="execution",
            recoverable=False,
        )

    details = {"field": "value"}
    error = StableError(
        code="internal_error",
        category="internal",
        stage="execution",
        recoverable=False,
        details=details,
    )
    details["leak"] = "must not appear"
    assert "leak" not in error.to_dict()["details"]

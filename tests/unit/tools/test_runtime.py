from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from code_review_agent.adapters.tools.registry import ToolRegistry
from code_review_agent.adapters.tools.runtime import RestrictedToolRuntime
from code_review_agent.domain.common.errors import StableError
from code_review_agent.ports import tools as tool_contracts
from code_review_agent.ports.tools import (
    AuthorizedToolInput,
    ToolDeclaration,
    ToolExecutionContext,
    ToolExecutionState,
)

from .conftest import tool_manifest
from .test_registry import ALL_ALLOWED_RULES


def _runtime(
    *, rules: Sequence[Mapping[str, Any]] = ALL_ALLOWED_RULES
) -> tuple[RestrictedToolRuntime, ToolDeclaration]:
    registry = ToolRegistry()
    declaration = registry.register_toml(tool_manifest(rules=rules))[0]
    registry.freeze()
    return RestrictedToolRuntime(registry), declaration


def _authorized_input(
    text: str, *, changed_lines: tuple[int, ...] = ()
) -> AuthorizedToolInput:
    return AuthorizedToolInput(
        text=text,
        path="review.py",
        scope="hunk:1",
        token_count=tool_contracts.deterministic_tool_token_count(text),
        changed_lines=changed_lines,
    )


def test_runtime_executes_every_allowed_operation_as_inert_data() -> None:
    runtime, declaration = _runtime()
    text = "\n".join(
        (
            "TODO: auth (",
            'disable eval(user) subprocess.run(password log, mode("unsafe"));',
            "DEBUG",
            "ordinary line",
            "danger unsafe",
            "))",
        )
    )

    result = runtime.execute(
        declaration.fixed_reference(),
        _authorized_input(text, changed_lines=(5,)),
        declaration.limits,
        ToolExecutionContext(task_id="task-1", execution_id="execution-1"),
    )

    assert result.state is ToolExecutionState.SUCCEEDED
    matched_rules = {evidence.rule_id for evidence in result.evidence}
    assert matched_rules == {rule["id"] for rule in ALL_ALLOWED_RULES}
    assert result.steps > 0
    assert result.result_digest


def test_same_fixed_rule_input_scope_and_limits_have_same_order_and_digest() -> None:
    runtime, declaration = _runtime()
    authorized_input = _authorized_input("eval(user_input)\neval(other)")
    context = ToolExecutionContext(task_id="task-1", execution_id="attempt-1")

    first = runtime.execute(
        declaration.fixed_reference(),
        authorized_input,
        declaration.limits,
        context,
    )
    second = runtime.execute(
        declaration.fixed_reference(),
        authorized_input,
        declaration.limits,
        context,
    )

    assert first == second
    assert first.result_digest == second.result_digest
    assert tuple(item.ordinal for item in first.evidence) == tuple(
        range(1, len(first.evidence) + 1)
    )


@pytest.mark.parametrize(
    "limited_field", ("max_steps", "max_matches", "max_output_items")
)
def test_runtime_fails_the_whole_attempt_instead_of_returning_partial_success(
    limited_field: str,
) -> None:
    repeated_rule = (
        {
            "id": "many",
            "op": "literal_contains",
            "message": "many matches",
            "params": {"literal": "eval("},
        },
    )
    runtime, declaration = _runtime(rules=repeated_rule)
    limits = replace(declaration.limits, **{limited_field: 1})

    result = runtime.execute(
        declaration.fixed_reference(),
        _authorized_input("eval(a) eval(b) eval(c)"),
        limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.FAILED_KNOWN
    assert result.error_code == f"tool_{limited_field}_exceeded"
    assert result.evidence == ()


@pytest.mark.parametrize(
    ("input_value", "error_code"),
    (
        (
            AuthorizedToolInput(
                text="0123456789",
                path="a.py",
                scope="hunk:1",
                token_count=1,
            ),
            "tool_input_bytes_exceeded",
        ),
        (
            AuthorizedToolInput(
                text="one two three four five six",
                path="a.py",
                scope="hunk:1",
                token_count=100,
            ),
            "tool_input_tokens_exceeded",
        ),
    ),
)
def test_runtime_blocks_oversized_authorized_input(
    input_value: AuthorizedToolInput, error_code: str
) -> None:
    runtime, declaration = _runtime()
    max_input_bytes = 5 if error_code == "tool_input_bytes_exceeded" else 100
    limits = replace(declaration.limits, max_input_bytes=max_input_bytes, max_tokens=5)

    result = runtime.execute(
        declaration.fixed_reference(),
        input_value,
        limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.BLOCKED
    assert result.error_code == error_code
    assert result.evidence == ()


def test_underreported_token_count_cannot_bypass_the_runtime_limit() -> None:
    runtime, declaration = _runtime()
    text = "one two three four five six"
    underreported = AuthorizedToolInput(
        text=text,
        path="a.py",
        scope="hunk:1",
        token_count=1,
    )
    limits = replace(declaration.limits, max_tokens=5)

    result = runtime.execute(
        declaration.fixed_reference(),
        underreported,
        limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.BLOCKED
    assert result.error_code == "tool_input_tokens_exceeded"
    assert result.evidence == ()


def test_runtime_rejects_an_untrusted_token_count_even_when_under_the_limit() -> None:
    runtime, declaration = _runtime()
    incorrect_count = AuthorizedToolInput(
        text="one two",
        path="a.py",
        scope="hunk:1",
        token_count=1,
    )

    result = runtime.execute(
        declaration.fixed_reference(),
        incorrect_count,
        declaration.limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.BLOCKED
    assert result.error_code == "tool_input_token_count_mismatch"
    assert result.evidence == ()


def test_effective_rule_limit_is_enforced_before_any_rule_runs() -> None:
    runtime, declaration = _runtime()
    limits = replace(declaration.limits, max_rules=len(declaration.rules) - 1)

    result = runtime.execute(
        declaration.fixed_reference(),
        _authorized_input("eval(user_input)"),
        limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.FAILED_KNOWN
    assert result.error_code == "tool_max_rules_exceeded"
    assert result.steps == 0
    assert result.evidence == ()


def test_effective_field_limit_fails_the_whole_attempt_before_output() -> None:
    long_message_rule = (
        {
            "id": "long-message",
            "op": "literal_contains",
            "message": "x" * 80,
            "params": {"literal": "eval("},
        },
    )
    runtime, declaration = _runtime(rules=long_message_rule)
    limits = replace(declaration.limits, max_field_length=64)

    result = runtime.execute(
        declaration.fixed_reference(),
        _authorized_input("eval(user_input)"),
        limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.FAILED_KNOWN
    assert result.error_code == "tool_max_field_length_exceeded"
    assert result.steps == 0
    assert result.evidence == ()


def test_digest_mismatch_marks_determinism_violation_and_disables_version() -> None:
    runtime, declaration = _runtime()
    authorized_input = _authorized_input("eval(user_input)")
    context = ToolExecutionContext(
        task_id="task-1",
        execution_id="attempt-2",
        expected_success_digest="0" * 64,
    )

    result = runtime.execute(
        declaration.fixed_reference(),
        authorized_input,
        declaration.limits,
        context,
    )

    assert result.state is ToolExecutionState.DETERMINISM_VIOLATION
    assert result.error_code == "tool_determinism_violation"
    assert result.evidence == ()
    with pytest.raises(StableError) as raised:
        runtime.execute(
            declaration.fixed_reference(),
            authorized_input,
            declaration.limits,
            ToolExecutionContext(task_id="task-1", execution_id="attempt-3"),
        )
    assert raised.value.code == "tool_version_disabled"


def test_execution_context_exposes_no_external_or_mutating_capabilities() -> None:
    assert {field.name for field in fields(ToolExecutionContext)} == {
        "task_id",
        "execution_id",
        "expected_success_digest",
    }


def test_untrusted_input_is_never_executed(tmp_path: Path) -> None:
    runtime, declaration = _runtime()
    marker = tmp_path / "executed"
    untrusted = f"touch {marker}\n__import__('os').system('touch {marker}')\neval("

    result = runtime.execute(
        declaration.fixed_reference(),
        _authorized_input(untrusted),
        declaration.limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.SUCCEEDED
    assert not marker.exists()


@pytest.mark.parametrize("text", ("line\r\n", "e\u0301"))
def test_runtime_blocks_text_outside_the_lf_nfc_input_contract(text: str) -> None:
    runtime, declaration = _runtime()

    result = runtime.execute(
        declaration.fixed_reference(),
        _authorized_input(text),
        declaration.limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.BLOCKED
    assert result.error_code == "tool_input_not_normalized"

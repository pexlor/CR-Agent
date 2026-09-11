from __future__ import annotations

from code_review_agent.adapters.tools.registry import ToolRegistry
from code_review_agent.adapters.tools.runtime import RestrictedToolRuntime
from code_review_agent.ports.tools import (
    AuthorizedToolInput,
    ToolExecutionContext,
    ToolExecutionState,
)

from tests.unit.tools.conftest import tool_manifest


def test_new_static_pattern_tool_is_added_using_declaration_data_only() -> None:
    base_manifest = tool_manifest(tool_id="base-tool")
    added_manifest = tool_manifest(
        tool_id="unsafe-deserialization",
        rules=(
            {
                "id": "python-pickle-loads",
                "op": "token_sequence",
                "message": "Untrusted pickle data can execute code",
                "params": {"tokens": ["pickle", ".", "loads"]},
            },
        ),
    )
    registry = ToolRegistry()
    registry.register_toml(base_manifest)

    before = {item.tool_id for item in registry.catalog()}
    registry.register_toml(added_manifest)
    after = {item.tool_id for item in registry.catalog()}
    registry.freeze()

    added = registry.resolve_exact("unsafe-deserialization", "1.0.0")
    result = RestrictedToolRuntime(registry).execute(
        added.fixed_reference(),
        AuthorizedToolInput(
            text="value = pickle.loads(payload)",
            path="codec.py",
            scope="line:1",
            token_count=6,
            changed_lines=(1,),
        ),
        added.limits,
        ToolExecutionContext(task_id="task-1", execution_id="tool-attempt-1"),
    )

    assert after - before == {"unsafe-deserialization"}
    assert result.state is ToolExecutionState.SUCCEEDED
    assert [item.rule_id for item in result.evidence] == ["python-pickle-loads"]

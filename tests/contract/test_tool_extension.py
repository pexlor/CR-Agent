from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from code_review_agent.adapters.tools.registry import ToolRegistry
from code_review_agent.adapters.tools.runtime import RestrictedToolRuntime
from code_review_agent.ports import tools as tool_contracts
from code_review_agent.ports.tools import (
    AuthorizedToolInput,
    FixedToolReference,
    ToolCatalogPort,
    ToolDeclaration,
    ToolExecutionContext,
    ToolExecutionState,
    ToolRegistrySnapshot,
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
            token_count=tool_contracts.deterministic_tool_token_count(
                "value = pickle.loads(payload)"
            ),
            changed_lines=(1,),
        ),
        added.limits,
        ToolExecutionContext(task_id="task-1", execution_id="tool-attempt-1"),
    )

    assert after - before == {"unsafe-deserialization"}
    assert result.state is ToolExecutionState.SUCCEEDED
    assert [item.rule_id for item in result.evidence] == ["python-pickle-loads"]


@dataclass
class _CatalogDouble:
    declaration: ToolDeclaration
    disabled: bool = False

    def freeze(self) -> ToolRegistrySnapshot:
        return ToolRegistrySnapshot((self.declaration.fixed_reference(),))

    def catalog(self) -> tuple[ToolDeclaration, ...]:
        return (self.declaration,)

    def resolve_exact(self, tool_id: str, version: str) -> ToolDeclaration:
        assert (tool_id, version) == (
            self.declaration.tool_id,
            self.declaration.version,
        )
        return self.declaration

    def verify_fixed_reference(self, reference: FixedToolReference) -> ToolDeclaration:
        assert reference == self.declaration.fixed_reference()
        return self.declaration

    def disable_version(self, reference: FixedToolReference, *, reason: str) -> None:
        assert reference == self.declaration.fixed_reference()
        assert reason == "determinism_violation"
        self.disabled = True


def test_runtime_accepts_the_catalog_port_without_a_concrete_registry() -> None:
    registry = ToolRegistry()
    declaration = registry.register_toml(tool_manifest(tool_id="port-backed"))[0]
    catalog: ToolCatalogPort = _CatalogDouble(declaration)
    text = "eval(user_input)"

    result = RestrictedToolRuntime(catalog).execute(
        declaration.fixed_reference(),
        AuthorizedToolInput(
            text=text,
            path="review.py",
            scope="line:1",
            token_count=tool_contracts.deterministic_tool_token_count(text),
        ),
        declaration.limits,
        ToolExecutionContext(task_id="task-1", execution_id="attempt-1"),
    )

    assert result.state is ToolExecutionState.SUCCEEDED


def test_runtime_module_does_not_import_the_registry_adapter() -> None:
    path = Path("src/code_review_agent/adapters/tools/runtime.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    imports = {
        (node.level, node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert (1, "registry") not in imports

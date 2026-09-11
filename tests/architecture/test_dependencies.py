"""Static dependency direction checks for the domain layer."""

import ast
from pathlib import Path

FORBIDDEN_MODULES = {
    "typer",
    "rich",
    "sqlite3",
    "langchain",
    "github",
    "gitlab",
    "httpx",
    "keyring",
    "jinja2",
}


def test_domain_does_not_import_adapter_dependencies() -> None:
    domain_root = Path("src/code_review_agent/domain")
    for path in domain_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names = (node.module or "",)
            else:
                continue
            for name in names:
                root_module = name.partition(".")[0]
                assert root_module not in FORBIDDEN_MODULES, (
                    f"{path} imports forbidden dependency {name}"
                )

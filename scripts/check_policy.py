"""Enforces documentation, dependency, and release metadata boundaries."""

import ast
import io
import json
import sys
import tokenize
import tomllib
from pathlib import Path


def main() -> None:
    """Rejects undocumented code, inline comments and runtime dependencies."""
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    errors = []
    if metadata["dependencies"]:
        errors.append("Runtime dependencies must remain empty.")
    for directory in ("agent_bridge", "scripts"):
        for path in sorted((root / directory).glob("*.py")):
            text = path.read_text()
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (
                        ast.Module,
                        ast.ClassDef,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                    ),
                ) and not ast.get_docstring(node):
                    errors.append(
                        f"{path.name}:{getattr(node, 'lineno', 1)}: "
                        "missing docstring"
                    )
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] not in sys.stdlib_module_names | {
                        "agent_bridge"
                    }:
                        errors.append(f"{path.name}: non-stdlib import {name}")
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    errors.append(
                        f"{path.name}:{token.start[0]}: "
                        "use a docstring, not an inline comment"
                    )
    for client in ("codex", "claude"):
        path = root / "plugins/agent-bridge" / f".{client}-plugin/plugin.json"
        if json.loads(path.read_text())["version"] != metadata["version"]:
            errors.append(f"{client} plugin version differs from package")
    marketplace = json.loads(
        (root / ".claude-plugin/marketplace.json").read_text()
    )
    if marketplace["plugins"][0]["version"] != metadata["version"]:
        errors.append("Claude marketplace version differs from package")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        "Policy: documented code, no inline comments, "
        "stdlib runtime, aligned versions"
    )


if __name__ == "__main__":
    main()

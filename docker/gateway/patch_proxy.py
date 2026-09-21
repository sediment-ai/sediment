# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remove database-only retries from the pinned no-database LiteLLM proxy."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

# Any upstream source change needs a fresh review of the removed code paths.
SOURCES = {
    "proxy_server.py": (
        "0a33c833d0f269e16bb15d388dac7ea1967c202922765d5f0a81851c33c013de",
        0,
    ),
    "utils.py": (
        "eb990a71d0c12cb7d8115de0daf39c662915134cf0ca35cc79b4e48552437cf2",
        11,
    ),
}


def patch_proxy(root: Path) -> None:
    sources = {name: (root / name).read_bytes() for name in SOURCES}
    for name, source in sources.items():
        if hashlib.sha256(source).hexdigest() != SOURCES[name][0]:
            raise ValueError(f"Unexpected LiteLLM source: {name}")
    replacements = {}
    for name, source in sources.items():
        tree = ast.parse(source)
        remove: set[int] = set()
        imports = decorators = 0
        for node in ast.walk(tree):
            selected = None
            if isinstance(node, ast.Import) and any(
                item.name == "backoff" for item in node.names
            ):
                selected = node
                imports += 1
                # utils wraps this one import in an ImportError guard.
                if name == "utils.py":
                    selected = next(
                        item
                        for item in ast.walk(tree)
                        if isinstance(item, ast.Try) and item.body == [node]
                    )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id == "backoff"
                    ):
                        decorators += 1
                        remove.update(range(decorator.lineno - 1, decorator.end_lineno))
            if selected is not None:
                remove.update(range(selected.lineno - 1, selected.end_lineno))
        if imports != 1 or decorators != SOURCES[name][1]:
            raise ValueError(f"Unexpected LiteLLM patch sites: {name}")
        updated = b"".join(
            line
            for index, line in enumerate(source.splitlines(keepends=True))
            if index not in remove
        )
        if any(
            isinstance(node, ast.Name) and node.id == "backoff"
            for node in ast.walk(ast.parse(updated))
        ):
            raise ValueError(f"Remaining backoff use in LiteLLM source: {name}")
        replacements[name] = updated
    for name, updated in replacements.items():
        (root / name).write_bytes(updated)
        for bytecode in (root / "__pycache__").glob(f"{Path(name).stem}.*.pyc"):
            bytecode.unlink()


if __name__ == "__main__":
    patch_proxy(Path(sys.argv[1]))

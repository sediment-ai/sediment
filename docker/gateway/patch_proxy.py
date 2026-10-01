# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remove database retries and guard absent Prisma in the pinned LiteLLM proxy."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

# Any upstream source change needs a fresh review of the removed code paths.
SOURCES = {
    "proxy_server.py": (
        "80eba6eb3781eec788657ad4da72fcda1bd2c2c5001c57d65508a2d2926d8e2e",
        0,
    ),
    "utils.py": (
        "677f33b4b1c67e1dba0e91cfd400feacd6e12e81bf8ca65d865b90d1421b732a",
        11,
    ),
    "db/exception_handler.py": (
        "329f721d58e101b7195a2554d81a02cb77962c14fbc1e2869c4435b1ea2c6e36",
        9,
    ),
    "auth/user_api_key_auth.py": (
        "e0f27addeafaddaca17ee4f5dec599c13c23770ae3af3f1b4035db92c0deaa9d",
        1,
    ),
}


def patch_proxy(root: Path) -> None:
    sources = {name: (root / name).read_bytes() for name in SOURCES}
    for name, source in sources.items():
        if hashlib.sha256(source).hexdigest() != SOURCES[name][0]:
            raise ValueError(f"Unexpected LiteLLM source: {name}")
    replacements = {}
    for name, source in sources.items():
        if name == "auth/user_api_key_auth.py":
            # This image accepts only its master key; no key database is missing.
            old = (
                b'                message="No connected db.",\n'
                b"                type=ProxyErrorTypes.no_db_connection,\n"
                b"                code=400,\n"
            )
            if source.count(old) != SOURCES[name][1]:
                raise ValueError(f"Unexpected LiteLLM patch sites: {name}")
            replacements[name] = source.replace(
                old,
                b'                message="Invalid proxy API key.",\n'
                b"                type=ProxyErrorTypes.auth_error,\n"
                b"                code=401,\n",
            )
            continue
        if name == "db/exception_handler.py":
            # Every selected import is inside a Prisma-error classifier. Without
            # Prisma, no exception can be an instance of its error types, so a
            # Boolean predicate answers False and the SQLSTATE lookup None.
            fallbacks = {"bool": b"False", "str | None": b"None"}
            functions = [
                node
                for node in ast.walk(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            lines = source.splitlines(keepends=True)
            selected = [
                index
                for index, line in enumerate(lines)
                if line
                in (
                    b"        import prisma\n",
                    b"        import prisma.engine.errors\n",
                )
            ]
            if len(selected) != SOURCES[name][1]:
                raise ValueError(f"Unexpected LiteLLM patch sites: {name}")
            for index in selected:
                function = max(
                    (
                        node
                        for node in functions
                        if node.lineno <= index + 1 <= node.end_lineno
                    ),
                    key=lambda node: node.lineno,
                )
                returns = function.returns and ast.unparse(function.returns)
                if returns not in fallbacks:
                    raise ValueError(f"Unexpected LiteLLM patch sites: {name}")
                lines[index] = (
                    b"        try:\n    "
                    + lines[index]
                    + b"        except ModuleNotFoundError:\n            return "
                    + fallbacks[returns]
                    + b"\n"
                )
            updated = b"".join(lines)
            ast.parse(updated)
            replacements[name] = updated
            continue
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
        for bytecode in (
            (root / name)
            .parent.joinpath("__pycache__")
            .glob(f"{Path(name).stem}.*.pyc")
        ):
            bytecode.unlink()


if __name__ == "__main__":
    patch_proxy(Path(sys.argv[1]))

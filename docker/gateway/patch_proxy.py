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
        "8e3a49e253c6ae0a8fc3bb5ceb0c6d69395a9fe8b87575e3ad051d8bf05dbbe7",
        0,
    ),
    "utils.py": (
        "eb990a71d0c12cb7d8115de0daf39c662915134cf0ca35cc79b4e48552437cf2",
        11,
    ),
    "db/exception_handler.py": (
        "16036ddb46573dfa04746c2a21a844c9d04ac18a81e40f436a3000d84a66c2be",
        8,
    ),
    "auth/user_api_key_auth.py": (
        "5d294cc1818ccb9451f9a1a39ecf0a402fdfd265800e14c2348a00ec20832fd5",
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
            # Every selected import is inside a Boolean Prisma-error predicate.
            # Without Prisma, no exception can be an instance of its error types.
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
                lines[index] = (
                    b"        try:\n    "
                    + lines[index]
                    + b"        except ModuleNotFoundError:\n            return False\n"
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

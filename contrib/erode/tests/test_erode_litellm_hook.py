# SPDX-License-Identifier: MIT
"""The LiteLLM hook: in-place rewrite, logged messages, and fail-open paths."""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
import types

import pytest

from erode import litellm_hook
from erode.core import PrunePolicy, prune


def _request() -> dict:
    content = "x" * 5000
    messages: list[dict] = [{"role": "user", "content": "Fix a.py."}]
    for step, name in enumerate(["Read", "Read", "Bash", "Bash"], start=1):
        arguments = (
            {"file_path": "a.py"} if name == "Read" else {"command": f"ls {step}"}
        )
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{step}",
                        "name": name,
                        "input": arguments,
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{step}",
                        "content": content,
                    }
                ],
            }
        )
    return {"model": "claude-test", "messages": messages}


class _LoggingObject:
    """The member of LiteLLM's logging object that the hook calls."""

    def __init__(self) -> None:
        self.messages = None

    def update_messages(self, messages) -> None:
        self.messages = messages


def test_apply_rewrites_messages_in_place_and_updates_logging() -> None:
    data = _request()
    expected, report = prune(data["messages"], PrunePolicy())
    assert report["stubbed_results"] == 1
    data["litellm_logging_obj"] = logging_obj = _LoggingObject()
    assert litellm_hook.apply(data) == report
    assert data["messages"] == expected
    assert logging_obj.messages == expected


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"model": "m"},
        {**_request(), "context_management": {"edits": [{"type": "x"}]}},
    ],
)
def test_apply_leaves_unprunable_requests_alone(data) -> None:
    before = copy.deepcopy(data)
    assert litellm_hook.apply(data) is None
    assert data == before


def test_apply_restores_messages_when_logging_update_fails() -> None:
    class Broken(_LoggingObject):
        def update_messages(self, messages) -> None:
            raise RuntimeError("synthetic")

    data = _request()
    original = data["messages"]
    data["litellm_logging_obj"] = Broken()
    assert litellm_hook.apply(data) is None
    assert data["messages"] is original


def test_handler_prunes_inside_litellm(monkeypatch) -> None:
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = type("CustomLogger", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", types.ModuleType("litellm"))
    monkeypatch.setitem(
        sys.modules, "litellm.integrations", types.ModuleType("litellm.integrations")
    )
    monkeypatch.setitem(
        sys.modules, "litellm.integrations.custom_logger", custom_logger
    )
    hook = importlib.reload(litellm_hook)
    try:
        data = _request()
        expected, _ = prune(data["messages"], PrunePolicy())
        result = asyncio.run(hook.handler.async_pre_call_hook(None, None, data, "x"))
        assert result is data and data["messages"] == expected

        monkeypatch.setenv("ERODE_MODE", "off")
        untouched = _request()
        before = copy.deepcopy(untouched)
        assert (
            asyncio.run(hook.handler.async_pre_call_hook(None, None, untouched, "x"))
            is None
        )
        assert untouched == before
    finally:
        monkeypatch.undo()
        importlib.reload(litellm_hook)

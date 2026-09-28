# SPDX-License-Identifier: MIT
"""
erode as a LiteLLM pre-call hook.

Register it in the proxy's ``config.yaml``:

    litellm_settings:
      callbacks: erode.litellm_hook.handler

The hook prunes each request's messages before LiteLLM calls the model and
updates the messages that LiteLLM logs, so logging callbacks record exactly
what the model received. ``ERODE_MODE=off`` disables it. ``apply`` is the
standard-library core of the hook; another callback can call it directly.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from erode.core import PrunePolicy, prune_request

logger = logging.getLogger("erode.litellm_hook")


def apply(data: Any, policy: PrunePolicy | None = None) -> dict | None:
    """Prune a LiteLLM request dict in place; return the report, or None.

    None means the request wasn't pruned: it isn't a chat request, the provider
    manages its context, or pruning failed and the request is unchanged.
    """
    if not isinstance(data, dict):
        return None
    original = {key: data[key] for key in ("messages", "input") if key in data}
    try:
        pruned, report = prune_request(data, policy or PrunePolicy())
        if report is None or pruned is data:
            return report
        data.update({key: pruned[key] for key in original})
        logging_obj = data.get("litellm_logging_obj")
        if "messages" in original and hasattr(logging_obj, "update_messages"):
            logging_obj.update_messages(data["messages"])
    except Exception:  # noqa: BLE001 — pruning must never fail a model call
        data.update(original)
        logger.warning("erode_prune reason=prune_failed")
        return None
    return report


try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # LiteLLM is the host process, never a dependency
    CustomLogger = None

if CustomLogger is not None:

    class ErodeHook(CustomLogger):
        async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
            if os.environ.get("ERODE_MODE", "supersede").strip() == "off":
                return None
            return data if apply(data) is not None else None

    handler = ErodeHook()

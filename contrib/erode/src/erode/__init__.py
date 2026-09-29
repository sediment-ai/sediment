# SPDX-License-Identifier: MIT
"""erode wears stale tool output out of an agent's model requests."""

from erode.core import STUB_PREFIX, PrunePolicy, prune, prune_request

__all__ = ["STUB_PREFIX", "PrunePolicy", "prune", "prune_request"]
__version__ = "0.1.0"

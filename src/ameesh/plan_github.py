# SPDX-License-Identifier: AGPL-3.0-only
"""Compatibility alias for the development module (L100 T3, #4384)."""
import sys

from .modules.dev import plan_github as _implementation

sys.modules[__name__] = _implementation

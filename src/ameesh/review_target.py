# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.review_target` (L100 T4, #4385).

Both paths resolve to the same module object, preserving imports and patches
of module globals. Retained compatibility alias, declared and dated in
tests/test_generic_core.py (L104 T6).
"""
from __future__ import annotations

import sys

from .modules.dev import review_target as _implementation

sys.modules[__name__] = _implementation

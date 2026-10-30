# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.review_target` (L100 T4, #4385).

Both paths resolve to the same module object, preserving imports and patches
of module globals until the aliases are removed in L104.
"""
from __future__ import annotations

import sys

from .modules.dev import review_target as _implementation

sys.modules[__name__] = _implementation

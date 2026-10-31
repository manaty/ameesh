# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.plan_git` (L100 T2, #4383).

The git and content merge plan moved to the development module; the two names
resolve to the same module object, so every importer and every test that
patches a module global by the old name keeps working unchanged. Retained compatibility
alias, declared and dated in tests/test_generic_core.py (L104 T6).
"""
from __future__ import annotations

import sys

from .modules.dev import plan_git as _plan_git

#: `import ameesh.plan_git` hands back `ameesh.modules.dev.plan_git` itself
sys.modules[__name__] = _plan_git

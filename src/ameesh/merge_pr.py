# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.merge_pr` (L100 T1, #4382).

The merge gesture moved to the development module; the two names resolve to
the same module object, so every importer and every test that patches a module
global by the old name keeps working unchanged. Retained compatibility
alias, declared and dated in tests/test_generic_core.py (L104 T6).
"""
from __future__ import annotations

import sys

from .modules.dev import merge_pr as _merge_pr

#: `import ameesh.merge_pr` hands back `ameesh.modules.dev.merge_pr` itself
sys.modules[__name__] = _merge_pr

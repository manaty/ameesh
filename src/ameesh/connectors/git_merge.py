# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.git_merge` (L100 T1, #4382).

The `git-merge` action connector moved to the development module; the two
names resolve to the same module object, so importers and patchers of the old
path keep working unchanged. Retained compatibility
alias, declared and dated in tests/test_generic_core.py (L104 T6).
"""
from __future__ import annotations

import sys

from ..modules.dev import git_merge as _git_merge

#: `import ameesh.connectors.git_merge` hands back
#: `ameesh.modules.dev.git_merge` itself
sys.modules[__name__] = _git_merge

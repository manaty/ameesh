# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.housekeeping` (L148), deleted in stage 7.

The module was renamed ``menage`` -> ``housekeeping``. The two names resolve
to the same module object, so every importer keeps working unchanged --
including the tests that patch a module global (``menage.time``,
``menage.in_memory``): the patch lands on the code's own global. A plain
re-export shim would not: the patched copy would no longer be the one the
renamed code reads. ``menage.cmd_menage`` is the old spelling of
:func:`ameesh.housekeeping.cmd_housekeeping`.
"""
import sys

from . import housekeeping as _housekeeping

#: `import ameesh.menage` hands back `ameesh.housekeeping` itself
sys.modules[__name__] = _housekeeping

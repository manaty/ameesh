# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.ledger` (L148), deleted in stage 7.

The module was renamed ``grand_livre`` -> ``ledger``. The two names resolve to
the same module object, so every importer keeps working unchanged --
including the tests that patch a module global (``grand_livre.time``) and the
classes already written in session logs: the patch and the class land on the
code's own module. A plain re-export shim would create a second module object
and silently miss both. ``grand_livre.CorrectionError`` is the old spelling of
:class:`ameesh.ledger.CorrectionError`.

``python -m ameesh.grand_livre`` keeps its old behaviour: the usage line on
stderr, exit code 2.
"""
from __future__ import annotations

import sys

from . import ledger as _ledger
from .ledger import add_parser, main  # noqa: F401 - re-export the checker follows

if __name__ == "__main__":
    print("usage: ameesh cost correct [--apply] [--since 30d] [--json]", file=sys.stderr)
    sys.exit(2)

#: `import ameesh.grand_livre` hands back `ameesh.ledger` itself
sys.modules[__name__] = _ledger

# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.storage.reports` (P1b #7650).

Les lectures analytiques de l'étude de modèles portent sur des tables du
module `dev` (`ci_runs`, `provenance_receipts`, `review_finding_resolutions`) ;
leur SQL vit avec le module. Alias de compatibilité daté, déclaré dans
``tests/test_generic_core.py`` (L104 T6).
"""
from __future__ import annotations

import sys

from ...modules.dev.storage import reports as _reports

#: `import ameesh.storage.postgres.reports` rend le module du module `dev`
sys.modules[__name__] = _reports

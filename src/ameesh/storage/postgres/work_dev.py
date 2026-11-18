# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.storage.work` (P1b #7650).

Le stockage des tables de développement a rejoint le paquet du module `dev`
(étude #7616 §4.6). L'alias rend le même objet module : `storage.postgres.work`
en importe `DevWork` et lui délègue les opérations concernées, sans importer
l'implémentation du module (L97). Alias de compatibilité daté, déclaré dans
``tests/test_generic_core.py`` (L104 T6).
"""
from __future__ import annotations

import sys

from ...modules.dev.storage import work as _work

#: `import ameesh.storage.postgres.work_dev` rend `...modules.dev.storage.work`
sys.modules[__name__] = _work

# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.storage.migration_reservations` (P1b #7650).

Le stockage des tables de développement a rejoint le paquet du module `dev`
(étude #7616 §4.6) ; les deux noms rendent le même objet module, donc les
importeurs et les tests qui patchent un global par l'ancien chemin continuent
de fonctionner. Alias de compatibilité daté, déclaré dans
``tests/test_generic_core.py`` (L104 T6) et retiré avec les autres.
"""
from __future__ import annotations

import sys

from ...modules.dev.storage import migration_reservations as _impl

#: `import ameesh.storage.postgres.migration_reservations` rend `ameesh.modules.dev.storage.migration_reservations`
sys.modules[__name__] = _impl

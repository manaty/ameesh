# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.storage.agent_columns` (P1b #7650).

Les colonnes d'identité des tables du module `dev` sont réécrites par le
renommage d'agent ; leur SQL vit avec le module. Alias de compatibilité daté,
déclaré dans ``tests/test_generic_core.py`` (L104 T6).
"""
from __future__ import annotations

import sys

from ...modules.dev.storage import agent_columns as _agent_columns

#: `import ameesh.storage.postgres.agent_columns` rend le module du module `dev`
sys.modules[__name__] = _agent_columns

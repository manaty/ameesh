# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.storage.landings` (P1b #7650).

Les déclarations d'atterrissage (`work_landings`) sont une table de
développement ; leur SQL vit avec le module `dev`. Alias de compatibilité daté,
déclaré dans ``tests/test_generic_core.py`` (L104 T6).
"""
from __future__ import annotations

import sys

from ...modules.dev.storage import landings as _landings

#: `import ameesh.storage.postgres.landings` rend le module du module `dev`
sys.modules[__name__] = _landings

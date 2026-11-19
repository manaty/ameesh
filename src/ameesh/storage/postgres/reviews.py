# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of :mod:`ameesh.modules.dev.storage.reviews` (P1b #7650).

Le journal des constats de revue et les retraits de relecteur sont des tables
de développement ; leur SQL vit avec le module `dev`. Alias de compatibilité
daté, déclaré dans ``tests/test_generic_core.py`` (L104 T6).
"""
from __future__ import annotations

import sys

from ...modules.dev.storage import reviews as _reviews

#: `import ameesh.storage.postgres.reviews` rend le module du module `dev`
sys.modules[__name__] = _reviews

# SPDX-License-Identifier: AGPL-3.0-only
"""Tests du paquet agent-mesh (banc local, conteneur agent-mesh-pg)."""
import os
import tempfile

# #3042 : sous macOS `/tmp` et `/var` sont des liens vers `/private/...`.
# Les gardes de sécurité des scripts de déploiement refusent, à raison, tout
# ancêtre symbolique : un bac jetable créé sous le TMPDIR lexical rougissait
# sur la machine au lieu du code testé (canon §3). On résout le TMPDIR une
# fois, avant que le premier bac ne soit créé. Le CI garde un TMPDIR court
# (`/tmp/ameesh-ci`) : sa résolution (`/private/tmp/ameesh-ci`) le reste.
_tmpdir = os.path.realpath(tempfile.gettempdir())
if _tmpdir != tempfile.gettempdir():
    os.environ["TMPDIR"] = _tmpdir
    tempfile.tempdir = _tmpdir

# #3974 : le cache de l'hôte des balayages `git grep` (`grep_cache`) vit sous
# l'état de l'utilisateur ; les tests le coupent, sauf ceux qui le visent avec
# un dossier jetable.
# un drapeau de module, pas une variable : des tests vident l'environnement.
from ameesh import grep_cache as _grep_cache  # noqa: E402
_grep_cache.ENABLED = False

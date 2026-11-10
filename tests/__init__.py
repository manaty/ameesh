# SPDX-License-Identifier: AGPL-3.0-only
"""Tests du paquet agent-mesh (banc local, conteneur agent-mesh-pg)."""
import os
import sys
import tempfile

# #6609 : sans PYTHONPATH, `python -m unittest tests.…` prenait l'ameesh du
# venv installé et non celui de l'arbre testé (faux rouges des tests ciblés,
# paquet-horaire refusant une bibliothèque « déjà chargée ailleurs »). L'arbre
# `src` passe en tête une fois, ici, avant tout import d'ameesh ; une
# bibliothèque chargée hors de cet arbre arrête la suite net.
_SRC = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path[:] = [_SRC] + [p for p in sys.path
                        if not p or os.path.realpath(p) != _SRC]
import ameesh as _ameesh  # noqa: E402
_loaded = os.path.realpath(getattr(_ameesh, "__file__", None) or "")
if os.path.commonpath([_SRC, _loaded or "/"]) != _SRC:
    raise ImportError("tests : ameesh chargé depuis %s, hors de l'arbre "
                      "testé %s" % (_loaded or "?", _SRC))

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

# #5804 : le banc garde le HOME du poste (L142), pas son état d'exploitation.
# Une attestation de fixture matérialisée dans
# `~/.local/state/ameesh/mise-a-jour` a fait refuser le coupeur (#5748) ;
# toute écriture d'un test sous `mise-a-jour/` ou `release/` échoue désormais
# au moment du geste (tests/test_5804_real_state_guard.py).
from . import state_guard as _state_guard  # noqa: E402
_state_guard.install()

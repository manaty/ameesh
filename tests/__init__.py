# SPDX-License-Identifier: AGPL-3.0-only
"""Tests du paquet agent-mesh (banc local, conteneur agent-mesh-pg)."""
import atexit
import os
import shutil
import sys
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

# #8000 : la suite ne lit NI la configuration NI le canon de l'hôte sans le
# demander. Posé dans le paquet — donc avant tout import d'un module de test —
# un module qui n'importe pas `tests.support` (test_5729 et son
# `scripts/test-plan.py:make_plan`, par exemple) héritait sinon de
# `~/.config/ameesh/config.json` et de ses canons : vert en CI, rouge sur
# AG1/AG3 où le canon déclare `contract_selection` (#7970, #7996, #7999).
# « Aucune configuration » se dit par un fichier VIDE mais EXISTANT : un
# chemin explicite absent est refusé (#1127), et un chemin absent ne peut
# donc pas servir de sentinelle. `tests.support` réexporte `EMPTY_CONFIG`.
_CONFIG_TMP = tempfile.mkdtemp(prefix="ameesh-test-config-")
EMPTY_CONFIG = os.path.join(_CONFIG_TMP, "empty.json")
with open(EMPTY_CONFIG, "w", encoding="utf-8") as _fh:
    _fh.write("{}\n")
os.environ["AMEESH_CONFIG"] = EMPTY_CONFIG
os.environ["AGENT_MESH_CONFIG"] = EMPTY_CONFIG
#: le canon de l'hôte, lui, ne se remplace pas par un fichier : il se retire.
#: Un test qui en veut un le pose explicitement (`AMEESH_CANON` +
#: `AMEESH_CANON_REF`, ou `support.mechanics_canon()`).
for _name in ("AMEESH_CANON", "AMEESH_CANONS", "AMEESH_CANON_REF",
              "AMEESH_CANON_UNTRUSTED", "AGENT_MESH_CANON",
              "AGENT_MESH_CANON_REF", "AGENT_MESH_CANON_UNTRUSTED"):
    os.environ.pop(_name, None)

_CONFIG_TMP_PID = os.getpid()


def _cleanup_config_tmp() -> None:
    """#6228 : le dossier de la config vide ne survit pas à son processus.

    Un enfant forké qui sort normalement hérite des `atexit` : il ne doit pas
    retirer le dossier de son parent (même règle que `tests.support`)."""
    if os.getpid() == _CONFIG_TMP_PID:
        shutil.rmtree(_CONFIG_TMP, ignore_errors=True)


atexit.register(_cleanup_config_tmp)

# #6609 : sans PYTHONPATH, `python -m unittest tests.…` prenait l'ameesh du
# venv installé et non celui de l'arbre testé (faux rouges des tests ciblés,
# paquet-horaire refusant une bibliothèque « déjà chargée ailleurs »). L'arbre
# `src` passe en tête une fois, ici, avant tout import d'ameesh ; une
# bibliothèque chargée hors de cet arbre arrête la suite net. « .. » se réduit
# lexicalement avant de résoudre : un banc qui relie `tests/` par lien
# symbolique à côté d'une copie de `src` (test_3687) charge la copie voisine
# du lien, pas le `src` du dépôt réel.
_SRC = os.path.realpath(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path[:] = [_SRC] + [p for p in sys.path
                        if not p or os.path.realpath(p) != _SRC]
import ameesh as _ameesh  # noqa: E402
_loaded = os.path.realpath(getattr(_ameesh, "__file__", None) or "")
if os.path.commonpath([_SRC, _loaded or "/"]) != _SRC:
    raise ImportError("tests : ameesh chargé depuis %s, hors de l'arbre "
                      "testé %s" % (_loaded or "?", _SRC))

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

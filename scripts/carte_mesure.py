# SPDX-License-Identifier: AGPL-3.0-only
"""Deprecated alias of `scripts/map_measure.py` (L148), deleted at stage 7.

An `ameesh_carte.pth` file installed before the rename runs this path
directly (`scripts/map.py install`); it keeps working until the measurement
environment is installed again.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from map_measure import *  # noqa: E402,F401,F403
from map_measure import Logbook, repository_path, start  # noqa: E402,F401

#: old names, for the `.pth` files and callers written before the rename
ENV_DOSSIER = ENV_MEASURE_DIR  # noqa: F405
ENV_CONTEXTE = ENV_MEASURE_CONTEXT  # noqa: F405
Releve = Logbook
chemin_du_depot = repository_path
ligne_pth = pth_line  # noqa: F405
demarrer = start

if __name__ == "ameesh_carte_demarrage":
    demarrer()

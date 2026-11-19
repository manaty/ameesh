# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §4.6) : le stockage des tables du module `dev`.

Les implémentations sont dans ce paquet ; les noms historiques de
``ameesh.storage.postgres`` restent des alias datés. Tout SQL qui nomme une
table du module passe par :mod:`ameesh.modules.dev.storage.schema`, qui la
qualifie au schéma physique courant (noyau avant P2, ``<noyau>__dev`` après).
"""

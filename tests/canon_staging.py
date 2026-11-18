# SPDX-License-Identifier: AGPL-3.0-only
"""#7662 : la séquence staging Manaty, en fiches Host synthétiques.

Le cœur ne connaît aucun nom d'hôte : ces valeurs sont la fixture du canon
`manaty/home` (``hotes/*.md``), pas un défaut du produit. Un test qui porte
sur le mesh Manaty s'en sert pour bâtir la topologie qu'un vrai canon déclare ;
le profil nu, lui, reste vide (``canon.staging_topology([])``).
"""
from __future__ import annotations

import types

from ameesh import canon

#: ``staging.order`` et ``staging.kind`` des fiches Host du canon Manaty :
#: trois VM d'agents (toujours requises), le serveur sans exécutant (reportable)
#: puis le poste (reportable, en dernier), canon §14.
MANATY_HOSTS = (
    ("ameesh-agents-1", 10, "agents"),
    ("ameesh-agents-2", 20, "agents"),
    ("ameesh-agents-3", 30, "agents"),
    ("manaty-mesh-1", 40, "server"),
    ("pc-smichea", 50, "workstation"),
)

HOSTS = tuple(title for title, _order, _kind in MANATY_HOSTS)
AGENTS = tuple(title for title, _order, kind in MANATY_HOSTS if kind == "agents")
SERVER = next(title for title, _order, kind in MANATY_HOSTS if kind == "server")
WORKSTATION = next(title for title, _order, kind in MANATY_HOSTS
                   if kind == "workstation")
VMS = AGENTS + (SERVER,)


def host_cards() -> list:
    """Les fiches Host minimales que ``canon.staging_topology`` sait lire."""
    return [types.SimpleNamespace(title=title,
                                  staging=canon.HostStaging(order=order, kind=kind))
            for title, order, kind in MANATY_HOSTS]


def fake_canon() -> types.SimpleNamespace:
    """Un canon synthétique porteur de ces fiches (sources factices)."""
    return types.SimpleNamespace(sources=[], hosts=host_cards())


def topology() -> canon.StagingTopology:
    """La séquence calculée depuis ces fiches, sans nom codé dans le cœur."""
    return canon.staging_topology([fake_canon()])

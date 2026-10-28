# SPDX-License-Identifier: AGPL-3.0-only
"""#3641 : `host_lifecycle.critical_names` suit le repli sûr de L242c.

#3643 lit les `critical_roles` du canon ; mais un canon propriétaire
illisible laissait `card_role` répondre « non » en silence, et la persona
`auditeur` sans fiche n'était critique que par une fiche. Le drain doit
appliquer la même lecture que l'exécuteur et l'auditeur (#424 r1-r6) :
`critical_verdict`, avec le dernier verdict lu quand le canon est illisible,
et « critique » à défaut de verdict connu.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import host_lifecycle


class _Canon:
    """Canon minimal : des fiches avec rôles, et un `federation` facultatif."""

    def __init__(self, name, cards, federation=None, readable=True):
        self.name, self.federation, self.readable = name, federation, readable
        self.agents = [types.SimpleNamespace(
            title=agent, fiche=types.SimpleNamespace(data={"roles": roles}))
            for agent, roles in cards.items()]

    def owns(self, row_canon):
        return row_canon == self.name


def critical_names(rows, *, canons=(), configured_entries=(), memory=None,
                   critical_agents=()):
    cfg = types.SimpleNamespace(critical_agents=critical_agents)
    with mock.patch.object(host_lifecycle.work, "configured_canons",
                           return_value=list(canons)), \
            mock.patch.object(host_lifecycle.registry, "overview",
                              return_value=list(rows)), \
            mock.patch("ameesh.canon.configured", return_value=tuple(configured_entries)), \
            mock.patch("ameesh.sous_utilisation.critical_memory",
                       return_value=memory):
        return host_lifecycle.critical_names(cfg, None)


ROWS = [{"name": "auditeur", "canon": "home"},
        {"name": "codex1", "canon": "home"},
        {"name": "dev7", "canon": "home"}]


class CriticalNamesFallbackTest(unittest.TestCase):
    def test_un_canon_illisible_suit_le_dernier_verdict_lu(self):
        """Canon configuré mais illisible (#424 r1) : le cache tranche."""
        self.assertEqual(
            critical_names(ROWS, configured_entries=(object(),),
                           memory={"auditeur": True, "codex1": True, "dev7": False}),
            {"auditeur", "codex1"})

    def test_sans_verdict_connu_l_agent_reste_critique(self):
        """Cache absent ou nom jamais lu (#424 r6) : critique, jamais « non »."""
        self.assertEqual(
            critical_names(ROWS, configured_entries=(object(),), memory={}),
            {"auditeur", "codex1", "dev7"})

    def test_le_dernier_verdict_faux_est_respecte_et_la_surcharge_prime(self):
        """Canon illisible : le dernier verdict lu tranche, `critical_agents` aussi."""
        self.assertEqual(
            critical_names(ROWS, configured_entries=(object(),),
                           memory={"auditeur": False, "codex1": True, "dev7": False},
                           critical_agents=("dev7",)),
            {"codex1", "dev7"})

    def test_un_canon_lisible_ne_suit_pas_le_cache(self):
        """Canon lisible : la fiche décide, même contre un cache périmé."""
        canon = _Canon("home", {"orch": ["orchestrateur"], "dev7": ["implementation"]},
                       federation={"critical_roles": ["orchestrateur"]})
        self.assertEqual(
            critical_names([{"name": "orch", "canon": "home"},
                            {"name": "dev7", "canon": "home"}],
                           canons=[canon], memory={"orch": False, "dev7": True}),
            {"orch"})

    def test_l_auditeur_sans_fiche_reste_critique(self):
        """La persona auditeur l'est sans fiche tant que le rôle est critique."""
        canon = _Canon("home", {"orch": ["orchestrateur"]},
                       federation={"critical_roles": ["auditeur"]})
        self.assertEqual(
            critical_names([{"name": "auditeur", "canon": "home"},
                            {"name": "orch", "canon": "home"}],
                           canons=[canon], memory={}),
            {"auditeur"})

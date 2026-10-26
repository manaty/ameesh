# SPDX-License-Identifier: AGPL-3.0-only
"""#3643 : le drain d'hôte lit les rôles critiques du canon, pas une liste en
dur. Une organisation qui retire `relecture` de `critical_roles`
(manaty/home#179) ne compte plus ses relecteurs codex comme critiques au
retrait de l'hôte ; les rôles déclarés le restent, et `critical_agents` aussi.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import host_lifecycle, registry, storage
from .support import PgTestCase


class _Canon:
    """Canon minimal : des fiches avec rôles, et un `federation` facultatif."""

    def __init__(self, name, cards, federation=None):
        self.name, self.federation = name, federation
        self.agents = [types.SimpleNamespace(
            title=agent, fiche=types.SimpleNamespace(data={"roles": roles}))
            for agent, roles in cards.items()]

    def owns(self, row_canon):
        return row_canon == self.name


CARDS = {"orchestrateur": ["orchestrateur"], "mesh-design": ["orchestrateur", "conception"],
         "auditeur": ["auditeur"], "codex1": ["implementation", "relecture"],
         "dev7": ["implementation"]}
TROIS_ROLES = {"critical_roles": ["orchestrateur", "conception", "auditeur"]}
ROWS = [{"name": name, "canon": "home"} for name in CARDS]


def critical_names(cfg, canons, rows=ROWS):
    with mock.patch.object(host_lifecycle.work, "configured_canons",
                           return_value=list(canons)), \
            mock.patch.object(host_lifecycle.registry, "overview",
                              return_value=list(rows)):
        return host_lifecycle.critical_names(cfg, None)


class CriticalNamesTest(unittest.TestCase):
    def test_les_trois_roles_du_canon_restent_et_le_relecteur_sort(self):
        canon = _Canon("home", CARDS, federation=TROIS_ROLES)
        self.assertEqual(critical_names(types.SimpleNamespace(critical_agents=()), [canon]),
                         {"orchestrateur", "mesh-design", "auditeur"})

    def test_repli_sur_le_defaut_du_code_sans_declaration(self):
        canon = _Canon("home", CARDS)
        self.assertEqual(critical_names(types.SimpleNamespace(critical_agents=()), [canon]),
                         {"orchestrateur", "mesh-design", "auditeur", "codex1"})

    def test_liste_vide_aucun_role_critique(self):
        canon = _Canon("home", CARDS, federation={"critical_roles": []})
        self.assertEqual(critical_names(types.SimpleNamespace(critical_agents=()), [canon]),
                         set())

    def test_critical_agents_reste_la_surcharge(self):
        canon = _Canon("home", CARDS, federation={"critical_roles": []})
        self.assertEqual(critical_names(types.SimpleNamespace(critical_agents=("dev7",)),
                                        [canon]), {"dev7"})

    def test_la_fiche_d_un_autre_canon_ne_compte_pas(self):
        home = _Canon("home", {"dev7": ["implementation"]},
                      federation={"critical_roles": ["orchestrateur"]})
        autre = _Canon("autre", {"dev7": ["orchestrateur"]},
                       federation={"critical_roles": ["orchestrateur"]})
        self.assertEqual(critical_names(types.SimpleNamespace(critical_agents=()),
                                        [home, autre], [{"name": "dev7", "canon": "home"}]),
                         set())

    def test_synonyme_reviewer_reconnu_comme_relecture(self):
        canon = _Canon("home", {"rev": ["reviewer"]},
                       federation={"critical_roles": ["relecture"]})
        self.assertEqual(critical_names(types.SimpleNamespace(critical_agents=()), [canon],
                                        [{"name": "rev", "canon": "home"}]), {"rev"})


class DrainCriticalNamesTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle CASCADE")
        self.api = storage.of(self.db)
        for name in CARDS:
            registry.upsert(self.db, name, harness="deepseek", host="source",
                            cwd="/work/source")
        self.db.execute("UPDATE agent_registry SET canon='home' WHERE host='source'")
        self.api.hosts.record({"host": "source", "gate_state": "available"})

    def test_le_drain_marque_les_roles_du_canon_et_la_surcharge(self):
        canon = _Canon("home", CARDS, federation=TROIS_ROLES)
        cfg = types.SimpleNamespace(critical_agents=("dev7",))
        with mock.patch.object(host_lifecycle.work, "configured_canons",
                               return_value=[canon]):
            control = host_lifecycle.drain(cfg, self.db, "source", actor="human:op")
        rows = self.api.host_lifecycle.agents("source", control["generation"])
        self.assertEqual({row["agent"]: row["critical"] for row in rows},
                         {"orchestrateur": True, "mesh-design": True, "auditeur": True,
                          "codex1": False, "dev7": True})

    def test_le_repli_du_code_garde_le_relecteur_critique(self):
        canon = _Canon("home", CARDS)
        cfg = types.SimpleNamespace(critical_agents=())
        with mock.patch.object(host_lifecycle.work, "configured_canons",
                               return_value=[canon]):
            control = host_lifecycle.drain(cfg, self.db, "source", actor="human:op")
        rows = self.api.host_lifecycle.agents("source", control["generation"])
        self.assertTrue(next(row["critical"] for row in rows if row["agent"] == "codex1"))

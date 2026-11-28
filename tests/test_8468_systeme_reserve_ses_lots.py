# SPDX-License-Identifier: AGPL-3.0-only
"""#8468 (manaty/ameesh#6678) : un système non-agent déclaré (`system:…`, #6091)
crée les lots réservés à lui-même sans pilote — le miroir de backlog d'IMA crée
les siens sous `system:ima-backlog`. Réserver pour un autre, depuis une session
d'agent, au nom d'un système non déclaré ou retiré, ou changer une réservation
existante restent refusés."""
from __future__ import annotations

from unittest import mock

from ameesh import identity, work

from .test_l119_backlog import _Base


class SystemeReserveSesLotsTest(_Base):
    def declare(self, name="system:ima", responsible="human:proprio"):
        return work.declare_external_executor(
            self.db, name, responsible, actor="human:proprio", cfg=self.cfg)

    def add(self, executor="system:ima", actor="system:ima", **kw):
        return work.add(self.db, title="lot du miroir", app="nexlink",
                        external_executor=executor, actor=actor, cfg=self.cfg, **kw)

    def test_un_systeme_declare_cree_son_propre_lot(self):
        self.declare()
        item = self.add(unique_ref="ima-backlog:o/r#1")
        self.assertEqual(item["external_executor"], "system:ima")
        self.assertIsNone(item.get("assignee"))
        # Le rejeu de la même clé rend le même lot (#6090).
        self.assertEqual(self.add(unique_ref="ima-backlog:o/r#1")["id"], item["id"])

    def test_un_systeme_ne_reserve_pas_pour_un_autre(self):
        self.declare()
        self.declare("system:autre")
        for executor in ("system:autre", "human:proprio"):
            with self.assertRaisesRegex(work.WorkError, "neither a human"):
                self.add(executor=executor)

    def test_un_systeme_non_declare_ou_retire_est_refuse(self):
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self.add()
        self.declare()
        work.retire_external_executor(self.db, "system:ima", actor="human:proprio",
                                      cfg=self.cfg)
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self.add()

    def test_une_session_d_agent_ne_parle_pas_au_nom_d_un_systeme(self):
        self.declare()
        agent = identity.Binding(name="dev-1", source="session", ok=True)
        with mock.patch.object(identity, "resolve_binding", return_value=agent):
            with self.assertRaisesRegex(work.WorkError, "refused"):
                self.add()

    def test_changer_une_reservation_reste_un_geste_de_pilote(self):
        self.declare()
        item = self.add()
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            work.set_external_executor(self.db, item["id"], None, clear=True,
                                       actor="system:ima", cfg=self.cfg)
        cleared = work.set_external_executor(self.db, item["id"], None, clear=True,
                                             actor="human:proprio", cfg=self.cfg)
        self.assertIsNone(cleared["executor"])

# SPDX-License-Identifier: AGPL-3.0-only
"""#2789 : `agent-mail status` écrit le texte, jamais le statut.

Incident du 19/10 (impl-01, 23:29) : un agent en plein tour a lancé
`agent-mail status "…"` ; `PgBackend.set_status` posait `idle` sans bail. Le
registre l'a montré au repos pendant les 30 min de son tour : l'auditeur l'a
cru perdu et l'a relancé (`ameesh resume`, admis puisque l'agent n'était plus
`running`). 53 bascules de ce genre en 24 h, sur 19 agents."""
from __future__ import annotations

import unittest

from ameesh import registry, storage
from ameesh.backend import PgBackend

from .support import PgTestCase


class StatutLibreTest(PgTestCase):
    def _agent(self, name: str, **cols) -> None:
        registry.upsert(self.db, name, host="hote-agents")
        if cols:
            sets = ", ".join("%s = %%s" % k for k in cols)
            self.db.execute("UPDATE agent_registry SET %s WHERE name = %%s" % sets,
                            tuple(cols.values()) + (name,))

    def _ligne(self, name: str) -> dict:
        return self.db.query(
            "SELECT status, status_text, last_error, stop_reason, host, mode,"
            "       current_prompt, pending_prompt, lease_owner"
            "  FROM agent_registry WHERE name = %s", (name,))[0]

    def _tour_en_cours(self, name: str) -> None:
        self._agent(name, status="running", status_text="turn in progress",
                    current_prompt="brief du pilote", lease_owner="runner-x")
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() + interval"
                        " '5 minutes' WHERE name = %s", (name,))

    def test_un_tour_en_cours_reste_running(self):
        self._tour_en_cours("compositeur")
        PgBackend(self.cfg, self.db).set_status("compositeur", "recomposition h-2000")
        ligne = self._ligne("compositeur")
        self.assertEqual(ligne["status"], "running")
        self.assertEqual(ligne["status_text"], "recomposition h-2000")
        self.assertEqual(ligne["current_prompt"], "brief du pilote")
        self.assertEqual(ligne["lease_owner"], "runner-x")
        self.assertTrue(storage.of(self.db).leases.turn_in_progress("compositeur"))

    def test_une_reprise_reste_refusee_pendant_le_tour(self):
        # le geste de l'auditeur : sans oubli, une reprise sur un bail vivant
        # n'est admise que hors tour — le tour en cours doit rester visible
        self._tour_en_cours("compose2")
        PgBackend(self.cfg, self.db).set_status("compose2", "contrôles structurels")
        refus = storage.of(self.db).operations.resume(
            "compose2", prompt="brief du pilote", forget=False, status_text="reprise")
        self.assertIsNone(refus)
        self.assertIsNone(self._ligne("compose2")["pending_prompt"])

    def test_retenue_et_arret_gardent_leur_marqueur(self):
        self._agent("retenu", status="blocked", status_text="pressure hold: io",
                    last_error="pressure: io")
        self._agent("arrete", status="stopped", status_text="stopped by human:x: congé",
                    stop_reason="manuel")
        bk = PgBackend(self.cfg, self.db)
        for name in ("retenu", "arrete"):
            bk.set_status(name, "je travaille")
        self.assertFalse(registry.set_status_text(self.db, "retenu", "encore"))
        retenu, arrete = self._ligne("retenu"), self._ligne("arrete")
        self.assertEqual((retenu["status"], retenu["status_text"], retenu["last_error"]),
                         ("blocked", "pressure hold: io", "pressure: io"))
        self.assertEqual((arrete["status"], arrete["status_text"], arrete["stop_reason"]),
                         ("stopped", "stopped by human:x: congé", "manuel"))

    def test_au_repos_et_en_attente_le_texte_suit(self):
        self._agent("repos", status="idle")
        self._agent("attente", status="queued", pending_prompt="consigne")
        bk = PgBackend(self.cfg, self.db)
        bk.set_status("repos", "veille")
        bk.set_status("attente", "lecture")
        self.assertEqual(self._ligne("repos")["status_text"], "veille")
        attente = self._ligne("attente")
        self.assertEqual((attente["status"], attente["status_text"]), ("queued", "lecture"))

    def test_l_hote_d_un_agent_execute_ne_bouge_pas(self):
        self._agent("ailleurs", status="idle")
        self.assertNotEqual(self.cfg.host, "hote-agents")
        PgBackend(self.cfg, self.db).set_status("ailleurs", "x")
        ligne = self._ligne("ailleurs")
        self.assertEqual((ligne["host"], ligne["mode"]), ("hote-agents", "execute"))

    def test_un_nom_inconnu_nait_externe(self):
        PgBackend(self.cfg, self.db).set_status("nouveau", "première ligne")
        ligne = self._ligne("nouveau")
        self.assertEqual((ligne["mode"], ligne["status"], ligne["status_text"]),
                         ("externe", "idle", "première ligne"))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#2689 — agent idle sous bail avec du courrier non lu : ce que dit le statut.

Constat (veille mesh-design, 19/10) : impl-07 « idle (resume in 295s) » et
impl-02 « idle (instruction in 39s) », chacun avec des non-lus, sans aucun
tour pendant des heures ; la valeur semblait figée. Cause racine :

* `in 295s` / `in 39s` sont les **durées** des derniers tours (295 s pour un
  tour de reprise, 39 s pour un tour de consigne), et non le délai du
  prochain réveil — le libellé se lisait comme un compte à rebours ;
* les non-lus observés (6 annoncés chez impl-07, 5 retrouvés en base ; 2
  chez impl-02) sont tous des messages **passifs** (copies `cc`, radars de
  conflit) : L125 ne les remonte pas en tour, et l'alerte `idle_with_mail`
  les ignore à juste titre. La vue
  `agent_mesh_overview` ne les distinguait pas, d'où un « unread:6 » trompeur
  dans `agent-mail list`.

Témoin : le statut nomme le tour passé, la liste sépare le courrier actif du
passif, aucune alerte ne se lève pour du passif, et un message actif ouvre
bien un tour.
"""
from __future__ import annotations

import dataclasses
import os
import unittest

from ameesh import exploitation, mail, registry
from ameesh.runner import AgentWorker, Runner, turn_status_text

from .support import PgTestCase


class StatutDuDernierTourTest(unittest.TestCase):
    """Le libellé décrit un tour **fini**, jamais une échéance future."""

    def test_le_statut_n_est_pas_un_compte_a_rebours(self):
        texte = turn_status_text("resume", 295)
        self.assertEqual(texte, "last turn: resume, 295s")
        self.assertNotIn(" in ", texte)
        self.assertEqual(turn_status_text("instruction", 39),
                         "last turn: instruction, 39s")

    def test_les_suffixes_d_echec_et_de_cloture_sont_conserves(self):
        self.assertEqual(turn_status_text("mail", 3, ok=False),
                         "last turn: mail, 3s (failed)")
        self.assertEqual(turn_status_text("mail", 12, cloture="cap: tokens"),
                         "last turn: mail, 12s (closed: cap)")
        # une clôture n'apparaît que sur un tour abouti, comme avant
        self.assertEqual(turn_status_text("mail", 12, ok=False, cloture="cap: tokens"),
                         "last turn: mail, 12s (failed)")


class CourrierPassifAfficheTest(PgTestCase):
    """L'incident rejoué : 2 copies + 3 radars, un agent au repos sous bail."""

    def _worker(self, name: str) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        runner.mail_batch = 0.0       # aucun délai : l'actif est dû tout de suite
        runner.event_coalesce = 0.0
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _incident(self, name: str) -> None:
        """Les cinq messages que portait impl-07 : copies et radars, tous
        passifs (L125)."""
        mail.send(self.db, "mesh-design", name, "désignation dev1 + cx2",
                  kind="event", payload={"cc": "ameesh-dev1"})
        mail.send(self.db, "ameesh-cx2", name, "avis premier tour OK",
                  kind="event", payload={"cc": "mesh-design"})
        for lot in (2443, 2443, 2466):
            mail.send(self.db, "ameesh", name, "conflit de branche",
                      kind="event", work_item_id=str(lot),
                      payload={"radar": {"key": "target:%d" % lot}})
        # comme dans l'incident : arrivés bien avant l'observation
        self.db.execute(
            "UPDATE agent_mailbox SET created_at = now() - interval '1 hour' "
            "WHERE recipient = %s AND delivered_at IS NULL", (name,))

    def _ligne(self, sortie: str, name: str) -> str:
        lignes = [ligne for ligne in sortie.splitlines() if name in ligne]
        self.assertEqual(len(lignes), 1, sortie)
        return lignes[0]

    def test_l_incident_ne_doit_ni_reveiller_ni_alarmer(self):
        name = "impl-incident"
        worker = self._worker(name)
        self._incident(name)
        registry.set_status(self.db, name, "idle",
                            status_text=turn_status_text("resume", 295))

        # 1. L'exécuteur ne prend pas de tour : L125, le passif attend le tour
        #    suivant (qui viendra pour une autre raison).
        self.assertIsNone(worker.pick())
        self.assertFalse(worker.peek())

        # 2. La liste v0 garde le total (contrat documenté) et sépare le
        #    passif : « unread:5  passive:5 » ne se lit plus comme un agent
        #    en attente de tour.
        proc = self.cli("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ligne = self._ligne(proc.stdout, name)
        self.assertIn("unread:5", ligne)
        self.assertIn("passive:5", ligne)
        self.assertIn("last turn: resume, 295s", ligne)

        # 3. `ameesh list` (et son JSON, donc l'API) expose la distinction.
        proc = self.mesh("list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        [row] = [r for r in json.loads(proc.stdout) if r["name"] == name]
        self.assertEqual(row["unread"], 5)
        self.assertEqual(row["passive_unread"], 5)

        # 4. Aucune alerte `idle_with_mail` : le passif n'est pas un blocage.
        alertes = exploitation.alerts(self.cfg, self.db)
        self.assertFalse([a for a in alertes
                          if a["type"] == "idle_with_mail" and a["agent"] == name])

        # 5. Un message actif, lui, ouvre un tour immédiatement.
        du = mail.send(self.db, "dev1", name, "question qui demande un tour")
        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        self.assertIn(du, spec["ids"])
        self.assertIn("question qui demande un tour", spec["prompt"])

    def test_le_predicat_sql_suit_la_regle_l125(self):
        """La vue et `mail.passive_reason` classent les mêmes charges."""
        name = "impl-predicat"
        self._worker(name)
        charges = [
            ("ack", {"ack": True}),
            ("cc", {"cc": "dev1"}),
            ("diffusion", {"broadcast": True}),
            ("radar", {"radar": {"key": "pair:1-2"}}),
            ("échec CI passif", {"ci_passif": True}),
            ("ordinaire", {}),
            ("urgent", {"urgent": True}),
            ("urgent + copie", {"urgent": True, "cc": "dev1"}),
        ]
        for index, (libelle, payload) in enumerate(charges):
            mail.send(self.db, "dev1", name, libelle, kind="event",
                      payload=payload, thread=False)
        [row] = [r for r in registry.overview(self.db) if r["name"] == name]
        passifs = sum(1 for payload in (charge for _l, charge in charges)
                      if mail.passive_reason({"payload": payload}))
        self.assertEqual(passifs, 5)  # ack, cc, diffusion, radar, échec CI
        self.assertEqual((row["unread"], row["passive_unread"]),
                         (len(charges), passifs))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#2789 : `agent-mail status` ne pose plus `idle` sur un agent en tour.

19/10 23:29 : impl-01, en plein tour sur le brief d'un `ameesh restart`, a
posé son texte d'état ; la ligne est passée `running` → `idle` sans bail
rendu ni fin de tour. L'agent a paru au repos 25 min alors que son tour
tournait (jusqu'à 23:58), et `ameesh resume` a accepté une consigne de plus
sur un agent en tour. Le statut d'un agent `execute` n'appartient qu'à son
exécuteur ; `agent-mail status` n'y écrit que le texte."""
from __future__ import annotations

from ameesh import registry
from ameesh.backend import PgBackend

from .support import PgTestCase


class StatusEnTourTest(PgTestCase):
    def ligne(self, name: str) -> dict:
        return self.db.query(
            "SELECT status, status_text, current_prompt, pending_prompt, lease_owner,"
            " lease_epoch, last_error, stop_reason FROM agent_registry WHERE name = %s",
            (name,))[0]

    def en_tour(self, name: str) -> None:
        registry.upsert(self.db, name, chantier="p1")
        self.db.execute(
            "UPDATE agent_registry SET mode = 'execute', status = 'running',"
            " status_text = 'turn in progress', current_prompt = 'le brief',"
            " lease_owner = 'runner-x', lease_epoch = 7,"
            " lease_expires_at = clock_timestamp() + interval '5 minutes'"
            " WHERE name = %s", (name,))

    def test_agent_en_tour_garde_running_bail_et_consigne(self):
        self.en_tour("impl")
        PgBackend(self.cfg, self.db).set_status("impl", "recompose h-2000")
        row = self.ligne("impl")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["status_text"], "recompose h-2000")
        self.assertEqual(row["current_prompt"], "le brief")
        self.assertEqual((row["lease_owner"], row["lease_epoch"]), ("runner-x", 7))
        # le tour paraît toujours en cours : une reprise n'empile rien dessus
        self.assertTrue(self.db.query(
            "SELECT status = 'running' AS en_cours FROM agent_registry"
            " WHERE name = 'impl'")[0]["en_cours"])

    def test_queued_reste_queued(self):
        registry.upsert(self.db, "file", chantier="p1")
        self.db.execute("UPDATE agent_registry SET status = 'queued',"
                        " pending_prompt = 'à faire' WHERE name = 'file'")
        PgBackend(self.cfg, self.db).set_status("file", "texte")
        row = self.ligne("file")
        self.assertEqual((row["status"], row["status_text"], row["pending_prompt"]),
                         ("queued", "texte", "à faire"))

    def test_blocage_et_arret_gardent_leur_marqueur(self):
        for name, status in (("bloque", "blocked"), ("arrete", "stopped")):
            registry.upsert(self.db, name, chantier="p1")
            self.db.execute(
                "UPDATE agent_registry SET status = %s, status_text = 'marqueur',"
                " last_error = 'cause' WHERE name = %s", (status, name))
            PgBackend(self.cfg, self.db).set_status(name, "texte de l'agent")
            row = self.ligne(name)
            self.assertEqual((row["status"], row["status_text"], row["last_error"]),
                             (status, "marqueur", "cause"), name)

    def test_agent_externe_garde_la_regle_d_avant(self):
        registry.upsert(self.db, "humain-shell", chantier="p1")
        self.db.execute("UPDATE agent_registry SET mode = 'externe', status = 'blocked',"
                        " last_error = 'x' WHERE name = 'humain-shell'")
        PgBackend(self.cfg, self.db).set_status("humain-shell", "en ligne")
        row = self.ligne("humain-shell")
        self.assertEqual((row["status"], row["status_text"], row["last_error"]),
                         ("idle", "en ligne", None))

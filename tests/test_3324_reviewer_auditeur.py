# SPDX-License-Identifier: AGPL-3.0-only
"""#3324 — l'auditeur peut désigner et retirer un relecteur (fiche 24/10).

Constat (auditeur, 24/10) : sa fiche, signée par le bureau, range la
redésignation d'un relecteur (`ameesh work reviewer`) au niveau 1, mais le code
la réserve aux humains, orchestrateurs et agents de conception. Cas réel :
#3297, dont le relecteur était mort avec pc-smichea ; il a fallu attendre le
pilote.

Témoins : l'auditeur (session liée, puis `--actor`) désigne et retire ; un
agent ordinaire reste refusé ; le plancher d'avis (`--floor`) reste un geste de
pilote, y compris pour l'auditeur ; le rôle d'auditeur se lit aussi sur la
fiche Agent du canon, pas seulement sur le nom de la persona.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import canon, registry, work

from .support import PgTestCase


class ReviewerAuditorTest(PgTestCase):
    def _lot(self, assignee="auteur"):
        self.register(assignee, "deepseek", cwd=self.tmp)
        self.register("chef", "claude", cwd=self.tmp)
        self.register("relecteur", "codex", cwd=self.tmp)
        return work.add(self.db, title="un lot", assignee=assignee)

    def _env(self, **extra):
        # `chef` reste l'orchestrateur déclaré : les autres témoins ne doivent
        # pas dépendre d'une identité de session héritée du lanceur.
        return self.env(AMEESH_ALERT_ORCHESTRATORS="chef", **extra)

    def _qa(self, lot):
        proc = self.mesh("work", "move", str(lot["id"]), "build", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa", "--actor", "chef",
                         env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_l_auditeur_designe_puis_retire(self):
        self.register("auditeur", "claude", cwd=self.tmp)
        lot = self._lot()
        self._qa(lot)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "relecteur",
                         "--actor", "auditeur", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer relecteur", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer relecteur designated" in n for n in notes), notes)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--remove",
                         "--actor", "auditeur", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("designation removed", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")

    def test_la_session_liee_a_l_auditeur_designe(self):
        """La fiche autorise l'auditeur ; sa session n'a pas à déclarer un
        orchestrateur en plus (`--actor` absent)."""
        self.register("auditeur", "claude", cwd=self.tmp)
        lot = self._lot()
        self._qa(lot)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "relecteur",
                         env=self._env(AGENT_MAIL_NAME="auditeur"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")

    def test_un_agent_ordinaire_reste_refuse(self):
        self.register("ordinaire", "claude", cwd=self.tmp)
        lot = self._lot()
        self._qa(lot)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "relecteur",
                         "--actor", "ordinaire", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("designation refused", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--remove",
                         "--actor", "ordinaire", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("designation refused", proc.stderr)

    def test_le_plancher_d_avis_reste_au_pilote(self):
        self.register("auditeur", "claude", cwd=self.tmp)
        lot = self._lot()
        self._qa(lot)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--floor", "2",
                         "--reason", "clés et prod", "--actor", "auditeur",
                         env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("review floor refused", proc.stderr)
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertFalse(any(str(n).startswith("reviews-floor:") for n in notes), notes)

    def test_l_auditeur_designe_a_l_entree_en_revue(self):
        """Même geste à l'entrée en `qa` (`work move … --reviewer`) : deux
        commandes, une seule autorité."""
        self.register("auditeur", "claude", cwd=self.tmp)
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "relecteur", "--actor", "auditeur",
                         env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")


class AuditorRoleTest(unittest.TestCase):
    """Le rôle se lit sur la fiche Agent du canon, pas seulement sur le nom."""

    def test_role_auditeur_du_canon(self):
        card = types.SimpleNamespace(title="veille", roles=["auditeur"])
        loaded = types.SimpleNamespace(agents=[card])
        cfg = types.SimpleNamespace()
        with mock.patch.object(registry, "overview",
                               return_value=[{"name": "veille"}, {"name": "auteur"}]), \
                mock.patch.object(canon, "load_configured", return_value=[loaded]):
            self.assertTrue(work.is_auditor(cfg, None, "veille"))
            self.assertFalse(work.is_auditor(cfg, None, "auteur"))

    def test_une_persona_ordinaire_n_est_pas_auditrice(self):
        cfg = types.SimpleNamespace()
        with mock.patch.object(registry, "overview",
                               return_value=[{"name": "auteur"}]), \
                mock.patch.object(canon, "load_configured", return_value=[]):
            self.assertFalse(work.is_auditor(cfg, None, "auteur"))

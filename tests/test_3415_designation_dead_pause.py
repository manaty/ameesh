# SPDX-License-Identifier: AGPL-3.0-only
"""#3415 — ni un agent mort ni un agent sous pause opérateur n'est désigné
relecteur automatiquement.

Constat : `review_designation.is_available` ne lisait que `stopped`/`paused` et
les préfixes de `status_text`. Deux cas passaient :

- un agent reapé `dead` (bail expiré) : le gel #18165 a désigné
  pilot-dryrun-3389, mort depuis ~21:35Z ;
- un agent ordinaire sous pause opérateur : le registre garde `status` `idle`
  et un texte « last turn: mail », qui ne commence pas par « paused ». Le gel
  #18160 a désigné pilot-resident-3389, sous drain depuis 22:01Z.

La pause opérateur est une **colonne** (`operator_paused`), pas un statut : la
garde la lit dans la ligne du registre, jamais dans le texte. `dead` entre dans
l'ensemble non vivant partagé avec la réassignation (#3328), pour qu'un statut
mort ne soit ni désigné ni laissé en siège mort.

Témoins : un par cas sans base, puis au gel sur un dépôt réel (le remplaçant de
la même famille est désigné), et l'attente explicite quand la famille du mort
n'a plus personne.
"""
from __future__ import annotations

import unittest

from ameesh import registry
from ameesh import review_designation as rd

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture, register_agent

#: une ligne du registre telle que `registry.overview` la rend (#3415)
def row(name, *, harness="claude", team="ameesh", status="idle", mode="execute",
        text="", operator_paused=False,
        capabilities=("read", "report-drift", "propose"), roles=("implementation",)):
    return {"name": name, "team": team, "harness": harness,
            "session_harness": "", "status": status, "mode": mode,
            "status_text": text, "operator_paused": operator_paused,
            "capabilities": list(capabilities), "roles": list(roles)}


class SelectionTest(unittest.TestCase):
    """La disponibilité écarte les deux cas (aucune base).

    `candidates_from_registry` porte la disponibilité dans
    `Candidate.available` : la sélection (`rd.select`) ne retient que les
    candidats disponibles. Les témoins unitaires visent la garde elle-même ;
    les témoins de gel ci-dessous prouvent la désignation de bout en bout.
    """

    def test_un_agent_dead_est_indisponible(self):
        self.assertFalse(rd.is_available(row("dead-1", status="dead",
                                             text="lease expired")))
        self.assertTrue(rd.is_available(row("alive-1")))

    def test_un_agent_sous_pause_operateur_est_indisponible(self):
        # Le statut reste `idle` et le texte « last turn: mail » : ni
        # `UNAVAILABLE_STATUS` ni les préfixes de pause ne le voient. La
        # colonne `operator_paused`, si.
        self.assertFalse(rd.is_available(row("drain-1", text="last turn: mail",
                                             operator_paused=True)))
        self.assertTrue(rd.is_available(row("alive-2")))

    def test_la_pause_operateur_n_est_pas_devinee_au_texte(self):
        # Un agent ordinaire dont le texte commence par « paused » est déjà
        # écarté par les préfixes ; la garde de #3415 ne dépend pas de ce
        # texte : `operator_paused` vrai suffit, même avec un texte neutre.
        self.assertFalse(rd.is_available(row("drain-2", text="",
                                             operator_paused=True)))
        self.assertTrue(rd.is_available(row("alive-3")))


class FreezeDeadPauseTest(_ActivationFixture, PgTestCase):
    """Au gel, le siège d'un mort ou d'un pausé n'est pas déposé."""

    def _agent(self, name, harness):
        # #4779 : profil de relecteur prouvé (propose + un rôle).
        register_agent(self.db, name, harness=harness, host=self.cfg.host,
                       cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (name,))

    def _dead(self, name, text="lease expired"):
        self.db.execute(
            "UPDATE agent_registry SET status = 'dead', status_text = %s"
            " WHERE name = %s", (text, name))

    def _operator_paused(self, name, text="last turn: mail"):
        self.db.execute(
            "UPDATE agent_registry SET operator_paused = true,"
            " operator_pause_reason = 'drain', operator_pause_by = 'operator',"
            " operator_pause_for = 'host', status = 'idle', status_text = %s"
            " WHERE name = %s", (text, name))

    def test_un_agent_dead_est_remplace_par_sa_famille(self):
        self._agent("r3", "claude")
        self._dead("r1")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(candidate["name"] for candidate in out["reviewers"]),
                         ["r2", "r3"])
        self.assertEqual(sorted(mail["recipient"] for mail in self.mailbox()),
                         ["r2", "r3"])

    def test_un_agent_dead_seul_dans_sa_famille_fait_attendre(self):
        # Aucun autre claude : le siège n'est pas déposé chez un mort ; la
        # famille codex disponible est pourvue et l'attente du second avis est
        # explicite, avec la notification de capacité (#3596).
        self._dead("r1")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r2"])
        self.assertEqual(out["missing"], 1)
        self.assertEqual([mail["recipient"] for mail in self.mailbox()],
                         ["pilot", "r2"])

    def test_un_agent_ordinaire_en_pause_operateur_n_est_pas_designe(self):
        # pilot-resident-3389 (gel #18160) : ordinaire, drainé, statut resté
        # `idle`, texte « last turn: mail ». Le gel doit prendre r3, pas r1.
        self._agent("r3", "claude")
        self._operator_paused("r1")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(candidate["name"] for candidate in out["reviewers"]),
                         ["r2", "r3"])
        self.assertEqual(sorted(mail["recipient"] for mail in self.mailbox()),
                         ["r2", "r3"])
        self.assertNotIn("r1", [candidate["name"] for candidate in out["reviewers"]])


if __name__ == "__main__":
    unittest.main()

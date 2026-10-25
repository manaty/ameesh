# SPDX-License-Identifier: AGPL-3.0-only
"""#3480 — un agent éphémère au repos n'est ni preneur de travail ni capacité.

#3411 a écarté les éphémères de la désignation des relecteurs **et** inséré
une garde dans la prise automatique (`backlog.auto_take`). Le 25/10 à 02:47Z,
un éphémère de mesure (`pilot-resident-3389-codex`, créé à 02:18Z pour #3389)
a pourtant pris le panier #3456 : la passe de prise venait d'un exécuteur dont
le code précédait #3411, et le reste du mesh continuait de le nommer comme
capacité au repos (`idle_capacity` le listait encore, vérifié en direct).

La règle est donc portée par le calcul du repos lui-même
(`sous_utilisation.idle_agents`) : tout appelant qui demande « qui est au
repos » — prise de la file (L119) et des lots d'équipe (L165), propositions
#1677, remise L233b, alertes `idle_capacity` et `backlog_empty` — hérite de
l'exclusion. Un éphémère n'est pas une capacité : c'est un instrument de
mesure.

Témoins :
- prise : un éphémère au repos, avec un panier ouvert, ne prend rien pendant
  DEUX passages ; un agent ordinaire, lui, prend toujours ;
- L165 : même chose pour un lot d'équipe sans preneur ;
- alerte : un éphémère au repos ne figure ni dans `idle_capacity` ni dans
  `backlog_empty`, l'ordinaire y figure (contrôle positif).

La désignation manuelle d'un éphémère reste possible : la garde est dans le
calcul du repos, jamais dans l'écriture d'un siège ou d'une assignation à la
main (même partage que #3411).
"""
from __future__ import annotations

import time
import unittest

from ameesh import storage, sous_utilisation, work

from .test_l119_backlog import HEURE
from .test_l165_prise_lots import _Lots


class _Ephemeres(_Lots):
    def ephemeral(self, name, *, harness="deepseek", expires_s=3600,
                  since_s=5 * HEURE):
        """Un agent éphémère au repos, comme `ameesh agent spawn --ttl`."""
        self.agent(name, harness, team="nexlink", since_s=since_s)
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = 'parent',"
            " ephemeral_expires_at = now() + make_interval(secs => %s)"
            " WHERE name = %s", (expires_s, name))

    def listing(self):
        return storage.of(self.db).operations.listing()


class PriseEphemereDeuxPassagesTest(_Ephemeres):
    """Le témoin du lot : deux passages ne suffisent pas à faire prendre."""

    def test_un_ephemere_ne_prend_pas_un_panier_pendant_deux_passages(self):
        # L119 : le panier #3456 (valeur 15) pris à 02:47Z par un éphémère.
        self.ephemeral("pilot-resident-3389-codex")
        item = self.element("dette", 90)
        self.assertEqual(self.take(), [])
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])

        # contrôle positif : la même file, le même passage, un agent ordinaire
        self.agent("claude1", team="nexlink")
        self.assertEqual([(taken["agent"], taken["kind"], taken["item"]["id"])
                          for taken in self.take()],
                         [("claude1", "backlog", item["id"])])
        self.assertEqual(work.get(self.db, item["id"])["assignee"], "claude1")

    def test_un_ephemere_ne_prend_pas_un_lot_d_equipe_pendant_deux_passages(self):
        # L165 : le lot sans preneur de sa propre équipe reste libre.
        self.ephemeral("pilot-resident-3389-codex")
        lot = self.lot("lot sans preneur")
        self.assertEqual(self.take(), [])
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

        self.agent("claude1", team="nexlink")
        self.assertEqual([(taken["agent"], taken["item"]["id"]) for taken in self.take()],
                         [("claude1", lot["id"])])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")

    def test_un_ephemere_expire_ne_prend_rien_non_plus(self):
        # L'échéance ne rend pas preneur : il n'est jamais au repos.
        self.ephemeral("pilot-dryrun-3389", expires_s=-3600)
        item = self.element("dette", 90)
        self.assertEqual(self.take(), [])
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])


class AlerteDeReposTest(_Ephemeres):
    """Un éphémère n'est pas de la capacité au repos (constat #3480)."""

    def test_idle_capacity_n_annonce_pas_un_ephemere(self):
        self.ephemeral("pilot-resident-3389-codex")
        lot = self.lot("lot sans preneur")
        alertes = sous_utilisation.idle_capacity(
            self.db, self.listing(), time.time(), ["orchestrateur"])
        self.assertEqual(alertes, [], "un éphémère ne constitue pas une capacité")
        # contrôle positif : le même lot, un agent ordinaire au repos
        self.agent("claude1", team="nexlink")
        alertes = sous_utilisation.idle_capacity(
            self.db, self.listing(), time.time(), ["orchestrateur"])
        self.assertEqual([a["agents"] for a in alertes], [["claude1"]])
        self.assertEqual(alertes[0]["lots"], [lot["id"]])

    def test_backlog_empty_n_annonce_pas_un_ephemere(self):
        self.ephemeral("pilot-resident-3389-codex")
        self.assertEqual(
            sous_utilisation.backlog_empty(self.db, self.listing(), time.time()), [])
        self.agent("claude1", team="nexlink")
        alertes = sous_utilisation.backlog_empty(
            self.db, self.listing(), time.time())
        self.assertEqual([a["agents"] for a in alertes], [["claude1"]])

    def test_le_panier_ouvert_est_la_raison_de_l_alerte_pas_l_ephemere(self):
        # Le panier ouvert explique pourquoi l'alerte existe (#119) ; sans
        # agent ordinaire au repos, elle ne doit pas exister quand même.
        self.ephemeral("pilot-resident-3389-codex")
        self.element("dette", 90)
        self.assertEqual(
            sous_utilisation.backlog_empty(self.db, self.listing(), time.time()), [])
        self.assertEqual(
            sous_utilisation.idle_capacity(
                self.db, self.listing(), time.time(), ["orchestrateur"]), [])


class GardeCentraleTest(unittest.TestCase):
    """La garde est dans `idle_agents`, pas seulement dans un appelant."""

    def test_idle_agents_ecarte_les_ephemeres_sans_toucher_aux_ordinaires(self):
        now = time.time()

        def row(name, ephemeral):
            return {"name": name, "ephemeral": ephemeral, "mode": "execute",
                    "status": "idle", "lease_owner": "runner-%s@h" % name,
                    "lease_live": True, "unread": 0, "actionable_lots": 0,
                    "last_turn_ts": now - 2 * HEURE, "status_since_ts": now - 2 * HEURE}

        idle = sous_utilisation.idle_agents(
            [row("pilot-resident-3389-codex", True),
             row("pilot-dryrun-3389", True),
             row("claude1", False)],
            now, HEURE, actionable_idle=True)
        self.assertEqual([r["name"] for r in idle], ["claude1"])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#3411 — un agent éphémère de mesure (`ameesh agent spawn --ttl`) n'est ni
désigné relecteur automatiquement, ni servi par la prise automatique.

Deux témoins, un par voie :

- désignation : un éphémère vivant et un éphémère expiré de la famille
  éligible, plus un agent ordinaire chargé ; l'ordinaire est désigné. Sans
  aucun ordinaire, l'attente est explicite — jamais le repli sur un éphémère ;
- prise : seuls des éphémères au repos (un vivant, un expiré) → rien n'est
  pris, ni un lot d'équipe sans preneur (L165) ni un élément de la file
  (L119, la prise de #3403). Un agent ordinaire, lui, prend toujours.

La désignation manuelle d'un éphémère reste possible : la garde est dans le
choix des candidats automatiques, pas dans le port ni dans la réassignation.
"""
from __future__ import annotations

import time
import unittest

from ameesh import review_designation as rd
from ameesh import work

from .test_l165_prise_lots import _Lots

#: #2451 : familles explicites ; l'activation passe celles du canon compilé
FAMILIES = ("claude", "codex", "deepseek")

#: l'auteur du gel dans les témoins de désignation
AUTHOR = [{"name": "mty-amh-impl-02", "family": "deepseek"}]


def row(name, *, ephemeral=False, expires=None, harness="claude", team="ameesh",
        status="idle", mode="execute", text="",
        capabilities=("read", "report-drift", "propose"), roles=("implementation",)):
    """Une ligne du registre telle que `registry.overview` la rend."""
    return {"name": name, "team": team, "harness": harness, "session_harness": "",
            "status": status, "mode": mode, "status_text": text,
            "ephemeral": ephemeral, "ephemeral_expires_ts": expires,
            "capabilities": list(capabilities), "roles": list(roles)}


def candidates(rows, *, loads=None):
    return rd.candidates_from_registry(rows, families=FAMILIES, project="ameesh",
                                       loads=loads)


class DesignationEphemereTest(unittest.TestCase):
    """La sélection automatique ne choisit jamais un éphémère (aucune base)."""

    def test_un_ephemere_vivant_ou_expire_n_est_pas_candidat(self):
        now = time.time()
        pool = candidates([
            row("pilot-resident-3389", ephemeral=True, expires=now + 3600),
            row("pilot-dryrun-3389", ephemeral=True, expires=now - 3600),
            row("ameesh-dev3"),
        ])
        self.assertEqual([candidate.name for candidate in pool], ["ameesh-dev3"])

    def test_un_ordinaire_charge_est_designe_plutot_qu_un_ephemere(self):
        # L'éphémère, moins chargé et au repos, serait choisi sans la garde :
        # même famille, charge nulle. Le gel #18128 de #3409 a désigné ainsi
        # pilot-resident-3389 (auditeur, 24/10).
        now = time.time()
        pool = candidates([
            row("pilot-resident-3389", ephemeral=True, expires=now + 3600),
            row("pilot-dryrun-3389", ephemeral=True, expires=now - 3600),
            row("ameesh-dev3"),
        ], loads={"ameesh-dev3": 3})
        out = rd.select(required=1, authors=AUTHOR, candidates=pool, families=FAMILIES)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["reviewers"], [{"name": "ameesh-dev3", "family": "claude"}])

    def test_sans_ordinaire_l_attente_est_explicite(self):
        # Même avec deux familles éligibles, un éphémère seul ne couvre pas un
        # avis : l'attente est dite, pas contournée par un repli.
        now = time.time()
        pool = candidates([
            row("pilot-resident-3389", ephemeral=True, expires=now + 3600),
            row("pilot-dryrun-3389", ephemeral=True, expires=now - 3600),
        ])
        out = rd.select(required=1, authors=AUTHOR, candidates=pool, families=FAMILIES)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(out["reviewers"], [])
        self.assertEqual(out["missing"], 1)

    def test_une_designation_manuelle_d_un_ephemere_reste_valable(self):
        # La garde ne juge que les candidats automatiques : un siège écrit à
        # la main reste compté (le pilote peut le retirer, pas nous).
        out = rd.select(required=1, authors=AUTHOR, candidates=[],
                        already=[{"name": "pilot-resident-3389", "family": "claude"}],
                        families=FAMILIES)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["manual"], [{"name": "pilot-resident-3389",
                                          "family": "claude"}])


class PriseEphemereTest(_Lots):
    """La prise automatique ne sert jamais un éphémère, vivant ou expiré."""

    def ephemeral(self, name, *, harness="deepseek", expires_s=3600,
                  since_s=5 * 3600):
        self.agent(name, harness, team="nexlink", since_s=since_s)
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true, created_by = 'parent',"
            " ephemeral_expires_at = now() + make_interval(secs => %s)"
            " WHERE name = %s", (expires_s, name))

    def test_les_ephemeres_ne_prennent_pas_le_lot_de_leur_equipe(self):
        # L165 : le lot sans preneur reste libre ; ni le vivant (le plus
        # ancien au repos, donc choisi sans la garde) ni l'expiré ne le prennent.
        self.ephemeral("pilot-resident-3389")
        self.ephemeral("pilot-dryrun-3389", expires_s=-3600)
        lot = self.lot("lot sans preneur")
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_les_ephemeres_ne_prennent_pas_la_file(self):
        # L119 : la prise de #3403 (valeur 25) par pilot-resident-3389, au
        # journal « prise automatique par … (valeur 25) ».
        self.ephemeral("pilot-resident-3389")
        item = self.element("dette", 90, team="nexlink")
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])

    def test_un_agent_ordinaire_prend_toujours_le_lot(self):
        self.ephemeral("pilot-resident-3389")
        self.agent("claude1", team="nexlink", since_s=3600)
        lot = self.lot("lot sans preneur")
        out = self.take()
        self.assertEqual([(taken["agent"], taken["item"]["id"]) for taken in out],
                         [("claude1", lot["id"])])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")

    def test_un_agent_ordinaire_prend_toujours_la_file(self):
        self.ephemeral("pilot-resident-3389")
        self.agent("claude1", team="nexlink", since_s=3600)
        item = self.element("dette", 90, team="nexlink")
        out = self.take()
        self.assertEqual([(taken["agent"], taken["kind"], taken["item"]["id"])
                          for taken in out],
                         [("claude1", "backlog", item["id"])])
        self.assertEqual(work.get(self.db, item["id"])["assignee"], "claude1")

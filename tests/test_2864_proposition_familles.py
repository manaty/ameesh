# SPDX-License-Identifier: AGPL-3.0-only
"""#2864 — la proposition de relecture écarte les familles des auteurs de la plage.

Constat du 20/10 (#2848) : un lot empilé sur une base codex (#2845), assigné
deepseek, a reçu une proposition de relecture pour un agent codex. La
désignation au gel, elle, attendait (« 1 eligible family (claude), forbidden
codex, deepseek ») et la porte (#2726) aurait refusé l'avis : la proposition ne
lisait que la famille de l'assigné au gel, pas les autres auteurs de la plage.

La proposition lit désormais la marque `review-designation` du dernier gel — la
lecture exacte de la désignation (`review_authors.reading`) — et écarte toutes
les familles d'auteur. Sans lecture durable (pas de marque pour ce gel, famille
d'auteur inconnue), elle ne propose rien : l'indépendance ne se suppose pas.

#3596 : le gel pourvoit désormais le siège disponible et lui écrit. Pour que
les témoins ci-dessous lisent vraiment la marque (et non la garde SQL qui
écarte un lot déjà muni d'un relecteur), le siège indépendant est rendu
indisponible au gel puis disponible : la proposition de #2864 est alors la
seule à pouvoir le désigner.
"""
from __future__ import annotations

import json
import time

from ameesh import backlog, plan_git, registry
from ameesh import review_designation as rd
from ameesh import review_designation_pg as pg
from ameesh import storage, work

from .test_2447_review_designation_activation import _ActivationFixture
from .support import PgTestCase


class _Stacked(_ActivationFixture):
    """Une plage empilée : main -> base codex (#2845) -> lot deepseek (#2848)."""

    def setUp(self):
        super().setUp()
        # #3742 : ce témoin empile deux familles d'auteurs avec deux avis
        # requis ; le gel refuse désormais une telle tête. La famille
        # supplémentaire, sans agent disponible, garde la tête relisible
        # (deux familles éligibles) et la désignation en attente, ce que ces
        # témoins vérifient.
        self.four_families()
        # main : le commit de base du dépôt, hors de la plage du lot.
        self.repo.write("src/ameesh/work.py", "main\n")
        self.base = self.repo.commit("main")
        # #2845, empilé : son commit est DANS la plage du lot suivant.
        self.repo.write("src/ameesh/base.py", "base codex\n")
        self.codex_base = self.repo.commit("base lot", agent="cx2")
        # #2848 : le lot relu, empilé sur la base codex.
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.frozen = self.repo.commit("lot", agent="author")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.db.execute("DELETE FROM provenance_receipts")
        self.receipts(actor="cx2", harness="codex", commits=[self.codex_base])
        self.receipts(actor="author", harness="deepseek", commits=[self.frozen])
        # Le candidat codex erroné du constat ; r1 (claude) est déjà au banc.
        registry.upsert(self.db, "cx1", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', status_since ="
            " now() - interval '2 hours', last_turn_at = now() - interval '2 hours',"
            " lease_owner = 'runner-' || name || '@h',"
            " lease_expires_at = now() + interval '1 hour',"
            " responsible = 'human:proprio' WHERE team = 'ameesh'")
        work.move(self.db, self.lot, "qa")

    def take(self):
        return backlog.auto_take(
            self.cfg, self.db, now=time.time(), paid=("deepseek",),
            budget_check=lambda name: "", orchestras=[])

    def mails(self, recipient):
        return self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = %s ORDER BY id",
            (recipient,))

    def marque(self, authors, *, freeze=None):
        freeze = freeze or plan_git.latest_freeze(work.milestones(self.db, self.lot))
        note = pg._mark_note(
            self.lot, int(freeze["id"]), state=rd.WAITING, required=2,
            reviewers=[], missing=2, why="test : désignation en attente",
            authors=authors, policy_ref="",
            sha=str(freeze.get("sha") or self.frozen))
        storage.of(self.db).work.note(self.lot, "qa", note, "test")

    def retire_marques(self):
        self.db.execute(
            "DELETE FROM work_item_events WHERE work_item_id = %s"
            " AND starts_with(note, %s)", (self.lot, pg.MARK_PREFIX))

    def marque_sans_inconnus(self, authors):
        """Une marque d'avant #2864 : les auteurs connus, pas la liste des
        commits non attribués. Sa complétude n'est pas prouvée."""
        freeze = plan_git.latest_freeze(work.milestones(self.db, self.lot))
        payload = {
            "state": rd.WAITING, "required": 2, "reviewers": [], "missing": 2,
            "why": "test : marque antérieure au champ unknown_authors",
            "freeze": {"id": int(freeze["id"]),
                       "sha": str(freeze.get("sha") or self.frozen)},
            "authors": [dict(row) for row in authors], "policy_ref": "", "sent": [],
        }
        note = "%s%d:%d: %s" % (pg.MARK_PREFIX, self.lot, int(freeze["id"]),
                                json.dumps(payload, ensure_ascii=False, sort_keys=True))
        storage.of(self.db).work.note(self.lot, "qa", note, "test")


class PropositionFamillesTest(_Stacked, PgTestCase):
    def freeze_sans_siege(self):
        """Gèle le lot sans siège : le seul relecteur indépendant (r1, claude)
        est indisponible au gel, puis rendu au repos.

        Sans cela, la désignation partielle de #3596 pourvoit r1 au gel et la
        garde SQL de `review_proposals` (`reviewer` non vide, postgres/work.py)
        écarte le lot avant toute lecture des marques : les témoins négatifs de
        #2864 passeraient sans rien prouver. Ici la colonne reste vide, `take()`
        lit la marque, et la proposition est la seule à pouvoir désigner r1."""
        self.db.execute(
            "UPDATE agent_registry SET status = 'dead' WHERE name = 'r1'")
        _row, outcome = self.freeze()
        self.assertEqual(outcome["state"], rd.WAITING, outcome)
        self.assertEqual(outcome["reviewers"], [], outcome)
        self.assertEqual(self.mails("r1"), [], "aucun siège pourvu au gel")
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle',"
            " status_since = now() - interval '2 hours' WHERE name = 'r1'")
        return outcome

    def test_le_lot_empile_propose_la_famille_independante(self):
        self.freeze_sans_siege()
        mark = pg.designation_of(self.marks()[0])
        self.assertEqual({row["name"]: row["family"] for row in mark["authors"]},
                         {"author": "deepseek", "cx2": "codex"})
        self.assertIn("forbidden", mark["why"])
        self.assertFalse(work.get(self.db, self.lot).get("reviewer"),
                         "le gel sans siège ne pourvoit personne")

        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("review", "r1", self.lot)])
        self.assertEqual(self.mails("cx1"), [], "codex est auteur de la plage")
        self.assertEqual(len(self.mails("r1")), 1)
        self.assertEqual(work.get(self.db, self.lot)["reviewer"], "r1")

    def test_sans_marque_de_designation_aucune_proposition(self):
        self.freeze_sans_siege()
        self.retire_marques()
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

    def test_provenance_partiellement_inconnue_ne_propose_rien(self):
        # B/P1 de la revue cx3 : des auteurs connus ne prouvent pas que tous
        # les commits le sont. Un commit d'un agent hors registre, sans reçu,
        # rend la désignation waiting (unknown) ; aucune proposition ne doit
        # en sortir.
        self.repo.write("src/ameesh/third.py", "third\n")
        self.frozen = self.repo.commit("third party", agent="unregistered")
        _row, outcome = self.freeze()
        self.assertEqual(outcome["state"], rd.WAITING, outcome)
        self.assertIn("author provenance unknown", outcome["why"])
        mark = pg.designation_of(self.marks()[0])
        self.assertEqual(mark["unknown_authors"], [self.frozen[:12]])
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

    def test_une_marque_sans_liste_inconnue_ne_prouve_rien(self):
        self.freeze_sans_siege()
        # La marque la plus récente ne porte pas la liste de complétude : la
        # lecture ne prouve pas l'absence d'auteur inconnu, donc rien n'est
        # proposé (échec fermé), même avec des familles connues.
        self.marque_sans_inconnus([{"name": "author", "family": "deepseek"},
                                   {"name": "cx2", "family": "codex"}])
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

    def test_une_famille_d_auteur_inconnue_ne_propose_rien(self):
        self.freeze_sans_siege()
        self.marque([{"name": "author", "family": ""},
                     {"name": "cx2", "family": "codex"}])
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

    def test_la_marque_d_un_gel_anterieur_ne_couvre_pas_le_nouveau(self):
        self.freeze_sans_siege()
        # nouveau gel sans désignation : la lecture du gel courant manque.
        # Posé au stockage, ce témoin lit les marques, pas la porte du gel.
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="b" * 40, actor="author", verdict=None, note="")
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

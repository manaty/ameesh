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

#3596 : le gel pourvoit désormais le siège disponible et lui écrit ; les
témoins ci-dessous vérifient puis consomment ce courrier de désignation du gel,
et ne comptent ensuite que ce que la proposition ajoute.
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
    def consommer_designation_au_gel(self):
        """#3596 : le gel pourvoit le siège indépendant disponible (claude,
        r1) et lui écrit. Les témoins de #2864 ne comptent ensuite que le
        courrier de la proposition : on vérifie la désignation du gel puis on
        vide la boîte du relecteur."""
        mails = self.mails("r1")
        self.assertEqual(len(mails), 1, mails)
        self.assertIn("Automatic designation at freeze", mails[0]["body"])
        self.db.execute("DELETE FROM agent_mailbox WHERE recipient = %s", ("r1",))

    def test_le_lot_empile_designe_la_famille_independante(self):
        _row, outcome = self.freeze()
        self.assertEqual(outcome["state"], rd.WAITING, outcome)
        mark = pg.designation_of(self.marks()[0])
        self.assertEqual({row["name"]: row["family"] for row in mark["authors"]},
                         {"author": "deepseek", "cx2": "codex"})
        self.assertIn("forbidden", mark["why"])

        # #3596 : le gel pourvoit le siège de la seule famille indépendante
        # (claude) ; cx1 (codex, auteur de la plage) ne reçoit rien et la
        # prise ne re-propose pas un siège déjà pourvu.
        self.consommer_designation_au_gel()
        self.assertEqual(work.get(self.db, self.lot)["reviewer"], "r1")
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [], "codex est auteur de la plage")
        self.assertEqual(self.mails("r1"), [], "le siège désigné n'est pas re-proposé")
        self.assertEqual(work.get(self.db, self.lot)["reviewer"], "r1")

    def test_sans_marque_de_designation_aucune_proposition(self):
        self.freeze()
        self.consommer_designation_au_gel()
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
        self.freeze()
        self.consommer_designation_au_gel()
        # La marque la plus récente ne porte pas la liste de complétude : la
        # lecture ne prouve pas l'absence d'auteur inconnu, donc rien n'est
        # proposé (échec fermé), même avec des familles connues.
        self.marque_sans_inconnus([{"name": "author", "family": "deepseek"},
                                   {"name": "cx2", "family": "codex"}])
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

    def test_une_famille_d_auteur_inconnue_ne_propose_rien(self):
        self.freeze()
        self.consommer_designation_au_gel()
        self.marque([{"name": "author", "family": ""},
                     {"name": "cx2", "family": "codex"}])
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

    def test_la_marque_d_un_gel_anterieur_ne_couvre_pas_le_nouveau(self):
        self.freeze()
        self.consommer_designation_au_gel()
        # nouveau gel sans désignation : la lecture du gel courant manque.
        # Posé au stockage, ce témoin lit les marques, pas la porte du gel.
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="b" * 40, actor="author", verdict=None, note="")
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("cx1"), [])
        self.assertEqual(self.mails("r1"), [])

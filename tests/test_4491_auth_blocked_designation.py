# SPDX-License-Identifier: AGPL-3.0-only
"""#4491 — un agent `blocked` (auth_blocked) n'est ni désigné ni laissé en siège.

Constat : depuis la bascule de 03:49Z vers claude/tertiaire, ameesh-dev1,
dev2, dev4 et dev7 (ameesh-agents-3) sont `blocked` avec le texte
`auth_blocked: claude/tertiaire`. La désignation au gel continuait pourtant de
les choisir (`review_designation.is_available` ne connaissait que
`stopped`/`dead`/`paused` et les préfixes `paused`/`budget : `/`accounts : `) :
#4478 et #4457 sont restés en « waiting: verdict by ameesh-dev2 » alors que
deux familles avaient rendu OK, jusqu'au retrait manuel du siège.

Attendu : un `status` `blocked`, ou un `status_text` qui commence par
`auth_blocked`, n'est jamais désigné ; un siège déjà désigné sur un tel agent
ne retient pas la porte quand les familles requises sont couvertes, sinon il
est redésigné sur un agent disponible comme pour un hôte draining (#3328).

Témoins : la disponibilité sans base, la désignation neuve au gel sur un
registre où r1 est bloqué (repli de la même famille), la reprise du siège
existant (remplacement), le texte `auth_blocked` seul (statut non rafraîchi),
et le siège retiré sans courrier quand les sièges gardés couvrent les avis.
"""
from __future__ import annotations

import unittest

from ameesh import auth_failure, registry, storage, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


def row(name, *, status="idle", text="", mode="execute"):
    return {"name": name, "team": "ameesh", "harness": "claude",
            "session_harness": "", "status": status, "mode": mode,
            "status_text": text, "operator_paused": False}


class AvailabilityTest(unittest.TestCase):
    """La disponibilité écarte le blocage, même sans base."""

    def test_un_agent_blocked_est_indisponible(self):
        self.assertFalse(rd.is_available(
            row("dev2", status="blocked",
                text=auth_failure.status_text("claude", "tertiaire"))))
        # Même sans texte : un `blocked` sans fenêtre courte est un siège mort.
        self.assertFalse(rd.is_available(row("dev2", status="blocked")))
        self.assertTrue(rd.is_available(row("dev1")))

    def test_le_texte_auth_blocked_ecarte_meme_un_statut_non_blocked(self):
        # Le statut peut ne pas avoir été rafraîchi : le préfixe de la marque
        # du runner suffit, comme `accounts : ` pour #3973.
        self.assertFalse(rd.is_available(
            row("dev2", text=auth_failure.status_text("claude", "tertiaire"))))
        self.assertTrue(rd.is_available(row("dev1", text="last turn: ok")))

    def test_le_prefixe_de_la_designation_suit_l_executeur(self):
        self.assertTrue(auth_failure.STATUS_PREFIX.startswith(rd.AUTH_BLOCKED_PREFIX))
        self.assertIn(rd.AUTH_BLOCKED_PREFIX, rd.PAUSE_PREFIXES)

    def test_une_fenetre_courte_blocked_reste_disponible(self):
        # #3876 ne doit pas régresser : la fenêtre courte du compte est écrite
        # `blocked` + `budget : …` comme une pause de jauge, mais dure minutes.
        prefixed = ("budget : account cx1: 2/2 concurrent turns (provider burst "
                    "bucket shared by sessions, #3779; running: cx2, dev3)")
        raw = ("account cx1: next turn in 20s (start gap 30s between turns, "
               "#3779; last start 06:25)")
        for text in (prefixed, raw):
            with self.subTest(text=text):
                self.assertFalse(rd.is_blocked_pause(row("cx1", text=text)))
                self.assertTrue(rd.is_available(row("cx1", text=text)))

    def test_un_blocked_sans_fenetre_courte_est_un_siege_mort(self):
        # Le même fait sert à la désignation et à la reprise (#3328) : un
        # `blocked` d'authentification, d'hôte ou de compte est un siège mort.
        self.assertTrue(rd.is_blocked_pause(
            row("dev2", status="blocked",
                text=auth_failure.status_text("claude", "tertiaire"))))
        self.assertTrue(rd.is_blocked_pause(
            row("dev2", status="blocked", text="hôte non prêt : harnais introuvable")))
        self.assertFalse(rd.is_blocked_pause(row("dev1", status="idle")))


class AuthBlockedDesignationTest(_ActivationFixture, PgTestCase):
    """Désignation neuve et reprise d'un siège sur un agent bloqué."""

    def _agent(self, name, harness):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (name,))

    def _block(self, name, *, status="blocked", text=None):
        self.db.execute(
            "UPDATE agent_registry SET status = %s, status_text = %s WHERE name = %s",
            (status, auth_failure.status_text("claude", "tertiaire")
             if text is None else text, name))

    def _designated(self):
        return act.reviewers(self.cfg, self.db, self.lot)["designation"]

    def _column(self):
        return sorted(self.db.query(
            "SELECT reviewers FROM work_items WHERE id = %s",
            (self.lot,))[0]["reviewers"] or [])

    def _requests(self, name):
        return [m for m in self.mailbox()
                if m["recipient"] == name and m["kind"] == rd.REQUEST_KIND]

    def _verdict(self, actor, harness):
        mark = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                              actor=actor, verdict="ok", repo=self.repo.root,
                              after_commit=[])
        # The sensitive gate reads only a verdict with its exact #1465 receipt.
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, 't', %s, %s)",
            ("milestone:" + str(int(mark["id"])), self.frozen, actor,
             "s-" + harness, harness, self.lot))
        return mark

    def test_un_agent_blocked_n_est_pas_designe_au_gel(self):
        # Témoin du lot : dev2 est bloqué, r3 (même famille) est sain. Le gel
        # doit prendre r3 et ne déposer aucune demande chez r1.
        self._agent("r3", "claude")
        self._block("r1")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r2", "r3"])
        self.assertNotIn("r1", [r["name"] for r in out["reviewers"]])
        self.assertEqual(sorted(m["recipient"] for m in self.mailbox()), ["r2", "r3"])

    def test_le_texte_auth_blocked_seul_n_est_pas_designe_au_gel(self):
        # Statut non rafraîchi : le texte `auth_blocked:` écarte quand même.
        self._agent("r3", "claude")
        self._block("r1", status="idle")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r2", "r3"])

    def test_un_siege_blocked_existant_est_redesigne(self):
        # Une désignation écrite avant le blocage est reprise au passage du
        # runner : r1 est remplacé par r3 (même famille), la colonne et le
        # journal suivent, et une seconde passe ne redésigne rien.
        self._agent("r3", "claude")
        _, out = self.freeze()
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r1", "r2"])
        self._block("r1")
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 1, report)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual(noted["state"], rd.DESIGNATED, noted)
        self.assertEqual(sorted(r["name"] for r in noted["reviewers"]), ["r2", "r3"])
        self.assertEqual(self._column(), ["r2", "r3"])
        self.assertEqual(len(self._requests("r3")), 1)
        self.assertIn("r1 (authentication blocked)",
                      self.notes(act.REASSIGN_PREFIX)[-1])
        mails = len(self.mailbox())
        again = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(again["reassigned"], 0, again)
        self.assertEqual(again["examined"], 0, again)
        self.assertEqual(len(self.mailbox()), mails)

    def test_un_siege_blocked_hors_auth_est_redesigne(self):
        # Le statut `blocked` lui-même suffit : l'hôte qui ne peut pas lancer
        # de tour n'est pas un siège qui lira la demande.
        self._agent("r3", "claude")
        self.freeze()
        self._block("r1", text="hôte non prêt : harnais introuvable")
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        self.assertEqual(self._column(), ["r2", "r3"])
        self.assertIn("r1 (status blocked)", self.notes(act.REASSIGN_PREFIX)[-1])

    def test_une_fenetre_courte_blocked_ne_deplace_pas_le_siege(self):
        # #3876 : le siège d'une fenêtre courte reste en place, comme pour une
        # pause budget ; seule la porte neuve évite de le désigner.
        self.freeze()
        self._block("r1", text=(
            "budget : account cx1: 2/2 concurrent turns (provider burst bucket "
            "shared by sessions, #3779; running: cx2, dev3)"))
        mails = len(self.mailbox())
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["examined"], 0, report)
        self.assertEqual(report["reassigned"], 0, report)
        self.assertEqual(self._column(), ["r1", "r2"])
        self.assertEqual(len(self.mailbox()), mails)
        self.assertFalse(self.notes(act.REASSIGN_PREFIX))

    def test_un_texte_auth_blocked_seul_est_redesigne(self):
        # La reprise lit le texte comme la désignation : un statut resté `idle`
        # dont le texte porte `auth_blocked:` est un siège mort (#4491).
        self._agent("r3", "claude")
        self.freeze()
        self._block("r1", status="idle")
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        self.assertEqual(self._column(), ["r2", "r3"])
        self.assertIn("r1 (authentication blocked)",
                      self.notes(act.REASSIGN_PREFIX)[-1])

    def test_un_siege_blocked_est_retire_quand_la_porte_est_ouverte(self):
        # #4457/#4478 : cx3 (codex) et l'auditeur (claude) ont rendu OK sur la
        # tête ; seul dev2 (claude) restait désigné sans avis. Le siège bloqué
        # est retiré, pas remplacé : aucune demande neuve, la colonne garde le
        # siège vivant, et la porte n'est pas retenue.
        self._agent("r5", "claude")
        self.freeze()
        for actor, harness in (("r2", "codex"), ("r5", "claude")):
            self._verdict(actor, harness)
        self._block("r1")
        mails = len(self.mailbox())
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual([r["name"] for r in noted["reviewers"]], ["r2"], noted)
        self.assertEqual(self._column(), ["r2"])
        self.assertEqual(len(self.mailbox()), mails)
        self.assertIn(act.GATE_COVERED_WHY, self.notes(act.REASSIGN_PREFIX)[-1])

    def test_les_sieges_gardes_qui_couvrent_la_porte_retirent_le_blocked(self):
        # Trois sièges dont un bloqué : les deux vivants couvrent les deux
        # familles requises. Le siège bloqué est retiré sans remplaçant et
        # sans courrier : il ne retient pas la porte.
        self._agent("r5", "claude")
        row, _out = self.freeze()
        note = pg._mark_note(
            self.lot, int(row["id"]), state=rd.DESIGNATED, required=2,
            reviewers=[{"name": "r1", "family": "claude"},
                       {"name": "r2", "family": "codex"},
                       {"name": "r5", "family": "claude"}],
            missing=0, why="test: three seats", authors=[], policy_ref="",
            sha=self.frozen)
        storage.of(self.db).work.note(self.lot, "build", note, "test")
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r1', 'r2', 'r5'] WHERE id = %s",
            (self.lot,))
        self._block("r1")
        mails = len(self.mailbox())
        report = act.reassign_unreachable(self.cfg, self.db)
        self.assertEqual(report["reassigned"], 1, report)
        noted = self._designated()
        self.assertEqual([r["name"] for r in noted["reviewers"]], ["r2", "r5"], noted)
        self.assertEqual(self._column(), ["r2", "r5"])
        self.assertEqual(len(self.mailbox()), mails)
        self.assertIn(act.COVERED_WHY, self.notes(act.REASSIGN_PREFIX)[-1])


if __name__ == "__main__":
    unittest.main()

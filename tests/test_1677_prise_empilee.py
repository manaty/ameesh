# SPDX-License-Identifier: AGPL-3.0-only
"""#1677 : la prise voit le travail réel (cas du 15/10).

(a) Un lot dont tous les bloqueurs sont en `qa` avec un dernier verdict
favorable est prenable : il s'empile sur leur tête gelée (branche et gel cités
dans le courrier de prise). Un verdict bloquant, ou l'absence de verdict,
bloque encore.
(b) L'inactivité d'un agent se lit sur ses lots actionnables — rien qui
l'occupe, aucune correction attendue — et non sur le temps depuis son dernier
tour, sinon un agent sans lot n'est jamais « au repos ».
"""
from __future__ import annotations

import json
import time

from ameesh import storage, sous_utilisation, work

from .test_l119_backlog import HEURE, _Base

SHA = "a" * 40


class _Empile(_Base):
    def lot(self, title, *, app="nexlink", age_s=HEURE, **kw):
        item = work.add(self.db, title=title, app=app, **kw)
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (age_s, item["id"]))
        return item

    def take(self, **kw):
        kw.setdefault("orchestras", ["orchestrateur"])
        return super().take(**kw)

    def amont(self, verdict=None, *, sha=SHA, branch="lot/amont"):
        """Le bloqueur du 15/10 : un lot en `qa` (gelé, éventuellement jugé)."""
        item = self.lot("amont", assignee="human:proprio")
        self.db.execute("UPDATE work_items SET state = 'qa', branch = %s"
                        " WHERE id = %s", (branch, item["id"]))
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="test")
        if verdict is not None:
            work.milestone(self.db, item["id"], "verdict", verdict=verdict, sha=sha,
                           actor="test")
        return item

    def bloque(self, amont, *, title="empile", priority=1):
        item = self.lot(title, priority=priority)
        storage.of(self.db).work_dependencies.add(item["id"], amont["id"], actor="test")
        return item

    def pret(self, item_id):
        return storage.of(self.db).work.assign(
            item_id, "claude1", current=None, note="test", actor="test", ready_only=True)

    def courrier(self, item_id):
        rows = self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = 'claude1'"
            " AND work_item_id = %s ORDER BY id DESC LIMIT 1", (str(item_id),))
        return rows[0]["body"] if rows else ""


class EmpilementTest(_Empile):
    def test_bloqueur_en_qa_a_verdict_favorable_est_prenable(self):
        self.agent("claude1", team="nexlink")
        amont = self.amont("ok")
        empile = self.bloque(amont)

        rows = storage.of(self.db).work.unassigned_lots(5)
        self.assertEqual([r["id"] for r in rows], [empile["id"]])
        # le courrier de prise nomme le bloqueur, sa branche et sa tête gelée
        self.assertIn("#%d" % amont["id"], rows[0]["stack_on"])
        self.assertIn("lot/amont", rows[0]["stack_on"])
        self.assertIn(SHA, rows[0]["stack_on"])

        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [empile["id"]])
        self.assertIn("Stacked on blocker(s)", self.courrier(empile["id"]))
        self.assertIn("lot/amont", self.courrier(empile["id"]))

    def test_assignation_directe_accepte_le_meme_bloqueur(self):
        self.agent("claude1", team="nexlink")
        amont = self.amont("ok")
        empile = self.bloque(amont, title="empile direct", priority=2)
        self.assertIsNotNone(self.pret(empile["id"]))

    def test_verdict_bloque_ou_absent_ne_rend_pas_prenable(self):
        self.agent("claude1", team="nexlink")
        sans = self.amont(None, branch="lot/sans-verdict")
        empile = self.bloque(sans)
        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertIsNone(self.pret(empile["id"]))
        self.assertEqual(self.take(), [])

        bloque = self.amont("blocked", branch="lot/bloque")
        autre = self.bloque(bloque, title="empile bloque")
        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertIsNone(self.pret(autre["id"]))

    def test_le_dernier_verdict_fait_foi(self):
        """Un `ok` ancien ne survit pas à un `blocked` plus récent : la
        correction attendue referme la prise."""
        self.agent("claude1", team="nexlink")
        amont = self.amont("ok")
        work.milestone(self.db, amont["id"], "verdict", verdict="blocked", sha=SHA,
                       actor="test")
        empile = self.bloque(amont)
        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertIsNone(self.pret(empile["id"]))

    def test_un_bloqueur_encore_ouvert_suffit_a_bloquer(self):
        """Deux bloqueurs : un fermé, un en qa à verdict favorable -> prenable ;
        il suffit qu'un troisième soit en build pour que la prise attende."""
        self.agent("claude1", team="nexlink")
        ferme = self.lot("ferme", assignee="human:proprio")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (ferme["id"],))
        juge = self.amont("ok", branch="lot/juge")
        encours = self.lot("encours", assignee="human:proprio")
        empile = self.bloque(juge, title="empile mixte", priority=3)
        storage.of(self.db).work_dependencies.add(empile["id"], ferme["id"], actor="test")
        storage.of(self.db).work_dependencies.add(empile["id"], encours["id"], actor="test")
        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        work.close(self.db, encours["id"], abandoned=True, actor="test")
        self.assertEqual([r["id"] for r in storage.of(self.db).work.unassigned_lots(5)],
                         [empile["id"]])

    def test_for_items_dit_la_meme_verite(self):
        amont = self.amont("ok")
        empile = self.bloque(amont)
        edge = storage.of(self.db).work_dependencies.for_items([empile["id"]])[0]
        self.assertFalse(edge["blocking"])
        self.assertEqual(edge["blocker_branch"], "lot/amont")
        self.assertEqual(edge["blocker_frozen_sha"], SHA)

        work.milestone(self.db, amont["id"], "verdict", verdict="blocked", sha=SHA,
                       actor="test")
        edge = storage.of(self.db).work_dependencies.for_items([empile["id"]])[0]
        self.assertTrue(edge["blocking"])


class PropositionTest(_Empile):
    """(c) : quand aucun lot n'est prenable, la prise propose une relecture
    (lot en qa sans verdict, autre famille que l'auteur) puis une correction
    (dernier verdict bloquant, auteur occupé)."""

    def auteur(self, name="auteur", harness="claude", team="nexlink"):
        self.agent(name, harness=harness, team=team)

    def qa(self, title, *, verdict=None, author="auteur", branch="lot/qa", sha=SHA):
        item = self.lot(title, assignee=author)
        self.db.execute("UPDATE work_items SET state = 'qa', branch = %s"
                        " WHERE id = %s", (branch, item["id"]))
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="test")
        if verdict is not None:
            work.milestone(self.db, item["id"], "verdict", verdict=verdict, sha=sha,
                           actor="test")
        return item

    def occupe(self, name="auteur", title="en cours"):
        """L'auteur est au travail : un lot **démarré** (sinon #540 le mettrait
        en file et le lot suivant perdrait son assigné, faute de place)."""
        item = self.lot(title, assignee=name, priority=2)
        self.db.execute("UPDATE work_items SET state = 'build', started_at = now()"
                        " WHERE id = %s", (item["id"],))
        return item

    def mails(self, name):
        rows = self.db.query("SELECT body, payload FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))
        out = []
        for row in rows:
            payload = row["payload"]
            out.append({"body": row["body"],
                        "payload": json.loads(payload) if isinstance(payload, str)
                        else payload})
        return out

    def test_une_relecture_va_a_l_autre_famille(self):
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.agent("codex1", harness="codex", team="nexlink")
        self.agent("claude2", harness="claude", team="nexlink")

        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("review", "codex1", lot["id"])])
        mails = self.mails("codex1")
        self.assertEqual(len(mails), 1)
        self.assertIn("#%d" % lot["id"], mails[0]["body"])
        self.assertIn("lot/a-relire", mails[0]["body"])
        self.assertIn(SHA, mails[0]["body"])
        self.assertEqual(mails[0]["payload"]["review_proposal"]["kind"], "review")
        self.assertEqual(self.mails("claude2"), [], "même famille que l'auteur")

    def test_la_correction_va_a_un_agent_si_l_auteur_est_occupe(self):
        self.auteur()
        lot = self.qa("corriger", verdict="blocked", branch="lot/a-corriger")
        self.occupe()
        self.agent("codex1", harness="codex", team="nexlink")

        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("correction", "codex1", lot["id"])])
        body = self.mails("codex1")[0]["body"]
        self.assertIn("blocking last verdict", body)
        self.assertIn("lot/a-corriger", body)

    def test_la_correction_reste_a_l_auteur_qui_n_est_pas_occupe(self):
        self.auteur()
        self.qa("corriger seul", verdict="blocked", branch="lot/seul")
        self.agent("codex1", harness="codex", team="nexlink")

        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("codex1"), [])

    def test_la_correction_va_a_toute_famille_quand_l_auteur_est_occupe(self):
        """La famille ne borne que la relecture : une correction due peut aller
        à un agent au repos de la même famille que l'auteur."""
        self.auteur()
        lot = self.qa("corriger", verdict="blocked", branch="lot/a-corriger")
        self.occupe()
        self.agent("claude2", harness="claude", team="nexlink")

        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("correction", "claude2", lot["id"])])

    def test_la_relecture_passe_avant_la_correction(self):
        self.auteur()
        self.qa("a corriger", verdict="blocked", branch="lot/correction")
        self.occupe()
        relire = self.qa("a relire", branch="lot/relecture")
        self.agent("codex1", harness="codex", team="nexlink")

        out = self.take()
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in out],
                         [("review", relire["id"])],
                         "la relecture due passe avant la correction")

    def test_pas_deux_fois_la_meme_proposition(self):
        self.auteur()
        self.qa("relire", branch="lot/relire")
        self.agent("codex1", harness="codex", team="nexlink")

        self.assertEqual([t["kind"] for t in self.take()], ["review"])
        self.assertEqual(self.take(), [], "le lot est marqué, l'agent a du courrier")
        self.assertEqual(len(self.mails("codex1")), 1)

    def test_un_lot_prenable_passe_avant_les_propositions(self):
        self.auteur()
        self.qa("relire", branch="lot/relire")
        self.agent("codex1", harness="codex", team="nexlink")
        prenable = self.lot("prenable", priority=1)

        out = self.take()
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in out],
                         [("lot", prenable["id"])])
        self.assertEqual(self.mails("codex1"), [], "la prise n'a pas eu lieu")


class InactiviteTest(_Empile):
    def ligne(self, name):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == name)

    def repos(self, now):
        return [r["name"] for r in sous_utilisation.idle_agents(
            storage.of(self.db).operations.listing(), now, 900, waiting_free=True)]

    def test_un_verdict_bloquant_rend_le_lot_actionnable(self):
        # dernier tour à l'instant : le temps ne dit plus rien (#1677)
        self.agent("claude1", team="nexlink", since_s=0)
        lot = self.lot("en qa", assignee="claude1")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot["id"],))
        self.assertEqual(int(self.ligne("claude1")["actionable_lots"]), 0)
        now = time.time()
        self.assertEqual(self.repos(now), ["claude1"])

        work.milestone(self.db, lot["id"], "frozen", sha=SHA, actor="test")
        work.milestone(self.db, lot["id"], "verdict", verdict="blocked", sha=SHA,
                       actor="test")
        self.assertEqual(int(self.ligne("claude1")["actionable_lots"]), 1)
        self.assertEqual(self.repos(now), [])

    def test_un_lot_en_build_occupe_encore(self):
        self.agent("claude1", team="nexlink", since_s=0)
        lot = self.lot("en build", assignee="claude1")
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s", (lot["id"],))
        self.assertEqual(int(self.ligne("claude1")["actionable_lots"]), 1)
        self.assertEqual(self.repos(time.time()), [])

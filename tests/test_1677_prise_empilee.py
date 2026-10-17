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

from ameesh import backlog, storage, sous_utilisation, work

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

    def ligne(self, name):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == name)

    def juge(self, item_id, *, sha=SHA, verdict="ok"):
        """Un verdict déclaré sur le lot (sans gel, pour le cas B2)."""
        return work.milestone(self.db, item_id, "verdict", verdict=verdict, sha=sha,
                              actor="test")

    def regeler(self, item_id, sha):
        """Un nouveau gel, plus récent que le précédent (B1)."""
        return work.milestone(self.db, item_id, "frozen", sha=sha, actor="test")


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

    # -- B1 : le verdict ne vaut que sur le dernier gel ---------------------

    def test_un_regel_apres_un_ok_perime_le_verdict(self):
        """B1 : regelé sur une autre tête, le lot n'est plus pris sur un `ok`
        qui ne nomme pas son dernier gel — c'est la règle de la file de
        fusion, et elle vaut aussi pour la prise."""
        self.agent("claude1", team="nexlink")
        amont = self.amont("ok")
        empile = self.bloque(amont)
        self.assertEqual([r["id"] for r in storage.of(self.db).work.unassigned_lots(5)],
                         [empile["id"]])

        self.regeler(amont["id"], "b" * 40)
        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertIsNone(self.pret(empile["id"]))
        self.assertEqual(self.take(), [])
        edge = storage.of(self.db).work_dependencies.for_items([empile["id"]])[0]
        self.assertTrue(edge["blocking"])

    def test_un_regel_sur_la_meme_tete_garde_le_verdict(self):
        """B1 : le préfixe admis — regeler le MÊME commit (sha court) ne périme
        pas le verdict, et la tête rendue est celle qui a été relue."""
        self.agent("claude1", team="nexlink")
        amont = self.amont("ok", sha=SHA)
        self.regeler(amont["id"], SHA[:12])
        empile = self.bloque(amont)
        rows = storage.of(self.db).work.unassigned_lots(5)
        self.assertEqual([r["id"] for r in rows], [empile["id"]])
        # la tête rendue est celle que le verdict nomme (le sha complet relu)
        self.assertIn(SHA, rows[0]["stack_on"])

    # -- B2 : la base d'empilement -----------------------------------------

    def test_un_bloqueur_juge_sans_gel_empile_sur_le_sha_du_verdict(self):
        """B2 : un `ok` sans jalon de gel offre la tête que le verdict nomme."""
        self.agent("claude1", team="nexlink")
        amont = self.lot("amont juge", assignee="human:proprio")
        self.db.execute("UPDATE work_items SET state = 'qa', branch = 'lot/juge'"
                        " WHERE id = %s", (amont["id"],))
        self.juge(amont["id"])
        empile = self.bloque(amont)

        rows = storage.of(self.db).work.unassigned_lots(5)
        self.assertEqual([r["id"] for r in rows], [empile["id"]])
        self.assertIn(SHA, rows[0]["stack_on"])
        self.assertEqual([t["item"]["id"] for t in self.take()], [empile["id"]])

    def test_un_bloqueur_sans_tete_relue_reste_bloquant(self):
        """B2 : un `ok` qui ne nomme aucune tête (ni gel) n'offre aucune base :
        le dépendant n'est pas pris."""
        self.agent("claude1", team="nexlink")
        amont = self.lot("amont sans tete", assignee="human:proprio")
        self.db.execute("UPDATE work_items SET state = 'qa', branch = 'lot/sans-tete'"
                        " WHERE id = %s", (amont["id"],))
        self.juge(amont["id"], sha="")
        empile = self.bloque(amont)

        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertIsNone(self.pret(empile["id"]))
        self.assertEqual(self.take(), [])
        edge = storage.of(self.db).work_dependencies.for_items([empile["id"]])[0]
        self.assertTrue(edge["blocking"])


class PropositionTest(_Empile):
    """(c) : quand aucun lot n'est prenable, la prise propose une relecture
    (lot en qa sans verdict, autre famille que l'auteur) puis une correction
    (dernier verdict bloquant, auteur occupé)."""

    def auteur(self, name="auteur", harness="claude", team="nexlink"):
        self.agent(name, harness=harness, team=team)

    def qa(self, title, *, verdict=None, author="auteur", branch="lot/qa", sha=SHA,
           app="nexlink"):
        item = self.lot(title, assignee=author, app=app)
        self.db.execute("UPDATE work_items SET state = 'qa', branch = %s"
                        " WHERE id = %s", (branch, item["id"]))
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="test")
        if verdict is not None:
            work.milestone(self.db, item["id"], "verdict", verdict=verdict, sha=sha,
                           actor="test")
        return item

    def harnais_vivant(self, name, harness):
        """L242b : la session vivante de l'agent, distincte de son étiquette."""
        self.db.execute(
            "UPDATE agent_registry SET session_id = %s, session_origin = %s,"
            " session_harness = %s WHERE name = %s",
            ("s-" + name, "s-" + name, harness, name))
        return name

    def capacite(self, item_id, capability):
        self.db.execute("UPDATE work_items SET required_capabilities = %s::text[]"
                        " WHERE id = %s", ("{" + capability + "}", item_id))
        return item_id

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

    # -- B1 : le regel rouvre la relecture ---------------------------------

    def test_un_regel_apres_un_ok_rouvre_la_relecture(self):
        """B1 : un `ok` périmé par un regel ne dispense plus de relire — la
        tête relue a changé."""
        self.auteur()
        lot = self.qa("regel apres ok", verdict="ok", branch="lot/ok-perime")
        self.agent("codex1", harness="codex", team="nexlink")
        self.assertEqual(self.take(), [], "verdict courant : rien à proposer")

        self.regeler(lot["id"], "b" * 40)
        rows = storage.of(self.db).work.review_proposals(
            5, proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE)
        self.assertEqual([(r["id"], r["last_verdict"]) for r in rows],
                         [(lot["id"], None)])
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in self.take()],
                         [("review", lot["id"])])

    def test_un_regel_apres_un_blocked_rouvre_la_relecture(self):
        """B1 : la correction est livrée puis le lot regelé — le `blocked`
        périmé ne le rend plus « actionnable », la relecture est due."""
        self.auteur()
        lot = self.qa("regel apres blocked", verdict="blocked", branch="lot/blocked-perime")
        self.assertEqual(int(self.ligne("auteur")["actionable_lots"]), 1)

        self.regeler(lot["id"], "b" * 40)
        self.assertEqual(int(self.ligne("auteur")["actionable_lots"]), 0,
                         "un blocked périmé n'attend plus de correction")
        rows = storage.of(self.db).work.review_proposals(
            5, proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE)
        self.assertEqual([(r["id"], r["last_verdict"]) for r in rows],
                         [(lot["id"], None)])

    # -- B3 : l'auteur ne relit pas son lot, sa famille est vivante --------

    def test_on_ne_propose_pas_a_l_auteur_son_propre_lot(self):
        """B3 : l'auteur est écarté, et sa famille se lit sur sa session
        vivante (L242b) — déclaré claude, session codex : la relecture revient
        à un Claude, pas à lui."""
        self.auteur()
        self.harnais_vivant("auteur", "codex")
        lot = self.qa("propre lot", branch="lot/propre")

        self.assertEqual(self.take(), [], "l'auteur ne relit pas son propre lot")
        self.assertEqual(self.mails("auteur"), [])
        self.agent("claude2", harness="claude", team="nexlink")
        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("review", "claude2", lot["id"])],
                         "la famille vivante de l'auteur (codex) demande un Claude")

    # -- B4 : équipe et capacités, comme toute prise ------------------------

    def test_une_correction_ne_sort_pas_de_l_equipe_du_lot(self):
        """B4 : l'équipe du lot se résout comme à la prise (ici l'application)
        — un agent d'une autre équipe n'est pas proposé."""
        self.auteur(team="ameesh")
        self.occupe()
        lot = self.qa("corriger", verdict="blocked", branch="lot/a-corriger",
                      app="ameesh")
        self.agent("codex1", harness="codex", team="nexlink")
        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("codex1"), [])

        self.agent("codex2", harness="codex", team="ameesh")
        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("correction", "codex2", lot["id"])])

    def test_une_relecture_respecte_les_capacites_du_lot(self):
        """B4 : une capacité exigée s'applique aussi à la relecture."""
        self.auteur(team="ameesh")
        lot = self.qa("relire", branch="lot/a-relire", app="ameesh")
        self.capacite(lot["id"], "propose")
        self.agent("codex1", harness="codex", team="ameesh")
        self.assertEqual(self.take(), [], "capacité exigée absente")

        self.agent("codex2", harness="codex", team="ameesh",
                   capabilities=["propose"])
        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("review", "codex2", lot["id"])])

    def test_un_lot_non_appariable_ne_bloque_pas_les_suivants(self):
        """N6 : la lecture couvre plus large que la pièce — six lots d'une
        autre équipe n'empêchent pas de proposer le septième."""
        self.auteur(team="nexlink")
        for i in range(6):
            self.qa("hors equipe %d" % i, verdict="blocked", app="ameesh",
                    branch="lot/hors-%d" % i)
        bon = self.qa("appariable", verdict="blocked", app="nexlink",
                      branch="lot/appariable")
        self.agent("codex1", harness="codex", team="nexlink")

        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("correction", "codex1", bon["id"])])


class InactiviteTest(_Empile):
    def ligne(self, name):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == name)

    def repos(self, now):
        # #1677 : le repos de la prise et des alertes se lit sur les lots
        # actionnables, pas sur le temps depuis le dernier tour.
        return [r["name"] for r in sous_utilisation.idle_agents(
            storage.of(self.db).operations.listing(), now, 900,
            waiting_free=True, actionable_idle=True)]

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


class RelecteurOccupeTest(_Empile):
    """#1726 : une relecture désignée (#705) sans verdict courant occupe son
    relecteur — la prise ne lui donne ni lot ni autre relecture, et l'alerte de
    sous-emploi ne le liste pas."""

    def ligne(self, name):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == name)

    def repos(self, now):
        # #1677 : le repos de la prise et des alertes se lit sur les lots
        # actionnables, pas sur le temps depuis le dernier tour (#1726).
        return [r["name"] for r in sous_utilisation.idle_agents(
            storage.of(self.db).operations.listing(), now, 900,
            waiting_free=True, actionable_idle=True)]

    def designe(self, item_id, reviewer):
        self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                        (reviewer, item_id))

    def occupe(self, name="auteur"):
        item = self.lot("en cours", assignee=name, priority=2)
        self.db.execute("UPDATE work_items SET state = 'build', started_at = now()"
                        " WHERE id = %s", (item["id"],))
        return item

    def test_une_relecture_designee_occupe_son_relecteur(self):
        self.agent("claude1", team="nexlink", since_s=0)
        lot = self.lot("a relire", assignee="human:proprio")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot["id"],))
        self.designe(lot["id"], "claude1")

        self.assertEqual(int(self.ligne("claude1")["actionable_lots"]), 1,
                         "relecture désignée sans verdict courant")
        now = time.time()
        self.assertEqual(self.repos(now), [])

        # verdict rendu sur la tête gelée : plus rien à relire
        work.milestone(self.db, lot["id"], "frozen", sha=SHA, actor="test")
        work.milestone(self.db, lot["id"], "verdict", verdict="ok", sha=SHA, actor="test")
        self.assertEqual(int(self.ligne("claude1")["actionable_lots"]), 0)
        self.assertEqual(self.repos(now), ["claude1"])

        # regel : la relecture est de nouveau due
        work.milestone(self.db, lot["id"], "frozen", sha="b" * 40, actor="test")
        self.assertEqual(int(self.ligne("claude1")["actionable_lots"]), 1)
        self.assertEqual(self.repos(now), [])

    def test_le_relecteur_designe_ne_prend_pas_de_lot(self):
        self.agent("auteur", team="nexlink")
        self.agent("codex1", harness="codex", team="nexlink")
        self.occupe("auteur")
        lot = self.lot("a relire", assignee="auteur")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot["id"],))
        self.designe(lot["id"], "codex1")
        prenable = self.lot("prenable", priority=1)

        self.assertEqual(self.take(), [], "le relecteur doit sa relecture")
        self.assertIsNone(work.get(self.db, prenable["id"])["assignee"],
                          "le lot prenable n'a pas été donné au relecteur")

        # verdict rendu : le relecteur redevient preneur
        work.milestone(self.db, lot["id"], "frozen", sha=SHA, actor="test")
        work.milestone(self.db, lot["id"], "verdict", verdict="ok", sha=SHA, actor="test")
        out = self.take()
        self.assertEqual([(t["kind"], t["agent"], t["item"]["id"]) for t in out],
                         [("lot", "codex1", prenable["id"])])

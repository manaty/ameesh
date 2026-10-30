# SPDX-License-Identifier: AGPL-3.0-only
"""#1677 : la prise voit le travail réel (cas du 15/10).

(a) #2252 remplace #1677(a) : un bloqueur en `qa` n'est jamais satisfait, même
à verdict courant favorable ; seul un bloqueur fusionné libère le dépendant
(section `DependanceNonFusionneeTest`). La règle est celle de `work show` et de
« planned: waiting on lots ».
(b) L'inactivité d'un agent se lit sur ses lots actionnables — rien qui
l'occupe, aucune correction attendue — et non sur le temps depuis son dernier
tour, sinon un agent sans lot n'est jamais « au repos ».
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import patch

from ameesh import backlog, plan_git, storage, sous_utilisation, work
from ameesh import review_designation as rd
from ameesh import review_designation_pg as pg

from .test_l119_backlog import HEURE, _Base

SHA = "a" * 40


class _Prise(_Base):
    def setUp(self):
        super().setUp()
        # #2864 : les auteurs lus par la désignation du dernier gel, tels que
        # la marque `review-designation` les garde (par lot).
        self.marques: dict = {}

    def lot(self, title, *, app="nexlink", age_s=HEURE, **kw):
        item = work.add(self.db, title=title, app=app, **kw)
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (age_s, item["id"]))
        return item

    def marque(self, item_id, authors, *, state=rd.WAITING, required=1, sha=SHA):
        """La marque du dernier gel, avec la lecture d'auteurs de la désignation.

        #2864 : la proposition de relecture lit les auteurs de la plage sur
        cette marque ; un banc sans désignation réelle la pose explicitement.
        """
        freeze = plan_git.latest_freeze(work.milestones(self.db, int(item_id)))
        self.assertIsNotNone(freeze, "le lot doit être gelé avant sa marque")
        note = pg._mark_note(
            int(item_id), int(freeze["id"]), state=state, required=required,
            reviewers=[], missing=1, why="test : désignation en attente",
            authors=[dict(row) for row in authors], policy_ref="",
            sha=str(freeze.get("sha") or sha))
        storage.of(self.db).work.note(int(item_id), "qa", note, "test")
        self.marques[int(item_id)] = [dict(row) for row in authors]
        return freeze

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
        """Un verdict déclaré sur le lot (sans gel, pour le cas B2).

        #2962 : un SHA complet doit se résoudre, même sur un lot jamais gelé ;
        ce témoin simule le dépôt qui porte le commit, le cas B2 restant celui
        d'un verdict sans gel (le sujet est la prise, pas la tête relue)."""
        with patch("ameesh.freeze_checks.commit_in", return_value=sha):
            return work.milestone(self.db, item_id, "verdict", verdict=verdict,
                                  sha=sha, actor="test")

    def regeler(self, item_id, sha):
        """Un nouveau gel, plus récent que le précédent (B1), avec la marque de
        désignation du nouveau gel (#2864 : sans elle, aucune proposition)."""
        row = work.milestone(self.db, item_id, "frozen", sha=sha, actor="test")
        self.marque(item_id, self.marques.get(
            int(item_id), [{"name": "auteur", "family": "claude"}]))
        return row


class DependanceNonFusionneeTest(_Prise):
    """(a) #2252 : une dépendance n'est satisfaite que FUSIONNÉE.

    #1677 admettait un bloqueur en `qa` à verdict courant favorable, le
    dépendant s'empilant sur sa tête gelée. Le 17/10, #661 a reçu un `ok` à
    13:45:52, #667 a été pris à 13:45:57, puis #661 a reçu un `blocked` à
    13:46:07 : la base venait de changer sous l'agent. La règle de #2252 est
    celle de `work show` et de « planned: waiting on lots » — seule une fusion
    libère le dépendant."""

    def test_bloqueur_en_qa_a_verdict_favorable_ne_rend_pas_prenable(self):
        self.agent("claude1", team="nexlink")
        amont = self.amont("ok")
        empile = self.bloque(amont)


        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertIsNone(self.pret(empile["id"]))
        self.assertEqual(self.take(), [])
        self.assertEqual(self.courrier(empile["id"]), "")
        edge = storage.of(self.db).work_dependencies.for_items([empile["id"]])[0]
        self.assertTrue(edge["blocking"], "un bloqueur en qa est bloquant")
        self.assertEqual(edge["blocker_branch"], "lot/amont")
        self.assertEqual(edge["blocker_frozen_sha"], SHA)

        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (amont["id"],))
        self.assertEqual([r["id"] for r in storage.of(self.db).work.unassigned_lots(5)],
                         [empile["id"]])
        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [empile["id"]])
        # plus d'empilement : le courrier ne cite plus de tête en qa
        self.assertNotIn("Stacked on", self.courrier(empile["id"]))

    def test_verdict_et_gel_ne_changent_plus_la_prise(self):
        """Toutes les formes de `qa` bloquent : sans verdict, à verdict ok ou
        bloqué, avec ou sans tête relue. Le dernier bloqueur fusionné libère."""
        self.agent("claude1", team="nexlink")
        sans = self.amont(None, branch="lot/sans-verdict")
        juge = self.lot("amont juge", assignee="human:proprio")
        self.db.execute("UPDATE work_items SET state = 'qa', branch = 'lot/juge'"
                        " WHERE id = %s", (juge["id"],))
        self.juge(juge["id"])                   # ok sur un lot jamais gelé (B2)
        bloque = self.amont("blocked", branch="lot/bloque")
        empile = self.bloque(sans, title="empile trois qa", priority=3)
        for amont in (juge, bloque):
            storage.of(self.db).work_dependencies.add(empile["id"], amont["id"],
                                                      actor="test")

        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.assertEqual(self.take(), [])
        for amont in (sans, juge):
            self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                            (amont["id"],))
            self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [])
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (bloque["id"],))
        self.assertEqual([t["item"]["id"] for t in self.take()], [empile["id"]])

    def test_un_bloqueur_encore_ouvert_suffit_a_bloquer(self):
        """Les autres dépendances closes ne rachètent pas un bloqueur en qa."""
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
        self.assertEqual(storage.of(self.db).work.unassigned_lots(5), [],
                         "le bloqueur en qa tient encore")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (juge["id"],))
        self.assertEqual([r["id"] for r in storage.of(self.db).work.unassigned_lots(5)],
                         [empile["id"]])


class PropositionTest(_Prise):
    """(c) : quand aucun lot n'est prenable, la prise propose une relecture
    (lot en qa sans verdict, autre famille que l'auteur) puis une correction
    (dernier verdict bloquant, auteur occupé)."""

    def auteur(self, name="auteur", harness="claude", team="nexlink"):
        self.agent(name, harness=harness, team=team)

    def qa(self, title, *, verdict=None, author="auteur", branch="lot/qa", sha=SHA,
           app="nexlink", author_family="claude"):
        item = self.lot(title, assignee=author, app=app)
        self.db.execute("UPDATE work_items SET state = 'qa', branch = %s"
                        " WHERE id = %s", (branch, item["id"]))
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="test")
        if verdict is not None:
            work.milestone(self.db, item["id"], "verdict", verdict=verdict, sha=sha,
                           actor="test")
        self.marque(item["id"], [{"name": author, "family": author_family}], sha=sha)
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
        lot = self.qa("propre lot", branch="lot/propre", author_family="codex")

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
        # Le profil de relecture (propose + rôle) est acquis par la fixture ;
        # la capacité exigée ici est distincte pour éprouver `lot_fits` seul.
        self.capacite(lot["id"], "read")
        self.agent("codex1", harness="codex", team="ameesh")
        self.assertEqual(self.take(), [], "capacité exigée absente")

        self.agent("codex2", harness="codex", team="ameesh",
                   capabilities=["propose", "read"])
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

    # -- #1738 : N3 (interrupteur), N5 (sérialisation), N7 (relecteur) ------

    def test_une_relecture_designe_son_relecteur(self):
        """#1738 (N7) : la relecture proposée désigne son relecteur (#705) —
        le lot sort des propositions suivantes et `stale_lot` s'adresse à
        lui."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.agent("codex1", harness="codex", team="nexlink")

        out = self.take()
        self.assertEqual([(t["kind"], t["agent"]) for t in out], [("review", "codex1")])
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "codex1")
        self.assertEqual(storage.of(self.db).work.review_proposals(
            5, proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE), [],
            "un relecteur désigné écarte le lot des propositions")
        self.assertEqual(self.take(), [])
        self.assertEqual(len(self.mails("codex1")), 1, "aucune double proposition")

    def test_une_relecture_ecarte_une_fiche_sans_propose(self):
        """#4779 (B1 dev4) : la voie des relectures proposées applique le
        garde de fiche — un canari `[read]` / `roles: []` ne reçoit pas le
        siège, et le lot reste disponible pour un relecteur éligible."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.agent("canari", harness="codex", team="nexlink",
                   capabilities=["read"], roles=[])

        self.assertEqual(self.take(), [], "fiche sans propose : aucune proposition")
        self.assertEqual(self.mails("canari"), [])
        self.assertFalse(work.get(self.db, lot["id"])["reviewer"])
        self.assertEqual(storage.of(self.db).work.review_proposals(
            5, proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE)[0]["id"], lot["id"],
            "le lot n'a pas été consommé par l'agent écarté")

        self.agent("codex1", harness="codex", team="nexlink")
        out = self.take()
        self.assertEqual([(t["kind"], t["agent"]) for t in out], [("review", "codex1")])
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "codex1")

    def test_une_correction_ne_touche_pas_au_relecteur(self):
        """#1738 (N7) : une correction proposée ne désigne personne — le champ
        `reviewer` reste au relecteur de la relecture."""
        self.auteur()
        lot = self.qa("corriger", verdict="blocked", branch="lot/a-corriger")
        self.occupe()
        self.agent("codex1", harness="codex", team="nexlink")

        out = self.take()
        self.assertEqual([t["kind"] for t in out], ["correction"])
        self.assertFalse((work.get(self.db, lot["id"])["reviewer"] or "").strip())

    def test_la_proposition_est_serialisee_par_lot(self):
        """#1738 (N5) : le contrôle et la note tiennent sous un verrou par lot
        — le second passage ne propose pas le même lot."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        st = storage.of(self.db).work
        claim = dict(agent="codex1", kind="review",
                     proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE,
                     window_s=backlog.REVIEW_PROPOSAL_WINDOW_S, actor="ameesh")
        self.assertTrue(st.claim_review_proposal(lot["id"], **claim))
        self.assertFalse(st.claim_review_proposal(lot["id"], **claim),
                         "fenêtre : pas de seconde proposition")
        self.assertEqual(self.mails("codex1"), [], "le verrou n'envoie aucun courrier")

    def test_une_proposition_hors_qa_est_refusee(self):
        """#1738 (N5) : un lot sorti de `qa` entre la lecture et la note n'est
        pas proposé."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (lot["id"],))
        self.assertFalse(storage.of(self.db).work.claim_review_proposal(
            lot["id"], agent="codex1", kind="review",
            proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE,
            window_s=backlog.REVIEW_PROPOSAL_WINDOW_S, actor="ameesh"))

    def claim(self, lot, agent="codex1", kind="review"):
        return storage.of(self.db).work.claim_review_proposal(
            lot["id"], agent=agent, kind=kind,
            proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE,
            window_s=backlog.REVIEW_PROPOSAL_WINDOW_S, actor="ameesh")

    def test_un_relecteur_designe_par_le_pilote_n_est_pas_ecrase(self):
        """#1738 R1 (B1) : le pilote désigne dev7 (hors verrou) entre la
        lecture des propositions et la prise — la prise ne l'écrase pas."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        storage.of(self.db).work.designate_reviewer(lot["id"], "dev7", actor="pilote")
        self.assertFalse(self.claim(lot))
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "dev7")

    def test_un_lot_juge_entre_temps_n_est_pas_propose(self):
        """#1738 R1 (B1) : un verdict courant `ok` (ou un `kind` périmé) arrivé
        entre la lecture et la prise : rien n'est proposé ni écrit."""
        self.auteur()
        ok = self.qa("juge ok", verdict="ok", branch="lot/ok")
        self.assertFalse(self.claim(ok))
        self.assertFalse((work.get(self.db, ok["id"])["reviewer"] or "").strip())
        bloque = self.qa("juge bloque", verdict="blocked", branch="lot/bloque")
        self.assertFalse(self.claim(bloque, kind="review"), "kind périmé")
        self.assertTrue(self.claim(bloque, kind="correction"))

    def test_un_verdict_concurrent_empeche_la_relecture(self):
        """Un verdict validé pendant l'attente du lot précède sa revalidation."""
        self.auteur()
        lot = self.qa("verdict concurrent", branch="lot/concurrent")
        other = self.connect()
        self.addCleanup(other.close)
        with other.transaction() as tx:
            driver = type(tx)
        query = driver.query
        ready, results, errors = threading.Event(), [], []

        def observe(db, sql, params=()):
            if threading.current_thread() is worker and (
                    "FOR NO KEY UPDATE" in sql or sql.startswith("UPDATE work_items SET")):
                ready.set()
            return query(db, sql, params)

        def claim():
            try:
                results.append(storage.of(other).work.claim_review_proposal(
                    lot["id"], agent="codex1", kind="review", actor="ameesh",
                    proposal_prefix=backlog.REVIEW_PROPOSAL_NOTE,
                    window_s=backlog.REVIEW_PROPOSAL_WINDOW_S))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=claim)
        with patch.object(driver, "query", observe):
            try:
                with self.db.transaction() as held:
                    work.milestone(held, lot["id"], "verdict", verdict="ok",
                                   sha=SHA, actor="test")
                    worker.start()
                    self.assertTrue(ready.wait(10), (results, errors))
            finally:
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [False], "le verdict acquis interdit la proposition")
        self.assertFalse(work.get(self.db, lot["id"])["reviewer"])

    def test_on_coupe_les_propositions_et_la_file_reprend(self):
        """#1738 (N3) : `review_max_per_hour=0` coupe les propositions ; la
        file d'amélioration, elle, est servie dans le même passage."""
        self.auteur()
        self.qa("relire", branch="lot/a-relire")
        self.agent("codex1", harness="codex", team="nexlink")
        item = self.element("amelioration prete", 50)

        out = self.take(review_max_per_hour=0)
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in out],
                         [("backlog", item["id"])])
        self.assertTrue(all("review_proposal" not in m["payload"]
                            for m in self.mails("codex1")),
                        "seul le courrier de la file, aucune proposition")

    def test_les_propositions_restent_par_defaut(self):
        """#1738 (N3) : sans réglage, la proposition de relecture passe avant
        la file d'amélioration (comportement #1677 conservé)."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.agent("codex1", harness="codex", team="nexlink")
        item = self.element("amelioration prete", 50)

        out = self.take()
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in out],
                         [("review", lot["id"])])
        self.assertIsNone(work.get(self.db, item["id"])["assignee"])


class InactiviteTest(_Prise):
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
        work.milestone(self.db, lot["id"], "frozen", sha=SHA, actor="test")
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


class RelecteurOccupeTest(_Prise):
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

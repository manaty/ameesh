# SPDX-License-Identifier: AGPL-3.0-only
"""#705 : désigner le relecteur quand un lot entre en qa.

Avant tout verdict, ameesh ne connaissait le relecteur qu'après coup (l'auteur
du dernier verdict) : un lot en qa attendait « verdict d'un relecteur (non
désigné) », donc personne. `work move <id> qa --reviewer <agent>` le désigne ;
l'attente, la vue de progrès et `work show` le nomment ; un verdict courant
garde son auteur réel, et la revue suivante peut redésigner quelqu'un.

L'alerte `stale_lot` suit : elle part au relecteur désigné (le verdict lui est
dû) ; sans relecteur, elle part aux orchestrateurs du projet du lot et demande
d'en désigner un.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from ameesh import (alert_briefs, db as db_mod, exploitation, mail, notify, plan,
                    progress, registry, storage, work)
from ameesh.runner import AgentWorker, Runner

from .support import REPO, PgTestCase


class ReviewerMilestonesTest(unittest.TestCase):
    """Le relecteur lu par `progress` : désigné, ou l'auteur du verdict courant."""

    def _item(self, **extra):
        item = {"state": "qa", "reviewer": "", "created_ts": 0.0}
        item.update(extra)
        return item

    def test_designe_avant_tout_verdict(self):
        jalons = progress._work_item_milestones(self._item(reviewer="relecteur"), [], [])
        self.assertEqual(jalons["reviewer"], "relecteur")
        self.assertIsNone(jalons["verdict"])

    def test_verdict_courant_garde_son_auteur(self):
        events = [
            {"state": "qa", "created_ts": 1.0},
            {"state": "build", "created_ts": 2.0, "actor": "autre"},
        ]
        jalons = progress._work_item_milestones(self._item(reviewer="designe"), events, [])
        self.assertTrue(jalons["verdict_current"])
        self.assertEqual(jalons["reviewer"], "autre")

    def test_regel_rend_la_parole_au_designe(self):
        events = [
            {"state": "qa", "created_ts": 1.0},
            {"state": "build", "created_ts": 2.0, "actor": "autre"},
            {"state": "qa", "created_ts": 3.0},
        ]
        jalons = progress._work_item_milestones(self._item(reviewer="designe"), events, [])
        self.assertFalse(jalons["verdict_current"])
        self.assertEqual(jalons["reviewer"], "designe")

    def test_sans_designation_ni_verdict_personne(self):
        jalons = progress._work_item_milestones(self._item(), [], [])
        self.assertIsNone(jalons["reviewer"])

    def test_verdict_non_requis_dispense_la_revue(self):
        """#1979 : la marque « verdict non requis » (colonne du relecteur) dit
        qu'aucun re-verdict n'est dû — le lot est prêt à fusionner, pas « en
        revue », et la marque ne sort pas comme un nom de relecteur."""
        jalons = progress._work_item_milestones(
            self._item(reviewer=work.REVIEWER_NONE), [], [])
        self.assertFalse(jalons["verdict_required"])
        self.assertEqual(jalons["reviewer"], work.REVIEWER_NONE)
        self.assertEqual(progress.lot_state("qa", jalons), "approved")

    def test_verdict_requis_par_defaut(self):
        """#1979 : sans la marque, la revue reste due (désignée ou pas)."""
        jalons = progress._work_item_milestones(self._item(reviewer="relecteur"), [], [])
        self.assertTrue(jalons["verdict_required"])
        self.assertEqual(progress.lot_state("qa", jalons), "review")

    def test_verdict_non_requis_et_bloquant_perime(self):
        """#1979 : un verdict bloquant périmé ne barre pas la dispense (même
        règle que `stagnation.waiting`) ; un bloquant courant, si."""
        jalons = {"verdict_kind": "blocked", "verdict_current": False,
                  "verdict_required": False}
        self.assertEqual(progress.lot_state("qa", jalons), "approved")
        jalons["verdict_current"] = True
        self.assertEqual(progress.lot_state("qa", jalons), "review")


class ReviewerBriefsTest(unittest.TestCase):
    """#705 : routage de l'alerte `stale_lot` d'un lot en qa (textes et destinataires)."""

    LISTING = [{"name": "auteur", "team": "ameesh"},
               {"name": "relecteur", "team": "ameesh"},
               {"name": "orch", "team": "ameesh"},
               {"name": "orch-b", "team": "nexlink"},
               {"name": "auditeur", "team": ""}]

    def _alert(self, **extra):
        alert = {"type": "stale_lot", "agent": "auteur", "lot": 42, "title": "export",
                 "lot_state": "qa", "value": 7 * 3600, "threshold": 6 * 3600,
                 "waiting_for": "verdict", "project": "ameesh",
                 "waiting": {"what": "verdict", "who": None,
                             "label": "verdict from a reviewer (none designated)"},
                 "orchestrators": ["orch", "orch-b"]}
        alert.update(extra)
        return alert

    def test_relecteur_designe_prevenu_du_verdict_du(self):
        alert = self._alert(waiting={"what": "verdict", "who": "relecteur",
                                     "label": "verdict by relecteur"})
        briefs = alert_briefs.briefs(alert, self.LISTING, auditor_names=["auditeur"])
        self.assertIn("relecteur", briefs)
        self.assertIn("ameesh work milestone 42 verdict ok|blocked", briefs["relecteur"])
        self.assertIn("orch", briefs)          # les orchestrateurs restent prévenus
        # L713 (main) : `stale_lot` ne réveille plus l'auditeur par courrier,
        # même quand un relecteur est désigné ; il le lit à sa passe.
        self.assertNotIn("auditeur", briefs)

    def test_sans_relecteur_l_orchestrateur_du_projet_est_prevenu(self):
        briefs = alert_briefs.briefs(self._alert(), self.LISTING)
        self.assertEqual(sorted(briefs), ["orch"])      # projet ameesh, pas orch-b
        self.assertIn("ameesh work reviewer 42 <agent>", briefs["orch"])
        self.assertIn("ameesh work milestone 42 verdict", briefs["orch"])

    def test_projet_inconnu_retombe_sur_l_equipe_de_l_assigne(self):
        alert = self._alert()
        alert.pop("project")
        briefs = alert_briefs.briefs(alert, self.LISTING)
        self.assertEqual(sorted(briefs), ["orch"])

    def test_autre_attente_inchangee(self):
        # verdict bloquant courant : l'auteur doit corriger, pas le relecteur
        alert = self._alert(waiting={"what": "fix", "who": "auteur", "label": "fix by auteur"},
                            waiting_for="correction")
        briefs = alert_briefs.briefs(alert, self.LISTING)
        self.assertEqual(sorted(briefs), ["orch"])
        self.assertIn("nudge auteur", briefs["orch"])
        self.assertNotIn("--reviewer", briefs["orch"])

    def test_alerte_sans_attente_inchangee(self):
        alert = self._alert()
        alert.pop("waiting")
        alert["waiting_for"] = None
        briefs = alert_briefs.briefs(alert, self.LISTING)
        self.assertEqual(sorted(briefs), ["orch"])
        self.assertIn("nudge auteur", briefs["orch"])


class ReviewerDesignationTest(PgTestCase):
    def _lot(self, title="un lot", assignee="auteur", **extra):
        self.register(assignee, "claude", cwd=self.tmp)
        # #705 : la désignation est un geste de pilote (`work.corrector`) : les
        # tests désignent en tant qu'orchestrateur déclaré, jamais en tant
        # qu'assigné du lot.
        self.register("chef", "claude", cwd=self.tmp)
        return work.add(self.db, title=title, assignee=assignee, **extra)

    def _env(self):
        return self.env(AMEESH_ALERT_ORCHESTRATORS="chef")

    def _qa(self, lot, *args):
        proc = self.mesh("work", "move", str(lot["id"]), "build",
                         env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # These fixtures exercise reviewer routing after a declared freeze.
        self._freeze_at(lot, "a" * 40)
        return self.mesh("work", "move", str(lot["id"]), "qa", *args, "--actor", "chef",
                         env=self._env())

    def _freeze_at(self, lot, sha):
        """#2962 : un SHA complet non résolu ne gèle plus par la porte ; ces
        témoins posent la tête gelée au stockage (#2962 a ses propres témoins
        d'écriture du gel)."""
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor)"
            " VALUES (%s, 'frozen', %s, 'chef')", (lot["id"], sha))

    def _stale(self, lot):
        """Vieillit la ligne, les transitions, les jalons et le courrier (7 h).

        #2478 : la désignation dépose un courrier lié au lot, qui compte comme
        activité ; vieillir le courrier aussi garde le témoin `stale_lot`.
        """
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours' WHERE id = %s",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'"
                    " WHERE work_item_id = %s",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"
                    " WHERE work_item_id = %s"):
            self.db.execute(sql, (lot,))
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '7 hours'"
                        " WHERE work_item_id = %s", (str(lot),))

    def _stale_alert(self, lot):
        alertes = exploitation.alerts(self.cfg, self.db,
                                      orchestrators_declared=["orchestrateur"])
        return next(a for a in alertes if a["type"] == "stale_lot" and a["lot"] == lot)

    def _briefs(self, alert):
        # #3036 : un brief ne part plus à un agent sans bail vivant ; ce banc
        # simule les runners qui tiennent les agents enregistrés.
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'")
        return alert_briefs.briefs(alert, storage.of(self.db).operations.listing())

    def test_relecteur_designe_nomme_avant_tout_verdict(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer relecteur", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")

        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")
        self.assertEqual(rows[0]["waiting_for"]["who"], "relecteur")
        self.assertEqual(rows[0]["waiting_for"]["label"], "verdict by relecteur")

        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer : relecteur", proc.stdout)
        self.assertIn("waiting  : verdict by relecteur", proc.stdout)

    def test_sans_relecteur_l_attente_le_dit(self):
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertIsNone(rows[0]["waiting_for"]["who"])
        self.assertEqual(rows[0]["waiting_for"]["label"],
                         "verdict from a reviewer (none designated)")

    def test_refus_hors_qa_et_pour_un_nom_inconnu(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build",
                         "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--reviewer", proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "fantome", "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not in the registry", proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["reviewer"]), ("build", ""))

    def test_revue_suivante_garde_puis_redesigne(self):
        self.register("premier", "codex", cwd=self.tmp)
        self.register("second", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "premier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # retour en build (verdict bloquant) : le relecteur connu reste posé
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "premier")
        # une nouvelle revue peut redésigner quelqu'un d'autre
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "second", "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "second")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["label"], "verdict by premier, second")
        # le journal garde la désignation
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer second" in n for n in notes), notes)

    def test_alerte_stale_lot_va_au_relecteur_designe(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        self.register("orchestrateur", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("orchestrateur",))
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._stale(lot["id"])

        alert = self._stale_alert(lot["id"])
        self.assertEqual(alert["project"], "ameesh")
        self.assertEqual(alert["waiting"]["who"], "relecteur")
        briefs = self._briefs(alert)
        self.assertIn("relecteur", briefs)
        self.assertIn("ameesh work milestone %d verdict ok|blocked" % lot["id"],
                      briefs["relecteur"])
        self.assertIn("orchestrateur", briefs)      # les orchestrateurs restent prévenus

    def test_alerte_stale_sans_relecteur_va_a_l_orchestrateur_du_projet(self):
        self.register("orchestrateur", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("orchestrateur",))
        lot = self._lot(app="ameesh")
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._stale(lot["id"])

        alert = self._stale_alert(lot["id"])
        self.assertIsNone(alert["waiting"]["who"])
        briefs = self._briefs(alert)
        self.assertEqual(sorted(briefs), ["orchestrateur"])
        self.assertIn("ameesh work reviewer %d <agent>" % lot["id"],
                      briefs["orchestrateur"])

    def test_stale_lot_ne_reveille_pas_un_assigneur_historique(self):
        """#1862 : un agent qui a réassigné un lot dans les 30 jours n'est pas
        un orchestrateur pour `stale_lot` — la clause historique mettait
        presque toute l'équipe dans les destinataires (32 noms mesurés le
        15/10) et chaque lot stagnant réveillait une vingtaine d'agents sans
        mandat. Les orchestrateurs DÉCLARÉS restent prévenus."""
        self.register("orchestrateur", "claude", cwd=self.tmp)
        self.register("preneur", "claude", cwd=self.tmp)
        self.register("receveur", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name IN ('orchestrateur', 'preneur')")
        lot = self._lot(app="ameesh")
        autre = work.add(self.db, title="à confier", assignee="chef")
        work.assign(self.db, int(autre["id"]), "receveur", actor="preneur")
        # l'historique (L94) compte bien ce preneur comme assigneur…
        self.assertIn("preneur", storage.of(self.db).operations.assigners(since_s=3600.0))
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._stale(lot["id"])

        # …mais il ne reçoit pas l'alerte d'un lot stagnant qui n'est pas le sien
        alert = self._stale_alert(lot["id"])
        self.assertIn("orchestrateur", alert["orchestrators"])
        self.assertNotIn("preneur", alert["orchestrators"])
        briefs = self._briefs(alert)
        self.assertIn("orchestrateur", briefs)
        self.assertNotIn("preneur", briefs)

    def test_le_courrier_de_l_alerte_part_au_relecteur(self):
        self.register("relecteur", "codex", cwd=self.tmp)
        self.register("orchestrateur", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("orchestrateur",))
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._stale(lot["id"])

        alert = self._stale_alert(lot["id"])
        # #3036 : le brief n'est plus déposé à un agent sans bail vivant.
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'"
                        " WHERE name IN ('relecteur', 'orchestrateur')")
        ncfg = notify.parse_config({"default": []})
        notifier = notify.Notifier(self.cfg, ncfg, sender=object(), log=lambda t: None)
        notifier.run_pass(self.db, current=[alert])
        rows = self.db.query(
            "SELECT body, payload FROM agent_mailbox WHERE recipient = %s"
            " AND payload->>'alert' = 'stale_lot'", ("relecteur",))
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("designated reviewer", rows[0]["body"])
        self.assertIn("ameesh work milestone %d verdict" % lot["id"], rows[0]["body"])
        self.assertEqual(rows[0]["payload"]["alert"], "stale_lot")
        self.assertEqual(rows[0]["payload"]["lot"], lot["id"])

    def test_auto_relecture_refusee(self):
        """#705 (revue r1, P2-2) : l'assigné ne peut pas être son relecteur."""
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "auteur", "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("holds lot", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "build")

    def test_designation_refusee_a_un_non_orchestrateur(self):
        """#705 (revue r1, P2-2) : désigner est un geste de pilote, pas d'agent."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "relecteur", "--actor", "auteur")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("designation refused", proc.stderr)

    def test_work_reviewer_designe_un_lot_deja_en_qa(self):
        """#705 (revue r1, P2-5) : changer le relecteur d'un lot en qa, sans
        aller-retour build/qa, et l'alerte recommande cette commande."""
        self.register("premier", "codex", cwd=self.tmp)
        self.register("second", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "premier")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "second",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer second", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "second")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")
        self.assertEqual(rows[0]["waiting_for"]["who"], "premier, second")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer second" in n for n in notes), notes)

        self._stale(lot["id"])
        alert = self._stale_alert(lot["id"])
        self.assertEqual(alert["waiting"]["who"], "premier, second")
        briefs = self._briefs(alert)
        self.assertIn("ameesh work milestone %d verdict" % lot["id"], briefs["second"])

    def test_la_designation_reveille_le_relecteur(self):
        """#2478 : la désignation dépose un `request` lié au lot ; le runner du
        relecteur le voit comme courrier actif, donc un tour s'ouvre."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        rows = self.db.query(
            "SELECT id, kind, work_item_id, body FROM agent_mailbox"
            " WHERE recipient = %s AND payload ? 'review_request' ORDER BY id",
            ("relecteur",))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["kind"], "request")
        self.assertEqual(rows[0]["work_item_id"], str(lot["id"]))
        self.assertIn("lot #%d" % lot["id"], rows[0]["body"])
        self.assertIn("ameesh work milestone %d verdict" % lot["id"],
                      rows[0]["body"])
        self.assertEqual([m["id"] for m in mail.unread_active(self.db, "relecteur")],
                         [rows[0]["id"]])

    def test_work_reviewer_reveille_le_nouveau_relecteur(self):
        """#2478 : la désignation d'un lot déjà en `qa` pousse le même `request`;
        un retrait n'en pousse pas."""
        self.register("premier", "codex", cwd=self.tmp)
        self.register("second", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "premier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sha = "a" * 40
        work.milestone(self.db, lot["id"], "frozen", sha=sha, actor="auteur")

        proc = self.mesh("work", "reviewer", str(lot["id"]), "second",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.db.query(
            "SELECT id, kind, work_item_id, body FROM agent_mailbox"
            " WHERE recipient = %s AND payload ? 'review_request' ORDER BY id",
            ("second",))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]["kind"], rows[0]["work_item_id"]),
                         ("request", str(lot["id"])))
        self.assertIn(sha, rows[0]["body"])
        self.assertEqual(len(mail.unread_active(self.db, "second")), 1)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "second", "--remove",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.db.query(
            "SELECT id FROM agent_mailbox"
            " WHERE recipient = %s AND payload ? 'review_request'", ("second",))
        self.assertEqual(len(rows), 1)

    def test_redesignation_ne_renvoie_pas_de_request(self):
        """#2495 : re-désigner le même relecteur pour la même tête ne dépose pas
        un second `request` ; une nouvelle tête gelée, une fois le premier lu, si."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        self.assertEqual(self._qa(lot, "--reviewer", "relecteur").returncode, 0)

        def requests():
            return self.db.query(
                "SELECT id FROM agent_mailbox WHERE recipient = %s"
                " AND payload ? 'review_request' ORDER BY id", ("relecteur",))

        def redesignate():
            proc = self.mesh("work", "reviewer", str(lot["id"]), "relecteur",
                             "--actor", "chef", env=self._env())
            self.assertEqual(proc.returncode, 0, proc.stderr)

        redesignate()
        self.assertEqual(len(requests()), 1)
        mail.mark_delivered(self.db, [r["id"] for r in requests()])
        work.milestone(self.db, lot["id"], "frozen", sha="b" * 40, actor="auteur")
        redesignate()
        self.assertEqual(len(requests()), 2)
        redesignate()
        self.assertEqual(len(requests()), 2)

    def test_redesignation_retrouve_le_request_hors_fenetre(self):
        """#2504 : une re-désignation retrouve le request du lot même derrière
        plus de 200 courriers plus récents ; le filtre est fait en base par lot
        et nature, pas dans une fenêtre d'historique."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        self.assertEqual(self._qa(lot, "--reviewer", "relecteur").returncode, 0)
        first = self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = %s"
            " AND payload ? 'review_request' ORDER BY id", ("relecteur",))
        self.assertEqual(len(first), 1)
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, work_item_id)"
            " SELECT 'chef', 'relecteur', 'other lot mail', 'notify', '9999'"
            " FROM generate_series(1, 250)")
        proc = self.mesh("work", "reviewer", str(lot["id"]), "relecteur",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.db.query(
            "SELECT id FROM agent_mailbox WHERE recipient = %s"
            " AND payload ? 'review_request' ORDER BY id", ("relecteur",))
        self.assertEqual([r["id"] for r in rows], [r["id"] for r in first])

    def test_le_runner_du_relecteur_prend_un_tour(self):
        """#2478 témoin : la désignation dépose un courrier actif ; le runner du
        relecteur le choisit comme prochain tour, sur le lot du message."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        runner = Runner(self.cfg, self.db, once=True)
        runner.mail_batch = 0.0
        lease = registry.claim(self.db, "relecteur", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, "relecteur"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        self.assertIn("Review request", spec["prompt"])
        self.assertEqual(spec.get("lot"), str(lot["id"]))

    def test_work_reviewer_refuse_hors_qa(self):
        """#705 : la commande ne désigne que dans une revue en cours."""
        lot = self._lot()
        proc = self.mesh("work", "reviewer", str(lot["id"]), "chef",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not qa", proc.stderr)

    # -- #1979 : retirer une désignation, ou la marquer « verdict non requis »

    def test_retrait_de_la_designation(self):
        """#1979 : `--remove` rend le lot à l'attente réelle d'un relecteur."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--remove",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("designation removed", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")
        self.assertEqual(rows[0]["waiting_for"]["label"],
                         "verdict from a reviewer (none designated)")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer designation removed" in n for n in notes), notes)

    def test_verdict_non_requis_dispense_la_revue_et_les_rappels(self):
        """#1979 : `--no-verdict` (résolution mécanique, canon §11 bis/#87) :
        l'attente passe à la fusion, la vue n'est plus « en revue », et
        l'alerte ne réclame ni relecteur ni verdict."""
        self.register("relecteur", "codex", cwd=self.tmp)
        self.register("orchestrateur", "claude", cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("orchestrateur",))
        lot = self._lot(app="ameesh")
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("verdict not required", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], work.REVIEWER_NONE)

        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "merge")
        self.assertEqual(rows[0]["waiting_for"]["label"],
                         "merge (executor not designated)")

        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reviewer : — (verdict not required", proc.stdout)
        self.assertIn("waiting  : merge (executor not designated)", proc.stdout)

        jalons = progress._jalons_de_lot(
            work.get(self.db, lot["id"]),
            storage.of(self.db).progress.lot_transitions([lot["id"]]),
            [], work.milestones(self.db, lot["id"]))
        self.assertFalse(jalons["verdict_required"])
        self.assertEqual(progress.lot_state("qa", jalons), "approved")

        self._stale(lot["id"])
        alert = self._stale_alert(lot["id"])
        self.assertEqual(alert["waiting"]["what"], "merge")
        briefs = self._briefs(alert)
        self.assertIn("orchestrateur", briefs)
        self.assertNotIn("relecteur", briefs)
        for text in briefs.values():
            self.assertNotIn("ameesh work reviewer", text)
            self.assertNotIn("ameesh work milestone", text)
        self.assertIn("merge packet/queue", briefs["orchestrateur"])

    def test_verdict_non_requis_refuse_sur_verdict_bloquant_courant(self):
        """#1979 : un verdict bloquant courant demande une correction, pas une
        dispense — la marque dormirait et dispenserait la revue suivante."""
        lot = self._lot(app="ameesh")
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._freeze_at(lot, "a" * 40)
        work.milestone(self.db, lot["id"], "verdict", verdict="blocked",
                       sha="a" * 40, actor="chef")

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("blocked verdict", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")

    def test_verdict_non_requis_accepte_sur_verdict_bloquant_perime(self):
        """#1979 : un verdict bloquant PÉRIMÉ (gel postérieur) ne barre pas la
        dispense — c'est le cas de la résolution de composition mécanique."""
        lot = self._lot(app="ameesh")
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._freeze_at(lot, "a" * 40)
        work.milestone(self.db, lot["id"], "verdict", verdict="blocked",
                       sha="a" * 40, actor="chef")
        self._freeze_at(lot, "b" * 40)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], work.REVIEWER_NONE)
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "merge")

    def test_la_dispense_vaut_pour_la_tete_de_sa_revue(self):
        """#1979 (relecture ameesh-dev2) : un gel d'une autre tête, ou une
        sortie de qa, lève la dispense — la revue suivante est due ; un gel
        renouvelé sur le même commit la garde."""
        lot = self._lot(app="ameesh")
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        work.milestone(self.db, lot["id"], "frozen", sha="a" * 40, actor="chef")
        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        work.milestone(self.db, lot["id"], "frozen", sha="a" * 40, actor="chef")
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], work.REVIEWER_NONE)

        work.milestone(self.db, lot["id"], "frozen", sha="c" * 40, actor="chef")
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["what"], "verdict")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("verdict required again" in n for n in notes), notes)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        work.move(self.db, lot["id"], "build", actor="chef")
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")

    def test_une_designation_leve_la_dispense(self):
        """#1979 : désigner un relecteur après la dispense rend la revue due."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot(app="ameesh")
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.mesh("work", "reviewer", str(lot["id"]), "relecteur",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "relecteur")
        rows = work.list_items(self.db, limit=10)
        plan.annotate(self.db, rows)
        self.assertEqual(rows[0]["waiting_for"]["label"], "verdict by relecteur")

    def test_les_marques_sont_des_gestes_de_pilote_et_exclusives(self):
        """#1979 : même autorité que la désignation, et un seul geste à la fois."""
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "reviewer", str(lot["id"]), "--no-verdict",
                         "--actor", "auteur", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("designation refused", proc.stderr)

        proc = self.mesh("work", "reviewer", str(lot["id"]),
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("give AGENT", proc.stderr)
        proc = self.mesh("work", "reviewer", str(lot["id"]), "chef", "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mutually exclusive", proc.stderr)
        proc = self.mesh("work", "reviewer", str(lot["id"]), "--remove", "--no-verdict",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 2)
        self.assertIn("mutually exclusive", proc.stderr)

    # -- #705 r2 (B1) : la règle « le relecteur n'est pas l'assigné » tient
    #    aussi APRÈS la désignation, dans l'écriture elle-même --------------

    def test_work_reviewer_refuse_l_assigne(self):
        """#705 r2 (B1) : désigner l'assigné du lot est refusé (l'assigné a pu
        changer depuis la lecture : la règle est aussi dans l'écriture)."""
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "reviewer", str(lot["id"]), "auteur",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("holds lot", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "")

    def test_la_designation_relit_l_assigne_dans_l_ecriture(self):
        """#705 r2 (B1) : `_designate_reviewer` revérifie l'assigné dans
        l'UPDATE même (l'appelant a lu la ligne avant) : rien n'est écrit, et
        la désignation en place reste."""
        self.register("premier", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "premier")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = storage.of(self.db).work.designate_reviewer(lot["id"], "auteur",
                                                          actor="chef")
        self.assertIsNone(row)
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "premier")

    def test_assign_au_relecteur_retire_la_designation(self):
        """#705 r2 (B1) : assigner le lot à son relecteur retire la
        désignation dans la même écriture (sinon il relirait son propre
        travail) ; le journal le dit."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "assign", str(lot["id"]), "relecteur",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["reviewer"]), ("relecteur", ""))
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer relecteur cleared (they now hold the lot)" in n
                            for n in notes), notes)

    def test_assign_ne_bloque_pas_une_ecriture_au_journal_du_lot(self):
        """#813 point 2 : `_reassign` tient la ligne en NO KEY UPDATE — pendant
        qu'il la tient, une insertion au journal du lot (contrôle de clé
        étrangère, FOR KEY SHARE) écrit sans attendre. FOR UPDATE la bloquait
        jusqu'au commit.

        #1946 : le témoin ne parie plus sur une horloge. Il pose la demande
        de verrou du contrôle de clé étrangère elle-même, en NOWAIT : elle
        n'échoue que si un verrou incompatible la précède (FOR UPDATE), et
        jamais parce que la machine est chargée. L'ancien `lock_timeout` de
        200 ms lisait « bloqué » toute attente de verrou plus longue que lui,
        fût-elle étrangère au verrou testé : faux rouge sous charge du
        16/10 (refonte #1425), vert en isolation."""
        from ameesh.storage.postgres.work import WorkItems
        lot = self._lot()
        other = self.connect()
        self.addCleanup(other.close)
        seen = []
        real = WorkItems._event

        def event(items, item_id, state, note, actor):
            try:
                with other.transaction() as tx:
                    # Garde-fou seulement (un verrou de table régresserait en
                    # attente) : 30 s, très au-delà de toute attente légitime.
                    tx.execute("SET LOCAL lock_timeout = '30s'")
                    # Le verrou exact de RI_FKey_check_ins, sans attente.
                    tx.execute("SELECT 1 FROM work_items WHERE id = %s"
                               " FOR KEY SHARE NOWAIT", (int(item_id),))
                    tx.execute("INSERT INTO work_item_events"
                               " (work_item_id, state, note, actor)"
                               " VALUES (%s, 'build', 'concurrent', 'test')",
                               (int(item_id),))
                seen.append("inserted")
            except db_mod.DbError as exc:
                seen.append("blocked: %s" % exc)
            return real(items, item_id, state, note, actor)

        with mock.patch.object(WorkItems, "_event", new=event):
            item = storage.of(self.db).work.assign(lot["id"], "autre",
                                                   current="auteur", note="reassign",
                                                   actor="chef")
        self.assertIsNotNone(item)
        self.assertEqual(seen, ["inserted"], seen)

    def test_delegate_au_relecteur_retire_la_designation(self):
        """#705 r2 (B1) : déléguer le lot à son relecteur retire la
        désignation dans la même écriture ; le journal le dit."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot, "--reviewer", "relecteur")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "delegate", str(lot["id"]), "relecteur",
                         "--within", "30m", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["reviewer"]), ("relecteur", ""))
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer relecteur cleared (they now hold the lot)" in n
                            for n in notes), notes)

    def test_retour_de_delegation_au_relecteur_retire_la_designation(self):
        """#705 (second avis) : le délégant désigné relecteur pendant la
        délégation (il n'était plus l'assigné) ne le reste pas quand le lot
        lui revient à l'échéance (`rendue`) ; le journal le dit."""
        self.register("delegue", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "delegate", str(lot["id"]), "delegue",
                         "--within", "30m", "--actor", "auteur", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh("work", "reviewer", str(lot["id"]), "auteur",
                         "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        done = work.expire_delegations(self.db, now=time.time() + 3600)
        self.assertEqual([d["outcome"] for d in done], ["rendue"])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["reviewer"]), ("auteur", ""))
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("reviewer auteur cleared (they now hold the lot)" in n
                            for n in notes), notes)

    # -- #705 r2 (N1, N2) : la désignation dans la foulée du move ------------

    def test_move_reviewer_sans_la_migration_0421_refuse_avant_d_ecrire(self):
        """#705 r2 (N1) : sans la colonne (migration 0421), `move … qa
        --reviewer` refuse AVANT le move — jamais un lot à moitié désigné."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_sync ON work_items")
        # 0462 : le trigger d'audit dépend aussi de la colonne `reviewer`.
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_audit ON work_items")
        # 0516 : le trigger d'orthographe des noms d'agents aussi.
        self.db.execute("DROP TRIGGER IF EXISTS ameesh_agent_spelling ON work_items")
        self.db.execute("ALTER TABLE work_items DROP COLUMN reviewer")
        self.addCleanup(self._restore_reviewer_column)
        self.db.__dict__.pop("_has_reviewer", None)

        proc = self.mesh("work", "move", str(lot["id"]), "qa",
                         "--reviewer", "relecteur", "--actor", "chef", env=self._env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("0421", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "build")

    def test_move_dit_la_designation_qui_n_a_pas_eu_lieu(self):
        """#705 r2 (N2) : si la désignation ne peut plus se faire (le lot a
        quitté qa, ou l'assigné est devenu le relecteur), le move est écrit
        mais l'échec est dit — `reviewer` n'est jamais affiché à tort."""
        self.register("relecteur", "codex", cwd=self.tmp)
        lot = self._lot()
        proc = self.mesh("work", "move", str(lot["id"]), "build", env=self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        real = storage.of(self.db).work

        class _NoDesignation:
            def __getattr__(self, name):
                return getattr(real, name)

            def designate_reviewer(self, *args, **kwargs):
                return None

        with mock.patch("ameesh.work.storage.of",
                        lambda db: mock.Mock(work=_NoDesignation())):
            with self.assertRaises(work.WorkError) as caught:
                work.move(self.db, lot["id"], "qa", reviewer="relecteur",
                          actor="human:chef")
        self.assertIn("was not designated", str(caught.exception))
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["reviewer"]), ("qa", ""))

    def _restore_reviewer_column(self):
        self.db.execute("ALTER TABLE work_items ADD COLUMN IF NOT EXISTS"
                        " reviewer text NOT NULL DEFAULT ''")
        self.db.__dict__.pop("_has_reviewer", None)
        # DROP COLUMN also drops its UPDATE OF triggers. Restore the schema
        # used by subsequent tests, including compatibility for old writers.
        for migration in ("0456_work_item_reviewers.sql", "0462_reviewer_audit.sql",
                          "0516_agent_rename.sql"):
            with open(os.path.join(REPO, "src/ameesh/migrations", migration),
                      encoding="utf-8") as fh:
                self.db.script(fh.read())

    def test_lectures_avant_la_migration_0421(self):
        """#705 (revue r1, P2-3) : la prod peut être en 0373 ; sans la colonne,
        lectures, vue et alertes fonctionnent (le relecteur est alors inconnu)."""
        lot = self._lot(app="ameesh")
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_sync ON work_items")
        # 0462 : le trigger d'audit dépend aussi de la colonne `reviewer`.
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_audit ON work_items")
        # 0516 : le trigger d'orthographe des noms d'agents aussi.
        self.db.execute("DROP TRIGGER IF EXISTS ameesh_agent_spelling ON work_items")
        self.db.execute("ALTER TABLE work_items DROP COLUMN reviewer")
        self.addCleanup(self._restore_reviewer_column)
        self.db.__dict__.pop("_has_reviewer", None)

        item = work.get(self.db, lot["id"])
        self.assertEqual(item["reviewer"], "")
        rows = work.list_items(self.db, limit=10)
        self.assertEqual(rows[0]["id"], lot["id"])
        # `progress` lit la même liste : sans colonne, `to_jsonb` rend ''
        lots = storage.of(self.db).progress.lots(since_ts=0.0, project=None, limit=10)
        self.assertIn(lot["id"], [r["id"] for r in lots])
        listing = storage.of(self.db).operations.open_lots_activity(10)
        self.assertIn(lot["id"], [r["id"] for r in listing])
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("waiting  : verdict from a reviewer (none designated)",
                      proc.stdout)
        # l'alerte lit la même jointure : sans la colonne, elle est quand même
        # collectée (le relecteur est simplement inconnu), #705 P2-3
        self._stale(lot["id"])
        alertes = exploitation.alerts(self.cfg, self.db,
                                      orchestrators_declared=["orchestrateur"])
        alerte = next(a for a in alertes
                      if a["type"] == "stale_lot" and a["lot"] == lot["id"])
        self.assertIsNone(alerte["waiting"]["who"])

    def test_la_reponse_negative_de_has_reviewer_ne_se_cache_pas(self):
        """#813 (ex-#798) : la réponse négative n'est pas mise en cache — une
        connexion vivante (démon runner, serve) qui a lu « pas de colonne »
        désigne dès que 0421 est appliquée, sans redémarrage ni purge."""
        lot = self._lot()
        proc = self._qa(lot)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_sync ON work_items")
        # 0462 : le trigger d'audit dépend aussi de la colonne `reviewer`.
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_audit ON work_items")
        # 0516 : le trigger d'orthographe des noms d'agents aussi.
        self.db.execute("DROP TRIGGER IF EXISTS ameesh_agent_spelling ON work_items")
        self.db.execute("ALTER TABLE work_items DROP COLUMN reviewer")
        self.addCleanup(self._restore_reviewer_column)
        daemon = self.connect()                 # née avant 0421, aucun cache
        self.addCleanup(daemon.close)

        with self.assertRaises(db_mod.DbError) as caught:   # la colonne manque
            storage.of(daemon).work.designate_reviewer(lot["id"], "chef", actor="chef")
        self.assertIn("0421", str(caught.exception))

        # `ameesh migrate` applique 0421 : la MÊME connexion désigne, sans
        # redémarrage et sans purge du cache.
        self._restore_reviewer_column()
        row = storage.of(daemon).work.designate_reviewer(lot["id"], "chef", actor="chef")
        self.assertIsNotNone(row)
        self.assertEqual(row["reviewer"], "chef")
        self.assertEqual(work.get(self.db, lot["id"])["reviewer"], "chef")


class ReviewerGrantTest(unittest.TestCase):
    """#705 (revue r1, P2-4) : le GRANT du superviseur porte la colonne lue."""

    def test_role_superviseur_lit_reviewer(self):
        path = os.path.join(REPO, "deploy/sql/role-superviseur.sql")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        block = text.split("('work_items', ARRAY[", 1)[1].split("]),", 1)[0]
        self.assertIn("'reviewer'", block)

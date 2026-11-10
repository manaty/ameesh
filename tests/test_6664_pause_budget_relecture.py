# SPDX-License-Identifier: AGPL-3.0-only
"""#6664 — un agent en pause budget n'est ni désigné, ni relancé pour un avis.

Constat du 10/11 : claude1 était en pause budget (compte secondaire jusqu'à
08:30Z, événements #207575/#207577) mais recevait encore des désignations de
relecture (#6513, #6498) et des relances SLO, chaque fois suivi d'une
redésignation manuelle.

Attendu :

* la désignation automatique au gel écarte un siège manuel indisponible
  (pause budget de jauge, arrêt, pause opérateur, hôte fermé) et se replie sur
  un autre siège de la même famille ou d'une famille éligible ;
* un avis déjà dû à un relecteur qui entre en pause n'est pas relancé chez
  lui : l'alerte SLO et la relance du débit vont au pilote, qui désigne un
  autre relecteur ou attend la levée ;
* à la fin de la pause, le siège redevient éligible ;
* s'il n'y a aucun autre siège, la notification de capacité va au pilote.

La fenêtre courte du compte (#3876) n'est pas une pause : elle reste
candidate, comme avant.
"""
from __future__ import annotations

import json
import unittest

from ameesh import alert_briefs, debit, registry, storage, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture
from .test_3036_alert_liveness import _row, _slo

#: la pause réelle de claude1 (registre, 10/11 07:2xZ)
BUDGET = ("budget : pause (account secondaire, five_hour 77 %) until "
          "08:30@1794299400; no claude account to place")
#: la fenêtre courte du compte, écrite sous le même préfixe (#3876)
SHORT = ("budget : account cx1: 2/2 concurrent turns (provider burst bucket "
         "shared by sessions, #3779; running: cx2, dev3)")
HEURE = 3600.0
T = 1794186000.0            # 2026-11-09 01:00:00Z


def row(name, *, harness="claude", status="idle", text="", operator_paused=False):
    return {"name": name, "team": "ameesh", "harness": harness,
            "session_harness": "", "status": status, "mode": "execute",
            "status_text": text, "operator_paused": operator_paused,
            "capabilities": ["read", "report-drift", "propose"],
            "roles": ["implementation"]}


class BudgetPauseReadingTest(unittest.TestCase):
    """La lecture de la pause : jauge indisponible, fenêtre courte éligible."""

    def test_la_pause_de_jauge_est_une_pause_budget(self):
        self.assertTrue(rd.is_budget_pause({"status_text": BUDGET}))
        self.assertFalse(rd.is_available(row("claude1", status="blocked",
                                             text=BUDGET)))

    def test_la_fenetre_courte_n_est_pas_une_pause_budget(self):
        self.assertFalse(rd.is_budget_pause({"status_text": SHORT}))
        self.assertTrue(rd.is_available(row("cx1", status="blocked",
                                             text=SHORT)))

    def test_un_statut_sans_prefixe_budget_n_est_pas_une_pause(self):
        self.assertFalse(rd.is_budget_pause({"status_text": "hôte non prêt"}))


class ManualSeatPauseTest(_ActivationFixture, PgTestCase):
    """Au gel, le siège manuel d'un relecteur en pause ne couvre plus."""

    def _agent(self, name, harness):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute(
            "UPDATE agent_registry SET team = 'ameesh', capabilities ="
            " ARRAY['read','report-drift','propose'], roles = ARRAY['implementation']"
            " WHERE name = %s", (name,))

    def _manual(self, name):
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (self.lot,))
        storage.of(self.db).work.designate_reviewer(
            self.lot, name, actor="human:smichea",
            event="reviewer %s designated" % name)

    def _pause(self, name):
        self.db.execute(
            "UPDATE agent_registry SET status = 'blocked', status_text = %s"
            " WHERE name = %s", (BUDGET, name))

    def _lift(self, name):
        self.db.execute(
            "UPDATE agent_registry SET status = 'idle',"
            " status_text = 'last turn: mail' WHERE name = %s", (name,))

    def freeze(self, *, repo=None):
        """Le gel du lot, toujours rattaché au dépôt de test.

        Sans `repo`, la sonde `freeze_head` interroge le répertoire courant :
        dans un clone partiel (le banc local), un SHA du dépôt de test y
        déclenche un fetch paresseux et fige le témoin. Le dépôt nommé est
        celui où la tête se résout, comme un gel réel (`--repo`).
        """
        threads: list = []
        outcome: dict = {}
        hook = act.hook(self.cfg, outcome)
        row = work.milestone(
            self.db, self.lot, "frozen", sha=self.frozen, actor="author",
            on_frozen=hook, after_commit=threads,
            repo=repo if repo is not None else self.repo.root)
        for record in threads:
            record(self.db.cfg, self.db)
        return row, outcome

    def _refreeze(self, label="lot 2"):
        self.repo.write("src/ameesh/mail.py", label + "\n")
        self.frozen = self.repo.commit(label)
        self.receipts()
        return self.freeze()

    def _new_mails(self, before):
        return self.mailbox()[before:]

    def _requested_on_head(self):
        """Les demandes d'avis en attente qui portent la tête gelée courante.

        #6641 : un regel réécrit sur place la demande en attente d'un
        relecteur (même courrier, âge de file gardé) ; la boîte ne gagne donc
        pas de ligne pour lui. C'est le contenu de la demande qui dit la tête.
        """
        head = str(self.frozen or "").casefold()
        requested = {}
        for row in self.mailbox():
            if row["kind"] != "request" or str(row["work_item_id"]) != str(self.lot):
                continue
            payload = row.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            designation = payload.get("review_designation") or {}
            freeze = (payload.get("review_request") or {}).get("freeze") or {}
            sha = str(designation.get("sha") or freeze.get("sha") or "")
            if sha.casefold() == head:
                requested[row["recipient"]] = sha
        return requested

    def test_un_agent_en_pause_budget_n_est_pas_designe_au_gel(self):
        self._agent("r3", "claude")
        self._pause("r1")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r2", "r3"])
        self.assertEqual(sorted(m["recipient"] for m in self.mailbox()), ["r2", "r3"])

    def test_un_siege_manuel_en_pause_est_remplace_dans_sa_famille(self):
        self.freeze()
        self._agent("m1", "claude")
        self._agent("r3", "claude")
        self._manual("m1")
        self._pause("m1")
        self._pause("r1")            # l'autre relecteur claude est aussi en pause
        before = len(self.mailbox())
        _, out = self._refreeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(sorted(r["name"] for r in out["reviewers"]), ["r2", "r3"])
        # #6641 : la demande en attente de r2 est réécrite sur place sur la
        # nouvelle tête (même courrier, âge gardé) ; seule celle de r3 est un
        # courrier neuf. On lit la tête portée par les demandes, pas les
        # seules lignes ajoutées depuis `before`.
        self.assertEqual(self._requested_on_head(), {
            "r2": self.frozen, "r3": self.frozen})
        self.assertEqual([m["recipient"] for m in self._new_mails(before)], ["r3"])
        self.assertNotIn("m1", [m["recipient"] for m in self.mailbox()])
        # Le siège manuel reste dans la colonne (geste du pilote), mais ne
        # couvre plus : le remplaçant de la même famille est désigné.
        self.assertIn("m1", designated(work.get(self.db, self.lot)))
        self.assertIn("r3", designated(work.get(self.db, self.lot)))

    def test_sans_autre_siege_la_capacite_va_au_pilote(self):
        self.freeze()
        self._agent("m1", "claude")
        self._manual("m1")
        self._pause("m1")
        self._pause("r1")
        self._pause("r2")
        before = len(self.mailbox())
        _, out = self._refreeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertGreaterEqual(out["missing"], 1)
        new = self._new_mails(before)
        self.assertEqual([m["recipient"] for m in new], ["pilot"])
        self.assertEqual(new[0]["kind"], "notify")
        self.assertIn("No implicit admission", new[0]["body"])
        self.assertNotIn("m1", [m["recipient"] for m in new])

    def test_la_fin_de_pause_rend_le_siege_eligible(self):
        self.freeze()
        self._agent("m1", "claude")
        self._manual("m1")
        self._pause("m1")
        self._refreeze("lot 2")      # m1 écarté, aucun courrier pour lui
        self._lift("m1")
        before = len(self.mailbox())
        _, out = self._refreeze("lot 3")
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        new = self._new_mails(before)
        self.assertIn("m1", [m["recipient"] for m in new])
        self.assertIn("m1", designated(work.get(self.db, self.lot)))


class ManualSeatUnitTest(unittest.TestCase):
    """La preuve d'un avis déjà rendu reste valable malgré la pause."""

    FAMILIES = ("claude", "codex", "deepseek")

    def seats(self, *manual, answered=(), status="blocked", text=BUDGET,
              operator_paused=False):
        rows = [row("m1", status=status, text=text,
                    operator_paused=operator_paused)]
        return act._manual_seats(
            list(manual), rows,
            authors=[{"name": "author", "family": "deepseek"}],
            families=self.FAMILIES, answered=answered)

    def test_un_siege_en_pause_ne_couvre_pas(self):
        self.assertEqual(self.seats("m1"), [])

    def test_un_avis_deja_rendu_sur_la_tete_reste_une_preuve(self):
        self.assertEqual([s["name"] for s in self.seats("m1", answered=("m1",))],
                         ["m1"])

    def test_la_fenetre_courte_reste_un_siege(self):
        self.assertEqual([s["name"] for s in self.seats(
            "m1", status="blocked", text=SHORT)], ["m1"])

    def test_la_pause_operateur_garde_son_siege_manuel(self):
        # #2627/#5129 : le sommeil de la feuille de route est levé par la
        # désignation, une pause humaine garde le siège manuel du pilote.
        self.assertEqual([s["name"] for s in self.seats(
            "m1", status="idle", text="last turn: mail",
            operator_paused=True)], ["m1"])


class SloPauseRelayTest(unittest.TestCase):
    """La relance SLO d'un relecteur en pause va au pilote (#6664)."""

    def route(self, *, status="blocked", text=BUDGET, pilot_paused=False,
              auditor=True, pilot=True):
        reviewer = dict(_row("claude1"), status=status, status_text=text)
        listing = [reviewer]
        if pilot:
            listing.append(dict(_row("orch"), operator_paused=pilot_paused))
        if auditor:
            listing.append(_row("auditeur"))
        alert = _slo(level=1, targets=("assignee",), step="green_verdict",
                     agent="claude1", lot=6513, lot_state="qa",
                     value=8000, threshold=7200,
                     waiting={"what": "verdict"})
        return alert_briefs.briefs(alert, listing,
                                   auditor_names=(["auditeur"] if auditor else []),
                                   orchestrator_names=(["orch"] if pilot else []))

    def test_la_relance_passe_au_pilote_et_nomme_la_pause(self):
        briefs = self.route()
        self.assertEqual(set(briefs), {"orch"})
        self.assertIn("budget paused", briefs["orch"])
        self.assertIn("Escalated from level 1 to level 2", briefs["orch"])
        self.assertIn("reviewer claude1 has not delivered", briefs["orch"])

    def test_une_fenetre_courte_reste_destinataire(self):
        self.assertEqual(set(self.route(text=SHORT)), {"claude1"})

    def test_un_relecteur_disponible_reste_destinataire(self):
        self.assertEqual(set(self.route(status="idle", text="last turn: mail")),
                         {"claude1"})

    def test_un_pilote_aussi_en_pause_passe_au_palier_3(self):
        self.assertEqual(set(self.route(pilot_paused=True)), {"auditeur"})

    def test_les_autres_briefs_gardent_leur_regle(self):
        author = dict(_row("claude1"), status="blocked", status_text=BUDGET)
        live, dead = alert_briefs._split_live(
            {"claude1": "ordinary mail"}, {"claude1": author}, set())
        self.assertEqual(live, {"claude1": "ordinary mail"})
        self.assertEqual(dead, {})


class DebitPauseRelayTest(unittest.TestCase):
    """Le débit ne prescrit pas de relancer un relecteur en pause (#6664)."""

    def _item(self, ident=6664):
        return {"id": ident, "title": "un lot gelé", "app": "nexlink",
                "assignee": "dev1", "priority": 2, "branch": None,
                "branch_target": None}

    def _mark(self, ident, freeze_id, names):
        return "review-designation:%d:%d: %s" % (
            ident, freeze_id,
            json.dumps({"state": "designated", "required": 1,
                        "reviewers": [{"name": name, "family": "claude"}
                                      for name in names],
                        "missing": 0, "why": "test",
                        "freeze": {"id": freeze_id, "sha": "a" * 40}}))

    def report(self, *, status="blocked", text=BUDGET):
        freeze_at = T - 4 * HEURE
        item = self._item()
        return debit.build(
            items=[item],
            milestones=[{"work_item_id": item["id"], "id": 100, "kind": "frozen",
                         "at_ts": freeze_at, "sha": "a" * 40,
                         "actor": "auteur"}],
            events=[{"work_item_id": item["id"], "id": 100, "state": "qa",
                     "at_ts": freeze_at, "actor": "pilote",
                     "note": self._mark(item["id"], 100, ["claude1"])}],
            agents=[{"name": "claude1", "harness": "claude", "status": status,
                     "status_text": text, "mode": "execute", "team": "nexlink",
                     "operator_paused": False}],
            turns=[], merges=[], ci_runs=[], jobs=[], now=T,
            installed="1.45.0",
            devices={"names": [], "phrases": [], "sources": [], "unreadable": []},
            hours=6, wait_s=2 * HEURE, ci_hours=24, ci_slow_s=30 * 60,
            families=("claude", "codex", "deepseek"))

    def _wait(self, report):
        rows = [r for r in report["slow_waits"]
                if r["lot"] == 6664 and r["kind"] == "qa"]
        self.assertEqual(len(rows), 1, report["slow_waits"])
        return rows[0]

    def test_un_relecteur_en_pause_renvoie_le_geste_au_pilote(self):
        row = self._wait(self.report())
        self.assertEqual(row["who"], "pilote")
        self.assertIn("pause", row["gesture"])
        self.assertIn("pilote", row["gesture"])
        self.assertTrue(row.get("paused"))

    def test_un_relecteur_actif_reste_nomme(self):
        row = self._wait(self.report(status="idle", text="last turn: mail"))
        self.assertEqual(row["who"], "claude1")
        self.assertNotIn("pause", row["gesture"])

    def test_la_fenetre_courte_reste_une_relance_normale(self):
        row = self._wait(self.report(status="blocked", text=SHORT))
        self.assertEqual(row["who"], "claude1")
        self.assertNotIn("pause", row["gesture"])


if __name__ == "__main__":
    unittest.main()

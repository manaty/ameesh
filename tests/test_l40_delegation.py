# SPDX-License-Identifier: AGPL-3.0-only
"""L40 (décision 0030, point 5) : délégation de lot à échéance.

`ameesh work delegate <lot> <agent> --within 30m` ; à l'échéance, sans tour du
délégué sur le lot, le lot revient au délégant (`work.expire_delegations`,
appelée à chaque passe de l'exécuteur) ; alerte `delegation_expired` ;
affichage dans `work list`, `work show` et `progress`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest

from ameesh import exploitation, mail, progress, registry, storage, work
from ameesh.runner import Runner

from .support import PgTestCase


def _lignes(db, sql, params=()):
    return db.query(sql, params)


class VueTest(unittest.TestCase):
    """`work.delegation_view` : libellés, sans base."""

    def test_libelles(self):
        item = {"state": "build", "assignee": "relais", "delegated_by": "orch",
                "delegated_ts": 1000.0, "due_ts": 1000.0 + 1800}
        vue = work.delegation_view(item, now=1000.0 + 18 * 60)
        self.assertEqual(vue["label"], "délégué par orch, échéance dans 12 min")
        self.assertEqual((vue["due_in_s"], vue["overdue"], vue["settled"]), (720, False, False))
        self.assertEqual(vue["delegate"], "relais")
        vue = work.delegation_view(item, now=1000.0 + 1800 + 5 * 60 + 3)
        self.assertEqual(vue["label"], "délégué par orch, en retard de 5 min")
        self.assertTrue(vue["overdue"])
        vue = work.delegation_view(dict(item, due_ts=None), now=5000.0)
        self.assertEqual(vue["label"], "délégué par orch, échéance soldée (travail constaté)")
        self.assertTrue(vue["settled"])
        self.assertIsNone(work.delegation_view(dict(item, delegated_by=None)))
        self.assertIsNone(work.delegation_view(dict(item, state="closed")))

    def test_durees(self):
        self.assertEqual(work.parse_within("30m"), 1800.0)
        self.assertEqual(work.parse_within("2h"), 7200.0)
        self.assertEqual(work.parse_within(90), 90.0)
        for mauvais in ("", "abc", "0m", -5):
            with self.assertRaises(work.WorkError):
                work.parse_within(mauvais)
        self.assertEqual(work.span(7500), "2 h 05")


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE actions CASCADE")
        self.register("orch", "claude", cwd=self._cwd("orch"))
        self.register("relais", "claude", cwd=self._cwd("relais"))
        self.lot = work.add(self.db, title="lot à confier", assignee="orch")

    def _cwd(self, name):
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _item(self, item_id=None):
        return work.get(self.db, item_id or self.lot["id"])

    def _delegations(self, item_id=None):
        return _lignes(self.db, "SELECT delegate, delegated_by, outcome, resolved_by,"
                       " first_turn_at IS NOT NULL AS tour FROM work_item_delegations"
                       " WHERE work_item_id = %s ORDER BY id", (item_id or self.lot["id"],))

    def _boite(self, recipient):
        return _lignes(self.db, "SELECT sender, recipient, kind, work_item_id, body"
                       " FROM agent_mailbox WHERE recipient = %s ORDER BY id", (recipient,))

    def _echoir(self, minutes=1):
        """Avance l'échéance dans le passé (lot et registre)."""
        self.db.execute("UPDATE work_items SET due_at = now() - make_interval(mins => %s)"
                        " WHERE due_at IS NOT NULL", (minutes,))
        self.db.execute("UPDATE work_item_delegations SET due_at = now() -"
                        " make_interval(mins => %s) WHERE outcome IS NULL", (minutes,))

    def _plus_tard(self, seconds=3600):
        return time.time() + seconds


class DelegationTest(_Base):
    def test_delegation_acceptee(self):
        proc = self.mesh("work", "delegate", str(self.lot["id"]), "relais", "--within", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("délégué à relais par orch, échéance dans 30 min", proc.stdout)
        item = self._item()
        self.assertEqual((item["assignee"], item["delegated_by"]), ("relais", "orch"))
        self.assertAlmostEqual(item["due_ts"] - item["delegated_ts"], 1800.0, delta=1.0)
        notes = [(e["note"], e["actor"]) for e in work.events(self.db, self.lot["id"])]
        self.assertIn(("délégué à relais par orch, échéance dans 30 min (avant : orch)",
                       "orch"), notes)
        # l'événement qui réveille le délégué, lié au lot
        boite = self._boite("relais")
        self.assertEqual(len(boite), 1)
        self.assertEqual((boite[0]["sender"], boite[0]["kind"], boite[0]["work_item_id"]),
                         ("orch", "event", str(self.lot["id"])))
        self.assertIn("revient à orch", boite[0]["body"])
        self.assertEqual([(d["delegate"], d["delegated_by"], d["outcome"])
                          for d in self._delegations()], [("relais", "orch", None)])

    def test_delegant_humain(self):
        lot = work.add(self.db, title="sans assigné")
        proc = self.mesh("work", "delegate", str(lot["id"]), "relais", "--within", "1h")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--actor", proc.stderr)
        proc = self.mesh("work", "delegate", str(lot["id"]), "relais", "--within", "1h",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = self._item(lot["id"])
        self.assertEqual((item["assignee"], item["delegated_by"]), ("relais", "human:proprio"))

    def test_refus(self):
        ident = str(self.lot["id"])
        cas = [
            (["fantome"], "agent inconnu"),
            (["human:proprio"], "humain"),
            (["orch"], "lui-même"),
            (["relais", "--actor", "inconnu"], "délégant inconnu"),
        ]
        registry.upsert(self.db, "coord", harness="codex", mode="externe")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio'"
                        " WHERE name = 'coord'")
        cas.append((["coord"], "externe"))
        for extra, attendu in cas:
            proc = self.mesh("work", "delegate", ident, *extra, "--within", "30m")
            self.assertEqual(proc.returncode, 1, (extra, proc.stdout, proc.stderr))
            self.assertIn(attendu, proc.stderr, extra)
        proc = self.mesh("work", "delegate", ident, "relais", "--within", "bientôt")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("durée illisible", proc.stderr)
        # rien n'a bougé
        item = self._item()
        self.assertEqual((item["assignee"], item["delegated_by"]), ("orch", None))
        self.assertEqual(self._delegations(), [])
        self.assertEqual(self._boite("relais"), [])
        # lot fermé
        work.close(self.db, self.lot["id"], abandoned=True)
        proc = self.mesh("work", "delegate", ident, "relais", "--within", "30m")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("rien à déléguer", proc.stderr)

    def test_agent_arrete_accepte_avec_avertissement(self):
        registry.set_status(self.db, "relais", "stopped", status_text="arrêté à la main",
                            stop_reason="manuel")
        proc = self.mesh("work", "delegate", str(self.lot["id"]), "relais", "--within", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("arrêté", proc.stderr)

    def test_nouvelle_delegation_remplace_l_echeance(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        self.register("second", "codex", cwd=self._cwd("second"))
        out = work.delegate(self.db, self.lot["id"], "second", within="2h", actor="orch")
        self.assertEqual(out["replaced"]["delegate"], "relais")
        item = self._item()
        self.assertEqual((item["assignee"], item["delegated_by"]), ("second", "orch"))
        self.assertAlmostEqual(item["due_ts"] - item["delegated_ts"], 7200.0, delta=1.0)
        self.assertEqual([(d["delegate"], d["outcome"]) for d in self._delegations()],
                         [("relais", "remplacee"), ("second", None)])
        # le délégué redélègue : le lot lui reviendra, à lui
        self.register("tiers", "claude", cwd=self._cwd("tiers"))
        work.delegate(self.db, self.lot["id"], "tiers", within="10m")
        self.assertEqual(self._item()["delegated_by"], "second")

    def test_assign_efface_la_delegation(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        self.register("second", "codex", cwd=self._cwd("second"))
        proc = self.mesh("work", "assign", str(self.lot["id"]), "second", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        item = self._item()
        self.assertEqual((item["assignee"], item["delegated_by"], item["delegated_ts"],
                          item["due_ts"]), ("second", None, None, None))
        self.assertEqual([d["outcome"] for d in self._delegations()], ["annulee"])
        self.assertTrue(any("délégation de orch annulée" in e["note"]
                            for e in work.events(self.db, self.lot["id"])))
        # plus rien à échoir
        self.assertEqual(work.expire_delegations(self.db, self._plus_tard()), [])


class EcheanceTest(_Base):
    def setUp(self):
        super().setUp()
        work.delegate(self.db, self.lot["id"], "relais", within="30m")

    def test_avant_l_echeance_rien(self):
        self.assertEqual(work.expire_delegations(self.db), [])
        self.assertEqual(self._item()["assignee"], "relais")

    def test_sans_tour_le_lot_revient_au_delegant(self):
        out = work.expire_delegations(self.db, self._plus_tard(), actor="exécuteur r1")
        self.assertEqual([(r["outcome"], r["delegated_by"], r["delegate"]) for r in out],
                         [("rendue", "orch", "relais")])
        item = self._item()
        self.assertEqual((item["assignee"], item["delegated_by"], item["delegated_ts"],
                          item["due_ts"]), ("orch", None, None, None))
        self.assertEqual([(d["outcome"], d["resolved_by"]) for d in self._delegations()],
                         [("rendue", "exécuteur r1")])
        notes = [e["note"] for e in work.events(self.db, self.lot["id"])]
        self.assertIn("délégation échue : retour à orch (aucun tour de relais sur le lot "
                      "depuis la délégation)", notes)
        # l'événement réveille le délégant
        boite = self._boite("orch")
        self.assertEqual(len(boite), 1)
        self.assertEqual((boite[0]["sender"], boite[0]["kind"], boite[0]["work_item_id"]),
                         (work.SYSTEM_SENDER, "event", str(self.lot["id"])))
        self.assertIn("délégation échue", boite[0]["body"])
        self.assertEqual(out[0]["message_id"], int(_lignes(
            self.db, "SELECT max(id) AS id FROM agent_mailbox")[0]["id"]))
        # traitée une fois pour toutes
        self.assertEqual(work.expire_delegations(self.db, self._plus_tard()), [])

    def test_delegant_humain_sans_message(self):
        lot = work.add(self.db, title="humain")
        work.delegate(self.db, lot["id"], "relais", within="10m", actor="human:proprio")
        out = {r["work_item_id"]: r for r in
               work.expire_delegations(self.db, self._plus_tard())}
        self.assertEqual(out[lot["id"]]["outcome"], "rendue")
        self.assertIsNone(out[lot["id"]]["message_id"])
        self.assertEqual(self._item(lot["id"])["assignee"], "human:proprio")
        self.assertEqual(self._boite("human:proprio"), [])

    def test_un_tour_du_delegue_solde_la_delegation(self):
        self.assertEqual(work.mark_delegate_turn(self.db, "relais", [self.lot["id"]]),
                         [self.lot["id"]])
        # une seule fois par délégation ; un autre agent ne compte pas
        self.assertEqual(work.mark_delegate_turn(self.db, "relais", [self.lot["id"]]), [])
        self.assertEqual(work.mark_delegate_turn(self.db, "orch", [str(self.lot["id"])]), [])
        self.assertTrue(any(e["note"] == "délégation : tour de relais sur le lot"
                            and e["actor"] == "relais"
                            for e in work.events(self.db, self.lot["id"])))
        out = work.expire_delegations(self.db, self._plus_tard())
        self.assertEqual([r["outcome"] for r in out], ["soldee"])
        item = self._item()
        self.assertEqual((item["assignee"], item["delegated_by"], item["due_ts"]),
                         ("relais", "orch", None))
        self.assertEqual(work.delegation_view(item)["label"],
                         "délégué par orch, échéance soldée (travail constaté)")
        self.assertEqual(self._boite("orch"), [])
        self.assertEqual([(d["outcome"], d["tour"]) for d in self._delegations()],
                         [("soldee", True)])

    def test_autres_preuves_de_travail(self):
        preuves = {
            "note": lambda i: work.note(self.db, i, "j'avance", actor="relais"),
            "jalon": lambda i: work.milestone(self.db, i, "frozen", sha="abc", actor="relais"),
            "message": lambda i: mail.send(self.db, "relais", "orch", "point d'étape",
                                           work_item_id=str(i)),
            "transition": lambda i: work.move(self.db, i, "build", actor="agent:relais"),
        }
        lots = {}
        for nom, geste in preuves.items():
            lot = work.add(self.db, title=nom, assignee="orch")
            work.delegate(self.db, lot["id"], "relais", within="30m")
            geste(lot["id"])
            lots[lot["id"]] = nom
        # le travail d'un AUTRE (le délégant) ne compte pas pour le délégué
        work.note(self.db, self.lot["id"], "tu en es où ?", actor="orch")
        out = {r["work_item_id"]: r["outcome"]
               for r in work.expire_delegations(self.db, self._plus_tard())}
        self.assertEqual({lots[i]: out[i] for i in lots},
                         {nom: "soldee" for nom in preuves})
        self.assertEqual(out[self.lot["id"]], "rendue")

    def test_lot_ferme_entre_temps(self):
        work.close(self.db, self.lot["id"], abandoned=True)
        out = work.expire_delegations(self.db, self._plus_tard())
        self.assertEqual([r["outcome"] for r in out], ["annulee"])
        item = self._item()
        self.assertEqual((item["state"], item["assignee"], item["delegated_by"]),
                         ("closed", "relais", None))
        self.assertEqual(self._boite("orch"), [])

    def test_dry_run_n_ecrit_rien(self):
        self._echoir()
        proc = self.mesh("work", "expire-delegations", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[dry-run] lot #%d : rendu à orch" % self.lot["id"], proc.stdout)
        self.assertEqual(self._item()["assignee"], "relais")
        self.assertEqual([d["outcome"] for d in self._delegations()], [None])
        self.assertEqual(self._boite("orch"), [])
        proc = self.mesh("work", "expire-delegations", "--dry-run", "--json")
        lignes = json.loads(proc.stdout)
        self.assertEqual([(l["outcome"], l["dry_run"]) for l in lignes], [("rendue", True)])
        proc = self.mesh("work", "expire-delegations")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d : rendu à orch" % self.lot["id"], proc.stdout)
        self.assertEqual(self._item()["assignee"], "orch")
        proc = self.mesh("work", "expire-delegations")
        self.assertIn("aucune délégation échue", proc.stdout)

    def test_deux_executeurs_un_seul_retour(self):
        self._echoir()
        connexions = [self.connect() for _ in range(4)]
        resultats: list = []
        erreurs: list = []
        depart = threading.Barrier(len(connexions))

        def passe(db):
            try:
                depart.wait(timeout=10)
                resultats.extend(work.expire_delegations(db, actor="exécuteur"))
            except Exception as exc:  # noqa: BLE001 - remonté par l'assertion
                erreurs.append(exc)

        fils = [threading.Thread(target=passe, args=(db,)) for db in connexions]
        for fil_ in fils:
            fil_.start()
        for fil_ in fils:
            fil_.join(timeout=60)
        for db in connexions:
            db.close()
        self.assertEqual(erreurs, [])
        self.assertEqual([r["outcome"] for r in resultats], ["rendue"])
        self.assertEqual(len(self._boite("orch")), 1)
        echues = [e for e in work.events(self.db, self.lot["id"])
                  if e["note"].startswith("délégation échue")]
        self.assertEqual(len(echues), 1)
        # et la même délégation relue après coup : rien
        st = storage.of(self.db).work
        ident = _lignes(self.db, "SELECT id FROM work_item_delegations")[0]["id"]
        self.assertIsNone(st.resolve_delegation(ident, now_ts=self._plus_tard(), actor="x",
                                                describe=None))


class ExecuteurTest(_Base):
    def test_chaque_passe_traite_les_echeances(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        runner = Runner(self.cfg, self.db, agents=["personne"], once=True)
        runner.sweep()
        self.assertEqual(self._item()["assignee"], "relais")
        self._echoir()
        runner.sweep()
        self.assertEqual(self._item()["assignee"], "orch")
        self.assertEqual(len(self._boite("orch")), 1)
        # en dry-run, rien n'est écrit
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        self._echoir()
        Runner(self.cfg, self.db, agents=["personne"], once=True, dry_run=True).sweep()
        self.assertEqual(self._item()["assignee"], "relais")

    def test_le_tour_du_delegue_est_note_par_l_executeur(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        proc = self.runner("--once", "--agents", "relais")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.turns(), "aucun tour lancé")
        self.assertEqual([d["tour"] for d in self._delegations()], [True])
        out = work.expire_delegations(self.db, self._plus_tard())
        self.assertEqual([r["outcome"] for r in out], ["soldee"])
        self.assertEqual(self._item()["assignee"], "relais")

    def test_l_executeur_rend_le_lot_sans_tour(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        self._echoir()
        proc = self.runner("--once", "--agents", "personne")
        self.assertIn("délégation échue : lot #%d rendu à orch" % self.lot["id"],
                      proc.stdout + proc.stderr)
        self.assertEqual(self._item()["assignee"], "orch")


class AlerteTest(_Base):
    def _alertes(self, **kw):
        return [a for a in exploitation.alerts(self.cfg, self.db, **kw)
                if a["type"] == "delegation_expired"]

    def test_en_retard_puis_rendu(self):
        self.db.execute("UPDATE agent_registry SET responsible = 'human:proprio'"
                        " WHERE name = 'orch'")
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        self.assertEqual(self._alertes(), [])
        # L46 : rien pendant le délai de grâce (5 min par défaut)
        self._echoir(minutes=2)
        self.assertEqual(self._alertes(), [])
        self.assertEqual(len(self._alertes(delegation_grace_s=60)), 1)
        self._echoir(minutes=6)
        alertes = self._alertes()
        self.assertEqual(len(alertes), 1)
        alerte = alertes[0]
        self.assertEqual((alerte["reason"], alerte["agent"], alerte["lot"], alerte["title"],
                          alerte["delegate"], alerte["delegated_by"], alerte["responsible"]),
                         ("en_retard", "relais", self.lot["id"], "lot à confier", "relais",
                          "orch", "human:proprio"))
        self.assertGreaterEqual(alerte["value"], 359)
        self.assertIsNotNone(alerte["due_ts"])
        work.expire_delegations(self.db)
        alertes = self._alertes()
        self.assertEqual([(a["reason"], a["delegated_by"]) for a in alertes],
                         [("rendu", "orch")])
        self.assertEqual(alertes[0]["responsible"], "human:proprio")
        # au-delà d'une heure, plus d'alerte
        self.assertEqual(self._alertes(now=self._plus_tard(2 * 3600)), [])
        # clé de --follow : en retard puis rendu, deux alertes distinctes
        self.assertIn("delegation_expired", exploitation.ALERT_TYPES)

    def test_delegant_humain_est_le_responsable(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m", actor="human:chef")
        work.expire_delegations(self.db, self._plus_tard())
        alertes = self._alertes()
        self.assertEqual([(a["reason"], a["responsible"]) for a in alertes],
                         [("rendu", "human:chef")])

    def test_cli_json(self):
        work.delegate(self.db, self.lot["id"], "relais", within="30m")
        self._echoir(minutes=6)
        proc = self.mesh("alerts", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lignes = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        lignes = [l for l in lignes if l["type"] == "delegation_expired"]
        self.assertEqual(len(lignes), 1)
        self.assertTrue({"lot", "title", "delegate", "delegated_by", "due_ts",
                         "responsible"} <= set(lignes[0]))


class AffichageTest(_Base):
    def setUp(self):
        super().setUp()
        work.delegate(self.db, self.lot["id"], "relais", within="30m")

    def test_work_list_et_show(self):
        proc = self.mesh("work", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("délégué par orch, échéance dans", proc.stdout)
        rows = json.loads(self.mesh("work", "list", "--json").stdout)
        self.assertEqual(rows[0]["delegated_by"], "orch")
        self.assertIsNotNone(rows[0]["due_ts"])
        self.assertEqual(rows[0]["delegation"]["delegate"], "relais")
        self.assertFalse(rows[0]["delegation"]["overdue"])
        # les clés d'avant restent
        self.assertTrue({"assignee", "waiting_for", "stale", "delays"} <= set(rows[0]))
        proc = self.mesh("work", "show", str(self.lot["id"]))
        self.assertIn("délégué  : délégué par orch, échéance dans", proc.stdout)
        item = json.loads(self.mesh("work", "show", str(self.lot["id"]), "--json").stdout)
        self.assertEqual(item["delegation"]["delegated_by"], "orch")
        self._echoir(minutes=7)
        proc = self.mesh("work", "list")
        self.assertIn("délégué par orch, en retard de 7 min", proc.stdout)

    def test_progress(self):
        snap = progress.snapshot(self.db, self.cfg)
        lot = next(l for l in snap["lots"] if l["id"] == self.lot["id"])
        self.assertEqual(lot["delegation"]["delegated_by"], "orch")
        self.assertIn("délégué par orch, échéance dans", progress.format_text(snap, 100))
        self._echoir(minutes=3)
        snap = progress.snapshot(self.db, self.cfg)
        self.assertIn("en retard de 3 min", progress.format_text(snap, 100))
        proc = self.mesh("progress", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lots = json.loads(proc.stdout)["lots"]
        self.assertTrue(lots[0]["delegation"]["overdue"])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#3070 (canon §12) : un appareil indisponible lève la porte de sa séance."""
from __future__ import annotations

import datetime
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import canon, db as D, device_gates, runner, storage, work
from tests.support import PgTestCase
from tests.test_canon import _TmpMixin, codes, fiche, write

TODAY = datetime.date.today()
FUTURE = (TODAY + datetime.timedelta(days=7)).isoformat()
PAST = (TODAY - datetime.timedelta(days=1)).isoformat()


def host(name, until=None, reason=None):
    policy = canon.HostPolicy(max_agents=0)
    return canon.Host(name, "human:x", policy, canon.Fiche("Host", name, "m", "p", "r", {}),
                      unavailable_until=until, unavailable_reason=reason)


def canons(*hosts):
    return [types.SimpleNamespace(hosts=list(hosts))]


def ev(ident, state, note, ts=None):
    return {"id": ident, "work_item_id": 1, "state": state, "note": note,
            "actor": "dev", "created_ts": float(ts if ts is not None else ident)}


BLOCKED = {"id": 1, "state": "blocked", "title": "écran GCash"}


class NoteTest(unittest.TestCase):
    def test_note_ecrite_et_relue(self):
        text = device_gates.session_note(["nx-carina", "nx-marco", "nx-carina"], "cas iOS")
        self.assertEqual(text, "séance: nx-carina, nx-marco — cas iOS")
        self.assertEqual(device_gates.parse_session(text),
                         {"devices": ["nx-carina", "nx-marco"], "cases": "cas iOS"})
        self.assertEqual(device_gates.parse_session("seance: pc-1"),
                         {"devices": ["pc-1"], "cases": ""})

    def test_note_illisible_ne_declare_rien(self):
        for text in ("séance:", "séance: — cas", "séance: nom/invalide", "build → blocked",
                     "hold: attend la séance"):
            self.assertIsNone(device_gates.parse_session(text), text)
        with self.assertRaises(work.WorkError):
            device_gates.session_note(["nom invalide"])


class AssessTest(unittest.TestCase):
    def assess(self, events, *hosts, item=BLOCKED, **kwargs):
        return device_gates.assess(item, events, hosts=device_gates.hosts_by_name(
            canons(*hosts)), now=time.time(), **kwargs)

    def journal(self, *extra):
        return [ev(1, "intake", "created"), ev(2, "qa", "build → qa"),
                ev(3, "blocked", "séance: nx-carina — cas iOS"), *extra]

    def test_date_future_porte_levable_retour_a_l_etat_d_avant(self):
        view = self.assess(self.journal(), host("nx-carina", FUTURE, "absente"))
        self.assertTrue(view["liftable"], view)
        self.assertEqual((view["before"], view["back_to"]), ("qa", "qa"))
        self.assertEqual(view["cases"], "cas iOS")
        self.assertIn("qualification physique reportée", device_gates.label(view))

    def test_date_passee_la_regle_cesse(self):
        view = self.assess(self.journal(), host("nx-carina", PAST))
        self.assertFalse(view["liftable"])
        self.assertIn("available", " ".join(view["others"]))

    def test_blocage_mixte_reste_bloque(self):
        dep = [{"blocker_id": 9, "blocking": True}]
        view = self.assess(self.journal(), host("nx-carina", FUTURE), dependencies=dep)
        self.assertFalse(view["liftable"])
        self.assertEqual(view["others"], ["open dependency on lot #9"])
        # un appareil disponible à côté de l'indisponible : la séance reste due
        events = self.journal(ev(4, "blocked", "séance: pc-smichea"))
        view = self.assess(events, host("nx-carina", FUTURE), host("pc-smichea"))
        self.assertFalse(view["liftable"])
        self.assertEqual(view["others"], ["session on pc-smichea, available"])
        # une demande de décision, une autre retenue, un appareil inconnu
        view = self.assess(self.journal(), host("nx-carina", FUTURE),
                           decisions=[{"id": 77}])
        self.assertEqual(view["others"], ["open decision request #77"])
        view = self.assess(self.journal(ev(4, "blocked", "hold: attend le go owner")),
                           host("nx-carina", FUTURE))
        self.assertEqual(view["others"], ["hold by dev"])
        view = self.assess(self.journal(), host("autre", FUTURE))
        self.assertFalse(view["liftable"])

    def test_retenue_levee_ou_retenue_de_seance(self):
        events = self.journal(ev(4, "blocked", "hold: attend le go owner"),
                              ev(5, "blocked", "unhold: go reçu"))
        self.assertTrue(self.assess(events, host("nx-carina", FUTURE))["liftable"])
        events = [ev(1, "build", "x"), ev(2, "blocked", "hold: séance: nx-carina — cas")]
        view = self.assess(events, host("nx-carina", FUTURE))
        self.assertTrue(view["liftable"])
        self.assertEqual(view["back_to"], "build")

    def test_seance_d_un_sejour_precedent_ne_compte_pas(self):
        events = [ev(1, "build", "x"), ev(2, "blocked", "séance: nx-carina"),
                  ev(3, "build", "repris"), ev(4, "blocked", "attend une revue")]
        self.assertIsNone(self.assess(events, host("nx-carina", FUTURE)))
        self.assertIsNone(self.assess(events[:3], host("nx-carina", FUTURE),
                                      item=dict(BLOCKED, state="build")))

    def test_declaration_illisible_garde_le_lot_bloque(self):
        # #3070 B2 : une séance illisible après une séance lisible retient le lot
        events = self.journal(ev(4, "blocked", "séance: appareil/inconnu — cas B"))
        view = self.assess(events, host("nx-carina", FUTURE))
        self.assertFalse(view["liftable"])
        self.assertEqual(view["others"], [
            "unreadable session declaration 'séance: appareil/inconnu — cas B'"])
        self.assertEqual(view["cases"], "cas iOS")
        # la même, posée en retenue ; et seule, elle est encore dite
        events = self.journal(ev(4, "blocked", "hold: séance: — cas B"))
        self.assertFalse(self.assess(events, host("nx-carina", FUTURE))["liftable"])
        alone = [ev(1, "build", "x"), ev(2, "blocked", "séance: a/b")]
        view = self.assess(alone, host("nx-carina", FUTURE))
        self.assertFalse(view["liftable"])
        self.assertIn("session declaration unreadable → stays blocked", device_gates.label(view))

    def test_lot_de_verification_jamais_leve(self):
        item = dict(BLOCKED, title="%s de nx-carina : séance reportée de #1 — x"
                    % device_gates.VERIFY_TITLE)
        view = self.assess(self.journal(), host("nx-carina", FUTURE), item=item)
        self.assertFalse(view["liftable"])


class CanonFieldTest(_TmpMixin, unittest.TestCase):
    def load(self, **fields):
        root = self.example_copy()
        write(root, "hotes/nx-carina.md", fiche(type="Host", title="nx-carina",
                                                 responsible="human:bruno",
                                                 policy="{max_agents: 0}", **fields))
        loaded = canon.load(root, untrusted=True)
        found = next(h for h in loaded.hosts if h.title == "nx-carina")
        return found, canon.validate(loaded)

    def test_date_future_lue(self):
        found, findings = self.load(indisponible_jusqu_au=FUTURE,
                                    indisponible_motif="'Mac de Carina absent'")
        self.assertEqual((found.unavailable_until, found.unavailable_reason),
                         (FUTURE, "Mac de Carina absent"))
        self.assertTrue(found.unavailable_at(time.time()))
        self.assertNotIn("host-unavailable-invalid", codes(findings))
        self.assertNotIn("host-unavailable-past", codes(findings))

    def test_date_passee_information_seulement(self):
        found, findings = self.load(indisponible_jusqu_au=PAST)
        self.assertFalse(found.unavailable_at(time.time()))
        self.assertIn("host-unavailable-past", codes(findings, canon.INFO))

    def test_date_illisible_erreur_et_rien_declare(self):
        found, findings = self.load(indisponible_jusqu_au="'30/10/2026'")
        self.assertIsNone(found.unavailable_until)
        self.assertIn("host-unavailable-invalid", codes(findings, canon.ERROR))
        found, findings = self.load(indisponible_motif="absent")
        self.assertIsNone(found.unavailable_until)
        self.assertIn("host-unavailable-invalid", codes(findings, canon.WARNING))


class LiftTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")

    def blocked(self, devices=("nx-carina",), cases="cas iOS", before="qa"):
        ident = work.add(self.db, title="écran GCash", app="nexlink")["id"]
        work.move(self.db, ident, "build", actor="dev")
        if before == "qa":
            work.move(self.db, ident, "qa", actor="dev")
        work.move(self.db, ident, "blocked", actor="dev",
                  note=device_gates.session_note(devices, cases))
        return ident

    def test_levee_trace_et_lot_de_verification(self):
        ident = self.blocked()
        gates = canons(host("nx-carina", FUTURE, "absente"))
        done = device_gates.lift(self.db, canons=gates)
        self.assertEqual(len(done), 1)
        self.assertEqual(work.get(self.db, ident)["state"], "qa")
        trace = work.events(self.db, ident, 1)[0]
        self.assertTrue(trace["note"].startswith(device_gates.TRACE), trace)
        self.assertIn("nx-carina indisponible jusqu'au %s (absente)" % FUTURE, trace["note"])
        self.assertIn("cas non joués : cas iOS", trace["note"])
        self.assertIn("#%d" % done[0]["verify"], trace["note"])
        verify = work.get(self.db, done[0]["verify"])
        self.assertEqual((verify["state"], verify.get("assignee"), verify["priority"],
                          verify["app"]), ("intake", None, 3, "nexlink"))
        self.assertTrue(verify["title"].startswith(device_gates.VERIFY_TITLE))
        plans = storage.of(self.db).roadmap.item_plans([done[0]["verify"]])
        back = (datetime.date.fromisoformat(FUTURE) + datetime.timedelta(days=1)).isoformat()
        self.assertEqual(str(plans[0]["planned_start"])[:10], back)
        # le lot de vérification ne bloque rien, et la passe suivante ne refait rien
        self.assertEqual(storage.of(self.db).work_dependencies.for_items([ident]), [])
        self.assertEqual(device_gates.lift(self.db, canons=gates), [])

    def test_date_passee_ou_blocage_mixte_restent_bloques(self):
        ident = self.blocked(before="build")
        self.assertEqual(device_gates.lift(self.db, canons=canons(host("nx-carina", PAST))), [])
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")
        other = work.add(self.db, title="prérequis")["id"]
        storage.of(self.db).work_dependencies.add(ident, other, actor="test")
        gates = canons(host("nx-carina", FUTURE))
        self.assertEqual(device_gates.lift(self.db, canons=gates), [])
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")
        view = device_gates.of_item(self.db, work.get(self.db, ident), canons=gates)
        self.assertEqual(view["others"], ["open dependency on lot #%d" % other])
        # le prérequis retiré, la séance seule retient le lot : il repart en build
        storage.of(self.db).work_dependencies.remove(ident, other, actor="test")
        done = device_gates.lift(self.db, canons=gates)
        self.assertEqual([(d["lot"], d["state"]) for d in done], [(ident, "build")])

    def test_vue_perimee_jamais_appliquee(self):
        # #3070 B1 : la levée réévalue sous verrou ; une dépendance posée après
        # la lecture groupée garde le lot bloqué, sans lot de vérification
        ident = self.blocked()
        gates = canons(host("nx-carina", FUTURE))
        views = device_gates.candidates(self.db, canons=gates)
        self.assertTrue(views[0]["liftable"])
        other = work.add(self.db, title="prérequis")["id"]
        storage.of(self.db).work_dependencies.add(ident, other, actor="test")
        hosts = device_gates.hosts_by_name(gates)
        self.assertIsNone(device_gates.lift_one(self.db, ident, hosts=hosts, now=time.time()))
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")
        self.assertEqual(len(work.list_items(self.db)), 2)
        # une séance illisible ajoutée après la lecture, de même
        storage.of(self.db).work_dependencies.remove(ident, other, actor="test")
        work.note(self.db, ident, "séance: x/y — cas B", actor="dev")
        self.assertIsNone(device_gates.lift_one(self.db, ident, hosts=hosts, now=time.time()))
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")

    def test_levee_attend_une_arete_en_cours_et_la_voit(self):
        # #3070 B1 : une arête en cours d'écriture (verrou de graphe tenu par
        # une autre transaction) est attendue par la levée, puis relue
        ident = self.blocked()
        other = work.add(self.db, title="prérequis")["id"]
        hosts = device_gates.hosts_by_name(canons(host("nx-carina", FUTURE)))
        conn = D.connect(self.cfg)
        self.addCleanup(conn.close)
        scope = conn.transaction()
        tx = scope.__enter__()
        committed = False
        try:
            self.assertEqual(storage.of(tx).work_dependencies.add(ident, other, actor="test"),
                             {"added": True})
            results = []
            worker = threading.Thread(target=lambda: results.append(device_gates.lift_one(
                D.connect(self.cfg), ident, hosts=hosts, now=time.time())), daemon=True)
            worker.start()
            worker.join(timeout=1.5)
            self.assertTrue(worker.is_alive(), "the lift must wait for the graph lock")
            scope.__exit__(None, None, None)
            committed = True
            worker.join(timeout=20)
            self.assertFalse(worker.is_alive())
        finally:
            if not committed:
                scope.__exit__(RuntimeError, RuntimeError("abort"), None)
        self.assertEqual(results, [None])
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")

    def test_dry_run_n_ecrit_rien(self):
        ident = self.blocked()
        done = device_gates.lift(self.db, canons=canons(host("nx-carina", FUTURE)),
                                 dry_run=True)
        self.assertEqual(done, [{"lot": ident, "state": "qa", "dry_run": True}])
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")
        self.assertEqual(len(work.list_items(self.db)), 1)

    def test_passe_du_runner_espacee_et_signee(self):
        ident = self.blocked()
        fake = types.SimpleNamespace(db=self.db, cfg=None, runner_id="r1", dry_run=False)
        gates = canons(host("nx-carina", FUTURE))
        with mock.patch.object(work, "configured_canons", return_value=gates):
            mediated = types.SimpleNamespace(**vars(fake), mediated=True)
            self.assertEqual(runner.Runner.lift_device_gates_once(mediated, now=10.0), [])
            done = runner.Runner.lift_device_gates_once(fake, now=1000.0)
            self.assertEqual([d["lot"] for d in done], [ident])
            self.assertEqual(work.events(self.db, ident, 1)[0]["actor"],
                             "device-gates (runner r1)")
            ident2 = self.blocked()
            self.assertEqual(runner.Runner.lift_device_gates_once(fake, now=1100.0), [])
            self.assertEqual(work.get(self.db, ident2)["state"], "blocked")
            later = 1000.0 + runner.DEVICE_GATES_EVERY_S
            self.assertEqual([d["lot"] for d in
                              runner.Runner.lift_device_gates_once(fake, now=later)], [ident2])

    def test_cli_move_device_show_et_device_gates(self):
        ident = work.add(self.db, title="écran GCash")["id"]
        work.move(self.db, ident, "build", actor="dev")
        refused = self.mesh("work", "move", str(ident), "qa", "--device", "nx-carina")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        moved = self.mesh("work", "move", str(ident), "blocked", "--device", "nx-carina",
                          "--note", "cas iOS", "--actor", "dev")
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertEqual(work.events(self.db, ident, 1)[0]["note"],
                         "séance: nx-carina — cas iOS")
        # sans canon configuré, l'appareil est inconnu : le lot reste bloqué
        shown = self.mesh("work", "show", str(ident))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("device   : session nx-carina (unknown device) → stays blocked",
                      shown.stdout)
        listed = self.mesh("work", "device-gates")
        self.assertIn("lot #%d: session nx-carina (unknown device)" % ident, listed.stdout)
        applied = self.mesh("work", "device-gates", "--apply")
        self.assertIn("no lot lifted", applied.stdout)
        self.assertEqual(work.get(self.db, ident)["state"], "blocked")


if __name__ == "__main__":
    unittest.main()

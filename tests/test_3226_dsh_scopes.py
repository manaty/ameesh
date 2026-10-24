# SPDX-License-Identifier: AGPL-3.0-only
"""#3226 : les scopes systemd `dsh-subprocess-*` qu'un tour clos laisse derrière
lui (dsh sans la garde #490 : agents-2, quatre scopes du tour 1eafef02 vivants
dix jours) sont récoltés par le ménage ; un scope d'un dsh vivant ou d'un tour
en cours est gardé ; un scope non attribuable est gardé et signalé.

Témoins réels : de vrais processus rangés dans une hiérarchie cgroup factice
(`cgroup.procs`), relevés et tués par `platform.dsh_scopes`."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ameesh import exploitation, menage, storage
from ameesh.platform import dsh_scopes, is_linux

from .support import PgTestCase

HOST = "agents-test"
TURN = "1eafef023e7945efae973c2d4fdc9143"


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _scope(dsh_pid=None, *, owner_alive=False, turns=(TURN,), unread=False,
           unreadable=False, start=None, unit=None):
    start = time.time() - 7200 if start is None else start
    members = [{"pid": 4000 + i, "start": start, "turn": t, "unread": unread,
                "tmpdir": "/home/a/.local/state/ameesh/.menage/tmp/mty-nxl-rel-01",
                "command": "bash fakes/az group exists"} for i, t in enumerate(turns)]
    dsh_pid = 2846133 if dsh_pid is None else dsh_pid
    return {"unit": unit or "dsh-subprocess-%d-72deff11d18c.scope" % dsh_pid,
            "path": "/x", "dsh_pid": dsh_pid, "owner_alive": owner_alive,
            "members": members, "unreadable": unreadable}


def _row(status="done", ended=None, host=HOST):
    return {"turn_id": TURN, "agent": "mty-nxl-rel-01", "host": host, "status": status,
            "started_ts": time.time() - 9000,
            "ended_ts": time.time() - 7200 if ended is None else ended}


class VerdictTest(unittest.TestCase):
    """Le verdict seul : récolté, gardé (attente), gardé et signalé."""

    def verdict(self, scope, row, now=None):
        return menage.dsh_scope_verdict(scope, row, HOST, time.time() if now is None else now)

    def test_tour_clos_apres_la_grace_recolte(self):
        action, why, turn = self.verdict(_scope(), _row())
        self.assertEqual((action, turn), ("kill", TURN))
        self.assertIn("dsh pid 2846133 gone", why)

    def test_tour_orphelin_sans_fin_mesure_la_grace_depuis_le_processus(self):
        self.assertEqual(self.verdict(_scope(), _row("orphan", ended="x"))[0], "kill")
        jeune = _scope(start=time.time() - 60)
        self.assertEqual(self.verdict(jeune, dict(_row("orphan"), ended_ts=None))[0], "wait")

    def test_tour_vivant_ou_dsh_vivant_garde(self):
        self.assertEqual(self.verdict(_scope(owner_alive=True), _row())[0], "wait")
        self.assertEqual(self.verdict(_scope(), _row("running"))[0], "wait")
        # tour fini il y a une minute : délai de grâce
        self.assertEqual(self.verdict(_scope(), _row(ended=time.time() - 60))[0], "wait")

    def test_non_attribuable_garde_et_signale(self):
        for scope, row in ((_scope(unread=True), _row()),
                           (_scope(unreadable=True), _row()),
                           (_scope(turns=(None,)), _row()),
                           (_scope(turns=(TURN, "autre")), _row()),
                           (_scope(), None),
                           (_scope(), _row(host="autre-hote"))):
            with self.subTest(scope=scope, row=row):
                self.assertEqual(self.verdict(scope, row)[0], "signal")


@unittest.skipUnless(is_linux(), "requires Linux /proc")
class ReleveEtSignalTest(unittest.TestCase):
    """`platform.dsh_scopes` sur une hiérarchie factice peuplée de vrais
    processus : relevé, attribution par l'environnement, TERM puis KILL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap)
        self.uid = os.getuid()
        self.app = dsh_scopes.manager_dir(self.uid, self.tmp.name) / "app.slice"
        self.app.mkdir(parents=True)

    def _reap(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def spawn(self, argv, turn=TURN):
        env = dict(os.environ, AMEESH_TURN_ID=turn) if turn else {
            k: v for k, v in os.environ.items() if k != "AMEESH_TURN_ID"}
        proc = subprocess.Popen(argv, env=env)
        self.procs.append(proc)
        return proc

    def place(self, unit, *procs):
        scope = self.app / unit
        scope.mkdir()
        (scope / "cgroup.procs").write_text("".join("%d\n" % p.pid for p in procs))

    def scan(self):
        return {s["unit"]: s for s in dsh_scopes.scan(uid=self.uid, root_dir=self.tmp.name)}

    def test_releve_attribue_et_distingue_le_dsh_vivant(self):
        mort = _dead_pid()
        orphelin = self.spawn(["sleep", "60"])
        self.place("dsh-subprocess-%d-72deff11d18c.scope" % mort, orphelin)
        # le dsh vivant (ce processus de test) a démarré avant son outil
        vivant = self.spawn(["sleep", "60"])
        self.place("dsh-subprocess-%d-1dfd2fb208eb.scope" % os.getpid(), vivant)
        (self.app / ("dsh-subprocess-%d-393a6d6a7245.scope" % mort)).mkdir()  # vide
        (self.app / "autre.scope").mkdir()
        time.sleep(0.1)
        scopes = self.scan()
        self.assertEqual(sorted(scopes), sorted([
            "dsh-subprocess-%d-72deff11d18c.scope" % mort,
            "dsh-subprocess-%d-1dfd2fb208eb.scope" % os.getpid()]))
        clos = scopes["dsh-subprocess-%d-72deff11d18c.scope" % mort]
        self.assertFalse(clos["owner_alive"])
        self.assertEqual([(m["pid"], m["turn"], m["unread"]) for m in clos["members"]],
                         [(orphelin.pid, TURN, False)])
        self.assertTrue(scopes["dsh-subprocess-%d-1dfd2fb208eb.scope" % os.getpid()]
                        ["owner_alive"])

    def test_pid_recycle_ne_tient_pas_lieu_de_dsh(self):
        outil = self.spawn(["sleep", "60"])
        time.sleep(1.2)
        recycle = self.spawn(["sleep", "60"])   # né APRÈS l'outil du scope
        self.place("dsh-subprocess-%d-aa.scope" % recycle.pid, outil)
        self.assertFalse(self.scan()["dsh-subprocess-%d-aa.scope" % recycle.pid]
                         ["owner_alive"])

    def test_term_puis_kill(self):
        docile = self.spawn(["sleep", "60"])
        tetu = self.spawn(["bash", "-c", "trap '' TERM; while :; do sleep 0.05; done"])
        self.place("dsh-subprocess-%d-bb.scope" % _dead_pid(), docile, tetu)
        time.sleep(0.3)  # le trap est posé
        scope = next(iter(self.scan().values()))
        left = dsh_scopes.terminate(scope["members"], term_wait=0.5, kill_wait=5)
        self.assertEqual(left, [])
        self.assertEqual(docile.wait(5), -15)
        self.assertEqual(tetu.wait(5), -9)

    def test_pid_repris_n_est_jamais_signale(self):
        """cx1 B2 : un pid repris par un autre processus après le relevé ne
        reçoit aucun signal (pidfd validé par l'heure de démarrage)."""
        repris = self.spawn(["sleep", "60"])
        time.sleep(0.1)
        membre = {"pid": repris.pid, "start": dsh_scopes.process.start_time(repris.pid) - 100,
                  "turn": TURN, "unread": False}
        self.assertEqual(dsh_scopes.terminate([membre], term_wait=0.2, kill_wait=0.2), [])
        self.assertIsNone(repris.poll())

    def test_identite_non_tenue_aucun_signal(self):
        """cx1 B1 : heure illisible ou pidfd indisponible pour UN membre :
        aucun membre du scope n'est signalé, tous sont rendus avec la raison."""
        from ameesh.platform import signals
        a = self.spawn(["sleep", "60"])
        b = self.spawn(["sleep", "60"])
        time.sleep(0.1)
        membres = [{"pid": p.pid, "start": dsh_scopes.process.start_time(p.pid),
                    "turn": TURN, "unread": False} for p in (a, b)]
        sans_heure = [membres[0], dict(membres[1], start=None)]
        left = dsh_scopes.terminate(sans_heure, term_wait=0.2, kill_wait=0.2)
        self.assertEqual([(m["pid"], m["why"]) for m in left],
                         [(b.pid, "identity not held")])
        vrai_open = signals.ProcessHandle.open
        with mock.patch.object(signals.ProcessHandle, "open",
                               side_effect=lambda pid, start: None if pid == b.pid
                               else vrai_open(pid, start)):
            left = dsh_scopes.terminate(membres, term_wait=0.2, kill_wait=0.2)
        self.assertEqual([m["pid"] for m in left], [b.pid])
        self.assertIsNone(a.poll())
        self.assertIsNone(b.poll())

    def test_sans_gestionnaire_rien_n_est_vu(self):
        self.assertIsNone(dsh_scopes.scan(uid=self.uid + 1, root_dir=self.tmp.name))


class RecolteJournalAlerteTest(PgTestCase):
    """Le passage : tué et journalisé, gardé et signalé (une fois), `gone`, alerte."""

    def setUp(self):
        super().setUp()
        for table in ("housekeeping_log", "turn_resources"):
            self.db.execute("DELETE FROM " + table)
        turns = storage.of(self.db).turn_resources
        turns.open_turn(TURN, "mty-nxl-rel-01", HOST, pgid=2846133, label="dsh")
        turns.close_turn(TURN, orphan=False)
        turns.open_turn("vivant", "deepseek1", HOST, pgid=99, label="dsh")
        self.killed: list = []

    def run_pass(self, scopes, now, dry_run=False):
        def terminate(members):
            self.killed.append([m["pid"] for m in members])
            return []
        out = menage.reap_dsh_scopes(self.db, HOST, dry_run=dry_run, now=now,
                                     scan=lambda: scopes, terminate=terminate)
        if not dry_run:
            storage.of(self.db).housekeeping.log(HOST, out["entries"], actor="test")
        return out

    def alerts(self):
        return [a for a in exploitation._housekeeping_alerts([], self.db, readings=[])
                if a["type"] == "dsh_scope"]

    def test_clos_tue_vivant_garde_inconnu_signale(self):
        clos = _scope(unit="dsh-subprocess-1-a.scope")
        vivant = _scope(unit="dsh-subprocess-2-b.scope", turns=("vivant",))
        inconnu = _scope(unit="dsh-subprocess-3-c.scope", turns=(None,))
        # dans le délai de grâce : rien n'est tué
        out = self.run_pass([clos, vivant, inconnu], time.time())
        self.assertEqual(self.killed, [])
        self.assertEqual([(e["path"], e["action"]) for e in out["entries"]],
                         [("dsh-subprocess-3-c.scope", "signaled")])
        # l'essai ne tue rien ni n'écrit
        plus_tard = time.time() + 2 * menage.DSH_SCOPE_GRACE_S
        essai = self.run_pass([clos, vivant, inconnu], plus_tard, dry_run=True)
        self.assertEqual(self.killed, [])
        self.assertEqual({p["path"]: p["action"] for p in essai["plan"]}, {
            "dsh-subprocess-1-a.scope": "suppression",
            "dsh-subprocess-2-b.scope": "attend",
            "dsh-subprocess-3-c.scope": "signalé"})
        out = self.run_pass([clos, vivant, inconnu], plus_tard)
        self.assertEqual(self.killed, [[4000]])
        # le signalement inchangé n'est pas répété à chaque passage
        self.assertEqual([(e["path"], e["action"], e["agent"]) for e in out["entries"]],
                         [("dsh-subprocess-1-a.scope", "killed", "mty-nxl-rel-01")])
        alertes = {a["path"]: a for a in self.alerts()}
        self.assertEqual(sorted(alertes), ["dsh-subprocess-1-a.scope",
                                           "dsh-subprocess-3-c.scope"])
        self.assertIn("killed", alertes["dsh-subprocess-1-a.scope"]["detail"])
        self.assertIn("kept", alertes["dsh-subprocess-3-c.scope"]["detail"])
        # le scope gardé disparu : `gone`, l'alerte s'éteint
        out = self.run_pass([vivant], plus_tard)
        self.assertEqual([(e["path"], e["action"]) for e in out["entries"]],
                         [("dsh-subprocess-3-c.scope", "gone")])
        self.assertEqual([a["path"] for a in self.alerts()], ["dsh-subprocess-1-a.scope"])

    def test_survivant_au_kill_signale(self):
        out = menage.reap_dsh_scopes(
            self.db, HOST, now=time.time() + 2 * menage.DSH_SCOPE_GRACE_S,
            scan=lambda: [_scope()], terminate=lambda members: members)
        self.assertEqual([e["action"] for e in out["entries"]], ["signaled"])
        self.assertIn("not killed", out["entries"][0]["detail"])

    def test_sans_releve_rien(self):
        self.assertEqual(menage.reap_dsh_scopes(self.db, HOST, scan=lambda: None),
                         {"entries": [], "plan": []})


if __name__ == "__main__":
    unittest.main()

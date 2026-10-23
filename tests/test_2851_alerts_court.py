# SPDX-License-Identifier: AGPL-3.0-only
"""#2851 : `ameesh alerts --json` reste sous les dix secondes.

Mesuré sur pc-smichea (paquet 1.14.0) : ~63 s pour 271 alertes. Le relevé
payait, dans cet ordre de gravité :

- une sonde de présence de commit qui réveillait le promisor d'un clone
  partiel : `git rev-parse <absent>^{commit}` lançait un fetch (~10 s vers
  GitHub) avant d'échouer, et le balayage des dossiers du registre pour
  retrouver le dépôt d'un gel en payait plusieurs par passe ;
- quatre lectures `hosts.current` (~1 s chacune) et une histoire disque de 6 h
  à travers ~50 colonnes (~0,6-0,9 s par hôte) ;
- le journal du ménage trié en entier pour ramener 4 000 chemins `tmp` dont
  une poignée d'alertes, plus deux fois le tri des mêmes lignes.

Les témoins ci-dessous bornent la sonde (un remote promisor qui pend : le
commit absent doit rendre la main sans le toucher), prouvent qu'un relevé
`db.batched` n'ouvre la porte des avis qu'une fois (pas à chaque passe à
blanc), et fixent les lectures étroites des hôtes et du journal.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import (db as db_mod, exploitation, lot_class, merge_queue, plan_git,
                    registry, storage, work)

from .support import PgTestCase

#: durée du faux transport : assez pour qu'une sonde qui fetche se voie, assez
#: courte pour qu'une régression ne bloque pas la CI.
HANG_S = 5
#: la borne du témoin : une lecture d'objets locale est en millisecondes.
BOUND_S = 2.0


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True, **kw)


def _repo(base: str, name: str, *, hang_remote: bool = False) -> str:
    """Un dépôt avec un commit, et (optionnellement) un remote promisor dont le
    transport pend : toute lecture qui fetche se paie `HANG_S` secondes."""
    repo = os.path.join(base, name)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    _git(repo, "config", "user.email", "dev1@ameesh.invalid")
    _git(repo, "config", "user.name", "dev1")
    with open(os.path.join(repo, "f"), "w") as fh:
        # le nom entre dans le contenu : deux dépôts n'ont pas le même commit
        fh.write("base %s\n" % name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    if hang_remote:
        _git(repo, "remote", "add", "origin", "hang://x")
        _git(repo, "config", "remote.origin.promisor", "true")
        _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
    return repo


class _HangingTransport:
    """Un helper `git-remote-hang` qui journalise puis dort : il ne doit être
    appelé par aucune sonde de présence."""

    def __init__(self, base: str) -> None:
        self.dir = os.path.join(base, "bin")
        os.makedirs(self.dir)
        self.log = os.path.join(base, "fetch.log")
        helper = os.path.join(self.dir, "git-remote-hang")
        with open(helper, "w") as fh:
            fh.write("#!/bin/sh\necho \"$@\" >> %s\nsleep %d\n" % (self.log, HANG_S))
        os.chmod(helper, 0o755)

    def __enter__(self):
        self._env = mock.patch.dict(
            os.environ, {"PATH": self.dir + os.pathsep + os.environ.get("PATH", "")})
        self._env.start()
        return self

    def __exit__(self, *_exc):
        self._env.stop()
        return False

    def called(self) -> bool:
        try:
            with open(self.log, encoding="utf-8") as fh:
                return bool(fh.read().strip())
        except FileNotFoundError:
            return False


class LocalPresenceProbe(unittest.TestCase):
    """`plan_git.local_commit` lit les objets locaux, sans promisor."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-2851-")
        self.addCleanup(subprocess.run, ["rm", "-rf", self.base])
        self.repo = _repo(self.base, "repo", hang_remote=True)
        self.transport = _HangingTransport(self.base)

    def test_absent_commit_rend_la_main_sans_fetcher(self):
        missing = "0" * 40
        with self.transport:
            started = time.monotonic()
            self.assertIsNone(plan_git.local_commit(self.repo, missing))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, BOUND_S, "la sonde ne doit pas fetcher")
        self.assertFalse(self.transport.called(),
                         "le transport promisor ne doit jamais être réveillé")

    def test_present_commit_se_resout_quand_même(self):
        head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        with self.transport:
            started = time.monotonic()
            self.assertEqual(plan_git.local_commit(self.repo, head[:12]), head)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, BOUND_S)
        self.assertFalse(self.transport.called())


class RepoForFreezeTest(PgTestCase):
    """Le balayage du registre traverse un clone partiel étranger sans fetcher."""

    def test_un_clone_partiel_absent_ne_coute_pas_un_fetch(self):
        base = tempfile.mkdtemp(prefix="ameesh-2851-gel-")
        self.addCleanup(subprocess.run, ["rm", "-rf", base])
        foreign = _repo(base, "foreign", hang_remote=True)
        right = _repo(base, "right")
        sha = _git(right, "rev-parse", "HEAD").stdout.strip()
        transport = _HangingTransport(base)

        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host,
                        cwd=foreign)
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host,
                        cwd=right)
        lot = work.add(self.db, title="un gel à retrouver", assignee="dev1")
        item = work.get(self.db, lot["id"])

        with transport:
            started = time.monotonic()
            found = lot_class.repo_for_freeze(self.db, self.cfg, item, sha)
            elapsed = time.monotonic() - started
        self.assertEqual(found, (os.path.realpath(right), sha))
        self.assertLess(elapsed, BOUND_S, "le clone partiel ne doit pas fetcher")
        self.assertFalse(transport.called())


class BatchedSnapshotTest(PgTestCase):
    """Un relevé `db.batched` n'ouvre la porte des avis que sur la vraie passe."""

    def _seuils(self) -> dict:
        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        seuils["orchestrators_declared"] = ["orch"]
        return seuils

    def test_la_porte_des_avis_n_est_ouverte_qu_une_fois(self):
        for name in ("dev1", "orch"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        lot = work.add(self.db, title="un lot SLO", assignee="dev1")
        work.move(self.db, lot["id"], "build")
        work.move(self.db, lot["id"], "qa")
        self.db.execute(
            "UPDATE work_items SET created_at = now() - interval '2 hours'"
            " WHERE id = %s", (lot["id"],))
        self.db.execute(
            "UPDATE work_item_events SET created_at = now() - interval '90 minutes'"
            " WHERE work_item_id = %s AND state = 'build'", (lot["id"],))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', now() - interval '100 minutes', %s, 'dev1')",
            (lot["id"], "a" * 40))
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', now() - interval '50 minutes', %s, 'cx', 'ok')",
            (lot["id"], "a" * 40))

        now = time.time()
        with mock.patch.object(merge_queue, "review_gate_for_item",
                               return_value={"ok": True}) as gate:
            db_mod.batched(
                self.db,
                lambda db: exploitation.alerts(self.cfg, db, now=now, **self._seuils()))
        self.assertEqual(gate.call_count, 1,
                         "une ouverture (la vraie passe), pas une par passe à blanc")


class NarrowReadsTest(PgTestCase):
    """Les lectures qui dominaient le relevé ne paient plus l'inutile."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("DELETE FROM housekeeping_log")
        self.hosts = storage.of(self.db).hosts

    def record(self, host: str = "pc", **over) -> dict:
        row = {"host": host, "mem_available_bytes": 5 * 1024 ** 3,
               "swap_used_bytes": 0, "load1": 1.5, "cpu_count": 8,
               "disk_free_bytes": 50 * 1024 ** 3, "disk_path": "/tmp",
               "turns_in_progress": 0}
        row.update(over)
        return self.hosts.record(row)

    def test_disk_history_ne_rend_que_les_champs_de_la_tendance(self):
        self.record(volumes=[{"path": "/", "paths": ["/"], "fstype": "ext4",
                              "disk_free_bytes": 40 * 1024 ** 3,
                              "disk_total_bytes": 100 * 1024 ** 3}])
        rows = self.hosts.disk_history("pc", 6 * 3600.0, 300.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]),
                         {"id", "sampled_ts", "volumes", "disk_path", "disk_free_bytes"})
        # #2879 : les champs de tendance seuls suffisent, le reste (fstype,
        # capacité) ne traverse plus le lien à chaque tranche.
        self.assertEqual(rows[0]["volumes"][0],
                         {"path": "/", "paths": ["/"],
                          "disk_free_bytes": 40 * 1024 ** 3})

    def test_current_ne_rend_que_le_dernier_releve(self):
        self.record(mem_available_bytes=1024)
        self.record(mem_available_bytes=2048)
        rows = self.hosts.current(None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mem_available_bytes"], 2048)

    def test_le_journal_du_menage_se_lit_sur_la_fenetre(self):
        hk = storage.of(self.db).housekeeping
        hk.log("pc", [
            {"kind": "orphan", "action": "signaled", "path": "/tmp/ancien",
             "bytes": 1, "agent": "a"},
            {"kind": "orphan", "action": "signaled", "path": "/tmp/recent",
             "bytes": 2, "agent": "a"},
        ], actor="test")
        self.db.execute("UPDATE housekeeping_log SET at = now() - interval '2 days'"
                        " WHERE path = '/tmp/ancien'")
        recent = hk.latest_by_path("pc", "orphan", since_s=86400.0)
        self.assertEqual([row["path"] for row in recent], ["/tmp/recent"])
        self.assertEqual({row["path"] for row in hk.latest_by_path("pc", "orphan")},
                         {"/tmp/ancien", "/tmp/recent"})

    def test_latest_by_path_ne_garde_que_les_chemins_d_unowned(self):
        hk = storage.of(self.db).housekeeping
        hk.log("pc", [
            {"kind": "tmp", "action": "signaled", "path": "/tmp/bruit",
             "bytes": 1},
            {"kind": "tmp", "action": "unowned", "path": "/tmp/a", "bytes": 2},
            {"kind": "tmp", "action": "deleted", "path": "/tmp/b", "bytes": 3},
        ], actor="test")
        rows = hk.latest_by_path("pc", "tmp", only_paths_with=("unowned",))
        self.assertEqual([row["path"] for row in rows], ["/tmp/a"])

    def test_unowned_ancien_reste_signale(self):
        hk = storage.of(self.db).housekeeping
        hk.log("pc", [{"kind": "tmp", "action": "unowned", "path": "/tmp/ancien",
                       "bytes": 2, "agent": "a",
                       "data": {"first_seen": 1000.0, "command": "ls -la"}}],
               actor="test")
        self.db.execute("UPDATE housekeeping_log SET at = now() - interval '30 hours'"
                        " WHERE path = '/tmp/ancien'")
        alerts = [a for a in exploitation._housekeeping_alerts([], self.db)
                  if a["type"] == "tmp_unowned"]
        self.assertEqual([a["path"] for a in alerts], ["/tmp/ancien"])

    def test_toute_action_ulterieure_clot_l_unowned_meme_hors_fenetre(self):
        """#2851 (relecture cx3) : `unowned` puis `deleted`/`evicted`/`held`/
        `gone` — la dernière ligne du chemin fait foi, quel que soit son âge ;
        seule une action postérieure plus récente qu'une ligne `unowned` compte."""
        hk = storage.of(self.db).housekeeping
        paths = ("/tmp/supprime", "/tmp/evince", "/tmp/tenu", "/tmp/parti",
                 "/tmp/reste")
        for path in paths:
            hk.log("pc", [{"kind": "tmp", "action": "unowned", "path": path,
                           "bytes": 2, "agent": "a"}], actor="test")
        self.db.execute("UPDATE housekeeping_log SET at = now() - interval '3 days'"
                        " WHERE path <> '/tmp/reste'")
        for action, path in (("deleted", "/tmp/supprime"),
                             ("evicted", "/tmp/evince"),
                             ("held", "/tmp/tenu"),
                             ("gone", "/tmp/parti")):
            hk.log("pc", [{"kind": "tmp", "action": action, "path": path,
                           "bytes": 1}], actor="test")
        alerts = [a for a in exploitation._housekeeping_alerts([], self.db)
                  if a["type"] == "tmp_unowned"]
        self.assertEqual([a["path"] for a in alerts], ["/tmp/reste"])


class OneHostSnapshotTest(PgTestCase):
    """`_host_alerts` lit les relevés une fois, `_housekeeping_alerts` hérite."""

    def test_le_releve_des_hotes_est_lu_une_fois(self):
        real = storage.of(self.db)
        counts = {"current": 0}

        class Hosts:
            def __init__(self, hosts):
                self._hosts = hosts

            def current(self, host):
                counts["current"] += 1
                return self._hosts.current(host)

            def __getattr__(self, name):
                return getattr(self._hosts, name)

        store = type("Store", (), {})()
        store.hosts = Hosts(real.hosts)
        store.housekeeping = real.housekeeping
        store.turn_resources = real.turn_resources
        with mock.patch.object(exploitation.storage, "of", return_value=store), \
                mock.patch("ameesh.canon.load_configured", return_value=[]):
            exploitation._host_alerts(None, self.db, now=time.time())
        self.assertEqual(counts["current"], 1)


if __name__ == "__main__":
    unittest.main()

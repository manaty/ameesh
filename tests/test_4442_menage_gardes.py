# SPDX-License-Identifier: AGPL-3.0-only
"""#4442 : un worktree gardé (sale ou non poussé) n'est jamais retiré — il
l'est maintenant après `dirty_worktree_after`, son travail archivé d'abord
(`HEAD.bundle` pour les commits absents de la forge, patchs et non suivis
comme #3728), copies étrangères comprises ; la grâce de 24 h se fonde sur la
raison NORMALISÉE (un SHA ou un nombre de fichiers qui bouge ne la repart
plus), et l'alerte `worktree_kept` est agrégée par hôte."""
from __future__ import annotations

import json
import os
import subprocess
import tarfile
import time

from ameesh import exploitation, housekeeping as hk, menage, registry, storage, work

from .support import PgTestCase
from .test_l230_build_artifacts import _write as _write_text
from .test_l73_housekeeping import (_Cfg, _age_tree, _git, _pose_system_tmp, _repo,
                                   _sans_processus)

#: la politique de la fiche Host (deux durées déclarées : l'archivage existe)
AFTER = 6 * 3600.0
RETENTION = 14 * 24 * 3600.0
LATER = 30 * 24 * 3600.0


class GardesArchiveesTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees", "host_resources"):
            self.db.execute("DELETE FROM " + table)
        self.cfgl = _Cfg(self.state)
        self.cfgl.host = self.cfg.host
        os.makedirs(os.path.join(self.tmp, "system-tmp"), exist_ok=True)
        _pose_system_tmp(self, os.path.join(self.tmp, "system-tmp"))
        _sans_processus(self)
        self.policy = menage.Policy(dirty_worktree_after=AFTER,
                                    dirty_archive_retention=RETENTION)
        self.archives = os.path.join(self.state, ".menage", "worktree-archives")
        registry.upsert(self.db, "a1", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a1"))

    def _lot(self, title: str = "lot") -> int:
        return int(work.add(self.db, title=title, app="ameesh")["id"])

    def _merged(self, lot: int) -> None:
        work.move(self.db, lot, "build", actor="t")
        work.move(self.db, lot, "qa", actor="t")
        work.move(self.db, lot, "merged", actor="t")

    def _suivi(self, main: str, name: str, *, lot=None, agent: str = "a1",
               branch: str = "") -> str:
        wt = os.path.join(self.tmp, name)
        _git("worktree", "add", "-q", wt, "-b", branch or name, cwd=main)
        row = storage.of(self.db).housekeeping.register_worktree(
            host="pc", path=wt, repo=menage.common_dir(main), agent=agent,
            lot=str(lot) if lot is not None else None, turn_id="t",
            branch=branch or name, head="")
        storage.of(self.db).housekeeping.set_worktree_status(row["id"], "active", "")
        return wt

    def _pass(self, now: float, *, dry_run: bool = False, policy=None) -> dict:
        return menage.run_pass(self.cfgl, self.db, policy or self.policy, host="pc",
                               actor="t", dry_run=dry_run, now=now)

    @staticmethod
    def _plan(report: dict) -> dict:
        return {p["path"]: p for p in report["plan"] if p.get("kind") == "worktree"}

    def _commit(self, wt: str, name: str) -> None:
        _write_text(os.path.join(wt, name), text="travail de %s\n" % name)
        _git("add", name, cwd=wt)
        _git("commit", "-q", "-m", name, cwd=wt)

    def test_commits_non_pousses_bundle_puis_retrait(self):
        """#4442 : un worktree d'un lot fini gardé pour un commit non poussé
        est archivé (`HEAD.bundle`) puis retiré ; la branche reste et
        l'archive se restaure."""
        main = _repo(self.tmp)
        lot = self._lot("fini")
        work.assign(self.db, lot, "a1")
        branch = "lot/%d-non-pousse" % lot
        wt = self._suivi(main, "lot-%d-non-pousse" % lot, lot=lot, branch=branch)
        self._commit(wt, "rattrape.txt")
        _write_text(os.path.join(wt, "notes", "brouillon.md"), text="non suivi\n")
        self._merged(lot)
        now = time.time() + LATER
        _age_tree(wt, LATER - 3600)

        essai = self._pass(now, dry_run=True)
        self.assertEqual(self._plan(essai)[wt]["action"], "archivage")
        self.assertIn("commits not pushed", self._plan(essai)[wt]["detail"])
        self.assertTrue(os.path.isdir(wt))
        self.assertFalse(os.path.exists(self.archives) and os.listdir(self.archives))

        report = self._pass(now)
        self.assertEqual(self._plan(report)[wt]["action"], "retrait")
        self.assertFalse(os.path.exists(wt))
        archived, = [e for e in report["entries"] if e["action"] == "archived"]
        self.assertGreaterEqual(archived["data"]["commits"], 1)
        folder = archived["data"]["archive"]
        with open(os.path.join(folder, "manifest.json")) as fh:
            manifest = json.load(fh)
        self.assertGreaterEqual(manifest["commits"], 1)
        self.assertEqual(manifest["bundle"], "HEAD.bundle")
        self.assertTrue(os.path.isfile(os.path.join(folder, "HEAD.bundle")))
        with tarfile.open(os.path.join(folder, "untracked.tar.gz")) as tar:
            self.assertEqual([m.name for m in tar.getmembers()], ["notes/brouillon.md"])
        self.assertEqual(storage.of(self.db).housekeeping.worktrees("pc")[0]["status"],
                         "removed")
        # nothing committed is lost: the branch stays, the bundle restores it
        self.assertIn(branch, _git("branch", "--list", branch, cwd=main))
        target = os.path.join(self.tmp, "restaure")
        proc = subprocess.run(["bash", "-c", manifest["restore"].replace("<path>", target)],
                              cwd=main, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_git("show", "--name-only", "--format=", "HEAD", cwd=target),
                         "rattrape.txt\n")
        self.assertTrue(os.path.isfile(os.path.join(target, "notes", "brouillon.md")))

    def test_copie_etrangere_non_poussee_archivee_apres_la_politique(self):
        """#2182/#4442 : la copie étrangère non poussée était gardée sans fin.
        Sans politique, elle reste gardée et signalée ; avec
        `dirty_worktree_after`, elle est archivée puis retirée."""
        main = _repo(self.tmp)
        registry.upsert(self.db, "a2", harness="claude", host="pc",
                        cwd=os.path.join(self.tmp, "a2"))
        lot = self._lot("fini de a2")
        work.assign(self.db, lot, "a2")
        wt = self._suivi(main, "relecture-non-poussee", agent="a1",
                         branch="lot/%d-relecture" % lot)
        self._commit(wt, "travail.txt")
        self._merged(lot)
        now = time.time() + LATER
        _age_tree(wt, LATER - 3600)

        report = self._pass(now, policy=menage.Policy())
        self.assertEqual(self._plan(report)[wt]["action"], "garde")
        self.assertIn("commits not pushed", self._plan(report)[wt]["detail"])
        row = storage.of(self.db).housekeeping.worktrees("pc")[0]
        self.assertEqual(row["status"], "kept")
        self.assertIn("assigned to another agent", row["detail"])
        self.assertTrue(os.path.isdir(wt))

        report = self._pass(now)
        self.assertEqual(self._plan(report)[wt]["action"], "retrait")
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(storage.of(self.db).housekeeping.worktrees("pc")[0]["status"],
                         "removed")
        self.assertTrue(os.listdir(self.archives))

    def test_grace_ne_repart_pas_quand_seul_le_sha_change(self):
        """#4442 : la grâce de 24 h se fonde sur la raison normalisée — un
        commit de plus (SHA différent) ne réécrit pas le statut et ne remet
        pas le compteur à zéro ; 24 h après LA MÊME raison, retrait."""
        main = _repo(self.tmp)
        lot = self._lot("fini")
        work.assign(self.db, lot, "a1")
        branch = "lot/%d-grace" % lot
        wt = self._suivi(main, "lot-%d-grace" % lot, lot=lot, branch=branch)
        self._commit(wt, "travail.txt")
        _git("push", "-q", "origin", branch, cwd=wt)
        self._merged(lot)
        now = time.time() + 2 * 3600

        report = self._pass(now)
        self.assertEqual(self._plan(report)[wt]["action"], "garde")
        row = storage.of(self.db).housekeeping.worktrees("pc")[0]
        self.assertNotIn(_git("rev-parse", "HEAD", cwd=wt).strip()[:12], row["detail"])
        self.assertIn("kept — HEAD not integrated", row["detail"])
        self.db.execute("UPDATE managed_worktrees SET checked_at = now() - interval '1 hour'"
                        " WHERE id = %s", (row["id"],))
        checked = storage.of(self.db).housekeeping.worktrees("pc")[0]["checked_ts"]

        self._commit(wt, "suite.txt")
        _git("push", "-q", "origin", branch, cwd=wt)
        report = self._pass(now)
        self.assertEqual([e for e in report["entries"] if e["action"] == "kept"], [])
        self.assertAlmostEqual(
            storage.of(self.db).housekeeping.worktrees("pc")[0]["checked_ts"],
            checked, places=3)
        self.assertIn("kept — HEAD not integrated", self._plan(report)[wt]["detail"])

        self.db.execute("UPDATE managed_worktrees SET checked_at = now() - interval '2 days'"
                        " WHERE id = %s", (row["id"],))
        report = self._pass(now)
        self.assertEqual(self._plan(report)[wt]["action"], "retrait")
        self.assertFalse(os.path.exists(wt))
        self.assertIn(branch, _git("branch", "--list", branch, cwd=main))

    def test_alerte_worktree_kept_agregee_par_hote(self):
        """#4442 : une alerte par hôte (nombre, raisons, chemins), plus une
        ligne par worktree ; elle dit la politique d'archivage absente."""
        store = storage.of(self.db).housekeeping
        for host, path, detail in (
                ("pc", "/w/a", "lot #1 merged: kept — commits not pushed"),
                ("pc", "/w/b", "lot #2 merged: kept — modified or untracked files"),
                ("pc2", "/w/c", "no lot named: kept — commits not pushed")):
            row = store.register_worktree(host=host, path=path, repo="/r", agent="a1",
                                          lot=None, turn_id="t", branch="", head="")
            store.set_worktree_status(row["id"], "kept", detail)
        alertes = [a for a in exploitation._housekeeping_alerts([], self.db, readings=[])
                   if a["type"] == "worktree_kept"]
        self.assertEqual([a["host"] for a in alertes], ["pc", "pc2"])
        pc = alertes[0]
        self.assertEqual((pc["value"], pc["count"]), (2, 2))
        self.assertIsNone(pc["since"])
        self.assertEqual(sorted(pc["paths"]), ["/w/a", "/w/b"])
        self.assertEqual(pc["reasons"], {"commits not pushed": 1,
                                         "modified or untracked files": 1})
        self.assertIn("no archive policy", pc["detail"])
        self.assertEqual(exploitation.alert_key(pc),
                         ("worktree_kept", None, "pc", None, None, None))

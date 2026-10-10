# SPDX-License-Identifier: AGPL-3.0-only
"""L118 : le courrier qui confie du travail est lié aux lots ; un lot se ferme
quand sa branche est fusionnée, avec ou sans PR ; `idle_capacity` part aussi
à l'orchestrateur."""
from __future__ import annotations

import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import (assignments, exploitation, notify, plan_git, registry, storage,
                    sous_utilisation, work)

from .support import PgTestCase

HEURE = 3600.0


def _git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class _Base(PgTestCase):
    def agent(self, name, **kw):
        registry.upsert(self.db, name, harness="claude", host=kw.pop("host", self.cfg.host),
                        **kw)

    def unread(self, name):
        return self.db.query("SELECT body, work_item_id, kind FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))


class MailLinksLotsTest(_Base):
    """`mail send` : avertissement, `--lot`, `--new-lot`."""

    def send(self, *args, orchestrators="orch"):
        env = self.env(AMEESH_ALERT_ORCHESTRATORS=orchestrators)
        return self.cli("send", *args, env=env)

    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "dev2"):
            self.agent(name)

    def test_orchestrateur_averti_si_le_destinataire_n_a_aucun_lot(self):
        proc = self.send("dev1", "Prends la relecture du module de facturation.", "--from",
                         "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev1 n'a aucun lot ouvert", proc.stderr)
        self.assertEqual(len(self.unread("dev1")), 1)      # le message part quand même
        # un autre expéditeur n'est pas averti ; un destinataire avec un lot non plus
        proc = self.send("dev1", "Une question sur ta revue.", "--from", "dev2")
        self.assertNotIn("aucun lot ouvert", proc.stderr)
        work.add(self.db, title="relecture", assignee="dev1")
        proc = self.send("dev1", "Autre chose à regarder.", "--from", "orch")
        self.assertNotIn("aucun lot ouvert", proc.stderr)

    def test_lot_rattache_assigne_et_branche_citee(self):
        lot = work.add(self.db, title="RT-9 : cercle de confiance")
        proc = self.send("dev1", "À toi : travaille sur agent/dev1-rt-9 puis gèle.", "--from",
                         "orch", "--lot", "RT-9")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("aucun lot ouvert", proc.stderr)
        self.assertIn("lot #%d assigné à dev1" % lot["id"], proc.stdout)
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["branch"]), ("dev1", "agent/dev1-rt-9"))
        self.assertEqual(self.unread("dev1")[0]["work_item_id"], str(lot["id"]))
        # par numéro aussi, et un lot déjà à lui ne bouge pas
        proc = self.send("dev1", "Rappel sur ce lot.", "--from", "orch", "--lot", str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_lot_d_un_autre_agent_jamais_repris(self):
        lot = work.add(self.db, title="implémentation", assignee="dev1")
        proc = self.send("dev2", "Relis le lot de dev1.", "--from", "orch", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("appartient à dev1 : non réassigné", proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_new_lot_cree_et_assigne(self):
        proc = self.send("dev2", "Nouveau chantier, branche agent/dev2-ci.", "--from", "orch",
                         "--new-lot", "CI plus rapide")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = work.list_items(self.db, assignee="dev2")
        self.assertEqual([(r["title"], r["branch"]) for r in rows],
                         [("CI plus rapide", "agent/dev2-ci")])
        self.assertEqual(self.unread("dev2")[0]["work_item_id"], str(rows[0]["id"]))
        self.assertIn("créé et assigné à dev2", proc.stdout)

    def test_garde_d_attribution_rien_n_est_depose(self):
        proc = self.send("inconnu", "Travail pour toi.", "--from", "orch", "--new-lot", "x")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("message non déposé", proc.stderr)
        self.assertEqual(self.unread("inconnu"), [])
        self.assertEqual(work.list_items(self.db), [])

    def test_lot_d_un_agent_ordinaire_reste_une_etiquette(self):
        lot = work.add(self.db, title="libre")
        proc = self.send("dev1", "Je réponds sur ce lot.", "--from", "dev2", "--lot",
                         str(lot["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        # une étiquette libre reste acceptée (inchangé), l'orchestrateur est prévenu
        proc = self.send("dev1", "Sur le sujet libre.", "--from", "orch", "--lot",
                         "SUJET-LIBRE")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ne désigne aucun lot ouvert", proc.stderr)
        self.assertEqual(self.unread("dev1")[-1]["work_item_id"], "SUJET-LIBRE")

    def test_reference_ambigue_refusee(self):
        work.add(self.db, title="DUP : un")
        work.add(self.db, title="DUP : deux")
        with self.assertRaises(assignments.AssignmentError):
            assignments.resolve_lot(self.db, "DUP")
        with mock.patch.dict(os.environ, {"AMEESH_ALERT_ORCHESTRATORS": "orch"}):
            self.assertTrue(assignments.is_orchestrator(self.cfg, self.db, "orch"))
            self.assertFalse(assignments.is_orchestrator(self.cfg, self.db, "dev1"))


class BranchMergeTest(_Base):
    """Un lot se ferme quand sa branche entre dans sa cible, avec ou sans PR."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "develop")
        self.commit("socle")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        self.agent("dev1", cwd=self.repo)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)
        return _git(self.repo, "rev-parse", "HEAD")

    def branch(self, name, *files):
        _git(self.repo, "checkout", "-q", "-b", name, "develop")
        for f in files:
            self.commit(f)
        _git(self.repo, "checkout", "-q", "develop")

    def lot(self, branch):
        return work.add(self.db, title="lot", assignee="dev1", branch=branch)

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_fusion_par_commit_de_fusion(self):
        self.branch("agent/dev1-a", "a1", "a2")
        lot = self.lot("agent/dev1-a")
        report = self.sync()
        self.assertEqual([r["result"] for r in report["results"]], ["open"])
        self.assertTrue(work.get(self.db, lot["id"])["branch_head"])
        self.commit("ailleurs")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge agent/dev1-a", "agent/dev1-a")
        self.assertEqual(self.sync(dry_run=True)["results"][0]["result"], "would-merge")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "ancestor"))
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["state"], "merged")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("branche agent/dev1-a" in n for n in notes), notes)
        self.assertEqual(self.sync()["results"], [])          # lot livré : plus examiné

    def test_branche_sans_travail_n_est_pas_une_fusion(self):
        self.branch("agent/dev1-vide")
        lot = self.lot("agent/dev1-vide")
        self.assertEqual(self.sync()["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_avance_rapide_apres_un_releve(self):
        self.branch("agent/dev1-ff", "f1")
        lot = self.lot("agent/dev1-ff")
        self.sync()                                   # retient la pointe en avance
        _git(self.repo, "merge", "-q", "--ff-only", "agent/dev1-ff")
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "vu-ancestor")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")

    def test_squash_puis_branche_supprimee(self):
        self.branch("agent/dev1-sq", "s1", "s2")
        lot = self.lot("agent/dev1-sq")
        self.sync()
        _git(self.repo, "merge", "-q", "--squash", "agent/dev1-sq")
        _git(self.repo, "commit", "-q", "-m", "squash")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-sq")
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "vu-squash")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")

    def test_commit_de_fusion_qui_cite_la_branche(self):
        self.branch("agent/dev1-m", "m1")
        self.branch("agent/dev1-m-2", "m2")
        lot = self.lot("agent/dev1-m")
        lot2 = self.lot("agent/dev1-m-2")
        self.commit("pendant")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "merge(m): agent/dev1-m", "agent/dev1-m")
        _git(self.repo, "branch", "-q", "-D", "agent/dev1-m")
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(report[lot["id"]]["how"], "message")
        self.assertEqual(report[lot2["id"]]["result"], "open")   # nom entier seulement
        self.assertEqual(work.get(self.db, lot2["id"])["state"], "intake")

    def test_autre_hote_et_dossier_absent(self):
        self.agent("ailleurs", host="autre-hote", cwd=self.repo)
        self.agent("sansdossier", cwd=os.path.join(self.tmp, "absent"))
        self.branch("agent/x", "x1")
        work.add(self.db, title="l", assignee="ailleurs", branch="agent/x")
        work.add(self.db, title="l", assignee="sansdossier", branch="agent/x")
        self.assertEqual([r["result"] for r in self.sync()["results"]], ["no-repo"])

    def test_cli_branche_et_releve(self):
        self.branch("agent/dev1-cli", "c1")
        proc = self.mesh("work", "add", "--title", "par la CLI", "--assignee", "dev1",
                         "--branch", "agent/dev1-cli", "--target", "develop")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lot = work.list_items(self.db)[0]
        self.assertEqual((lot["branch"], lot["branch_target"]), ("agent/dev1-cli", "develop"))
        proc = self.mesh("work", "show", str(lot["id"]))
        self.assertIn("branche  : agent/dev1-cli → develop", proc.stdout)
        other = work.add(self.db, title="autre", assignee="dev1")
        proc = self.mesh("work", "assign", str(other["id"]), "dev1", "--branch", "agent/y")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, other["id"])["branch"], "agent/y")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "m", "agent/dev1-cli")
        proc = self.mesh("work", "sync-branches")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FERMÉ", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")

    def test_noms_refuses(self):
        for bad in ("a..b", "-x", "a b", "x.lock"):
            with self.assertRaises(work.WorkError):
                work.add(self.db, title="t", branch=bad)
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="t", branch_target="develop")
        self.assertEqual(plan_git.cited_branch("agent/a puis agent/b"), None)
        self.assertEqual(plan_git.cited_branch("voir agent/a-b."), "agent/a-b")


class IdleCapacityToOrchestratorTest(_Base):
    """`idle_capacity` part aussi à l'orchestrateur, par courrier `event`."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        for name in ("orch", "dev1", "dev2"):
            self.agent(name)
            self.db.execute(
                "UPDATE agent_registry SET status = 'idle', lease_owner = %s,"
                " lease_expires_at = now() + interval '1 hour', team = 'equipe',"
                " responsible = 'human:proprio',"
                " status_since = now() - interval '2 hours',"
                " last_turn_at = now() - interval '2 hours' WHERE name = %s",
                ("runner-%s@h" % name, name))

    def test_briefs_et_envoi(self):
        lot = work.add(self.db, title="à prendre")
        alert = {"type": "idle_capacity", "agents": ["dev1", "dev2", "orch"],
                 "lots": [lot["id"]], "orchestrators": ["orch"], "threshold": 1800.0}
        listing = storage.of(self.db).operations.listing()
        briefs = sous_utilisation.orchestrator_briefs(alert, listing, {lot["id"]: "à prendre"})
        self.assertEqual(list(briefs), ["orch"])
        self.assertIn("dev1, dev2", briefs["orch"])
        self.assertNotIn("orch,", briefs["orch"])
        self.assertIn("#%d « à prendre »" % lot["id"], briefs["orch"])

        args = exploitation.build_parser().parse_args(["alerts"])
        seuils = exploitation.thresholds(args)
        seuils["orchestrators_declared"] = ("orch",)
        ncfg = notify.parse_config({"default": ["desktop"]})

        class Faux:
            def send(self, channel, message):
                pass

        notifier = notify.Notifier(self.cfg, ncfg, sender=Faux(), log=lambda t: None,
                                   clock=lambda: self.now)
        notifier.run_pass(self.db, thresholds=seuils)
        rows = self.unread("orch")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["kind"], "event")
        self.assertIn("sans lot", rows[0]["body"])
        notifier.run_pass(self.db, thresholds=seuils)       # même alerte : pas de doublon
        self.assertEqual(len(self.unread("orch")), 1)


if __name__ == "__main__":
    unittest.main()

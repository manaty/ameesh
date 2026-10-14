# SPDX-License-Identifier: AGPL-3.0-only
"""Correctif du suivi des lots (constat du 2026-10-10).

Des lots fusionnés en local sur la branche d'intégration, sans PR ni gel ni
branche déclarée, restaient en `intake` : ni `sync-merges`, ni `sync-github`,
ni le relevé des branches ne les fermaient, et `stale_lot` les disait
« sans activité ». Les événements écrits par `ameesh work` sans `--actor`
avaient un acteur vide ; `ameesh projects` montrait comme lot en cours un lot
périmé ; un `promoted` posé par erreur ne se corrigeait pas.

* `ameesh work merged <id> --sha S` : tout état ouvert → `merged` ;
* `ameesh work move <id> merged --correct "raison"` : un `promoted` à tort,
  réservé aux humains et aux orchestrateurs ou agents de conception ;
* relevé des fusions : `ameesh-work: <id>` dans un commit de fusion, `#<id>`
  dans son titre si le dépôt l'active (`git config ameesh.lotRef hash`),
  cible `git config ameesh.target` (introuvable : une erreur) ;
* acteur par défaut : l'identité liée, sinon « inconnu » (averti) ;
* lot en cours : le dernier lot ouvert cité par l'agent, jamais un lot fermé.
"""
from __future__ import annotations

import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import (assignments, exploitation, mail, plan_git, projects, registry,
                    sous_utilisation, work)

from .support import PgTestCase, hold_for_owner

SHA = "0123456789abcdef0123456789abcdef01234567"
#: l'identité de la session qui lance les tests ne doit pas fuir dans ceux qui
#: résolvent l'identité dans le processus même
_IDENTITE = ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
             "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH")


def identite(name: str | None = None):
    """Environnement du processus avec, pour seule identité, `name` (ou aucune)."""
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITE}
    if name:
        env["AGENT_MAIL_NAME"] = name
    return mock.patch.dict(os.environ, env, clear=True)


def _git(repo, *args, env=None):
    base = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    base.update(env or {})
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=base)
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


class _Base(PgTestCase):
    def agent(self, name, **kw):
        registry.upsert(self.db, name, harness="claude", host=kw.pop("host", self.cfg.host),
                        **kw)

    def promoted(self, title="livré"):
        lot = work.add(self.db, title=title, actor="orch")
        for state in ("build", "qa", "merged", "promoted"):
            work.move(self.db, lot["id"], state, actor="orch")
        return lot

    def merged_milestone(self, lot_id):
        return next(m for m in work.milestones(self.db, lot_id) if m["kind"] == "merged")


class MergedCommandTest(_Base):
    """`ameesh work merged <id> --sha S`."""

    def test_tout_etat_ouvert_passe_merged_en_une_fois(self):
        for state in ("intake", "build", "qa", "blocked", "waiting_human"):
            lot = work.add(self.db, title="lot %s" % state, actor="orch")
            if state == "qa":
                work.move(self.db, lot["id"], "build")
            if state == "waiting_human":
                # L233 : waiting_human only behind an open owner request
                hold_for_owner(self.db, lot["id"])
            elif state != "intake":
                work.move(self.db, lot["id"], state)
            done = work.merged(self.db, lot["id"], sha=SHA.upper(), actor="claude1",
                               note="fusion locale sur develop")
            self.assertEqual((done["result"], done["item"]["state"]), ("merged", "merged"),
                             state)
            jalon = self.merged_milestone(lot["id"])
            self.assertEqual((jalon["sha"], jalon["actor"]), (SHA, "claude1"))
            note = work.events(self.db, lot["id"])[0]
            self.assertEqual(note["actor"], "claude1")
            self.assertEqual(note["note"], "merge declared by claude1 (commit %s) — fusion "
                                           "locale sur develop" % SHA[:12])

    def test_idempotent_et_jamais_de_reouverture(self):
        lot = work.add(self.db, title="lot")
        work.merged(self.db, lot["id"], sha=SHA, actor="claude1")
        again = work.merged(self.db, lot["id"], sha="abcdef1", actor="claude2")
        self.assertEqual(again["result"], "already")
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], SHA)   # rien de réécrit
        ferme = work.add(self.db, title="abandonné")
        work.close(self.db, ferme["id"], abandoned=True)
        with self.assertRaises(work.WorkError) as ctx:
            work.merged(self.db, ferme["id"], sha=SHA, actor="claude1")
        self.assertIn("not reopened", str(ctx.exception))
        for bad in ("", "xyz1234", "abc12", "g" * 40):
            with self.assertRaises(work.WorkError):
                work.merged(self.db, lot["id"], sha=bad, actor="claude1")

    def test_cli_acteur_lie_par_defaut(self):
        lot = work.add(self.db, title="lot")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", SHA, "--note", "à la main",
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("lot #%d → merged (commit %s, declared by claude1)"
                      % (lot["id"], SHA[:12]), proc.stdout)
        self.assertEqual(self.merged_milestone(lot["id"])["actor"], "claude1")
        proc = self.mesh("work", "merged", str(lot["id"]), "--sha", SHA,
                         env=self.env(AGENT_MAIL_NAME="claude1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already merged", proc.stdout)
        proc = self.mesh("work", "merged", str(lot["id"]))          # --sha obligatoire
        self.assertEqual(proc.returncode, 2)


class CorrectionTest(_Base):
    """`ameesh work move <id> merged --correct "raison"` : un promoted à tort."""

    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1"):
            self.agent(name)
        self.lot = self.promoted()

    def correct(self, *extra, **env):
        return self.mesh("work", "move", str(self.lot["id"]), "merged", "--correct",
                         "promu par erreur : pas encore en production", *extra,
                         env=self.env(**env))

    def test_sans_correct_la_transition_reste_refusee_avec_la_marche_a_suivre(self):
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, self.lot["id"], "merged")
        self.assertIn("--correct", str(ctx.exception))

    def test_orchestrateur(self):
        proc = self.correct(AGENT_MAIL_NAME="orch", AMEESH_ALERT_ORCHESTRATORS="orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("correction recorded in the log, by orch", proc.stdout)
        item = work.get(self.db, self.lot["id"])
        self.assertEqual(item["state"], "merged")
        self.assertIsNone(item["closed_ts"])
        event = work.events(self.db, self.lot["id"])[0]
        self.assertEqual((event["state"], event["actor"]), ("merged", "orch"))
        self.assertEqual(event["note"], "correction: promoted → merged — promu par erreur : "
                                        "pas encore en production")
        # un seul jalon de fusion : la correction n'en invente pas un second
        self.assertEqual(sum(1 for m in work.milestones(self.db, self.lot["id"])
                             if m["kind"] == "merged"), 1)
        # ensuite, les transitions habituelles
        self.assertEqual(work.move(self.db, self.lot["id"], "blocked")["state"], "blocked")

    def test_humain_qui_se_nomme(self):
        proc = self.correct("--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, self.lot["id"])[0]["actor"], "human:proprio")

    def test_refus(self):
        cas = [
            ((), {}, "say who corrects"),                                  # humain anonyme
            ((), {"AGENT_MAIL_NAME": "dev1"}, "correction refused: dev1"),   # agent ordinaire
            (("--actor", "human:proprio"), {"AGENT_MAIL_NAME": "dev1"},
             "this session belongs to dev1"),                            # au nom d'un autre
            (("--actor", "dev1"), {}, "correction refused: dev1"),
        ]
        for extra, env, attendu in cas:
            proc = self.correct(*extra, **env)
            self.assertEqual(proc.returncode, 1, (extra, env, proc.stdout))
            self.assertIn(attendu, proc.stderr, (extra, env))
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "promoted")
        # seule correction possible : promoted → merged ; et une raison
        autre = work.add(self.db, title="autre")
        with self.assertRaises(work.WorkError) as ctx:
            work.move(self.db, autre["id"], "merged", actor="orch", correct="raison")
        self.assertIn("correction refused: intake → merged", str(ctx.exception))
        with self.assertRaises(work.WorkError):
            work.move(self.db, self.lot["id"], "merged", actor="orch", correct="  ")

    def test_role_de_conception_au_canon(self):
        class Fiche:
            def __init__(self, data):
                self.data = data

        class Agent:
            def __init__(self, title, roles):
                self.title, self.fiche = title, Fiche({"roles": roles})

        class Canon:
            agents = [Agent("dev1", ["conception"])]

        listing = [{"name": "dev1"}]
        self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, listing,
                                                        canons=[Canon()]), [])
        self.assertEqual(sous_utilisation.orchestrators(self.cfg, self.db, listing,
                                                        canons=[Canon()],
                                                        roles=work.CORRECTOR_ROLES), ["dev1"])
        with identite(), mock.patch.object(
                assignments, "is_orchestrator", side_effect=lambda cfg, db, name, roles=None, history=True:
                name == "dev1" and roles == work.CORRECTOR_ROLES and history):
            self.assertEqual(work.corrector(self.cfg, self.db, "dev1"), "dev1")
        with identite("dev1"), mock.patch.object(
                assignments, "is_orchestrator", side_effect=lambda cfg, db, name, roles=None, history=True:
                name == "dev1" and roles == work.CORRECTOR_ROLES and history):
            self.assertEqual(work.corrector(self.cfg, self.db), "dev1")


class ActorTest(_Base):
    """L'acteur des commandes `work` : `--actor`, sinon l'identité liée, sinon « inconnu »."""

    def test_identite_liee_puis_inconnu(self):
        proc = self.mesh("work", "add", "--title", "par l'orchestrateur",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lot = work.list_items(self.db)[0]
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "orch")
        proc = self.mesh("work", "note", str(lot["id"]), "une note sans identité")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unknown actor", proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], work.UNKNOWN_ACTOR)
        proc = self.mesh("work", "move", str(lot["id"]), "build", "--actor", "human:proprio",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "human:proprio")
        proc = self.mesh("work", "milestone", str(lot["id"]), "frozen", "--sha", SHA,
                         env=self.env(AGENT_MAIL_NAME="dev1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.milestones(self.db, lot["id"])[0]["actor"], "dev1")
        proc = self.mesh("work", "close", str(lot["id"]), "--abandoned",
                         env=self.env(AGENT_MAIL_NAME="orch"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warning", proc.stderr)
        self.assertEqual(work.events(self.db, lot["id"])[0]["actor"], "orch")
        self.assertFalse([e for e in work.events(self.db, lot["id"]) if not e["actor"]])

    def test_resolve_actor(self):
        with identite("claude1"):
            self.assertEqual(work.resolve_actor(self.cfg, self.db), ("claude1", None))
            self.assertEqual(work.resolve_actor(self.cfg, self.db, " x "), ("x", None))
        with identite():
            actor, warning = work.resolve_actor(self.cfg, self.db)
            self.assertEqual(actor, "inconnu")
            self.assertIn("--actor", warning)


class MergeSweepTest(_Base):
    """Le relevé des fusions ferme un lot SANS branche que désigne un commit de fusion."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "develop")
        self.agent("claude1", cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()                           # état du processus
            self.addCleanup(state.clear)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)

    def merge(self, branch, message, env=None):
        """Une branche de travail fusionnée en local sur develop, sans PR (`--no-ff`)."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(branch.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", message, branch, env=env)
        return _git(self.repo, "rev-parse", "HEAD")

    def lot(self, title="lot sans branche"):
        return work.add(self.db, title=title, assignee="claude1", actor="orch",
                        force="test: several lots for one agent (#540)")

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_ligne_ameesh_work_dans_le_commit_de_fusion(self):
        lot = self.lot()
        autre = self.lot("autre lot")
        report = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(report[lot["id"]]["result"], "open")
        self.assertIn("ameesh-work: %d" % lot["id"], report[lot["id"]]["detail"])
        sha = self.merge("feature/x", "merge(desktop): DESKTOP-X\n\nameesh-work: %d" % lot["id"])
        self.assertEqual({r["work_item"]: r["result"] for r in self.sync(dry_run=True)["results"]},
                         {lot["id"]: "would-merge", autre["id"]: "open"})
        report = self.sync()
        self.assertEqual(report["merged"], 1)
        item = work.get(self.db, lot["id"])
        self.assertEqual(item["state"], "merged")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual(jalon["sha"], sha)
        self.assertIn("merge sweep, merge commit of develop that carries "
                      "\"ameesh-work: %d\"" % lot["id"], jalon["note"])
        self.assertEqual(work.get(self.db, autre["id"])["state"], "intake")
        self.assertEqual([r["work_item"] for r in self.sync()["results"]], [autre["id"]])

    def test_diese_seulement_si_le_depot_l_active(self):
        lot = self.lot()
        sha = self.merge("feature/y", "Merge #%d COMPUTE-IDLE" % lot["id"])
        self.sync()
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")  # n° de PR ailleurs
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "#id"))
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)
        self.assertIn("cites #%d in its title (ameesh.lotRef hash)" % lot["id"],
                      self.merged_milestone(lot["id"])["note"])

    def test_formes_voisines_et_corps_ignores(self):
        _git(self.repo, "config", plan_git.LOT_REF_GIT_KEY, "hash")
        lot = self.lot()
        n = lot["id"]
        self.merge("feature/a", "Merge PR#%d" % n)
        self.merge("feature/b", "Merge depot#%d et ##%d" % (n, n))
        self.merge("feature/c", "Merge feature/c\n\n* suite de #%d" % n)
        self.sync()
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    # --- L118b : la fusion d'une branche lot/<id>-… ferme le lot -----------

    def test_fusion_github_d_une_branche_lot_ferme_le_lot(self):
        lot = self.lot()
        n = lot["id"]
        sha = self.merge("lot/%d-garde" % n,
                         "Merge pull request #7 from manaty/lot/%d-garde\n\nL1: garde" % n)
        _git(self.repo, "branch", "-q", "-D", "lot/%d-garde" % n)   # supprimée après la PR
        lus = []
        report = self.sync(pr_body=lambda repo, num: lus.append(num) or "Lot #%d." % n)
        self.assertEqual((report["merged"], report["results"][0]["how"]), (1, "lot-branch"))
        self.assertEqual(lus, [7])
        self.assertEqual(self.merged_milestone(n)["sha"], sha)
        self.assertIn("merges the branch lot/%d-…" % n, self.merged_milestone(n)["note"])

    def test_pr_partielle_le_lot_reste_ouvert_jusqu_a_la_derniere(self):
        lot = self.lot("parapluie")
        n = lot["id"]
        corps = {7: "Part one.\n\nameesh-work-partial: %d" % n, 8: "The rest."}
        self.merge("lot/%d-un" % n, "Merge pull request #7 from manaty/lot/%d-un" % n)
        report = self.sync(pr_body=lambda repo, num: corps[num])
        self.assertEqual(report["results"][0]["how"], "partial")
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        sha = self.merge("lot/%d-deux" % n, "Merge pull request #8 from manaty/lot/%d-deux" % n)
        report = self.sync(pr_body=lambda repo, num: corps[num])
        self.assertEqual(report["merged"], 1)
        self.assertEqual(self.merged_milestone(n)["sha"], sha)

    def test_corps_illisible_aucune_fermeture(self):
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-x" % n, "Merge pull request #9 from manaty/lot/%d-x" % n)

        def illisible(repo, num):
            raise plan_git.GitError("gh: not logged in")

        report = self.sync(pr_body=illisible)
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertIn("unreadable", report["results"][0]["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_fusion_locale_d_une_branche_lot(self):
        lot = self.lot()
        n = lot["id"]

        def jamais(repo, num):
            raise AssertionError("no PR to read")

        self.merge("lot/%d-local" % n, "Merge branch 'lot/%d-local' into develop" % n)
        self.assertEqual(self.sync(pr_body=jamais)["merged"], 1)

    def test_branche_implicite_en_avance_rapide(self):
        lot = self.lot()
        n = lot["id"]
        _git(self.repo, "checkout", "-q", "-b", "lot/%d-ff" % n, "develop")
        self.commit("ff")
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()                       # la pointe en avance est retenue
        self.assertEqual(report["results"][0]["branch"], "lot/%d-ff" % n)
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        _git(self.repo, "merge", "-q", "--ff-only", "lot/%d-ff" % n)
        self.assertEqual(work.get(self.db, n)["branch"], "lot/%d-ff" % n)
        self.assertEqual(self.sync()["merged"], 1)

    def test_formes_lot_branch(self):
        cite = plan_git.cited_lots
        self.assertEqual(cite("Merge pull request #3 from o/lot/42-x"), {42: "lot-branch"})
        self.assertEqual(cite("Merge pull request #3 from o/lot/42"), {42: "lot-branch"})
        self.assertEqual(cite("Merge branch 'lot/42-x'"), {42: "lot-branch"})
        self.assertEqual(cite("Merge remote-tracking branch 'origin/main' into lot/42-x"), {})
        self.assertEqual(cite("Merge pull request #3 from o/lot/42x"), {})
        self.assertEqual(cite("Merge pull request #3 from o/agent/lot/42-x"), {})
        self.assertEqual(cite("Merge branch 'lot/42-x'\n\nameesh-work-partial: 42"), {})
        self.assertEqual(plan_git.merge_pr_number("Merge pull request #3 from o/lot/42-x"), 3)
        self.assertIsNone(plan_git.merge_pr_number("Merge branch 'lot/42-x'"))

    # --- #312 r1 --------------------------------------------------------

    def test_r1_branche_enregistree_seule_compte(self):
        # P1 : le lot porte agent/courante, non fusionnée ; la fusion d'une
        # autre lot/<id>-old ne le ferme pas
        lot = self.lot()
        n = lot["id"]
        work.set_branch(self.db, n, "agent/courante", actor="orch", source="test")
        _git(self.repo, "checkout", "-q", "-b", "agent/courante", "develop")
        self.commit("courante")
        _git(self.repo, "checkout", "-q", "develop")
        self.merge("lot/%d-old" % n, "Merge branch 'lot/%d-old'" % n)
        report = self.sync(pr_body=lambda repo, num: "")
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        # la branche enregistrée elle-même, fusionnée par son titre : fermé
        lot2 = self.lot("deux")
        work.set_branch(self.db, lot2["id"], "origin/lot/%d-bon" % lot2["id"], actor="orch",
                        source="test")
        self.merge("lot/%d-bon" % lot2["id"],
                   "Merge pull request #4 from manaty/lot/%d-bon" % lot2["id"])
        self.sync(pr_body=lambda repo, num: "")
        self.assertEqual(work.get(self.db, lot2["id"])["state"], "merged")

    def test_r1_fusion_locale_partielle_retient_la_branche_implicite(self):
        # P2 : `ameesh-work-partial` dans le message d'une fusion locale
        # retient aussi la preuve par la branche implicite
        lot = self.lot("parapluie")
        n = lot["id"]
        self.merge("lot/%d-un" % n, "Merge branch 'lot/%d-un'\n\nameesh-work-partial: %d"
                   % (n, n))
        report = self.sync()
        self.assertEqual(report["results"][0]["how"], "partial")
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_r1_corps_illisible_puis_pr_complete_lisible(self):
        # P2 : le corps toujours illisible de #7 ne cache pas #8, complète
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-a" % n, "Merge pull request #7 from manaty/lot/%d-a" % n)
        sha = self.merge("lot/%d-b" % n, "Merge pull request #8 from manaty/lot/%d-b" % n)
        lus = []

        def lire(repo, num):
            lus.append(num)
            if num == 7:
                raise plan_git.GitError("gh: HTTP 404")
            return "The rest."

        report = self.sync(pr_body=lire)          # un échec de gh par relevé
        self.assertIn("unreadable", report["results"][0]["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")
        self.assertEqual(self.sync(pr_body=lire)["merged"], 1)   # #7 recule : #8 lu
        self.assertEqual(lus, [7, 8])
        self.assertEqual(self.merged_milestone(n)["sha"], sha)

    def test_r1_recul_et_plafond_des_lectures_de_gh(self):
        # P2 : au plus un échec de gh par dépôt et par relevé ; une PR en échec
        # recule (délai doublé à chaque échec) ; au plus MAX_PR_BODY_READS
        # lectures par relevé
        lots = [self.lot("l%d" % i) for i in range(3)]
        for i, lot in enumerate(lots):
            self.merge("lot/%d-x" % lot["id"],
                       "Merge pull request #%d from manaty/lot/%d-x" % (10 + i, lot["id"]))
        appels = []

        def en_panne(repo, num):
            appels.append(num)
            raise plan_git.GitError("gh: timeout")

        for _ in range(4):
            self.sync(pr_body=en_panne)
        self.assertEqual(appels, [10, 11, 12])     # rien pendant les reculs
        key = (self.repo, 10)
        self.assertEqual(plan_git._pr_body_backoff[key][1], plan_git.PR_BODY_BACKOFF_S)
        plan_git._pr_body_backoff[key] = (0.0, plan_git._pr_body_backoff[key][1])
        self.sync(pr_body=en_panne)
        self.assertEqual(appels, [10, 11, 12, 10])
        self.assertEqual(plan_git._pr_body_backoff[key][1], 2 * plan_git.PR_BODY_BACKOFF_S)
        # plafond : une lecture par relevé ; une lecture réussie oublie le recul
        plan_git._pr_body_backoff.clear()
        lus = []
        with mock.patch.object(plan_git, "MAX_PR_BODY_READS", 1):
            report = self.sync(pr_body=lambda repo, num: lus.append(num) or "")
        self.assertEqual((len(lus), report["merged"]), (1, 1))
        self.assertTrue(any("bodies read in this sweep" in r["detail"]
                            for r in report["results"]))

    def test_r2_corps_lus_gardes_pas_de_famine(self):
        # P2 : un corps déjà lu ne reprend pas de lecture : le lot suivant
        # est lu au relevé suivant, même avec un plafond d'une lecture
        a, b = self.lot("parapluie"), self.lot("complet")
        self.merge("lot/%d-x" % a["id"], "Merge pull request #30 from manaty/lot/%d-x" % a["id"])
        self.merge("lot/%d-y" % b["id"], "Merge pull request #31 from manaty/lot/%d-y" % b["id"])
        corps = {30: "ameesh-work-partial: %d" % a["id"], 31: "Done."}
        lus = []

        def lire(repo, num):
            lus.append(num)
            return corps[num]

        with mock.patch.object(plan_git, "MAX_PR_BODY_READS", 1):
            self.sync(pr_body=lire)
            self.sync(pr_body=lire)
            self.sync(pr_body=lire)
        self.assertEqual(lus, [30, 31])
        self.assertEqual(work.get(self.db, b["id"])["state"], "merged")
        self.assertEqual(work.get(self.db, a["id"])["state"], "intake")

    def test_r2_seule_la_derniere_fusion_partielle_compte(self):
        # P2 : fusion locale partielle, puis la suite de la même branche
        # entre dans la cible en avance rapide : le lot est fermé
        lot = self.lot("parapluie")
        n = lot["id"]
        branch = "lot/%d-un" % n
        self.merge(branch, "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n))
        self.assertEqual(self.sync()["results"][0]["how"], "partial")
        _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "merge", "-q", "--ff-only", "develop")
        self.commit("la-suite")
        _git(self.repo, "checkout", "-q", "develop")
        self.assertEqual(self.sync()["results"][0]["how"], "partial")   # vue en avance
        _git(self.repo, "merge", "-q", "--ff-only", branch)
        self.assertEqual(self.sync()["merged"], 1)

    def pointe_puis_fusion_partielle(self, *, antidate=None):
        """S vu en avance, la branche ramenée à P (poussée forcée) et fusionnée
        partiellement (M, sa date de committer `antidate`), puis S fusionné
        dans la cible par une autre branche, et la branche du lot avancée
        jusqu'à la cible. Le numéro du lot."""
        n = self.lot("parapluie")["id"]
        self.db.execute("UPDATE work_items SET created_at = now() - interval '1 hour'"
                        " WHERE id = %s", (n,))
        branch = "lot/%d-un" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("p")
        p = _git(self.repo, "rev-parse", "HEAD")
        self.commit("s")
        _git(self.repo, "checkout", "-q", "develop")
        self.sync()                                               # S vu en avance
        s = work.get(self.db, n)["branch_head"]
        _git(self.repo, "branch", "-q", "-f", "autre", s)
        _git(self.repo, "branch", "-q", "-f", branch, p)          # poussée forcée à P
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n), branch,
             env={"GIT_COMMITTER_DATE": antidate} if antidate else None)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'autre'", "autre")
        _git(self.repo, "branch", "-q", "-f", branch, "develop")  # avance rapide
        return n

    def test_l118d_r1_pointe_vue_avant_la_fusion_partielle(self):
        # #346 r1 P1 : la pointe S, vue avant la fusion partielle, ne la lève pas
        n = self.pointe_puis_fusion_partielle()
        for _ in range(2):
            report = self.sync()
            self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_l118d_r2_fusion_partielle_antidatee(self):
        # #346 r2 P1 : la date de committer de M, antidatée de 10 minutes
        # (au-delà de CLOCK_SLACK_S), ne compte pas : seul l'instant où le
        # relevé a vu M, horloge de la base, compte
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
        n = self.pointe_puis_fusion_partielle(antidate=stamp)
        for _ in range(2):                   # M vue, puis relevé suivant
            report = self.sync()
            self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        row = work.get(self.db, n)
        self.assertLess(row["branch_head_ts"], row["partial_seen_ts"])

    def test_l118d_r2_pointe_inchangee_relevee_apres_la_fusion(self):
        # #346 r2 (suite) : S vu en avance, P seul fusionné partiellement, la
        # branche toujours à S : S est relevée de nouveau après M, puis entre
        # dans la cible en avance rapide : le lot est fermé
        n = self.lot("parapluie")["id"]
        branch = "lot/%d-un" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("p")
        p = _git(self.repo, "rev-parse", "HEAD")
        self.commit("s")
        _git(self.repo, "checkout", "-q", "develop")
        self.sync()                                               # S vu en avance
        _git(self.repo, "merge", "-q", "--no-ff", "-m",
             "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n), p)
        self.assertEqual(self.sync()["results"][0]["how"], "partial")   # S relevée après M
        row = work.get(self.db, n)
        self.assertGreater(row["branch_head_ts"], row["partial_seen_ts"])
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'autre'", branch)
        self.assertEqual(self.sync()["merged"], 1)

    def test_403_dry_run_deuxieme_fusion_partielle(self):
        # #403 : M1 partielle relevée, S vu en avance après M1, puis une
        # deuxième fusion partielle M2 (de P seul) et S entré dans la cible
        # par une autre branche : le dry-run juge comme le vrai relevé, M2
        # pas encore relevée ne se compare pas à l'instant de M1
        n = self.lot("parapluie")["id"]
        self.db.execute("UPDATE work_items SET created_at = now() - interval '1 hour'"
                        " WHERE id = %s", (n,))
        branch = "lot/%d-un" % n
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit("p0")
        _git(self.repo, "checkout", "-q", "develop")
        partielle = "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n)
        _git(self.repo, "merge", "-q", "--no-ff", "-m", partielle, branch)
        self.assertEqual(self.sync()["results"][0]["how"], "partial")   # M1 relevée
        m1 = work.get(self.db, n)["partial_merge"]
        _git(self.repo, "checkout", "-q", branch)
        self.commit("p")
        p = _git(self.repo, "rev-parse", "HEAD")
        self.commit("s")
        _git(self.repo, "checkout", "-q", "develop")
        self.sync()                                               # S vu en avance après M1
        s = work.get(self.db, n)["branch_head"]
        _git(self.repo, "branch", "-q", "-f", "autre", s)
        _git(self.repo, "branch", "-q", "-f", branch, p)          # poussée forcée à P
        _git(self.repo, "merge", "-q", "--no-ff", "-m", partielle, branch)   # M2
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'autre'", "autre")
        _git(self.repo, "branch", "-q", "-f", branch, "develop")  # avance rapide
        report = self.sync(dry_run=True)
        self.assertEqual((report["results"][0]["result"], report["results"][0]["how"]),
                         ("open", "partial"))
        self.assertEqual(work.get(self.db, n)["partial_merge"], m1)   # rien d'écrit
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_l118d_r1_erreur_git_garde_le_lot_ouvert(self):
        # #346 r1 P2 : une ascendance illisible (code 128) n'est pas une preuve
        merge = {"sha": "m" * 40, "ts": 0.0, "parents": ["a" * 40, "b" * 40]}
        codes = iter([128, 128, 0])
        with mock.patch.object(plan_git, "_git", side_effect=lambda *a: subprocess.
                               CompletedProcess(a, next(codes))):
            self.assertFalse(plan_git._moved_past(self.repo, "s" * 40, 10_000.0, "s" * 40,
                                                  merge, 0.0))

    def test_l118d_avance_jusqu_a_la_cible_sans_travail_nouveau(self):
        # #312 r3 : après une fusion partielle, la branche est avancée jusqu'à
        # develop sans travail nouveau (et develop a avancé entre-temps) : le
        # lot reste ouvert
        lot = self.lot("parapluie")
        n = lot["id"]
        branch = "lot/%d-un" % n
        self.merge(branch, "Merge branch '%s'\n\nameesh-work-partial: %d" % (branch, n))
        self.assertEqual(self.sync()["results"][0]["how"], "partial")
        _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "merge", "-q", "--ff-only", "develop")      # la fusion elle-même
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.commit("travail-d-un-autre")                         # develop avance
        _git(self.repo, "checkout", "-q", branch)
        _git(self.repo, "merge", "-q", "--ff-only", "develop")
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()
        self.assertEqual((report["merged"], report["results"][0]["how"]), (0, "partial"))
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_cited_lots(self):
        cas = {
            "merge(desktop): DESKTOP-MAC-UNIVERSAL (#91) …": {91: "#id"},
            "Merge #93 COMPUTE-IDLE": {93: "#id"},
            "Merge pull request #45 from org/branche": {45: "#id"},
            "Merge #7.": {7: "#id"},
            "Merge PR#9, depot#9, ##9, #9a, &#93;": {},
            "Merge x\n\nameesh-work: #12\n  ameesh-work: 13": {12: "ameesh-work",
                                                               13: "ameesh-work"},
            "Merge x\n\nvoir ameesh-work: 14 plus tard": {},
        }
        for message, attendu in cas.items():
            self.assertEqual(plan_git.cited_lots(message, hash_refs=True), attendu, message)
        self.assertEqual(plan_git.cited_lots("Merge #93 COMPUTE-IDLE"), {})
        self.assertEqual(plan_git.cited_lots("Merge x\n\nameesh-work: 5"), {5: "ameesh-work"})

    def test_fusion_anterieure_au_lot_ignoree(self):
        lot = self.lot()
        self.merge("feature/old", "Merge\n\nameesh-work: %d" % lot["id"],
                   env={"GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"})
        self.sync()
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_cible_configuree_introuvable(self):
        lot = self.lot()
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "origin/develop")
        entry = self.sync()["results"][0]
        self.assertEqual((entry["work_item"], entry["result"]), (lot["id"], "error"))
        self.assertIn("ameesh.target = origin/develop not found", entry["detail"])

    def test_stale_lot_dit_l_action_puis_se_tait_apres_le_releve(self):
        lot = self.lot()
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours'",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"):
            self.db.execute(sql)
        stale = [a for a in exploitation.alerts(self.cfg, self.db) if a["type"] == "stale_lot"]
        self.assertEqual([a["lot"] for a in stale], [lot["id"]])
        self.assertIn("merged? close it: ameesh work merged %d --sha <merge commit>; "
                      "otherwise nudge claude1" % lot["id"], stale[0]["detail"])
        self.assertEqual(stale[0]["action"], exploitation.stale_action(stale[0] | {
            "id": lot["id"], "assignee": "claude1"}))
        self.merge("feature/z", "Merge #%d\n\nameesh-work: %d" % (lot["id"], lot["id"]))
        self.assertEqual(self.sync()["merged"], 1)
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db)
                          if a["type"] == "stale_lot"])

    def test_cli_sync_branches(self):
        lot = self.lot()
        self.merge("feature/cli", "Merge\n\nameesh-work: %d" % lot["id"])
        proc = self.mesh("work", "sync-branches")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("(no branch)", proc.stdout)
        self.assertIn("CLOSED", proc.stdout)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")


class RealTargetTest(_Base):
    """#746 : la cible RÉELLE du dépôt (`develop`) est suivie quand ni le lot
    ni le dépôt n'en déclarent, et qu'`origin/HEAD` (figé au clone) dit main.

    Liste blanche fermée : `main`, `master`, `develop`, `release/*`, sur le
    remote `origin` seul. Preuve : une fusion citante (trailer, `#<id>` si le
    dépôt l'active, ou nom entier de la branche), jamais le seul voisinage ;
    un lot à branche enregistrée exige que la fusion ait porté SA branche
    (deuxième parent = sa pointe). Une cible déclarée ne ferme jamais
    ailleurs : le résidu est signalé, sans fermeture."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot746")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        _git(self.repo, "checkout", "-q", "-b", "develop")
        # ni `ameesh.target` ni `origin/HEAD` : la cible par défaut est main
        _git(self.repo, "update-ref", "refs/remotes/origin/main",
             _git(self.repo, "rev-parse", "HEAD"))
        self.agent("claude1", cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()                           # état du processus
            self.addCleanup(state.clear)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        _git(self.repo, "add", name)
        _git(self.repo, "commit", "-q", "-m", "ajoute %s" % name)

    def merge(self, branch, message, env=None):
        """Une branche fusionnée en `--no-ff` sur develop (la cible réelle)."""
        _git(self.repo, "checkout", "-q", "-b", branch, "develop")
        self.commit(branch.replace("/", "-"))
        _git(self.repo, "checkout", "-q", "develop")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", message, branch, env=env)
        return _git(self.repo, "rev-parse", "HEAD")

    def publish(self, ref, sha=None):
        _git(self.repo, "update-ref", "refs/remotes/origin/" + ref,
             sha or _git(self.repo, "rev-parse", "HEAD"))

    def lot(self, title="lot sans branche"):
        # #540 : le témoin #990 crée deux lots pour claude1 ; le second doit
        # donc forcer la file d'équipe (même motif que MergeSweepTest.lot).
        return work.add(self.db, title=title, assignee="claude1", actor="orch",
                        force="test: several lots for one agent (#540)")

    def sync(self, **kw):
        return plan_git.sync_branches(self.db, host=self.cfg.host, **kw)

    def test_746_liste_blanche_et_ordre_des_candidats(self):
        self.publish("develop")
        self.publish("lot/12-travail")
        self.publish("review/12-r1")
        self.publish("release/1.0")
        self.assertEqual(plan_git.target_candidates(self.repo, "origin/main"),
                         ["origin/develop", "origin/release/1.0"])

    def test_746_citation_sur_develop_ferme_le_lot(self):
        lot = self.lot()
        sha = self.merge("agent/libre", "merge(desktop): X\n\nameesh-work: %d" % lot["id"])
        self.publish("develop")
        report = self.sync()
        entry = report["results"][0]
        self.assertEqual(entry["result"], "merged")
        self.assertEqual(entry["target"], "origin/develop")
        self.assertEqual(entry["off_target"], "origin/main")
        self.assertIn("ameesh.target", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "merged")
        jalon = self.merged_milestone(lot["id"])
        self.assertEqual(jalon["sha"], sha)
        self.assertIn("origin/develop", jalon["note"])
        self.assertIn("declare `git config ameesh.target", jalon["note"])

    def test_746_citation_sur_une_branche_de_travail_ne_ferme_pas(self):
        lot = self.lot()
        base = _git(self.repo, "rev-parse", "develop")
        sha = self.merge("lot/autre-lot", "Merge branch 'lot/autre-lot' into develop\n\n"
                         "ameesh-work: %d" % lot["id"])
        self.publish("develop", base)          # develop n'a pas la fusion
        self.publish("lot/autre-lot", sha)     # la branche de travail, elle, l'a
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_746_cible_declaree_citation_develop_note_sans_fermeture(self):
        lot = self.lot()
        _git(self.repo, "config", plan_git.TARGET_GIT_KEY, "origin/main")
        sha = self.merge("agent/libre", "merge(desktop): X\n\nameesh-work: %d" % lot["id"])
        self.publish("develop")
        entry = self.sync()["results"][0]
        self.assertEqual(entry["result"], "off-target")
        self.assertIn("origin/develop", entry["detail"])
        self.assertIn("declared target origin/main", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any("origin/develop" in n and sha[:12] in n for n in notes), notes)
        # le relevé suivant ne répète pas le signalement
        self.sync()
        self.assertEqual([e["note"] for e in work.events(self.db, lot["id"])], notes)

    def test_746_fusion_partielle_sur_develop_ne_ferme_pas(self):
        lot = self.lot()
        n = lot["id"]
        self.merge("lot/%d-un" % n, "Merge pull request #7 from manaty/lot/%d-un" % n)
        self.publish("develop")
        report = self.sync(pr_body=lambda repo, num: "part one\n\nameesh-work-partial: %d" % n)
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertIn("partial", report["results"][0]["detail"])
        self.assertEqual(work.get(self.db, n)["state"], "intake")

    def test_746_branche_enregistree_sans_deuxieme_parent_ne_ferme_pas(self):
        lot = work.add(self.db, title="lot à branche", assignee="claude1", actor="orch",
                       branch="agent/x-746")
        self.merge("agent/x-746", "Merge branch 'agent/x-746' into develop")
        self.publish("develop")
        _git(self.repo, "checkout", "-q", "agent/x-746")
        self.commit("suite")                    # la pointe n'est plus le second parent
        _git(self.repo, "checkout", "-q", "develop")
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "open")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_746_branche_enregistree_avec_deuxieme_parent_ferme(self):
        lot = work.add(self.db, title="lot à branche", assignee="claude1", actor="orch",
                       branch="agent/x-746")
        sha = self.merge("agent/x-746", "Merge branch 'agent/x-746' into develop")
        self.publish("develop")
        report = self.sync()
        self.assertEqual(report["results"][0]["result"], "merged")
        self.assertEqual(self.merged_milestone(lot["id"])["sha"], sha)

    def test_746_ref_illisible_signalee_sans_fermeture(self):
        lot = self.lot()
        self.publish("develop")
        vrai = plan_git.merge_commits

        def casse(repo, target, since_ts=None, **kw):
            if plan_git.short_branch(target) == "develop":
                raise plan_git.MergeProbeError("cannot read origin/develop")
            return vrai(repo, target, since_ts, **kw)

        with mock.patch.object(plan_git, "merge_commits", side_effect=casse):
            entry = self.sync()["results"][0]
            notes = [e["note"] for e in work.events(self.db, lot["id"])]
            self.sync()                    # le résidu ne se répète pas
            self.assertEqual([e["note"] for e in work.events(self.db, lot["id"])], notes)
        self.assertEqual(entry["result"], "off-target")
        self.assertIn("unreadable integration ref", entry["detail"])
        self.assertIn("origin/develop", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        self.assertTrue(any("origin/develop" in (n or "") for n in notes))

    def test_990_cache_des_candidats_par_cible_et_non_par_depot(self):
        """#990 (P2 de la relecture de #746) : la liste des candidats dépend de
        la cible primaire, propre au lot. Un lot à cible déclarée examiné en
        premier ne doit pas figer la liste pour les lots suivants, sans cible,
        du même dépôt : ils resteraient ouverts alors qu'une fusion citante les
        livre sur `origin/develop`."""
        premier = work.add(self.db, title="cible déclarée en premier",
                           assignee="claude1", actor="orch", branch="agent/a-990",
                           branch_target="origin/develop")
        suivant = self.lot("sans cible, livré sur develop")
        self.merge("agent/libre",
                   "merge(desktop): X\n\nameesh-work: %d" % suivant["id"])
        self.publish("develop")
        by_id = {r["work_item"]: r for r in self.sync()["results"]}
        self.assertEqual(by_id[premier["id"]]["result"], "open")
        self.assertEqual(by_id[suivant["id"]]["result"], "merged",
                         "le lot suivant, sans cible, est livré sur develop : "
                         "la liste des candidats ne doit pas être figée par le "
                         "lot précédent, qui déclarait origin/develop")
        self.assertEqual(work.get(self.db, suivant["id"])["state"], "merged")


class CurrentLotTest(_Base):
    """`ameesh projects` : le lot en cours est le dernier lot OUVERT cité."""

    def setUp(self):
        super().setUp()
        for name in ("claude1", "claude2", "orch"):
            self.agent(name, chantier="nx")
        self.vieux = work.add(self.db, title="COMPUTE-IDLE : veille", assignee="claude1")
        self.relu = work.add(self.db, title="REV-118 : relecture", assignee="claude2")

    def lot_of(self, name):
        view = projects.snapshot(self.db, paid_harnesses=())
        return next(a for p in view["projects"] for a in p["agents"] if a["name"] == name)["lot"]

    def test_lot_cite_par_l_agent_puis_jamais_un_lot_ferme(self):
        self.db.execute("UPDATE agent_registry SET session_work_item = %s WHERE name = 'claude1'",
                        (str(self.vieux["id"]),))
        self.assertEqual(self.lot_of("claude1")["id"], self.vieux["id"])
        # claude1 relit le lot de claude2 et le dit par une étiquette : le
        # numéro est enregistré (tout expéditeur), le lot en cours suit
        proc = self.cli("send", "orch", "Relecture en cours.", "--from", "claude1",
                        "--lot", "REV-118")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = self.db.query("SELECT work_item_id FROM agent_mailbox WHERE sender = 'claude1'")
        self.assertEqual(row[0]["work_item_id"], str(self.relu["id"]))
        self.assertEqual(work.get(self.db, self.relu["id"])["assignee"], "claude2")  # pas volé
        self.assertEqual(self.lot_of("claude1"), {"id": self.relu["id"],
                                                  "title": "REV-118 : relecture",
                                                  "state": "intake", "source": "mail"})
        # le lot cité est fusionné : le lot de session (ouvert) reprend la main
        work.merged(self.db, self.relu["id"], sha=SHA, actor="claude2")
        self.assertEqual(self.lot_of("claude1")["source"], "session")
        # le lot de session est fusionné à son tour : jamais affiché
        work.merged(self.db, self.vieux["id"], sha=SHA, actor="claude1")
        self.assertIsNone(self.lot_of("claude1"))

    def test_etiquettes_resolues_dans_la_vue(self):
        # une étiquette restée telle quelle dans le courrier (message ancien)
        mail.send(self.db, "claude1", "orch", "Sur la veille.", work_item_id="compute-idle")
        lot = self.lot_of("claude1")
        self.assertEqual((lot["id"], lot["source"]), (self.vieux["id"], "mail"))
        # une étiquette ambiguë ne désigne rien
        work.add(self.db, title="DUP : un")
        work.add(self.db, title="DUP : deux")
        mail.send(self.db, "claude2", "orch", "Doublon.", work_item_id="DUP")
        lot = self.lot_of("claude2")
        self.assertEqual((lot["id"], lot["source"]), (self.relu["id"], "assigned"))
        # lot de session donné par une étiquette
        self.db.execute("UPDATE agent_registry SET session_work_item = 'REV-118'"
                        " WHERE name = 'orch'")
        lot = self.lot_of("orch")
        self.assertEqual((lot["id"], lot["source"]), (self.relu["id"], "session"))

    def test_ameesh_list_sans_lot_de_session_ferme(self):
        self.db.execute("UPDATE agent_registry SET session_work_item = %s WHERE name = 'claude1'",
                        (str(self.vieux["id"]),))
        autre = work.add(self.db, title="autre", assignee="claude1",
                         force="test: second lot of claude1 (#540)")
        work.merged(self.db, self.vieux["id"], sha=SHA, actor="claude1")
        rows = exploitation.annotate(self.cfg, self.db, [{"name": "claude1"}])
        self.assertEqual((rows[0]["lot"]["id"], rows[0]["lot"]["source"]),
                         (str(autre["id"]), "assigned"))


if __name__ == "__main__":
    unittest.main()

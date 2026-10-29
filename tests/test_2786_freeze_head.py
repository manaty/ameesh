# SPDX-License-Identifier: AGPL-3.0-only
"""#2786 — un gel nomme une tête immuable, jamais le `HEAD` d'un clone.

Constat du 19/10 23:34 (#2712, Nexlink) : le premier gel a écrit la chaîne
« HEAD » ; la désignation au gel a lu « no changed path », puis le balayage des
fusions a résolu ce `HEAD` dans SON clone et passé le lot en `merged` alors que
la PR nexlink#1809 (182b00d1) était ouverte.

Correctif : à l'écriture, un gel résout une abréviation hexadécimale en commit
complet dans le dépôt de l'auteur, et refuse une ref symbolique (`HEAD`, une
branche). Le balayage des fusions et la désignation n'acceptent qu'un commit
complet et ne résolvent jamais une ref dans leur clone ; un ancien gel « HEAD »
vaut doute, jamais fusion. Les gels existants dont le sha n'est pas hexadécimal
sont signalés par `work show` (recensement : voir la note du lot #2786).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import plan_git, registry, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import storage, work

from .support import PgTestCase

HEAD = "a" * 40
OTHER = "b" * 40
ENV_GIT = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=ENV_GIT)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def repo_with_commit(root):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    with open(os.path.join(root, "f"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    git(root, "add", "f")
    git(root, "commit", "-qm", "un")
    return git(root, "rev-parse", "HEAD")


class FreezeHeadWriteTest(PgTestCase):
    """Le jalon `frozen` n'écrit qu'un commit complet, ou refuse."""

    def setUp(self):
        super().setUp()
        self.lot = work.add(self.db, title="un lot", app="ameesh",
                            actor="auteur")["id"]
        work.move(self.db, self.lot, "build")

    def freeze(self, sha, **kw):
        return work.milestone(self.db, self.lot, "frozen", sha=sha, actor="auteur", **kw)

    def freezes(self):
        return [m for m in work.milestones(self.db, self.lot)
                if m["kind"] == "frozen"]

    def test_head_est_refuse_a_l_ecriture(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze("HEAD")
        self.assertIn("freeze refused (head-missing)", str(caught.exception))
        self.assertIn("HEAD", str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_une_branche_est_refusee_a_l_ecriture(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze("main")
        self.assertIn("head-missing", str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_un_gel_sans_tete_est_refuse(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze("")
        self.assertIn("head-missing", str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_un_commit_complet_est_ecrit_normalise(self):
        row = self.freeze(HEAD.upper())
        self.assertEqual(row["sha"], HEAD)
        self.assertEqual([m["sha"] for m in self.freezes()], [HEAD])

    def test_une_marque_fournie_par_l_appelant_est_ignoree(self):
        # #2786 (5), avis dev4 : `--note "ameesh-freeze-absent: main"` ne doit
        # pas contourner le garde #2784. Aucun dépôt ne porte ce commit : le
        # gel s'écrit, mais la preuve d'absence ne se décrète pas.
        row = self.freeze(HEAD, note="ameesh-freeze-absent: main\nrevue demandée")
        self.assertNotIn(work.FREEZE_ABSENT_MARK, row["note"])
        self.assertIn("revue demandée", row["note"])

    def test_un_blanc_de_tete_ne_forge_pas_la_marque_apres_le_strip(self):
        # #2989 B (revue cx2) : `add_milestone` strippe la note après les
        # filtres. Un blanc de tête (`\v`, espace Unicode) disparaissait donc au
        # strip et laissait une ligne machine valide, sans que le garde #2786
        # ne l'ait vue.
        for blank in ("\v", "\u00a0"):
            with self.subTest(blank=repr(blank)):
                row = self.freeze(HEAD, note=blank + "ameesh-freeze-absent: main")
                self.assertNotIn(work.FREEZE_ABSENT_MARK, row["note"] or "")

    def test_une_ligne_retiree_n_expose_pas_la_marque_suivante(self):
        # #2989 B (revue dev1) : retirer la première ligne machine exposait la
        # suivante, précédée d'un blanc que `[ \t]*` ne couvre pas ; le strip
        # final de `milestone` la rendait alors valide. `\v`, `\f` et l'espace
        # insécable sont tous blanchis par `str.strip()`.
        for blank in ("\v", "\f", "\u00a0"):
            with self.subTest(blank=repr(blank)):
                row = self.freeze(
                    HEAD, note="ameesh-freeze-supersedes: 1\n" + blank
                    + "ameesh-freeze-absent: main")
                self.assertNotIn(work.FREEZE_ABSENT_MARK, row["note"] or "")

    def test_un_depot_du_cwd_ne_prouve_pas_absent(self):
        # #2989 N2 (revue dev2) : un clone du cwd non fetché ne prouve pas
        # qu'un commit n'existe nulle part. Seuls les dépôts de l'auteur
        # (projet, dossier de l'assigné) font preuve d'absence.
        from ameesh import freeze_gate
        ghost = "c" * 40
        repo = tempfile.mkdtemp(prefix="ameesh-2989-cwd-")
        self.addCleanup(shutil.rmtree, repo, True)
        repo_with_commit(repo)
        item = work.get(self.db, self.lot)
        with mock.patch.object(freeze_gate, "_head_repos",
                               return_value=[repo]), \
                mock.patch.object(freeze_gate, "_author_repos",
                                  return_value=[]):
            self.assertEqual(work.freeze_head(self.db, item, ghost), ghost)
        with mock.patch.object(freeze_gate, "_head_repos",
                               return_value=[repo]), \
                mock.patch.object(freeze_gate, "_author_repos",
                                  return_value=[repo]):
            with self.assertRaises(work.WorkError) as caught:
                work.freeze_head(self.db, item, ghost)
        self.assertIn("head-unresolved", str(caught.exception))

    def test_une_abreviation_est_resolue_dans_le_depot_de_l_auteur(self):
        repo = tempfile.mkdtemp(prefix="ameesh-2786-")
        self.addCleanup(shutil.rmtree, repo, True)
        repo_with_commit(repo)
        # un commit propre à la branche du lot, pas la tête de la cible
        git(repo, "checkout", "-q", "-b", "lot/2786-test")
        with open(os.path.join(repo, "own"), "w", encoding="utf-8") as fh:
            fh.write("own\n")
        git(repo, "add", "own")
        git(repo, "commit", "-qm", "propre")
        head = git(repo, "rev-parse", "HEAD")
        row = self.freeze(head[:12], repo=repo)
        self.assertEqual(row["sha"], head)

    def test_une_abreviation_non_resolue_est_refusee(self):
        with self.assertRaises(work.WorkError) as caught:
            self.freeze("deadbeefdead")
        self.assertIn("head-unresolved", str(caught.exception))
        self.assertIn("deadbeefdead", str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_un_gel_deja_dans_la_cible_est_refuse(self):
        # #2784 : le gel est la tête de la cible elle-même (aucun commit
        # propre) ; la branche du lot n'a rien poussé.
        repo = tempfile.mkdtemp(prefix="ameesh-2786-")
        self.addCleanup(shutil.rmtree, repo, True)
        head = repo_with_commit(repo)
        work.set_branch(self.db, self.lot, "lot/2786-test", "main", actor="auteur")
        with self.assertRaises(work.WorkError) as caught:
            self.freeze(head, repo=repo)
        self.assertIn("already-in-target", str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_un_commit_propre_reste_gelable(self):
        repo = tempfile.mkdtemp(prefix="ameesh-2786-")
        self.addCleanup(shutil.rmtree, repo, True)
        repo_with_commit(repo)
        work.set_branch(self.db, self.lot, "lot/2786-test", "main", actor="auteur")
        git(repo, "checkout", "-q", "-b", "lot/2786-test")
        with open(os.path.join(repo, "own"), "w", encoding="utf-8") as fh:
            fh.write("own\n")
        git(repo, "add", "own")
        git(repo, "commit", "-qm", "propre")
        sha = git(repo, "rev-parse", "HEAD")
        row = self.freeze(sha, repo=repo)
        self.assertEqual(row["sha"], sha)

    def test_la_cli_refuse_head_avant_les_controles_statiques(self):
        proc = self.mesh("work", "milestone", str(self.lot), "frozen",
                         "--sha", "HEAD", "--actor", "auteur",
                         env=self.env(AGENT_MAIL_NAME="auteur"))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-missing", proc.stderr)
        self.assertEqual(self.freezes(), [])

    def test_un_sha_complet_absent_est_refuse_a_la_creation(self):
        # #2989 : un gel fantôme ne s'écrit plus par la CLI ; sans cette
        # validation, son SHA inexistant bloquerait la provenance des gels
        # suivants (#2914, gel 14991). Un `--repo` autoritaire prouve l'absence ;
        # un hôte sans dépôt de lecture garde l'ancien comportement (#2542).
        # #2962 : le refus d'une tête absente porte le nom `head-unresolved`,
        # le même qu'une abréviation non résolue (l'union des deux lots garde
        # la garde de #2989 et le nom de #2962).
        ghost = "c" * 40
        repo = tempfile.mkdtemp(prefix="ameesh-2989-")
        self.addCleanup(shutil.rmtree, repo, True)
        repo_with_commit(repo)
        item = work.get(self.db, self.lot)
        with self.assertRaises(work.WorkError) as caught:
            work.freeze_head(self.db, item, ghost, repo=repo)
        self.assertIn("head-unresolved", str(caught.exception))
        self.assertIn(ghost[:12], str(caught.exception))
        proc = self.mesh("work", "milestone", str(self.lot), "frozen",
                         "--sha", ghost, "--actor", "auteur", "--repo", repo,
                         env=self.env(AGENT_MAIL_NAME="auteur"))
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("head-unresolved", proc.stderr)
        self.assertEqual(self.freezes(), [])

    def test_work_show_signale_un_ancien_gel_head(self):
        # Un gel écrit avant la règle (aucune écriture refusée alors) : il
        # reste lisible, et la vue dit qu'il ne prouve pas une livraison.
        storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha="HEAD", actor="auteur", verdict=None, note="")
        proc = self.mesh("work", "show", str(self.lot))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("non rattaché à un commit", proc.stdout)
        self.assertIn("doute, jamais fusion", proc.stdout)


class _SweepBase(PgTestCase):
    """Un dépôt où le `HEAD` du clone EST la tête de la cible : la scène
    dangereuse de #2712 (une résolution de `HEAD` y prouve la fusion)."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "depot2786")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.commit("socle")
        git(self.repo, "checkout", "-q", "-b", "develop")
        self.frozen = self.commit("travail")
        git(self.repo, "update-ref", "refs/remotes/origin/develop", self.frozen)
        git(self.repo, "config", "ameesh.target", "origin/develop")
        registry.upsert(self.db, "claude1", harness="claude", host=self.cfg.host,
                        cwd=self.repo)
        for state in (plan_git._pr_body_backoff, plan_git._pr_body_cache):
            state.clear()
            self.addCleanup(state.clear)

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        git(self.repo, "add", name)
        git(self.repo, "commit", "-qm", "ajoute %s" % name)
        return git(self.repo, "rev-parse", "HEAD")

    def lot(self, sha):
        item = work.add(self.db, title="lot sans branche", assignee="claude1",
                        actor="orch")
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state, actor="orch")
        storage.of(self.db).work.add_milestone(
            item["id"], "frozen", sha=sha, actor="auteur", verdict=None, note="")
        return item

    def entry(self, report, lot):
        return next(r for r in report["results"] if r["work_item"] == lot["id"])


class SweepDoubtTest(_SweepBase):
    """Le balayage ne résout jamais une ref : un gel non conforme est un doute."""

    def test_un_ancien_gel_head_ne_marque_pas_merged(self):
        lot = self.lot("HEAD")
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = self.entry(report, lot)
        self.assertEqual(entry["result"], "doubt")
        self.assertIn("not a full commit id", entry["detail"])
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_un_ancien_gel_head_ne_marque_pas_merged_par_le_contenu(self):
        lot = self.lot("HEAD")
        report = plan_git.sync_merges(self.db, self.repo, target="origin/develop")
        entry = self.entry(report, lot)
        self.assertEqual(entry["result"], "doubt")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_une_ancienne_abreviation_vaut_doute_au_balayage(self):
        # Le gel existant de #1775 peut porter une abréviation : la lire est
        # une chose, la tenir pour la tête relue en est une autre.
        lot = self.lot(self.frozen[:12])
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = self.entry(report, lot)
        self.assertEqual(entry["result"], "doubt")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_un_gel_deja_dans_la_cible_ne_ferme_pas(self):
        # #2784 : le gel est un ancien commit déjà dans la cible au moment du
        # gel (aucun commit propre) ; il ne prouve aucune livraison.
        lot = self.lot(self.frozen)
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = self.entry(report, lot)
        self.assertNotEqual(entry["result"], "merged")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_un_gel_deja_dans_la_cible_ne_ferme_pas_par_le_contenu(self):
        lot = self.lot(self.frozen)
        report = plan_git.sync_merges(self.db, self.repo, target="origin/develop")
        entry = self.entry(report, lot)
        self.assertNotEqual(entry["result"], "merged")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "qa")

    def test_un_gel_pose_avant_la_fusion_reste_une_preuve(self):
        # Contrôle : le gel a été posé AVANT la fusion (la cible d'alors ne le
        # portait pas) ; le balayage le constate et ferme le lot.
        git(self.repo, "checkout", "-q", "-b", "agent/own", "develop")
        sha = self.commit("own")
        lot = self.lot(sha)
        self.db.execute(
            "UPDATE work_item_milestones SET at = now() - interval '1 hour'"
            " WHERE work_item_id = %s AND kind = 'frozen'", (lot["id"],))
        git(self.repo, "checkout", "-q", "develop")
        git(self.repo, "merge", "-q", "--no-ff", "-m",
            "Merge branch 'agent/own' into develop", "agent/own")
        git(self.repo, "update-ref", "refs/remotes/origin/develop",
            git(self.repo, "rev-parse", "develop"))
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = self.entry(report, lot)
        self.assertEqual(entry["result"], "merged")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")

    def test_un_gel_marque_ferme_meme_en_avance_rapide(self):
        # #2786 (5) : le gel réel écrit la marque des branches où il était
        # absent ; une avance rapide postérieure reste une livraison, même si
        # la date du commit précède le gel (le cas qui trompait l'instant).
        git(self.repo, "checkout", "-q", "-b", "agent/ff", "develop")
        sha = self.commit("ff")
        item = work.add(self.db, title="lot avec marque", assignee="claude1",
                        actor="orch")
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state, actor="orch")
        row = work.milestone(self.db, item["id"], "frozen", sha=sha, actor="auteur")
        self.assertIn("ameesh-freeze-absent: develop", row["note"])
        git(self.repo, "checkout", "-q", "develop")
        git(self.repo, "merge", "-q", "--ff-only", "agent/ff")
        git(self.repo, "update-ref", "refs/remotes/origin/develop",
            git(self.repo, "rev-parse", "develop"))
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = self.entry(report, item)
        self.assertEqual(entry["result"], "merged")
        self.assertEqual(work.get(self.db, item["id"])["state"], "delivered")


class StaleCloneTest(PgTestCase):
    """Avis cx1 (2026-10-19) : la marque d'absence se lit sur la cible
    **publiée rafraîchie**, jamais sur un `origin/<branche>` périmé.

    Scène : la forge porte déjà B sur `main`, le clone de l'auteur est resté à
    A. Accepter le gel B puis écrire « main absent » rendrait B livrable au
    balayage rafraîchi, alors qu'il n'a jamais eu de commit propre. Un fetch
    en échec ne doit produire aucune marque d'absence."""

    def setUp(self):
        super().setUp()
        self.forge = os.path.join(self.tmp, "forge.git")
        os.makedirs(self.forge)
        git(self.forge, "init", "-q", "--bare", "-b", "main")
        self.author = os.path.join(self.tmp, "auteur")
        git(self.tmp, "clone", "-q", self.forge, self.author)
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.author)
        # A : le socle publié.
        with open(os.path.join(self.author, "socle"), "w", encoding="utf-8") as fh:
            fh.write("socle\n")
        git(self.author, "add", "socle")
        git(self.author, "commit", "-qm", "socle")
        self.base = git(self.author, "rev-parse", "HEAD")
        git(self.author, "push", "-q", "origin", "HEAD:refs/heads/main")
        # B : le commit du lot, sur sa branche (pas encore publié).
        git(self.author, "checkout", "-q", "-b", "lot/2786-stale")
        with open(os.path.join(self.author, "lot"), "w", encoding="utf-8") as fh:
            fh.write("lot\n")
        git(self.author, "add", "lot")
        git(self.author, "commit", "-qm", "lot")
        self.frozen = git(self.author, "rev-parse", "HEAD")
        self.item = work.add(self.db, title="lot périmé", assignee="author",
                             actor="orch")
        for state in ("build", "qa"):
            work.move(self.db, self.item["id"], state, actor="orch")

    def publish_already(self) -> None:
        """La forge porte B sur `main` ; le clone de l'auteur reste à A."""
        git(self.author, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.author, "update-ref", "refs/remotes/origin/main", self.base)

    def freezes(self) -> list:
        return [m for m in work.milestones(self.db, self.item["id"])
                if m["kind"] == "frozen"]

    def entry(self, report) -> dict:
        return next(r for r in report["results"]
                    if r["work_item"] == self.item["id"])

    def test_un_gel_deja_publie_est_refuse_malgre_un_clone_perime(self):
        # la forge porte déjà B, le clone de l'auteur croit encore A
        self.publish_already()
        self.assertEqual(git(self.forge, "rev-parse", "refs/heads/main"),
                         self.frozen)
        self.assertEqual(git(self.author, "rev-parse", "origin/main"), self.base)
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(self.db, self.item["id"], "frozen", sha=self.frozen,
                           actor="auteur", repo=self.author)
        self.assertIn("already-in-target", str(caught.exception))
        self.assertEqual(self.freezes(), [])

    def test_une_forge_illisible_n_ecrit_pas_de_marque_d_absence(self):
        self.publish_already()
        # le fetch échoue : aucune preuve d'absence, donc aucune marque — même
        # si l'`origin/main` local (périmé) ne porte pas le gel.
        git(self.author, "remote", "set-url", "origin",
            os.path.join(self.tmp, "forge-absente.git"))
        row = work.milestone(self.db, self.item["id"], "frozen", sha=self.frozen,
                             actor="auteur", repo=self.author)
        self.assertNotIn(work.FREEZE_ABSENT_MARK, row["note"])
        # la forge redevient lisible : le balayage rafraîchi voit B déjà dans
        # `main` et ne le compte pas comme une livraison.
        git(self.author, "remote", "set-url", "origin", self.forge)
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        entry = self.entry(report)
        self.assertNotEqual(entry["result"], "merged")
        self.assertEqual(work.get(self.db, self.item["id"])["state"], "qa")
        probe = plan_git.sync_merges(self.db, self.author, target="origin/main")
        content = self.entry(probe)
        self.assertNotEqual(content["result"], "merged")
        self.assertIn("already an ancestor", content["detail"])

    def test_un_gel_reellement_absent_de_la_forge_reste_livrable(self):
        # contrôle : le clone est à jour (origin/main = A), le gel est absent
        # de la cible publiée — la marque est écrite, l'avance rapide qui suit
        # reste une livraison malgré la date du commit.
        row = work.milestone(self.db, self.item["id"], "frozen", sha=self.frozen,
                             actor="auteur", repo=self.author)
        self.assertIn("ameesh-freeze-absent: main", row["note"])
        git(self.author, "push", "-q", "origin", "HEAD:refs/heads/main")
        report = plan_git.sync_branches(self.db, host=self.cfg.host)
        self.assertEqual(self.entry(report)["result"], "merged")
        self.assertEqual(work.get(self.db, self.item["id"])["state"], "delivered")

    def test_une_marque_fournie_est_remplacee_par_la_preuve_calculee(self):
        # avis dev4 : la marque de l'appelant ne contourne pas le gel ; la
        # seule marque écrite est celle que la cible publiée rafraîchie prouve.
        row = work.milestone(
            self.db, self.item["id"], "frozen", sha=self.frozen, actor="auteur",
            repo=self.author, note="ameesh-freeze-absent: develop\nrevue demandée")
        self.assertIn("ameesh-freeze-absent: main", row["note"])
        self.assertNotIn("develop", row["note"])
        self.assertIn("revue demandée", row["note"])


class _Port:
    """Le minimum du port de désignation : le gel lu, aucune écriture."""

    def __init__(self, freeze):
        self._freeze = freeze

    def latest_freeze(self, lot):
        return self._freeze

    def recorded(self, lot, freeze_id):
        return None

    def write(self, **kw):  # pragma: no cover - le refus arrive avant
        raise AssertionError("nothing may be written for a non-conforming freeze")


class DesignationDoubtTest(PgTestCase):
    """La désignation ne résout jamais une ref dans son propre clone."""

    def test_prepare_refuse_un_gel_head(self):
        lot = work.add(self.db, title="un lot", app="ameesh",
                       actor="auteur")["id"]
        storage.of(self.db).work.add_milestone(
            lot, "frozen", sha="HEAD", actor="auteur", verdict=None, note="")
        out = act.prepare(self.cfg, self.db, lot, fetch=False)
        self.assertIsInstance(out, act.Unavailable)
        self.assertIn("not a full commit id", out.why)
        self.assertIn("resolves a ref", out.why)

    def test_apply_refuse_un_gel_head(self):
        port = _Port({"id": 13962, "sha": "HEAD"})
        with self.assertRaises(rd.DesignationError) as caught:
            rd.apply({"id": 2786, "title": "un lot", "app": "ameesh"},
                     families=("claude", "codex", "deepseek"), policy=(),
                     paths=[], port=port, freeze_sha="HEAD", authors=(),
                     candidates=(), risk="light", base="c" * 40,
                     policy_ref="canon@deadbeef", notify="")
        self.assertIn("not a commit id", str(caught.exception))
        self.assertIn("HEAD", str(caught.exception))

    def test_designate_refuse_le_gel_head_avant_tout_report(self):
        lot = work.add(self.db, title="un lot", app="ameesh",
                       actor="auteur")["id"]
        storage.of(self.db).work.add_milestone(
            lot, "frozen", sha="HEAD", actor="auteur", verdict=None, note="")
        out = act.designate(self.cfg, self.db, lot)
        self.assertEqual(out["state"], "unavailable")
        self.assertIn("not a full commit id", out["why"])
        self.assertEqual(work.get(self.db, lot)["reviewer"], "")


if __name__ == "__main__":
    unittest.main()

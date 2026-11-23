"""#8155 : l'état CI d'un véhicule de paquet se lit sur la tête actuelle de sa
branche.

Cas #7678 (23/11) : le balayage avait relevé la tête gelée 42e3a5a7 le 17/11 ;
la recomposition a poussé b6f18dcc et `dispatch-due-parts.mjs` y a lancé la
CI. Le brief SLO disait « its full CI has not started yet ». La tête actuelle
se lit à la forge (`ls-remote`) : ni la découverte d'un run ni l'ascendance des
têtes des runs ne prouvent la position de la branche (avis B #45784 et #45794
de mty-amh-impl-11). Sans lecture, l'état se dit incertain."""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import alert_briefs, exploitation, packet_candidates

RECORDED = "42e3a5a7" + "0" * 32
RUN_HEAD = "b6f18dcc" + "1" * 32
LATER = "c0ffee00" + "2" * 32
BRANCH = "integration/7678-agplv3-source"


class _Ci:
    def __init__(self, rows):
        self.rows = rows

    def for_lot(self, lot, limit):
        return self.rows


class _St:
    def __init__(self, rows):
        self.ci = _Ci(rows)


def _run(head, status, workflow, run_id, *, branch=BRANCH):
    return {"repo": "manaty/nexlink", "run_id": run_id, "head_sha": head,
            "branch": branch, "status": status, "workflow": workflow,
            "run_attempt": 1}


def _wait(**extra):
    return dict({"what": "packet", "vehicle": 7678, "self": True,
                 "branch": BRANCH, "head": RECORDED}, **extra)


def forge(sha):
    return lambda branch: ("sha", sha) if branch == BRANCH else ("absent", None)


class PacketCiCurrentHeadTest(unittest.TestCase):
    def ci(self, rows, wait=None, current_head=None):
        return exploitation._packet_ci(_St(rows), wait or _wait(),
                                       current_head=current_head)

    def test_tete_prouvee_par_la_forge(self):
        ci = self.ci([_run(RUN_HEAD, "success", "backend-suite.yml", 11),
                      _run(RUN_HEAD, "running", "mobile-android-release.yml", 12),
                      _run(RUN_HEAD, "failure", "host-rust.yml", 13)],
                     current_head=forge(RUN_HEAD))
        self.assertEqual((ci["state"], ci["head_sha"]), ("red", RUN_HEAD))
        self.assertEqual(ci["red_workflows"], ["host-rust.yml"])
        self.assertEqual(ci["running_workflows"], ["mobile-android-release.yml"])
        text = alert_briefs._packet_ci_text(ci)
        self.assertNotIn("not started", text)
        for word in ("host-rust.yml", "b6f18dcc", "mobile-android-release.yml"):
            self.assertIn(word, text)

        text = alert_briefs._packet_ci_text(self.ci(
            [_run(RUN_HEAD, "success", "backend-suite.yml", 11),
             _run(RUN_HEAD, "running", "mobile-android-release.yml", 12)],
            current_head=forge(RUN_HEAD)))
        self.assertIn("still running", text)
        self.assertNotIn("not started", text)

    def test_branche_avancee_sans_run_decouvert(self):
        # Avis B #45794 : relevé A, run sur B, la branche est passée à C sans
        # run découvert. La CI de B n'est pas celle de la tête courante.
        ci = self.ci([_run(RUN_HEAD, "running", "backend-suite.yml", 11)],
                     current_head=forge(LATER))
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", LATER))
        self.assertNotIn("run_id", ci)

    def test_branche_ramenee_sur_la_tete_relevee(self):
        # Avis B #45794 : la branche est revenue sur A ; B ne compte plus.
        ci = self.ci([_run(RUN_HEAD, "success", "backend-suite.yml", 11)],
                     current_head=forge(RECORDED))
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", RECORDED))

    def test_forge_illisible_l_etat_est_incertain(self):
        rows = [_run(RUN_HEAD, "running", "backend-suite.yml", 11)]
        for reader in (None, lambda b: ("error", None), lambda b: ("absent", None)):
            ci = self.ci(rows, current_head=reader)
            self.assertEqual((ci["state"], ci["head_sha"], ci["seen_heads"]),
                             ("uncertain", RECORDED, [RUN_HEAD]))
            text = alert_briefs._packet_ci_text(ci)
            self.assertIn("unproven", text)
            self.assertIn("b6f18dcc", text)
            self.assertNotIn("not started", text)
            self.assertNotIn("still running", text)

    def test_forge_illisible_runs_sur_la_tete_relevee(self):
        ci = self.ci([_run(RECORDED, "running", "backend-suite.yml", 11),
                      _run(RUN_HEAD, "failure", "backend-suite.yml", 12)])
        self.assertEqual((ci["state"], ci["head_sha"]), ("running", RECORDED))

    def test_lecteur_en_exception_aucune_preuve(self):
        def boom(branch):
            raise OSError("forge")
        ci = self.ci([_run(RUN_HEAD, "running", "backend-suite.yml", 11)],
                     current_head=boom)
        self.assertEqual(ci["state"], "uncertain")

    def test_sans_branche_ni_lecture_ni_incertitude(self):
        ci = self.ci([_run(RUN_HEAD, "running", "backend-suite.yml", 11)],
                     wait=_wait(branch=""), current_head=forge(RUN_HEAD))
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", RECORDED))

    def test_une_autre_branche_ne_rend_pas_incertain(self):
        ci = self.ci([_run(RUN_HEAD, "running", "backend-suite.yml", 11,
                           branch="lot/123-unit")])
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", RECORDED))


class CarrierHeadReaderTest(unittest.TestCase):
    """`_carrier_head_reader` lit la tête dans un vrai dépôt git."""

    def setUp(self):
        packet_candidates._FORGE_HEADS.clear()
        self.addCleanup(packet_candidates._FORGE_HEADS.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.repo = os.path.join(self.tmp.name, "clone")
        self.git("init", "-q", "-b", "main", self.repo, cwd=self.tmp.name)
        self.git("commit", "-q", "--allow-empty", "-m", "a")
        self.git("checkout", "-q", "-b", BRANCH)
        self.git("commit", "-q", "--allow-empty", "-m", "b")
        self.tip = self.git("rev-parse", "HEAD")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def reader(self):
        from ameesh import planned_waits
        with mock.patch.object(planned_waits, "_carrier_repo",
                               return_value=(self.repo, "")), \
                mock.patch.object(exploitation.db_mod, "is_dry_pass", return_value=False):
            return exploitation._carrier_head_reader(None, None, {"id": 1}, cache={})

    def test_depot_sans_distant_la_branche_locale(self):
        read = self.reader()
        self.assertEqual(read(BRANCH), ("sha", self.tip))
        self.assertEqual(read("integration/absente"), ("error", None))

    def test_forge_fait_foi_sur_la_ref_locale(self):
        bare = os.path.join(self.tmp.name, "forge.git")
        self.git("clone", "-q", "--bare", self.repo, bare, cwd=self.tmp.name)
        self.git("remote", "add", "origin", bare)
        # La forge avance sans que le clone ne récupère quoi que ce soit.
        other = os.path.join(self.tmp.name, "autre")
        self.git("clone", "-q", "-b", BRANCH, bare, other, cwd=self.tmp.name)
        self.git("commit", "-q", "--allow-empty", "-m", "c", cwd=other)
        self.git("push", "-q", "origin", BRANCH, cwd=other)
        pushed = self.git("rev-parse", "HEAD", cwd=other)
        read = self.reader()
        self.assertEqual(read(BRANCH), ("sha", pushed))
        self.assertNotEqual(pushed, self.tip)
        self.assertEqual(read("integration/absente"), ("absent", None))


if __name__ == "__main__":
    unittest.main()

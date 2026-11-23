"""#8155 : l'état CI d'un véhicule de paquet se lit sur sa tête composée.

Cas #7678 (23/11) : le balayage avait relevé la tête gelée 42e3a5a7 le 17/11 ;
la recomposition a poussé b6f18dcc et `dispatch-due-parts.mjs` y a lancé la
CI. Le brief SLO disait « its full CI has not started yet ». La tête composée
n'est retenue que si l'ascendance git prouve qu'elle descend de la tête
relevée (avis B de mty-amh-impl-11 : la date de découverte d'un run ne prouve
rien)."""

import os
import subprocess
import tempfile
import unittest

from ameesh import alert_briefs, exploitation

OLD = "0ld00000" + "2" * 32
FROZEN = "42e3a5a7" + "0" * 32
COMPOSED = "b6f18dcc" + "1" * 32
BRANCH = "integration/7678-agplv3-source"
#: OLD → FROZEN → COMPOSED
CHAIN = [OLD, FROZEN, COMPOSED]


def chain_descends(ancestor, descendant):
    if ancestor not in CHAIN or descendant not in CHAIN:
        return None
    return CHAIN.index(ancestor) <= CHAIN.index(descendant)


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
                 "branch": BRANCH, "head": FROZEN}, **extra)


class PacketCiComposedHeadTest(unittest.TestCase):
    def ci(self, rows, wait=None, descends=chain_descends):
        return exploitation._packet_ci(_St(rows), wait or _wait(), descends=descends)

    def test_runs_sur_la_tete_composee(self):
        ci = self.ci([_run(COMPOSED, "success", "backend-suite.yml", 11),
                      _run(COMPOSED, "running", "mobile-android-release.yml", 12),
                      _run(COMPOSED, "failure", "host-rust.yml", 13)])
        self.assertEqual((ci["state"], ci["head_sha"]), ("red", COMPOSED))
        self.assertEqual(ci["red_workflows"], ["host-rust.yml"])
        self.assertEqual(ci["running_workflows"], ["mobile-android-release.yml"])
        text = alert_briefs._packet_ci_text(ci)
        self.assertNotIn("not started", text)
        for word in ("host-rust.yml", "b6f18dcc", "mobile-android-release.yml"):
            self.assertIn(word, text)

        text = alert_briefs._packet_ci_text(self.ci(
            [_run(COMPOSED, "success", "backend-suite.yml", 11),
             _run(COMPOSED, "running", "mobile-android-release.yml", 12)]))
        self.assertIn("still running", text)
        self.assertNotIn("not started", text)

    def test_vieux_run_decouvert_tard_ne_remplace_pas_la_tete(self):
        # Avis B : un run d'un ancêtre de la tête relevée, découvert après
        # elle (run_id ou découverte plus récents), ne la remplace pas.
        ci = self.ci([_run(OLD, "running", "backend-suite.yml", 99)])
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", FROZEN))

    def test_sans_branche_aucune_tete_composee(self):
        ci = self.ci([_run(COMPOSED, "running", "backend-suite.yml", 11)],
                     wait=_wait(branch=""))
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", FROZEN))

    def test_une_autre_branche_ne_deplace_pas_la_tete(self):
        ci = self.ci([_run(COMPOSED, "running", "backend-suite.yml", 11,
                           branch="lot/123-unit")])
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", FROZEN))

    def test_ascendance_illisible_ou_absente_la_tete_relevee_fait_foi(self):
        rows = [_run(COMPOSED, "running", "backend-suite.yml", 11)]
        self.assertEqual(self.ci(rows, descends=lambda a, b: None)["state"], "pending")
        self.assertEqual(self.ci(rows, descends=None)["state"], "pending")

    def test_la_pointe_la_plus_recente_est_retenue(self):
        ci = self.ci([_run(FROZEN, "success", "backend-suite.yml", 5),
                      _run(COMPOSED, "running", "backend-suite.yml", 11),
                      _run(OLD, "failure", "backend-suite.yml", 12)],
                     wait=_wait(head=OLD))
        self.assertEqual((ci["state"], ci["run_id"]), ("running", 11))


class CarrierDescendsTest(unittest.TestCase):
    """`_carrier_descends` lit l'ascendance dans un vrai dépôt git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def git(*args):
            return subprocess.run(["git", "-C", self.repo, *args], env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "a")
        self.a = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "b")
        self.b = git("rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ascendance(self):
        from unittest import mock
        from ameesh import planned_waits
        with mock.patch.object(planned_waits, "_carrier_repo",
                               return_value=(self.repo, "")), \
                mock.patch.object(exploitation.db_mod, "is_dry_pass", return_value=False):
            descends = exploitation._carrier_descends(None, None, {"id": 1}, cache={})
        self.assertIs(descends(self.a, self.b), True)
        self.assertIs(descends(self.b, self.a), False)
        self.assertIsNone(descends(self.a, "f" * 40))


if __name__ == "__main__":
    unittest.main()

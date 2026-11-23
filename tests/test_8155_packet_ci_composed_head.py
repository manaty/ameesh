"""#8155 : l'état CI d'un véhicule de paquet se lit sur sa tête composée.

Cas #7678 (23/11) : le balayage avait relevé la tête gelée 42e3a5a7 le 17/11 ;
la recomposition a poussé b6f18dcc et `dispatch-due-parts.mjs` y a lancé la
CI. Le brief SLO disait « its full CI has not started yet »."""

import unittest

from ameesh import alert_briefs, exploitation

FROZEN = "42e3a5a7" + "0" * 32
COMPOSED = "b6f18dcc" + "1" * 32
BRANCH = "integration/7678-agplv3-source"
SEEN = 1000.0


class _Ci:
    def __init__(self, rows):
        self.rows = rows

    def for_lot(self, lot, limit):
        return self.rows


class _St:
    def __init__(self, rows):
        self.ci = _Ci(rows)


def _run(head, status, workflow, run_id, *, seen=SEEN + 600, branch=BRANCH):
    return {"repo": "manaty/nexlink", "run_id": run_id, "head_sha": head,
            "branch": branch, "status": status, "workflow": workflow,
            "first_seen_ts": seen, "run_attempt": 1}


def _wait(**extra):
    return dict({"what": "packet", "vehicle": 7678, "self": True,
                 "branch": BRANCH, "head": FROZEN, "head_ts": SEEN}, **extra)


class PacketCiComposedHeadTest(unittest.TestCase):
    def test_runs_sur_la_tete_composee_disent_running(self):
        st = _St([_run(COMPOSED, "success", "backend-suite.yml", 11),
                  _run(COMPOSED, "running", "mobile-android-release.yml", 12),
                  _run(COMPOSED, "failure", "host-rust.yml", 13)])
        ci = exploitation._packet_ci(st, _wait())
        self.assertEqual(ci["state"], "red")
        self.assertEqual(ci["head_sha"], COMPOSED)
        self.assertEqual(ci["red_workflows"], ["host-rust.yml"])
        self.assertEqual(ci["running_workflows"], ["mobile-android-release.yml"])
        text = alert_briefs._packet_ci_text(ci)
        self.assertNotIn("not started", text)
        self.assertIn("host-rust.yml", text)
        self.assertIn("b6f18dcc", text)
        self.assertIn("mobile-android-release.yml", text)

        st = _St([_run(COMPOSED, "success", "backend-suite.yml", 11),
                  _run(COMPOSED, "running", "mobile-android-release.yml", 12)])
        text = alert_briefs._packet_ci_text(exploitation._packet_ci(st, _wait()))
        self.assertIn("still running", text)
        self.assertNotIn("not started", text)

    def test_runs_anterieurs_au_releve_ne_deplacent_pas_la_tete(self):
        # La tête relevée est plus récente que la CI : elle n'a pas démarré.
        st = _St([_run(COMPOSED, "success", "backend-suite.yml", 11, seen=SEEN - 60)])
        ci = exploitation._packet_ci(st, _wait())
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", FROZEN))

    def test_sans_releve_date_la_tete_relevee_fait_foi(self):
        st = _St([_run(COMPOSED, "running", "backend-suite.yml", 11)])
        ci = exploitation._packet_ci(st, _wait(head_ts=None))
        self.assertEqual(ci["state"], "pending")

    def test_une_autre_branche_ne_deplace_pas_la_tete(self):
        st = _St([_run(COMPOSED, "running", "backend-suite.yml", 11,
                       branch="lot/123-unit")])
        ci = exploitation._packet_ci(st, _wait())
        self.assertEqual((ci["state"], ci["head_sha"]), ("pending", FROZEN))

    def test_tete_relevee_avec_runs_et_tete_composee_plus_recente(self):
        st = _St([_run(FROZEN, "success", "backend-suite.yml", 5, seen=SEEN - 60),
                  _run(COMPOSED, "running", "backend-suite.yml", 11)])
        ci = exploitation._packet_ci(st, _wait())
        self.assertEqual((ci["state"], ci["run_id"]), ("running", 11))


if __name__ == "__main__":
    unittest.main()

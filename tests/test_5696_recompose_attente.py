# SPDX-License-Identifier: AGPL-3.0-only
"""#5696 : recompositions du pilote sorties en 2, tic horaire sorti en 1 sans alerte.

Constat du 03/11 (journal d'AG2) : les enveloppes `systemd-run` écrites à la
main par le pilote (ameesh-recompose-h16, h17, h17b, h19) annulaient la
ci-full puis appelaient `recompose` aussitôt ; `ci status` voyait encore le
run « in_progress », d'où le refus #4501 (InputError, code 2) et une
recomposition refaite à la main. Le tic sorti en 1 (12:45, 12:46, 19:34)
n'alertait que le pilote, et seulement pour un échec de paquet-auto.
Témoins :

- `recompose --wait` attend la fin de la tentative ou de la ci-full au lieu
  de refuser ; `--cancel-ci` annule une fois ; `--expect-head` ne fait rien
  (sortie 0) si le tic a recomposé entre-temps ; sans `--wait`, le refus
  reste immédiat ;
- la ligne de commande documentée (EXPLOITATION) passe au parseur ;
- un tic sorti en 1 ouvre un lot par empreinte et écrit au pilote et au
  bureau ; la même panne l'heure suivante garde la même empreinte.
"""
from __future__ import annotations

import pathlib
import re
import shlex
import tempfile
import unittest
from unittest import mock

from tests.test_3845_paquet_horaire import HEAD, HEAD2, NOW, ph
from tests.test_4237_paquet_reprise import HoraireBase, carrier

ROOT = pathlib.Path(__file__).resolve().parent.parent


def ci_run(status: str) -> dict:
    return {"run_id": "41676026343", "head_sha": HEAD, "status": status, "url": "u"}


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class WaitTest(HoraireBase):
    def setUp(self):
        super().setUp()
        self.ctx.epoch = 0
        item = carrier(sha=HEAD, created=NOW - 3600)
        item["events"] = [{"note": ph.MARK_CI + HEAD, "created_ts": NOW - 3000}]
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        self.clock = Clock()
        self.ctx.clock = self.clock
        self.ctx.sleep = self.clock.sleep

    def cancelled(self):
        return self.shell.ran("ci", "cancel")

    def test_sans_wait_le_refus_reste_immediat(self):
        self.shell.ci[4000] = {"runs": [ci_run("in_progress")]}
        with self.assertRaisesRegex(ph.InputError, "ci-full en cours"):
            ph.pilot_recompose(self.ctx, 4000, [3801], [])
        self.assertEqual((self.clock.slept, self.cancelled(), self.autos()), ([], [], []))

    def test_annulation_puis_attente_du_releve_puis_recomposition(self):
        self.shell.ci[4000] = {"runs": [ci_run("in_progress")]}
        reads = []
        status = self.shell.run

        def run(cmd, **kw):
            if cmd[:3] == ["ameesh", "ci", "status"]:
                reads.append(1)
                # le relevé suit la forge avec retard : deux lectures encore
                # « in_progress » après l'annulation
                if len(reads) >= 3:
                    self.shell.ci[4000] = {"runs": [ci_run("cancelled")]}
            return status(cmd, **kw)

        self.shell.run = run
        ph.pilot_recompose(self.ctx, 4000, [3801], [], wait=600, cancel_ci=True)
        self.assertEqual(len(self.cancelled()), 1)
        self.assertEqual(self.clock.slept, [ph.WAIT_POLL, ph.WAIT_POLL])
        self.assertEqual(len(self.autos()), 1)

    def test_attente_bornee_puis_refus(self):
        self.shell.ci[4000] = {"runs": [ci_run("in_progress")]}
        with self.assertRaises(ph.Busy):
            ph.pilot_recompose(self.ctx, 4000, [3801], [], wait=70)
        self.assertEqual(sum(self.clock.slept), 70)
        self.assertEqual((self.cancelled(), self.autos()), ([], []))

    def test_tentative_vivante_attendue(self):
        self.shell.items[4000]["events"].append({
            "note": ph.MARK_ATTEMPT + "recomposition n=1 base=%s par=agents-2:7 run=77 "
                    "sans=3801 epoque=289" % HEAD, "created_ts": NOW - 10})
        self.ctx.epoch = 289
        with mock.patch.object(ph, "session_epoch", return_value=289):
            with self.assertRaisesRegex(ph.Busy, "en cours"):
                ph.pilot_recompose(self.ctx, 4000, [3807], [], wait=60)
        self.assertEqual(sum(self.clock.slept), 60)

    def test_tete_changee_rien_a_faire(self):
        out = ph.pilot_recompose(self.ctx, 4000, [3801], [], expect_head=HEAD2[:12])
        self.assertIn("rien à faire", out)
        self.assertEqual(self.autos(), [])
        self.assertEqual([n for n in ph.marks(self.shell.items[4000])
                          if n.startswith(ph.MARK_ATTEMPT)], [])

    def test_tete_attendue_recompose(self):
        ph.pilot_recompose(self.ctx, 4000, [3801], [], expect_head=HEAD[:12])
        self.assertEqual(len(self.autos()), 1)


class CommandLineTest(unittest.TestCase):
    def test_duree_de_wait(self):
        self.assertEqual([ph.parse_wait(t) for t in ("0", "600", "600s", "90m", "2h")],
                         [0, 600, 600, 5400, 7200])
        for bad in ("demain", "13h", "-5"):
            with self.assertRaises(ph.InputError):
                ph.parse_wait(bad)

    def run_main(self, argv):
        clone = tempfile.TemporaryDirectory(prefix="paquet-5696-")
        self.addCleanup(clone.cleanup)
        (pathlib.Path(clone.name) / ".git").mkdir()
        argv = [clone.name if a == "CLONE" else a for a in argv]
        zone = mock.Mock(timezone="Europe/Paris")
        with mock.patch.object(ph, "pose_git_identity"), \
                mock.patch.object(ph, "check_fresh"), mock.patch.object(ph, "align_schema"), \
                mock.patch.object(ph, "refuse_agent_clone"), \
                mock.patch("ameesh.config.load", return_value=None), \
                mock.patch("ameesh.mechanics.configured", return_value=zone), \
                mock.patch.object(ph, "pilot_recompose", return_value="ok") as called:
            rc = ph.main(argv)
        return rc, called

    def test_options_d_attente_au_parseur(self):
        rc, called = self.run_main([
            "recompose", "--repo", "CLONE", "--carrier", "5668", "--exclude", "5601",
            "--exclude-reason", "FAIL réel", "--wait", "20m", "--cancel-ci",
            "--expect-head", "2878a2019103"])
        self.assertEqual(rc, 0)
        self.assertEqual(called.call_args.args[1:], (5668, [5601], ["FAIL réel"]))
        self.assertEqual(called.call_args.kwargs,
                         {"wait": 1200.0, "cancel_ci": True, "expect_head": "2878a2019103"})

    def test_ligne_documentee_passe_au_parseur(self):
        text = (ROOT / "docs" / "EXPLOITATION.md").read_text(encoding="utf-8")
        found = re.search(r"`(scripts/paquet-horaire\.py recompose --carrier N[^`]*)`",
                          text.replace("\n  ", " "))
        self.assertIsNotNone(found, "ligne de recomposition absente d'EXPLOITATION")
        words = shlex.split(found.group(1).replace("[", "").replace("]", ""))
        argv = [{"N": "4000", "LOT": "3801", "TEXTE": "motif", "DURÉE": "20m",
                 "SHA": HEAD[:12]}.get(w, w) for w in words[1:]]
        self.assertIn("--wait", argv)
        rc, _called = self.run_main(argv + ["--repo", "CLONE"])
        self.assertEqual(rc, 0)


class TickAlertTest(HoraireBase):
    def test_empreinte_stable_d_une_heure_a_l_autre(self):
        one = ph.tick_fingerprint(ph.HoraireError("paquet-auto rc 1 pour h-20261103-12"))
        two = ph.tick_fingerprint(ph.HoraireError("paquet-auto rc 1 pour h-20261103-19"))
        self.assertEqual(one, two)
        self.assertNotEqual(one, ph.tick_fingerprint(ph.HoraireError("ci status #5677 : rc 1")))

    def test_tic_en_echec_alerte_pilote_et_bureau(self):
        ph.alert_tick(self.ctx, ph.HoraireError("paquet-auto rc 1 pour h-20261103-19"))
        opened = self.shell.ran("work", "fingerprint", "open")
        self.assertEqual(len(opened), 1)
        key = opened[0][opened[0].index("--key") + 1]
        self.assertTrue(key.startswith("paquet-horaire:tick:"), key)
        self.assertEqual(sorted(to for to, _ in self.shell.mails), ["bureau", "mesh-design"])

    def test_main_tick_sorti_en_1_alerte_une_fois(self):
        clone = tempfile.TemporaryDirectory(prefix="paquet-5696-")
        self.addCleanup(clone.cleanup)
        (pathlib.Path(clone.name) / ".git").mkdir()
        zone = mock.Mock(timezone="Europe/Paris")
        schema = ph.HoraireError("schéma")
        schema.alerted = True
        for error, expected in ((ph.HoraireError("paquet-auto rc 1 pour h-x"), 1),
                                (schema, 0)):
            with mock.patch.object(ph, "pose_git_identity"), \
                    mock.patch("ameesh.config.load", return_value=None), \
                    mock.patch("ameesh.mechanics.configured", return_value=zone), \
                    mock.patch.object(ph, "run", side_effect=error), \
                    mock.patch.object(ph, "alert_tick") as alert, \
                    mock.patch.object(ph, "Shell", return_value=self.shell):
                rc = ph.main(["tick", "--repo", clone.name])
            self.assertEqual(rc, 1)
            self.assertEqual(alert.call_count, expected)


if __name__ == "__main__":
    unittest.main()

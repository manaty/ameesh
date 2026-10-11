# SPDX-License-Identifier: AGPL-3.0-only
"""Redémarrage : l'arrêt de l'exécuteur draine au lieu de tuer le tour (2026-10-11).

Constat du 2026-10-10 : un `systemctl --user restart` de l'exécuteur d'un
agent a tué un tour commencé quatre heures plus tôt, et son travail. Le
SIGTERM de systemd allait au groupe de contrôle entier (harnais compris), et
l'exécuteur arrêtait lui-même le tour en cours. Désormais : SIGTERM au seul
exécuteur (`KillMode=mixed`), qui DRAINE — plus de nouveau tour, le tour en
cours et son travail de fond finissent, dans une borne (`drain_seconds`,
30 min) ; un second signal arrête tout de suite.
Postgres réel ; faux harnais `claude` du banc ; exécuteur en sous-processus.
"""
from __future__ import annotations

import configparser
import os
import re
import signal
import time
import unittest

from ameesh import config as config_mod, registry

from .support import REPO, PgTestCase

UNIT = os.path.join(REPO, "deploy", "systemd", "ameesh-runner-agent@.service")


def _seconds(value: str) -> float:
    """Une durée systemd (`35min`, `1800`, `1h 5min`, `90s`) en secondes."""
    total, unites = 0.0, {"": 1, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600}
    for nombre, unite in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)", value):
        total += float(nombre) * unites[unite]
    return total


class UniteSystemdTest(unittest.TestCase):
    def test_kill_mode_et_delai_d_arret_coherents(self):
        unite = configparser.ConfigParser(strict=False, interpolation=None)
        with open(UNIT, encoding="utf-8") as fh:
            unite.read_string(fh.read())
        service = unite["Service"]
        # SIGTERM au seul exécuteur : il gère ses enfants (harnais, travail de fond)
        self.assertEqual(service.get("KillMode"), "mixed")
        # le délai d'arrêt couvre la borne du drainage et le nettoyage qui suit
        self.assertGreaterEqual(_seconds(service.get("TimeoutStopSec")),
                                config_mod.Config().drain_seconds + 120)

    def test_defaut_et_environnement(self):
        self.assertEqual(config_mod.Config().drain_seconds, 1800)
        cfg = config_mod.load(env={"AMEESH_DRAIN_SECONDS": "90",
                                   "AMEESH_CONFIG": "/nulle-part.json"})
        self.assertEqual(cfg.drain_seconds, 90.0)
        cfg = config_mod.load(env={"AMEESH_DRAIN_SECONDS": "-1",
                                   "AMEESH_CONFIG": "/nulle-part.json"})
        self.assertEqual(cfg.drain_seconds, 1800.0)


class DrainageTest(PgTestCase):
    def _lance(self, nom: str, **env):
        cwd = os.path.join(self.tmp, "work", nom)
        os.makedirs(cwd, exist_ok=True)
        self.register(nom, "claude", cwd=cwd, prompt="consigne du tour")
        proc = self.runner_popen("--agents", nom, "--poll", "1", env=self.env(**env))
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(10)))
        self.wait_for(lambda: len(self.turns()) == 1, timeout=30)  # le tour a démarré
        return proc

    def test_sigterm_laisse_finir_le_tour_en_cours(self):
        proc = self._lance("draine", AMEESH_DRAIN_SECONDS="120", AMEESH_TEST_SLEEP="4")
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        # une consigne arrivée pendant le drainage attend l'exécuteur suivant
        time.sleep(0.5)
        registry.set_pending_prompt(self.db, "draine", "consigne d'après")
        sortie, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("drainage", sortie)
        self.assertIn("drainage terminé", sortie)
        row = registry.get(self.db, "draine")
        self.assertEqual(int(row["turns"]), 1, sortie)  # le tour a abouti
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["pending_prompt"], "consigne d'après")
        self.assertEqual(len(self.turns()), 1)  # aucun nouveau tour

    def test_borne_du_drainage_arrete_le_tour(self):
        proc = self._lance("borne", AMEESH_DRAIN_SECONDS="1", AMEESH_TEST_SLEEP="60")
        debut = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        sortie, _ = proc.communicate(timeout=60)
        self.assertLess(time.monotonic() - debut, 30, sortie)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("borne", sortie)
        # tour arrêté (comme avant le drainage) : sa consigne repart en attente
        self.assertEqual(registry.get(self.db, "borne")["pending_prompt"], "consigne du tour")

    def test_second_signal_arrete_tout_de_suite(self):
        proc = self._lance("presse", AMEESH_DRAIN_SECONDS="120", AMEESH_TEST_SLEEP="60")
        proc.send_signal(signal.SIGTERM)
        time.sleep(1.0)
        debut = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        sortie, _ = proc.communicate(timeout=60)
        self.assertLess(time.monotonic() - debut, 30, sortie)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("second signal", sortie)
        self.assertEqual(registry.get(self.db, "presse")["pending_prompt"],
                         "consigne du tour")

    def test_le_drainage_attend_le_travail_de_fond(self):
        pidfile = os.path.join(self.tmp, "fond.pid")
        proc = self._lance("fond-drain", AMEESH_DRAIN_SECONDS="120",
                           AMEESH_TEST_BACKGROUND="sleep 8",
                           AMEESH_TEST_BACKGROUND_PID=pidfile)
        # le tour fini, son travail de fond tourne : le signal attend sa fin
        self.wait_for(lambda: registry.get(self.db, "fond-drain")["turns"] == 1,
                      timeout=30)
        debut = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        sortie, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertGreater(time.monotonic() - debut, 1.0, sortie)
        self.assertIn("drainage terminé", sortie)
        self.assertRegex(sortie, r"travail de fond du tour \w+ : terminé")


if __name__ == "__main__":
    unittest.main()

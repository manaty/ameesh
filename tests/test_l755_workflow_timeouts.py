# SPDX-License-Identifier: AGPL-3.0-only
"""Chaque job de CI porte un `timeout-minutes`, sous le plafond (#755).

Règle du bureau (manaty/home, `processus/lots.md` §3, 13/10/2026) : « Chaque job
de CI porte une durée maximale (`timeout-minutes`). Un job bloqué ne doit jamais
retenir un runner pendant des heures. » Le plafond retenu ici est 60 minutes :
la règle interdit des heures, pas une valeur précise, et le maximum en place à
l'ouverture du lot est 40 (`carte.yml`, job `mesure`).

PyYAML n'est pas une dépendance du projet : le venv de la CI n'installe que
psycopg, cryptography et psutil, et les tests qui lisent du YAML se sautent
quand il manque (`tests/test_cluster.py`). Ce module lit donc le sous-ensemble
des workflows à la main : la clé `jobs:` de premier niveau, un job par clé
indentée de deux espaces, et `timeout-minutes` du job à l'indentation quatre
exactement — une clé d'étape, plus profonde, ne compte pas. Le parseur est
testé sur un texte synthétique, clé d'étape comprise.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: La règle interdit « des heures » : une heure pleine est la borne garde-fou.
MAX_TIMEOUT_MINUTES = 60

#: Un job : clé indentée de 2, nom nu ou entre guillemets.
_JOB = re.compile(r"""^  (?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+)):\s*(?:#.*)?$""")
#: Le timeout du job : clé indentée de 4 (celle d'une étape est plus profonde).
_JOB_TIMEOUT = re.compile(r"""^    timeout-minutes:\s*["']?(\d+)["']?\s*(?:#.*)?$""")


def job_timeouts(text: str) -> dict[str, int | None]:
    """`timeout-minutes` de chaque job du workflow, `None` quand il manque."""
    timeouts: dict[str, int | None] = {}
    current: str | None = None
    in_jobs = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_jobs = line.split(":", 1)[0].strip() == "jobs"
            current = None
            continue
        if not in_jobs:
            continue
        if indent == 2:
            match = _JOB.match(line)
            current = next((name for name in match.groups() if name), None) if match else None
            if current is not None:
                timeouts[current] = None
            continue
        if indent == 4 and current is not None:
            match = _JOB_TIMEOUT.match(line)
            if match:
                timeouts[current] = int(match.group(1))
    return timeouts


class WorkflowTimeoutsTest(unittest.TestCase):
    def workflows(self) -> list[Path]:
        files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertTrue(files, "aucun workflow sous %s" % WORKFLOWS)
        return files

    def test_chaque_job_declare_son_timeout(self):
        for path in self.workflows():
            timeouts = job_timeouts(path.read_text(encoding="utf-8"))
            with self.subTest(workflow=path.name):
                self.assertTrue(timeouts, "aucun job lu dans %s" % path.name)
            for job, value in timeouts.items():
                with self.subTest(workflow=path.name, job=job):
                    self.assertIsNotNone(
                        value, "%s/%s : timeout-minutes manquant" % (path.name, job))

    def test_le_plafond_est_respecte(self):
        for path in self.workflows():
            for job, value in job_timeouts(path.read_text(encoding="utf-8")).items():
                if value is None:
                    continue  # déjà signalé par test_chaque_job_declare_son_timeout
                with self.subTest(workflow=path.name, job=job):
                    self.assertLessEqual(
                        value, MAX_TIMEOUT_MINUTES,
                        "%s/%s : %s min, plafond %s" % (path.name, job, value,
                                                        MAX_TIMEOUT_MINUTES))

    def test_le_parseur_lit_le_timeout_du_job_pas_de_l_etape(self):
        text = (
            "name: ci\n"
            "on:\n"
            "  push:\n"
            "jobs:\n"
            "  a:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: sleep 1\n"
            "        timeout-minutes: 9\n"
            "  b:\n"
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 10  # plafond\n"
            '  "c:d":\n'
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 5\n"
            "  d:\n"
            "    runs-on: ubuntu-latest\n"
        )
        self.assertEqual(job_timeouts(text), {"a": None, "b": 10, "c:d": 5, "d": None})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

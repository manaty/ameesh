# SPDX-License-Identifier: AGPL-3.0-only
"""Chaque job de CI porte un `timeout-minutes`, sous le plafond (#755).

Règle du bureau (manaty/home, `processus/lots.md` §3, 13/10/2026) : « Chaque job
de CI porte une durée maximale (`timeout-minutes`). Un job bloqué ne doit jamais
retenir un runner pendant des heures. » Le plafond retenu ici est 60 minutes :
la règle interdit des heures, pas une valeur précise, et le maximum en place à
l'ouverture du lot est 40 (`carte.yml`, job `mesure`).

#1410 : GitHub refuse `timeout-minutes` sur un job qui appelle un workflow
réutilisable (`uses:`), la clé n'existe que pour un job à étapes. Ces jobs
portent donc leur borne dans le workflow appelé : celui-ci doit être local et
chacun de ses jobs doit déclarer son `timeout-minutes` (vérifié ici).

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
#: L'appel d'un workflow réutilisable : clé de job, indentée de 4.
_JOB_USES = re.compile(r"""^    uses:\s*["']?([^"'#\s]+)["']?\s*(?:#.*)?$""")


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


def caller_jobs(text: str) -> dict[str, str]:
    """Les jobs qui appellent un workflow réutilisable : job → cible `uses:`.

    GitHub refuse `timeout-minutes` sur ces jobs ; leur borne est portée par
    les jobs du workflow appelé, que `WorkflowTimeoutsTest` vérifie.
    """
    appels: dict[str, str] = {}
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
            continue
        if indent == 4 and current is not None:
            match = _JOB_USES.match(line)
            if match:
                appels[current] = match.group(1)
    return appels


class WorkflowTimeoutsTest(unittest.TestCase):
    def workflows(self) -> list[Path]:
        files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertTrue(files, "aucun workflow sous %s" % WORKFLOWS)
        return files

    def test_chaque_job_declare_son_timeout(self):
        for path in self.workflows():
            text = path.read_text(encoding="utf-8")
            appeles = caller_jobs(text)
            timeouts = job_timeouts(text)
            with self.subTest(workflow=path.name):
                self.assertTrue(timeouts, "aucun job lu dans %s" % path.name)
            for job, value in timeouts.items():
                if job in appeles:
                    continue  # #1410 : `uses:` refuse timeout-minutes ; voir le test voisin
                with self.subTest(workflow=path.name, job=job):
                    self.assertIsNotNone(
                        value, "%s/%s : timeout-minutes manquant" % (path.name, job))

    def test_un_job_appele_est_borne_par_les_jobs_de_l_appele(self):
        # #1410 : la borne d'un job `uses:` est celle des jobs du workflow
        # appelé ; l'appel doit rester local et chaque job appelé être borné.
        for path in self.workflows():
            for job, cible in caller_jobs(path.read_text(encoding="utf-8")).items():
                with self.subTest(workflow=path.name, job=job):
                    self.assertTrue(cible.startswith("./.github/workflows/"),
                                    "%s/%s : appel non local (%s)" % (path.name, job, cible))
                    appele = ROOT / cible[2:]
                    self.assertTrue(appele.exists(),
                                    "%s/%s : workflow appelé absent (%s)" % (path.name, job, cible))
                    timeouts = job_timeouts(appele.read_text(encoding="utf-8"))
                    self.assertTrue(timeouts, "aucun job lu dans %s" % appele.name)
                    for nom, valeur in timeouts.items():
                        self.assertIsNotNone(
                            valeur, "%s (appelé par %s/%s) : timeout-minutes manquant"
                            % (nom, path.name, job))

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

    def test_le_parseur_reconnait_un_appel_de_workflow(self):
        text = (
            "name: ci\n"
            "on:\n"
            "  push:\n"
            "jobs:\n"
            "  a:\n"
            "    uses: ./.github/workflows/appele.yml\n"
            "  b:\n"
            "    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 5\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        self.assertEqual(caller_jobs(text), {"a": "./.github/workflows/appele.yml"})
        self.assertEqual(job_timeouts(text), {"a": None, "b": 5})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


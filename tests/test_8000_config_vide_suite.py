# SPDX-License-Identifier: AGPL-3.0-only
"""#8000 — la suite ne lit ni la config ni le canon de l'hôte.

Trois lots de cause racine en 24 h (#7970, #7996, #7999) pour la même classe :
verts en CI, rouges sur AG1/AG3. `tests.support` posait déjà une configuration
vide, mais un module qui ne l'importait pas — `test_5729` et son appel indirect
`scripts/test-plan.py:make_plan` — lisait `~/.config/ameesh/config.json` et ses
canons, dont `contract_selection` pour ameesh.

`tests/__init__.py` pose désormais la configuration vide avant tout module de
test et retire le canon de la session d'exécution ; `support.EMPTY_CONFIG` la
réexporte. Ce module verrouille la classe :

* la config vide est posée AVANT le premier module, y compris pour un module
  qui n'importe pas `support` ;
* un hôte simulé (HOME peuplé, canon Git actif déclarant `contract_selection`)
  ne change le résultat d'aucun module ciblé — les mêmes tests, mêmes verdicts
  que sur un hôte sans configuration ;
* l'appel indirect `make_plan` sans `contract_policy` ne voit aucune politique ;
* les rares témoins qui veulent une configuration réelle ou absente la
  demandent explicitement (`AMEESH_CONFIG=…`, `support.env_without_config()`),
  jamais par héritage du poste. `test_1127_config_explicite`,
  `test_1466_config_profil` et `test_1466_relecture_config` en font partie et
  restent verts sous l'hôte simulé.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

from . import support
from .test_l1616_mechanics import FEDERATION, MECHANICS, _Repo

REPO = pathlib.Path(support.REPO)

#: Les modules qui portent la classe : prévalidation et plan (politique de
#: contrats, appel direct et indirect), puis les témoins de configuration
#: réelle/absente. Tous sans base, donc rapides ; le reste de la suite est
#: couvert par la suite complète sur AG1 (canon actif).
CIBLES = (
    "tests.test_1770_prevalidation_cli",
    "tests.test_1820_plan_df_conflict",
    "tests.test_4071_prevalidation_guest",
    "tests.test_5729_qualified_tree",
    "tests.test_1127_config_explicite",
    "tests.test_1466_config_profil",
    "tests.test_1466_relecture_config",
)

#: Le canon de l'hôte simulé : celui d'AG1/AG3, qui déclare une politique de
#: contrats pour ameesh. Le `projects: {}` du gabarit est remplacé, pas étendu
#: par un second bloc (le lecteur refuse un doublon).
CANON_HOST = MECHANICS.replace(
    "projects: {}",
    "projects:\n  ameesh:\n    contract_selection:\n"
    "      registry: tests/parts/contrats.json\n")

#: Ce que voit un interpréteur neuf après `import tests` : la config vide du
#: paquet, aucun canon, et `make_plan` sans politique. Le module indirect est
#: `test_5729`, qui n'importe pas `tests.support` : c'est la classe d'origine.
PROBE = r"""
import datetime, json, os, sys
import tests
from ameesh import config, contract_plan
from tests import test_5729_qualified_tree as indirect

cfg = config.load(register_credentials=False)
plan = indirect.test_plan.make_plan(
    "push", None,
    datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc),
    head="HEAD", repo=indirect.REPO, base="HEAD",
    ref="refs/heads/release/1.2", mode="auto")
print(json.dumps({
    "config_path": cfg.config_path,
    "canon": cfg.canon,
    "project": cfg.project,
    "humans": cfg.humans,
    "host": cfg.host,
    "policy": contract_plan.configured_policy("ameesh"),
    "plan_active": plan["contract_plan"]["active"],
    "plan_digest": plan["contract_plan"]["registry_digest"],
    "env_config": os.environ.get("AMEESH_CONFIG"),
    "env_canon": os.environ.get("AMEESH_CANON"),
    "empty_config": tests.EMPTY_CONFIG,
    "support_imported": "tests.support" in sys.modules,
}))
"""


class HostSimule:
    """Un hôte d'agents : HOME peuplé et canon Git actif (AG1/AG3)."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ameesh-test-8000-")
        root = pathlib.Path(self._tmp.name)
        self.host_home = root / "home"
        self.clean_home = root / "home-clean"
        (self.host_home / ".config" / "ameesh").mkdir(parents=True)
        self.clean_home.mkdir()
        self.canon = root / "canon"
        self.canon.mkdir()
        _Repo(str(self.canon), mechanics_text=CANON_HOST,
              federation_text=FEDERATION + "members: []\n")
        self.config = self.host_home / ".config" / "ameesh" / "config.json"
        self.config.write_text(json.dumps({
            "host": "hote-simule",
            "project": "ameesh",
            "humans": ["simule"],
            "canons": [{"path": str(self.canon), "ref": "HEAD"}],
        }), encoding="utf-8")

    def close(self) -> None:
        self._tmp.cleanup()

    def env(self, *, poisoned: bool) -> dict:
        """L'environnement d'un sous-processus, hôte simulé ou hôte nu.

        La config de l'hôte est nommée par `AMEESH_CONFIG` ET posée dans un
        HOME par défaut : les deux chemins de lecture sont couverts. L'hôte nu
        retire les variables de config, comme une CI neuve."""
        env = support.child_env(
            HOME=str(self.host_home if poisoned else self.clean_home))
        # l'identité de session de l'hôte ne doit pas masquer la config lue :
        # c'est le fichier (poisoned) ou le défaut (nu) qui parle.
        for name in ("AMEESH_HOST", "AGENT_MESH_HOST",
                     "AMEESH_STATE", "AGENT_MESH_STATE"):
            env.pop(name, None)
        if poisoned:
            env.update({
                "AMEESH_CONFIG": str(self.config),
                "AGENT_MESH_CONFIG": str(self.config),
                "AMEESH_CANON": str(self.canon),
                "AMEESH_CANON_REF": "HEAD",
                "AGENT_MESH_CANON": str(self.canon),
                "AGENT_MESH_CANON_REF": "HEAD",
            })
        else:
            env.pop("AMEESH_CONFIG", None)
            env.pop("AGENT_MESH_CONFIG", None)
        return env


class ConfigVideSuiteTest(unittest.TestCase):
    """#8000 : la config vide du paquet tient avant tout module de test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.host = HostSimule()
        cls.addClassCleanup(cls.host.close)

    def _run(self, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "unittest", "-q", *CIBLES],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            timeout=support.scaled(300))

    def test_le_paquet_pose_la_config_vide_et_reexporte_son_chemin(self):
        import tests

        self.assertEqual(os.environ.get("AMEESH_CONFIG"), support.EMPTY_CONFIG)
        self.assertEqual(tests.EMPTY_CONFIG, support.EMPTY_CONFIG)
        self.assertTrue(os.path.isfile(support.EMPTY_CONFIG))
        with open(support.EMPTY_CONFIG, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {})
        for name in ("AMEESH_CANON", "AMEESH_CANONS", "AMEESH_CANON_REF",
                     "AGENT_MESH_CANON", "AGENT_MESH_CANON_REF"):
            self.assertNotIn(name, os.environ)

    def test_un_module_sans_support_ne_lit_pas_le_canon_de_l_hote(self):
        proc = subprocess.run(
            [sys.executable, "-c", PROBE], cwd=str(REPO),
            env=self.host.env(poisoned=True), capture_output=True, text=True,
            timeout=support.scaled(120))
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        seen = json.loads(proc.stdout.strip().splitlines()[-1])
        # le module indirect n'importe pas le socle : c'est bien la classe
        self.assertFalse(seen["support_imported"])
        # la config vide du paquet a remplacé celle de l'hôte, avant le module
        self.assertEqual(seen["config_path"], seen["empty_config"])
        self.assertEqual(seen["env_config"], seen["empty_config"])
        self.assertIsNone(seen["env_canon"])
        self.assertFalse(seen["canon"])
        self.assertEqual(seen["project"], "")
        self.assertEqual(seen["humans"], "")
        self.assertNotEqual(seen["host"], "hote-simule")
        # l'appel indirect (make_plan, défaut `configured`) ne voit rien
        self.assertIsNone(seen["policy"])
        self.assertFalse(seen["plan_active"])
        self.assertIsNone(seen["plan_digest"])

    def test_hote_simule_ne_change_aucun_module_cible(self):
        nu = self._run(self.host.env(poisoned=False))
        simule = self._run(self.host.env(poisoned=True))
        self.assertEqual(nu.returncode, 0, nu.stderr[-6000:])
        self.assertEqual(simule.returncode, 0, simule.stderr[-6000:])
        compte = []
        for proc in (nu, simule):
            found = re.search(r"^Ran (\d+) tests?", proc.stderr, re.M)
            self.assertIsNotNone(found, proc.stderr[-2000:])
            compte.append(int(found.group(1)))
        self.assertEqual(compte[0], compte[1],
                         "l'hôte simulé a changé le nombre de tests exécutés")

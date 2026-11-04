# SPDX-License-Identifier: AGPL-3.0-only
"""#5737 : les unités systemd n'appellent que des lanceurs d'un stable NEUF.

`_installer_lanceurs` (bascule.py) écrit toujours `python` et `ameesh`, les
scripts console de la version activée, et un `pythonN` seulement s'il existait
déjà dans `bin/`. Une unité qui appelle `venv/bin/python3` échoue donc en
203/EXEC sur un stable neuf (constat du bureau, #189783). Banc isolé : un
stable vide dans un dossier temporaire, aucun hôte, aucune base.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy/systemd"
SPEC = importlib.util.spec_from_file_location(
    "bascule_5737", ROOT / "deploy/mise-a-jour/bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)

#: tout appel au venv stable d'une unité, avec ou sans %h
APPEL_VENV = re.compile(r"\.local/share/ameesh/venv/bin/([^\s/;'\"]+)")


def lanceurs_stable_neuf() -> set[str]:
    """Les noms de `bin/` après `_installer_lanceurs` sur un stable neuf."""
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    with tempfile.TemporaryDirectory() as tmp:
        venv = os.path.join(tmp, "venv")
        version = "neuf"
        cible = Path(bascule._version_dir(venv, version)) / "bin"
        cible.mkdir(parents=True)
        # la version activée porte ses interpréteurs et ses scripts console
        for nom in ["python", "python3", "python3.12", *scripts]:
            chemin = cible / nom
            chemin.write_text("#!%s\n" % (cible / "python"))
            chemin.chmod(0o755)
        os.makedirs(os.path.join(venv, "bin"))
        assert not any(re.fullmatch(r"python[0-9.]+", n)
                       for n in os.listdir(os.path.join(venv, "bin")))
        bascule._installer_lanceurs(venv, version)
        return set(os.listdir(os.path.join(venv, "bin")))


def appels_unites() -> list[tuple[str, str]]:
    appels = []
    for chemin in sorted(SYSTEMD.rglob("*")):
        if not chemin.is_file():
            continue
        for ligne in chemin.read_text().splitlines():
            if ligne.lstrip().startswith("#"):
                continue
            for nom in APPEL_VENV.findall(ligne):
                appels.append((str(chemin.relative_to(ROOT)), nom))
    return appels


class LanceursStableNeufTest(unittest.TestCase):
    def test_stable_neuf_sans_python3(self):
        produits = lanceurs_stable_neuf()
        self.assertIn("python", produits)
        self.assertIn("ameesh", produits)
        self.assertNotIn("python3", produits)

    def test_unites_appellent_des_lanceurs_garantis(self):
        produits = lanceurs_stable_neuf()
        appels = appels_unites()
        # le témoin couvre bien les unités du dépôt (notify, staging…)
        unites = {u for u, _ in appels}
        self.assertIn("deploy/systemd/ameesh-staging.service", unites)
        self.assertIn("deploy/systemd/ameesh-notify.service", unites)
        manquants = [(u, n) for u, n in appels if n not in produits]
        self.assertEqual(manquants, [], "lanceurs absents d'un stable neuf")

    def test_staging_appelle_python(self):
        texte = (SYSTEMD / "ameesh-staging.service").read_text()
        self.assertIn("/venv/bin/python -m ameesh.staging_executor", texte)


if __name__ == "__main__":
    unittest.main()

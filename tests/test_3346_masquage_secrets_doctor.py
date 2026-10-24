# SPDX-License-Identifier: AGPL-3.0-only
"""#3346 : `ameesh doctor` ne montre jamais la valeur d'une feuille secrète.

#3345 affiche les divergences de profil sur les clés partagées (`train`,
`github`, `release`) ; une valeur comme `github.token` s'y retrouvait en
clair. `_shown` masque toute feuille dont le nom ressemble à un secret
(token, secret, key, password, dsn), des deux côtés de la comparaison : le
constat nomme le profil, la clé et la feuille, jamais le jeton.

Témoin principal : un profil qui change `github.token` — le jeton du profil
et celui de `config.json` n'apparaissent dans aucune ligne de `doctor`.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import cli, config

SECRET_BASE = "ghp_base_0123456789abcdefghijklmnop"
SECRET_PROFIL = "ghp_profil_0123456789abcdefghijklmnop"

BASE = {
    "host": "pc-smichea",
    "train": {
        "schema": "ameesh-train-host/1",
        "host": "pc-smichea",
        "mode": "active",
        "projects": {
            "ameesh": {"repo": "/home/ameesh/work/ameesh", "target": "main"},
        },
    },
    "github": {
        "host": "pc-smichea",
        "token": SECRET_BASE,
        "projects": {"ameesh": {"repo": "manaty/ameesh"}},
    },
    "release": {
        "host": "pc-smichea",
        "projects": {"ameesh": {"repo": "manaty/ameesh", "target": "main"}},
    },
}


class Banc(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-test-3346-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.cfgdir = os.path.join(self.home, ".config", "ameesh")
        self.host = os.path.join(self.cfgdir, "config.json")
        self.profil_dir = os.path.join(self.cfgdir, "runner-profiles")
        os.makedirs(self.profil_dir)
        self._write(self.host, BASE)
        self.critiques = os.path.join(self.profil_dir, "critiques.json")

    def _write(self, path: str, payload: dict) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def load(self, path: str):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return config.load(env={"AMEESH_CONFIG": path})

    def audit(self, **kw):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return config.audit_profiles(**kw)

    def _doctor(self, cfg, **kw) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.home}), \
                redirect_stdout(out):
            code = cli.cmd_doctor(cfg, notify_test=False, **kw)
        return code, out.getvalue()


class ShownTest(unittest.TestCase):
    def test_les_noms_de_feuilles_secretes_sont_masques(self):
        for path in ("github.token", "github.TOKEN", "github.secret",
                     "github.api_key", "github.private_key", "train.password",
                     "github.pass", "github.passwd", "release.pwd",
                     "train.projects.ameesh.dsn", "github.token.value"):
            with self.subTest(path=path):
                self.assertEqual(config._shown("valeur-en-clair", path),
                                 config.MASKED_SECRET)

    def test_les_autres_feuilles_restent_lisibles(self):
        for path, value in (
                ("train.mode", "active"),
                ("train.projects.ameesh.repo", "/home/ameesh/work/ameesh"),
                ("github.projects.ameesh.repo", "manaty/ameesh"),
                ("release.host", "pc-smichea")):
            with self.subTest(path=path):
                self.assertEqual(config._shown(value, path),
                                 json.dumps(value, ensure_ascii=False, sort_keys=True))

    def test_une_feuille_absente_reste_absente_meme_secrete(self):
        """L'absence n'est pas une valeur : elle ne se masque pas, et le constat
        dit des deux côtés lequel porte le jeton."""
        self.assertEqual(config._shown(config._MISSING, "github.token"), "<absent>")


class AuditTest(Banc):
    def _ecart_github_token(self) -> str:
        self._write(self.critiques, {"github": {
            "host": "pc-smichea",
            "token": SECRET_PROFIL,
            "projects": BASE["github"]["projects"],
        }})
        audit = self.audit()
        self.assertFalse(audit.ok)
        self.assertEqual([d.path for d in audit.divergences], ["github.token"])
        return "\n".join(audit.lines())

    def test_le_constat_nomme_la_feuille_et_masque_les_deux_jetons(self):
        lignes = self._ecart_github_token()
        self.assertIn("critiques.json: github.token", lignes)
        self.assertIn(config.MASKED_SECRET, lignes)
        self.assertNotIn(SECRET_BASE, lignes)
        self.assertNotIn(SECRET_PROFIL, lignes)

    def test_doctor_profiles_ne_montre_pas_le_jeton_github(self):
        """Témoin du lot : la sortie réelle de `doctor --profiles`, le garde du
        déploiement, ne contient aucun des deux jetons."""
        self._ecart_github_token()
        cfg = self.load(self.host)
        code, text = self._doctor(cfg, profiles=True)
        self.assertEqual(code, 1)
        self.assertIn("DIVERGENCE", text)
        self.assertIn("github.token", text)
        self.assertIn(config.MASKED_SECRET, text)
        self.assertNotIn(SECRET_BASE, text)
        self.assertNotIn(SECRET_PROFIL, text)

    def test_un_jeton_absent_du_profil_ne_montre_que_le_cote_config(self):
        self._write(self.critiques, {"github": {
            "host": "pc-smichea",
            "projects": BASE["github"]["projects"],
        }})
        lignes = "\n".join(self.audit().lines())
        self.assertIn("github.token", lignes)
        self.assertIn("profile <absent>", lignes)
        self.assertIn("config.json %s" % config.MASKED_SECRET, lignes)
        self.assertNotIn(SECRET_BASE, lignes)

    def test_un_sous_objet_ajoute_ne_laisse_pas_fuir_son_jeton(self):
        """avis dev7 : `_config_diff` rend le sous-arbre `github.auth` entier ;
        son `token` imbriqué ne doit pas sortir en clair."""
        secret = "ghp_sous_objet_0123456789"
        self._write(self.critiques, {"github": {
            "host": "pc-smichea",
            "projects": BASE["github"]["projects"],
            "auth": {"token": secret},
        }})
        audit = self.audit()
        self.assertIn("github.auth", [d.path for d in audit.divergences])
        lignes = "\n".join(audit.lines())
        self.assertIn('"token": "%s"' % config.MASKED_SECRET, lignes)
        self.assertNotIn(secret, lignes)

    def test_un_github_absent_de_la_base_ne_laisse_pas_fuir_son_jeton(self):
        """avis cx1 : `github` ajouté en entier (clé absente de la base) est
        rendu comme un conteneur, pas feuille par feuille."""
        self._write(self.host, {k: v for k, v in BASE.items() if k != "github"})
        secret = "FAKE_SECRET_3346"
        self._write(self.critiques, {"github": {"token": secret}})
        audit = self.audit()
        self.assertEqual([d.path for d in audit.divergences], ["github"])
        lignes = "\n".join(audit.lines())
        self.assertIn("github", lignes)
        self.assertIn('"token": "%s"' % config.MASKED_SECRET, lignes)
        self.assertNotIn(secret, lignes)

    def test_un_github_remplace_par_null_ne_laisse_pas_fuir_le_jeton(self):
        """avis cx1 : un changement de type rend le conteneur de la base d'un
        bloc ; le jeton qu'il porte est masqué."""
        self._write(self.critiques, {"github": None})
        audit = self.audit()
        self.assertEqual([d.path for d in audit.divergences], ["github"])
        lignes = "\n".join(audit.lines())
        self.assertIn("github", lignes)
        self.assertIn('"token": "%s"' % config.MASKED_SECRET, lignes)
        self.assertNotIn(SECRET_BASE, lignes)

    def test_une_liste_d_objets_ne_laisse_pas_fuir_ses_jetons(self):
        """avis cx1 : une liste rendue en bloc est parcourue, ses objets aussi."""
        secret = "ghp_liste_0123456789"
        self._write(self.critiques, {"github": {
            "host": "pc-smichea",
            "projects": BASE["github"]["projects"],
            "hooks": [{"name": "a", "secret": secret}],
        }})
        audit = self.audit()
        self.assertIn("github.hooks", [d.path for d in audit.divergences])
        lignes = "\n".join(audit.lines())
        self.assertIn('"secret": "%s"' % config.MASKED_SECRET, lignes)
        self.assertNotIn(secret, lignes)

    def test_un_ecart_non_secret_reste_lisible(self):
        """Le masquage ne cache pas les feuilles ordinaires : `doctor` continue
        de nommer la valeur attendue et la valeur du profil."""
        self._write(self.critiques, {"train": {"mode": "shadow"}})
        lignes = "\n".join(self.audit().lines())
        self.assertIn("train.mode", lignes)
        self.assertIn('"shadow"', lignes)
        self.assertIn('"active"', lignes)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""#3345 : un profil de runner ne porte que ce qu'il surcharge, et une
divergence avec `config.json` sur les clés partagées n'est plus silencieuse.

Constat du bureau du 24/10 13:25Z (#133146) : les trois runners critiques
(orchestrateur, mesh-design, auditeur) lisaient `runner-profiles/critiques.json`
resté en `train.mode: shadow` et sans clé `github`, alors que `config.json`
était passé en train actif. La copie complète avait figé les clés partagées
(`train`, `github`, `release`) ; `ameesh doctor`, lancé sur `config.json`, ne
pouvait pas voir la divergence des autres profils.

Témoins :
* un profil qui ne surcharge que `train.mode` hérite du `host` et des
  `projects` de `config.json` : la fusion récursive est réservée à `train` et
  `release` (#133247) ;
* tout autre objet déclaré (`github`, `radar`, `packet_watch`, `notify`,
  `accounts`…) remplace celui de l'hôte en entier — un `notify: {}` vide bien
  les routes héritées ;
* `ameesh doctor` audite tous les profils de l'hôte et rend KO sur une
  divergence, en nommant le profil et la feuille ;
* `ameesh doctor --profiles`, sans base, est le garde des contrôles de
  déploiement (`deploy/mise-a-jour/vm.sh`, `poste.sh`).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ameesh import cli, config
from ameesh import db as db_mod

ROOT = Path(__file__).resolve().parents[1]

BASE = {
    "host": "pc-smichea",
    "budget_usd_per_hour": 100,
    "accounts": {"deepseek": [{"name": "cle-a", "hourly_usd": 100}]},
    "train": {
        "schema": "ameesh-train-host/1",
        "host": "pc-smichea",
        "mode": "active",
        "projects": {
            "ameesh": {
                "repo": "/home/ameesh/work/ameesh",
                "target": "main",
                "github": "manaty/ameesh",
            },
            "nexlink": {
                "repo": "/home/ameesh/work/nexlink",
                "target": "develop",
                "github": "manaty/nexlink",
            },
        },
    },
    "github": {
        "host": "pc-smichea",
        "projects": {
            "ameesh": {"repo": "manaty/ameesh", "exclude_terms": ["brouillon"]},
            "nexlink": {"repo": "manaty/nexlink"},
        },
    },
    "release": {
        "host": "pc-smichea",
        "projects": {"ameesh": {"repo": "manaty/ameesh", "target": "main"}},
    },
    "notify": {"types": ["decision_pending", "credential_failing"]},
    "harness_bins": {"claude": "/usr/bin/claude"},
}


class Banc(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-test-3345-")
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

class ProfilTest(Banc):
    # -- (1) la fusion au chargement -------------------------------------

    def test_un_profil_ne_porte_que_le_mode_et_herite_du_reste_du_train(self):
        """Le témoin du lot : `{"train": {"mode": "shadow"}}` ne recopie pas
        le bloc `train`, et le profil reste lisible (`overrides: train`)."""
        self._write(self.critiques, {"train": {"mode": "shadow"}})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.train["mode"], "shadow")
        self.assertEqual(cfg.train["host"], "pc-smichea")
        self.assertEqual(cfg.train["schema"], "ameesh-train-host/1")
        self.assertEqual(sorted(cfg.train["projects"]), ["ameesh", "nexlink"])
        self.assertEqual(cfg.train["projects"]["ameesh"]["github"], "manaty/ameesh")
        # une clé partagée absente du profil vient bien de config.json
        self.assertEqual(cfg.github, BASE["github"])
        self.assertEqual(cfg.release, BASE["release"])
        self.assertEqual(cfg.config_shadowed, ("train",))
        self.assertIn("overrides: train", cfg.config_source)

    def test_un_profil_sans_cle_github_herite_celle_de_config_json(self):
        self._write(self.critiques, {"host": "pc-smichea"})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.github, BASE["github"])

    def test_un_objet_hors_train_release_est_remplace_en_entier(self):
        """Décision #133247 : la récursion est réservée à `train` et
        `release`. Un `github` déclaré par le profil remplace celui de l'hôte,
        il n'en hérite aucune sous-clé."""
        self._write(self.critiques, {
            "github": {"projects": {"ameesh": {"exclude_terms": ["wip"]}}},
        })
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.github,
                         {"projects": {"ameesh": {"exclude_terms": ["wip"]}}})
        self.assertNotIn("host", cfg.github)
        self.assertNotIn("nexlink", cfg.github["projects"])

    def test_release_se_superpose_feuille_a_feuille(self):
        """Avec `train`, `release` reste le seul objet récursif : surcharger
        son hôte garde ses `projects`."""
        self._write(self.critiques, {"release": {"host": "autre-hote"}})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.release["host"], "autre-hote")
        self.assertEqual(cfg.release["projects"], BASE["release"]["projects"])

    def test_une_liste_declaree_remplace_la_base_au_lieu_de_s_y_ajouter(self):
        self._write(self.critiques, {"notify": {"types": ["account_shared"]}})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.notify, {"types": ["account_shared"]})
        # les autres réglages de l'hôte restent hérités
        self.assertEqual(cfg.harness_bins, {"claude": "/usr/bin/claude"})

    def test_un_notify_vide_remplace_la_base_au_lieu_de_l_heriter(self):
        """`notify: {}` doit vider les routes héritées, pas les conserver :
        la récursion générale de la première version de #3345 avait introduit
        cette régression (cx1)."""
        self._write(self.critiques, {"notify": {}})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.notify, {})
        self.assertEqual(cfg.config_shadowed, ("notify",))

    def test_les_comptes_restent_une_unite_d_isolation(self):
        """#1466 : un profil qui declare ses comptes les remplace, il n'herite
        pas des autres harnais de l'hote (quota et sessions partages)."""
        self._write(self.critiques, {"accounts": {"codex": [{"name": "primaire"}]}})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.accounts, {"codex": [{"name": "primaire"}]})
        self.assertEqual(cfg.config_shadowed, ("accounts",))

    def test_un_profil_fidele_n_ecrase_rien(self):
        """Déclarer une valeur égale à la base ne crée pas d'écart : le profil
        peut être une superposition sans bruit."""
        self._write(self.critiques, {"host": "pc-smichea",
                                     "train": {"mode": "active"}})
        cfg = self.load(self.critiques)
        self.assertEqual(cfg.config_shadowed, ())

    def test_un_radar_restreint_ne_relit_pas_les_entrees_de_l_hote(self):
        """Un profil qui restreint `radar` à un sous-ensemble ne doit pas
        hériter des entrées de l'hôte (régression relevée par l'auditeur)."""
        base = dict(BASE, radar={"projects": {"ameesh": {"migrations": "migrations"}}})
        self._write(self.host, base)
        self._write(self.critiques, {"radar": {"projects": {"nexlink": {"repo": "/n"}}}})
        cfg = self.load(self.critiques)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            relu = config.fresh_radar(cfg)
        self.assertEqual(relu.radar, {"projects": {"nexlink": {"repo": "/n"}}})

    # -- (2) l'audit des clés partagées ----------------------------------

    def test_l_audit_nomme_la_feuille_divergente(self):
        self._write(self.critiques, {"train": {"mode": "shadow"}})
        audit = self.audit()
        self.assertFalse(audit.ok)
        self.assertEqual(audit.profiles, (self.critiques,))
        self.assertEqual(len(audit.divergences), 1)
        divergence = audit.divergences[0]
        self.assertEqual(divergence.path, "train.mode")
        self.assertEqual(divergence.value, "shadow")
        self.assertEqual(divergence.base, "active")
        lignes = "\n".join(audit.lines())
        self.assertIn("DIVERGENCE", lignes)
        self.assertIn("critiques.json: train.mode", lignes)
        self.assertIn('"shadow"', lignes)
        self.assertIn('"active"', lignes)

    def test_un_profil_qui_ne_touche_que_les_comptes_ne_diverge_pas(self):
        self._write(self.critiques, {"accounts": {"deepseek": [{"name": "cle-b"}]}})
        audit = self.audit()
        self.assertTrue(audit.ok)
        self.assertEqual(audit.divergences, ())
        self.assertIn("train, github, release match", "\n".join(audit.lines()))

    def test_une_divergence_github_et_release_est_signalee(self):
        self._write(self.critiques, {
            "github": {"projects": {"ameesh": {"repo": "autres/ameesh"}}},
            "release": {"host": "autre-hote"},
        })
        chemins = sorted(d.path for d in self.audit().divergences)
        self.assertIn("github.projects.ameesh.repo", chemins)
        self.assertIn("release.host", chemins)

    def test_un_profil_autonome_n_est_pas_compare(self):
        self._write(self.critiques, {"inherit": False, "train": {"mode": "shadow"}})
        audit = self.audit()
        self.assertTrue(audit.ok)
        self.assertEqual(audit.profiles, ())

    def test_un_profil_herite_d_une_autre_base_n_est_pas_compare(self):
        autre = os.path.join(self.tmp, "autre.json")
        self._write(autre, {"train": {"mode": "shadow"}})
        self._write(self.critiques, {"inherit": autre})
        audit = self.audit()
        self.assertTrue(audit.ok)
        self.assertEqual(audit.profiles, ())

    def test_un_profil_illisible_est_signale_sans_casser_l_audit(self):
        with open(self.critiques, "w", encoding="utf-8") as fh:
            fh.write("{ pas du json")
        self._write(os.path.join(self.profil_dir, "sain.json"), {"host": "pc-smichea"})
        audit = self.audit()
        self.assertFalse(audit.ok)
        self.assertEqual(len(audit.errors), 1)
        self.assertIn("critiques.json", audit.errors[0])
        # le profil sain reste audité
        self.assertEqual(len(audit.profiles), 1)

    def test_le_profil_du_processus_hors_du_dossier_est_audite(self):
        """`ameesh doctor` lancé sur un profil `"inherit": true` rangé ailleurs
        doit voir sa divergence (c'est le processus courant)."""
        ailleurs = os.path.join(self.tmp, "profil-externe.json")
        self._write(ailleurs, {"inherit": True, "train": {"mode": "shadow"}})
        cfg = self.load(ailleurs)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            audit = config.audit_profiles(extra=(cfg.config_path,))
        self.assertFalse(audit.ok)
        self.assertEqual(audit.divergences[0].path, "train.mode")

    def test_dossier_absent_ou_vide_reste_vert(self):
        shutil.rmtree(self.profil_dir)
        self.assertTrue(self.audit().ok)
        os.makedirs(self.profil_dir)
        self.assertTrue(self.audit().ok)
        self.assertEqual(len(self.audit().profiles), 0)


class DoctorTest(Banc):
    def _doctor(self, cfg, **kw) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.home}), \
                redirect_stdout(out):
            code = cli.cmd_doctor(cfg, notify_test=False, **kw)
        return code, out.getvalue()

    def test_doctor_profiles_signale_la_divergence_sans_base(self):
        self._write(self.critiques, {"train": {"mode": "shadow"}})
        cfg = self.load(self.critiques)
        with mock.patch.object(cli.db_mod, "connect",
                               side_effect=AssertionError("`--profiles` must not touch the DB")):
            code, text = self._doctor(cfg, profiles=True)
        self.assertEqual(code, 1)
        self.assertIn("DIVERGENCE", text)
        self.assertIn("critiques.json: train.mode", text)
        self.assertIn("train, github, release", text)

    def test_doctor_profiles_vert_quand_les_profils_s_accordent(self):
        self._write(self.critiques, {"accounts": {"deepseek": [{"name": "cle-b"}]}})
        cfg = self.load(self.critiques)
        code, text = self._doctor(cfg, profiles=True)
        self.assertEqual(code, 0)
        self.assertIn("1 checked", text)
        self.assertIn("match", text)

    def test_doctor_complet_audite_les_autres_profils_meme_sur_config_json(self):
        """Le cas du 24/10 : doctor lancé sur `config.json` voyait un hôte vert
        alors que les runners critiques lisaient un autre profil."""
        self._write(self.critiques, {"train": {"mode": "shadow"}})
        cfg = self.load(self.host)
        with mock.patch.object(cli.db_mod, "connect",
                               side_effect=db_mod.Unavailable("offline")):
            _, text = self._doctor(cfg)
        self.assertIn("config     : %s" % self.host, text)
        self.assertIn("DIVERGENCE", text)
        self.assertIn("critiques.json: train.mode", text)

    def test_doctor_complet_reste_vert_sur_des_profils_fideles(self):
        self._write(self.critiques, {"accounts": {"deepseek": [{"name": "cle-b"}]}})
        cfg = self.load(self.host)
        with mock.patch.object(cli.db_mod, "connect",
                               side_effect=db_mod.Unavailable("offline")):
            _, text = self._doctor(cfg)
        self.assertNotIn("DIVERGENCE", text)
        # le KO vient de la base injoignable, pas des profils
        self.assertIn("1 checked", text)


class DeploiementTest(unittest.TestCase):
    def test_les_controles_de_deploiement_appellent_doctor_profiles(self):
        vm = (ROOT / "deploy/mise-a-jour/vm.sh").read_text(encoding="utf-8")
        poste = (ROOT / "deploy/mise-a-jour/poste.sh").read_text(encoding="utf-8")
        # mode agents et mode serveur
        self.assertGreaterEqual(vm.count("doctor --profiles"), 2)
        self.assertIn("ameesh_agents doctor --profiles", vm)
        self.assertIn("runuser -l ameesh -c 'ameesh doctor --profiles'", vm)
        self.assertIn('"$AMEESH" doctor --profiles', poste)


if __name__ == "__main__":
    unittest.main()

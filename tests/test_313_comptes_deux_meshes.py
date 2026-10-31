# SPDX-License-Identifier: AGPL-3.0-only
"""#313 (L65b, décision 0033) — un compte de fournisseur déclaré par deux
meshes d'un même hôte est signalé, et refusé en `account_sharing: strict`.

Constat du 10/10/2026 (pc-smichea) : le mesh Manaty (`config.json`) et celui
d'une autre organisation (`config-ancienne-base.json`) déclaraient les mêmes
dossiers d'identifiants Claude et Codex — quota commun, sessions dans les mêmes
dossiers, contraire à 0033. Chaque mesh a sa base : seul l'hôte le voit.

Depuis #2580, l'identité du mesh est lue en base (jamais le DSN) et le registre
est `~/.local/state/ameesh-host/accounts` ; ces témoins restent ceux de
#313 : détection, refus strict, refus relu au lancement, alerte, configuration.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import types
import unittest
from unittest import mock

from ameesh import account_claims, account_turn, accounts, config as config_mod, exploitation, notify
from ameesh import runner as runner_mod

from .test_l30_accounts import _dossier

MANATY = "host=/run/pgbouncer port=6432 user=ameesh dbname=ameesh"
AUTRE = "postgresql://ameesh@127.0.0.1:5442/ameesh"
MESH_A = "16ABCDEFt0000abcdj1111222233334444"
MESH_B = "16ABCDEFt0000abcdj5555666677778888"


class _Db:
    """Connexion factice : l'identité du mesh est celle de la base."""

    def __init__(self, cluster=MESH_A):
        self.cluster = cluster

    def query(self, sql, params=()):
        if "ameesh_events_cluster" in sql:
            return [{"cluster": self.cluster}]
        if "pg_control_system" in sql:
            return [{"system_identifier": "aabbccdd", "database": "ameesh",
                     "schema": "public"}]
        return [{"database": "ameesh", "schema": "public"}]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-313-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(account_claims.remember, None)
        self.registre = os.path.join(self.tmp, "registre")
        self.claude = _dossier(os.path.join(self.tmp, "claude-secondary"),
                               creds=".credentials.json")
        self.now = time.time()

    def cfg(self, dsn: str, comptes: dict, *, fichier: str = "config.json",
            sharing: str = "report") -> config_mod.Config:
        return config_mod.Config(
            dsn=dsn, host="pc-test", accounts=comptes, account_sharing=sharing,
            account_registry=self.registre,
            config_path=os.path.join(self.tmp, "conf", fichier))

    def secondaire(self, path: str | None = None) -> dict:
        return {"claude": [{"name": "secondaire", "path": path or self.claude}]}

    def refresh(self, cfg, mesh=MESH_A, **kw):
        return account_claims.refresh(cfg, mesh=mesh, now=self.now, **kw)

    def shared(self, cfg, mesh=MESH_A, **kw):
        return account_claims.shared(cfg, mesh=mesh, now=self.now, **kw)


class RegistreTest(_Base):
    def test_deux_meshes_meme_dossier_se_voient(self):
        manaty = self.cfg(MANATY, self.secondaire())
        autre = self.cfg(AUTRE, self.secondaire(), fichier="config-ancienne-base.json")
        self.refresh(manaty)
        self.refresh(autre, mesh=MESH_B)
        vu = self.shared(manaty)
        self.assertEqual([(p["harness"], p["account"]) for p in vu],
                         [("claude", "secondaire")])
        self.assertIn("config-ancienne-base.json", account_claims.describe(vu[0]))
        self.assertEqual([p["others"][0]["config"]
                          for p in self.shared(autre, mesh=MESH_B)],
                         ["config.json"])

    def test_deux_dsn_vers_la_meme_base_sont_le_meme_mesh(self):
        manaty = self.cfg(MANATY, self.secondaire())
        autre = self.cfg(AUTRE, self.secondaire(), fichier="config-ancienne-base.json")
        db = _Db(MESH_A)
        self.assertTrue(account_claims.refresh(manaty, db=db, now=self.now))
        self.assertTrue(account_claims.refresh(autre, db=db, now=self.now))
        self.assertEqual(account_claims.shared(manaty, db=db, now=self.now), [])

    def test_lien_et_dossier_par_defaut_sont_le_meme_identifiant(self):
        lien = os.path.join(self.tmp, "lien-claude")
        os.symlink(self.claude, lien)
        self.refresh(self.cfg(MANATY, self.secondaire()))
        autre = self.cfg(AUTRE, self.secondaire(lien))
        self.assertEqual(len(self.shared(autre, mesh=MESH_B)), 1)
        # un compte déclaré sans dossier désigne le dossier par défaut du harnais
        maison = os.path.join(self.tmp, "home")
        _dossier(os.path.join(maison, ".claude"))
        with mock.patch.dict(os.environ, {"HOME": maison}):
            self.refresh(self.cfg(MANATY, {"claude": [
                {"name": "primaire", "path": os.path.join(maison, ".claude")}]}))
            vu = self.shared(self.cfg(AUTRE, {"claude": [{"name": "primaire"}]}), mesh=MESH_B)
        self.assertEqual([p["account"] for p in vu], ["primaire"])

    def test_meme_fichier_de_cle(self):
        cle = os.path.join(self.tmp, "deepseek.key")
        with open(cle, "w", encoding="utf-8"):
            pass
        os.chmod(cle, 0o600)
        comptes = {"deepseek": [{"name": "cle", "type": "api_key_env", "key_file": cle}]}
        self.refresh(self.cfg(MANATY, comptes))
        self.assertEqual(len(self.shared(self.cfg(AUTRE, comptes), mesh=MESH_B)), 1)

    def test_meme_mesh_deux_configurations_ne_partagent_pas(self):
        self.refresh(self.cfg(MANATY, self.secondaire()))
        profil = self.cfg(MANATY, self.secondaire(), fichier="runner-profiles/x.json")
        self.refresh(profil)
        self.assertEqual(self.shared(profil), [])

    def test_dossiers_distincts_ne_partagent_pas(self):
        self.refresh(self.cfg(MANATY, self.secondaire()))
        propre = _dossier(os.path.join(self.tmp, "autre-claude"), creds=".credentials.json")
        self.assertEqual(self.shared(self.cfg(AUTRE, self.secondaire(propre)), mesh=MESH_B), [])

    def test_revendication_morte_n_est_plus_un_partage(self):
        self.refresh(self.cfg(MANATY, self.secondaire()))
        autre = self.cfg(AUTRE, self.secondaire())
        plus_tard = self.now + account_claims.CLAIM_WINDOW_S + 1
        self.assertEqual(account_claims.shared(autre, mesh=MESH_B, now=plus_tard), [])

    def test_compte_retire_retire_sa_revendication(self):
        manaty = self.cfg(MANATY, self.secondaire())
        self.refresh(manaty)
        autre = self.cfg(AUTRE, self.secondaire())
        self.assertTrue(self.shared(autre, mesh=MESH_B))
        # configuration corrigée : le compte n'est plus déclaré par Manaty
        self.refresh(dataclasses.replace(manaty, accounts={}))
        self.assertEqual(self.shared(autre, mesh=MESH_B), [])
        self.assertEqual(os.listdir(self.registre), [])

    def test_registre_sans_secret_et_prive(self):
        self.refresh(self.cfg(MANATY, self.secondaire()))
        (dossier,) = os.listdir(self.registre)
        (nom,) = os.listdir(os.path.join(self.registre, dossier))
        chemin = os.path.join(self.registre, dossier, nom)
        self.assertEqual(os.stat(chemin).st_mode & 0o777, 0o600)
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        self.assertNotIn(self.claude, texte)
        self.assertEqual(json.loads(texte)["account"], "secondaire")

    def test_revendication_illisible_ignoree(self):
        self.refresh(self.cfg(MANATY, self.secondaire()))
        (dossier,) = os.listdir(self.registre)
        (nom,) = os.listdir(os.path.join(self.registre, dossier))
        with open(os.path.join(self.registre, dossier, nom), "w", encoding="utf-8") as fh:
            fh.write("{")
        self.assertEqual(self.shared(self.cfg(AUTRE, self.secondaire()), mesh=MESH_B), [])


class PolitiqueTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.refresh(self.cfg(MANATY, self.secondaire()))

    def test_report_ne_refuse_pas(self):
        account_claims.remember(MESH_B)
        (profil,) = accounts.profiles(self.cfg(AUTRE, self.secondaire()), "claude")
        self.assertEqual(profil.refused, "")
        self.assertEqual(accounts.check(profil), [])

    def test_strict_refuse_le_compte(self):
        account_claims.remember(MESH_B)
        cfg = self.cfg(AUTRE, self.secondaire(), sharing="strict")
        (profil,) = accounts.profiles(cfg, "claude")
        self.assertIn("another mesh of this host", profil.refused)
        self.assertIn("config.json", profil.refused)
        self.assertEqual(accounts.check(profil)[0], profil.refused)
        # jamais classé, garde de budget active ou non (règle absolue)
        ev = accounts.evaluate(None, profil, [profil], self.now)
        self.assertTrue(ev.hard)
        self.assertEqual(accounts.rank([profil], {profil.name: ev}, self.now), [])
        self.assertEqual(accounts.rank([profil], {profil.name: ev}, self.now,
                                       paced=False), [])
        # le lancement le refuse aussi
        with self.assertRaises(accounts.AccountError):
            accounts.launch_env({}, {"claude": [profil]}, profil)

    def test_report_affiche_le_refus_strict(self):
        account_claims.remember(MESH_B)
        cfg = self.cfg(AUTRE, self.secondaire(), sharing="strict")
        (row,) = accounts.report(cfg, None, None, now=self.now, record=False)
        self.assertFalse(row["ok"])
        self.assertIn("another mesh", row["problems"][0])

    def test_lancement_relit_le_refus_apres_choix(self):
        account_claims.remember(MESH_B)
        cfg = self.cfg(AUTRE, self.secondaire(), sharing="strict")
        cached = accounts.parse(cfg.accounts)["claude"][0]
        worker = types.SimpleNamespace(cfg=cfg, agent={"harness": "claude"},
                                       _account_override=None, _account=cached)
        fresh = account_turn.for_turn(worker)
        self.assertTrue(fresh.refused)
        with self.assertRaises(accounts.AccountError):
            accounts.launch_env({}, {"claude": [fresh]}, fresh)

    def test_strict_sans_partage_ne_refuse_rien(self):
        account_claims.remember(MESH_B)
        propre = _dossier(os.path.join(self.tmp, "autre-claude"), creds=".credentials.json")
        cfg = self.cfg(AUTRE, self.secondaire(propre), sharing="strict")
        (profil,) = accounts.profiles(cfg, "claude")
        self.assertEqual(accounts.check(profil), [])


class AlerteTest(_Base):
    def test_alerte_account_shared(self):
        self.refresh(self.cfg(MANATY, self.secondaire()))
        self.refresh(self.cfg(AUTRE, self.secondaire(), sharing="strict"),
                     mesh=MESH_B)
        account_claims.remember(MESH_A)
        (alerte,) = exploitation._shared_account_alerts(
            self.cfg(MANATY, self.secondaire()), self.now)
        self.assertEqual(alerte["type"], "account_shared")
        self.assertEqual((alerte["host"], alerte["harness"], alerte["account"]),
                         ("pc-test", "claude", "secondaire"))
        self.assertIsNone(alerte["since"])
        self.assertIn("refused there", alerte["detail"])
        self.assertIn("reported only", alerte["detail"])
        self.assertIn("account_shared", exploitation.ALERT_TYPES)
        self.assertIn("account_shared", notify.DEFAULT_TYPES)

    def test_sans_partage_aucune_alerte(self):
        self.assertEqual(exploitation._shared_account_alerts(
            self.cfg(MANATY, self.secondaire()), self.now), [])


class ExecuteurTest(_Base):
    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_demarrage_revendique_et_journalise_une_fois(self):
        self.refresh(self.cfg(AUTRE, self.secondaire(),
                              fichier="config-ancienne-base.json"), mesh=MESH_B)
        faux = types.SimpleNamespace(cfg=self.cfg(MANATY, self.secondaire()), once=True,
                                     db=_Db(MESH_A))
        faux.account_claims_once = lambda: runner_mod.Runner.account_claims_once(faux)
        lignes: list[str] = []
        with mock.patch.object(runner_mod, "log_async", lignes.append):
            runner_mod.Runner.start_account_claims(faux)
            runner_mod.Runner.account_claims_once(faux)   # inchangé : pas de redite
        self.assertEqual(len(lignes), 1)
        self.assertIn("claude account secondaire", lignes[0])
        self.assertIn("config-ancienne-base.json", lignes[0])
        self.assertIn("reported only", lignes[0])
        # la revendication de Manaty est écrite : l'autre mesh le voit
        self.assertTrue(account_claims.shared(
            self.cfg(AUTRE, self.secondaire()), mesh=MESH_B, now=time.time()))

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_registre_inutilisable_journalise(self):
        with open(self.registre, "w", encoding="utf-8"):
            pass   # un fichier là où le registre attend un dossier
        faux = types.SimpleNamespace(cfg=self.cfg(MANATY, self.secondaire()), once=True,
                                     db=_Db(MESH_A))
        lignes: list[str] = []
        with mock.patch.object(runner_mod, "log_async", lignes.append):
            runner_mod.Runner.account_claims_once(faux)
        self.assertEqual(len(lignes), 1)
        self.assertIn("account registry of this host unusable", lignes[0])

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_identite_illisible_journalise(self):
        class _Illisible:
            def query(self, sql, params=()):
                raise RuntimeError("no access")
        faux = types.SimpleNamespace(cfg=self.cfg(MANATY, self.secondaire()), once=True,
                                     db=_Illisible())
        lignes: list[str] = []
        with mock.patch.object(runner_mod, "log_async", lignes.append):
            runner_mod.Runner.account_claims_once(faux)
        self.assertEqual(len(lignes), 1)
        self.assertIn("mesh identity unreadable", lignes[0])


class ConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-313-conf-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, payload: dict) -> config_mod.Config:
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return config_mod.load({"AMEESH_CONFIG": path})

    def test_defaut_report(self):
        cfg = self.load({})
        self.assertEqual(cfg.account_sharing, "report")
        self.assertEqual(account_claims.registry(cfg),
                         account_claims.default_registry())

    def test_strict_et_registre(self):
        cfg = self.load({"account_sharing": "Strict", "account_registry": "~/reg"})
        self.assertEqual(cfg.account_sharing, "strict")
        self.assertEqual(cfg.account_registry, os.path.expanduser("~/reg"))

    def test_politique_mal_ecrite_refusee(self):
        with self.assertRaises(SystemExit):
            self.load({"account_sharing": "strcit"})


if __name__ == "__main__":
    unittest.main()

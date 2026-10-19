# SPDX-License-Identifier: AGPL-3.0-only
"""#2580 (L65b, décision 0033) — un compte de fournisseur déclaré par deux
meshes d'un même hôte est signalé depuis le registre de l'hôte.

Le constat du 19/10/2026 sur pc-smichea : `config.json` (mesh Manaty) et
`config-ancienne-base.json` (mesh thepackengers) déclarent les mêmes dossiers
Claude et Codex. Chaque mesh a sa base ; le contrôle #1883 ne voit que les
configurations d'UNE base. Témoins :

* l'identité du mesh est lue EN BASE (`ameesh_events_cluster`), jamais
  dérivée du DSN : deux DSN différents vers la même base sont le même mesh,
  deux bases (ou deux schémas) sont deux meshes ;
* deux meshes, même dossier ou même clé (fichier résolu, valeur de variable) :
  signalés ; deux configurations d'un même mesh, revendication périmée,
  revendication d'une autre machine : rien ; deux noms d'hôte
  (`AMEESH_HOST`) sur une même machine : signalés (#2593) ;
* le registre ne contient ni chemin ni secret (dossier 0700, fichier 0600) ;
* `report` (défaut) signale sans refuser ; `accounts_shared: refuse` refuse
  le compte dans ce mesh, comme `account_sharing: strict` (#313) ;
* `ameesh doctor` et `ameesh accounts list` le disent.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import account_claims, accounts, config as config_mod, cost, exploitation, notify

from .test_l30_accounts import _Base as _PgBase, _dossier

HOUR = 3600.0
MESH_A = "16ABCDEFt0000abcdj1111222233334444"
#: le même mesh (même cluster, même journal) après une bascule ou un PITR : la
#: timeline a changé, pas l'identité (#2592 N1)
MESH_A_AUTRE_TIMELINE = "16ABCDEFt00000002j1111222233334444"
MESH_B = "16ABCDEFt0000abcdj5555666677778888"
#: la forme canonique : system_identifier + journal, sans timeline (#2592)
MESH_A_CANONIQUE = "mesh:16abcdef:j:1111222233334444"


class _Db:
    """Une connexion factice : l'identité du mesh est celle que la base rend."""

    def __init__(self, cluster=MESH_A, *, system_identifier="16abcdef",
                 journal="1111222233334444"):
        self.cluster = cluster
        self.system_identifier = system_identifier
        self.journal = journal
        self.calls: list[str] = []

    def query(self, sql, params=()):
        self.calls.append(sql)
        if "ameesh_events_cluster" in sql:
            return [{"cluster": self.cluster}]
        if "ameesh_events_journal" in sql:
            return [{"system_identifier": self.system_identifier,
                     "journal": self.journal}]
        return []


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2580-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(account_claims.remember, None)
        self.registre = os.path.join(self.tmp, "registre")
        self.claude = _dossier(os.path.join(self.tmp, "claude-secondary"),
                               creds=".credentials.json")
        self.now = time.time()

    def cfg(self, comptes: dict, *, fichier: str = "config.json",
            sharing: str = "report", shared: str = "report") -> config_mod.Config:
        # Le DSN ne sert plus à identifier le mesh : deux DSN différents vers
        # la même base sont le MÊME mesh (#2580).
        return config_mod.Config(
            dsn="postgresql://ameesh@127.0.0.1:5442/ameesh",
            host="pc-test", accounts=comptes, account_sharing=sharing,
            accounts_shared=shared, account_registry=self.registre,
            config_path=os.path.join(self.tmp, "conf", fichier))

    def secondaire(self, path: str | None = None) -> dict:
        return {"claude": [{"name": "secondaire", "path": path or self.claude}]}

    def refresh(self, cfg, mesh=MESH_A, **kw):
        return account_claims.refresh(cfg, mesh=mesh, now=self.now, **kw)

    def shared(self, cfg, mesh=MESH_A, **kw):
        return account_claims.shared(cfg, mesh=mesh, now=self.now, **kw)

    def claims_files(self) -> list[str]:
        out = []
        for root, _dirs, files in os.walk(self.registre):
            out.extend(os.path.join(root, name) for name in files)
        return out


class DeuxMeshesTest(_Base):
    def test_deux_meshes_meme_dossier_signale(self):
        manaty = self.cfg(self.secondaire())
        autre = self.cfg(self.secondaire(), fichier="config-ancienne-base.json")
        self.refresh(manaty, mesh=MESH_A)
        self.refresh(autre, mesh=MESH_B)
        vu = self.shared(manaty)
        self.assertEqual([(p["harness"], p["account"]) for p in vu],
                         [("claude", "secondaire")])
        self.assertIn("config-ancienne-base.json", account_claims.describe(vu[0]))
        self.assertEqual([p["others"][0]["config"] for p in self.shared(autre, mesh=MESH_B)],
                         ["config.json"])

    def test_deux_dsn_meme_mesh_ne_partagent_pas(self):
        """L'identité est lue en base : pgbouncer et TCP direct = même mesh."""
        manaty = self.cfg(self.secondaire(), fichier="config.json")
        autre = self.cfg(self.secondaire(), fichier="config-ancienne-base.json")
        db = _Db(MESH_A)
        self.assertTrue(account_claims.refresh(manaty, db=db, now=self.now))
        self.assertTrue(account_claims.refresh(autre, db=db, now=self.now))
        self.assertEqual(account_claims.shared(manaty, db=db, now=self.now), [])

    def test_deux_bases_sont_deux_meshes(self):
        manaty = self.cfg(self.secondaire())
        autre = self.cfg(self.secondaire(), fichier="config-ancienne-base.json")
        self.assertTrue(account_claims.refresh(manaty, db=_Db(MESH_A), now=self.now))
        self.assertTrue(account_claims.refresh(autre, db=_Db(MESH_B), now=self.now))
        db_b = _Db(MESH_B)
        vu = account_claims.shared(autre, db=db_b, now=self.now)
        self.assertEqual([p["account"] for p in vu], ["secondaire"])

    def test_identite_canonique_une_seule_forme(self):
        """#2592 N2 : le cluster et les deux parties stables du même mesh
        donnent la MÊME clé, quel que soit le droit du rôle à lire l'un ou
        l'autre."""
        self.assertEqual(account_claims.mesh_identity(_Db(MESH_A)),
                         MESH_A_CANONIQUE)
        self.assertEqual(account_claims.mesh_identity(_Db(cluster=None)),
                         MESH_A_CANONIQUE)
        cfg = self.cfg(self.secondaire())
        self.assertTrue(account_claims.refresh(cfg, db=_Db(cluster=None),
                                               now=self.now))
        self.assertEqual(account_claims.shared(cfg, db=_Db(MESH_A), now=self.now), [])

    def test_bascule_timeline_reste_le_meme_mesh(self):
        """#2592 N1 : une bascule ou un PITR change la timeline, pas le mesh."""
        self.assertEqual(account_claims.mesh_identity(_Db(MESH_A)),
                         account_claims.mesh_identity(_Db(MESH_A_AUTRE_TIMELINE)))
        un = self.cfg(self.secondaire())
        deux = self.cfg(self.secondaire(), fichier="config-ancienne-base.json")
        self.assertTrue(account_claims.refresh(un, db=_Db(MESH_A), now=self.now))
        self.assertTrue(account_claims.refresh(
            deux, db=_Db(MESH_A_AUTRE_TIMELINE), now=self.now))
        self.assertEqual(account_claims.shared(
            deux, db=_Db(MESH_A_AUTRE_TIMELINE), now=self.now), [])

    def test_revendication_v2_ignoree(self):
        """La v2 (identité brute, timeline incluse) n'est pas une clé
        comparable : elle est ignorée, jamais vue comme un autre mesh."""
        cfg = self.cfg(self.secondaire())
        self.refresh(cfg, mesh=MESH_A)
        (chemin,) = self.claims_files()
        dossier, nom = os.path.split(chemin)
        (suffixe,) = nom.split(".", 1)[1:]
        with open(os.path.join(dossier, "0123456789abcdef." + suffixe),
                  "w", encoding="utf-8") as fh:
            json.dump({"schema": "ameesh-account-claim/2", "host": "pc-test",
                       "harness": "claude", "account": "secondaire",
                       "config": "config.json", "policy": "report",
                       "seen_at": self.now}, fh)
        self.assertEqual(self.shared(self.cfg(self.secondaire()), mesh=MESH_A), [])

    def test_identite_illisible_n_ecrit_rien(self):
        class _Illisible:
            def query(self, sql, params=()):
                raise RuntimeError("no access")

        cfg = self.cfg(self.secondaire())
        self.assertFalse(account_claims.refresh(cfg, db=_Illisible(), now=self.now))
        self.assertFalse(os.path.exists(self.registre))
        self.assertEqual(account_claims.shared(cfg, db=_Illisible(), now=self.now), [])

    def test_lien_et_dossier_par_defaut_sont_le_meme_identifiant(self):
        lien = os.path.join(self.tmp, "lien-claude")
        os.symlink(self.claude, lien)
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        self.assertEqual(len(self.shared(self.cfg(self.secondaire(lien)), mesh=MESH_B)), 1)
        # un compte déclaré sans dossier désigne le dossier par défaut du harnais
        maison = os.path.join(self.tmp, "home")
        _dossier(os.path.join(maison, ".claude"))
        with mock.patch.dict(os.environ, {"HOME": maison}):
            self.refresh(self.cfg({"claude": [
                {"name": "primaire", "path": os.path.join(maison, ".claude")}]}),
                mesh=MESH_A)
            vu = self.shared(self.cfg({"claude": [{"name": "primaire"}]}), mesh=MESH_B)
        self.assertEqual([p["account"] for p in vu], ["primaire"])

    def test_meme_fichier_de_cle(self):
        cle = os.path.join(self.tmp, "deepseek.key")
        with open(cle, "w", encoding="utf-8") as fh:
            fh.write("cle-factice-a\n")
        os.chmod(cle, 0o600)
        comptes = {"deepseek": [{"name": "cle", "type": "api_key_env",
                                 "key_file": cle}]}
        self.refresh(self.cfg(comptes), mesh=MESH_A)
        self.assertEqual(len(self.shared(self.cfg(comptes), mesh=MESH_B)), 1)

    def test_cle_api_key_env_partagee(self):
        comptes = {"deepseek": [{"name": "cle", "type": "api_key_env",
                                 "key_env": "AMEESH_TEST_DEEPSEEK_KEY"}]}
        with mock.patch.dict(os.environ, {"AMEESH_TEST_DEEPSEEK_KEY": "valeur-secrete"}):
            self.refresh(self.cfg(comptes), mesh=MESH_A)
            self.assertEqual(len(self.shared(self.cfg(comptes), mesh=MESH_B)), 1)
        # la valeur n'est jamais dans le registre
        for chemin in self.claims_files():
            with open(chemin, encoding="utf-8") as fh:
                self.assertNotIn("valeur-secrete", fh.read())
        with mock.patch.dict(os.environ, {"AMEESH_TEST_DEEPSEEK_KEY": "autre-valeur"}):
            self.assertEqual(self.shared(self.cfg(comptes), mesh=MESH_B), [])
        # valeur absente de l'environnement : rien à comparer, aucune revendication
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMEESH_TEST_DEEPSEEK_KEY", None)
            self.assertEqual(self.shared(self.cfg(comptes), mesh=MESH_B), [])

    def test_meme_mesh_deux_configurations_ne_partagent_pas(self):
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        profil = self.cfg(self.secondaire(), fichier="runner-profiles/x.json")
        self.refresh(profil, mesh=MESH_A)
        self.assertEqual(self.shared(profil), [])

    def test_dossiers_distincts_ne_partagent_pas(self):
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        propre = _dossier(os.path.join(self.tmp, "autre-claude"),
                          creds=".credentials.json")
        self.assertEqual(self.shared(self.cfg(self.secondaire(propre)), mesh=MESH_B), [])

    def test_revendication_morte_n_est_plus_un_partage(self):
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        plus_tard = self.now + account_claims.CLAIM_WINDOW_S + 1
        self.assertEqual(
            account_claims.shared(self.cfg(self.secondaire()), mesh=MESH_B, now=plus_tard),
            [])

    def test_deux_noms_d_hote_meme_machine_signale(self):
        """#2593 : AMEESH_HOST diffère entre les deux meshes du poste, la
        machine est la même — le partage est vu dans les deux sens."""
        manaty = self.cfg(self.secondaire())
        autre = dataclasses.replace(
            self.cfg(self.secondaire(), fichier="config-ancienne-base.json"),
            host="pc-ailleurs")
        self.refresh(manaty, mesh=MESH_A)
        self.refresh(autre, mesh=MESH_B)
        self.assertEqual([p["account"] for p in self.shared(autre, mesh=MESH_B)],
                         ["secondaire"])
        self.assertEqual([p["account"] for p in self.shared(manaty, mesh=MESH_A)],
                         ["secondaire"])

    def test_revendication_d_une_autre_machine_ignoree(self):
        """Registre partagé entre machines (XDG_STATE_HOME commun) : un même
        chemin y désigne deux identifiants distincts, rien n'est signalé."""
        with mock.patch.object(account_claims, "machine_hash",
                               return_value="0123456789abcdef"):
            self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        self.assertEqual(self.shared(self.cfg(self.secondaire()), mesh=MESH_B), [])

    def test_revendication_sans_machine_comparee_sur_l_hote(self):
        """Une revendication d'avant #2593 n'a pas d'empreinte de machine :
        elle reste départagée par `host`."""
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        for chemin in self.claims_files():
            with open(chemin, encoding="utf-8") as fh:
                data = json.load(fh)
            del data["machine"]
            with open(chemin, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        meme = self.cfg(self.secondaire())
        self.assertTrue(self.shared(meme, mesh=MESH_B))
        ailleurs = dataclasses.replace(meme, host="pc-ailleurs")
        self.assertEqual(self.shared(ailleurs, mesh=MESH_B), [])

    def test_empreinte_de_machine_stable_sans_identifiant_en_clair(self):
        account_claims._MACHINE = None
        self.addCleanup(setattr, account_claims, "_MACHINE", None)
        fichier = os.path.join(self.tmp, "machine-id")
        with open(fichier, "w", encoding="ascii") as fh:
            fh.write("0f1e2d3c4b5a69788796a5b4c3d2e1f0\n")
        with mock.patch.object(account_claims, "MACHINE_ID_FILES",
                               (os.path.join(self.tmp, "absent"), fichier)):
            empreinte = account_claims.machine_hash()
        self.assertRegex(empreinte, r"^[0-9a-f]{16}$")
        self.assertNotIn(empreinte, "0f1e2d3c4b5a69788796a5b4c3d2e1f0")
        account_claims._MACHINE = None
        with mock.patch.object(account_claims, "MACHINE_ID_FILES", (fichier,)):
            self.assertEqual(account_claims.machine_hash(), empreinte)
        account_claims._MACHINE = None
        with mock.patch.object(account_claims, "MACHINE_ID_FILES", ()), \
                mock.patch.object(account_claims.socket, "gethostname",
                                  return_value="pc-test"):
            self.assertNotEqual(account_claims.machine_hash(), empreinte)

    def test_compte_retire_retire_sa_revendication(self):
        manaty = self.cfg(self.secondaire())
        self.refresh(manaty, mesh=MESH_A)
        self.assertTrue(self.shared(self.cfg(self.secondaire()), mesh=MESH_B))
        # configuration corrigée : le compte n'est plus déclaré par Manaty
        self.refresh(dataclasses.replace(manaty, accounts={}), mesh=MESH_A)
        self.assertEqual(self.shared(self.cfg(self.secondaire()), mesh=MESH_B), [])
        self.assertEqual(os.listdir(self.registre), [])

    def test_registre_sans_secret_et_prive(self):
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        (dossier,) = os.listdir(self.registre)
        self.assertEqual(os.stat(os.path.join(self.registre, dossier)).st_mode & 0o777,
                         0o700)
        (nom,) = os.listdir(os.path.join(self.registre, dossier))
        chemin = os.path.join(self.registre, dossier, nom)
        self.assertEqual(os.stat(chemin).st_mode & 0o777, 0o600)
        with open(chemin, encoding="utf-8") as fh:
            texte = fh.read()
        self.assertNotIn(self.claude, texte)
        self.assertNotIn(self.tmp, texte)
        self.assertEqual(json.loads(texte)["account"], "secondaire")
        self.assertEqual(json.loads(texte)["schema"], account_claims.SCHEMA)

    def test_revendication_illisible_ignoree(self):
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        for chemin in self.claims_files():
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write("{")
        self.assertEqual(self.shared(self.cfg(self.secondaire()), mesh=MESH_B), [])

    def test_fenetre_de_vie_identique_a_1883(self):
        # la revendication vit aussi longtemps qu'une empreinte d'identité
        self.assertEqual(account_claims.CLAIM_WINDOW_S, accounts.IDENTITY_WINDOW_S)

    def test_registre_par_defaut_suit_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp}):
            self.assertEqual(
                account_claims.registry(config_mod.Config()),
                os.path.join(self.tmp, "ameesh-host", "accounts"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}):
            self.assertEqual(account_claims.registry(config_mod.Config()),
                             os.path.expanduser(
                                 "~/.local/state/ameesh-host/accounts"))


class PolitiqueTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        self.addCleanup(account_claims.remember, None)

    def test_report_ne_refuse_pas(self):
        account_claims.remember(MESH_B)
        (profil,) = accounts.profiles(self.cfg(self.secondaire()), "claude")
        self.assertEqual(profil.refused, "")
        self.assertEqual(accounts.check(profil), [])

    def test_accounts_shared_refuse(self):
        account_claims.remember(MESH_B)
        cfg = self.cfg(self.secondaire(), shared="refuse")
        (profil,) = accounts.profiles(cfg, "claude")
        self.assertIn("another mesh of this host", profil.refused)
        self.assertIn("accounts_shared: refuse", profil.refused)
        self.assertIn("config.json", profil.refused)
        self.assertEqual(accounts.check(profil)[0], profil.refused)

    def test_strict_refuse_le_compte(self):
        account_claims.remember(MESH_B)
        cfg = self.cfg(self.secondaire(), sharing="strict")
        (profil,) = accounts.profiles(cfg, "claude")
        self.assertIn("another mesh of this host", profil.refused)
        self.assertIn("account_sharing: strict", profil.refused)
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

    def test_report_affiche_le_partage_et_le_refus(self):
        account_claims.remember(MESH_B)
        cfg = self.cfg(self.secondaire(), shared="refuse")
        (row,) = accounts.report(cfg, None, None, now=self.now, record=False)
        self.assertFalse(row["ok"])
        self.assertIn("another mesh", row["problems"][0])
        self.assertTrue(row["shared"])
        texte = accounts.format_rows([row])
        self.assertIn("shared with another mesh of the host", texte)
        self.assertIn("config.json", texte)

    def test_doctor_lines(self):
        account_claims.remember(MESH_B)
        lignes = account_claims.doctor_lines(self.cfg(self.secondaire()))
        self.assertEqual(len(lignes), 1)
        self.assertIn("claude/secondaire", lignes[0])
        self.assertIn("reported only", lignes[0])
        self.assertEqual(account_claims.doctor_lines(
            self.cfg(self.secondaire(), shared="refuse"))[0].split("(")[-1].strip(")"),
            "refused here")

    def test_strict_sans_partage_ne_refuse_rien(self):
        account_claims.remember(MESH_B)
        propre = _dossier(os.path.join(self.tmp, "autre-claude"),
                          creds=".credentials.json")
        cfg = self.cfg(self.secondaire(propre), sharing="strict")
        (profil,) = accounts.profiles(cfg, "claude")
        self.assertEqual(accounts.check(profil), [])

    def test_lancement_relit_le_refus_apres_choix(self):
        import types
        from ameesh import account_turn
        account_claims.remember(MESH_B)
        cfg = self.cfg(self.secondaire(), shared="refuse")
        cached = accounts.parse(cfg.accounts)["claude"][0]
        worker = types.SimpleNamespace(cfg=cfg, agent={"harness": "claude"},
                                       _account_override=None, _account=cached)
        fresh = account_turn.for_turn(worker)
        self.assertTrue(fresh.refused)
        with self.assertRaises(accounts.AccountError):
            accounts.launch_env({}, {"claude": [fresh]}, fresh)

    def _use(self, cfg, **patches):
        """`ameesh accounts use claude secondaire` dans le mesh B : le
        résultat (code ou exception) et le profil passé à `force`."""
        import contextlib, io, types
        from ameesh import mesh_cli

        class _Conn(_Db):
            def close(self):
                pass

        args = types.SimpleNamespace(accounts_command="use", harness="claude",
                                     account="secondaire")
        with mock.patch.object(mesh_cli, "_open", return_value=_Conn(MESH_B)), \
                mock.patch.object(mesh_cli.fil, "record"), \
                mock.patch.object(mesh_cli.accounts_mod, "force",
                                  **(patches or {"wraps": accounts.force})) as force, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                out = mesh_cli.cmd_accounts(cfg, args)
            except accounts.AccountError as exc:
                out = exc
        (profil,) = force.call_args.args[3]
        return out, profil

    def test_use_refuse_un_compte_refuse(self):
        # #2572 (#313 N2) : le forçage d'un compte refusé par la politique de
        # partage est refusé à la commande, avec sa raison, avant toute
        # écriture de l'état des comptes.
        for cfg in (self.cfg(self.secondaire(), sharing="strict"),
                    self.cfg(self.secondaire(), shared="refuse")):
            exc, profil = self._use(cfg)
            self.assertIsInstance(exc, accounts.AccountError)
            self.assertIn("unusable", str(exc))
            self.assertIn(profil.refused, str(exc))
            self.assertIn("another mesh of this host", str(exc))

    def test_use_en_report_force_le_compte(self):
        code, profil = self._use(self.cfg(self.secondaire()), return_value=True)
        self.assertEqual(code, 0)
        self.assertEqual(profil.refused, "")


class AlerteTest(_Base):
    def test_alerte_account_shared(self):
        self.refresh(self.cfg(self.secondaire()), mesh=MESH_A)
        self.refresh(self.cfg(self.secondaire(), sharing="strict",
                              fichier="config-ancienne-base.json"), mesh=MESH_B)
        account_claims.remember(MESH_A)
        (alerte,) = exploitation._shared_account_alerts(
            self.cfg(self.secondaire()), self.now)
        self.assertEqual(alerte["type"], "account_shared")
        self.assertEqual((alerte["host"], alerte["harness"], alerte["account"]),
                         ("pc-test", "claude", "secondaire"))
        self.assertIsNone(alerte["since"])
        self.assertIn("refused there", alerte["detail"])
        self.assertIn("reported only", alerte["detail"])
        self.assertIn("account_shared", exploitation.ALERT_TYPES)
        self.assertIn("account_shared", notify.DEFAULT_TYPES)
        # une alerte par compte et par jour : la clé de notification ne change
        # pas d'un passage au suivant le même jour, et change le jour suivant
        self.assertEqual(notify.key_of(alerte), notify.key_of(dict(alerte)))
        self.assertNotEqual(notify.key_of(alerte),
                            notify.key_of(dict(alerte, day="2000-01-01")))

    def test_sans_partage_aucune_alerte(self):
        self.assertEqual(exploitation._shared_account_alerts(
            self.cfg(self.secondaire()), self.now), [])


class ConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2580-conf-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, payload: dict) -> config_mod.Config:
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return config_mod.load({"AMEESH_CONFIG": path})

    def test_defaut_report(self):
        cfg = self.load({})
        self.assertEqual(cfg.account_sharing, "report")
        self.assertEqual(cfg.accounts_shared, "report")
        self.assertEqual(account_claims.registry(cfg),
                         account_claims.default_registry())

    def test_refus_et_registre(self):
        cfg = self.load({"accounts_shared": "Refuse", "account_registry": "~/reg"})
        self.assertEqual(cfg.accounts_shared, "refuse")
        self.assertEqual(cfg.account_registry, os.path.expanduser("~/reg"))
        self.assertTrue(account_claims.refuses(cfg))

    def test_politiques_mal_ecrites_refusees(self):
        with self.assertRaises(SystemExit):
            self.load({"accounts_shared": "refusé"})
        with self.assertRaises(SystemExit):
            self.load({"account_sharing": "strcit"})


class BaseTest(_PgBase):
    """Le registre lit l'identité du mesh dans la vraie base de test."""

    def setUp(self) -> None:
        super().setUp()
        self.registre = os.path.join(self.tmp, "claims-2580")
        self.now = time.time()

    def cfg_claims(self, comptes: dict):
        return dataclasses.replace(self.cfg, host="pc-test", accounts=comptes,
                                   account_registry=self.registre,
                                   config_path=os.path.join(self.tmp, "conf", "config.json"))

    def test_identite_lue_en_base(self):
        cluster = self.db.query("select ameesh_events_cluster() as cluster")[0]["cluster"]
        stable = self.db.query(
            "select (select to_hex(system_identifier) from pg_control_system())"
            " as system_identifier,"
            " (select journal from ameesh_events_journal where one) as journal")[0]
        canonique = "mesh:%s:j:%s" % (stable["system_identifier"],
                                      stable["journal"])
        self.assertEqual(account_claims.mesh_identity(self.db), canonique)
        self.assertNotEqual(canonique, cluster)
        # la timeline de la base n'est pas dans l'identité du mesh
        (timeline,) = cluster.split("t", 1)[1].split("j", 1)[:1]
        self.assertNotIn(timeline, canonique)

    def test_le_choix_ecrit_la_revendication(self):
        dossier = _dossier(os.path.join(self.tmp, "comptes", "claude-1"),
                           creds=".credentials.json")
        comptes = {"claude": [{"name": "primaire", "path": dossier}]}
        cfg = self.cfg_claims(comptes)
        items = accounts.parse(comptes)["claude"]
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        choix = accounts.choose(self.db, cfg.host, "claude", items, book,
                                now=self.now, cfg=cfg)
        self.assertIsNotNone(choix.profile)
        self.assertTrue(os.path.isdir(self.registre))
        autres = account_claims.shared(cfg, mesh=MESH_B, now=self.now)
        self.assertEqual([(p["harness"], p["account"]) for p in autres],
                         [("claude", "primaire")])
        self.assertEqual(account_claims.shared(cfg, db=self.db, now=self.now), [])


if __name__ == "__main__":

    unittest.main()

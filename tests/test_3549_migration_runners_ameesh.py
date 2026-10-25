# SPDX-License-Identifier: AGPL-3.0-only
"""#3549 — runbook et script réversibles : exécuteurs de pc-smichea sous ameesh.

Aucun test ne touche l'hôte : le script est chargé comme module, un `Hote`
factice enraciné dans un répertoire temporaire enregistre chaque commande, et
les commandes système sont simulées. On vérifie les gardes qui comptent pour
la décision owner #135872 a :

* la cible ne reçoit jamais les clés de l'owner (`~/.ssh`, `.credentials.json`
  de Claude) : seulement une clé dédiée et les auth Codex/DeepSeek copiées ;
* un compte dans `docker`, `sudo` ou `wheel` est refusé (la clé resterait
  lisible par un conteneur) ;
* les pièces #3444 (`cles-owner.conf`) et #3540 (`git-agents.conf`) ne sont
  pas recopiées dans le home cible ; elles sont sauvegardées puis retirées à
  la bascule, restaurées au retour ;
* le retour est une seule commande, qui restaure l'ensemble actif de la
  source, ses drop-ins et ses cwd ;
* le créneau et la file des paquets sont des gardes, pas des politesses :
  une bascule à moins de 60 min d'une coupure ou pendant un paquet est
  refusée avant tout arrêt.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/poste/migration-runners-ameesh.py"


def charger():
    spec = importlib.util.spec_from_file_location("migration3549", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["migration3549"] = module
    spec.loader.exec_module(module)
    return module


M = charger()


class FauxHote:
    """Un exécuteur qui répond par motif et enregistre les commandes."""

    def __init__(self, racine: Path, utilisateurs: dict[str, "M.Compte"]):
        self.racine = racine
        self.utilisateurs = utilisateurs
        self.reponses: list[tuple[str, int, str, str]] = []
        self.appels: list[tuple[str, ...]] = []
        self.executeur = self.executer

    def repondre(self, motif: str, code: int = 0, sortie: str = "", erreur: str = ""):
        self.reponses.append((motif, code, sortie, erreur))

    def remplacer(self, motif: str, code: int = 0, sortie: str = "", erreur: str = ""):
        self.reponses.insert(0, (motif, code, sortie, erreur))

    def executer(self, argv, env, delai):
        commande = " ".join(argv)
        self.appels.append(tuple(argv))
        for motif, code, sortie, erreur in self.reponses:
            if motif in commande:
                return M.Resultat(code, sortie, erreur)
        return M.Resultat(0, "", "")

    def a_appele(self, motif: str) -> list[tuple[str, ...]]:
        return [appel for appel in self.appels if motif in " ".join(appel)]


class Banc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="migration3549-")
        self.addCleanup(self.tmp.cleanup)
        self.racine = Path(self.tmp.name)
        self.source_home = self.racine / "home" / "smichea"
        self.cible_home = self.racine / "home" / "ameesh"
        self.source_home.mkdir(parents=True)
        self.cible_home.mkdir(parents=True)
        # les homes des comptes sont les chemins logiques ; `Hote.chemin`
        # les enracine dans le temporaire du banc.
        self.comptes = {
            "smichea": M.Compte("smichea", 1000, 1000, "/home/smichea",
                                "/bin/bash", ("smichea", "docker")),
            "ameesh": M.Compte("ameesh", 1001, 1001, "/home/ameesh",
                               "/bin/bash", ("ameesh",)),
        }
        self.faux = FauxHote(self.racine, self.comptes)
        self.hote = M.Hote(racine=self.racine, comptes=self.comptes,
                           executeur=self.faux.executer)
        self.ctx = M.Contexte(
            hote=self.hote, depot=ROOT,
            etat_dir=self.hote.chemin("/var/lib/ameesh/migration-runners"),
            source=self.comptes["smichea"], cible=self.comptes["ameesh"],
            racine_surcharge=True)
        self.ctx.etat_dir.mkdir(parents=True)
        self.ecrire_config()
        self.ecrire_unites()
        self.ecrire_share()
        self.faux.repondre("systemctl --user show",
                           0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\n"
                              "UnitFileState=enabled\n")
        self.faux.repondre("systemctl --user list-units", 0, "")
        self.faux.repondre("loginctl show-user", 0, "Linger=yes\n")
        self.faux.repondre("ameesh list --json", 0, json.dumps([
            {"name": "ameesh-dev1", "host": "pc-smichea", "mode": "execute",
             "harness": "claude", "status": "idle", "operator_paused": False},
            {"name": "codex2", "host": "pc-smichea", "mode": "execute",
             "harness": "codex", "status": "idle", "operator_paused": False},
            {"name": "bureau", "host": "pc-smichea", "mode": "externe",
             "harness": "codex", "status": "idle", "operator_paused": False},
        ]))
        self.faux.repondre("release calendar", 0,
                           "ameesh: canon 0b97d2303ab2, coupures 11:00, 23:00, "
                           "dernière 11:00, prochaine 25/10 23:00, cible main\n")
        self.faux.repondre("work queue show --target main --json", 0, "[]")
        self.faux.repondre("mechanics.configured", 0, "Europe/Paris\n")

    def ecrire_config(self):
        dossier = self.source_home / ".config/ameesh"
        dossier.mkdir(parents=True)
        (dossier / "config.json").write_text(json.dumps({
            "dsn": "postgresql://ameesh@example/ameesh",
            "host": "pc-smichea",
            "accounts": {
                "claude": [{"name": "primaire"},
                           {"name": "secondaire", "type": "config_dir",
                            "path": str(self.source_home / ".claude-secondary")}],
                "codex": [{"name": "primaire"}],
            },
            "canons": [{"path": str(self.source_home / "canon"), "ref": "origin/main"}],
        }, indent=2), encoding="utf-8")
        os.chmod(dossier / "config.json", 0o600)
        (dossier / "env").write_text("DEEPSEEK_API_KEY=secret-de-test\n",
                                     encoding="utf-8")
        os.chmod(dossier / "env", 0o600)
        (self.source_home / ".ssh").mkdir(mode=0o700)
        (self.source_home / ".ssh/id_ed25519").write_text("cle-owner\n")
        (self.source_home / ".ssh/id_ed25519.pub").write_text("ssh-ed25519 OWNER\n")
        (self.source_home / ".ssh/known_hosts").write_text(
            "github.com ssh-ed25519 AAAA\n", encoding="utf-8")
        (self.source_home / ".claude").mkdir()
        (self.source_home / ".claude/.credentials.json").write_text(
            '{"claudeAiOauth": {"accessToken": "secret"}}\n', encoding="utf-8")

    def ecrire_unites(self):
        dossier = self.source_home / ".config/systemd/user"
        dossier.mkdir(parents=True)
        (dossier / "ameesh-runner-agent@.service").write_text(
            "[Service]\n"
            "UnsetEnvironment=SSH_AUTH_SOCK SSH_AGENT_PID\n"
            "ExecStart=%h/.local/share/ameesh/venv/bin/ameesh run "
            "--agents %i --runner-id runner-%i@%H\n")
        (dossier / "ameesh-agents.slice").write_text("[Slice]\nMemoryMax=85%\n")
        dropins = dossier / "ameesh-runner-agent@.service.d"
        dropins.mkdir()
        (dropins / "cles-owner.conf").write_text(
            "[Service]\nInaccessiblePaths=%h/.ssh\n")
        (self.source_home / ".config/ameesh/git-agents.conf").write_text(
            "[credential]\nhelper = !gh\n")

    def ecrire_share(self):
        share = self.source_home / ".local/share/ameesh"
        share.mkdir(parents=True)
        (share / "src-abcdef0/deploy/mise-a-jour").mkdir(parents=True)
        (share / "src-abcdef0/deploy/mise-a-jour/bascule.py").write_text(
            "# faux bascule\n")
        (share / "src-current").symlink_to("src-abcdef0")
        (share / "venv/bin").mkdir(parents=True)
        (share / "venv/bin/ameesh").write_text("#!/bin/sh\necho ameesh 1.22.0\n")
        os.chmod(share / "venv/bin/ameesh", 0o755)
        self.ctx.cible_home.joinpath(
            ".local/share/ameesh/venv/bin").mkdir(parents=True)
        (self.ctx.cible_home / ".local/share/ameesh/venv/bin/ameesh").write_text(
            "#!/bin/sh\necho ameesh 1.22.0\n")
        os.chmod(self.ctx.cible_home / ".local/share/ameesh/venv/bin/ameesh", 0o755)
        canon = self.source_home / "canon"
        canon.mkdir()
        (canon / "index.md").write_text("# canon\n")
        dev = self.source_home / "development/manaty/ameesh-dev1"
        (dev / ".git").mkdir(parents=True)
        (dev / "README.md").write_text("clone\n")
        dev_cible = self.cible_home / "development/manaty/ameesh-dev1"
        (dev_cible / ".git").mkdir(parents=True)
        (dev_cible / "README.md").write_text("clone\n")

    def test_definition_de_la_cible_absente_de_la_source(self):
        """La cible ne reçoit ni ~/.ssh ni les identifiants Claude."""
        M.preparer_config(self.ctx)
        self.assertFalse((self.cible_home / ".ssh").exists())
        self.assertFalse((self.cible_home / ".claude").exists())
        self.assertTrue((self.cible_home / ".config/ameesh/config.json").is_file())
        self.assertFalse((self.cible_home / ".config/ameesh/bascule.env").exists())
        self.assertFalse((self.cible_home / ".config/ameesh/git-agents.conf").exists())
        config = json.loads((self.cible_home / ".config/ameesh/config.json").read_text())
        self.assertNotIn(str(self.source_home), json.dumps(config))

    def test_compte_dans_docker_refuse(self):
        self.comptes["ameesh"] = M.Compte("ameesh", 1001, 1001,
                                          str(self.cible_home), "/bin/bash",
                                          ("ameesh", "docker"))
        with self.assertRaises(M.Refus):
            M.verifier_compte_cible(self.ctx)
        self.assertFalse(self.faux.a_appele("useradd"))

    def test_unites_obsoletes_jamais_copiees(self):
        M.preparer_unites(self.ctx)
        cible = self.cible_home / ".config/systemd/user"
        self.assertTrue((cible / "ameesh-runner-agent@.service").is_file())
        self.assertTrue((cible / "ameesh-agents.slice").is_file())
        self.assertFalse((cible / "ameesh-runner-agent@.service.d/"
                          "cles-owner.conf").exists())
        self.assertFalse((self.cible_home / ".config/ameesh/"
                          "git-agents.conf").exists())
        self.assertTrue(self.faux.a_appele("daemon-reload"))
        self.assertFalse(self.faux.a_appele("systemctl --user enable"))

    def test_creneau_refuse_une_coupure_proche(self):
        from datetime import datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
        except Exception:  # pragma: no cover
            self.skipTest("tzdata absent")
        fuseau = ZoneInfo("Europe/Paris")
        prochain = datetime.now(fuseau) + timedelta(minutes=10)
        self.faux.remplacer("release calendar", 0,
                            "prochaine %s, cible main"
                            % prochain.strftime("%d/%m %H:%M"))
        ok, detail = M.creneau(self.ctx)
        self.assertFalse(ok)
        self.assertIn("coupure dans", detail)

    def test_file_de_paquets_active_refusee(self):
        self.faux.remplacer("work queue show --target main --json", 0,
                            json.dumps([{"work_item_id": 42, "state": "queued"}]))
        ok, detail = M.paquets_inactifs(self.ctx)
        self.assertFalse(ok)
        self.assertIn("#42", detail)

    def test_commandes_claude_une_par_compte_sans_secret(self):
        config = M.lire_json(self.source_home / ".config/ameesh/config.json")
        self.faux.repondre("claude auth status", 0, '{"loggedIn": false}\n')
        commandes, manques = M.commandes_claude(self.ctx, config, creer=False)
        self.assertEqual(len(commandes), 2)
        self.assertTrue(all("CLAUDE_CONFIG_DIR=" in c for c in commandes))
        self.assertTrue(all("auth login" in c for c in commandes))
        self.assertTrue(manques)
        self.assertFalse(any("secret" in c for c in commandes))

    def test_retour_restaure_unites_dropins_et_pauses(self):
        # état d'une bascule réussie
        self.ctx.etat_dir.mkdir(parents=True, exist_ok=True)
        etat = M.etat_vide()
        etat.update({"stage": "bascule", "actifs": ["ameesh-dev1", "codex2"],
                     "enabled_source": ["ameesh-dev1", "codex2"],
                     "enabled_cible": ["ameesh-dev1", "codex2"],
                     "migre_le": "2026-10-25T10:00:00Z", "canari": "codex2",
                     "pauses": {}, "pieces_retirees": [
                         "config/ameesh/git-agents.conf"]})
        M.ecrit_etat(self.ctx, etat)
        sauvegarde = self.ctx.etat_dir / "sauvegarde/config/ameesh"
        sauvegarde.mkdir(parents=True)
        (sauvegarde / "git-agents.conf").write_text("[credential]\n")
        # la cible est active ; la source est arrêtée. L'état change après
        # stop/start, comme un vrai gestionnaire.
        etat = {"cible": "active", "source": "inactive"}

        delegue = self.faux.executer

        def systemctl(argv, env, delai):
            commande = " ".join(argv)
            self.faux.appels.append(tuple(argv))
            if "show" in commande and "ameesh-runner-agent@" in commande:
                cote = "cible" if env and env.get("HOME") == "/home/ameesh" \
                    else "source"
                actif, pid = etat[cote], "123" if etat[cote] == "active" else "0"
                return M.Resultat(0, "LoadState=loaded\nActiveState=%s\nMainPID=%s\n"
                                  "UnitFileState=enabled\n" % (actif, pid))
            if "systemctl --user stop" in commande:
                etat["cible"] = "inactive"
                return M.Resultat(0, "")
            if "systemctl --user start" in commande:
                etat["source"] = "active"
                return M.Resultat(0, "")
            if "daemon-reload" in commande or "list-units" in commande:
                return M.Resultat(0, "")
            return delegue(argv, env, delai)
        self.faux.executer = systemctl
        self.hote.executeur = systemctl
        # pauses : pose et levée OK
        for motif in ("ameesh pause", "ameesh resume"):
            self.faux.repondre(motif, 0, '{"changed": true, "reason": "x"}\n')
        self.faux.repondre("ameesh show", 0, json.dumps(
            {"last_turn_ts": 0, "lease_owner": None}))
        args = mock.Mock(force=False, attente_arret=5)
        with mock.patch.object(M, "agents_du_poste", return_value=[
                {"name": "ameesh-dev1", "harness": "claude", "status": "idle"},
                {"name": "codex2", "harness": "codex", "status": "idle"}]):
            with mock.patch.object(M, "canon_sync", return_value=(True, "ok")):
                rc = M.retour(self.ctx, args)
        self.assertEqual(rc, 0)
        self.assertTrue((self.source_home / ".config/ameesh/git-agents.conf").is_file())
        self.assertTrue(self.faux.a_appele("systemctl --user start"))
        self.assertTrue(self.faux.a_appele("systemctl --user disable"))
        self.assertTrue(self.faux.a_appele("systemctl --user enable"))
        self.assertEqual(M.lit_etat(self.ctx)["stage"], "retour")


    def test_bascule_canari_puis_reste_et_etat_final(self):
        # source active, cible inactive ; stop/start changent l'état
        etat_unites = {"source": "active", "cible": "inactive"}
        delegue = self.faux.executer
        appels = self.faux.appels

        def systemctl(argv, env, delai):
            commande = " ".join(argv)
            appels.append(tuple(argv))
            if "show" in commande and "ameesh-runner-agent@" in commande:
                cote = "cible" if env and env.get("HOME") == "/home/ameesh" \
                    else "source"
                actif = etat_unites[cote]
                return M.Resultat(0, "LoadState=loaded\nActiveState=%s\nMainPID=%s\n"
                                  "UnitFileState=enabled\n"
                                  % (actif, "123" if actif == "active" else "0"))
            if "systemctl --user stop" in commande:
                etat_unites["source"] = "inactive"
                return M.Resultat(0, "")
            if "systemctl --user start" in commande:
                etat_unites["cible"] = "active"
                return M.Resultat(0, "")
            if "daemon-reload" in commande or "list-units" in commande:
                return M.Resultat(0, "")
            return delegue(argv, env, delai)
        self.faux.executer = systemctl
        self.hote.executeur = systemctl
        for motif, sortie in (
                ("ameesh pause", '{"changed": true, "reason": "x"}\n'),
                ("ameesh resume", ""),
                ("ameesh show", json.dumps(
                    {"last_turn_ts": 0, "lease_owner": "runner-codex2@laptop",
                     "lease_expires_ts": 9999999999, "status": "idle"})),
                ("ameesh mail send", ""),
        ):
            self.faux.remplacer(motif, 0, sortie)
        self.faux.remplacer("systemctl --user show", 0, "")
        self.ctx.etat_dir.mkdir(parents=True, exist_ok=True)
        etat = M.etat_vide()
        etat["stage"] = "prepare"
        M.ecrit_etat(self.ctx, etat)
        args = mock.Mock(canari="codex2", attente_arret=5)
        agents = [{"name": "ameesh-dev1", "harness": "claude", "status": "idle"},
                  {"name": "codex2", "harness": "codex", "status": "idle"}]
        with mock.patch.object(M, "agents_du_poste", return_value=agents), \
                mock.patch.object(M, "verifier_avant_bascule", return_value=[]), \
                mock.patch.object(M, "canon_sync", return_value=(True, "ok")), \
                mock.patch.object(M, "verifier_cwd", return_value=[]), \
                mock.patch.object(M, "test_courrier",
                                  return_value=(True, "courrier test livré")), \
                mock.patch.object(M, "attendre_bail",
                                  return_value=(True, {"lease_owner":
                                                       "runner-codex2@laptop"})):
            rc = M.basculer(self.ctx, args)
        self.assertEqual(rc, 0)
        final = M.lit_etat(self.ctx)
        self.assertEqual(final["stage"], "bascule")
        self.assertIn("courrier test livré", final["preuve_courrier"])
        self.assertEqual(set(final["actifs"]), {"ameesh-dev1", "codex2"})
        self.assertTrue(self.faux.a_appele("systemctl --user stop"))
        self.assertTrue(self.faux.a_appele("systemctl --user start"))
        self.assertTrue(self.faux.a_appele("systemctl --user disable"))
        self.assertTrue(self.faux.a_appele("systemctl --user enable"))
        # les deux pièces retirées de la source, sauvegardées
        self.assertFalse((self.source_home / ".config/systemd/user/"
                          "ameesh-runner-agent@.service.d/cles-owner.conf").exists())
        self.assertTrue((self.ctx.etat_dir / "sauvegarde/.config/systemd/user/"
                         "ameesh-runner-agent@.service.d/cles-owner.conf").is_file())

    def test_ssh_owner_eacces_sous_ameesh(self):
        self.faux.repondre("python3 -c", 0, "EACCES\n")
        ok, detail = M.verifier_ssh_isole(self.ctx)
        self.assertTrue(ok, detail)
        self.assertIn("EACCES", detail)

    def test_docker_rend_la_preuve_rouge(self):
        self.comptes["ameesh"] = M.Compte("ameesh", 1001, 1001,
                                          str(self.cible_home), "/bin/bash",
                                          ("ameesh", "docker"))
        ok, detail = M.verifier_docker(self.ctx)
        self.assertFalse(ok)
        self.assertIn("docker", detail)

    def test_git_fetch_ssh_utilise_la_cle_du_home(self):
        self.faux.repondre("git fetch", 0, "")
        ok, detail = M.git_ssh_fetch(self.ctx)
        self.assertTrue(ok, detail)
        appels = [appel for appel in self.faux.appels if "fetch" in " ".join(appel)]
        self.assertTrue(appels)
        self.assertIn("git@github.com", " ".join(appels[0]))

    def test_obsolete_retiree_et_sauvegardee(self):
        etat = M.etat_vide()
        M.retirer_pieces_obsoletes(self.ctx, etat)
        self.assertFalse((self.source_home / ".config/systemd/user/"
                          "ameesh-runner-agent@.service.d/cles-owner.conf").exists())
        self.assertFalse((self.source_home / ".config/ameesh/git-agents.conf").exists())
        self.assertTrue((self.ctx.etat_dir / "sauvegarde/.config/systemd/user/"
                         "ameesh-runner-agent@.service.d/cles-owner.conf").is_file())
        self.assertIn("pieces_retirees", etat)


if __name__ == "__main__":
    unittest.main()

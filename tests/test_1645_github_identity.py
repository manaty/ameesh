# SPDX-License-Identifier: AGPL-3.0-only
"""#1645 — journal des gestes gh d'un tour et identité GitHub par rôle.

Sans base : `github_identity` ne lit que des fichiers (configuration, jeton,
journal) et l'environnement du tour.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from ameesh import github_identity as gi  # noqa: E402

#: un faux `gh` : écrit ses arguments sur la sortie et sort en 3
FAUX_GH = """#!/bin/sh
printf '%s\\n' "$*"
printf 'GH_TOKEN=%s\\n' "${GH_TOKEN:-}"
exit 3
"""
JETON = "ghp_" + "A" * 36


class Base(unittest.TestCase):
    def setUp(self):
        self.racine = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.racine, True)
        self.config = self.racine / "config"
        self.state = self.racine / "state"
        self.config.mkdir()
        self.state.mkdir()
        self.bin = self.racine / "vraibin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAUX_GH)
        gh.chmod(0o755)

    def ecrire_config(self, data: dict) -> None:
        (self.config / gi.CONFIG_NAME).write_text(json.dumps(data))

    def jeton(self, nom: str, mode: int = 0o600, contenu: str = JETON) -> str:
        chemin = self.racine / nom
        chemin.write_text(contenu + "\n")
        chemin.chmod(mode)
        return str(chemin)

    def env_tour(self, agent: str = "dev-x", lot: str | None = "1645") -> tuple[dict, dict]:
        env = {"PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
               "GITHUB_TOKEN": "hérité", "AGENT_MAIL_NAME": agent,
               "AMEESH_TURN_ID": "tour-1", "AMEESH_HOST": "hote"}
        info = gi.apply(env, agent, str(self.config), str(self.state), lot)
        return env, info


class ResolutionTest(Base):
    def test_sans_configuration_identite_heritee_et_journal_actif(self):
        info = gi.resolve(str(self.config), "dev-x")
        self.assertEqual(info["identity"], "inherited")
        self.assertEqual(info["role"], "")
        self.assertTrue(info["journal"])
        self.assertEqual(info["problem"], "")

    def test_role_nomme_prime_sur_le_role_par_defaut(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token")},
            "pilote": {"token_file": self.jeton("pilote.token"),
                       "agents": ["mesh-design"]}}})
        self.assertEqual(gi.resolve(str(self.config), "mesh-design")["role"], "pilote")
        self.assertEqual(gi.resolve(str(self.config), "dev-x")["role"], "agent")
        self.assertEqual(gi.resolve(str(self.config), "dev-x")["identity"], "role")

    def test_jeton_lisible_par_d_autres_comptes_refuse(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token", 0o644)}}})
        info = gi.resolve(str(self.config), "dev-x")
        self.assertEqual(info["identity"], "inherited")
        self.assertIn("0600", info["problem"])

    def test_configuration_illisible_garde_l_identite_heritee(self):
        (self.config / gi.CONFIG_NAME).write_text("{pas du json")
        info = gi.resolve(str(self.config), "dev-x")
        self.assertEqual(info["identity"], "inherited")
        self.assertIn("illisible", info["problem"])

    def test_la_resolution_ne_rend_jamais_le_jeton(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token")}}})
        self.assertNotIn(JETON, json.dumps(gi.resolve(str(self.config), "dev-x")))


class ApplicationTest(Base):
    def test_sans_role_l_environnement_d_identite_est_inchange(self):
        env, info = self.env_tour()
        self.assertEqual(env["GITHUB_TOKEN"], "hérité")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GH_CONFIG_DIR", env)
        self.assertNotIn("GIT_CONFIG_COUNT", env)

    def test_role_configure_pose_son_jeton_et_isole_gh_et_git(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token")}}})
        env = {"PATH": str(self.bin), "GITHUB_TOKEN": "hérité",
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.x",
               "GIT_CONFIG_VALUE_0": "1"}
        gi.apply(env, "dev-x", str(self.config), str(self.state))
        self.assertEqual(env["GH_TOKEN"], JETON)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env[gi.ENV_ROLE], "agent")
        self.assertTrue(os.path.isdir(env["GH_CONFIG_DIR"]))
        self.assertEqual(os.listdir(env["GH_CONFIG_DIR"]), [])
        # la clé existante est gardée ; la liste des assistants est vidée
        # pour github.com, puis l'assistant du rôle est ajouté
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.x")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "")
        self.assertIn("GH_TOKEN", env["GIT_CONFIG_VALUE_2"])
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "credential.https://github.com.helper")

    def test_assistant_git_rend_le_jeton_du_role(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token")}}})
        env, _ = self.env_tour()
        env["HOME"] = str(self.racine)  # aucune configuration git globale
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        sortie = subprocess.run(
            ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(sortie.returncode, 0, sortie.stderr)
        self.assertIn("password=%s" % JETON, sortie.stdout)
        self.assertIn("username=x-access-token", sortie.stdout)

    def test_journal_desactive_par_la_configuration(self):
        self.ecrire_config({"journal": False})
        env, _ = self.env_tour()
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.bin))
        self.assertNotIn(gi.ENV_JOURNAL, env)

    def test_sans_gh_sur_le_path_le_journal_est_sans_objet(self):
        env = {"PATH": str(self.racine / "vide")}
        info = gi.apply(env, "dev-x", str(self.config), str(self.state))
        self.assertEqual(env["PATH"], str(self.racine / "vide"))
        self.assertIn("gh absent", info["problem"])

    def test_echec_du_dossier_gh_config_laisse_l_identite_heritee(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token")}}})
        (self.state / "github").mkdir()
        (self.state / "github" / "gh-config").write_text("")  # pas un dossier
        env, info = self.env_tour()
        self.assertEqual(info["identity"], "inherited")
        self.assertIn("non posée", info["problem"])
        self.assertEqual(env["GITHUB_TOKEN"], "hérité")
        for cle in ("GH_TOKEN", "GH_CONFIG_DIR", gi.ENV_ROLE, "GIT_CONFIG_COUNT"):
            self.assertNotIn(cle, env)
        self.assertIn(gi.ENV_JOURNAL, env)  # le journal, lui, est posé

    def test_le_jeton_n_est_lu_qu_une_fois(self):
        fichier = self.jeton("agent.token")
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": fichier}}})
        lu = gi._read_token
        appels = []

        def une_fois(chemin):
            appels.append(chemin)
            jeton = lu(chemin)
            os.unlink(fichier)  # disparu juste après la lecture
            return jeton
        gi._read_token = une_fois
        self.addCleanup(setattr, gi, "_read_token", lu)
        env, info = self.env_tour()
        self.assertEqual(len(appels), 1)
        self.assertEqual((info["identity"], env["GH_TOKEN"]), ("role", JETON))

    def test_un_fifo_au_chemin_du_jeton_ne_bloque_pas(self):
        fifo = self.racine / "agent.token"
        os.mkfifo(fifo, 0o600)
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": str(fifo)}}})
        info = gi.resolve(str(self.config), "dev-x")
        self.assertEqual(info["identity"], "inherited")
        self.assertIn("fichier ordinaire", info["problem"])

    def test_configuration_mal_typee_ignoree_sans_exception(self):
        for data in ({"roles": []}, {"roles": {"agent": "x"}, "default_role": "agent"},
                     {"roles": {"agent": {"token_file": 3}}, "default_role": "agent"},
                     {"roles": {"../x": {"token_file": "f"}}, "default_role": "../x"},
                     {"roles": {"a": {"agents": "dev-x", "token_file": "f"}}},
                     {"roles": {}, "default_role": ["x"]}):
            self.ecrire_config(data)
            self.assertEqual(gi.resolve(str(self.config), "dev-x")["identity"], "inherited")

    def test_le_vrai_gh_exclut_le_dossier_genere(self):
        env, _ = self.env_tour()
        dossier = env["PATH"].split(os.pathsep)[0]
        # un second tour hérite d'un PATH qui contient déjà le gh généré
        self.assertEqual(gi.real_gh(env["PATH"], dossier), str(self.bin / "gh"))


class JournalTest(Base):
    def lancer(self, env: dict, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", *args], env=env, capture_output=True,
                              text=True, timeout=30)

    def test_le_geste_est_consigne_et_la_commande_inchangee(self):
        env, _ = self.env_tour(agent="dev-x", lot="1645")
        libre = "x" * 190 + " " + "ghp_" + "A" * 40  # coupé à 200 avant : fuite
        sortie = self.lancer(env, "run", "cancel", "38709406320", "--body", JETON, libre)
        # le vrai gh a tourné avec ses arguments intacts, code de sortie gardé
        self.assertEqual(sortie.returncode, 3)
        self.assertIn("run cancel 38709406320 --body %s %s" % (JETON, libre), sortie.stdout)
        gestes = gi.read_journal(str(self.state))
        self.assertEqual(len(gestes), 1)
        geste = gestes[0]
        self.assertEqual((geste["agent"], geste["lot"], geste["turn"], geste["host"]),
                         ("dev-x", "1645", "tour-1", "hote"))
        self.assertEqual(geste["argv"], ["run", "cancel", "38709406320", "--body",
                                         "[valeur]", "[jeton]"])
        self.assertNotIn("ghp_", json.dumps(geste))  # pas même un préfixe
        fichier = next((self.state / "github").glob("gestes-*.jsonl"))
        self.assertEqual(stat.S_IMODE(fichier.stat().st_mode), 0o600)

    def test_les_valeurs_libres_ne_sont_jamais_consignees(self):
        env, _ = self.env_tour()
        secret = "SYNTHETIC_SECRET_1645_ONLY"
        for args in (("secret", "set", "DEMO", "--body", secret),
                     ("secret", "set", "DEMO", "--body=" + secret),
                     ("secret", "set", "DEMO", "-b" + secret),
                     ("api", "-H", "Authorization: Bearer " + secret, "user"),
                     ("api", "-f", "x=ZZZghp_" + "B" * 36, "graphql"),
                     ("api", "repos/o/r/pulls?access_token=" + secret),
                     ("pr", "create", "--title", secret, "--fill"),
                     # R2 cx3 : une valeur commençant par un tiret reste la valeur
                     ("secret", "set", "DEMO", "--body", "-R" + secret),
                     ("secret", "set", "DEMO", "--body", "--head=" + secret),
                     # positionnel libre d'une commande sans identifiant
                     ("search", "code", secret),
                     # booléen connu, puis option inconnue : rien n'est consigné après
                     ("pr", "create", "--no-maintainer-edit", "--body", secret),
                     ("pr", "create", "--inconnue", "--body", secret),
                     ("pr", "create", "--inconnue", "--head=" + secret),
                     ("[commande]", secret),
                     ("pr", "inconnue", secret),
                     ("pr", "view", "--", secret),
                     # R3 cx3 : -c/--comment, texte de pr close, booléen de pr review
                     ("pr", "close", "1", "--comment", secret),
                     ("issue", "close", "1", "-c", secret),
                     ("pr", "close", "1", "--comment", "-R" + secret),
                     # -a/-r, booléens de pr review : --body ne fuit pas après eux
                     ("pr", "review", "1", "-a", "--body", secret),
                     ("pr", "review", "1", "-r", "-b", secret),
                     ("pr", "review", "1", "--comment", secret)):
            self.assertEqual(self.lancer(env, *args).returncode, 3)
        texte = (next((self.state / "github").glob("gestes-*.jsonl"))).read_text()
        self.assertEqual(len(texte.splitlines()), 22)
        self.assertNotIn(secret, texte)
        self.assertNotIn("ZZZ", texte)
        self.assertNotIn("Bearer", texte)

    def test_un_booleen_pris_pour_option_a_valeur_ne_decale_rien(self):
        # R4 cx3, qa-01 : `-a` est booléen pour `pr review`, `--body` y prend
        # donc `--head=…` comme corps libre ; de même pour un groupe `-ab`
        secret = "SYNTHETIC_PRIVATE_1645"
        for args in (["pr", "review", "1", "-a", "--body", "--head=" + secret],
                     ["pr", "review", "1", "-a", "--body", "-R" + secret],
                     ["pr", "review", "1", "-r", "-b", "--head=" + secret],
                     ["pr", "review", "1", "--comment", "--body", "-R" + secret],
                     ["pr", "create", "-d", "--body", "--head=" + secret],
                     ["pr", "merge", "12", "-m", "--body", "-R" + secret],
                     ["label", "create", "X", "-f", "--description", "--head=" + secret],
                     ["pr", "review", "1", "-ab", "--head=" + secret],
                     ["pr", "review", "1", "-ab", "--head", secret]):
            self.assertNotIn(secret, " ".join(gi.sanitize_argv(args)), args)

    def test_les_identifiants_utiles_restent_consignes(self):
        sha = "e4e90fa07bf4f2ead10ff6822057bc2fef37d6ab"
        args = ["pr", "merge", "1256", "--auto", "--squash", "--match-head-commit",
                sha, "-R", "manaty/ameesh"]
        self.assertEqual(gi.sanitize_argv(args), args)
        # --json est booléen de `workflow run` : sa valeur n'est jamais consignée
        self.assertEqual(gi.sanitize_argv(["pr", "view", "1", "--json", "state"]),
                         ["pr", "view", "1", "--json", "[valeur]"])
        # option d'identifiant hors commande d'identifiants : masquée
        self.assertEqual(gi.sanitize_argv(["secret", "set", "N", "--user", "x"]),
                         ["secret", "set", "[valeur]", "--user", "[valeur]"])
        self.assertEqual(gi.sanitize_argv(
            ["api", "repos/manaty/ameesh/actions/runs/1/cancel", "-X", "POST"]),
            ["api", "repos/manaty/ameesh/actions/runs/1/cancel", "-X", "POST"])
        self.assertEqual(gi.sanitize_argv(["api", "/repos/o/r/pulls?q=x"]),
                         ["api", "/repos/o/r/pulls?[valeur]"])
        # après la première option, plus aucun positionnel n'est consigné
        self.assertEqual(gi.sanitize_argv(
            ["api", "-X", "POST", "repos/manaty/ameesh/actions/runs/1/cancel"]),
            ["api", "-X", "POST", "[valeur]"])
        # une option inconnue : rien n'est consigné après elle
        self.assertEqual(gi.sanitize_argv(["run", "list", "--inconnue", "abc", "-R", "o/r"]),
                         ["run", "list", "[option]", "[valeur]", "[valeur]", "[valeur]"])
        self.assertEqual(gi.sanitize_argv(["secret", "set", "DEMO", "--repo", "o/r"]),
                         ["secret", "set", "[valeur]", "--repo", "[valeur]"])
        self.assertEqual(gi.sanitize_argv(["search", "code", "motif"]),
                         ["search", "code", "[valeur]"])

    def test_stderr_ferme_et_journal_impossible_la_commande_part(self):
        env, _ = self.env_tour()
        bloque = self.racine / "fichier"
        bloque.write_text("")
        env[gi.ENV_JOURNAL] = str(bloque / "sous" / "gestes.jsonl")
        sortie = subprocess.run(["sh", "-c", "exec 2>&-; exec gh pr view 1"], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(sortie.returncode, 3)
        self.assertIn("pr view 1", sortie.stdout)

    def test_le_role_est_consigne_et_le_jeton_transmis(self):
        self.ecrire_config({"default_role": "agent", "roles": {
            "agent": {"token_file": self.jeton("agent.token")}}})
        env, _ = self.env_tour()
        sortie = self.lancer(env, "pr", "merge", "12")
        self.assertIn("GH_TOKEN=%s" % JETON, sortie.stdout)
        self.assertEqual(gi.read_journal(str(self.state))[0]["role"], "agent")

    def test_un_journal_inscriptible_ne_bloque_jamais_la_commande(self):
        env, _ = self.env_tour()
        bloque = self.racine / "fichier"
        bloque.write_text("")
        env[gi.ENV_JOURNAL] = str(bloque / "sous" / "gestes.jsonl")
        sortie = self.lancer(env, "pr", "view", "1")
        self.assertEqual(sortie.returncode, 3)
        self.assertIn("pr view 1", sortie.stdout)
        self.assertIn("journal gh non écrit", sortie.stderr)

    def test_lecture_filtree_par_agent_motif_et_periode(self):
        dossier = self.state / "github"
        dossier.mkdir()
        maintenant = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)
        lignes = [
            {"ts": "2026-10-15T11:50:00+0000", "agent": "a", "argv": ["run", "cancel", "9"]},
            {"ts": "2026-10-15T11:55:00+0000", "agent": "b", "argv": ["pr", "merge", "3"]},
            {"ts": "2026-10-14T08:00:00+0000", "agent": "a", "argv": ["run", "cancel", "8"]},
        ]
        (dossier / "gestes-2026-10.jsonl").write_text(
            "".join(json.dumps(l) + "\n" for l in lignes) + "{ligne coupée\n")
        lire = lambda **k: gi.read_journal(str(self.state), now=maintenant, **k)  # noqa: E731
        self.assertEqual(len(lire()), 3)
        self.assertEqual([g["argv"][2] for g in lire(match="run cancel")], ["8", "9"])
        self.assertEqual([g["argv"][2] for g in lire(since=datetime.timedelta(hours=1))],
                         ["9", "3"])
        self.assertEqual([g["agent"] for g in lire(agent="b")], ["b"])


class ExecuteurTest(Base):
    """Le câblage : une session `attach` reçoit le journal comme un tour."""

    def test_session_attachee_journalisee(self):
        from unittest import mock
        from ameesh import runner
        from ameesh.config import Config
        cfg = Config(state_dir=str(self.state), config_dir=str(self.config))
        with mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin"}):
            env = runner._attach_env(cfg, "dev-x", "attach:u@h", 3)
        self.assertEqual(env[gi.ENV_REAL], str(self.bin / "gh"))
        self.assertTrue(env["PATH"].startswith(str(self.state / "github" / "bin")))
        self.assertEqual(env["AGENT_MAIL_NAME"], "dev-x")


if __name__ == "__main__":
    unittest.main()

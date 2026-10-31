# SPDX-License-Identifier: AGPL-3.0-only
"""#1604 — chemin rapide de `agent-mail hook` / `agent-mail statusline`.

Le hook tourne après CHAQUE outil : quand la boîte locale dit qu'il n'y a rien
à faire, il ne doit ni importer `ameesh.cli` (base, psycopg) ni ouvrir la base.
Ce que ces tests protègent :

* la décision rapide (marqueur frais) et le retour au chemin complet dès que le
  marqueur est absent, périmé ou dit « à vérifier » ;
* la livraison : Stop, SessionStart et UserPromptSubmit passent TOUJOURS par la
  base, et un courrier en attente est remis même avec un marqueur frais ;
* le marqueur lui-même : posé après un passage complet, invalidé par un envoi
  local, et un cache de barre d'état de quelques secondes ;
* le coût d'import : le vrai sous-processus rapide ne charge ni `ameesh.cli`,
  ni le backend, ni psycopg. Le CPU se mesure après installation (protocole
  du lot #1604), sans seuil dépendant de la machine dans la CI (#1785).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import fastmail, mail, registry, session_bindings
from ameesh.backend import FileBackend

from .support import PgTestCase

#: le code d'un sous-processus qui exécute l'entrée `agent-mail` et dit ce qui
#: a été importé — la preuve structurelle du chemin rapide.
SONDE = (
    "import sys; sys.argv = ['agent-mail', 'hook', 'claude'];"
    "import ameesh.entry as e; rc = e.main();"
    "print('rc', rc);"
    "print('cli' if 'ameesh.cli' in sys.modules else 'rapide');"
    "print('backend' if 'ameesh.backend' in sys.modules else 'sans-base');"
    "print('psycopg' if 'psycopg' in sys.modules else 'sans-psycopg')"
)


class LeasedFastHookTest(unittest.TestCase):
    def test_runner_hook_uses_the_full_hooks_l166_registration_key(self):
        with tempfile.TemporaryDirectory() as state:
            cfg = fastmail.Config(state_dir=state, backend="pg")
            env = {"AGENT_MAIL_NAME": "alpha", "AMEESH_RUNNER_ID": "runner-1",
                   "AMEESH_LEASE_EPOCH": "7", "AMEESH_HARNESS": "claude"}
            with mock.patch.dict(os.environ, env, clear=True):
                # L166: cmd_hook registers None, leaving the runner stream
                # responsible for the authoritative session ID.
                fastmail.register_done(cfg, "alpha",
                                       ["claude", state, None, "runner-1", 7])
                fastmail.note(cfg, "alpha", pending=False)
                for session in ("main-session", "child-session"):
                    raw = json.dumps({"hook_event_name": "PostToolUse",
                                      "cwd": state, "session_id": session})
                    with self.subTest(session=session):
                        self.assertTrue(fastmail.hook_rapide(cfg, "claude", raw))
                # A different lease must still go through the full hook.
                os.environ["AMEESH_LEASE_EPOCH"] = "8"
                self.assertFalse(fastmail.hook_rapide(cfg, "claude", raw))


class EntreeRendStdinTest(unittest.TestCase):
    """#1689 (N3) : le JSON lu par le chemin rapide est rendu au chemin
    complet, à l'octet près, même illisible ou pas un objet."""

    def test_stdin_rendu_tel_quel(self):
        from ameesh import entry

        cfg = fastmail.Config(state_dir=tempfile.mkdtemp())
        for raw in ("{pas du json", "[1, 2]", ""):
            with self.subTest(raw=raw):
                with mock.patch.object(sys, "stdin", io.StringIO(raw)):
                    self.assertIsNone(entry._rapide_statusline(cfg, fastmail, []))
                    self.assertEqual(sys.stdin.read(), raw)


class HookRapideTest(PgTestCase):
    """#1604 : hook/statusline sans base quand il n'y a rien à faire."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)

    # -- utilitaires -------------------------------------------------------
    def entry(self, *args: str, env: dict | None = None, stdin: str | None = None,
              timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.entry", *args],
            input=stdin, capture_output=True, text=True, timeout=timeout,
            env=env or self.env(), cwd=self.tmp,
        )

    def _posttooluse(self, session: str = "sess-1") -> str:
        return json.dumps({"hook_event_name": "PostToolUse", "cwd": self.tmp,
                           "session_id": session})

    def _marqueur_frais(self, session: str = "sess-1", pending: bool = False) -> dict:
        """Le marqueur et l'inscription qu'un passage complet vient d'écrire."""
        fastmail.register_done(self.cfg, "alpha",
                               ["claude", self.tmp, session, None, None])
        fastmail.note(self.cfg, "alpha", pending=pending, unread=1 if pending else 0)
        return self.env(AGENT_MAIL_NAME="alpha", AMEESH_HOOK_FAST_TTL="600")

    def _send(self, sender: str = "beta", text: str = "un message") -> None:
        proc = self.cli("send", "alpha", text, env=self.env(AGENT_MAIL_NAME=sender))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _livre(self) -> bool:
        return bool(self.db.query(
            "SELECT 1 FROM agent_mailbox WHERE recipient = %s AND delivered_at IS NOT NULL",
            ("alpha",)))

    # -- décision rapide ---------------------------------------------------
    def test_posttooluse_sans_base_quand_rien_a_faire(self):
        env = self._marqueur_frais()
        env["INVOCATION_ID"] = "inherited-from-runner"
        garde = (
            "import sys\n"
            "def refuser(event, args):\n"
            "    if event in ('socket.connect', 'subprocess.Popen'):\n"
            "        raise AssertionError('chemin rapide : ' + event)\n"
            "sys.addaudithook(refuser)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", garde + SONDE], input=self._posttooluse(),
            capture_output=True, text=True, timeout=60, env=env, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(),
                         ["rc 0", "rapide", "sans-base", "sans-psycopg"])
        self.assertEqual(proc.stderr, "")

    def test_marqueur_absent_ou_perime_repasse_par_la_base(self):
        for etat in ("absent", "perime"):
            with self.subTest(etat=etat):
                env = self.env(AGENT_MAIL_NAME="alpha", AMEESH_HOOK_FAST_TTL="600")
                fastmail.register_done(self.cfg, "alpha",
                                       ["claude", self.tmp, "sess-1", None, None])
                if etat == "absent":
                    if os.path.exists(fastmail.marker_path(self.cfg, "alpha")):
                        os.unlink(fastmail.marker_path(self.cfg, "alpha"))
                else:
                    fastmail.note(self.cfg, "alpha", pending=False, unread=0,
                                  maintenant=time.time() - 3600)
                proc = subprocess.run(
                    [sys.executable, "-c", SONDE], input=self._posttooluse(),
                    capture_output=True, text=True, timeout=60, env=env, cwd=self.tmp)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("cli", proc.stdout.split())

    def test_inscription_due_repasse_par_la_base(self):
        self._marqueur_frais()
        fastmail.register_done(self.cfg, "alpha",
                               ["claude", self.tmp, "sess-1", None, None])
        # une clé d'inscription différente (session neuve) : chemin complet
        proc = subprocess.run(
            [sys.executable, "-c", SONDE],
            input=json.dumps({"hook_event_name": "PostToolUse", "cwd": self.tmp,
                              "session_id": "sess-2"}),
            capture_output=True, text=True, timeout=60,
            env=self.env(AGENT_MAIL_NAME="alpha", AMEESH_HOOK_FAST_TTL="600"),
            cwd=self.tmp)
        self.assertIn("cli", proc.stdout.split())

    # -- la livraison ne change pas ---------------------------------------
    def test_stop_livre_meme_avec_un_marqueur_frais(self):
        self._send("beta", "un message pour alpha")
        env = self._marqueur_frais()
        payload = json.dumps({"hook_event_name": "Stop", "cwd": self.tmp,
                              "session_id": "sess-1"})
        proc = self.entry("hook", "claude", env=env, stdin=payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reponse = json.loads(proc.stdout)
        self.assertEqual(reponse["decision"], "block")
        self.assertIn("un message pour alpha", reponse["reason"])
        self.assertTrue(self._livre(), "le Stop doit remettre le courrier")

    def test_posttooluse_livre_si_le_marqueur_dit_a_verifier(self):
        self._send("gamma", "courrier a verifier")
        env = self._marqueur_frais(pending=True)
        proc = self.entry("hook", "claude", env=env, stdin=self._posttooluse())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reponse = json.loads(proc.stdout)
        self.assertIn("courrier a verifier",
                      reponse["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self._livre())

    def test_le_chemin_complet_repose_le_marqueur(self):
        """Après une remise, le marqueur autorise le chemin rapide suivant."""
        self._send("beta", "un message")
        env = self._marqueur_frais(pending=True)
        proc = self.entry("hook", "claude", env=env, stdin=self._posttooluse())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marque = fastmail.read_marker(self.cfg, "alpha")
        self.assertIsNotNone(marque)
        self.assertFalse(marque["pending"])
        self.assertEqual(marque["unread"], 0)
        # le hook suivant, servilement rapide, ne charge plus rien
        proc2 = subprocess.run(
            [sys.executable, "-c", SONDE], input=self._posttooluse(),
            capture_output=True, text=True, timeout=60, env=env, cwd=self.tmp)
        self.assertIn("rapide", proc2.stdout.split())
        self.assertIn("sans-base", proc2.stdout.split())

    def test_envoi_local_invalide_le_marqueur(self):
        fastmail.note(self.cfg, "alpha", pending=False, unread=0)
        self._send("beta", "du courrier neuf")
        marque = fastmail.read_marker(self.cfg, "alpha")
        self.assertTrue(marque["pending"])

    # -- #1689 : les quatre N de la relecture Claude -----------------------
    def test_envoi_par_la_base_invalide_le_marqueur(self):
        """N1 : tout dépôt local réveille le chemin rapide, pas seulement
        `agent-mail send` (approbations, exécuteur, veille passent par là)."""
        fastmail.note(self.cfg, "alpha", pending=False, unread=0)
        mail.send(self.db, "beta", "alpha", "un message déposé par la base")
        self.assertTrue(fastmail.read_marker(self.cfg, "alpha")["pending"])

    def test_envoi_repli_fichier_invalide_le_marqueur(self):
        """N1 (repli fichier) : un envoi direct et sa copie réveillent le
        chemin rapide sans passer par la CLI."""
        fastmail.note(self.cfg, "alpha", pending=False, unread=0)
        fastmail.note(self.cfg, "gamma", pending=False, unread=0)
        FileBackend(self.cfg).send("beta", "alpha", "un message", cc=["gamma"])
        self.assertTrue(fastmail.read_marker(self.cfg, "alpha")["pending"])
        self.assertTrue(fastmail.read_marker(self.cfg, "gamma")["pending"])

    def test_identite_explicite_sous_bail_garde_le_diagnostic(self):
        """N2 : le refus L46 précède l'inscription. Sinon le premier hook
        tamponnait la clé de ce shell et, marqueur frais, le chemin rapide
        taisait « driven by the runner » sur les PostToolUse suivants."""
        registry.upsert(self.db, "alpha", harness="claude", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner-x', lease_epoch = 1, "
            "lease_expires_at = now() + interval '1 hour' WHERE name = 'alpha'")
        fastmail.note(self.cfg, "alpha", pending=False, unread=0)
        env = self.env(AGENT_MAIL_NAME="alpha", AMEESH_HOOK_FAST_TTL="600")
        for tour in (1, 2):
            proc = self.entry("hook", "claude", env=env, stdin=self._posttooluse())
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("is driven by the runner", proc.stderr, "tour %d" % tour)
            self.assertEqual(proc.stdout, "", "tour %d" % tour)
        # rien n'a été inscrit pour ce shell : le refus se redit à chaque hook
        self.assertTrue(fastmail.register_due(
            self.cfg, "alpha", ["claude", self.tmp, "sess-1", None, None]))

    def test_liaison_locale_invalidee_au_bind_et_au_unbind(self):
        """N4 : le cache local (600 s) ne garde pas l'ancien nom après une
        liaison qui change."""
        chemin = fastmail.liaison_path(self.cfg, "claude", "sess-1")
        fastmail.remember_liaison(self.cfg, "alpha", "claude", "sess-1")
        self.assertTrue(os.path.exists(chemin))
        registry.upsert_unleased(self.db, "beta", harness="claude", host=self.cfg.host)
        session_bindings.bind(self.cfg, self.db, "beta", session_id="sess-1",
                              harness="claude", by="test")
        self.assertFalse(os.path.exists(chemin), "le bind doit invalider le cache")
        fastmail.remember_liaison(self.cfg, "beta", "claude", "sess-1")
        self.assertTrue(os.path.exists(chemin))
        session_bindings.unbind(self.cfg, self.db, session_id="sess-1",
                                harness="claude", by="test")
        self.assertFalse(os.path.exists(chemin), "le unbind doit invalider le cache")

    def test_entree_et_cli_rendent_le_meme_json(self):
        """Le chemin complet garde le JSON à l'octet près (compatibilité)."""
        # instant fixe : les deux rendus portent la même heure (jamais d'horloge
        # dans la comparaison)
        ts = 1770000000.0
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, created_at) "
            "VALUES (%s, %s, %s, to_timestamp(%s))", ("beta", "alpha", "même texte", ts))
        fastmail.note(self.cfg, "alpha", pending=True, unread=1)
        fastmail.register_done(self.cfg, "alpha",
                               ["claude", self.tmp, "sess-1", None, None])
        env = self.env(AGENT_MAIL_NAME="alpha")
        payload = self._posttooluse()
        direct = self.cli("hook", "claude", env=env, stdin=payload)
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, created_at) "
            "VALUES (%s, %s, %s, to_timestamp(%s))", ("beta", "alpha", "même texte", ts))
        fastmail.note(self.cfg, "alpha", pending=True, unread=1)
        entree = self.entry("hook", "claude", env=env, stdin=payload)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(entree.returncode, 0, entree.stderr)
        self.assertEqual(json.loads(direct.stdout), json.loads(entree.stdout))

    # -- barre d'état ------------------------------------------------------
    def test_statusline_en_cache_quelques_secondes(self):
        dossier = os.path.join(self.tmp, "sl")
        os.makedirs(dossier, exist_ok=True)
        self.cli("alias", "alpha", dossier, "chantier")
        self._send("beta", "un message")
        payload = json.dumps({"workspace": {"current_dir": dossier}})
        env = self.env(AGENT_MAIL_NAME="alpha")
        premier = self.entry("statusline", env=env, stdin=payload)
        self.assertEqual(premier.returncode, 0, premier.stderr)
        self.assertIn("✉ 1", premier.stdout)
        self.db.execute("DELETE FROM agent_mailbox")
        # dans la fenêtre du cache : la ligne n'est pas recalculée
        second = self.entry("statusline", env=env, stdin=payload)
        self.assertEqual(second.stdout, premier.stdout)
        # TTL nul : le rendu revient à la base et le courrier a disparu
        frais = self.entry("statusline", stdin=payload,
                           env=self.env(AGENT_MAIL_NAME="alpha",
                                        AMEESH_STATUSLINE_TTL="0"))
        self.assertEqual(frais.returncode, 0, frais.stderr)
        self.assertNotIn("✉", frais.stdout)

    # -- suites de la relecture (#1628) ------------------------------------

    def test_statusline_en_cache_sans_spawn_avec_invocation_heritee(self):
        payload = json.dumps({"workspace": {"current_dir": self.tmp}})
        env = self.env(AGENT_MAIL_NAME="alpha", INVOCATION_ID="inherited-from-runner")
        first = self.entry("statusline", env=env, stdin=payload)
        self.assertEqual(first.returncode, 0, first.stderr)
        guard = (
            "import sys\n"
            "def refuse(event, args):\n"
            "    if event in ('socket.connect', 'subprocess.Popen'):\n"
            "        raise AssertionError('cached statusline: ' + event)\n"
            "sys.addaudithook(refuse)\n"
        )
        probe = SONDE.replace("['agent-mail', 'hook', 'claude']",
                              "['agent-mail', 'statusline']")
        cached = subprocess.run([sys.executable, "-c", guard + probe],
                                input=payload, capture_output=True, text=True,
                                timeout=60, env=env, cwd=self.tmp)
        self.assertEqual(cached.returncode, 0, cached.stderr)
        self.assertEqual(cached.stdout, first.stdout +
                         "rc 0\nrapide\nsans-base\nsans-psycopg\n")
        self.assertEqual(cached.stderr, "")

    def test_statusline_ne_partage_pas_la_ligne_entre_deux_agents(self):
        """#1628 N1 : sans session_id, deux sessions d'un même dossier ne
        partagent plus la ligne en cache."""
        dossier = os.path.join(self.tmp, "sl-1628")
        os.makedirs(dossier, exist_ok=True)
        payload = json.dumps({"workspace": {"current_dir": dossier}})
        alpha = self.entry("statusline", env=self.env(AGENT_MAIL_NAME="alpha"),
                           stdin=payload)
        self.assertEqual(alpha.returncode, 0, alpha.stderr)
        self.assertIn("alpha", alpha.stdout)
        # le second appel est dans la fenêtre du cache : sans le nom dans la
        # clé, il rendrait la ligne d'alpha (l'ancien défaut)
        beta = self.entry("statusline", env=self.env(AGENT_MAIL_NAME="beta"),
                          stdin=payload)
        self.assertEqual(beta.returncode, 0, beta.stderr)
        self.assertIn("beta", beta.stdout)
        self.assertNotIn("alpha", beta.stdout)

    def test_cle_de_statusline_departage_agents_et_sessions(self):
        """#1628 N1 : nom d'agent et transcript_path entrent dans la clé."""
        base = {"workspace": {"current_dir": self.tmp}}
        cles = {
            fastmail.statusline_key(dict(base, session_id="s1"), "alpha"),
            fastmail.statusline_key(dict(base), "beta"),
            fastmail.statusline_key(dict(base, transcript_path="/t/1"), "alpha"),
            fastmail.statusline_key(dict(base, transcript_path="/t/2"), "alpha"),
        }
        self.assertEqual(len(cles), 4)
        # même session, même nom : même clé (le cache fonctionne toujours)
        self.assertEqual(fastmail.statusline_key(dict(base, session_id="s1"), "alpha"),
                         fastmail.statusline_key(dict(base, session_id="s1"), "alpha"))

    def test_marqueur_pose_meme_appele_depuis_un_except(self):
        """#1628 N2 : le marqueur ne dépend plus de l'exception extérieure."""
        env = self.env(AGENT_MAIL_NAME="alpha")
        charge = json.dumps({"hook_event_name": "PostToolUse", "cwd": self.tmp,
                             "session_id": "sess-exc"})
        code = (
            "import io, sys\n"
            "sys.argv = ['agent-mail', 'hook', 'claude']\n"
            "sys.stdin = io.StringIO(%r)\n"
            "from ameesh import cli\n"
            "try:\n"
            "    raise RuntimeError('exterieure')\n"
            "except RuntimeError:\n"
            "    rc = cli.main()\n"
            "print('rc', rc)\n"
        ) % charge
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60, env=env, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rc 0", proc.stdout)
        marque = fastmail.read_marker(self.cfg, "alpha")
        self.assertIsNotNone(marque, "un appel depuis un except ne doit pas "
                                     "empêcher le marqueur")
        self.assertFalse(marque["pending"])

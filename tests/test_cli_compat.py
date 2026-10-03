"""Compatibilité v0 : commandes et surtout JSON des hooks, à l'octet près."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from agent_mesh import cli

from .support import REPO, PgTestCase

TS = 1770000000.0  # instant fixe, pour que v0 et v1 rendent la même heure


class CliCompatTest(PgTestCase):
    # -- helpers -----------------------------------------------------------
    def _seed(self, name: str, sender: str, text: str, ts: float = TS,
              reset: bool = True) -> None:
        """Le même message dans la boîte v0 (fichier) et dans Postgres.

        `reset=False` garde les compteurs de relance (test du plafond Stop).
        """
        if reset:
            shutil.rmtree(os.path.join(self.state, "hooks"), ignore_errors=True)
            shutil.rmtree(os.path.join(self.v0state, "agents"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.v0state, "inbox", name), ignore_errors=True)
        inbox = os.path.join(self.v0state, "inbox", name)
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "%d-%s-1.json" % (int(ts * 1000), sender)),
                  "w", encoding="utf-8") as fh:
            json.dump({"from": sender, "to": name, "ts": ts, "text": text}, fh)
        self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body, created_at) "
            "VALUES (%s, %s, %s, to_timestamp(%s)) RETURNING id",
            (sender, name, text, ts),
        )

    def _hook_pair(self, event: str, name: str, text: str = "bonjour alpha") -> tuple:
        self._seed(name, "beta", text)
        payload = json.dumps({"hook_event_name": event, "cwd": self.tmp})
        env = self.env(AGENT_MAIL_NAME=name)
        v0 = subprocess.run(
            [sys.executable, os.path.join(REPO, "agent-mail.v0.py"), "hook", "claude"],
            input=payload, capture_output=True, text=True, env=env,
        )
        v1 = self.cli("hook", "claude", env=env, stdin=payload)
        return v0, v1

    # -- hooks -------------------------------------------------------------
    def test_hook_sessionstart_json_identique_a_la_v0(self):
        v0, v1 = self._hook_pair("SessionStart", "alpha")
        self.assertEqual(v0.returncode, 0)
        self.assertEqual(v1.returncode, 0)
        self.assertEqual(json.loads(v0.stdout), json.loads(v1.stdout))
        self.assertEqual(len(json.loads(v1.stdout)["hookSpecificOutput"]["additionalContext"]),
                         len(json.loads(v0.stdout)["hookSpecificOutput"]["additionalContext"]))

    def test_hook_userpromptsubmit_et_posttooluse_identiques(self):
        for event, name in (("UserPromptSubmit", "bravo"), ("PostToolUse", "charlie")):
            with self.subTest(event=event):
                v0, v1 = self._hook_pair(event, name, text="message pour %s" % name)
                self.assertEqual(json.loads(v0.stdout), json.loads(v1.stdout))
                self.assertEqual(v1.returncode, 0)

    def test_hook_stop_identique_a_la_v0(self):
        v0, v1 = self._hook_pair("Stop", "delta")
        self.assertEqual(json.loads(v0.stdout), json.loads(v1.stdout))
        self.assertEqual(json.loads(v1.stdout)["decision"], "block")

    def test_hook_marque_les_messages_remis(self):
        self._seed("echo", "beta", "à remettre")
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": self.tmp})
        self.cli("hook", "codex", env=self.env(AGENT_MAIL_NAME="echo"), stdin=payload)
        restant = self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = 'echo' AND delivered_at IS NULL"
        )[0]["n"]
        self.assertEqual(restant, 0)

    def test_hook_sans_message_est_silencieux(self):
        for event in ("SessionStart", "Stop", "UserPromptSubmit", "PostToolUse"):
            with self.subTest(event=event):
                payload = json.dumps({"hook_event_name": event, "cwd": self.tmp})
                proc = self.cli("hook", "claude", env=self.env(AGENT_MAIL_NAME="foxtrot"),
                                stdin=payload)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_hook_entree_invalide_est_silencieux(self):
        for stdin in ("", "pas du json", "{}", '{"hook_event_name": "Inconnu"}'):
            with self.subTest(stdin=stdin):
                proc = self.cli("hook", "deepseek", env=self.env(AGENT_MAIL_NAME="golf"),
                                stdin=stdin)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_hook_ne_consomme_pas_si_l_ecriture_echoue(self):
        """Sonde codex3 (7) : un harnais qui ferme son entrée ne perd pas le message."""
        name = "kilo"
        self._seed(name, "beta", "message à ne pas perdre")

        class _Broken(io.TextIOBase):
            def write(self, _data):
                raise BrokenPipeError("harnais parti")

            def flush(self):
                raise BrokenPipeError("harnais parti")

            def close(self):
                pass  # le finaliseur ne doit pas relancer l'exception

        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": self.tmp})
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": name}), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(sys, "stdout", _Broken()):
            self.assertEqual(cli.cmd_hook(self.cfg, "claude"), 0)
        restant = self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = %s AND delivered_at IS NULL", (name,))[0]["n"]
        self.assertEqual(restant, 1, "le message ne doit pas être marqué remis")

        # écriture normale : le message est bien remis
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": self.tmp})
        proc = self.cli("hook", "claude", env=self.env(AGENT_MAIL_NAME=name), stdin=payload)
        self.assertEqual(proc.returncode, 0)
        restant = self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = %s AND delivered_at IS NULL", (name,))[0]["n"]
        self.assertEqual(restant, 0)

    def test_hook_stop_plafonne_a_trois_relances(self):
        """Comme la v0 : au plus 3 relances forcées d'affilée, puis silence."""
        name = "hotel"
        payload = json.dumps({"hook_event_name": "Stop", "cwd": self.tmp})
        env = self.env(AGENT_MAIL_NAME=name)
        for tour in range(3):
            self._seed(name, "beta", "relance %d" % tour, reset=(tour == 0))
            proc = self.cli("hook", "claude", env=env, stdin=payload)
            self.assertEqual(json.loads(proc.stdout)["decision"], "block", "tour %d" % tour)
        self._seed(name, "beta", "relance de trop", reset=False)
        proc = self.cli("hook", "claude", env=env, stdin=payload)
        self.assertEqual(proc.stdout, "")
        # un prompt utilisateur remet le compteur à zéro
        self.cli("hook", "claude", env=env,
                 stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": self.tmp}))
        self._seed(name, "beta", "après reset", reset=False)
        proc = self.cli("hook", "claude", env=env, stdin=payload)
        self.assertEqual(json.loads(proc.stdout)["decision"], "block")

    # -- commandes ---------------------------------------------------------
    def test_whoami_alias_et_status(self):
        dossier = os.path.join(self.tmp, "worktrees", "deepseek7")
        os.makedirs(dossier, exist_ok=True)
        proc = self.cli("alias", "deepseek7", dossier, "nexlink")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("alias deepseek7 → %s" % os.path.realpath(dossier), proc.stdout)
        with open(os.path.join(self.conf, "aliases.tsv"), encoding="utf-8") as fh:
            self.assertIn("deepseek7\t%s\tnexlink" % os.path.realpath(dossier), fh.read())

        proc = self.cli("whoami", "--cwd", dossier)
        self.assertEqual(proc.stdout.strip(), "deepseek7")

        proc = self.cli("status", "banc v1", env=self.env(AGENT_MAIL_NAME="deepseek7"))
        self.assertEqual(proc.stdout.strip(), "[nexlink/deepseek7] banc v1")
        row = self.db.query(
            "SELECT status_text FROM agent_registry WHERE name = 'deepseek7'"
        )[0]
        self.assertEqual(row["status_text"], "banc v1")

    def test_send_inbox_list(self):
        proc = self.cli("send", "alpha", "bonjour alpha", env=self.env(AGENT_MAIL_NAME="beta"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "déposé pour : alpha")

        proc = self.cli("inbox", "alpha")
        self.assertEqual(proc.stdout.strip(), "de beta : bonjour alpha")

        proc = self.cli("list")
        ligne = [l for l in proc.stdout.splitlines() if l.startswith("alpha")][0]
        self.assertIn("non lus:1", ligne)
        self.assertIn("@", ligne)

        # inbox ne marque pas lu (comme la v0)
        self.assertEqual(len(self.cli("inbox", "alpha").stdout.strip().splitlines()), 1)

    def test_send_invalide(self):
        for args in (["send", "alpha"], ["send", "mauvais nom!", "texte"],
                     ["send", "alpha", "   "]):
            with self.subTest(args=args):
                proc = self.cli(*args, env=self.env(AGENT_MAIL_NAME="beta"))
                self.assertEqual(proc.returncode, 2)

    def test_statusline(self):
        dossier = os.path.join(self.tmp, "sl")
        os.makedirs(dossier, exist_ok=True)
        self.cli("alias", "india", dossier, "chantier")
        self.cli("send", "india", "un message", env=self.env(AGENT_MAIL_NAME="beta"))
        payload = json.dumps({"workspace": {"current_dir": dossier}})
        proc = self.cli("statusline", stdin=payload)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("[chantier/india]", proc.stdout)
        self.assertIn("✉ 1", proc.stdout)


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: AGPL-3.0-only
"""Repli lisible quand la base est absente : les agents continuent de se parler."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from .support import REPO, PgTestCase

#: port fermé sur la boucle locale : la base est « absente »
UNREACHABLE = "postgresql://ameesh:ameesh@127.0.0.1:5599/ameesh"
TS = 1770000000.0


class FallbackTest(PgTestCase):
    def env_bad(self, **extra: str) -> dict:
        return self.env(
            AMEESH_DSN=UNREACHABLE, AMEESH_CONNECT_TIMEOUT="1", **extra
        )

    def test_send_replie_et_previent(self):
        proc = self.cli("send", "beta", "via les fichiers",
                        env=self.env_bad(AGENT_MAIL_NAME="alpha"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "déposé pour : beta")
        self.assertIn("repli sur les fichiers", proc.stderr)
        self.assertIn(UNREACHABLE.split("@")[1], proc.stderr)  # le DSN est montré
        fichiers = os.listdir(os.path.join(self.v0state, "inbox", "beta"))
        self.assertEqual(len(fichiers), 1)
        with open(os.path.join(self.v0state, "inbox", "beta", fichiers[0]),
                  encoding="utf-8") as fh:
            message = json.load(fh)
        self.assertEqual(message["from"], "alpha")
        self.assertEqual(message["text"], "via les fichiers")

    def test_inbox_list_et_hook_sur_le_repli(self):
        # un message déposé pendant que la base est absente
        self.cli("send", "beta", "coucou", env=self.env_bad(AGENT_MAIL_NAME="alpha"))
        proc = self.cli("inbox", "beta", env=self.env_bad())
        self.assertEqual(proc.stdout.strip(), "de alpha : coucou")

        # le hook enregistre l'agent dans l'état fichier v0
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": self.tmp,
                              "session_id": "sess-file"})
        hook = self.cli("hook", "claude", env=self.env_bad(AGENT_MAIL_NAME="beta"),
                        stdin=payload)
        self.assertEqual(hook.returncode, 0)
        self.assertEqual(json.loads(hook.stdout)["hookSpecificOutput"]["hookEventName"],
                         "SessionStart")
        with open(os.path.join(self.v0state, "agents", "beta.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["session_id"], "sess-file")

        proc = self.cli("list", env=self.env_bad())
        self.assertIn("beta", proc.stdout)
        # en repli, le hook reste silencieux : aucun avertissement sur stdout
        self.assertNotIn("repli", hook.stdout)

    def test_hook_json_identique_v0_en_repli(self):
        """Le repli fichier de la v1 rend exactement le JSON de la v0."""
        name = "delta"
        v0state = os.path.join(self.tmp, "v0-only")
        v1state = os.path.join(self.tmp, "v1-only")
        for state, suffixe in ((v0state, 1), (v1state, 2)):
            inbox = os.path.join(state, "inbox", name)
            os.makedirs(inbox, exist_ok=True)
            with open(os.path.join(inbox, "%d-beta-%d.json" % (int(TS * 1000), suffixe)),
                      "w", encoding="utf-8") as fh:
                json.dump({"from": "beta", "to": name, "ts": TS,
                           "text": "repli identique"}, fh)
        payload = json.dumps({"hook_event_name": "SessionStart", "cwd": self.tmp})
        v0 = subprocess.run(
            [sys.executable, os.path.join(REPO, "agent-mail.v0.py"), "hook", "claude"],
            input=payload, capture_output=True, text=True,
            env=self.env(AGENT_MAIL_NAME=name, AGENT_MAIL_STATE=v0state),
        )
        v1 = self.cli("hook", "claude", stdin=payload,
                      env=self.env_bad(AGENT_MAIL_NAME=name, AGENT_MAIL_STATE=v1state))
        self.assertEqual(v0.returncode, 0)
        self.assertEqual(v1.returncode, 0)
        self.assertEqual(json.loads(v0.stdout), json.loads(v1.stdout))

        # idem pour le Stop (décision de blocage)
        payload = json.dumps({"hook_event_name": "Stop", "cwd": self.tmp})
        for state, suffixe in ((v0state, 3), (v1state, 4)):
            inbox = os.path.join(state, "inbox", name)
            os.makedirs(inbox, exist_ok=True)
            with open(os.path.join(inbox, "%d-beta-%d.json" % (int(TS * 1000), suffixe)),
                      "w", encoding="utf-8") as fh:
                json.dump({"from": "beta", "to": name, "ts": TS, "text": "stop"}, fh)
        v0 = subprocess.run(
            [sys.executable, os.path.join(REPO, "agent-mail.v0.py"), "hook", "claude"],
            input=payload, capture_output=True, text=True,
            env=self.env(AGENT_MAIL_NAME=name, AGENT_MAIL_STATE=v0state),
        )
        v1 = self.cli("hook", "claude", stdin=payload,
                      env=self.env_bad(AGENT_MAIL_NAME=name, AGENT_MAIL_STATE=v1state))
        self.assertEqual(json.loads(v0.stdout), json.loads(v1.stdout))

    def test_send_signe_en_repli_avertit(self):
        """Le repli fichier ne peut pas porter de signature : il le dit."""
        private, _public, _fp = self.make_test_key("proprietaire")
        proc = self.cli("send", "beta", "message signé", "--sign", "--key", private,
                        env=self.env_bad(AGENT_MAIL_NAME="proprietaire"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("déposé pour : beta", proc.stdout)
        self.assertIn("signature n'est pas conservée", proc.stderr)
        fichiers = os.listdir(os.path.join(self.v0state, "inbox", "beta"))
        self.assertEqual(len(fichiers), 1)

    def test_backend_pg_force_echoue_sans_repli(self):
        proc = self.cli("send", "beta", "non", env=self.env_bad(AMEESH_BACKEND="pg"))
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("déposé", proc.stdout)
        self.assertIn("erreur", proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.v0state, "inbox", "beta")))

    def test_schema_non_migre_dit_de_migrer(self):
        schema = "t_pasmigre_%s" % os.urandom(4).hex()
        self.db.execute('CREATE SCHEMA "%s"' % schema)
        try:
            proc = self.cli("send", "beta", "non", env=self.env(AMEESH_SCHEMA=schema))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("migrate", proc.stderr)
            self.assertNotIn("déposé", proc.stdout)
        finally:
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_doctor_signale_la_base_absente(self):
        proc = self.cli("doctor", env=self.env_bad())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("verdict    : KO", proc.stdout)
        self.assertIn("fichiers", proc.stdout)


if __name__ == "__main__":
    unittest.main()

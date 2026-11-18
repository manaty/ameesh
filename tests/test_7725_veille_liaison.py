# SPDX-License-Identifier: AGPL-3.0-only
"""#7725 : `veille` n'est pas une liaison de session.

La session Codex du poste (liaison #54) écrivait en tant que `veille`, mais
agent-mail refuse tout envoi à `veille` (#3342) : aucune réponse ne lui
parvenait. Une session externe ne se lie donc pas à l'identité d'ÉMISSION de
la veille ; une liaison déjà active ne donne plus d'identité ; et un message
explicitement émis en tant que `veille` avertit son expéditeur que les réponses
ne sont pas livrées.
"""
from __future__ import annotations

import json
import os

from ameesh import identity, mail, registry, storage, undeliverable, watch

from .support import PgTestCase


class VeilleBindingTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")

    # -- utilitaires -------------------------------------------------------
    def unbound_env(self) -> dict:
        env = self.env()
        env.pop("AGENT_MAIL_NAME", None)
        return env

    def bind(self, *args: str, env: dict | None = None):
        return self.cli("bind", *args, env=env or self.unbound_env())

    def legacy_binding(self, session: str = "s-54") -> dict | None:
        """La liaison #54, telle qu'elle existait avant ce refus."""
        return storage.of(self.db).session_bindings.bind(
            host=self.cfg.host, harness="codex", session_id=session,
            agent=watch.SENDER, pid=None, created_by="human:smichea")

    def undelivered(self, name: str) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox "
            "WHERE recipient = %s AND delivered_at IS NULL", (name,))[0]["n"]

    # -- le refus à la liaison -------------------------------------------
    def test_bind_refuse_veille_meme_avec_force(self):
        proc = self.bind(watch.SENDER, "--session", "s-veille", "--harness", "codex")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no read mailbox", proc.stderr)
        self.assertIn("agent it carries", proc.stderr)
        self.assertIsNone(registry.get(self.db, watch.SENDER))
        self.assertEqual(storage.of(self.db).session_bindings.listing(), [])

        # `--force` ne lève pas ce refus (il ne lève que le mode `execute`)
        proc = self.bind(watch.SENDER, "--session", "s-veille", "--harness", "codex",
                         "--force")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no read mailbox", proc.stderr)
        self.assertEqual(storage.of(self.db).session_bindings.listing(), [])

        # l'import du pont local rapporte le refus, sans lier la session
        path = os.path.join(self.tmp, "external-session-bindings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "sessions": {
                "s-veille": {"name": watch.SENDER, "harness": "codex"}}}, fh)
        proc = self.bind("--import", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bindings imported: 0; skipped: 1", proc.stdout)
        self.assertIn("no read mailbox", proc.stdout)
        self.assertEqual(storage.of(self.db).session_bindings.listing(), [])

    # -- une liaison active antérieure au refus ---------------------------
    def test_liaison_active_a_veille_ne_donne_pas_d_identite(self):
        self.assertIsNotNone(self.legacy_binding())
        env = self.unbound_env()

        binding = identity.resolve_binding(self.cfg, self.db, harness="codex",
                                           session_id="s-54")
        self.assertFalse(binding.ok)
        self.assertEqual((binding.name, binding.source), (watch.SENDER, "session"))
        self.assertIn("no read mailbox", binding.reason)

        proc = self.cli("whoami", "--session", "s-54", "--harness", "codex", env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unbound identity", proc.stderr)
        self.assertIn("no read mailbox", proc.stderr)

        # la session ne parle plus en veille : rien n'est déposé à sens unique
        registry.upsert(self.db, "beta", harness="codex", host=self.cfg.host)
        proc = self.cli("send", "beta", "Message d'une session liée.", env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unbound sender", proc.stderr)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM agent_mailbox WHERE recipient = 'beta'"
        )[0]["n"], 0)

        # le hook ne remet pas non plus le courrier resté dans la boîte veille
        mail.send(self.db, "human:alice", watch.SENDER, "Réponse perdue.")
        before = self.undelivered(watch.SENDER)
        hook = self.cli("hook", "codex", env=env, stdin=json.dumps({
            "hook_event_name": "UserPromptSubmit", "session_id": "s-54"}))
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertEqual(hook.stdout, "")
        self.assertIn("no read mailbox", hook.stderr)
        self.assertEqual(self.undelivered(watch.SENDER), before)

    # -- l'expéditeur est averti ------------------------------------------
    def test_message_emis_sans_boite_lue_avertit_l_expediteur(self):
        registry.upsert(self.db, "alpha", harness="codex", host=self.cfg.host)
        registry.upsert(self.db, "beta", harness="codex", host=self.cfg.host)

        notes = undeliverable.check_send(self.cfg, self.db, sender=watch.SENDER,
                                         dest="beta", explicit=True)
        self.assertTrue(any("replies to %s are not delivered" % watch.SENDER in note
                            for note in notes), notes)
        # un expéditeur ordinaire n'a pas la mention
        notes = undeliverable.check_send(self.cfg, self.db, sender="alpha", dest="beta")
        self.assertFalse(any("replies to" in note for note in notes), notes)

        # le message part quand même, et l'avertissement arrive à l'expéditeur
        proc = self.cli("send", "beta", "Rapport de veille.", "--from", watch.SENDER,
                        env=self.unbound_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("replies to veille are not delivered", proc.stderr)
        self.assertIn("readable thread", proc.stderr)
        [row] = self.db.query(
            "SELECT sender, body FROM agent_mailbox WHERE recipient = 'beta'")
        self.assertEqual(row["sender"], watch.SENDER)
        self.assertIn("Rapport de veille.", row["body"])

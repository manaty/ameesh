# SPDX-License-Identifier: AGPL-3.0-only
"""#6964 — la remise du hook à une session externe liée est bornée.

Constat (pc-smichea, 2026-11-11) : une session Codex liée par `ameesh mail
bind` n'a ni tour ni bail ; la borne L105 valait donc 0 (sans borne) et chaque
Stop remettait 200 messages d'un ancien arriéré. Désormais : au plus
`turn_mail_max` messages par hook, un seul Stop bloqué par invite pour une
session liée, et le
reliquat reste non lu, intact. Postgres réel.
"""
from __future__ import annotations

import json
import unittest

from ameesh import mail, registry, session_bindings

from .support import PgTestCase

SID = "sess-externe-6964"


class HookExterneBorneTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")

    def _lier(self, nom: str, harness: str) -> list[int]:
        registry.upsert_unleased(self.db, nom, harness=harness, host=self.cfg.host)
        session_bindings.bind(self.cfg, self.db, nom, session_id=SID,
                              harness=harness, by="test")
        # un arriéré de 230 messages, en une requête
        rows = self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body) "
            "SELECT 'pair', %s, 'message n°' || g FROM generate_series(0, 229) g "
            "ORDER BY g RETURNING id", (nom,))
        return [int(r["id"]) for r in rows]

    def _hook(self, harness: str, event: str, borne: str = "") -> str:
        env = self.env(AMEESH_TURN_MAIL_MAX=borne) if borne else self.env()
        env.pop("AGENT_MAIL_NAME", None)
        proc = self.cli("hook", harness, env=env, stdin=json.dumps(
            {"hook_event_name": event, "session_id": SID, "cwd": self.tmp}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _livres(self, ids) -> int:
        return sum(1 for i in ids if mail.get(self.db, i)["delivered_ts"] is not None)

    def _non_reserves(self, nom: str) -> int:
        jeton = mail.new_token()
        rows = mail.reserve(self.db, nom, None, None, jeton, porteur="test", limit=1000)
        mail.release(self.db, nom, jeton, [int(r["id"]) for r in rows])
        return len(rows)

    def test_stop_codex_borne_puis_sortie_libre(self):
        ids = self._lier("bureau-ext", "codex")
        sortie = json.loads(self._hook("codex", "Stop"))
        self.assertEqual(sortie["decision"], "block")
        self.assertIn("message n°19", sortie["reason"])
        self.assertNotIn("message n°20", sortie["reason"])
        self.assertIn("at least 180 more pending message(s)", sortie["reason"])
        self.assertEqual(self._livres(ids), 20)
        # second Stop de la même invite : la sortie n'est plus retenue, et
        # rien n'est livré ni laissé réservé
        self.assertEqual(self._hook("codex", "Stop"), "")
        self.assertEqual(self._livres(ids), 20)
        self.assertEqual(self._non_reserves("bureau-ext"), 210)
        # nouvelle invite : la suite, toujours bornée
        self._hook("codex", "UserPromptSubmit", borne="5")
        self.assertEqual(self._livres(ids), 25)

    def test_posttooluse_borne_pour_chaque_harnais(self):
        for harness in ("claude", "codex", "deepseek"):
            with self.subTest(harness=harness):
                self.db.execute("TRUNCATE session_bindings RESTART IDENTITY")
                nom = "ext-" + harness
                ids = self._lier(nom, harness)
                sortie = self._hook(harness, "PostToolUse", borne="7")
                contexte = json.loads(sortie)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("message n°6", contexte)
                self.assertNotIn("message n°7", contexte)
                self.assertEqual(self._livres(ids), 7)
                self.assertEqual(self._non_reserves(nom), 223)


if __name__ == "__main__":
    unittest.main()

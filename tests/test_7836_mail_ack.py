# SPDX-License-Identifier: AGPL-3.0-only
"""#7836 — `agent-mail ack` : le lecteur externe remet par la voie des runners.

Le veilleur du bureau lit `agent_mailbox` en base et n'a jamais posé
`delivered_at` : plus de 8 000 messages du bureau sont restés non livrés, et
la porte pre-act a refusé un acte KMS sur un courrier de lot de 15 jours.
`ack` réserve sous un jeton puis solde la même réservation, par id, comme
l'exécuteur et le hook ; il ne prend jamais une remise active d'une autre
session, reste idempotent pour l'identité bureau partagée BG/PC, et laisse le
message lisible par `show`.

Postgres réel.
"""
from __future__ import annotations

import json
import os

from ameesh import mail, pre_act, registry, work

from .support import PgTestCase


class MailAckTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "bureau", harness="claude", host=self.cfg.host,
                        mode="externe")
        registry.upsert(self.db, "alpha", harness="claude", host="autre",
                        mode="externe")
        registry.upsert(self.db, "gamma", harness="codex", host="autre",
                        mode="externe")
        registry.upsert(self.db, "ouvrier", harness="deepseek", host=self.cfg.host,
                        mode="execute")

    def _ack(self, *args, name: str = "bureau", stdin: str | None = None, **extra):
        env = self.env(AGENT_MAIL_NAME=name, **extra)
        return self.cli("ack", *args, env=env, stdin=stdin)

    def _row(self, message_id) -> dict:
        rows = self.db.query(
            "SELECT recipient, delivered_at, status, meta FROM agent_mailbox"
            " WHERE id = %s", (int(message_id),))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _lot(self) -> int:
        return int(work.add(self.db, title="ack 7836", app="ameesh",
                            actor="test", cfg=self.cfg)["id"])

    def _pre_act(self, lot: int):
        with self.db.transaction() as tx:
            return pre_act.inspect(tx, lot, "bureau", 0, "kms")

    def test_ack_delivers_and_pre_act_no_longer_sees_unread_lot_mail(self):
        lot = self._lot()
        message_id = mail.send(self.db, "alpha", "bureau", "information #%d" % lot,
                               work_item_id=str(lot), thread=False)
        with self.assertRaisesRegex(pre_act.Refused, "unread lot mail"):
            self._pre_act(lot)
        self.assertIsNone(self._row(message_id)["delivered_at"])

        proc = self._ack(str(message_id))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("#%d acknowledged" % message_id, proc.stdout)
        row = self._row(message_id)
        self.assertIsNotNone(row["delivered_at"])
        self.assertEqual(row["status"], "delivered")
        self.assertIn("ack@", json.dumps(row["meta"]))
        # La porte pre-act ne voit plus le courrier du lot comme non lu.
        self.assertTrue(self._pre_act(lot))

    def test_ack_refuses_a_third_party_without_leak(self):
        message_id = mail.send(self.db, "alpha", "bureau", "SECRET-7836",
                               thread=False)
        proc = self._ack(str(message_id), name="gamma")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no message #%d addressed to gamma" % message_id, proc.stderr)
        self.assertNotIn("SECRET-7836", proc.stdout + proc.stderr)
        self.assertIsNone(self._row(message_id)["delivered_at"])

    def test_ack_is_idempotent_for_the_shared_identity(self):
        message_id = mail.send(self.db, "alpha", "bureau", "partagé-7836",
                               thread=False)
        first = self._ack(str(message_id))
        self.assertEqual(first.returncode, 0, first.stderr)
        delivered_ts = self._row(message_id)["delivered_at"]

        second = self._ack(str(message_id), "--by", "ack@pc-smichea:abcdef12")

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("#%d already delivered" % message_id, second.stdout)
        self.assertEqual(self._row(message_id)["delivered_at"], delivered_ts,
                         "une seconde session a réécrit delivered_at")
        # Le message reste lisible par l'autre session de la même identité.
        show = self.cli("show", str(message_id), env=self.env(AGENT_MAIL_NAME="bureau"))
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("partagé-7836", show.stdout)

    def test_ack_never_steals_an_active_reservation(self):
        message_id = mail.send(self.db, "alpha", "bureau", "réservé-7836",
                               thread=False)
        token = mail.new_token()
        self.assertEqual(
            [r["id"] for r in mail.reserve(
                self.db, "bureau", None, None, token, ids=[message_id],
                porteur="test-7836")],
            [message_id])

        proc = self._ack(str(message_id))

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("#%d reserved by another delivery" % message_id, proc.stdout)
        self.assertIsNone(self._row(message_id)["delivered_at"])
        self.assertEqual(mail.deliver(self.db, "bureau", None, None, token,
                                      [message_id]), [message_id])
        self.assertIn("#%d already delivered" % message_id,
                      self._ack(str(message_id)).stdout)

    def test_ack_batches_a_tsv_inventory_and_traces_the_reader(self):
        ids = [mail.send(self.db, "alpha", "bureau", "stock-%d" % n,
                         thread=False) for n in range(5)]
        path = os.path.join(self.tmp, "inventaire.tsv")
        with open(path, "w", encoding="utf-8") as fh:
            for message_id in ids:
                fh.write("%d\talpha\t2026-11-19T05:28Z\t7836\n" % message_id)

        proc = self._ack("--ids-from", path, "--batch", "2",
                         "--by", "ack@bureau-goal:526a0a74")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ack: 5 acknowledged, 0 already delivered, 0 reserved, "
                      "0 not addressed (by ack@bureau-goal:526a0a74)", proc.stdout)
        self.assertIn("ack: 2/5", proc.stderr)      # progrès des lots
        for message_id in ids:
            row = self._row(message_id)
            self.assertIsNotNone(row["delivered_at"])
            self.assertEqual(
                row["meta"]["consigne"]["porteur"], "ack@bureau-goal:526a0a74")

    def test_ack_reads_stdin_and_limit_bounds_the_bulk(self):
        ids = [mail.send(self.db, "alpha", "bureau", "stdin-%d" % n,
                         thread=False) for n in range(3)]
        inventory = "".join("%d\n" % message_id for message_id in ids)
        refused = self._ack("--ids-from", "-", "--limit", "2",
                            stdin=inventory)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("exceed --limit 2", refused.stderr)
        for message_id in ids:
            self.assertIsNone(self._row(message_id)["delivered_at"])
        proc = self._ack("--ids-from", "-", "--batch", "2", stdin=inventory)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ack: 3 acknowledged", proc.stdout)
        for message_id in ids:
            self.assertIsNotNone(self._row(message_id)["delivered_at"])

    def test_ack_refuses_one_bad_positional_id_before_writing_anything(self):
        good = mail.send(self.db, "alpha", "bureau", "bon-7836", thread=False)
        proc = self._ack(str(good), "999999")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no message #999999", proc.stderr)
        self.assertIsNone(self._row(good)["delivered_at"],
                          "un id invalide a acquitté les autres")

    def test_ack_validates_ids_and_file(self):
        for value in ("0", "-1", "abc"):
            with self.subTest(value=value):
                proc = self._ack(value)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("invalid message number", proc.stderr)
        proc = self._ack()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no message number", proc.stderr)
        path = os.path.join(self.tmp, "vide.ids")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# rien\n\n")
        proc = self._ack("--ids-from", path)
        self.assertEqual(proc.returncode, 2)

    def test_ack_refuses_an_execute_agent_explicit_identity(self):
        message_id = mail.send(self.db, "alpha", "ouvrier", "tour-7836",
                               thread=False)
        proc = self._ack(str(message_id), name="ouvrier")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("driven by its executor", proc.stderr)
        self.assertIsNone(self._row(message_id)["delivered_at"])

    def test_ack_requires_a_bound_identity_and_the_database(self):
        message_id = mail.send(self.db, "alpha", "bureau", "identité-7836",
                               thread=False)
        proc = self.cli("ack", str(message_id), env=self.env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unbound identity", proc.stderr)
        proc = self._ack(str(message_id), AMEESH_BACKEND="file")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("requires the database", proc.stderr)
        self.assertIsNone(self._row(message_id)["delivered_at"])

    def test_ack_dry_run_writes_nothing(self):
        message_id = mail.send(self.db, "alpha", "bureau", "sec-7836", thread=False)
        proc = self._ack(str(message_id), "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no write", proc.stdout)
        self.assertIsNone(self._row(message_id)["delivered_at"])

    def test_help_lists_ack_and_ameesh_mail_alias_works(self):
        aide = self.cli("--help")
        self.assertEqual(aide.returncode, 0)
        self.assertIn("agent-mail ack <id>", aide.stdout)
        message_id = mail.send(self.db, "alpha", "bureau", "alias-7836",
                               thread=False)
        proc = self.mesh("mail", "ack", str(message_id),
                         env=self.env(AGENT_MAIL_NAME="bureau"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(self._row(message_id)["delivered_at"])

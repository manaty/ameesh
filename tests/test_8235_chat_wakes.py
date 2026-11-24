# SPDX-License-Identifier: AGPL-3.0-only
"""#8235 (issue #6403) : les réveils programmés des agents de conversation.

Un agent `chat-*` n'est mené par aucun exécuteur : ses réveils programmés
n'étaient jamais réclamés ni consommés, et `follow_up_unfollowed` finissait
par le dire (#8196). La veille de `ameesh chat` les délivre désormais avec les
mêmes opérations et le même bail que `runner.py` (`due_wakes`, `claim_wake`,
`consume_wake`) : elle réclame l'échéance au moment de servir, l'écrit dans la
conversation, puis la consomme — la consommation écrit la ligne
`wake: scheduled wake delivered` au journal du lot.
"""
from __future__ import annotations

import time
import unittest

from ameesh import chat, registry, storage, work

from .support import PgTestCase

HUMAN = "human:alice"
NAME = "chat-alice"


class ChatWakeCase(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, NAME, harness="claude", host=self.cfg.host,
                        cwd=self.tmp, mode="externe")
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = %s",
                        (HUMAN, NAME))

    def arm(self, due: float, title: str = "Rappeler le fournisseur") -> int:
        lot = work.add(self.db, title=title, type="follow-up", assignee=NAME,
                       externe=True, app="ameesh", actor=NAME)["id"]
        work.schedule_wake(self.db, lot, due, text="fournisseur", actor=NAME)
        return lot

    def veille(self, *, phase=None) -> chat.Veille:
        return chat.Veille(
            self.cfg, HUMAN, NAME, now=time.monotonic, log=lambda text: None,
            phase=phase or (lambda: chat.PHASE_STOP),
            wakes=chat.ScheduledWakes(storage.of(self.db).lot_sessions, NAME))

    def wake_at(self, lot: int):
        return work.get(self.db, lot).get("wake_ts")

    def wake_claim(self, lot: int):
        rows = self.db.query("SELECT wake_claim_at FROM work_items WHERE id = %s",
                             (lot,))
        return rows[0]["wake_claim_at"] if rows else None

    def notes(self, lot: int) -> list[str]:
        return [e.get("note") or "" for e in work.events(self.db, lot)]


class ChatWakeDeliveryTest(ChatWakeCase):
    def test_a_due_wake_is_served_claimed_then_consumed(self):
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille.observe([], [])                       # la première lecture est un repère

        line = veille.due()
        self.assertIsNotNone(line)
        self.assertIn("[veille ameesh]", line)
        self.assertIn("#%d" % lot, line)
        self.assertIn("Rappeler le fournisseur", line)
        self.assertIn("réveil programmé", line)
        self.assertIsNotNone(self.wake_at(lot), "réclamé, pas encore consommé")
        self.assertIsNotNone(self.wake_claim(lot))

        veille.delivered()
        self.assertIsNone(self.wake_at(lot), "la consommation efface l'échéance")
        self.assertTrue(any(n.startswith("wake: scheduled wake delivered")
                            for n in self.notes(lot)))
        self.assertIsNone(veille.due(), "un réveil ne sert qu'une fois")

    def test_a_future_wake_is_not_served(self):
        lot = self.arm(time.time() + 3600)
        veille = self.veille()
        veille.observe([], [])
        self.assertIsNone(veille.due())
        self.assertIsNotNone(self.wake_at(lot))
        self.assertIsNone(self.wake_claim(lot))

    def test_a_wake_claimed_by_another_deliverer_is_not_taken(self):
        lot = self.arm(time.time() - 1)
        self.assertIsNotNone(
            storage.of(self.db).lot_sessions.claim_wake(NAME, lot, time.time()))
        veille = self.veille()
        veille.observe([], [])
        self.assertIsNone(veille.due(), "le bail d'un autre livreur est respecté")

    def test_a_human_draft_holds_the_wake_without_claiming_it(self):
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille.observe([], [])
        veille.note_human(1000.0, b"un brouillon")
        self.assertIsNone(veille.due(now=2000.0))
        self.assertIsNone(self.wake_claim(lot), "aucun bail n'est pris d'avance")
        veille.note_human(1000.0, b"\r")            # Entrée ferme le brouillon
        self.assertIsNotNone(veille.due(now=2000.0))

    def test_a_busy_harness_holds_the_wake_without_claiming_it(self):
        lot = self.arm(time.time() - 1)
        phase = {"value": chat.PHASE_BUSY}
        veille = self.veille(phase=lambda: phase["value"])
        veille.observe([], [])
        self.assertIsNone(veille.due())
        self.assertIsNone(self.wake_claim(lot))
        phase["value"] = chat.PHASE_STOP
        self.assertIn("#%d" % lot, veille.due())

    def test_a_failed_consume_leaves_the_wake_armed(self):
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille.observe([], [])
        self.assertIsNotNone(veille.due())

        class Boom:
            def consume(self, taken):
                raise RuntimeError("base en panne")
        veille.wakes = Boom()
        veille.delivered()          # ne lève pas ; le bail expirera
        self.assertIsNotNone(self.wake_at(lot))

    def test_without_a_wake_source_nothing_changes(self):
        lot = self.arm(time.time() - 1)
        veille = chat.Veille(self.cfg, HUMAN, NAME, now=time.monotonic,
                             log=lambda text: None,
                             phase=lambda: chat.PHASE_STOP)
        veille.observe([], [])
        self.assertIsNone(veille.due())
        self.assertIsNotNone(self.wake_at(lot))


class ChatWakeAlertTest(ChatWakeCase):
    """#8196 lit exactement ce que la veille écrit : une échéance consommée
    n'est plus `wake_undelivered`."""

    def test_delivery_lifts_the_undelivered_reason(self):
        from ameesh import exploitation
        check = getattr(exploitation, "_follow_ups_unfollowed", None)
        if check is None:
            self.skipTest("alerte #8196 absente de cette tête")
        lot = self.arm(time.time() - 1)
        now = time.time()
        listing = storage.of(self.db).operations.listing()
        reasons = [a["reason"] for a in check(self.db, listing, now + 60)
                   if a.get("lot") == lot]
        self.assertEqual(reasons, [])
        veille = self.veille()
        veille.observe([], [])
        self.assertIsNotNone(veille.due())
        veille.delivered()
        [alert] = [a for a in check(self.db, listing, now + 60) if a.get("lot") == lot]
        self.assertEqual(alert["reason"], "delivered_without_note")


if __name__ == "__main__":
    unittest.main()

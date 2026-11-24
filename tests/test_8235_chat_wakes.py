# SPDX-License-Identifier: AGPL-3.0-only
"""#8235 (issue #6403) : les réveils programmés des agents de conversation.

Un agent `chat-*` n'est mené par aucun exécuteur : ses réveils programmés
n'étaient jamais réclamés ni consommés, et `follow_up_unfollowed` finissait
par le dire (#8196). La veille de `ameesh chat` les délivre désormais avec les
mêmes opérations et le même bail que `runner.py` (`due_wakes`, `claim_wake`,
`consume_wake`) : le fil de la veille (`Veille._loop`, exercé ici par
`_pump_wakes`) réclame l'échéance quand la garde d'état est ouverte et
renouvelle son bail ; le fil du terminal l'écrit dans la conversation
(`due`) puis la marque servie (`delivered`), et la passe suivante la consomme
— la consommation écrit la ligne `wake: scheduled wake delivered` au journal
du lot. Aucune opération de base n'a lieu dans le fil du terminal.
"""
from __future__ import annotations

import time
import unittest

from ameesh import chat, registry, storage, work

from .support import PgTestCase

HUMAN = "human:alice"
NAME = "chat-alice"


class _Clock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t


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

    def veille(self, *, phase=None, now=time.monotonic) -> chat.Veille:
        return chat.Veille(
            self.cfg, HUMAN, NAME, now=now, log=lambda text: None,
            phase=phase or (lambda: chat.PHASE_STOP),
            wakes=chat.ScheduledWakes(storage.of(self.db).lot_sessions, NAME))

    def wake_at(self, lot: int):
        return work.get(self.db, lot).get("wake_ts")

    def wake_claim(self, lot: int):
        rows = self.db.query("SELECT wake_claim_at FROM work_items WHERE id = %s",
                             (lot,))
        return rows[0]["wake_claim_at"] if rows else None

    def claim_age(self, lot: int) -> float:
        rows = self.db.query(
            "SELECT extract(epoch from clock_timestamp() - wake_claim_at)::float8"
            "       AS age FROM work_items WHERE id = %s", (lot,))
        return rows[0]["age"]

    def notes(self, lot: int) -> list[str]:
        return [e.get("note") or "" for e in work.events(self.db, lot)]


class ChatWakeDeliveryTest(ChatWakeCase):
    def test_a_due_wake_is_served_claimed_then_consumed(self):
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille.observe([], [])                       # la première lecture est un repère

        self.assertIsNone(veille.due(), "le fil du terminal ne lit pas la base")
        veille._pump_wakes()                         # la passe de `_loop`
        self.assertIsNotNone(veille._claimed_wake, "réveil échu réclamé")
        self.assertIsNotNone(self.wake_at(lot), "réclamé, pas encore consommé")
        self.assertIsNotNone(self.wake_claim(lot))

        line = veille.due()
        self.assertIsNotNone(line)
        self.assertIn("[veille ameesh]", line)
        self.assertIn("#%d" % lot, line)
        self.assertIn("Rappeler le fournisseur", line)
        self.assertIn("réveil programmé", line)

        veille.delivered()                           # écrit dans le pty
        self.assertIsNone(veille._claimed_wake)
        self.assertIsNotNone(veille._served_wake)
        self.assertIsNotNone(self.wake_at(lot), "consommé à la passe suivante")
        veille._pump_wakes()                         # consommation, dans `_loop`
        self.assertIsNone(self.wake_at(lot), "la consommation efface l'échéance")
        self.assertTrue(any(n.startswith("wake: scheduled wake delivered")
                            for n in self.notes(lot)))
        self.assertIsNone(veille.due(), "un réveil ne sert qu'une fois")

    def test_a_future_wake_is_not_served(self):
        lot = self.arm(time.time() + 3600)
        veille = self.veille()
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNone(veille.due())
        self.assertIsNotNone(self.wake_at(lot))
        self.assertIsNone(self.wake_claim(lot))

    def test_a_wake_claimed_by_another_deliverer_is_not_taken(self):
        lot = self.arm(time.time() - 1)
        self.assertIsNotNone(
            storage.of(self.db).lot_sessions.claim_wake(NAME, lot, time.time()))
        veille = self.veille()
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNone(veille._claimed_wake)
        self.assertIsNone(veille.due(), "le bail d'un autre livreur est respecté")

    def test_a_held_claim_is_renewed_on_each_pass(self):
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille._pump_wakes()
        self.assertIsNotNone(veille._claimed_wake)
        # le bail aurait expiré depuis longtemps sans renouvellement
        self.db.execute(
            "UPDATE work_items SET wake_claim_at = clock_timestamp()"
            " - interval '10 minutes' WHERE id = %s", (lot,))
        veille._pump_wakes()
        self.assertIsNotNone(veille._claimed_wake, "le bail tenu est renouvelé")
        self.assertLess(self.claim_age(lot), 60.0)
        self.assertIn("#%d" % lot, veille.due())

    def test_a_human_draft_holds_the_wake_without_claiming_it(self):
        lot = self.arm(time.time() - 1)
        clock = _Clock(1000.0)
        veille = self.veille(now=clock)
        veille.observe([], [])
        veille.note_human(1000.0, b"un brouillon")
        veille._pump_wakes()
        self.assertIsNone(veille._claimed_wake)
        self.assertIsNone(self.wake_claim(lot), "aucun bail n'est pris d'avance")
        self.assertIsNone(veille.due(now=1000.0))
        veille.note_human(1000.0, b"\r")            # Entrée ferme le brouillon
        clock.t = 1005.0
        veille._pump_wakes()
        self.assertIsNotNone(veille._claimed_wake)
        self.assertIsNotNone(veille.due(now=1005.0))

    def test_serving_news_does_not_consume_the_held_wake(self):
        """B/P1 #46308 : une nouvelle ordinaire servie au-delà de `max_defer`
        ne doit pas emporter le réveil retenu que `delivered` n'a pas écrit."""
        lot = self.arm(time.time() - 1)
        clock = _Clock(1000.0)
        veille = self.veille(now=clock)
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNotNone(veille._claimed_wake, "réveil échu réclamé")
        # frappe récente : le réveil attend ; une nouvelle plus vieille que
        # `max_defer` passe quand même (une session occupée n'affame pas la veille)
        veille.note_human(999.0)
        veille._text = "[veille ameesh] ligne ordinaire"
        veille._text_at = 99.0
        self.assertEqual(veille.due(now=clock.t), "[veille ameesh] ligne ordinaire")
        veille.delivered()                           # c'est cette ligne qui a été écrite
        self.assertIsNone(veille._served_wake, "le réveil n'a pas été écrit")
        self.assertIsNotNone(veille._claimed_wake, "le réveil reste retenu")
        self.assertIsNotNone(self.wake_at(lot), "rien n'est consommé")
        self.assertFalse(any(n.startswith("wake: scheduled wake delivered")
                             for n in self.notes(lot)))
        veille._pump_wakes()                         # la passe renouvelle le bail
        self.assertIsNotNone(veille._claimed_wake)
        self.assertIsNotNone(self.wake_at(lot))
        # passé `max_defer` depuis la première lecture, le réveil est servi
        clock.t = 1901.0
        self.assertIn("#%d" % lot, veille.due(now=clock.t))
        veille.delivered()
        veille._pump_wakes()
        self.assertIsNone(self.wake_at(lot), "le réveil rendu est consommé")

    def test_a_note_between_serve_and_consume_counts(self):
        """B/P2 #46312 (ameesh-dev3) : la délivrance est datée du claim, donc
        une note écrite après la ligne du pty et avant la consommation compte."""
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille.observe([], [])
        veille._pump_wakes()                         # réclamation
        self.assertIn("#%d" % lot, veille.due())     # ligne écrite dans le pty
        veille.delivered()
        work.note(self.db, lot, "j'ai fait le geste", actor=NAME)
        veille._pump_wakes()                         # consommation, plus tard en vrai
        self.assertIsNone(work.get(self.db, lot).get("wake_ts"))
        rows = [r for r in storage.of(self.db).work.follow_up_watch()
                if int(r["id"]) == lot]
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["noted_ts"],
                             "la note entre écriture et consommation est vue")

    def test_a_busy_harness_holds_the_wake_without_claiming_it(self):
        lot = self.arm(time.time() - 1)
        phase = {"value": chat.PHASE_BUSY}
        veille = self.veille(phase=lambda: phase["value"])
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNone(veille._claimed_wake)
        self.assertIsNone(self.wake_claim(lot))
        phase["value"] = chat.PHASE_STOP
        veille._pump_wakes()
        self.assertIn("#%d" % lot, veille.due())

    def test_a_failed_consume_leaves_the_wake_armed(self):
        lot = self.arm(time.time() - 1)
        veille = self.veille()
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNotNone(veille.due())

        class Boom:
            def consume(self, taken):
                raise RuntimeError("base en panne")
        veille.wakes = Boom()
        veille.delivered()
        veille._pump_wakes()        # ne lève pas ; le bail expirera
        self.assertIsNone(veille._served_wake)
        self.assertIsNotNone(self.wake_at(lot))

    def test_without_a_wake_source_nothing_changes(self):
        lot = self.arm(time.time() - 1)
        veille = chat.Veille(self.cfg, HUMAN, NAME, now=time.monotonic,
                             log=lambda text: None,
                             phase=lambda: chat.PHASE_STOP)
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNone(veille.due())
        self.assertIsNotNone(self.wake_at(lot))

    def test_the_serve_path_touches_no_storage(self):
        """B/P2 #8235 : `due` et `delivered` ne doivent appeler aucune
        opération de base — une source de réveils qui échoue à tout appel le
        prouve. Seule la passe de `_loop` (`_pump_wakes`) la consulte."""
        class Boom:
            def __init__(self):
                self.calls: list[str] = []

            def _boom(self, name):
                self.calls.append(name)
                raise AssertionError("%s called from the terminal thread" % name)

            def due(self, *args, **kwargs):
                self._boom("due")

            def claim(self, *args, **kwargs):
                self._boom("claim")

            def renew(self, *args, **kwargs):
                self._boom("renew")

            def consume(self, *args, **kwargs):
                self._boom("consume")

        wakes = Boom()
        veille = chat.Veille(object(), HUMAN, NAME, now=lambda: 2000.0,
                             log=lambda text: None,
                             phase=lambda: chat.PHASE_STOP, wakes=wakes)
        veille.observe([], [])
        self.assertIsNone(veille.due())              # rien de retenu
        veille.delivered()                           # rien de servi
        # l'état qu'une passe de `_loop` aurait posé en mémoire
        veille._claimed_wake = {"id": 42, "title": "t", "state": "build",
                                "wake_ts": 1.0}
        self.assertIn("#42", veille.due())
        veille.delivered()
        self.assertIsNotNone(veille._served_wake)
        self.assertEqual(wakes.calls, [], "aucun appel de base dans le fil du pty")
        veille._pump_wakes()                         # la passe de `_loop`, elle, le fait
        self.assertEqual(wakes.calls, ["consume"])


class ChatWakeAlertTest(ChatWakeCase):
    """#8196 lit exactement ce que la veille écrit : l'échéance échue n'est
    signalée `wake_undelivered` qu'après la grâce de suivi, et sa
    consommation la fait passer `delivered_without_note`, grâce comprise."""

    def test_delivery_lifts_the_undelivered_reason(self):
        from ameesh import exploitation
        check = getattr(exploitation, "_follow_ups_unfollowed", None)
        if check is None:
            self.skipTest("alerte #8196 absente de cette tête")
        grace = exploitation.FOLLOW_UP_GRACE_S
        lot = self.arm(time.time() - 1)
        armed = time.time()
        listing = storage.of(self.db).operations.listing()
        reasons = [a["reason"] for a in check(self.db, listing, armed + 60)
                   if a.get("lot") == lot]
        self.assertEqual(reasons, [], "la grâce de suivi court encore")
        [alert] = [a for a in check(self.db, listing, armed + grace + 1)
                   if a.get("lot") == lot]
        self.assertEqual(alert["reason"], "wake_undelivered")

        veille = self.veille()
        veille.observe([], [])
        veille._pump_wakes()
        self.assertIsNotNone(veille.due())
        veille.delivered()
        veille._pump_wakes()
        delivered = time.time()
        self.assertEqual([a for a in check(self.db, listing, delivered + 60)
                          if a.get("lot") == lot], [],
                         "la délivrance ouvre un nouvel épisode sous la grâce")
        [alert] = [a for a in check(self.db, listing, delivered + grace + 1)
                   if a.get("lot") == lot]
        self.assertEqual(alert["reason"], "delivered_without_note")


if __name__ == "__main__":
    unittest.main()

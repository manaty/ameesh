# SPDX-License-Identifier: AGPL-3.0-only
"""L385 storage: explicit edges, atomic cycle checks and durable audit."""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from ameesh import db as D, mail, plan, planned_waits, progress, stagnation, storage, work
from ameesh import work_dependencies as dependencies
from ameesh.storage.postgres.work_dependencies import WorkDependencies as PgLandingStore
from tests.support import PgTestCase


class StoreTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")
        self.a, self.b, self.c = [work.add(self.db, title=title)["id"] for title in ("a", "b", "c")]
        self.store = storage.of(self.db).work_dependencies

    def add(self, a, b):
        return self.store.add(a, b, actor="test")

    def test_add_replay_remove_and_journal(self):
        self.assertEqual(self.add(self.a, self.b), {"added": True})
        after = work.get(self.db, self.a)["updated_ts"]
        self.assertEqual(self.add(self.a, self.b), {"added": False})
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], after)
        rows = self.store.for_items([self.a])
        self.assertEqual([(r["blocker_id"], r["blocking"]) for r in rows], [(self.b, True)])
        self.assertTrue(self.store.remove(self.a, self.b, actor="test"))
        self.assertFalse(self.store.remove(self.a, self.b, actor="test"))
        self.assertEqual(self.store.for_items([self.a]), [])
        notes = [e["note"] for e in work.events(self.db, self.a) if e["note"].startswith("Dependency")]
        self.assertEqual(notes, ["Dependency removed: lot #%d" % self.b,
                                 "Dependency added: lot #%d" % self.b])

    def test_self_unknown_and_transitive_cycle_are_refused_without_mutation(self):
        self.assertEqual(self.add(self.a, self.a)["error"], "self_dependency")
        self.assertEqual(self.add(self.a, 999999)["error"], "unknown_lot")
        self.assertEqual(self.add(self.a, self.b), {"added": True})
        self.assertEqual(self.add(self.b, self.c), {"added": True})
        before = work.get(self.db, self.c)["updated_ts"]
        self.assertEqual(self.add(self.c, self.a)["error"], "dependency_cycle")
        self.assertEqual(self.store.for_items([self.c]), [])
        self.assertEqual(work.get(self.db, self.c)["updated_ts"], before)

    def test_closed_blocker_keeps_edge_but_no_longer_blocks(self):
        self.add(self.a, self.b)
        work.close(self.db, self.b, abandoned=True)
        row = self.store.for_items([self.a])[0]
        self.assertFalse(row["blocking"])
        self.assertEqual(row["blocker_state"], "closed")
        self.assertEqual(self.add(self.a, self.b), {"added": False})
        self.assertEqual(self.add(self.c, self.b)["error"], "finished_lot")

    def test_concurrent_opposite_edges_cannot_both_commit(self):
        start = threading.Barrier(2)
        results, errors = [], []
        def add(a, b):
            db = D.connect(self.cfg)
            try:
                start.wait(timeout=5)
                results.append(storage.of(db).work_dependencies.add(a, b, actor="test"))
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=add, args=pair, daemon=True)
                   for pair in ((self.a, self.b), (self.b, self.a))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(r.get("added") is True for r in results), 1)
        self.assertEqual(sum(r.get("error") == "dependency_cycle" for r in results), 1)
        self.assertEqual(len(self.store.for_items([self.a, self.b])), 1)

    def test_graph_and_audit_roll_back_together(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                self.assertEqual(storage.of(tx).work_dependencies.add(self.a, self.b, actor="test"),
                                 {"added": True})
                raise RuntimeError("rollback")
        self.assertEqual(self.store.for_items([self.a]), [])
        self.assertFalse(any(e["note"].startswith("Dependency") for e in work.events(self.db, self.a)))


    def age(self, item_id, now):
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s),"
                        " updated_at = to_timestamp(%s) WHERE id = %s", (now - 40000, now - 40000, item_id))
        self.db.execute("UPDATE work_item_events SET created_at = to_timestamp(%s)"
                        " WHERE work_item_id = %s", (now - 40000, item_id))
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE work_item_id = %s", (now - 40000, item_id))

    def test_explicit_wait_in_alerts_work_and_progress_releases_without_resetting_activity(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        self.db.execute("UPDATE work_items SET app = 'acme' WHERE id = %s", (self.a,))
        self.db.execute("UPDATE work_items SET app = 'other' WHERE id = %s", (self.b,))
        now = time.time()
        self.age(self.a, now)
        self.age(self.c, now)
        before = work.get(self.db, self.a)["updated_ts"]
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=now, limit=1)], [self.c])
        row = plan.annotate(self.db, [work.get(self.db, self.a)], now=now)[0]
        self.assertEqual(row["planned_wait"], {"what": "dependency", "lots": [self.b]})
        self.assertIsNone(row["stale"])
        self.assertTrue(row["dependencies"][0]["blocking"])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=now, max_lots=1, gantt=False)
        self.assertEqual(len(snap["lots"]), 1)
        self.assertEqual(snap["lots"][0]["dependencies"][0]["blocker_id"], self.b)
        self.assertIsNone(snap["lots"][0]["stale"])
        # Blocker is outside both the project's view and its rendering bound.
        work.close(self.db, self.b, abandoned=True)
        row = plan.annotate(self.db, [work.get(self.db, self.a)], now=now)[0]
        self.assertIsNone(row["planned_wait"])
        self.assertIsNotNone(row["stale"])
        self.assertFalse(row["dependencies"][0]["blocking"])
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], before)
        self.assertIn(self.a, [r["id"] for r in stagnation.stagnant_lots(self.db, now=now)])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=now, gantt=False)
        self.assertFalse(snap["lots"][0]["dependencies"][0]["blocking"])
        self.assertIsNotNone(snap["lots"][0]["stale"])

    def test_cli_edits_replay_refusals_and_archived_prerequisites(self):
        def cli(*args, ok=True):
            proc = self.mesh("work", "dependencies", str(self.a), *args, "--actor", "test")
            self.assertEqual(proc.returncode, 0 if ok else 1, proc.stdout + proc.stderr)
            return proc
        first = json.loads(cli("--add", str(self.b), "--json").stdout)
        self.assertTrue(first["changed"])
        self.assertEqual(first["dependencies"][0]["created_by"], "test")
        before = work.get(self.db, self.a)["updated_ts"]
        self.assertFalse(json.loads(cli("--add", str(self.b), "--json").stdout)["changed"])
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], before)
        self.assertIn("itself", cli("--add", str(self.a), ok=False).stderr)
        self.assertIn("existing lots", cli("--add", "999999", ok=False).stderr)
        proc = self.mesh("work", "dependencies", str(self.b), "--add", str(self.a), "--actor", "test")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("cycle", proc.stderr)
        show = self.mesh("work", "show", str(self.a), "--json")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(json.loads(show.stdout)["planned_wait"]["lots"], [self.b])
        text = self.mesh("work", "list")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("waiting on lots #%d" % self.b, text.stdout)
        work.close(self.db, self.b, abandoned=True)
        self.assertIn("satisfied", cli().stdout)
        show = self.mesh("work", "show", str(self.a))
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("depends  : #%d (closed, satisfied)" % self.b, show.stdout)
        self.assertTrue(json.loads(cli("--remove", str(self.b), "--json").stdout)["changed"])
        self.assertFalse(json.loads(cli("--remove", str(self.b), "--json").stdout)["changed"])
        self.assertEqual(json.loads(cli("--json").stdout)["dependencies"], [])


class ReleasedWakeTest(PgTestCase):
    """L385 suite : la fermeture d'un bloqueur réveille l'agent du dépendant.

    Le réveil est un courrier durable (`kind=event`) porté par le lot
    dépendant : le runner existant s'en sert tel quel, aucun sondage ni
    session neuve ; un agent arrêté à la main reste arrêté. Le courrier est
    écrit DANS la transaction de fermeture, une seule fois, et seulement
    quand plus aucune attente ne retient le lot : un échec annule la
    fermeture entière (reprise), et le verrou de graphe empêche deux
    fermetures concurrentes de se doubler ou de perdre le réveil.
    """

    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = [work.add(self.db, title=title)["id"]
                                  for title in ("waiting", "blocker", "other blocker")]
        self.assign(self.a, "dev-a")

    def assign(self, item, name):
        self.db.execute("UPDATE work_items SET assignee = %s WHERE id = %s", (name, item))

    def inbox(self, name="dev-a"):
        return mail.unread(self.db, name)

    def mail_lot(self, row):
        """Le lot porté par un message (la colonne n'est pas dans `unread`)."""
        return self.db.query("SELECT work_item_id FROM agent_mailbox WHERE id = %s",
                             (int(row["id"]),))[0]["work_item_id"]

    def test_merging_the_blocker_wakes_the_assignee_once(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        done = work.close_merged(self.db, self.b, sha="a" * 40, actor="git:main")
        self.assertEqual(done["result"], "merged")
        [row] = self.inbox()
        self.assertEqual((row["kind"], self.mail_lot(row)), ("event", str(self.a)))
        self.assertIn("lot #%d is delivered" % self.b, row["body"])
        self.assertIn("lot #%d" % self.a, row["body"])
        self.assertEqual(mail.message_payload(row)["dependency"],
                         {"dependent": self.a, "blocker": self.b, "blocker_state": "delivered"})
        # Rejouer la déclaration de fusion ne réveille pas une seconde fois.
        self.assertEqual(work.close_merged(self.db, self.b, sha="a" * 40, actor="git:main")["result"],
                         "already")
        self.assertEqual(len(self.inbox()), 1)

    def test_the_wake_waits_for_the_last_blocker(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        dependencies.add(self.db, self.a, self.c, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        # L'autre bloqueur tient encore : rien n'est libéré, rien n'est envoyé.
        self.assertEqual(dependencies.released_by(self.db, self.b), [])
        self.assertEqual(dependencies.released_by(self.db, self.c), [])
        self.assertEqual(self.inbox(), [])
        work.close(self.db, self.c, abandoned=True, actor="test")
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual([r["id"] for r in dependencies.released_by(self.db, self.c)], [self.a])

    def test_an_abandoned_blocker_wakes_with_the_reason(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        [row] = self.inbox()
        self.assertIn("lot #%d was closed (abandoned), not merged" % self.b, row["body"])
        self.assertIn("Reassess the prerequisite", row["body"])

    def test_a_future_planned_start_is_not_released(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        self.db.execute("UPDATE work_items SET planned_start = '2999-01-01' WHERE id = %s",
                        (self.a,))
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(dependencies.released_by(self.db, self.b), [])
        self.assertEqual(self.inbox(), [])

    def test_a_dependent_already_closed_is_not_woken(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.a, abandoned=True, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(dependencies.released_by(self.db, self.b), [])
        self.assertEqual(self.inbox(), [])

    def test_an_unassigned_dependent_is_released_without_a_recipient(self):
        self.assign(self.a, None)
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual([r["id"] for r in dependencies.released_by(self.db, self.b)], [self.a])
        self.assertEqual(storage.of(self.db).mailbox.unread_total(), 0)

    def test_an_unassigned_blocked_dependent_returns_to_intake(self):
        """#1668 : sans destinataire, le lot que plus rien ne retient repasse
        `intake` — `actionable()` ne lit que `intake`/`build`, sinon il reste
        invisible au self-service jusqu'à l'alerte stale (jusqu'à 14 h)."""
        self.assign(self.a, None)
        self.db.execute("UPDATE work_items SET state = 'blocked' WHERE id = %s", (self.a,))
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(work.get(self.db, self.a)["state"], "intake")
        notes = [e["note"] for e in work.events(self.db, self.a)]
        self.assertIn("Dependency released: back to intake (no assignee, #1668)", notes)
        self.assertEqual(storage.of(self.db).mailbox.unread_total(), 0)

    def test_a_pinned_blocked_dependent_is_not_released_by_a_legacy_closure(self):
        """#1922/B4 (relecture Claude) : la réouverture historique ne lève pas
        une retenue du registre Process. Un lot pinné, non assigné, retenu
        `blocked` par le registre (vérifié par C) et dont le dernier
        prérequis tombe restait rouvert `intake` : la retenue tombait sans
        commande `release` ni vérificateur C, et l'étape restait (ex. review
        avec state intake). Filtre `workflow_ref` dans le SQL."""
        self.assign(self.a, None)
        self.db.execute(
            "UPDATE work_items SET state = 'blocked', workflow_ref = %s,"
            " workflow_stage = 'review', workflow_version = 1 WHERE id = %s",
            ("acme/process-workflow@0f1e2d3c", self.a))
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        row = self.db.query(
            "SELECT state, workflow_stage FROM work_items WHERE id = %s", (self.a,))[0]
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["workflow_stage"], "review")
        notes = [e["note"] for e in work.events(self.db, self.a)]
        self.assertNotIn("Dependency released: back to intake (no assignee, #1668)", notes)
        self.assertEqual(storage.of(self.db).mailbox.unread_total(), 0)

    def test_a_pinned_assigned_dependent_is_not_woken_by_a_legacy_closure(self):
        """#1922/N13 (relecture Claude) : la fermeture d'un prérequis ne lève
        pas la retenue d'un lot pinné — seul un `release` du registre, vérifié
        par C, le débloque. Le dépendant ASSIGNÉ recevait pourtant le courrier
        « resume it when you are ready », qui promet une reprise que le
        registre seul peut autoriser ; il n'est plus candidat à la
        libération."""
        self.db.execute(
            "UPDATE work_items SET state = 'blocked', workflow_ref = %s,"
            " workflow_stage = 'review', workflow_version = 1 WHERE id = %s",
            ("acme/process-workflow@0f1e2d3c", self.a))
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(dependencies.released_by(self.db, self.b), [])
        self.assertEqual(self.inbox(), [])
        row = self.db.query(
            "SELECT state, workflow_stage FROM work_items WHERE id = %s", (self.a,))[0]
        self.assertEqual((row["state"], row["workflow_stage"]), ("blocked", "review"))

    def test_an_assigned_blocked_dependent_keeps_its_state(self):
        """#1668 : un lot assigné garde son état — le courrier de réveil porte
        le geste à son agent, qui juge la suite (pas de réouverture forcée)."""
        self.db.execute("UPDATE work_items SET state = 'blocked' WHERE id = %s", (self.a,))
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(work.get(self.db, self.a)["state"], "blocked")
        [row] = self.inbox()
        self.assertIn("lot #%d was closed" % self.b, row["body"])

    def test_a_dependent_that_another_blocker_holds_stays_blocked(self):
        """#1668 : la réouverture ne part qu'à la DERNIÈRE attente tombée."""
        self.assign(self.a, None)
        self.db.execute("UPDATE work_items SET state = 'blocked' WHERE id = %s", (self.a,))
        dependencies.add(self.db, self.a, self.b, actor="test")
        dependencies.add(self.db, self.a, self.c, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(work.get(self.db, self.a)["state"], "blocked")
        work.close(self.db, self.c, abandoned=True, actor="test")
        self.assertEqual(work.get(self.db, self.a)["state"], "intake")

    def test_an_unassigned_dependent_outside_blocked_is_untouched(self):
        """#1668 : seule la retenue `blocked` se rouvre — une porte humaine
        (`waiting_human`) ou un lot en cours ne sont pas des attentes de lot."""
        self.assign(self.a, None)
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s",
                        (self.a,))
        dependencies.add(self.db, self.a, self.b, actor="test")
        work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(work.get(self.db, self.a)["state"], "waiting_human")

    def test_the_wake_is_written_with_the_closure_in_one_transaction(self):
        """Le courrier part dans la transaction de fermeture : quand il est
        écrit, la transition est déjà visible (et elle n'est pas committée
        sans lui, voir le témoin d'échec)."""
        dependencies.add(self.db, self.a, self.b, actor="test")
        seen = []
        real = mail.send

        def spy(db, sender, recipient, body, **kw):
            # lu par la MÊME transaction que la fermeture : la transition est
            # déjà écrite quand le courrier part (et committée avec lui)
            seen.append((work.get(db, self.b)["state"], recipient, kw.get("kind"),
                         bool(kw.get("after_commit") is not None)))
            return real(db, sender, recipient, body, **kw)

        with mock.patch("ameesh.mail.send", side_effect=spy):
            work.close_merged(self.db, self.b, sha="b" * 40, actor="git:main")
        self.assertEqual(seen, [("delivered", "dev-a", "event", True)])

    def test_a_failed_wake_rolls_back_the_closure_and_a_retry_wakes_once(self):
        """Adversaire : le courrier échoue après l'écriture de la fermeture.

        Le défaut d'origine committait la fusion sans son réveil, et le rejeu
        rendait `already` : le réveil était définitivement perdu. Ici la
        fermeture et le courrier sont une seule transaction — l'échec annule
        la fusion, la reprise ferme ET réveille, exactement une fois.
        """
        dependencies.add(self.db, self.a, self.b, actor="test")
        with mock.patch("ameesh.mail.send", side_effect=RuntimeError("mailbox down")):
            with self.assertRaises(RuntimeError):
                work.close_merged(self.db, self.b, sha="c" * 40, actor="git:main")
        self.assertEqual(work.get(self.db, self.b)["state"], "intake")
        self.assertEqual(self.inbox(), [])
        done = work.close_merged(self.db, self.b, sha="c" * 40, actor="git:main")
        self.assertEqual(done["result"], "merged")
        self.assertEqual(work.get(self.db, self.b)["state"], "delivered")
        [row] = self.inbox()
        self.assertEqual(mail.message_payload(row)["dependency"],
                         {"dependent": self.a, "blocker": self.b, "blocker_state": "delivered"})
        # Rejouer la déclaration, une fois la fusion commise, ne double pas.
        self.assertEqual(work.close_merged(self.db, self.b, sha="c" * 40, actor="git:main")["result"],
                         "already")
        self.assertEqual(len(self.inbox()), 1)

    def test_a_failed_wake_rolls_back_an_abandoned_closure_too(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        with mock.patch("ameesh.mail.send", side_effect=RuntimeError("mailbox down")):
            with self.assertRaises(RuntimeError):
                work.close(self.db, self.b, abandoned=True, actor="test")
        self.assertEqual(work.get(self.db, self.b)["state"], "intake")
        self.assertEqual(self.inbox(), [])
        work.close(self.db, self.b, abandoned=True, actor="test")
        [row] = self.inbox()
        self.assertIn("was closed (abandoned)", row["body"])

    def test_parallel_closures_wake_the_dependent_exactly_once(self):
        """Adversaire : B et C ferment en parallèle.

        Sans sérialisation, chaque fermeture peut lire l'autre bloqueur
        encore ouvert (aucun réveil : perdu) ; ou, en lisant après coup, les
        deux peuvent voir la libération (deux courriers : doublon). Le verrou
        de graphe les sérialise : exactement un courrier, quel que soit
        l'entrelacement — l'assertion ne dépend pas de l'ordonnancement.
        """
        for _round in range(3):
            waiting = work.add(self.db, title="dépendant")["id"]
            self.assign(waiting, "dev-a")
            blockers = [work.add(self.db, title="bloqueur %d" % i)["id"] for i in (0, 1)]
            for blocker in blockers:
                dependencies.add(self.db, waiting, blocker, actor="test")
            start = threading.Barrier(2)
            results, errors = [], []

            def merge(blocker):
                db = D.connect(self.cfg)
                try:
                    start.wait(timeout=5)
                    results.append(work.close_merged(db, blocker, sha="e" * 40,
                                               actor="git:main")["result"])
                except Exception as exc:  # noqa: BLE001 - remonté par le test
                    errors.append(exc)
                finally:
                    db.close()

            threads = [threading.Thread(target=merge, args=(blocker,), daemon=True)
                       for blocker in blockers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, ["merged", "merged"])
            [row] = self.inbox()
            self.assertEqual(mail.message_payload(row)["dependency"]["dependent"], waiting)
            self.assertEqual(len(self.inbox()), 1)
            self.db.execute("DELETE FROM agent_mailbox")

    def test_a_closure_holds_the_graph_lock_while_it_decides(self):
        """Adversaire déterministe du réveil perdu : la première fermeture
        tient le verrou de graphe sans committer ; la seconde doit l'attendre
        (observé dans `pg_locks`), donc elle lit le commit de la première et
        réveille le dépendant, une seule fois. Sans verrou pris par la
        fermeture, aucune attente n'apparaît : la seconde décide sur un état
        non commis et le réveil est perdu."""
        dependencies.add(self.db, self.a, self.b, actor="test")
        dependencies.add(self.db, self.a, self.c, actor="test")
        held, release = threading.Event(), threading.Event()
        errors = []

        def holder():
            db = D.connect(self.cfg)
            try:
                with db.transaction() as tx:
                    # même clé que le verrou de graphe (primitive existante)
                    tx.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                               "current_schema() || ':work_dependencies', 0))")
                    storage.of(tx).work.close(
                        self.b, current="intake", reason="abandoned", superseded_by=None,
                        actor="test", note="abandoned")
                    held.set()
                    release.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001 - remonté par le test
                errors.append(exc)
            finally:
                db.close()

        results = []

        def closer():
            db = D.connect(self.cfg)
            try:
                results.append(work.close_merged(db, self.c, sha="f" * 40, actor="git:main")["result"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(timeout=10), "la transaction tenante n'a pas démarré")
        closer_thread = threading.Thread(target=closer, daemon=True)
        closer_thread.start()
        # état (pas durée) : la fermeture attend le verrou de graphe du schéma
        waiting = False
        deadline = time.time() + 10
        while time.time() < deadline and not waiting:
            waiting = bool(self.db.query(
                """
                SELECT 1 FROM pg_locks
                 WHERE locktype = 'advisory' AND NOT granted AND objsubid = 1
                   AND classid = ((hashtextextended(current_schema()
                                    || ':work_dependencies', 0) >> 32)
                                  & 4294967295)::oid
                   AND objid = (hashtextextended(current_schema()
                                  || ':work_dependencies', 0) & 4294967295)::oid
                """))
            if not waiting:
                time.sleep(0.05)
        release.set()
        closer_thread.join(timeout=15)
        thread.join(timeout=15)
        self.assertFalse(closer_thread.is_alive())
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(waiting, "la fermeture n'attend pas le verrou de graphe")
        self.assertEqual(results, ["merged"])
        self.assertEqual(len(self.inbox()), 1)

    def test_the_cli_declaration_wakes_too(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        import os
        from .test_merged_work_item_tracking import _git
        repo = os.path.join(self.tmp, "declared-blocker")
        os.makedirs(repo)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "remote", "add", "origin", "git@github.com:manaty/ameesh.git")
        self.db.execute("UPDATE work_items SET issue_ref = %s WHERE id = %s",
                        ("manaty/ameesh#1", self.b))
        _git(repo, "commit", "--allow-empty", "-qm", "ameesh-work: %d" % self.b)
        sha = _git(repo, "rev-parse", "HEAD")
        proc = self.mesh("work", "merged", str(self.b), "--sha", sha,
                         "--repo", repo, "--actor", "test")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [row] = self.inbox()
        self.assertEqual(self.mail_lot(row), str(self.a))


class LandingTest(PgTestCase):
    """#1945 : geste `ameesh work landing`, refus et journal, avant toute écriture."""

    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")
        self.waiting = work.add(self.db, title="waiting", app="acme")["id"]
        self.vehicle = work.add(self.db, title="vehicle", app="acme")["id"]
        # #2171 N3 : le lot parké est en revue (qa), gelé, verdict ok acquis.
        work.move(self.db, self.waiting, "build", actor="test")
        work.move(self.db, self.waiting, "qa", actor="test")
        self.sha = "a" * 40
        work.milestone(self.db, self.vehicle, "frozen", sha=self.sha, actor="test")
        self.lot_sha = "d" * 40
        work.milestone(self.db, self.waiting, "frozen", sha=self.lot_sha, actor="test")
        work.milestone(self.db, self.waiting, "verdict", verdict="ok", sha=self.lot_sha,
                       actor="test")

    def cli(self, *args, ok=True):
        proc = self.mesh("work", "landing", *args, "--actor", "test")
        self.assertEqual(proc.returncode, 0 if ok else 1, proc.stdout + proc.stderr)
        return proc

    def test_declare_renew_clear_and_read_back(self):
        until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        renewed = (datetime.date.fromisoformat(until) + datetime.timedelta(days=2)).isoformat()
        payload = json.loads(self.cli(str(self.waiting), "--vehicle", str(self.vehicle),
                                      "--until", until, "--json").stdout)
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["landing"]["vehicle_id"], self.vehicle)
        self.assertEqual(payload["landing"]["until_on"], until)
        self.assertEqual(payload["landing"]["pinned_sha"], self.sha)
        self.assertTrue(payload["landing"]["open"])
        self.assertEqual(payload["landing"]["created_by"], "test")
        # rejouer remplace la déclaration : une seule ligne, un second journal
        other = work.add(self.db, title="other vehicle", app="acme")["id"]
        other_sha = "b" * 40
        work.milestone(self.db, other, "frozen", sha=other_sha, actor="test")
        payload = json.loads(self.cli(str(self.waiting), "--vehicle", str(other),
                                      "--until", renewed, "--json").stdout)
        self.assertEqual(payload["landing"]["vehicle_id"], other)
        self.assertEqual(payload["landing"]["pinned_sha"], other_sha)
        self.assertEqual(len(storage.of(self.db).work_dependencies.landings_for([self.waiting])), 1)
        notes = [e["note"] for e in work.events(self.db, self.waiting)
                 if e["note"].startswith("Landing")]
        self.assertEqual(notes, [
            "Landing declared: vehicle #%d until %s (frozen %s)" % (other, renewed, other_sha[:12]),
            "Landing declared: vehicle #%d until %s (frozen %s)" % (self.vehicle, until, self.sha[:12])])
        text = self.mesh("work", "show", str(self.waiting))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("landing on vehicle #%d" % other, text.stdout)
        # le retrait est idempotent et journalisé une fois
        self.assertTrue(json.loads(self.cli(str(self.waiting), "--clear", "--json").stdout)["changed"])
        self.assertFalse(json.loads(self.cli(str(self.waiting), "--clear", "--json").stdout)["changed"])
        self.assertEqual(storage.of(self.db).work_dependencies.landings_for([self.waiting]), [])
        self.assertIn("Landing cleared: vehicle #%d" % other,
                      [e["note"] for e in work.events(self.db, self.waiting)])

    def test_refusals_leave_no_row_and_no_journal(self):
        until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        self.assertIn("needs --vehicle and --until", self.cli(str(self.waiting), ok=False).stderr)
        self.assertIn("itself", self.cli(str(self.waiting), "--vehicle", str(self.waiting),
                                         "--until", until, ok=False).stderr)
        self.assertIn("existing lots", self.cli(str(self.waiting), "--vehicle", "999999",
                                                "--until", until, ok=False).stderr)
        self.assertIn("not found", self.cli("999999", "--vehicle", str(self.vehicle),
                                            "--until", until, ok=False).stderr)
        self.assertIn("ISO day", self.cli(str(self.waiting), "--vehicle", str(self.vehicle),
                                          "--until", "someday", ok=False).stderr)
        # un véhicule sans gel ne peut pas être épinglé
        unfrozen = work.add(self.db, title="unfrozen", app="acme")["id"]
        self.assertIn("frozen head", self.cli(str(self.waiting), "--vehicle", str(unfrozen),
                                              "--until", until, ok=False).stderr)
        # un véhicule bloqué est refusé avant écriture
        blocked = work.add(self.db, title="blocked vehicle", app="acme")["id"]
        work.milestone(self.db, blocked, "frozen", sha="c" * 40, actor="test")
        work.move(self.db, blocked, "blocked", actor="test")
        self.assertIn("blocked", self.cli(str(self.waiting), "--vehicle", str(blocked),
                                          "--until", until, ok=False).stderr)
        work.close(self.db, self.vehicle, abandoned=True)
        self.assertIn("must still be open", self.cli(str(self.waiting), "--vehicle",
                                                     str(self.vehicle), "--until", until,
                                                     ok=False).stderr)
        self.assertEqual(storage.of(self.db).work_dependencies.landings_for([self.waiting]), [])
        self.assertEqual([e["note"] for e in work.events(self.db, self.waiting)
                          if e["note"].startswith("Landing")], [])


    def test_refuses_a_lot_that_is_not_in_review_frozen_or_with_acquired_verdict(self):
        until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        lot = work.add(self.db, title="not in review", app="acme")["id"]
        self.assertIn("must be in review (qa)", self.cli(str(lot), "--vehicle", str(self.vehicle),
                                                         "--until", until, ok=False).stderr)
        work.move(self.db, lot, "build", actor="test")
        self.assertIn("must be in review (qa)", self.cli(str(lot), "--vehicle", str(self.vehicle),
                                                         "--until", until, ok=False).stderr)
        work.move(self.db, lot, "qa", actor="test")
        sha = "e" * 40
        work.milestone(self.db, lot, "frozen", sha=sha, actor="test")
        self.assertIn("acquired ok verdict", self.cli(str(lot), "--vehicle", str(self.vehicle),
                                                      "--until", until, ok=False).stderr)
        work.milestone(self.db, lot, "verdict", verdict="blocked", sha=sha, actor="test")
        self.assertIn("acquired ok verdict", self.cli(str(lot), "--vehicle", str(self.vehicle),
                                                      "--until", until, ok=False).stderr)
        # un verdict ok d'une AUTRE tête ne vaut pas pour le dernier gel (avis
        # ancien, écrit avant que la porte #2962 ne refuse une tête inconnue)
        storage.of(self.db).work.add_milestone(
            lot, "verdict", sha="f" * 40, actor="test", verdict="ok", note="")
        self.assertIn("acquired ok verdict", self.cli(str(lot), "--vehicle", str(self.vehicle),
                                                      "--until", until, ok=False).stderr)
        work.milestone(self.db, lot, "verdict", verdict="ok", sha=sha, actor="test")
        self.cli(str(lot), "--vehicle", str(self.vehicle), "--until", until)
        self.assertEqual(len(storage.of(self.db).work_dependencies.landings_for([lot])), 1)

    def test_refuses_a_past_or_too_far_deadline(self):
        today = datetime.date.today()
        for day, needle in (
                (today - datetime.timedelta(days=1), "already past"),
                (today + datetime.timedelta(days=dependencies.DEFAULT_MAX_DAYS + 1), "too far")):
            self.assertIn(needle, self.cli(str(self.waiting), "--vehicle", str(self.vehicle),
                                           "--until", day.isoformat(), ok=False).stderr)
        # la borne haute exacte est admise
        self.cli(str(self.waiting), "--vehicle", str(self.vehicle), "--until",
                 (today + datetime.timedelta(days=dependencies.DEFAULT_MAX_DAYS)).isoformat())

    def test_new_unreviewed_freeze_before_landing_is_rejected_atomically(self):
        transaction = self.db.transaction
        injected = False

        @contextmanager
        def racing_transaction():
            nonlocal injected
            if not injected:
                injected = True
                work.milestone(self.db, self.waiting, "frozen", sha="e" * 40, actor="test")
            with transaction() as tx:
                yield tx

        until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        with mock.patch.object(self.db, "transaction", side_effect=racing_transaction):
            with self.assertRaisesRegex(work.WorkError, "acquired ok verdict"):
                dependencies.land(self.db, self.waiting, self.vehicle,
                                  until_on=until, actor="test")
        self.assertEqual(storage.of(self.db).work_dependencies.landings_for([self.waiting]), [])
        self.assertFalse(any(e["note"].startswith("Landing")
                             for e in work.events(self.db, self.waiting)))


class ResolverTest(unittest.TestCase):
    def test_invalid_landing_ids_days_and_empty_actor_are_refused_before_storage(self):
        for item in (-1, 0, True, "1"):
            with self.assertRaises(work.WorkError):
                dependencies.land(None, item, 2, until_on="2026-10-18", actor="test")
        with self.assertRaises(work.WorkError):
            dependencies.land(None, 1, 2, until_on="2026-10-18", actor="")
        with self.assertRaises(work.WorkError):
            dependencies.clear_landing(None, 1, actor="")
        for day in (None, "", "someday", "2026-13-45"):
            with self.assertRaises(work.WorkError):
                dependencies.land(None, 1, 2, until_on=day, actor="test")

    def test_direct_links_union_with_commitments_and_ignore_satisfied_edges(self):
        rows = [{"id": 1, "state": "build"}]
        edges = [{"work_item_id": 1, "blocker_id": 2, "blocking": True},
                 {"work_item_id": 1, "blocker_id": 4, "blocking": False}]
        commitments = [{"status": "open", "work_item_id": 1, "depends_on": ["#3", "#2"]}]
        self.assertEqual(planned_waits.resolve(rows, {}, commitments, {},
            [{"id": 2, "state": "qa"}, {"id": 3, "state": "build"}], now=0, dependencies=edges),
            {1: {"what": "dependency", "lots": [2, 3]}})
        self.assertEqual(planned_waits.resolve(rows, {1: {"planned_start": "2999-01-01"}},
            [], {}, [], now=0, dependencies=edges),
            {1: {"what": "scheduled", "start": "2999-01-01"}})
        self.assertEqual(planned_waits.resolve([{ "id": 1, "state": "merged"}], {},
            [], {}, [], now=0, dependencies=edges), {})

    def test_invalid_service_ids_and_empty_actor_are_refused_before_storage(self):
        for item in (-1, 0, True, "1"):
            with self.assertRaises(work.WorkError):
                dependencies.add(None, item, 2, actor="test")
        with self.assertRaises(work.WorkError):
            dependencies.add(None, 1, 2, actor="")

    def test_landing_deadline_is_bounded_before_storage(self):
        """#2171 N4 : ni un jour passé, ni une échéance invraisemblable."""
        today = datetime.date.today()
        for day in ((today - datetime.timedelta(days=1)).isoformat(),
                    (today + datetime.timedelta(days=dependencies.DEFAULT_MAX_DAYS + 1)).isoformat()):
            with self.assertRaises(work.WorkError):
                dependencies.land(None, 1, 2, until_on=day, actor="test")

    def test_landing_term_is_an_operating_parameter_not_a_canon_rule(self):
        """#2171 N4 : le terme vient de `AMEESH_LANDING_MAX_DAYS` (§7)."""
        self.assertEqual(dependencies.max_days({}), dependencies.DEFAULT_MAX_DAYS)
        self.assertEqual(dependencies.max_days(
            {dependencies.LANDING_MAX_ENV: "7"}), 7)
        for raw in ("0", "-1", "soon"):
            with self.assertRaises(work.WorkError):
                dependencies.max_days({dependencies.LANDING_MAX_ENV: raw})
        today = datetime.date.today()
        with mock.patch.dict(os.environ, {dependencies.LANDING_MAX_ENV: "7"}):
            with self.assertRaisesRegex(work.WorkError, "at most 7 days ahead"):
                dependencies.land(None, 1, 2, actor="test",
                                  until_on=(today + datetime.timedelta(days=8)).isoformat())

    def test_landing_head_breaks_freeze_ties_on_id_not_reading_order(self):
        """#2171 N2 : même (at, id) maximal que set_landing, quel que soit l'ordre."""
        st = mock.Mock()
        st.progress.lot_milestones.return_value = [
            {"id": 9, "work_item_id": 2, "kind": "frozen", "at_ts": 5.0, "sha": "b" * 40},
            {"id": 3, "work_item_id": 2, "kind": "frozen", "at_ts": 5.0, "sha": "a" * 40},
            {"id": 4, "work_item_id": 2, "kind": "verdict", "at_ts": 9.0, "sha": "c" * 40},
        ]
        with mock.patch.object(dependencies.storage, "of", return_value=st):
            self.assertEqual(dependencies.landing_head(None, 2), "b" * 40)


class _RecordingTx:
    """Transaction factice : enregistre les ordres et rend des réponses fixes."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append(str(sql))

    def query(self, sql, params=()):
        self.calls.append(str(sql))
        return self.responses.pop(0) if self.responses else []


class _RecordingDb:
    """Pilote factice : `transaction()` ouvre la transaction, `execute` non."""

    def __init__(self, tx):
        self.tx = tx
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append(str(sql))

    def transaction(self):
        return _TxContext(self.tx)


class _TxContext:
    def __init__(self, tx):
        self.tx = tx

    def __enter__(self):
        return self.tx

    def __exit__(self, *exc):
        return False


class LandingTransactionLockTest(unittest.TestCase):
    """#2171 N1 : le verrou consultatif part dans la TRANSACTION.

    `self.db.execute(_LOCK)` le posait dans une autre session (pilote psql,
    autocommit) : il était relâché aussitôt, et le « même verrou que le graphe »
    était faux. Le factice discrimine exactement ce chemin."""

    def test_set_and_clear_take_the_graph_lock_on_the_transaction(self):
        tx = _RecordingTx(
            [{"id": 1, "state": "qa"}, {"id": 2, "state": "qa"}],   # les deux lots
            [{"sha": "a" * 40}],                                    # gel du véhicule
        )
        db = _RecordingDb(tx)
        self.assertEqual(PgLandingStore(db).set_landing(
            1, 2, until_on="2026-10-18", actor="test"), {"landed": True})
        clear_tx = _RecordingTx([{"vehicle_id": 2}])
        clear_db = _RecordingDb(clear_tx)
        self.assertTrue(PgLandingStore(clear_db).clear_landing(1, actor="test"))
        for driver, calls in ((db, tx.calls), (clear_db, clear_tx.calls)):
            self.assertEqual(driver.calls, [], "le verrou est parti hors transaction")
            self.assertIn("pg_advisory_xact_lock", calls[0],
                          "le verrou doit précéder toute lecture dans la transaction")

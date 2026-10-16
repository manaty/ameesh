# SPDX-License-Identifier: AGPL-3.0-only
"""L385 storage: explicit edges, atomic cycle checks and durable audit."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from ameesh import db as D, mail, plan, planned_waits, progress, stagnation, storage, work
from ameesh import work_dependencies as dependencies
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
        done = work.merged(self.db, self.b, sha="a" * 40, actor="git:main")
        self.assertEqual(done["result"], "merged")
        [row] = self.inbox()
        self.assertEqual((row["kind"], self.mail_lot(row)), ("event", str(self.a)))
        self.assertIn("lot #%d is merged" % self.b, row["body"])
        self.assertIn("lot #%d" % self.a, row["body"])
        self.assertEqual(mail.message_payload(row)["dependency"],
                         {"dependent": self.a, "blocker": self.b, "blocker_state": "merged"})
        # Rejouer la déclaration de fusion ne réveille pas une seconde fois.
        self.assertEqual(work.merged(self.db, self.b, sha="a" * 40, actor="git:main")["result"],
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
            work.merged(self.db, self.b, sha="b" * 40, actor="git:main")
        self.assertEqual(seen, [("merged", "dev-a", "event", True)])

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
                work.merged(self.db, self.b, sha="c" * 40, actor="git:main")
        self.assertEqual(work.get(self.db, self.b)["state"], "intake")
        self.assertEqual(self.inbox(), [])
        done = work.merged(self.db, self.b, sha="c" * 40, actor="git:main")
        self.assertEqual(done["result"], "merged")
        self.assertEqual(work.get(self.db, self.b)["state"], "merged")
        [row] = self.inbox()
        self.assertEqual(mail.message_payload(row)["dependency"],
                         {"dependent": self.a, "blocker": self.b, "blocker_state": "merged"})
        # Rejouer la déclaration, une fois la fusion commise, ne double pas.
        self.assertEqual(work.merged(self.db, self.b, sha="c" * 40, actor="git:main")["result"],
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
                    results.append(work.merged(db, blocker, sha="e" * 40,
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
                results.append(work.merged(db, self.c, sha="f" * 40, actor="git:main")["result"])
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
        proc = self.mesh("work", "merged", str(self.b), "--sha", "d" * 40, "--actor", "test")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [row] = self.inbox()
        self.assertEqual(self.mail_lot(row), str(self.a))


class ResolverTest(unittest.TestCase):
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

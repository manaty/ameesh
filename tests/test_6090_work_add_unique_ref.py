# SPDX-License-Identifier: AGPL-3.0-only
"""#6090 (manaty/ameesh#4642, écart 2) : `work add --unique-ref`, création
idempotente sur une clé durable.

La clé est écrite sur le lot (colonne `unique_ref`, migration 0555) et un
second appel avec la même clé rend le lot existant, **quel que soit son état**,
au lieu d'en créer un. Le premier contenu gagne : un rejeu avec un autre titre
ne réécrit rien. Deux premiers appels concurrents se sérialisent sous le verrou
consultatif de la clé (le perdant relit le gagnant) ; l'index unique partiel est
la barrière de dernier recours.

Le volet CLI vérifie aussi `--json` (`created`), le texte sur stderr, la clé
vide refusée avant écriture, la clé montrée par `work show`, et une écriture
N-1 (sans la colonne) qui reste lisible et ne bloque pas une clé neuve.
"""
from __future__ import annotations

import json
import threading
from unittest import mock

from ameesh import work
from ameesh.db import DbError

from .test_l119_backlog import _Base

KEY = "ima:thepackengers/issue-manager-agent#1132:1"


class UniqueRefStoreTest(_Base):
    def add(self, **kw):
        kw.setdefault("title", "Création idempotente")
        kw.setdefault("app", "t6090")
        kw.setdefault("actor", "human:proprio")
        return work.add(self.db, **kw)

    def count(self):
        return int(self.db.query("SELECT count(*)::int AS n FROM work_items")[0]["n"])

    def test_second_creation_returns_the_first_lot(self):
        first = self.add(unique_ref=KEY, title="premier")
        self.assertFalse(first.get("existing"))
        self.assertEqual(first["unique_ref"], KEY)
        second = self.add(unique_ref=KEY, title="second")
        self.assertTrue(second.get("existing"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["title"], "premier")
        self.assertEqual(self.count(), 1)

    def test_the_key_holds_after_the_lot_closed(self):
        first = self.add(unique_ref=KEY)
        work.close(self.db, first["id"], abandoned=True, actor="human:proprio")
        second = self.add(unique_ref=KEY)
        self.assertTrue(second.get("existing"))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["state"], "closed")
        self.assertEqual(self.count(), 1)

    def test_a_replay_does_not_rewrite_the_first_content(self):
        first = self.add(unique_ref=KEY, title="premier", priority=3)
        second = self.add(unique_ref=KEY, title="autre", priority=1)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["title"], "premier")
        self.assertEqual(second["priority"], 3)
        self.assertEqual(self.count(), 1)

    def test_issue_ref_can_still_be_shared_by_several_keys(self):
        a = self.add(unique_ref=KEY + ":a", issue_ref="manaty/ameesh#4642")
        b = self.add(unique_ref=KEY + ":b", issue_ref="manaty/ameesh#4642")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.count(), 2)

    def test_without_a_key_creation_is_unchanged(self):
        a = self.add()
        b = self.add(title="second")
        self.assertNotEqual(a["id"], b["id"])
        self.assertIsNone(a["unique_ref"])
        self.assertEqual(self.count(), 2)

    def test_the_key_is_trimmed_and_case_sensitive(self):
        first = self.add(unique_ref="  %s  " % KEY)
        self.assertEqual(first["unique_ref"], KEY)
        self.assertEqual(self.add(unique_ref=KEY)["id"], first["id"])
        other = self.add(unique_ref=KEY.upper())
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(self.count(), 2)

    def test_an_empty_a_bool_or_an_overlong_key_is_refused_before_writing(self):
        for bad in ("", "   ", True, "x" * (work.UNIQUE_REF_MAX + 1)):
            with self.assertRaises(work.WorkError):
                self.add(unique_ref=bad)
        self.assertEqual(self.count(), 0)

    def test_the_index_is_unique_and_partial(self):
        rows = self.db.query(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema()"
            " AND indexname = 'work_items_unique_ref_idx'")
        self.assertEqual(len(rows), 1)
        definition = rows[0]["indexdef"]
        self.assertIn("UNIQUE INDEX", definition.upper())
        self.assertIn("unique_ref IS NOT NULL", definition)
        # NULL n'est pas une valeur : plusieurs créations ordinaires coexistent
        self.add()
        self.add(title="autre")
        self.assertEqual(self.count(), 2)

    def test_the_index_refuses_a_second_row_with_the_same_key(self):
        self.add(unique_ref=KEY)
        other = self.add(title="sans clé")
        with self.assertRaises(DbError):
            self.db.execute("UPDATE work_items SET unique_ref = %s WHERE id = %s",
                            (KEY, other["id"]))

    def test_previous_writer_without_the_column_reads_and_leaves_the_key_free(self):
        """#5690 : l'écrivain N-1 n'écrit pas `unique_ref` ; la ligne reste
        lisible (NULL) et une clé neuve peut se créer ensuite."""
        self.previous_insert(
            "work_items",
            {"type": "evolution", "source": "", "app": "t6090",
             "title": "écrit par N-1", "body": ""},
            added=("unique_ref",))
        rows = self.db.query("SELECT id FROM work_items WHERE title = 'écrit par N-1'")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(work.get(self.db, rows[0]["id"])["unique_ref"])
        created = self.add(unique_ref=KEY)
        self.assertNotEqual(created["id"], rows[0]["id"])
        self.assertEqual(self.add(unique_ref=KEY)["id"], created["id"])

    def test_two_concurrent_first_calls_create_one_lot(self):
        """La course est fermée dans le magasin, pas seulement par la lecture
        préalable : les deux appels entrent sans voir de lot."""
        barrier = threading.Barrier(2, timeout=30)
        results: list[dict] = []
        errors: list[BaseException] = []

        def attempt() -> None:
            db = self.connect()
            try:
                barrier.wait()
                results.append(work.add(
                    db, title="concurrent", app="t6090", actor="human:proprio",
                    unique_ref=KEY))
            except BaseException as exc:  # noqa: BLE001 - remonté par le témoin
                errors.append(exc)
            finally:
                db.close()

        with mock.patch.object(work, "existing_by_unique_ref", return_value=None):
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len({int(item["id"]) for item in results}), 1)
        self.assertEqual(sum(1 for item in results if item.get("existing")), 1)
        self.assertEqual(self.count(), 1)


class UniqueRefCliTest(_Base):
    def mesh_add(self, *extra, key=KEY, title="geste ima"):
        return self.mesh("work", "add", "--title", title, "--app", "t6090",
                         "--new-project", "--actor", "human:proprio",
                         "--unique-ref", key, "--json", *extra)

    def count(self):
        return int(self.db.query("SELECT count(*)::int AS n FROM work_items")[0]["n"])

    def test_cli_json_says_created_then_existing(self):
        first = self.mesh_add()
        self.assertEqual(first.returncode, 0, first.stderr)
        out = json.loads(first.stdout)
        self.assertEqual(out["schema"], "ameesh.work.add/1")
        self.assertTrue(out["created"])
        self.assertEqual(out["lot"]["unique_ref"], KEY)
        # le texte habituel reste sur stderr
        self.assertIn("#%d created" % out["lot"]["id"], first.stderr)

        second = self.mesh_add(title="autre titre")
        self.assertEqual(second.returncode, 0, second.stderr)
        replay = json.loads(second.stdout)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["lot"]["id"], out["lot"]["id"])
        self.assertEqual(replay["lot"]["title"], "geste ima")
        self.assertIn("already exists", second.stderr)
        self.assertNotIn("created in", second.stderr)
        self.assertEqual(self.count(), 1)

    def test_cli_replay_after_close_returns_the_closed_lot(self):
        first = json.loads(self.mesh_add().stdout)
        work.close(self.db, first["lot"]["id"], abandoned=True, actor="human:proprio")
        second = self.mesh_add()
        self.assertEqual(second.returncode, 0, second.stderr)
        replay = json.loads(second.stdout)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["lot"]["id"], first["lot"]["id"])
        self.assertEqual(replay["lot"]["state"], "closed")
        self.assertEqual(self.count(), 1)

    def test_cli_an_empty_key_is_refused_before_writing(self):
        proc = self.mesh_add(key="   ")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("non-empty key", proc.stderr)
        self.assertEqual(self.count(), 0)

    def test_cli_a_refused_creation_does_not_consume_the_key(self):
        refused = self.mesh_add("--parent-lot", "999999")
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.count(), 0)
        created = self.mesh_add()
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(json.loads(created.stdout)["created"])

    def test_work_show_exposes_the_key(self):
        lot_id = json.loads(self.mesh_add().stdout)["lot"]["id"]
        detail = self.mesh("work", "show", str(lot_id), "--json")
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertEqual(json.loads(detail.stdout)["unique_ref"], KEY)
        text = self.mesh("work", "show", str(lot_id))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("unique-ref: %s" % KEY, text.stdout)

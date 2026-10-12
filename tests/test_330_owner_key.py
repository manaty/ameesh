# SPDX-License-Identifier: AGPL-3.0-only
"""#330: the owner's key is registered without any agent (`key register
--human`), in `key_holders`; a signed decision answered by `human:alice`
signs under `alice` by default and is accepted; the identity holds a key and
nothing else — no registry row, so no runner, attach, project or alert sees
it; the stopgap registry row of 2026-10-12 is copied there (migration 0364)
and leaves the registry at `key register --human`.

#326 r1: a key is never overwritten — a first key only, a first owner key
only while none exists (alert `owner_key_registered`); a revocation is free;
replacing, rotating or re-registering after a revocation is the owner's own
gesture (`--replace`, the database's owner role); no agent is created under a
holder's name."""
from __future__ import annotations

import os
import threading
import time

from ameesh import authority, decisions, exploitation, registry, signing, storage
from ameesh.db import DbError

from .test_l124_decisions import ALICE, _Base

MIGRATION = os.path.join(os.path.dirname(__file__), os.pardir, "src", "ameesh",
                         "migrations", "0364_key_holders.sql")


class _Keys(_Base):
    def keypair(self, name="alice"):
        private, public, _fp = signing.write_keypair(os.path.join(self.tmp, "keys-" + name),
                                                     name)
        return private, public

    def no_key_yet(self):
        """An install where no key was ever registered: the bootstrap open
        again. Only the tables' owner can (TRUNCATE skips the row triggers;
        the executor has no TRUNCATE right) — a test's reset, never a path
        of ameesh."""
        self.db.execute("TRUNCATE key_holders, key_events RESTART IDENTITY")

    def registry_names(self):
        return {r["name"] for r in self.db.query("SELECT name FROM agent_registry")}


class OwnerKeyTest(_Keys):

    def test_human_key_signs_decisions_without_an_agent(self):
        private, public = self.keypair()
        out = authority.register_human_key(self.db, ALICE, signing.read_public(public),
                                           role="owner")
        self.assertEqual((out["agent"], out["human"], out["role"]),
                         ("alice", ALICE, "owner"))
        self.assertNotIn("alice", self.registry_names())
        info = authority.key_info(self.db, "alice")
        self.assertEqual((info["holder"], info["key_role"]), ("human", "owner"))
        view = self.ask(needs_signature=True)
        answered = self.answer(view["id"], "a", key_path=private)
        proof = answered["decision"]["answer"]["proof"]
        self.assertEqual(proof["approver"], "alice")
        self.assertTrue(decisions.verify_proof(self.db, answered["decision"])[0])
        self.assertEqual([(r["name"], r["holder"]) for r in storage.of(self.db).keys.registered()],
                         [("alice", "human")])
        # no agent is created later under that name (#326 r1)
        with self.assertRaises(DbError) as ctx:
            registry.upsert(self.db, "alice", harness="claude", host=self.cfg.host)
        self.assertIn("holds a human's key", str(ctx.exception))
        self.assertNotIn("alice", self.registry_names())
        self.assertEqual(authority.key_info(self.db, "alice")["holder"], "human")
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_key(self.db, "alice", signing.read_public(public), role="owner")
        self.assertIn("--human alice", str(ctx.exception))
        # revoked: the proof is worth nothing any more
        self.assertTrue(authority.revoke_key(self.db, "alice"))
        self.assertFalse(decisions.verify_proof(self.db, decisions.get(self.db, view["id"]))[0])

    def test_never_on_an_agent_identity(self):
        _private, public = self.keypair("orch")
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_human_key(self.db, "human:orch", signing.read_public(public),
                                         role="owner")
        self.assertIn("an agent is named orch", str(ctx.exception))
        self.assertIsNone(authority.key_info(self.db, "orch").get("public_key"))
        # the old error now gives the command
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_key(self.db, "smichea", signing.read_public(public), role="owner")
        self.assertIn("ameesh key register --human smichea", str(ctx.exception))

    def test_cli_register_human(self):
        _private, public = self.keypair()
        proc = self.mesh("key", "register", "--public-key", public, "--role", "owner",
                         env=self.mesh_env())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        proc = self.mesh("key", "register", "--human", "alice", "--public-key", public,
                         "--role", "owner", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("human:alice, key holder only", proc.stdout)
        proc = self.mesh("key", "show", "alice", env=self.mesh_env())
        self.assertIn("human", proc.stdout)
        proc = self.mesh("key", "register", "--human", "orch", "--public-key", public,
                         env=self.mesh_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused: an agent is named orch", proc.stderr)

    def test_stopgap_row_moves_out_of_the_registry(self):
        # exactly the registry row made by SQL on 2026-10-12, its key
        # registered meanwhile
        registry.upsert(self.db, "smichea", harness="none", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET mode = 'externe', status = 'stopped', "
                        "responsible = 'human:smichea' WHERE name = 'smichea'")
        _private, public = self.keypair("smichea")
        authority.register_key(self.db, "smichea", signing.read_public(public), role="owner")
        # a row like it under another name is not the stopgap: left alone
        registry.upsert(self.db, "bob", harness="none", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET mode = 'externe', status = 'stopped', "
                        "responsible = 'human:bob' WHERE name = 'bob'")
        authority.register_key(self.db, "bob", signing.read_public(public), role="agent")
        with open(MIGRATION, encoding="utf-8") as fh:
            self.db.script(fh.read())
        # copied with its key, read from key_holders at once (additive: the
        # row stays until `key register --human`)
        info = authority.key_info(self.db, "smichea")
        self.assertEqual((info["holder"], info["key_role"]), ("human", "owner"))
        self.assertEqual(authority.key_info(self.db, "bob")["holder"], "agent")
        # the holder exists: registering again is a replacement
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_human_key(self.db, "human:smichea", signing.read_public(public),
                                         role="owner")
        self.assertIn("already holds a key", str(ctx.exception))
        self.assertNotIn("smichea", self.registry_names())
        self.assertIn("bob", self.registry_names())
        # a human's key never absorbs an agent of that name
        with self.assertRaises(authority.AuthorityError):
            authority.register_human_key(self.db, "human:bob", signing.read_public(public))
        self.assertIn("bob", self.registry_names())

    def test_stopgap_with_a_session_is_not_the_stopgap(self):
        registry.upsert(self.db, "smichea", harness="none", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET mode = 'externe', status = 'stopped', "
                        "responsible = 'human:smichea', session_id = 's-1' "
                        "WHERE name = 'smichea'")
        _private, public = self.keypair("smichea")
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_human_key(self.db, "human:smichea", signing.read_public(public),
                                         role="owner")
        self.assertIn("an agent is named smichea", str(ctx.exception))
        self.assertIn("smichea", self.registry_names())


class OwnerKeyGuardTest(_Keys):
    """#326 r1: who may write `key_holders` (trigger of 0364)."""

    def setUp(self):
        super().setUp()
        _private, self.public = self.keypair("alice")
        _private, self.other = self.keypair("mallory")
        self.first = authority.register_human_key(
            self.db, ALICE, signing.read_public(self.public), role="owner")

    def fingerprint(self, name):
        return authority.key_info(self.db, name)["public_key_fingerprint"]

    def test_no_overwrite_no_second_owner_key(self):
        self.assertEqual(self.first["status"], "created")
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_human_key(self.db, ALICE, signing.read_public(self.other),
                                         role="owner")
        self.assertIn("never overwritten", str(ctx.exception))
        with self.assertRaises(authority.AuthorityError) as ctx:
            authority.register_human_key(self.db, "human:mallory",
                                         signing.read_public(self.other), role="owner")
        self.assertIn("an owner key already exists", str(ctx.exception))
        self.assertIsNone(authority.key_info(self.db, "mallory"))
        self.assertEqual(self.fingerprint("alice"), self.first["fingerprint"])
        # a holder's agent key is a first key: allowed
        out = authority.register_human_key(self.db, "human:carol",
                                           signing.read_public(self.other))
        self.assertEqual((out["status"], out["role"]), ("created", "agent"))

    def test_the_table_itself_refuses(self):
        # straight SQL, as any agent could: no update, no delete
        for sql in ("UPDATE key_holders SET public_key = 'x' WHERE name = 'alice'",
                    "UPDATE key_holders SET key_revoked_at = now(), public_key = 'x' "
                    "WHERE name = 'alice'",
                    "DELETE FROM key_holders WHERE name = 'alice'",
                    "INSERT INTO key_holders (name, human, key_role) "
                    "VALUES ('mallory', 'human:mallory', 'owner')"):
            with self.assertRaises(DbError, msg=sql) as ctx:
                self.db.execute(sql)
            self.assertIn("own gesture", str(ctx.exception))
        self.assertEqual(self.fingerprint("alice"), self.first["fingerprint"])

    def test_revoked_key_is_never_overwritten(self):
        self.assertTrue(authority.revoke_key(self.db, "alice"))
        with self.assertRaises(authority.AuthorityError):
            authority.register_human_key(self.db, ALICE, signing.read_public(self.other),
                                         role="owner")
        with self.assertRaises(DbError):
            self.db.execute("UPDATE key_holders SET key_revoked_at = NULL WHERE name = 'alice'")
        # a revoked owner key still counts: no other first owner key
        with self.assertRaises(authority.AuthorityError):
            authority.register_human_key(self.db, "human:mallory",
                                         signing.read_public(self.other), role="owner")
        self.assertTrue(authority.key_info(self.db, "alice")["key_revoked_ts"])

    def test_replace_is_the_owner_gesture(self):
        # the test role owns the tables (it ran the migrations): the owner's role
        self.assertTrue(authority.revoke_key(self.db, "alice"))
        out = authority.register_human_key(self.db, ALICE, signing.read_public(self.other),
                                           role="owner", replace=True)
        self.assertEqual(out["status"], "replaced")
        info = authority.key_info(self.db, "alice")
        self.assertEqual((info["public_key_fingerprint"], info["key_revoked_ts"]),
                         (out["fingerprint"], None))
        # the setting outside the gesture's transaction: gone
        with self.assertRaises(DbError):
            self.db.execute("UPDATE key_holders SET public_key = 'x' WHERE name = 'alice'")

    def test_cli_replace(self):
        args = ("key", "register", "--human", "alice", "--public-key", self.other,
                "--role", "owner")
        proc = self.mesh(*args, env=self.mesh_env())
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused: alice already holds a key", proc.stderr)
        proc = self.mesh(*args, "--replace", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("key of alice replaced", proc.stdout)
        self.assertEqual(self.fingerprint("alice"), signing.fingerprint(
            signing.read_public(self.other)))
        proc = self.mesh("key", "register", "orch", "--public-key", self.other, "--replace",
                         env=self.mesh_env())
        self.assertEqual(proc.returncode, 2)

    def test_alert_and_note_on_a_first_owner_key(self):
        alerts = exploitation._owner_key_registered(self.db, time.time())
        self.assertEqual([(a["type"], a["reason"], a["agent"], a["holder"]) for a in alerts],
                         [("owner_key_registered", "alice#1", None, "human")])
        self.assertIsNone(alerts[0].get("responsible"))
        self.assertIn("ameesh key confirm alice", alerts[0]["detail"])
        # #326 r3: not the owner's gesture — no time limit, until confirmed
        self.assertEqual(len(exploitation._owner_key_registered(self.db,
                                                                time.time() + 30 * 86400)), 1)
        # #326 r2: a revocation right after use does not erase it
        authority.revoke_key(self.db, "alice")
        self.assertEqual(len(exploitation._owner_key_registered(self.db, time.time())), 1)
        proc = self.mesh("key", "confirm", "alice", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(exploitation._owner_key_registered(self.db, time.time()), [])
        with self.assertRaises(authority.AuthorityError):
            authority.confirm_key(self.db, "nobody")
        # the CLI writes to the owner's chat, if it exists
        registry.upsert(self.db, "chat-dave", harness="claude", host=self.cfg.host)
        _private, public = self.keypair("dave")
        with self.assertRaises(authority.AuthorityError):    # alice's, revoked, counts
            authority.register_human_key(self.db, "human:dave", signing.read_public(public),
                                         role="owner")
        self.no_key_yet()
        proc = self.mesh("key", "register", "--human", "dave", "--public-key", public,
                         "--role", "owner", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.db.query("SELECT body FROM agent_mailbox WHERE recipient = 'chat-dave'")
        self.assertEqual(len(rows), 1)
        self.assertIn("An owner key was created for human:dave", rows[0]["body"])

    def test_owner_key_on_an_agent_is_the_owner_gesture(self):
        # #326 r2: straight SQL, as any agent could — no GUC
        registry.upsert(self.db, "mallory", harness="claude", host=self.cfg.host)
        fingerprint = signing.fingerprint(signing.read_public(self.other))
        for sql in ("UPDATE agent_registry SET public_key = 'k', public_key_fingerprint = "
                    "'%s', key_role = 'owner' WHERE name = 'mallory'" % fingerprint,
                    "INSERT INTO agent_registry (name, public_key, key_role) "
                    "VALUES ('mallory2', 'k', 'owner')"):
            with self.assertRaises(DbError, msg=sql) as ctx:
                self.db.execute(sql)
            self.assertIn("own gesture", str(ctx.exception))
        # an agent key stays free; turning it owner, or renaming an owner, is not
        authority.register_key(self.db, "mallory", signing.read_public(self.other))
        with self.assertRaises(DbError):
            self.db.execute("UPDATE agent_registry SET key_role = 'owner' "
                            "WHERE name = 'mallory'")
        # the owner's gesture: `key register <agent> --role owner` with its role
        authority.register_key(self.db, "orch", signing.read_public(self.other), role="owner")
        self.assertEqual(authority.key_info(self.db, "orch")["key_role"], "owner")
        with self.assertRaises(DbError):
            self.db.execute("UPDATE agent_registry SET name = 'orch2' WHERE name = 'orch'")
        self.assertTrue(authority.revoke_key(self.db, "orch"))
        with self.assertRaises(DbError):
            self.db.execute("UPDATE agent_registry SET key_revoked_at = NULL "
                            "WHERE name = 'orch'")
        events = [(r["name"], r["holder"], r["event"])
                  for r in self.db.query("SELECT * FROM key_events ORDER BY id")]
        self.assertEqual(events, [("alice", "human", "registered"),
                                  ("mallory", "agent", "registered"),
                                  ("orch", "agent", "registered"),
                                  ("orch", "agent", "revoked")])
        self.assertEqual([a["reason"] for a in
                          exploitation._owner_key_registered(self.db, time.time())],
                         ["alice#1", "orch#3"])

    def test_dates_are_the_servers_and_the_journal_is_untouchable(self):
        self.no_key_yet()
        self.db.execute("INSERT INTO key_holders (name, human, public_key, key_role, "
                        "key_updated_at, created_at) VALUES ('mallory', 'human:mallory', "
                        "'k', 'owner', now() - interval '30 days', "
                        "now() - interval '30 days')")
        self.assertLess(time.time() - authority.key_info(self.db, "mallory")["key_updated_ts"],
                        60)
        self.assertEqual([a["reason"] for a in
                          exploitation._owner_key_registered(self.db, time.time())],
                         ["mallory#1"])
        for sql in ("UPDATE key_events SET at = now() - interval '30 days'",
                    "DELETE FROM key_events",
                    "INSERT INTO key_events (name, holder, event) "
                    "VALUES ('x', 'human', 'revoked')"):
            with self.assertRaises(DbError, msg=sql):
                self.db.execute(sql)
        self.assertEqual(len(self.db.query("SELECT 1 FROM key_events")), 1)

    def test_a_holder_never_shadows_an_agent(self):
        # #326 r3: an agent with an owner key, no owner key holder
        self.no_key_yet()
        authority.register_key(self.db, "orch", signing.read_public(self.public), role="owner")
        for sql in ("INSERT INTO key_holders (name, human, public_key, public_key_fingerprint, "
                    "key_role) VALUES ('orch', 'human:orch', 'k', 'f', 'owner')",
                    "INSERT INTO key_holders (name, human, public_key, public_key_fingerprint, "
                    "key_role) VALUES ('orch', 'human:orch', 'k', 'f', 'agent')",
                    # the agents' owner keys count: no other first owner key
                    "INSERT INTO key_holders (name, human, public_key, key_role) "
                    "VALUES ('mallory', 'human:mallory', 'k', 'owner')"):
            with self.assertRaises(DbError, msg=sql):
                self.db.execute(sql)
        info = authority.key_info(self.db, "orch")
        self.assertEqual((info["holder"], info["public_key_fingerprint"]),
                         ("agent", signing.fingerprint(signing.read_public(self.public))))
        # a name both, with two keys (a race, a row past the triggers): no key
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            tx.execute("INSERT INTO key_holders (name, human, public_key, "
                       "public_key_fingerprint, key_role) "
                       "VALUES ('orch', 'human:orch', 'k', 'f', 'owner')")
        self.assertIsNone(authority.key_info(self.db, "orch"))

    def test_event_dates_are_the_events_not_the_transactions(self):
        # #326 r3: a transaction opened long before the insert
        self.no_key_yet()
        with self.db.transaction() as tx:
            tx.query("SELECT pg_sleep(1.5)")
            tx.execute("INSERT INTO key_holders (name, human, public_key, key_role) "
                       "VALUES ('late', 'human:late', 'k', 'owner')")
            lag = tx.query(
                "SELECT extract(epoch from (SELECT at FROM key_events WHERE name = 'late') "
                "- now())::float8 AS e, extract(epoch from (SELECT key_updated_at "
                "FROM key_holders WHERE name = 'late') - now())::float8 AS k")[0]
        self.assertGreater(lag["e"], 1.0)
        self.assertGreater(lag["k"], 1.0)

    def test_two_first_owner_keys_at_once(self):
        # #326 r2: two transactions, neither seeing the other's owner key
        self.no_key_yet()
        first = self.db.transaction()
        first.__enter__()
        first.execute("INSERT INTO key_holders (name, human, public_key, key_role) "
                      "VALUES ('bootstrap', 'human:bootstrap', 'k1', 'owner')")
        outcome = {}

        def second():
            try:
                with self.db.transaction() as tx:
                    tx.execute("INSERT INTO key_holders (name, human, public_key, key_role) "
                               "VALUES ('mallory', 'human:mallory', 'k2', 'owner')")
                outcome["second"] = "committed"
            except DbError as exc:
                outcome["second"] = str(exc)

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(2)              # blocked on the unique index until the first ends
        first.__exit__(None, None, None)
        thread.join(30)
        self.assertIn("key_holders_one_owner", outcome["second"])
        self.assertEqual([r["name"] for r in self.db.query(
            "SELECT name FROM key_holders WHERE key_role = 'owner'")], ["bootstrap"])

    def test_taking_an_owner_key_away_never_reopens_the_bootstrap(self):
        # #326 r4 P1: an agent with an owner key, no owner key holder
        self.no_key_yet()
        authority.register_key(self.db, "orch", signing.read_public(self.public), role="owner")
        # straight SQL, as any agent could: a revocation is free...
        self.assertTrue(authority.revoke_key(self.db, "orch"))
        # ...taking the key away is not, revoked or not
        for sql in ("UPDATE agent_registry SET public_key = NULL, "
                    "public_key_fingerprint = NULL, key_role = 'agent' WHERE name = 'orch'",
                    "UPDATE agent_registry SET key_role = 'agent' WHERE name = 'orch'",
                    "UPDATE agent_registry SET public_key = 'k', "
                    "public_key_fingerprint = 'f' WHERE name = 'orch'",
                    "DELETE FROM agent_registry WHERE name = 'orch'"):
            with self.assertRaises(DbError, msg=sql) as ctx:
                self.db.execute(sql)
            self.assertIn("own gesture", str(ctx.exception))
        # even once the owner took it away, a first owner key never comes back
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            tx.execute("DELETE FROM agent_registry WHERE name = 'orch'")
        self.assertEqual(self.db.query("SELECT 1 FROM agent_registry "
                                       "WHERE key_role = 'owner'"), [])
        with self.assertRaises(DbError) as ctx:
            self.db.execute("INSERT INTO key_holders (name, human, public_key, "
                            "public_key_fingerprint, key_role) "
                            "VALUES ('mallory', 'human:mallory', 'k', 'f', 'owner')")
        self.assertIn("an owner key already exists", str(ctx.exception))
        # an agent key stays the agent's to clear
        registry.upsert(self.db, "worker", harness="claude", host=self.cfg.host)
        authority.register_key(self.db, "worker", signing.read_public(self.other))
        self.db.execute("UPDATE agent_registry SET public_key = NULL, "
                        "public_key_fingerprint = NULL WHERE name = 'worker'")

    def test_no_agent_renamed_onto_a_holder(self):
        # #326 r4 P2: alice holds the owner key (setUp)
        registry.upsert(self.db, "intruder", harness="claude", host=self.cfg.host)
        authority.register_key(self.db, "intruder", signing.read_public(self.other))
        with self.assertRaises(DbError) as ctx:
            self.db.execute("UPDATE agent_registry SET name = 'alice' "
                            "WHERE name = 'intruder'")
        self.assertIn("holds a human's key", str(ctx.exception))
        self.assertEqual(self.fingerprint("alice"), self.first["fingerprint"])
        # a name both, with two keys (a row past the triggers): no key; once
        # the agent's key is revoked, the holder's is back
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            tx.execute("INSERT INTO key_holders (name, human, public_key, "
                       "public_key_fingerprint, key_role) "
                       "VALUES ('intruder', 'human:bob', 'k', 'f', 'agent')")
        self.assertIsNone(authority.key_info(self.db, "intruder"))
        self.db.execute("UPDATE agent_registry SET key_revoked_at = now() "
                        "WHERE name = 'intruder'")
        info = authority.key_info(self.db, "intruder")
        self.assertEqual((info["holder"], info["public_key_fingerprint"]), ("human", "f"))

    def test_a_confirmation_is_ordered_by_the_clock_and_names_its_key(self):
        # #326 r4 P2: ids are a sequence's values, the order is the server's clock
        self.no_key_yet()
        self.db.query("SELECT setval('key_events_id_seq', 1000, true)")
        authority.register_human_key(self.db, ALICE, signing.read_public(self.public),
                                     role="owner")                      # event 1001
        self.db.query("SELECT setval('key_events_id_seq', 1, false)")
        self.assertEqual(authority.confirm_key(self.db, "alice"), 1)
        self.assertEqual(exploitation._owner_key_registered(self.db, time.time()), [])
        confirmed = self.db.query("SELECT fingerprint FROM key_events "
                                  "WHERE event = 'confirmed'")
        self.assertEqual([r["fingerprint"] for r in confirmed],
                         [signing.fingerprint(signing.read_public(self.public))])


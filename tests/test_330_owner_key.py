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
        alerts = [a for a in exploitation._owner_key_registered(self.db, time.time())]
        self.assertEqual([(a["type"], a["reason"], a["agent"]) for a in alerts],
                         [("owner_key_registered", "alice", None)])
        self.assertIsNone(alerts[0].get("responsible"))
        self.assertEqual(exploitation._owner_key_registered(self.db, time.time() + 8 * 86400),
                         [])
        authority.revoke_key(self.db, "alice")
        self.assertEqual(exploitation._owner_key_registered(self.db, time.time()), [])
        # the CLI writes to the owner's chat, if it exists
        registry.upsert(self.db, "chat-dave", harness="claude", host=self.cfg.host)
        _private, public = self.keypair("dave")
        with self.assertRaises(authority.AuthorityError):    # alice's, revoked, counts
            authority.register_human_key(self.db, "human:dave", signing.read_public(public),
                                         role="owner")
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL ameesh.owner_gesture = 'on'")
            tx.execute("DELETE FROM key_holders")
        proc = self.mesh("key", "register", "--human", "dave", "--public-key", public,
                         "--role", "owner", env=self.mesh_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = self.db.query("SELECT body FROM agent_mailbox WHERE recipient = 'chat-dave'")
        self.assertEqual(len(rows), 1)
        self.assertIn("An owner key was created for human:dave", rows[0]["body"])

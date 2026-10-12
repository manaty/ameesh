# SPDX-License-Identifier: AGPL-3.0-only
"""#330: the owner's key is registered without any agent (`key register
--human`), in `key_holders`; a signed decision answered by `human:alice`
signs under `alice` by default and is accepted; the identity holds a key and
nothing else — no registry row, so no runner, attach, project or alert sees
it; the stopgap registry row of 2026-10-12 is copied there (migration 0364)
and leaves the registry at `key register --human`."""
from __future__ import annotations

import os

from ameesh import authority, decisions, registry, signing, storage

from .test_l124_decisions import ALICE, _Base

MIGRATION = os.path.join(os.path.dirname(__file__), os.pardir, "src", "ameesh",
                         "migrations", "0364_key_holders.sql")


class OwnerKeyTest(_Base):
    def keypair(self, name="alice"):
        private, public, _fp = signing.write_keypair(os.path.join(self.tmp, "keys-" + name),
                                                     name)
        return private, public

    def registry_names(self):
        return {r["name"] for r in self.db.query("SELECT name FROM agent_registry")}

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
        # an agent row created later under that name never shadows the key
        registry.upsert(self.db, "alice", harness="claude", host=self.cfg.host)
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
        # the registry row made by SQL on 2026-10-12, its key registered meanwhile
        registry.upsert(self.db, "alice", harness="none", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET mode = 'externe', status = 'stopped', "
                        "responsible = %s WHERE name = 'alice'", (ALICE,))
        private, public = self.keypair()
        authority.register_key(self.db, "alice", signing.read_public(public), role="owner")
        # a real external agent of a human is left alone
        registry.upsert(self.db, "bob-session", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET mode = 'externe', responsible = 'human:bob' "
                        "WHERE name = 'bob-session'")
        with open(MIGRATION, encoding="utf-8") as fh:
            sql = "\n".join(line for line in fh if not line.lstrip().startswith("--"))
        for statement in sql.split(";"):
            if statement.strip():
                self.db.execute(statement)
        # copied with its key, read from key_holders at once (additive: the
        # row stays until `key register --human`)
        info = authority.key_info(self.db, "alice")
        self.assertEqual((info["holder"], info["key_role"]), ("human", "owner"))
        view = self.ask(needs_signature=True)
        answered = self.answer(view["id"], "a", key_path=private)
        self.assertTrue(decisions.verify_proof(self.db, answered["decision"])[0])
        authority.register_human_key(self.db, ALICE, signing.read_public(public), role="owner")
        self.assertNotIn("alice", self.registry_names())
        self.assertIn("bob-session", self.registry_names())
        # a human's key never absorbs a real external session of that human
        with self.assertRaises(authority.AuthorityError):
            authority.register_human_key(self.db, "human:bob-session",
                                         signing.read_public(public), role="owner")
        self.assertIn("bob-session", self.registry_names())

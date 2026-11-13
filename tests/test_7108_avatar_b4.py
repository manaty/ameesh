# SPDX-License-Identifier: AGPL-3.0-only
"""#7108 (avatar B4, design 5322 §3.5): `agent move` for `releve: preference`.

The manual path takes an avatar from a dedicated host to a shared (tenant)
host and back, with the conversation carried by `conversation_moves`, never
by a copied session or credential file. Every preflight refusal is proven to
happen before any effect.
"""
from __future__ import annotations

import dataclasses
import json
import time
import unittest
import uuid
from unittest import mock

from ameesh import conversation_move, election, registry, relocation, storage, tenancy

from . import test_canon
from .test_canon import fiche, write
from .test_placement import host_card

ALICE, BRUNO = "human:alice", "human:bruno"
AVATAR = "alice-avatar"
ATELIER, BANC = "atelier", "banc"


def _tables_reset(db) -> None:
    # FK order: handoff events before handoffs, handoffs before the registry.
    for table in ("host_handoff_events", "host_handoffs", "conversation_moves",
                  "agent_candidates", "agent_registry", "runner_presence",
                  "host_tenants", "host_tenancy", "role_standings",
                  "host_hold_events", "host_holds", "host_lifecycle",
                  "host_resources", "canon_state"):
        db.execute("DELETE FROM " + table)


class _AvatarCase(test_canon._CanonDbCase):
    """One avatar admitted on a dedicated poste (`atelier`) and a shared host."""

    def setUp(self) -> None:
        super().setUp()
        _tables_reset(self.db)
        write(self.root, "hotes/banc.md", host_card(
            "banc", ALICE,
            harnesses=["deepseek", "codex"],
            providers=["deepseek", "openai"],
            credential_modes=["api-key"],
            workspaces="{roots: {acme-web: ~/acme/acme-web}}",
            max_agents="2", shared="true", tenants=[ALICE, BRUNO]))
        write(self.root, "hotes/atelier.md", host_card(
            "atelier", "human:bruno",
            harnesses=["claude", "codex", "deepseek"],
            providers=["anthropic", "openai", "deepseek"],
            credential_modes=["api-key", "subscription"],
            workspaces="{roots: {acme-web: ~/acme/acme-web}}",
            max_agents="4"))
        write(self.root, "agents/alice-avatar.md", fiche(
            type="Agent", title=AVATAR, responsible=ALICE, team="acme-web",
            capabilities=["read", "report-drift"], harness="codex",
            provider="openai", credential_mode="api-key", releve="preference"))
        write(self.root, "placements/alice-avatar.md", fiche(
            type="Placement", title=AVATAR, agent=AVATAR,
            hosts=[ATELIER, BANC], credential_mode="api-key"))
        self.sync(ATELIER)
        self.sync(BANC)
        self.assertEqual(self.row(AVATAR)["host"], ATELIER)
        self.assertEqual(self.row(AVATAR)["candidats"], [ATELIER, BANC])

    # -- fixtures ----------------------------------------------------------
    def source(self) -> dict:
        """A live source lease and a native session on the source host."""
        self.db.execute(
            "UPDATE agent_registry SET session_id = 'sess-source',"
            " lease_owner = 'runner-source', lease_epoch = 3,"
            " lease_expires_at = clock_timestamp() + interval '5 minutes',"
            " status = 'idle', status_text = '' WHERE name = %s", (AVATAR,))
        return self.row(AVATAR)

    def ready(self, host: str = BANC, *, tenant: bool = True,
              profiles: list[str] | None = None) -> None:
        """Make `host` a present candidate (reading, standing, canon, tenant)."""
        self.db.execute(
            "DELETE FROM host_resources WHERE host = %s", (host,))
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, load1, cpu_count,"
            " mem_available_bytes, turns_in_progress)"
            " VALUES (%s, clock_timestamp(), 0.1, 4, %s, 0)",
            (host, 8 * 1024 ** 3))
        self.db.execute(
            "DELETE FROM role_standings WHERE name = %s AND host = %s", (AVATAR, host))
        self.db.execute(
            "INSERT INTO role_standings (name, host, evaluated_at, availability,"
            " capacity, score, ready, diagnostic)"
            " VALUES (%s, %s, clock_timestamp(), 99.0, 0.5, 99.5, true, '')",
            (AVATAR, host))
        self.db.execute("DELETE FROM host_lifecycle WHERE host = %s", (host,))
        self.db.execute("DELETE FROM host_holds WHERE host = %s", (host,))
        if not tenant:
            return
        api = storage.of(self.db).tenancy
        api.bind(host, ALICE, unix_user="ameesh-alice", uid=2001,
                 runner_id="%s/alice" % host, actor=ALICE)
        api.bind(host, BRUNO, unix_user="ameesh-bruno", uid=2002,
                 runner_id="%s/bruno" % host, actor=ALICE)
        cfg = dataclasses.replace(
            self.cfg, host=host, tenant=ALICE, runner_id="%s/alice" % host,
            accounts={"codex": [{"name": "org", "type": "config_dir",
                                 "path": self.tmp + "/codex-org"}]})
        self.assertTrue(tenancy.beat(cfg, self.db, version="1.52.0"))
        if profiles is not None:
            self.db.execute(
                "UPDATE runner_presence SET profiles = %s WHERE host = %s"
                " AND tenant = %s", (profiles, host, ALICE))

    def attempt(self, move_id: str) -> dict:
        return storage.of(self.db).conversation_moves.get(move_id)

    def moved_events(self) -> int:
        return self.db.query(
            "SELECT count(*)::int AS n FROM ameesh_events"
            " WHERE type = 'agent.moved' AND object = %s",
            ("agent:" + AVATAR,))[0]["n"]

    def assert_no_effect(self, before: dict, open_move: str | None) -> None:
        self.assertEqual(self.row(AVATAR), before)
        self.assertEqual(
            (storage.of(self.db).conversation_moves.open_for(AVATAR) or {}).get("move_id"),
            open_move)

    def refused(self, pattern: str, target: str = BANC) -> None:
        before = self.row(AVATAR)
        open_move = (storage.of(self.db).conversation_moves.open_for(AVATAR) or {}).get(
            "move_id")
        events = self.moved_events()
        with self.assertRaisesRegex(relocation.MoveError, pattern):
            relocation.move_manual(self.cfg, self.db, AVATAR, target,
                                   actor=ALICE)
        self.assert_no_effect(before, open_move)
        self.assertEqual(self.moved_events(), events)


class MoveBothWaysTest(_AvatarCase):
    def test_shared_to_dedicated_and_back(self):
        self.source()
        self.ready(BANC, tenant=True)
        self.ready(ATELIER, tenant=False)
        result = relocation.move_manual(
            self.cfg, self.db, AVATAR, BANC, actor=ALICE)
        row = self.row(AVATAR)
        self.assertEqual((row["host"], row["lease_owner"]), (BANC, None))
        self.assertEqual(row["lease_epoch"], 4)
        self.assertEqual(row["session_id"], "sess-source")  # provisional
        self.assertEqual(row["cwd"], "/home/ameesh-alice/acme/acme-web")
        move = self.attempt(result["conversation_move"])
        self.assertEqual((move["state"], move["reason"]), ("switched", "manual"))
        self.assertEqual((move["source_host"], move["target_host"]), (ATELIER, BANC))
        self.assertEqual(move["target_cwd"], "/home/ameesh-alice/acme/acme-web")
        self.assertEqual(move["source_session"], "sess-source")
        self.assertIsNone(move["target_session"])          # not verified yet
        event = self.db.query(
            "SELECT data FROM ameesh_events WHERE id = %s", (result["event"],))[0]
        self.assertEqual(event["data"]["conversation_move"], move["move_id"])
        self.assertEqual(event["data"]["reason"], "manual")

        # the target can claim; the source waits its holdoff (never two holders)
        now = time.monotonic()
        free_since = now - 60.0  # past the rank delay of the cloud host
        allowed, _reason, _ = election.claim_wait(self.db, row, BANC, free_since,
                                                  now=now)
        self.assertTrue(allowed)
        allowed, reason, _ = election.claim_wait(self.db, row, ATELIER, free_since,
                                                 now=now)
        self.assertFalse(allowed)
        self.assertIn("holdoff", reason)
        self.assertIsNotNone(registry.claim(self.db, AVATAR, "banc/alice", 120))

        # close the attempt (the target's first turn would verifiy it) and go back
        self.db.execute(
            "UPDATE conversation_moves SET state = 'verified', target_session = 'tgt',"
            " target_owner = 'banc/alice', target_epoch = 4, digest = %s"
            " WHERE move_id = %s", ("a" * 64, move["move_id"]))
        self.ready(ATELIER, tenant=False)
        before_epoch = int(self.row(AVATAR)["lease_epoch"])
        result = relocation.move_manual(
            self.cfg, self.db, AVATAR, ATELIER, actor=ALICE)
        row = self.row(AVATAR)
        self.assertEqual((row["host"], row["lease_owner"]), (ATELIER, None))
        self.assertEqual(row["lease_epoch"], before_epoch + 1)
        back = self.attempt(result["conversation_move"])
        self.assertEqual((back["source_host"], back["target_host"]), (BANC, ATELIER))
        self.assertEqual(back["target_cwd"], self.row(AVATAR)["cwd"])
        self.assertTrue(back["target_cwd"].endswith("/acme/acme-web"))

    def test_no_session_or_credential_file_is_copied(self):
        self.source()
        self.ready(BANC)
        source_dir = self.tmp + "/source-state"
        import os
        os.makedirs(source_dir, exist_ok=True)
        session_file = source_dir + "/native-session.jsonl"
        credential_file = source_dir + "/credentials.json"
        with open(session_file, "w", encoding="utf-8") as handle:
            handle.write("native transcript stays here\n")
        with open(credential_file, "w", encoding="utf-8") as handle:
            handle.write('{"secret": "stays here"}\n')
        with mock.patch("shutil.copyfile") as copyfile, \
                mock.patch("shutil.copy2") as copy2, \
                mock.patch("shutil.copytree") as copytree, \
                mock.patch.object(relocation, "unit_stopped",
                                  side_effect=AssertionError("no host probe")):
            result = relocation.move_manual(
                self.cfg, self.db, AVATAR, BANC, actor=ALICE)
        copyfile.assert_not_called()
        copy2.assert_not_called()
        copytree.assert_not_called()
        with open(session_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "native transcript stays here\n")
        with open(credential_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"secret": "stays here"}\n')
        move = self.attempt(result["conversation_move"])
        self.assertIsNone(move["target_session"])
        self.assertFalse(os.path.exists(source_dir + "/../target-state"))


class PreflightTest(_AvatarCase):
    def setUp(self) -> None:
        super().setUp()
        self.source()
        self.ready(BANC)

    def test_presence_missing_refused_before_effect(self):
        self.db.execute("DELETE FROM role_standings WHERE name = %s AND host = %s",
                        (AVATAR, BANC))
        self.refused("move-presence")

    def test_tenant_not_bound_refused_before_effect(self):
        self.db.execute("DELETE FROM runner_presence WHERE host = %s", (BANC,))
        self.db.execute("DELETE FROM host_tenants WHERE host = %s", (BANC,))
        self.refused("move-tenant-missing")

    def test_account_profile_missing_refused_before_effect(self):
        self.db.execute(
            "UPDATE runner_presence SET profiles = '{}' WHERE host = %s AND tenant = %s",
            (BANC, ALICE))
        self.refused("move-account-missing")

    def test_account_profile_lost_between_preflight_and_lock_is_refused(self):
        # B #40410: the tenant runner's next heartbeat publishes no profile
        # after the caller's preflight and before the write's locked recheck.
        # The fresh row must be revalidated under the lock, before any effect.
        before = self.row(AVATAR)
        events = self.moved_events()
        original = conversation_move.manual

        def lose_profile(db, agent, target_host, target_cwd, **kwargs):
            db.execute(
                "UPDATE runner_presence SET profiles = '{}'"
                " WHERE host = %s AND tenant = %s", (BANC, ALICE))
            return original(db, agent, target_host, target_cwd, **kwargs)

        with mock.patch.object(conversation_move, "manual",
                               side_effect=lose_profile):
            with self.assertRaisesRegex(relocation.MoveError,
                                        "move-account-missing"):
                relocation.move_manual(self.cfg, self.db, AVATAR, BANC,
                                       actor=ALICE)
        self.assertEqual(self.row(AVATAR), before)
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for(AVATAR))
        self.assertEqual(self.moved_events(), events)

    def test_harness_missing_refused_before_effect(self):
        self.db.execute(
            "UPDATE agent_candidates SET placement_ok = false,"
            " placement_diagnostic = 'harness codex not allowed by host banc'"
            " WHERE name = %s AND host = %s", (AVATAR, BANC))
        self.refused("move-harness-missing")

    def test_turn_in_progress_refused_before_effect(self):
        lease = registry.claim(self.db, AVATAR, "runner-source", 600,
                               host=ATELIER)
        self.assertIsNotNone(lease)
        self.assertTrue(registry.begin_turn(self.db, AVATAR, "runner-source",
                                            lease["lease_epoch"], "tour"))
        self.refused("turn in progress")

    def test_open_handoff_refused_before_effect(self):
        self.db.execute(
            "INSERT INTO host_handoffs (id, agent, source, target, state, plan,"
            " memory_bytes, disk_bytes, reserved_until)"
            " VALUES ('h-7108', %s, %s, %s, 'planned', '{}'::jsonb, 1, 1,"
            " clock_timestamp() + interval '1 hour')", (AVATAR, ATELIER, BANC))
        self.refused("open host handoff")

    def test_host_hold_refused_before_effect(self):
        storage.of(self.db).hosts.hold(
            BANC, reason="test", author=ALICE, seconds=600)
        self.refused("on hold")

    def test_open_conversation_move_refused_before_effect(self):
        self.assertIsNotNone(conversation_move.plan(
            self.db, AVATAR, BANC, requested_by="runner-source"))
        self.refused("move-conversation-open")


class RecoveryAndCliTest(_AvatarCase):
    """The write's own guards: keep-session, dead source, host race, CLI."""

    def test_keep_session_refused_before_effect(self):
        self.source()
        self.ready(BANC)
        before = self.row(AVATAR)
        events = self.moved_events()
        with self.assertRaisesRegex(relocation.MoveError,
                                    "--keep-session is refused"):
            relocation.move_manual(self.cfg, self.db, AVATAR, BANC,
                                   keep_session=True, actor=ALICE)
        self.assertEqual(self.row(AVATAR), before)
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for(AVATAR))
        self.assertEqual(self.moved_events(), events)

    def test_dead_source_move_recovers_and_replays_instruction(self):
        # a mark left by a host that went away: no live lease, status running,
        # one instruction in flight. The move is allowed and replays it once.
        self.ready(BANC)
        self.db.execute(
            "UPDATE agent_registry SET session_id = 'sess-source',"
            " lease_owner = 'runner-source', lease_epoch = 3,"
            " lease_expires_at = clock_timestamp() - interval '1 minute',"
            " status = 'running', status_text = 'tour',"
            " current_prompt = 'traite le lot 42' WHERE name = %s", (AVATAR,))
        result = relocation.move_manual(
            self.cfg, self.db, AVATAR, BANC, actor=ALICE)
        row = self.row(AVATAR)
        self.assertEqual((row["host"], row["status"], row["lease_owner"]),
                         (BANC, "idle", None))
        self.assertEqual(row["lease_epoch"], 4)
        self.assertIn("interrupted", row["current_prompt"])
        self.assertIn("traite le lot 42", row["current_prompt"])
        self.assertIn("host %s was interrupted" % ATELIER, row["current_prompt"])
        move = self.attempt(result["conversation_move"])
        self.assertEqual((move["state"], move["reason"]), ("recovered", "manual"))
        self.assertEqual(result["move_state"], "recovered")
        self.assertEqual(move["source_host"], ATELIER)
        self.assertEqual(move["source_session"], "sess-source")
        self.assertIsNone(move["target_session"])

    def test_source_host_race_leaves_the_row_and_no_attempt(self):
        # the caller's preflight saw `atelier`; the write is told another
        # source: the registry row must not be touched and no attempt opened.
        self.source()
        self.ready(BANC)
        before = self.row(AVATAR)
        with self.db.transaction() as tx:
            moved = storage.of(tx).conversation_moves.manual(
                str(uuid.uuid4()), AVATAR, BANC, "/target/cwd", actor=ALICE,
                source_host=BANC)
        self.assertIsNone(moved)
        self.assertEqual(self.row(AVATAR), before)
        self.assertIsNone(storage.of(self.db).conversation_moves.open_for(AVATAR))

    def test_cli_json_reports_the_conversation_move(self):
        self.source()
        self.ready(BANC)
        env = self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                       AMEESH_HOST=ATELIER)
        proc = self.mesh("agent", "move", AVATAR, "--to", BANC,
                         "--actor", ALICE, "--json", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual((payload["from"], payload["to"]), (ATELIER, BANC))
        self.assertEqual(payload["move_state"], "switched")
        self.assertEqual(payload["revoked_lease_epoch"], 3)
        move = self.attempt(payload["conversation_move"])
        self.assertEqual((move["state"], move["reason"]), ("switched", "manual"))
        self.assertEqual(self.row(AVATAR)["host"], BANC)


if __name__ == "__main__":
    unittest.main()

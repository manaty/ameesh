# SPDX-License-Identifier: AGPL-3.0-only
"""#8350: a turn's MCP source follows the agent's declared directory.

Reported from avatar-smichea (manaty/ameesh#6592): after the session `cd`'d
into `state/`, the turns resolved `.mcp.json` against that derived cwd, the
declared server was reported missing, and five such turns stopped the avatar.
The MCP source is a property of the agent's project: it must be read from the
declared directory (`ameesh show`: `directory`), never from a cwd inherited
from the session. A missing server is a configuration fault, not a harness
crash, so it never reaches the L48 stop.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import avatar_tools as policy, canon, mail, registry, runner, spawn

from .support import PgTestCase
from .test_7630_avatar_tools import avatar, mesh


class DeclaredMcpSourceTest(unittest.TestCase):
    """The declared directory wins over the turn cwd; the depot is explicit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "avatar")
        self.state = os.path.join(self.root, "state")
        self.out = os.path.join(self.tmp.name, "out")
        for path in (self.root, self.state, self.out):
            os.makedirs(path)
        self.write_source(self.root, {"declared": {"command": "true"}})
        self.write_source(self.state, {"drifted": {"command": "true"}})
        self.card = avatar(dict(tools=["Read", "mcp:declared"]))
        self.mesh = mesh(self.card)
        patch = mock.patch.object(canon, "load_configured",
                                  side_effect=lambda cfg: [self.mesh])
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def write_source(directory, servers):
        with open(os.path.join(directory, ".mcp.json"), "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": servers}, handle)

    def prepare(self, agent, cwd=None, mcp_root=None):
        return policy.prepare(None, agent, self.out, cwd or self.state,
                              mcp_root=mcp_root)

    def written(self):
        with open(os.path.join(self.out, spawn.MCP_FILE), encoding="utf-8") as handle:
            return json.load(handle)["mcpServers"]

    def agent(self, **raw):
        return dict(name="avatar", canon_governed=True, harness="claude", **raw)

    def test_the_declared_directory_wins_over_the_session_cwd(self):
        # The session `cd`ed into state/: the turn cwd is the derived one.
        self.prepare(self.agent(cwd=self.root))
        self.assertEqual(self.written(), {"declared": {"command": "true"}})

    def test_without_a_declared_directory_the_turn_cwd_is_the_fallback(self):
        # Older rows and the mediated depot have no declared cwd on this host.
        self.card.fiche.data["tools"] = ["Read", "mcp:drifted"]
        self.prepare(self.agent())
        self.assertEqual(self.written(), {"drifted": {"command": "true"}})

    def test_the_mediated_depot_is_an_explicit_exception(self):
        self.card.fiche.data["tools"] = ["Read", "mcp:drifted"]
        self.prepare(self.agent(cwd=self.root), mcp_root=self.state)
        self.assertEqual(self.written(), {"drifted": {"command": "true"}})

    def test_missing_server_names_the_expected_directory(self):
        self.card.fiche.data["tools"] = ["Read", "mcp:absent"]
        with self.assertRaises(spawn.SpawnToolsError) as ctx:
            self.prepare(self.agent(cwd=self.root))
        self.assertEqual(ctx.exception.code, "spawn-mcp-not-found")
        message = str(ctx.exception)
        self.assertIn(os.path.join(self.root, ".mcp.json"), message)
        self.assertIn("MCP source directory: %s" % self.root, message)


class MissingMcpIsAConfigurationFaultTest(PgTestCase):
    """A missing server is a configuration fault, never five fast failures."""

    def setUp(self):
        super().setUp()
        self.register("avatar8350", "claude", cwd=self.tmp)
        self.db.execute(
            "UPDATE agent_registry SET canon_ref = 'home:agents/avatar.md',"
            " responsible = 'human:alice', placement_ok = true,"
            " placement_profile = ameesh_placement_profile("
            "host, harness, provider, model, credential_mode)"
            " WHERE name = 'avatar8350'")
        self.db.execute(
            "INSERT INTO canon_state (host, status) VALUES (%s, 'ok')"
            " ON CONFLICT (host, canon) DO UPDATE SET status = 'ok'",
            (self.cfg.host,))
        self.card = avatar(dict(tools=["Read", "mcp:memoria"]))
        patch = mock.patch.object(canon, "load_configured",
                                  return_value=[mesh(self.card)])
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": os.path.join(os.path.dirname(__file__), "fakebin"),
            "AMEESH_TEST_LOG": self.turns_log,
        })
        env.start()
        self.addCleanup(env.stop)

    def test_five_missing_mcp_turns_do_not_stop_the_agent(self):
        # The declared root exists but has no .mcp.json: the card names a
        # server that cannot be resolved, exactly the reported incident.
        owner = runner.Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "avatar8350", owner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = runner.AgentWorker(owner, registry.get(self.db, "avatar8350"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        os.makedirs(worker.state_dir, exist_ok=True)
        self.register("sender8350", "claude", cwd=self.tmp)
        mail.send(self.db, "sender8350", "avatar8350", "hello", kind="request")
        try:
            for _ in range(5):
                self.assertFalse(worker.run_turn(worker.pick()))
                # L48 counts only fast failures; this one is classified apart.
                self.assertFalse(worker.fast_failure)
            self.assertEqual(worker.fast_failures, 0)
            row = registry.get(self.db, "avatar8350")
            self.assertEqual(row["status"], "blocked")
            self.assertTrue(row["status_text"].startswith("configuration fault: "))
            # The detailed message names the directory the source was
            # expected in; it lives in the error column, as for any block.
            self.assertIn("MCP source directory: %s" % self.tmp, row["last_error"])
            self.assertIn("not defined in %s" % os.path.join(self.tmp, ".mcp.json"),
                          row["last_error"])
            self.assertIn(row.get("stop_reason"), (None, ""))
        finally:
            registry.release(self.db, "avatar8350", owner.runner_id,
                             int(lease["lease_epoch"]))

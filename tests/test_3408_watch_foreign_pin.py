# SPDX-License-Identifier: AGPL-3.0-only
"""#3408: a watch or wake environment pinned to another host sends nothing."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import watch

from .support import PgTestCase


def _config(directory: str, host: str | None) -> str:
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({} if host is None else {"host": host}, fh)
    return path


class ForeignPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="ameesh-3408-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg = mock.Mock(host="ameesh-agents-2")

    def env(self, file_host, pinned=None, alias=None):
        env = {"AMEESH_CONFIG": _config(self.dir, file_host), "PATH": "/usr/bin:/bin",
               "HOME": self.dir}
        if pinned is not None:
            env["AMEESH_HOST"] = pinned
        if alias is not None:
            env["AGENT_MESH_HOST"] = alias
        return env

    def test_copy_from_another_host_is_foreign(self):
        env = self.env("ameesh-agents-1", pinned="ameesh-agents-2")
        self.assertEqual(watch.foreign_pin(self.cfg, env), "ameesh-agents-1")

    def test_alias_pin_is_read_too(self):
        env = self.env("ameesh-agents-1", alias="ameesh-agents-2")
        self.assertEqual(watch.foreign_pin(self.cfg, env), "ameesh-agents-1")

    def test_own_host_is_not_foreign(self):
        env = self.env("ameesh-agents-2", pinned="ameesh-agents-2")
        self.assertEqual(watch.foreign_pin(self.cfg, env), "")

    def test_no_pin_is_not_foreign(self):
        self.assertEqual(watch.foreign_pin(self.cfg, self.env("ameesh-agents-1")), "")

    def test_machine_name_when_the_file_has_no_host(self):
        machine = watch.config_mod.Config().host
        here = mock.Mock(host=machine)
        self.assertEqual(watch.foreign_pin(here, self.env(None, pinned=machine)), "")
        elsewhere = mock.Mock(host=machine + "-copy")
        self.assertEqual(watch.foreign_pin(elsewhere, self.env(None, pinned=machine + "-copy")),
                         machine)

    def host_env(self, text):
        os.makedirs(os.path.join(self.dir, ".config", "ameesh"), exist_ok=True)
        with open(os.path.join(self.dir, ".config", "ameesh", "env"), "w",
                  encoding="utf-8") as fh:
            fh.write(text)

    def test_host_wide_env_file_names_the_machine(self):
        # a host naming itself in ~/.config/ameesh/env only (the config has
        # another name, or none): its own watches are not refused
        self.host_env('DEEPSEEK_API_KEY=x\nAMEESH_HOST="pc-smichea"\n')
        here = mock.Mock(host="pc-smichea")
        self.assertEqual(watch.foreign_pin(here, self.env("laptop-x", pinned="pc-smichea")), "")
        # a copy pinned to another host is still caught
        self.assertEqual(watch.foreign_pin(self.cfg, self.env(None, pinned="ameesh-agents-2")),
                         "pc-smichea")

    def test_unreadable_configuration_blocks_nothing(self):
        env = {"AMEESH_CONFIG": os.path.join(self.dir, "absent.json"),
               "AMEESH_HOST": "ameesh-agents-2", "HOME": self.dir}
        self.assertEqual(watch.foreign_pin(self.cfg, env), "")


class SendAndFireRefusedTest(PgTestCase):
    def setUp(self) -> None:
        self.db.execute("DELETE FROM agent_watches")
        self.register("w3408", "claude")
        self.dir = tempfile.mkdtemp(prefix="ameesh-3408-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.here = dataclasses.replace(self.cfg, host="ameesh-agents-2")
        self.foreign = {"AMEESH_CONFIG": _config(self.dir, "ameesh-agents-1"),
                        "AMEESH_HOST": "ameesh-agents-2", "HOME": self.dir,
                        watch.UNIT_DIR_ENV: self.dir,
                        watch.IDENTITY_ENV: "w3408"}

    def test_send_refused_and_nothing_dropped(self):
        sent = []
        with self.assertRaises(watch.WatchError) as caught:
            watch.send(self.here, self.db, "w3408", env=self.foreign,
                       mail_send=lambda tx, to, body: sent.append(to))
        self.assertEqual(sent, [])
        text = str(caught.exception)
        self.assertIn("ameesh-agents-1", text)
        self.assertIn("ameesh watch enable --agent w3408", text)

    def test_fire_refused_before_reading_the_wake(self):
        with self.assertRaises(watch.WatchError) as caught:
            watch.fire(self.here, self.db, "w3408", "w1", env=self.foreign,
                       run=lambda *a, **k: None)
        self.assertIn("#3408", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

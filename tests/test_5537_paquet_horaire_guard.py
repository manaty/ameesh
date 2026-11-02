# SPDX-License-Identifier: AGPL-3.0-only
"""#5537: the hourly packet tick needs a resolved project on every candidate.

`ameesh-paquet-horaire.timer` is installed on each candidate of the
`mesh-design` lease. When the lease moves, the tick reads the computed list
from the new holder's configuration: without `radar.projects.<p>` nor
`packet_watch.projects.<p>.repo`, no project resolves there and the takeover
would compose without a list. `ameesh doctor` warns, without changing its
verdict — exactly the #3364 shape for the packet-watch timer.
"""
from __future__ import annotations

import tempfile
import types
import unittest

from ameesh import hostcheck


def cfg_with(*, radar=None, packet_watch=None, host="ameesh-agents-1"):
    return types.SimpleNamespace(host=host, radar=radar, packet_watch=packet_watch)


def systemctl(state):
    def run(*args):
        assert args[:1] == ("show",), args
        assert args[1] == hostcheck.PAQUET_HORAIRE_TIMER, args
        return None if state is None else state + "\n"
    return run


class PaquetHoraireGuardTest(unittest.TestCase):
    def test_active_timer_without_a_project_warns_and_names_the_gesture(self):
        lines = hostcheck.paquet_horaire_lines(cfg_with(), run=systemctl("active"))
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith("warning    :"), lines)
        self.assertIn(hostcheck.PAQUET_HORAIRE_TIMER, text)
        self.assertIn("no project resolves", text)
        self.assertIn("radar.projects", text)
        self.assertIn("packet_watch.projects.<p>.repo", text)
        self.assertIn('"role": "mesh-design"', text)

    def test_inactive_or_unreadable_timer_is_silent(self):
        for state in ("inactive", "failed", "unknown", None, ""):
            with self.subTest(state=state):
                self.assertEqual(
                    hostcheck.paquet_horaire_lines(cfg_with(), run=systemctl(state)), [])

    def test_a_radar_project_resolves_the_host(self):
        radar = {"projects": {"ameesh": {"repo": "/srv/ameesh", "target": "main"}}}
        self.assertEqual(
            hostcheck.paquet_horaire_lines(cfg_with(radar=radar),
                                           run=systemctl("active")), [])

    def test_a_packet_watch_repo_resolves_the_host_without_radar(self):
        with tempfile.TemporaryDirectory(prefix="5537-") as tmp:
            for block in (
                    {"role": "mesh-design",
                     "projects": {"ameesh": {"notify": ["composeur"], "repo": tmp}}},
                    {"projects": {"ameesh": {"notify": ["composeur"], "repo": tmp}}},
            ):
                with self.subTest(block=block):
                    self.assertEqual(
                        hostcheck.paquet_horaire_lines(cfg_with(packet_watch=block),
                                                       run=systemctl("active")), [])

    def test_a_declared_watch_without_repo_names_the_project(self):
        block = {"role": "mesh-design",
                 "projects": {"ameesh": {"notify": ["composeur", "pilote"]}}}
        lines = hostcheck.paquet_horaire_lines(cfg_with(packet_watch=block),
                                               run=systemctl("active"))
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("packet_watch.projects.ameesh without `repo`", lines[0])
        self.assertIn("missing from radar.projects", lines[0])

    def test_a_watch_resolved_by_radar_needs_no_repo(self):
        radar = {"projects": {"ameesh": {"repo": "/srv/ameesh", "target": "main"}}}
        block = {"role": "mesh-design",
                 "projects": {"ameesh": {"notify": ["composeur", "pilote"]}}}
        self.assertEqual(
            hostcheck.paquet_horaire_lines(cfg_with(radar=radar, packet_watch=block),
                                           run=systemctl("active")), [])

    def test_a_watch_of_another_project_is_not_hidden_by_radar(self):
        radar = {"projects": {"nexlink": {"repo": "/srv/nexlink", "target": "main"}}}
        block = {"role": "mesh-design",
                 "projects": {"ameesh": {"notify": ["composeur"]}}}
        lines = hostcheck.paquet_horaire_lines(
            cfg_with(radar=radar, packet_watch=block), run=systemctl("active"))
        self.assertIn("packet_watch.projects.ameesh", "\n".join(lines))

    def test_a_watch_pinned_to_another_host_still_warns_here(self):
        block = {"host": "ameesh-agents-2",
                 "projects": {"ameesh": {"notify": ["composeur"], "repo": "/srv/ameesh"}}}
        lines = hostcheck.paquet_horaire_lines(
            cfg_with(packet_watch=block, host="ameesh-agents-1"),
            run=systemctl("active"))
        self.assertIn("no project resolves", "\n".join(lines))

    def test_an_unreadable_configuration_is_reported_not_raised(self):
        for cfg, expected in (
                (cfg_with(radar={"projects": {"ameesh": {"repo": ""}}}),
                 "radar.projects unreadable"),
                (cfg_with(packet_watch={"projects": {"ameesh": {"notify": []}}}),
                 "packet_watch configuration unreadable"),
        ):
            with self.subTest(expected=expected):
                lines = hostcheck.paquet_horaire_lines(cfg, run=systemctl("active"))
                self.assertIn(expected, "\n".join(lines))


if __name__ == "__main__":
    unittest.main()

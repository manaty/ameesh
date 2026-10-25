# SPDX-License-Identifier: AGPL-3.0-only
"""#3416 — la lecture d'un paquet rapatrie la tête du véhicule avant son historique.

Constat h-12 (#3414, PR #2597) : la branche d'intégration poussée à 22:59Z
manquait au clone radar, et `packet_reviews._trailer_members` rendait « cannot
read the composition history » à 23:00Z ; un fetch manuel à 23:05Z a suffi.
Un paquet vert ne doit plus attendre que quelqu'un rafraîchisse le clone.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import packet_reviews, plan_git

_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, env=_ENV)
    if proc.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _commit(repo, name, text, message=None):
    full = os.path.join(repo, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message or text)
    return _git(repo, "rev-parse", "HEAD")


class VehicleHeadFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3416-fetch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.forge = os.path.join(self.tmp, "forge.git")
        subprocess.run(["git", "init", "-q", "--bare", self.forge], check=True)
        self.dev = os.path.join(self.tmp, "dev")
        subprocess.run(["git", "clone", "-q", self.forge, self.dev],
                       check=True, capture_output=True)
        _git(self.dev, "checkout", "-q", "-b", "main")
        self.base = _commit(self.dev, "README.md", "base\n")
        _git(self.dev, "push", "-q", "origin", "main")
        _git(self.forge, "symbolic-ref", "HEAD", "refs/heads/main")

    def _radar(self):
        """Le clone radar, fait AVANT la composition : il ne l'a pas."""
        path = os.path.join(self.tmp, "radar")
        subprocess.run(["git", "clone", "-q", self.forge, path],
                       check=True, capture_output=True)
        return path

    def _compose_and_push(self):
        """Une composition poussée : fusion --no-ff, trailer `ameesh-work`."""
        _git(self.dev, "checkout", "-q", "-b", "member/101", "main")
        member = _commit(self.dev, "src/part.py", "print(101)\n")
        _git(self.dev, "checkout", "-q", "-b", "integration/h-12", "main")
        _git(self.dev, "merge", "-q", "--no-ff", "member/101", "-m",
             "Merge #101 — composition\n\nameesh-work: 101")
        head = _git(self.dev, "rev-parse", "HEAD")
        _git(self.dev, "push", "-q", "origin", "integration/h-12")
        return head, member

    def test_the_frozen_head_is_fetched_then_the_composition_is_read(self):
        radar = self._radar()
        head, member = self._compose_and_push()
        self.assertIsNone(plan_git.local_commit(radar, head))  # le constat

        found = packet_reviews._trailer_members(radar, self.base, head, 999)
        self.assertEqual(found, [{"lot": 101, "source": member}])
        self.assertEqual(plan_git.local_commit(radar, head), head)

    def test_the_public_reader_also_works_on_a_late_clone(self):
        radar = self._radar()
        head, _member = self._compose_and_push()
        found = packet_reviews.members(None, radar, self.base, head, 999)
        self.assertEqual([entry["lot"] for entry in found], [101])

    def test_a_head_already_present_does_not_need_the_forge(self):
        radar = self._radar()
        head, _member = self._compose_and_push()
        _git(radar, "fetch", "-q", "origin", "integration/h-12")
        # La forge disparaît : la lecture doit rester locale, donc réussir.
        _git(radar, "remote", "set-url", "origin",
             os.path.join(self.tmp, "forge-absente.git"))
        found = packet_reviews._trailer_members(radar, self.base, head, 999)
        self.assertEqual([entry["lot"] for entry in found], [101])

    def test_an_unreachable_forge_leaves_a_named_refusal(self):
        radar = self._radar()
        _git(radar, "remote", "set-url", "origin",
             os.path.join(self.tmp, "forge-absente.git"))
        head, _member = self._compose_and_push()
        with self.assertRaisesRegex(packet_reviews.VehicleError,
                                    "composition history"):
            packet_reviews._trailer_members(radar, self.base, head, 999)

    def test_an_abbreviated_head_is_never_taken_for_a_fetch_target(self):
        radar = self._radar()
        head, _member = self._compose_and_push()
        # Le commit est absent et la ref est abrégée : aucun SHA exact à
        # demander, le doute reste nommé au lieu d'un fetch inventé.
        self.assertFalse(packet_reviews._fetch_head(radar, head[:12]))


if __name__ == "__main__":
    unittest.main()

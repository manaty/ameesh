# SPDX-License-Identifier: AGPL-3.0-only
"""#7666: weekly stable channel designation, publication and the single pin."""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import release_cutoff as rc
from ameesh import stable_channel as sc

ZONE = ZoneInfo("Europe/Paris")


class Rules:
    """The two calendar reads of the compiled mechanics the channel uses."""

    cutoffs = ("11:00", "23:00")

    def zone(self):
        return ZONE

    def cutoffs_on(self, day):
        return tuple((dt.datetime(day.year, day.month, day.day, int(label[:2]),
                                  int(label[3:]), tzinfo=ZONE).timestamp(), label)
                     for label in self.cutoffs)


def at(text: str) -> float:
    return dt.datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()


#: Monday 16/11/2026 11:00 Paris is D(2026-W47); D(2026-W48) is 23/11 11:00
W47 = at("2026-11-16T11:00")
W48 = at("2026-11-23T11:00")


def git(cwd, *args, date=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", GIT_TERMINAL_PROMPT="0")
    if date is not None:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = "@%d +0000" % int(date)
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class StableChannelTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.remote = os.path.join(root, "remote.git")
        self.repo = os.path.join(root, "repo")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        git(self.repo, "remote", "add", "origin", self.remote)
        self.commits = {}
        # v1.0.0 a week before, v1.1.0 two days before, v1.2.0 six hours
        # before D(W47): only the first two are past the 24 h soak
        for version, when in (("1.0.0", W47 - 8 * 86400), ("1.1.0", W47 - 2 * 86400),
                              ("1.2.0", W47 - 6 * 3600), ("1.3.0", W48 - 3 * 86400)):
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "release " + version,
                date=when)
            git(self.repo, "tag", "-a", "v" + version, "-m", "ameesh " + version, date=when)
            self.commits[version] = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main", "--tags")
        self.project = types.SimpleNamespace(name="ameesh", repo=self.repo, target="main",
                                             remote="origin", github="", stable_channel=True)
        self.green = set(self.commits.values())
        self.attested = set(self.commits.values())
        patches = [
            mock.patch.object(rc, "commit_is_green",
                              lambda repo, project, sha: sha in self.green),
            mock.patch.object(rc, "read_attestation",
                              lambda sha, db=None: {"sha": sha} if sha in self.attested
                              else None),
            mock.patch.object(sc, "check_first_decision", lambda db, project, d: int(d)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def designate(self, week, previous=None, **kwargs):
        return sc.designate(self.project, Rules(), week, previous=previous,
                            repository="https://example.invalid/ameesh", **kwargs)

    # -- the rule -----------------------------------------------------------

    def test_designation_instant_is_monday_first_cutoff(self):
        self.assertEqual(sc.designation_at(Rules(), "2026-W47"), W47)
        self.assertEqual(sc.week_of(W47 + 3 * 86400, Rules()), "2026-W47")
        self.assertIsNone(sc.current_week(Rules(), W47 - 1))
        self.assertEqual(sc.current_week(Rules(), W47 + 60), ("2026-W47", W47))
        with self.assertRaises(sc.StableError):
            sc.designation_at(Rules(), "2026-47")

    def test_highest_tag_past_the_soak_green_and_attested(self):
        report = self.designate("2026-W47")
        self.assertEqual(report["result"], "designated")
        self.assertEqual(report["manifest"]["tag"], "v1.1.0")
        self.assertEqual(report["manifest"]["commit"], self.commits["1.1.0"])
        self.assertEqual(report["manifest"]["tag_object"],
                         git(self.repo, "rev-parse", "refs/tags/v1.1.0"))
        self.assertIn({"tag": "v1.2.0", "reason": "tagged less than 24 h before the "
                                                  "designation"}, report["refused"])

    def test_a_stale_local_target_does_not_hide_a_remote_release(self):
        # main moved on the remote (another clone landed v1.1.5); the local
        # branch and the local tag list are behind
        other = os.path.join(self.tmp.name, "other")
        subprocess.run(["git", "clone", "-q", self.remote, other], check=True)
        git(other, "commit", "-q", "--allow-empty", "-m", "release 1.1.5",
            date=W47 - 30 * 3600)
        git(other, "tag", "-a", "v1.1.5", "-m", "ameesh 1.1.5", date=W47 - 30 * 3600)
        git(other, "push", "-q", "origin", "main", "--tags")
        commit = git(other, "rev-parse", "HEAD")
        self.green.add(commit)
        self.attested.add(commit)
        report = self.designate("2026-W47")
        self.assertEqual((report["manifest"]["tag"], report["manifest"]["commit"]),
                         ("v1.1.5", commit))

    def test_red_or_unattested_tag_is_skipped(self):
        self.green.discard(self.commits["1.1.0"])
        report = self.designate("2026-W47")
        self.assertEqual(report["manifest"]["tag"], "v1.0.0")
        self.assertIn({"tag": "v1.1.0", "reason": "CI not green"}, report["refused"])
        self.green.add(self.commits["1.1.0"])
        self.attested.discard(self.commits["1.1.0"])
        report = self.designate("2026-W47")
        self.assertEqual(report["manifest"]["tag"], "v1.0.0")
        self.assertIn({"tag": "v1.1.0", "reason": "no complete staging attestation"},
                      report["refused"])

    def test_nothing_eligible_carries_the_previous_never_backwards(self):
        previous = self.designate("2026-W47")["manifest"]
        self.green.discard(self.commits["1.2.0"])
        self.green.discard(self.commits["1.3.0"])
        report = self.designate("2026-W48", previous=previous)
        self.assertEqual(report["result"], "carried")
        self.assertEqual(report["manifest"]["tag"], "v1.1.0")
        self.assertEqual(report["manifest"]["week"], "2026-W48")
        self.assertEqual(report["manifest"]["previous"], "2026-W47")
        # with the previous stable red today, the green v1.0.0 is still never
        # chosen: the channel does not go backwards
        self.green.discard(self.commits["1.1.0"])
        report = self.designate("2026-W48", previous=previous)
        self.assertEqual((report["result"], report["manifest"]["tag"]), ("carried", "v1.1.0"))
        self.assertIn({"tag": "v1.0.0", "reason": "below the previous stable 1.1.0"},
                      report["refused"])

    def test_no_tag_and_no_previous_is_no_stable(self):
        self.green.clear()
        self.assertEqual(self.designate("2026-W47")["result"], "none")

    # -- publication and the consumer pin ------------------------------------

    def tick(self, now, decision=None):
        return sc.tick(None, None, self.project, now=now, rules=Rules(), decision=decision)

    def test_first_publication_waits_for_the_owner_then_weekly_is_automatic(self):
        self.assertEqual(self.tick(W47 - 60)["result"], "not-due")
        report = self.tick(W47 + 60)
        self.assertEqual(report["result"], "awaiting-decision")
        self.assertEqual(sc.remote_weeks(self.repo, "origin"), [])
        report = self.tick(W47 + 60, decision=42)
        self.assertEqual((report["result"], report["tag"]), ("designated", "stable/2026-W47"))
        self.assertEqual(report["manifest"]["decision"], 42)
        self.assertEqual(self.tick(W47 + 3600)["result"], "published")
        # the following week needs no decision; 1.3.0 is past its soak
        report = self.tick(W48 + 60)
        self.assertEqual((report["result"], report["manifest"]["tag"]),
                         ("designated", "v1.3.0"))
        self.assertIsNone(report["manifest"]["decision"])
        self.assertEqual(report["manifest"]["previous"], "2026-W47")
        self.assertEqual(sc.remote_weeks(self.repo, "origin"), ["2026-W47", "2026-W48"])

    def test_the_minute_tick_reads_the_remote_once_an_hour(self):
        cfg = types.SimpleNamespace(state_dir=os.path.join(self.tmp.name, "state"))
        self.assertEqual(sc.tick(cfg, None, self.project, now=W47 + 60, rules=Rules(),
                                 decision=1)["result"], "designated")
        with mock.patch.object(sc, "remote_weeks", side_effect=AssertionError("read")):
            report = sc.tick(cfg, None, self.project, now=W47 + 120, rules=Rules())
        self.assertEqual((report["result"], report.get("cached")), ("published", True))
        report = sc.tick(cfg, None, self.project, now=W47 + 60 + sc.RECHECK_S, rules=Rules())
        self.assertEqual((report["result"], report.get("cached")), ("published", None))

    def test_a_published_week_never_moves(self):
        manifest = self.designate("2026-W47")["manifest"]
        sc.publish(self.project, manifest)
        sc.publish(self.project, manifest)  # idempotent
        other = dict(manifest, tag="v1.0.0", version="1.0.0", commit=self.commits["1.0.0"],
                     tag_object=git(self.repo, "rev-parse", "refs/tags/v1.0.0"))
        with self.assertRaises(sc.StableError):
            sc.publish(self.project, other)

    def consumer(self):
        """A clone of the publisher, as an external module would hold one."""
        path = os.path.join(self.tmp.name, "consumer-clone")
        if not os.path.isdir(path):
            subprocess.run(["git", "clone", "-q", "--bare", self.remote, path], check=True)
        return path

    def test_witness_moves_from_one_stable_to_the_next_by_its_pin_alone(self):
        self.tick(W47 + 60, decision=7)
        clone = self.consumer()
        module = os.path.join(self.tmp.name, "module")
        os.makedirs(module)
        git(module, "init", "-q", "-b", "main")
        # the witness's whole ameesh identity comes from the single pin file
        with open(os.path.join(module, "build.py"), "w", encoding="utf-8") as stream:
            stream.write("import json\nPIN = json.load(open('ameesh-stable.json'))\n"
                         "print(PIN['tag'], PIN['commit'])\n")
        pin_path = os.path.join(module, sc.PIN_FILE)
        with open(pin_path, "w", encoding="utf-8") as stream:
            stream.write(sc.pin(clone, self.remote))
        git(module, "add", "-A")
        git(module, "commit", "-q", "-m", "pin 2026-W47")
        with open(pin_path, encoding="utf-8") as stream:
            first = sc.verify(stream.read(), clone, self.remote)
        self.assertEqual(first["tag"], "v1.1.0")

        self.tick(W48 + 60)
        with open(pin_path, "w", encoding="utf-8") as stream:
            stream.write(sc.pin(clone, self.remote))
        git(module, "add", "-A")
        changed = git(module, "diff", "--cached", "--name-only").split("\n")
        self.assertEqual(changed, [sc.PIN_FILE])
        with open(pin_path, encoding="utf-8") as stream:
            second = sc.verify(stream.read(), clone, self.remote, installed="1.3.0")
        self.assertEqual((second["week"], second["tag"]), ("2026-W48", "v1.3.0"))
        out = subprocess.run(["python3", "-I", "build.py"], cwd=module, check=True,
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["v1.3.0", self.commits["1.3.0"]])

    def test_verify_refuses_an_edited_pin_or_another_installation(self):
        self.tick(W47 + 60, decision=7)
        clone = self.consumer()
        text = sc.pin(clone, self.remote, "2026-W47")
        sc.verify(text, clone, self.remote)
        forged = text.replace(self.commits["1.1.0"], self.commits["1.0.0"])
        with self.assertRaises(sc.StableError):
            sc.verify(forged, clone, self.remote)
        with self.assertRaises(sc.StableError):
            sc.verify(text, clone, self.remote, installed="1.0.0")
        with self.assertRaises(sc.StableError):
            sc.pin(clone, self.remote, "2026-W40")


class FirstDecisionTest(unittest.TestCase):
    """The first public stable needs an answered, signed, current `publish`."""

    project = types.SimpleNamespace(name="ameesh", repo="/nonexistent", remote="origin",
                                    github="manaty/ameesh")
    scope = "stable-channel:ameesh@https://github.com/manaty/ameesh"

    def check(self, view, proof=(True, "")):
        from ameesh import decisions
        with mock.patch.object(decisions, "get", lambda db, i: view), \
                mock.patch.object(decisions, "require_current", lambda db, v: None), \
                mock.patch.object(decisions, "verify_proof", lambda db, v: proof):
            return sc.check_first_decision(None, self.project, 5)

    def test_refusals_and_approval(self):
        with self.assertRaises(sc.StableError):
            sc.check_first_decision(None, self.project, None)
        with self.assertRaises(sc.StableError):
            self.check(None)
        approved = {"state": "answered", "project": "ameesh", "answer": {"option": "publish"},
                    "options": [{"key": "publish", "label": "Publier " + self.scope},
                                {"key": "hold", "label": "Attendre"}]}
        other = [{"key": "publish", "label": "Publier la release v1.59.0 sur le site"}]
        for view in (dict(approved, state="pending"), dict(approved, answer={"option": "hold"}),
                     dict(approved, project="nexlink"), dict(approved, project=None),
                     # a valid signed publish for another operation
                     dict(approved, options=other),
                     dict(approved, options=[{"key": "publish", "label":
                                              "Publier stable-channel:nexlink@x"}])):
            with self.assertRaises(sc.StableError):
                self.check(view)
        with self.assertRaises(sc.StableError):
            self.check(approved, proof=(False, "unsigned answer"))
        self.assertEqual(self.check(approved), 5)


class ConfigTest(unittest.TestCase):

    def test_stable_channel_key(self):
        cfg = types.SimpleNamespace(humans="smichea", release={}, host="h", radar={})
        with mock.patch.object(rc.radar, "settings_of",
                               lambda cfg: types.SimpleNamespace(projects={})):
            project = rc.ProjectRelease.from_config(
                cfg, "ameesh", {"repo": "/tmp/x", "stable_channel": True})
            self.assertTrue(project.stable_channel)
            self.assertFalse(rc.ProjectRelease.from_config(
                cfg, "ameesh", {"repo": "/tmp/x"}).stable_channel)
            with self.assertRaises(rc.ReleaseError):
                rc.ProjectRelease.from_config(cfg, "ameesh",
                                              {"repo": "/tmp/x", "stable_channel": "yes"})


if __name__ == "__main__":
    unittest.main()

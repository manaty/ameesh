# SPDX-License-Identifier: AGPL-3.0-only
"""#3098 : les épingles du contrat CI ameesh sont lues au commit de base.

Le module installé est un instantané de release : quand un paquet change une
source du contrat et atterrit par la voie « déblocage CI », ses épingles sont
ré-écrites dans ``src/ameesh/train_contracts.py`` au même atterrissage, mais
le module installé garde les anciennes jusqu'au déploiement suivant. Chaque
coupure échouait alors pendant jusqu'à 12 h. Les témoins ci-dessous portent les
trois cas du lot : épingle de base = blob de tête → coupe ; source modifiée
dans le paquet → refus explicite ; ancienne version installée, base ré-épinglée
→ coupe.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from ameesh import train, train_ci

SOURCES = (
    ".github/workflows/paquet.yml",
    ".github/workflows/tests.yml",
    "scripts/test-plan.py",
    "scripts/test-parts.py",
    "scripts/ci-flow.py",
    "scripts/map.py",
)
CONTRACT = "src/ameesh/train_contracts.py"
#: #3098 relecture cx2 : une source que la base épingle et que la version
#: installée ne connaît pas encore (paquet atterri par « déblocage CI »).
EXTRA = "scripts/new-required-test.py"
STALE = "0" * 40


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def contract_text(pins, omit=()):
    lines = ["def ameesh():", "    return (Workflow(",
             '        path=".github/workflows/paquet.yml",',
             '        jobs=("plan", "tests"),',
             '        event="push",',
             '        called=".github/workflows/tests.yml",',
             "        sources=("]
    for path, blob in pins.items():
        if path in omit:
            continue
        lines.append('            (%r, %r),' % (path, blob))
    lines += ["        ),", '        pins_at="%s",' % CONTRACT, "    ),)"]
    return "\n".join(lines) + "\n"


class PinsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "test")
        os.makedirs(os.path.join(self.repo, ".github/workflows"))
        os.makedirs(os.path.join(self.repo, "scripts"))
        os.makedirs(os.path.join(self.repo, "src/ameesh"))
        for name in SOURCES:
            self.write(name, name + "\n")
        self.pins = {name: self.blob(name) for name in SOURCES}
        self.write(CONTRACT, contract_text(self.pins))
        self.base = self.commit()
        # The installed module of the previous release: same paths and jobs,
        # stale blobs. Only the base pins may decide the packet head.
        self.installed = train_ci.Workflow(
            ".github/workflows/paquet.yml", ("plan", "tests"), "push",
            called=".github/workflows/tests.yml",
            sources=tuple((path, STALE) for path in SOURCES),
            pins_at=CONTRACT)
        self.plan = {"base": self.base, "head": self.base, "tree": ""}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        target = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    def blob(self, name):
        return git(self.repo, "hash-object", name)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fixture")
        return git(self.repo, "rev-parse", "HEAD")

    def repin(self, name, text):
        """Land a changed source and its re-pinned contract in one commit."""
        self.write(name, text)
        self.pins[name] = self.blob(name)
        self.write(CONTRACT, contract_text(self.pins))
        return self.commit()

    def base_with_extra_pin(self, name=EXTRA, text="new required test\n"):
        """A base contract that adds a source to the installed pin set.

        A packet landed through the CI-unblock lane may add a pin the release
        module does not know yet; the base commit is still the reference.
        """
        self.write(name, text)
        self.pins[name] = self.blob(name)
        self.write(CONTRACT, contract_text(self.pins))
        return self.commit()


class VersionedSourcesTest(PinsFixture):
    def test_a_base_pin_equal_to_the_head_blob_is_a_cut(self):
        train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_source_changed_by_the_packet_is_refused_explicitly(self):
        self.write(".github/workflows/tests.yml", "changed\n")
        self.plan["head"] = self.commit()
        with self.assertRaisesRegex(train.TrainError,
                                    "source differs from audited CI contract"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_the_packet_cannot_re_pin_its_own_changed_source(self):
        # The head re-pins the new blob, but the reference is the base commit:
        # a packet never audits itself, it lands through the CI-unblock lane.
        self.write(".github/workflows/tests.yml", "changed\n")
        self.pins[".github/workflows/tests.yml"] = self.blob(
            ".github/workflows/tests.yml")
        self.write(CONTRACT, contract_text(self.pins))
        self.plan["head"] = self.commit()
        with self.assertRaisesRegex(train.TrainError,
                                    "source differs from audited CI contract"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_an_old_installed_version_cuts_on_a_re_pinned_base(self):
        base = self.repin(".github/workflows/tests.yml", "landed through unblock\n")
        self.plan.update(base=base, head=base)
        train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_source_first_pinned_by_the_base_cuts_when_unchanged(self):
        # The installed release does not know this pin yet, but the base
        # contract does: refusing the base outright would block the cut.
        base = self.base_with_extra_pin()
        self.plan.update(base=base, head=base)
        train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_source_first_pinned_by_the_base_cannot_move_at_the_head(self):
        # #3098 relecture cx2 : `own` ne venait que du module installé ; une
        # source ajoutée par la base échappait donc au contrôle de la tête.
        base = self.base_with_extra_pin()
        self.write(EXTRA, "changed by the packet\n")
        self.plan.update(base=base, head=self.commit())
        with self.assertRaisesRegex(train.TrainError,
                                    "source differs from audited CI contract"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_base_contract_that_forgets_a_source_is_refused(self):
        self.write(CONTRACT, contract_text(self.pins, omit=("scripts/map.py",)))
        self.plan["base"] = self.commit()
        with self.assertRaisesRegex(train.TrainError, "no longer pins"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_base_contract_pin_disagreeing_with_its_tree_is_refused(self):
        self.write(CONTRACT, contract_text(dict(self.pins, **{
            "scripts/map.py": "f" * 40})))
        self.plan["base"] = self.commit()
        with self.assertRaisesRegex(train.TrainError, "differently from its tree"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_base_without_the_pin_file_keeps_the_installed_snapshot(self):
        os.remove(os.path.join(self.repo, CONTRACT))
        self.plan["base"] = self.commit()
        self.installed = train_ci.Workflow(
            ".github/workflows/paquet.yml", ("plan", "tests"), "push",
            called=".github/workflows/tests.yml",
            sources=tuple((path, self.pins[path]) for path in SOURCES),
            pins_at=CONTRACT)
        train_ci.verify_local(self.repo, self.plan, (self.installed,))

    def test_a_missing_base_commit_is_refused_not_silently_fallen_back(self):
        # A base absent from the clone is not a pre-#3098 bootstrap: falling
        # back to the installed snapshot there would skip the base pins.
        self.plan["base"] = "f" * 40
        with self.assertRaisesRegex(train.TrainError, "base commit cannot be read"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))


class VersionedGitHubTest(PinsFixture):
    def gh(self, blobs):
        calls = []

        def run(args):
            calls.append(args)
            path = args[-1].split("/contents/", 1)[1].split("?", 1)[0]
            payload = dict(type="file", sha=blobs[path])
            return type("Proc", (), {"returncode": 0, "stdout": json.dumps([payload]),
                                     "stderr": ""})()
        return run, calls

    def test_remote_check_reads_the_base_pins_not_the_installed_snapshot(self):
        blobs = {path: git(self.repo, "rev-parse", "HEAD:" + path) for path in SOURCES}
        gh, calls = self.gh(blobs)
        train_ci.GitHub("o/r", (self.installed,), gh=gh, repo=self.repo).verify(self.plan)
        self.assertEqual(len(calls), len(SOURCES))

    def test_remote_check_refuses_a_head_outside_the_base_pins(self):
        blobs = {path: STALE for path in SOURCES}
        gh, _ = self.gh(blobs)
        with self.assertRaisesRegex(train.TrainError, "source differs"):
            train_ci.GitHub("o/r", (self.installed,), gh=gh,
                            repo=self.repo).verify(self.plan)

    def test_remote_check_also_covers_a_source_first_pinned_by_the_base(self):
        base = self.base_with_extra_pin()
        self.plan.update(base=base, head=base)
        blobs = {path: git(self.repo, "rev-parse", "HEAD:" + path)
                 for path in (*SOURCES, EXTRA)}
        gh, calls = self.gh(blobs)
        train_ci.GitHub("o/r", (self.installed,), gh=gh,
                        repo=self.repo).verify(self.plan)
        asked = [call[-1].split("/contents/", 1)[1].split("?", 1)[0] for call in calls]
        self.assertEqual(sorted(asked), sorted((*SOURCES, EXTRA)))

    def test_remote_check_refuses_a_head_moving_a_base_only_pin(self):
        base = self.base_with_extra_pin()
        self.plan.update(base=base, head=base)
        blobs = {path: git(self.repo, "rev-parse", "HEAD:" + path)
                 for path in (*SOURCES, EXTRA)}
        blobs[EXTRA] = STALE
        gh, _ = self.gh(blobs)
        with self.assertRaisesRegex(train.TrainError, "source differs"):
            train_ci.GitHub("o/r", (self.installed,), gh=gh,
                            repo=self.repo).verify(self.plan)


class ContractIdentityTest(unittest.TestCase):
    def test_the_pin_location_is_not_part_of_the_audited_digest(self):
        base = train_ci.Workflow(
            ".github/workflows/tests.yml", ("suite",),
            sources=((".github/workflows/tests.yml", "d" * 40),))
        pinned = train_ci.Workflow(
            ".github/workflows/tests.yml", ("suite",),
            sources=((".github/workflows/tests.yml", "d" * 40),),
            pins_at="src/ameesh/train_contracts.py")
        self.assertEqual(train_ci.contract_digest((base,)),
                         train_ci.contract_digest((pinned,)))


if __name__ == "__main__":
    unittest.main()

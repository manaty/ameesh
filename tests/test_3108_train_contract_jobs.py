# SPDX-License-Identifier: AGPL-3.0-only
"""#3108 : les jobs du contrat CI sont aussi lus au commit de base.

#3098 lit les blobs épinglés au commit de base, mais la liste des jobs requis
restait celle du module installé : un ``tests.yml`` ré-épinglé qui ajoutait un
job ne l'exigeait qu'au déploiement suivant. Les témoins ci-dessous portent
l'union « jobs de la base + jobs installés » : un job ajouté par la base est
exigé immédiatement, un job retiré reste exigé (refus fermé), une tête qui
s'ajoute un job ne s'auto-audite pas, et une expression de jobs non statique
est refusée nommément, jamais ignorée.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from ameesh import train, train_ci, train_contracts

SOURCES = (
    ".github/workflows/paquet.yml",
    ".github/workflows/tests.yml",
    "scripts/test-plan.py",
)
CONTRACT = "src/ameesh/train_contracts.py"
STALE = "0" * 40


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def contract_text(pins, jobs):
    """A contract module declaring ``jobs`` (a Python expression) and ``pins``."""
    lines = ["def ameesh():", "    return (Workflow(",
             '        path=".github/workflows/paquet.yml",',
             "        jobs=%s," % jobs,
             '        event="push",',
             "        sources=("]
    for path, blob in pins.items():
        lines.append("            (%r, %r)," % (path, blob))
    lines += ["        ),", '        pins_at="%s",' % CONTRACT, "    ),)"]
    return "\n".join(lines) + "\n"


class JobsFixture(unittest.TestCase):
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
        self.installed_jobs = ("plan", "tests")
        self.write(CONTRACT, contract_text(self.pins, repr(self.installed_jobs)))
        self.base = self.commit()
        # The installed module of the previous release: same paths, stale
        # blobs and the old job list. Only the base may widen the job set.
        self.installed = train_ci.Workflow(
            ".github/workflows/paquet.yml", self.installed_jobs, "push",
            sources=tuple((path, STALE) for path in SOURCES),
            pins_at=CONTRACT)
        self.plan = {"base": self.base, "head": self.base, "tree": "",
                     "branch": "integration/train-test"}

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

    def land(self, jobs, head_only=False):
        """Declare ``jobs`` (a Python expression) in a new base commit.

        With ``head_only`` the new contract is committed only at the head: the
        base stays the reference (#3098).
        """
        self.write(CONTRACT, contract_text(self.pins, jobs))
        if head_only:
            self.plan["head"] = self.commit()
        else:
            self.plan["base"] = self.commit()
            self.plan["head"] = self.plan["base"]

    def read(self, names, run_conclusion="success", failed=()):
        """A static packet run at the plan head with exactly ``names`` as jobs."""
        failed = set(failed)
        jobs = [dict(id=index, name=name, status="completed",
                     conclusion="failure" if name in failed else "success")
                for index, name in enumerate(names, 1)]
        head = self.plan["head"]
        blobs = {path: git(self.repo, "rev-parse", "%s:%s" % (head, path))
                 for path in SOURCES}
        run = dict(id=9, run_attempt=1, status="completed",
                   conclusion=run_conclusion, head_sha=head,
                   head_branch=self.plan["branch"],
                   path=".github/workflows/paquet.yml", event="push")

        def gh(args):
            path = args[-1]
            if "/contents/" in path:
                source = path.split("/contents/", 1)[1].split("?", 1)[0]
                payload = dict(type="file", sha=blobs[source])
            elif "/jobs?" in path:
                payload = dict(jobs=jobs)
            else:
                payload = dict(workflow_runs=[run])
            return type("Proc", (), {"returncode": 0, "stdout": json.dumps([payload]),
                                     "stderr": ""})()
        return train_ci.GitHub("o/r", (self.installed,), gh=gh, repo=self.repo)

    def assert_required(self, names):
        with self.assertRaisesRegex(train.TrainError, "required full-suite job missing"):
            self.read(names).read(self.plan)


class VersionedJobsTest(JobsFixture):
    def test_a_job_added_by_the_base_contract_is_required_immediately(self):
        self.land('("plan", "tests", "nouveau job")')
        self.assert_required(["plan", "tests"])
        self.assertEqual(self.read(["plan", "tests", "nouveau job"]).read(self.plan)["status"],
                         "green")

    def test_a_job_added_by_the_base_contract_can_go_red(self):
        self.land('("plan", "tests", "nouveau job")')
        result = self.read(["plan", "tests", "nouveau job"],
                           run_conclusion="failure", failed=("nouveau job",))
        self.assertEqual(result.read(self.plan)["status"], "red")

    def test_a_job_removed_by_the_base_contract_stays_required(self):
        # The installed set is the floor: a base that drops a job cannot let a
        # head skip it before a deployment audits the lower contract.
        self.land('("plan",)')
        self.assert_required(["plan"])

    def test_a_job_added_only_at_the_head_is_not_audited(self):
        self.land('("plan", "tests", "job de la tête")', head_only=True)
        self.assertEqual(self.read(["plan", "tests"]).read(self.plan)["status"], "green")

    def test_a_starred_range_matrix_is_expanded_and_required(self):
        self.land('("plan", *("psycopg %d/8" % n for n in range(1, 3)), "tests")')
        self.assert_required(["plan", "psycopg 1/8", "tests"])
        self.assertEqual(self.read(["plan", "psycopg 1/8", "psycopg 2/8",
                                    "tests"]).read(self.plan)["status"], "green")

    def test_a_jobs_expression_outside_the_static_subset_is_refused(self):
        self.land("JOBS")
        with self.assertRaisesRegex(train.TrainError, "cannot be read statically"):
            train_ci.verify_local(self.repo, self.plan, (self.installed,))
        with self.assertRaisesRegex(train.TrainError, "cannot be read statically"):
            self.read(["plan", "tests"]).read(self.plan)

    def test_malformed_base_jobs_are_refused_not_ignored(self):
        for expression, message in (('("plan", "plan")', "malformed jobs"),
                                    ('("plan", 3)', "malformed jobs"),
                                    ('("plan", _JOBS)', "cannot be read statically")):
            with self.subTest(expression=expression):
                self.land(expression)
                with self.assertRaisesRegex(train.TrainError, message):
                    train_ci.verify_local(self.repo, self.plan, (self.installed,))


class LiveContractTest(unittest.TestCase):
    def test_the_ameesh_contract_jobs_are_static_and_complete(self):
        """The real contract must stay readable by the base-jobs reader."""
        contract, = train_contracts.ameesh()
        with open(train_contracts.__file__, encoding="utf-8") as handle:
            text = handle.read()
        keywords = train_ci._contract_call(text, contract.path)
        self.assertIsNotNone(keywords)
        self.assertEqual(train_ci._declared_jobs(keywords, contract.path), contract.jobs)


if __name__ == "__main__":
    unittest.main()

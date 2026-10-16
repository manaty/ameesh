# SPDX-License-Identifier: AGPL-3.0-only
"""Witness of `ameesh.gitpush` (#1173): the two-attempt policy.

No git, no network, no database: a fake `run` returns the chosen codes and
outputs. First attempt accepted -> a single call; refusal then acceptance ->
two traced calls; refusal on both attempts -> the last proc is returned to the
caller, which decides to stop. Credentials in the traced detail are masked, and
an exception raised by `run` propagates without a new attempt.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from ameesh import gitpush


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    """A scripted `run`: returns the procs in order, records the argv."""

    def __init__(self, *procs: _Proc):
        self.procs = list(procs)
        self.argv: list[list[str]] = []

    def __call__(self, argv):
        self.argv.append(list(argv))
        return self.procs[min(len(self.argv) - 1, len(self.procs) - 1)]


class _Exploding:
    """A `run` that raises instead of returning a proc."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = 0

    def __call__(self, argv):
        self.calls += 1
        raise self.exc


class PushWithRetryTest(unittest.TestCase):
    def test_succes_du_premier_coup_un_seul_essai(self):
        run = _Runner(_Proc(0))
        traces: list[str] = []
        proc = gitpush.push_with_retry(run, ["git", "push", "origin", "HEAD:x"],
                                       delay=0, trace=traces.append)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(run.argv), 1)
        self.assertEqual(traces, [])

    def test_rejet_distant_puis_succes_deux_essais_traces(self):
        run = _Runner(_Proc(1, stderr="! [remote rejected] HEAD -> lot/x (failed)"),
                      _Proc(0))
        traces: list[str] = []
        proc = gitpush.push_with_retry(run, ["git", "push", "origin", "HEAD:x"],
                                       delay=0, trace=traces.append)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(run.argv), 2)
        self.assertEqual(len(traces), 2)
        self.assertIn("attempt 1/2", traces[0])
        self.assertIn("remote rejected", traces[0])
        self.assertIn("accepted on the second attempt", traces[1])

    def test_echec_definitif_deux_essais_puis_arret(self):
        run = _Runner(_Proc(1, stderr="! [remote rejected] HEAD -> lot/x (failed)"))
        traces: list[str] = []
        proc = gitpush.push_with_retry(run, ["git", "push", "origin", "HEAD:x"],
                                       delay=0, trace=traces.append)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(run.argv), 2)
        self.assertEqual(len(traces), 2)
        self.assertIn("attempt 1/2", traces[0])
        self.assertIn("attempt 2/2", traces[1])

    def test_le_delai_par_defaut_est_de_dix_secondes(self):
        self.assertEqual(gitpush.RETRY_DELAY_S, 10)
        run = _Runner(_Proc(1, stderr="! [remote rejected] (failed)"), _Proc(0))
        with mock.patch.object(gitpush.time, "sleep") as dormir:
            gitpush.push_with_retry(run, ["git", "push"])
        dormir.assert_called_once_with(gitpush.RETRY_DELAY_S)

    def test_le_detail_trace_est_borne(self):
        run = _Runner(_Proc(1, stderr="x" * 1000), _Proc(0))
        traces: list[str] = []
        gitpush.push_with_retry(run, ["git", "push"], delay=0, trace=traces.append)
        detail = traces[0].split("): ", 1)[1].split("; retrying", 1)[0]
        self.assertEqual(len(detail), gitpush.DETAIL_MAX)

    def test_identifiants_masques_dans_le_detail(self):
        run = _Runner(_Proc(1, stderr="fatal: unable to access "
                            "'https://user:secret@github.com/manaty/x.git/'"),
                      _Proc(0))
        traces: list[str] = []
        gitpush.push_with_retry(run, ["git", "push"], delay=0, trace=traces.append)
        self.assertNotIn("secret", traces[0])
        self.assertIn("https://***@github.com", traces[0])

    def test_exception_de_run_remonte_sans_nouvel_essai(self):
        run = _Exploding(subprocess.TimeoutExpired(["git", "push"], 10))
        with self.assertRaises(subprocess.TimeoutExpired):
            gitpush.push_with_retry(run, ["git", "push"], delay=0)
        self.assertEqual(run.calls, 1)

    def test_trace_absente_est_sans_effet(self):
        run = _Runner(_Proc(1), _Proc(1))
        proc = gitpush.push_with_retry(run, ["git", "push"], delay=0)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(run.argv), 2)

    def test_raccourci_push_construit_la_meme_commande(self):
        run = _Runner(_Proc(0))
        gitpush.push(run, "origin", "HEAD:x", delay=0)
        self.assertEqual(run.argv, [["git", "push", "origin", "HEAD:x"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)

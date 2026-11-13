# SPDX-License-Identifier: AGPL-3.0-only
"""#6960 : un portier annulé après la fin de ses jobs ne bloque plus la fusion.

`ameesh ci run --full` annule le portier `paquet` de la tête au profit du run
`pull_request` de la PR (#2876). Quand GitHub traite l'annulation après la fin
des jobs, le run conclut `cancelled` sans qu'aucun check ne soit `CANCELLED` :
`couverture` est `SUCCESS` et `suite complète du paquet` `SKIPPED`. Le radar
exigeait un check `CANCELLED` du run le plus récent et tenait donc la
sentinelle `paquet (run proof unavailable)` sur une PR verte (h-17, run
43059719618, 11/11 16:37Z).

Témoin : les mêmes lecteurs que #2876 (preuve stricte `ci_green`, `fire`,
`packet_automerge`, `merge_pr`) doivent verdir sur ce run, et bloquer dès
qu'un check du portier n'a pas conclu sans échec.

#6982 : l'admission d'un run annulé sans check CANCELLED repose sur une liste
blanche de conclusions (SUCCESS, SKIPPED, NEUTRAL) ; TIMED_OUT et
STARTUP_FAILURE ne sont pas des preuves de vert et bloquent.
"""
import unittest

from ameesh import fire, merge_pr, packet_automerge, radar

from . import test_2876_packet_replacement as t2876

SHA = t2876.SHA
OLD = t2876.OLD


def witness():
    """Run 43059719618 : couverture SUCCESS, suite SKIPPED, run cancelled."""
    return [
        t2876.check("couverture (PR ouverte sur cette tête)", workflow="paquet", end=OLD),
        t2876.check("suite complète du paquet", "SKIPPED", workflow="paquet", end=OLD),
        t2876.check("psql 2/16"),
        t2876.check("tests"),
    ]


class PacketCancelledFinishedTest(unittest.TestCase):
    def assert_gate(self, rows, green, **proof):
        normalized = radar.merge_rollup(rows, slug="manaty/ameesh", head=SHA,
                                        branch="integration/paquet-h-2876",
                                        gh=t2876.gh_for(rows, **proof))
        self.assertEqual(radar.ci_green(normalized), green)
        self.assertEqual(
            any(entry.get("name") == "paquet (run proof unavailable)"
                for entry in normalized), not green)
        status = fire._pr_status("manaty/ameesh", "integration/paquet-h-2876",
                                 gh=t2876.gh_for(rows, **proof))
        self.assertEqual(status["ci"] == "green", green)
        ready, why = fire.ready_pr(status, target="main", project=None,
                                  frozen=SHA, required=("tests",))
        self.assertEqual(ready, green, why)
        status = packet_automerge.pr_status("manaty/ameesh", 2194,
                                            gh=t2876.gh_for(rows, single=True, **proof))
        self.assertEqual(status["ci"] == "green", green)
        ready, why = fire.ready_pr(status, target="main", project=None,
                                  frozen=SHA, required=("tests",))
        self.assertEqual(ready, green, why)
        merge_view = merge_pr.pr_view("manaty/ameesh", 2194,
                                      gh=t2876.gh_for(rows, single=True, **proof))
        if green:
            merge_pr._pr_guards(merge_view, 2194, SHA, "main")
        else:
            with self.assertRaises(merge_pr.MergePrError):
                merge_pr._pr_guards(merge_view, 2194, SHA, "main")

    def test_cancelled_run_finished_without_cancelled_check_is_replaced(self):
        for ordered in (witness(), witness()[::-1]):
            self.assert_gate(ordered, True)

    def test_a_failed_or_unfinished_job_of_the_cancelled_run_blocks(self):
        for conclusion, status in (("FAILURE", "COMPLETED"), ("", "IN_PROGRESS")):
            rows = witness()
            rows[1].update(conclusion=conclusion, status=status,
                           completedAt=OLD if status == "COMPLETED" else None)
            with self.subTest(conclusion=conclusion, status=status):
                self.assert_gate(rows, False)

    def test_the_cancelled_run_is_admitted_only_on_allowed_conclusions(self):
        # #6982 : l'allowlist remplace l'exclusion du seul FAILURE. Un check
        # du portier TIMED_OUT ou STARTUP_FAILURE n'établit aucune preuve.
        for conclusion in ("SUCCESS", "SKIPPED", "NEUTRAL"):
            rows = witness()
            rows[1]["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion):
                self.assert_gate(rows, True)
        for conclusion in ("TIMED_OUT", "STARTUP_FAILURE"):
            rows = witness()
            rows[1]["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion):
                self.assert_gate(rows, False)

    def test_a_cancelled_check_of_another_run_does_not_prove_the_latest(self):
        # Le run 101 (le plus récent) n'a aucun check visible : un CANCELLED
        # du run 100 ne prouve pas que 101 a fini sans échec.
        self.assert_gate(witness(), False, runs=[t2876.packet_run(),
                                                 t2876.packet_run(101)])

    def test_an_old_cancellation_cannot_hide_a_failure_of_the_latest_run(self):
        rows = witness() + [
            t2876.check("suite complète du paquet / psql 3/16", "CANCELLED",
                        workflow="paquet", end=OLD, run=99),
            t2876.check("suite complète du paquet / tests", "FAILURE",
                        workflow="paquet", end=OLD, run=100),
        ]
        self.assert_gate(rows, False, runs=[t2876.packet_run(99),
                                            t2876.packet_run()])

    def test_the_pr_suite_must_finish_after_every_packet_check(self):
        rows = witness()
        rows[3]["completedAt"] = OLD
        self.assert_gate(rows, False)


if __name__ == "__main__":
    unittest.main()

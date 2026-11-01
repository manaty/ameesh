# SPDX-License-Identifier: AGPL-3.0-only
"""#5298 — le flux d'un attempt antérieur ne bloque pas une tête rejouée verte.

Constat (paquet h-20261101-10, #5273, PR #3986, tête 99efb551, run
41290078741) : l'attempt 1 laisse `tests (flux) psycopg 3/8` en `failure` ;
`gh run rerun --failed` conclut l'attempt 2 vert, mais `statusCheckRollup`
(lecture `filter=latest`) ne rend qu'un check par nom et garde l'ancien rouge
(son `startedAt` est plus grand, horloge du runner). L'agrégat requis `tests`,
vert et conclu après, prouve que la part a été rejouée : le flux antérieur ne
bloque plus. Un flux de l'attempt courant, lui, reste bloquant.

Le fix est dans `radar.superseded_flux` / `radar.packet_proof_entries` : la
preuve du portier et les deux portes (`fire.ready_pr`, `merge_pr`) lisent ce
remplacement. Les tests couvrent les deux côtés : le flux remplacé (fusion) et
le flux courant (refus), sans jamais verdir un rouge postérieur à l'agrégat.
"""
from __future__ import annotations

import json
import unittest

from ameesh import fire, merge_pr, packet_automerge, radar
from . import test_2813_packet_auto_merge as banc
from .test_2876_packet_replacement import NEW, OLD, SHA, check, gh_for, view, witness

FLUX = "tests (flux) psycopg 3/8"


def flux(end=OLD, conclusion="FAILURE"):
    return check(FLUX, conclusion, workflow="paquet", end=end)


def green_tests(end=NEW):
    return check("tests", "SUCCESS", end=end)


class SupersededFluxTest(unittest.TestCase):
    def names(self, rows):
        return [entry.get("name") for entry in radar.packet_proof_entries(rows)]

    def test_a_flux_concluded_before_a_later_green_aggregate_is_replaced(self):
        # Témoin du constat : le flux finit à 11:37:57, l'agrégat requis vert
        # à 12:20:00 — la part a été rejouée depuis.
        rows = [green_tests(), flux(OLD)]
        self.assertNotIn(FLUX, self.names(rows))
        self.assertEqual(fire._packet_proof_gate(rows), ([], []))

    def test_an_equal_timestamp_is_not_replaced(self):
        # Un flux conclu en même temps que l'agrégat n'est pas un antérieur :
        # la porte ne le suppose pas remplacé (dates ex aequo, doute fermé).
        rows = [green_tests(NEW), flux(NEW)]
        self.assertIn(FLUX, self.names(rows))
        self.assertEqual(fire._packet_proof_gate(rows),
                         (["%s (FAILURE)" % FLUX], []))

    def test_a_flux_after_the_aggregate_stays_blocking(self):
        # Un flux de l'attempt courant, conclu après l'agrégat, reste un rouge
        # courant : jamais toléré (#5298).
        rows = [green_tests(), flux("2026-10-20T12:30:00Z")]
        self.assertEqual(fire._packet_proof_gate(rows),
                         (["%s (FAILURE)" % FLUX], []))

    def test_no_green_aggregate_keeps_the_flux_blocking(self):
        rows = [flux(OLD)]
        self.assertEqual(fire._packet_proof_gate(rows),
                         (["%s (FAILURE)" % FLUX], []))

    def test_a_red_aggregate_does_not_replace_the_flux(self):
        rows = [check("tests", "FAILURE", end=NEW), flux(OLD)]
        self.assertEqual(fire._packet_proof_gate(rows),
                         (["%s (FAILURE)" % FLUX], []))

    def test_a_flux_without_a_readable_end_is_not_replaced(self):
        for end in (None, "", "invalid"):
            with self.subTest(end=end):
                rows = [green_tests(), flux(end)]
                self.assertEqual(fire._packet_proof_gate(rows),
                                 (["%s (FAILURE)" % FLUX], []))

    def test_an_in_progress_flux_still_waits(self):
        # Le flux courant n'a pas encore conclu : il attend, comme avant.
        rows = [green_tests(),
                dict(flux("2026-10-20T12:30:00Z"), status="IN_PROGRESS",
                     conclusion="")]
        self.assertEqual(fire._packet_proof_gate(rows),
                         (["%s (PENDING)" % FLUX], []))


class FireGateTest(unittest.TestCase):
    """La porte du feu/auto-merge sur le rollup réel du paquet h-10."""

    def ready(self, rows):
        gh = gh_for(rows, single=True)
        status = packet_automerge.pr_status("manaty/ameesh", 2194, gh=gh)
        return fire.ready_pr(status, target="main", project=None, frozen=SHA,
                             required=("tests",))

    def test_ready_pr_passes_with_the_stale_flux(self):
        ok, why = self.ready(witness() + [flux(OLD)])
        self.assertTrue(ok, why)

    def test_ready_pr_still_refuses_a_current_flux(self):
        ok, why = self.ready(witness() + [flux("2026-10-20T12:30:00Z")])
        self.assertFalse(ok, why)
        self.assertIn(FLUX, why)

    def test_ready_pr_still_refuses_a_current_flux_without_the_aggregate(self):
        rows = [entry for entry in witness() if entry["name"] != "tests"]
        rows.append(flux(OLD))
        ok, why = self.ready(rows)
        self.assertFalse(ok, why)
        self.assertIn(FLUX, why)


class MergePrGateTest(unittest.TestCase):
    """`merge-pr` sous `UNSTABLE` : même preuve, même règle."""

    def unstable(self, rows):
        data = view(rows)
        data["mergeStateStatus"] = "UNSTABLE"
        return data

    def test_unstable_tolerates_the_stale_flux(self):
        rows = [check("tests", "SUCCESS"), flux(OLD)]
        self.assertTrue(merge_pr._unstable_ci_ok(rows))
        merge_pr._pr_guards(self.unstable(rows), 2194, SHA, "main")

    def test_unstable_still_refuses_a_current_flux(self):
        rows = [check("tests", "SUCCESS"), flux("2026-10-20T12:30:00Z")]
        self.assertFalse(merge_pr._unstable_ci_ok(rows))
        with self.assertRaises(merge_pr.MergePrError):
            merge_pr._pr_guards(self.unstable(rows), 2194, SHA, "main")


class PacketAutoMergeTest(banc.Journal):
    """Le passage d'auto-merge du porteur, avec les checks de la PR #3986."""

    def forge(self, extra):
        gh = banc.FakeGh(tests="success")
        answer = gh.answer

        def respond(args):
            code, out, err = answer(args)
            if args[:2] == ["pr", "view"] and not code:
                data = json.loads(out)
                data["statusCheckRollup"].extend(extra)
                out = json.dumps(data)
            return code, out, err

        gh.answer = respond
        return gh

    def test_a_green_packet_with_a_stale_flux_merges(self):
        self.armed()
        # Le banc pose l'agrégat vert à 03:20:00Z : le flux antérieur finit
        # avant lui. Le flux courant du cas voisin, lui, finit après.
        gh = self.forge([flux("2026-10-20T03:18:14Z")])
        out = packet_automerge.check(None, None, banc.LOT, gh=gh)
        self.assertEqual(out["result"], "merged", out)
        self.assertEqual(gh.landed, [("main", banc.HEAD)])

    def test_a_current_flux_still_holds_the_packet(self):
        self.armed()
        gh = self.forge([flux("2026-10-20T03:30:00Z")])
        out = packet_automerge.check(None, None, banc.LOT, gh=gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertEqual(gh.landed, [])
        self.assertTrue(any(FLUX in note for note in self.notes()), self.notes())


if __name__ == "__main__":
    unittest.main()

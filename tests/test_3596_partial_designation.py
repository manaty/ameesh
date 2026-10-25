# SPDX-License-Identifier: AGPL-3.0-only
"""#3596 — désignation partielle au gel : pourvoir les familles présentes.

Quand les avis requis dépassent les familles disponibles, la désignation ne
doit plus rendre « aucun relecteur » : elle nomme tout de suite les sièges que
les familles éligibles peuvent tenir, reste `waiting` avec le seul manque, et
une reprise du même gel garde les automatiques déjà nommés — sans les compter
comme manuels, ni les effacer, ni leur renvoyer une demande.

Témoins du lot : (1) 2 avis, seule claude disponible (auteur deepseek) →
claude désigné, `waiting`, manque 1 ; (2) codex revient → la reprise ajoute
codex seul, claude conservé ; (3) auteur de famille inconnue → personne ;
(4) aucune famille → message inchangé.
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd

from .test_1617_review_designation import (FAMILIES, LOT, RULES, SHA_A, SHA_B,
                                           SHA_C, FakePort, candidates, row)

AUTHORS = [{"name": "author", "family": "deepseek"}]


def apply(port, **kw):
    args = dict(policy=RULES, paths=["src/ameesh/db.py"], port=port,
                authors=AUTHORS, candidates=(), risk="sensitive", base=SHA_C,
                policy_ref="canon@deadbeef", notify="mesh-design")
    args.update(kw)
    if "freeze_sha" not in args:
        latest = port.latest_freeze(LOT["id"]) or {}
        args["freeze_sha"] = latest.get("sha") or SHA_A
    return rd.apply(LOT, **dict({"families": FAMILIES}, **args))


class PartialSelectionTest(unittest.TestCase):
    """La sélection nomme ce qu'elle peut, et attend le reste."""

    def test_the_available_family_is_designated_at_once(self):
        # Témoin (1) : deux avis, seule claude est disponible.
        picked = rd.select(
            families=FAMILIES, required=2, authors=AUTHORS,
            candidates=candidates(row("claude-one", harness="claude",
                                      session="claude")))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertEqual(picked["missing"], 1)
        self.assertEqual(picked["families"], ["claude"])
        self.assertIn("1 independent review missing", picked["why"])

    def test_the_partial_seat_keeps_the_selection_order(self):
        # Allure/charge/nom : le siège partiel prend le représentant que la
        # sélection complète aurait pris pour cette famille.
        pool = candidates(row("claude-heavy", harness="claude", session="claude"),
                          row("claude-light", harness="claude", session="claude"),
                          loads={"claude-heavy": 7, "claude-light": 1})
        picked = rd.select(families=FAMILIES, required=2, authors=AUTHORS,
                           candidates=pool)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-light"])

    def test_an_unknown_author_family_designates_nobody(self):
        # Témoin (3) : l'indépendance n'est pas prouvée, même pour un siège.
        picked = rd.select(
            families=FAMILIES, required=2,
            authors=[{"name": "author", "family": "unknown"}],
            candidates=candidates(row("claude-one", harness="claude",
                                      session="claude")))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual(picked["reviewers"], [])
        self.assertEqual(picked["missing"], 2)

    def test_no_available_family_keeps_the_unchanged_wait(self):
        # Témoin (4) : aucune famille éligible, le message reste celui d'avant.
        picked = rd.select(
            families=FAMILIES, required=2, authors=AUTHORS,
            candidates=candidates(row("deepseek-one", harness="deepseek",
                                      session="deepseek")))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual(picked["reviewers"], [])
        self.assertEqual(picked["missing"], 2)
        self.assertIn("0 eligible families (none)", picked["why"])

    def test_an_already_named_automatic_is_never_counted_as_manual(self):
        # Le même nom arrive deux fois (colonne manuelle + marque automatique) :
        # un seul siège, et c'est l'automatique qui est conservé.
        picked = rd.select(
            families=FAMILIES, required=2, authors=AUTHORS,
            candidates=candidates(row("claude-one", harness="claude", session="claude")),
            already=[{"name": "claude-one", "family": "claude"}],
            automatic=[{"name": "claude-one", "family": "claude"}])
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual(picked["manual"], [])
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertEqual(picked["missing"], 1)


class PartialRetryTest(unittest.TestCase):
    """Une reprise du même gel garde les sièges déjà pourvus."""

    def setUp(self):
        self.port = FakePort({1617: [{"id": 1, "sha": SHA_A}]})
        self.two = candidates(row("claude-one", harness="claude", session="claude"),
                              row("codex-one", harness="codex", session="codex"))

    def apply(self, **kw):
        if "candidates" not in kw:
            kw["candidates"] = self.two
        return apply(self.port, **kw)

    def test_the_retry_adds_only_the_missing_family(self):
        # Témoin (2) : claude est nommé au premier passage, codex arrive après.
        first = self.apply(candidates=candidates(
            row("claude-one", harness="claude", session="claude")))
        self.assertEqual(first["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in first["reviewers"]], ["claude-one"])
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual([to for to, _body in self.port.messages],
                         ["mesh-design", "claude-one"])
        self.assertIn("Already designated: claude-one (claude)",
                      self.port.messages[0][1])

        second = self.apply()
        self.assertEqual(second["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in second["reviewers"]],
                         ["claude-one", "codex-one"])
        self.assertEqual(len(self.port.commits), 2)
        # claude ne reçoit pas de seconde demande ; seul codex est ajouté.
        self.assertEqual([to for to, _body in self.port.messages],
                         ["mesh-design", "claude-one", "codex-one"])

        third = self.apply()
        self.assertTrue(third.get("replayed"))
        self.assertEqual(len(self.port.commits), 2)
        self.assertEqual(len(self.port.messages), 3)

    def test_a_new_freeze_does_not_carry_the_partial_seat(self):
        # Un nouveau gel efface les automatiques comme aujourd'hui : seul le
        # candidat du nouveau gel est nommé.
        self.apply(candidates=candidates(row("claude-one", harness="claude",
                                             session="claude")))
        self.port.freezes[1617].append({"id": 2, "sha": SHA_B})
        out = self.apply(candidates=candidates(row("codex-one", harness="codex",
                                                   session="codex")))
        self.assertEqual(out["freeze"], {"id": 2, "sha": SHA_B})
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["codex-one"])
        self.assertEqual(len(self.port.commits), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

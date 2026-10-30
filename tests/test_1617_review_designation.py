# SPDX-License-Identifier: AGPL-3.0-only
"""#1617 — la sélection des relecteurs au gel : règles, familles, attente, gel.

Tests de la sélection seule (aucune base) : la politique versionnée en règles,
les candidats du registre, l'indépendance des familles (auteur, correcteur
devenu auteur), la disponibilité et la charge, l'attente explicite avec sa
notification de capacité, et le raccord idempotent au jalon `frozen` (même gel
rejoué = un seul courrier ; nouveau gel = nouvelle désignation, jamais un
retour à l'ancienne tête). Le port est simulé : le module ne fait aucune E/S.
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd

#: #2451 : familles explicites ; l'activation passe celles du canon compilé
FAMILIES = ("claude", "codex", "deepseek")

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40

LOT = {"id": 1617, "title": "Processus en code : désigner les relecteurs au gel",
       "app": "ameesh", "assignee": "mty-amh-impl-02"}

RULES = {"rules": [
    {"paths": ["src/ameesh/db.py", "**/*.sql"], "reviews": 2},
    {"paths": ["src/ameesh/work.py"], "reviews": 1},
]}


def row(name, *, team="ameesh", harness="deepseek", session="", status="idle",
        mode="execute", text="", capabilities=("read", "report-drift", "propose"),
        roles=("implementation",)):
    """Une ligne de registre de relecteur (#4779 : capacité `propose` et rôle)."""
    return {"name": name, "team": team, "harness": harness, "session_harness": session,
            "status": status, "mode": mode, "status_text": text,
            "capabilities": list(capabilities),
            "roles": None if roles is None else list(roles)}


def candidates(*rows, project="ameesh", loads=None):
    return rd.candidates_from_registry(rows, families=FAMILIES, project=project, loads=loads)


class FakePort:
    """Le raccord réel simulé : gel courant, marque par gel, une seule écriture."""

    def __init__(self, freezes, records=None):
        self.freezes = {int(lot): list(rows) for lot, rows in (freezes or {}).items()}
        self.records = dict(records or {})
        self.commits = []
        self.messages = []
        self.stale_refusals = 0

    def latest_freeze(self, lot):
        rows = self.freezes.get(int(lot)) or []
        return dict(rows[-1]) if rows else None

    def recorded(self, lot, freeze_id):
        found = self.records.get((int(lot), int(freeze_id)))
        return found

    def commit(self, lot, *, freeze, required, state, reviewers, missing, why, authors,
               policy_ref="", notify="", messages=(), unknown_authors=()):
        latest = self.latest_freeze(lot)
        if not latest or int(latest["id"]) != int(freeze["id"]):
            self.stale_refusals += 1
            raise rd.StaleFreeze("freeze is no longer the latest one")
        out = {"state": state, "required": required,
               "reviewers": [dict(r) for r in reviewers], "missing": missing, "why": why,
               "freeze": dict(freeze), "authors": [dict(a) for a in authors],
               "unknown_authors": [str(value) for value in unknown_authors],
               "policy_ref": policy_ref, "notify": notify,
               "messages": [dict(m) for m in messages]}
        self.records[(int(lot), int(freeze["id"]))] = out
        self.commits.append(out)
        self.messages.extend((m.get("to"), m.get("body")) for m in messages)
        return dict(out)


class MovingPort(FakePort):
    """Le gel bouge pendant l'écriture : le commit refuse, le moteur s'arrête."""

    def __init__(self, freezes, moves):
        super().__init__(freezes)
        self.moves = list(moves)

    def commit(self, lot, **kw):
        if self.moves:
            self.freezes[int(lot)].append(self.moves.pop(0))
            self.stale_refusals += 1
            raise rd.StaleFreeze("freeze moved during the write")
        return super().commit(lot, **kw)


class CoveringRequestTest(unittest.TestCase):
    """#2495 : un seul request par (lot, relecteur, tête)."""

    SHA = "a" * 40

    def row(self, *, sha="", delivered=False, lot="7", to="r1", kind="request", auto=False):
        if auto:
            payload = {"review_designation": {"lot": 7, "sha": sha}}
        else:
            payload = {"review_request": {"lot": 7, "reviewer": to, "manual": True}}
            if sha:
                payload["review_request"]["freeze"] = {"id": 3, "sha": sha}
        return {"id": 1, "kind": kind, "work_item_id": lot, "recipient": to,
                "payload": payload, "delivered_ts": 1.0 if delivered else None}

    def covered(self, row, sha=""):
        return rd.covering_request([row], lot=7, reviewer="r1", sha=sha) is not None

    def test_same_head_is_covered_delivered_or_not(self):
        for auto in (False, True):
            for delivered in (False, True):
                self.assertTrue(self.covered(self.row(sha=self.SHA, auto=auto,
                                                      delivered=delivered), self.SHA))
        # SHA abrégé du jalon : même gel
        self.assertTrue(self.covered(self.row(sha=self.SHA[:9]), self.SHA))

    def test_another_head_is_not_covered(self):
        self.assertFalse(self.covered(self.row(sha="b" * 40, auto=True), self.SHA))

    def test_a_manual_request_without_head_covers_the_freeze_while_unread(self):
        self.assertTrue(self.covered(self.row(), self.SHA))
        self.assertFalse(self.covered(self.row(delivered=True), self.SHA))
        # re-désignation avant le gel : déjà prévenu, lu ou non
        self.assertTrue(self.covered(self.row(delivered=True)))

    def test_other_lot_reviewer_or_kind_does_not_count(self):
        self.assertFalse(self.covered(self.row(sha=self.SHA, lot="8"), self.SHA))
        self.assertFalse(self.covered(self.row(sha=self.SHA, to="r2"), self.SHA))
        self.assertFalse(self.covered(self.row(sha=self.SHA, kind="notify"), self.SHA))

    def test_manual_message_does_not_promise_a_new_freeze_cancels_it(self):
        for freeze in (None, {"id": 3, "sha": self.SHA}):
            body = rd.manual_reviewer_message({"id": 7, "title": "t"}, freeze=freeze)
            self.assertNotIn("cancels this request", body)
            self.assertIn("A new freeze does not cancel this request", body)

    def test_an_ordinary_request_does_not_suppress_a_review_request(self):
        for payload in ({}, {"question": "Please clarify this lot"},
                        {"review_request": "invalid"}, "invalid json", []):
            row = dict(self.row(), payload=payload)
            self.assertFalse(self.covered(row, self.SHA))
            self.assertFalse(self.covered(row))


class PolicyTest(unittest.TestCase):
    """Le nombre requis : le maximum des règles par chemin, zéro pour un léger."""

    def test_max_over_paths_and_light_default(self):
        rules = rd.rules_of(RULES)
        self.assertEqual(rd.required_count(rules, []), 0)
        self.assertEqual(rd.required_count(rules, ["docs/guide.md"]), 0)
        self.assertEqual(rd.required_count(rules, ["src/ameesh/work.py"]), 1)
        self.assertEqual(rd.required_count(rules, ["src/ameesh/db.py"]), 2)
        self.assertEqual(rd.required_count(rules, ["src/ameesh/work.py", "a/b.sql"]), 2)
        self.assertEqual(rd.required_count(rules, ["src/ameesh/db.py",
                                                   "src/ameesh/work.py"]), 2)

    def test_the_declared_light_floor_raises_the_count(self):
        # #2642 : le plancher léger est lu par le lecteur de politique
        # (mechanics.reviews_for), jamais deviné ici ; il ne rabaisse aucune règle.
        rules = rd.rules_of(RULES)
        self.assertEqual(rd.required_count(rules, ["docs/guide.md"], light=1), 1)
        self.assertEqual(rd.required_count(rules, ["src/ameesh/work.py"], light=1), 1)
        self.assertEqual(rd.required_count(rules, ["src/ameesh/db.py"], light=1), 2)
        self.assertEqual(rd.required_count(rules, [], light=1), 1)

    def test_glob_semantics_do_not_leak_between_paths(self):
        rules = rd.rules_of({"rules": [{"paths": ["docs/**"], "reviews": 1}]})
        self.assertEqual(rd.required_count(rules, ["docs"]), 0)
        self.assertEqual(rd.required_count(rules, ["docs/x/y.md"]), 1)

    def test_malformed_rules_are_refused(self):
        for bad in ({}, {"rules": []}, {"rules": "x"}, {"rules": [{"paths": ["a"]}]},
                    {"rules": [{"paths": ["a"], "reviews": 3}]},
                    {"rules": [{"paths": ["a"], "reviews": True}]},
                    {"rules": [{"paths": [], "reviews": 1}]},
                    {"rules": [{"paths": [3], "reviews": 1}]},
                    {"rules": [{"paths": ["a"], "reviews": 1, "extra": 2}]}):
            with self.subTest(bad=bad), self.assertRaises(rd.DesignationError):
                rd.rules_of(bad)


class SelectionTest(unittest.TestCase):
    """Familles réelles, indépendance, disponibilité, charge, attente."""

    def test_two_reviews_go_to_two_distinct_families_outside_the_author(self):
        picked = rd.select(families=FAMILIES,
            required=2,
            authors=[{"name": "mty-amh-impl-02", "family": "deepseek"}],
            candidates=candidates(
                row("claude-one", harness="claude", session="claude"),
                row("claude-two", harness="claude", session="claude"),
                row("codex-one", harness="codex", session="codex"),
                row("deepseek-one", harness="deepseek", session="deepseek"),
            ))
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["family"] for r in picked["reviewers"]], ["claude", "codex"])
        self.assertEqual(picked["families"], ["claude", "codex"])
        self.assertNotIn("deepseek", {r["family"] for r in picked["reviewers"]})

    def test_corrector_become_author_is_never_designated(self):
        # rev-claude a corrigé la tête : il est auteur de sa correction.
        authors = [{"name": "mty-amh-impl-02", "family": "deepseek"},
                   {"name": "rev-claude", "family": "claude"}]
        pool = candidates(
            row("rev-claude", harness="claude", session="claude"),
            row("claude-two", harness="claude", session="claude"),
            row("codex-one", harness="codex", session="codex"),
            row("deepseek-one", harness="deepseek", session="deepseek"),
        )
        one = rd.select(families=FAMILIES, required=1, authors=authors, candidates=pool)
        self.assertEqual(one["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in one["reviewers"]], ["codex-one"])
        self.assertEqual(one["families"], ["codex"])
        # Deux avis hors claude et hors deepseek : la seule famille restante ne
        # suffit pas, mais elle est pourvue tout de suite (#3596).
        two = rd.select(families=FAMILIES, required=2, authors=authors, candidates=pool)
        self.assertEqual(two["state"], rd.WAITING)
        self.assertEqual(two["missing"], 1)
        self.assertEqual([r["name"] for r in two["reviewers"]], ["codex-one"])
        self.assertEqual(two["families"], ["codex"])

    def test_author_with_unknown_family_waits_instead_of_guessing(self):
        # codex-two est peut-être de la famille réelle de l'auteur : on attend.
        for family in ("", "unknown"):
            picked = rd.select(families=FAMILIES,
                required=1,
                authors=[{"name": "codex-one", "family": family}],
                candidates=candidates(row("codex-one", harness="codex", session="codex"),
                                      row("codex-two", harness="codex", session="codex")))
            self.assertEqual(picked["state"], rd.WAITING)
            self.assertEqual(picked["reviewers"], [])
            self.assertEqual(picked["missing"], 1)
            self.assertIn("codex-one", picked["why"])

    def test_unavailable_candidates_are_skipped_even_the_least_loaded(self):
        # Les indisponibles seraient les moins chargés : la disponibilité passe avant.
        pool = candidates(
            row("claude-stopped", harness="claude", session="claude", status="stopped"),
            row("claude-busy", harness="claude", session="claude", status="running"),
            row("codex-paused", harness="codex", session="codex", status="blocked",
                text="budget : plafond"),
            row("deepseek-ok", harness="deepseek", session="deepseek", status="idle"),
            loads={"claude-stopped": 0, "codex-paused": 0, "claude-busy": 3,
                   "deepseek-ok": 0})
        picked = rd.select(families=FAMILIES, required=1,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-busy"])

    def test_a_reviewer_on_a_closed_host_is_not_a_candidate(self):
        # #3328 : l'hôte aux claims fermés est la preuve que lit agent-mail ;
        # le même témoin doit écarter le candidat de la désignation.
        rows = [row("claude-lost", harness="claude", session="claude"),
                row("claude-ok", harness="claude", session="claude"),
                row("codex-ok", harness="codex", session="codex")]
        rows[0]["host"] = "pc-smichea"
        pool = rd.candidates_from_registry(rows, families=FAMILIES,
                                           project="ameesh",
                                           closed_hosts={"pc-smichea"})
        self.assertEqual([c.name for c in pool if c.available],
                         ["claude-ok", "codex-ok"])
        picked = rd.select(families=FAMILIES, required=2,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]],
                         ["claude-ok", "codex-ok"])

    def test_a_family_only_on_a_closed_host_waits_instead_of_designating_it(self):
        # #3328 : seul candidat de sa famille, mais sur un hôte perdu : le
        # siège attend (alerte capacité) au lieu d'être déposé chez un mort.
        rows = [row("claude-lost", harness="claude", session="claude"),
                row("codex-ok", harness="codex", session="codex")]
        rows[0]["host"] = "pc-smichea"
        pool = rd.candidates_from_registry(rows, families=FAMILIES,
                                           project="ameesh",
                                           closed_hosts={"pc-smichea"})
        picked = rd.select(families=FAMILIES, required=2,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-ok"])
        self.assertEqual(picked["missing"], 1)
        self.assertEqual(picked["families"], ["codex"])

    def test_an_undrained_host_is_a_candidate_again(self):
        # #3328, témoin (3) : l'hôte rouvert rend le relecteur candidat, sans
        # autre changement de registre.
        rows = [row("claude-lost", harness="claude", session="claude")]
        rows[0]["host"] = "pc-smichea"
        closed = rd.candidates_from_registry(rows, families=FAMILIES,
                                             project="ameesh",
                                             closed_hosts={"pc-smichea"})
        self.assertFalse(closed[0].available)
        opened = rd.candidates_from_registry(rows, families=FAMILIES,
                                             project="ameesh",
                                             closed_hosts=())
        self.assertTrue(opened[0].available)

    def test_load_orders_the_choice_and_is_deterministic(self):
        pool = candidates(
            row("claude-heavy", harness="claude", session="claude"),
            row("claude-light", harness="claude", session="claude"),
            row("codex-light", harness="codex", session="codex"),
            loads={"claude-heavy": 7, "claude-light": 1, "codex-light": 1})
        picked = rd.select(families=FAMILIES, required=1,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-light"])
        two = rd.select(families=FAMILIES, required=2,
                        authors=[{"name": "impl-02", "family": "deepseek"}],
                        candidates=pool)
        self.assertEqual([r["name"] for r in two["reviewers"]], ["claude-light", "codex-light"])
        # Un seul candidat par famille (le moins chargé) : jamais deux fois la même.
        self.assertEqual(len({r["family"] for r in two["reviewers"]}), 2)

    def test_the_pending_reviews_of_the_load_are_named_in_the_why(self):
        # #3004, témoin (a) : même famille, même rythme ; `claude-queue` a 0 lot
        # assigné mais 5 avis en attente, `claude-busy` 2 lots assignés et 0 avis.
        # La charge (`_loads`) tranche pour le second, et la marque du gel dit la
        # charge retenue de chaque candidat choisi.
        pool = candidates(
            row("claude-queue", harness="claude", session="claude"),
            row("claude-busy", harness="claude", session="claude"),
            loads={"claude-queue": 5, "claude-busy": 2})
        one = rd.select(families=FAMILIES, required=1,
                        authors=[{"name": "impl-02", "family": "deepseek"}],
                        candidates=pool)
        self.assertEqual([r["name"] for r in one["reviewers"]], ["claude-busy"])
        self.assertIn("load: claude-busy 2", one["why"])
        self.assertNotIn("claude-queue 5", one["why"])
        pool = candidates(
            row("claude-queue", harness="claude", session="claude"),
            row("claude-busy", harness="claude", session="claude"),
            row("codex-one", harness="codex", session="codex"),
            loads={"claude-queue": 5, "claude-busy": 2, "codex-one": 0})
        two = rd.select(families=FAMILIES, required=2,
                        authors=[{"name": "impl-02", "family": "deepseek"}],
                        candidates=pool)
        self.assertEqual([r["name"] for r in two["reviewers"]],
                         ["codex-one", "claude-busy"])
        self.assertIn("load:", two["why"])
        self.assertIn("codex-one 0", two["why"])
        self.assertIn("claude-busy 2", two["why"])

    def test_family_comes_from_the_session_never_from_the_name(self):
        pool = candidates(
            row("codex3", harness="deepseek", session="claude"),
            row("claude-bis", harness="other", session=""),
            row("veille", harness="deepseek", session="", status="running", team="ameesh"),
        )
        picked = rd.select(families=FAMILIES, required=1,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        # `codex3` tourne sous claude (L242b) : sa famille est claude, pas son nom.
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex3"])
        self.assertEqual(picked["reviewers"][0]["family"], "claude")

    def test_waiting_is_explicit(self):
        picked = rd.select(families=FAMILIES,
            required=2,
            authors=[{"name": "impl-02", "family": "deepseek"}],
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("claude-two", harness="claude", session="claude")))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertEqual(picked["missing"], 1)
        self.assertEqual(picked["families"], ["claude"])
        self.assertIn("claude", picked["why"])
        self.assertIn("deepseek", picked["why"])

    def test_a_manual_reviewer_covers_only_its_own_seat(self):
        # #2627 : un manuel claude couvre un des deux avis ; le manque est le
        # codex, mail et selection inchanges.
        picked = rd.select(families=FAMILIES,
            required=2,
            authors=[{"name": "impl-02", "family": "deepseek"}],
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex")),
            already=[{"name": "manual-claude", "family": "claude"}])
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])
        self.assertEqual(picked["manual"],
                         [{"name": "manual-claude", "family": "claude"}])
        self.assertEqual(picked["missing"], 0)

    def test_a_manual_reviewer_of_the_author_family_covers_no_seat(self):
        picked = rd.select(families=FAMILIES,
            required=1,
            authors=[{"name": "impl-02", "family": "deepseek"}],
            candidates=candidates(row("codex-one", harness="codex", session="codex")),
            already=[{"name": "manual-deepseek", "family": "deepseek"}])
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])
        self.assertEqual(picked["manual"], [])

    def test_two_manual_reviewers_of_one_family_cover_one_seat(self):
        picked = rd.select(families=FAMILIES,
            required=2,
            authors=[{"name": "impl-02", "family": "deepseek"}],
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("codex-one", harness="codex", session="codex")),
            already=[{"name": "manual-a", "family": "claude"},
                     {"name": "manual-b", "family": "claude"}])
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex-one"])
        self.assertEqual([r["name"] for r in picked["manual"]], ["manual-a"])

    def test_manual_reviewers_covering_the_need_designate_nobody(self):
        picked = rd.select(families=FAMILIES,
            required=2,
            authors=[{"name": "impl-02", "family": "deepseek"}],
            candidates=(),
            already=[{"name": "manual-claude", "family": "claude"},
                     {"name": "manual-codex", "family": "codex"}])
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual(picked["reviewers"], [])
        self.assertEqual(len(picked["manual"]), 2)
        self.assertEqual(picked["missing"], 0)

    def test_required_must_be_zero_one_or_two(self):
        for value in (3, -1, True, "1"):
            with self.subTest(value=value), self.assertRaises(rd.DesignationError):
                rd.select(families=FAMILIES, required=value, authors=[], candidates=[])

    def test_project_restricts_to_the_team(self):
        rows = [row("am-1", team="ameesh", harness="claude", session="claude"),
                row("nx-1", team="nexlink", harness="claude", session="claude"),
                row("nx-2", team="nexlink", harness="codex", session="codex")]
        self.assertEqual([c.name for c in candidates(*rows, project="nexlink")],
                         ["nx-1", "nx-2"])
        self.assertEqual(len(candidates(*rows, project="ameesh")), 1)
        self.assertEqual(len(candidates(*rows, project="")), 3)


class MultiFamilyAuthorTest(unittest.TestCase):
    """#3584 — un auteur prouvé sous plusieurs familles exclut chacune d'elles.

    Un agent codex gelé pendant la pause Codex tourne en repli deepseek : ses
    gels antérieurs prouvent codex, sa session de repli deepseek. La lecture
    rend `{family: "", families: ["codex", "deepseek"]}` : ce n'est ni une
    famille inconnue, ni une ambiguïté à refuser.
    """

    MULTI = {"name": "cx1", "family": "",
             "families": ["codex", "deepseek"]}

    def test_the_proven_set_is_not_an_unknown_family(self):
        self.assertEqual(rd.families_of([self.MULTI]),
                         ({"cx1"}, {"codex", "deepseek"}))
        self.assertEqual(rd.unknown_families([self.MULTI]), [])
        self.assertEqual(rd.unknown_families([{"name": "cx1", "family": ""}]),
                         ["cx1"])

    def test_a_test_only_corrector_is_never_unknown(self):
        # #3658 : un correcteur test_only n'interdit que son nom (#3518),
        # dans `unknown_families` comme dans `families_of`.
        rows = [{"name": "impl-06", "family": "deepseek"},
                {"name": "dev4", "family": "claude", "test_only": True}]
        self.assertEqual(rd.unknown_families(rows), [])
        self.assertEqual(rd.families_of(rows), ({"impl-06", "dev4"}, {"deepseek"}))

    def test_one_opinion_from_an_outside_family_is_designated(self):
        picked = rd.select(
            families=FAMILIES, required=1, authors=[self.MULTI],
            candidates=candidates(
                row("claude-one", harness="claude", session="claude"),
                row("codex-one", harness="codex", session="codex"),
                row("deepseek-one", harness="deepseek", session="deepseek")))
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])

    def test_two_opinions_name_both_excluded_families_not_unknown(self):
        picked = rd.select(
            families=FAMILIES, required=2, authors=[self.MULTI],
            candidates=candidates(
                row("claude-one", harness="claude", session="claude"),
                row("codex-one", harness="codex", session="codex"),
                row("deepseek-one", harness="deepseek", session="deepseek")))
        self.assertEqual(picked["state"], rd.WAITING)
        # #3596 : le siège de la famille disponible est pourvu dès le gel ;
        # l'autre famille reste à couvrir, et le message la nomme par exclusion,
        # jamais comme une famille inconnue.
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertEqual(picked["missing"], 1)
        self.assertIn("codex", picked["why"])
        self.assertIn("deepseek", picked["why"])
        self.assertNotIn("unknown", picked["why"])

    def test_a_manual_reviewer_of_an_excluded_family_does_not_cover_a_seat(self):
        seats = rd.manual_seats(
            [{"name": "manual-codex", "family": "codex"},
             {"name": "manual-claude", "family": "claude"}],
            authors=[self.MULTI], families=FAMILIES)
        self.assertEqual(seats, [{"name": "manual-claude", "family": "claude"}])

    def test_an_author_without_any_proof_still_waits(self):
        picked = rd.select(
            families=FAMILIES, required=1,
            authors=[{"name": "cx1", "family": ""}],
            candidates=candidates(
                row("claude-one", harness="claude", session="claude")))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertIn("author family unknown", picked["why"])


class NexlinkPolicyTest(unittest.TestCase):
    """La politique est par projet : Nexlink a ses propres règles (PR85)."""

    RULES = {"rules": [{"paths": ["**"], "reviews": 1},
                       {"paths": ["supabase/**", "scripts/migrate*.sh"], "reviews": 2}]}

    def test_nexlink_counts_and_selection(self):
        rules = rd.rules_of(self.RULES)
        self.assertEqual(rd.required_count(rules, ["app/page.tsx"]), 1)
        self.assertEqual(rd.required_count(rules, ["supabase/migrations/1.sql"]), 2)
        self.assertEqual(rd.required_count(rules, ["scripts/migrate-up.sh"]), 2)
        rows = [row("nx-deep", team="nexlink", harness="deepseek", session="deepseek"),
                row("nx-claude", team="nexlink", harness="claude", session="claude"),
                row("nx-codex", team="nexlink", harness="codex", session="codex"),
                row("am-claude", team="ameesh", harness="claude", session="claude")]
        pool = candidates(*rows, project="nexlink")
        picked = rd.select(families=FAMILIES, required=2,
                           authors=[{"name": "nx-codex", "family": "codex"}],
                           candidates=pool)
        self.assertEqual({r["family"] for r in picked["reviewers"]},
                         {"claude", "deepseek"})
        self.assertNotIn("nx-codex", [r["name"] for r in picked["reviewers"]])


class MessageTest(unittest.TestCase):
    """Le courrier remet la tête exacte ; la capacité manquante est notifiée."""

    def test_reviewer_message_hands_the_exact_head(self):
        text = rd.reviewer_message(
            LOT, freeze={"id": 42, "sha": SHA_A}, required=2, rank=2,
            reviewer={"name": "rev-claude", "family": "claude"},
            authors=[{"name": "mty-amh-impl-02", "family": "deepseek"}],
            base=SHA_B, risk="sensitive", policy_ref="canon@deadbeef")
        self.assertIn(SHA_A, text)
        self.assertIn("#1617", text)
        self.assertIn("frozen milestone #42", text)
        self.assertIn("ameesh work milestone 1617 verdict ok|blocked --sha " + SHA_A, text)
        self.assertIn("git diff %s..%s" % (SHA_B[:12], SHA_A[:12]), text)
        self.assertIn("canon@deadbeef", text)
        self.assertIn("any correction makes you an author of the new head", text)
        # #1534 N6 : le courrier de désignation couvre la règle home#81 entière
        self.assertIn("~50 lines", text)
        self.assertIn("second family", text)
        self.assertIn("ameesh provenance show", text)

    def test_reviewer_message_refuses_a_fix_that_leaves_too_few_families(self):
        # #2670, cas #2664 : deux avis requis, deux familles éligibles ; une
        # correction qui fait du relecteur un auteur laisserait une seule
        # famille et rendrait la porte insatisfaisable.
        text = rd.reviewer_message(
            LOT, freeze={"id": 42, "sha": SHA_A}, required=2, rank=2,
            reviewer={"name": "rev-codex", "family": "codex"},
            authors=[{"name": "mty-amh-impl-02", "family": "deepseek"}],
            eligible_families=2)
        self.assertIn("do not push this correction", text)
        self.assertIn("1 independent family", text)
        self.assertIn("2 required opinion", text)
        self.assertIn("#2670", text)

    def test_reviewer_message_allows_a_fix_with_a_spare_family(self):
        # Trois familles pour deux avis : la correction laisse deux familles.
        text = rd.reviewer_message(
            LOT, freeze={"id": 42, "sha": SHA_A}, required=2, rank=2,
            reviewer={"name": "rev-codex", "family": "codex"},
            authors=[{"name": "mty-amh-impl-02", "family": "deepseek"}],
            eligible_families=3)
        self.assertIn("is receivable", text)
        self.assertIn("2 independent families", text)
        self.assertIn("#2670", text)

    def test_reviewer_message_states_the_rule_without_a_count(self):
        # Appel direct sans le compte : la règle est dite, jamais devinée.
        text = rd.reviewer_message(
            LOT, freeze={"id": 42, "sha": SHA_A}, required=2, rank=2,
            reviewer={"name": "rev-codex", "family": "codex"},
            authors=[{"name": "mty-amh-impl-02", "family": "deepseek"}])
        self.assertIn("fewer independent families than required opinions", text)
        self.assertIn("#2670", text)

    def test_manual_reviewer_message_carries_the_family_rule(self):
        text = rd.manual_reviewer_message({"id": 7, "title": "t"})
        self.assertIn("fewer independent families", text)
        self.assertIn("#2670", text)
        self.assertIn("#2888", text)

    def test_capacity_message_refuses_implicit_admission(self):
        text = rd.capacity_message(LOT, freeze={"id": 42, "sha": SHA_A}, required=2,
                                   missing=1, why="1 missing family")
        self.assertIn("No implicit admission", text)
        self.assertIn("missing: 1", text)
        self.assertIn("frozen milestone #42", text)


class ApplyTest(unittest.TestCase):
    """Le raccord au gel : une seule écriture, rejouable, jamais une vieille tête."""

    def setUp(self):
        self.port = FakePort({1617: [{"id": 1, "sha": SHA_A}]})
        self.authors = [{"name": "mty-amh-impl-02", "family": "deepseek"}]
        self.pool = candidates(row("claude-one", harness="claude", session="claude"),
                               row("codex-one", harness="codex", session="codex"))

    def apply(self, **kw):
        args = dict(policy=RULES, paths=["src/ameesh/db.py"], port=self.port,
                    authors=self.authors, candidates=self.pool, risk="sensitive",
                    base=SHA_C, policy_ref="canon@deadbeef", notify="mesh-design")
        args.update(kw)
        if "freeze_sha" not in args:
            # l'appelant collecte ses entrées sur le dernier gel qu'il voit
            latest = self.port.latest_freeze(LOT["id"]) or {}
            args["freeze_sha"] = latest.get("sha") or SHA_A
        return rd.apply(LOT, **dict({"families": FAMILIES}, **args))

    def test_a_multi_family_author_designates_outside_both_families(self):
        # #3584 : l'auteur a gelé sous codex puis tourné en repli deepseek ;
        # la désignation exclut les deux familles et prend claude.
        self.authors = [{"name": "cx1", "family": "",
                         "families": ["codex", "deepseek"]}]
        self.pool = candidates(
            row("claude-one", harness="claude", session="claude"),
            row("codex-one", harness="codex", session="codex"),
            row("deepseek-one", harness="deepseek", session="deepseek"))
        out = self.apply(
            policy={"rules": [{"paths": ["src/ameesh/**"], "reviews": 1}]},
            required=1)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["claude-one"])
        self.assertEqual(out["authors"][0]["families"], ["codex", "deepseek"])

    def test_an_abbreviated_freeze_is_the_same_head(self):
        # #1775 N1 : un jalon frozen peut porter un SHA abrégé alors que le
        # collecteur a le SHA complet — ce n'est pas un gel qui a bougé.
        self.port = FakePort({1617: [{"id": 6281, "sha": SHA_A[:8]}]})
        out = self.apply(freeze_sha=SHA_A)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["freeze"], {"id": 6281, "sha": SHA_A[:8]})

    def test_a_different_freeze_still_moved(self):
        self.port = FakePort({1617: [{"id": 6281, "sha": SHA_A}]})
        with self.assertRaises(rd.FreezeMoved):
            self.apply(freeze_sha=SHA_B)
        self.assertEqual(self.port.commits, [])

    def test_designates_two_reviewers_and_hands_them_the_frozen_head(self):
        out = self.apply()
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["required"], 2)
        self.assertEqual(out["freeze"], {"id": 1, "sha": SHA_A})
        self.assertEqual(len(out["messages"]), 2)
        self.assertEqual({m["to"] for m in out["messages"]}, {"claude-one", "codex-one"})
        for _to, body in self.port.messages:
            self.assertIn(SHA_A, body)
            # #2670 : deux familles pour deux avis, une correction qui fait du
            # relecteur un auteur laisserait la porte insatisfaisable.
            self.assertIn("do not push this correction", body)
            self.assertIn("1 independent family", body)
        self.assertEqual(len(self.port.commits), 1)

    def test_the_required_override_reads_the_critical_floor(self):
        # #2627 : le canon ne déclare qu'un avis, la porte en exige deux. Le
        # moteur reçoit le besoin compilé et désigne les deux familles.
        one = {"rules": [{"paths": ["src/ameesh/**"], "reviews": 1}]}
        out = self.apply(policy=one, required=2)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["required"], 2)
        self.assertEqual(len(out["reviewers"]), 2)

    def test_manual_reviewers_are_completed_by_only_the_missing_seat(self):
        out = self.apply(already=[{"name": "manual-claude", "family": "claude"}])
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["codex-one"])
        self.assertEqual(len(out["messages"]), 1)
        self.assertEqual(out["messages"][0]["to"], "codex-one")
        self.assertIn("review 2/2", out["messages"][0]["body"])

    def test_manual_reviewers_covering_the_need_write_nothing(self):
        before = len(self.port.commits)
        out = self.apply(already=[{"name": "manual-claude", "family": "claude"},
                                  {"name": "manual-codex", "family": "codex"}])
        self.assertEqual(out["state"], "manual")
        self.assertEqual(out["reviewers"], [])
        self.assertEqual(len(self.port.commits), before)
        self.assertEqual(self.port.messages, [])

    def test_the_same_freeze_replayed_sends_no_second_mail(self):
        first = self.apply()
        again = self.apply()
        self.assertTrue(again.get("replayed"))
        self.assertEqual(again["reviewers"], first["reviewers"])
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(len(self.port.messages), 2)

    def test_a_new_freeze_supersedes_the_stale_designation(self):
        self.apply()
        self.port.freezes[1617].append({"id": 2, "sha": SHA_B})
        out = self.apply()
        self.assertEqual(out["freeze"]["id"], 2)
        self.assertEqual(out["freeze"]["sha"], SHA_B)
        self.assertFalse(out.get("replayed"))
        self.assertEqual(len(self.port.commits), 2)
        for _to, body in self.port.messages[2:]:
            self.assertIn(SHA_B, body)
            self.assertNotIn(SHA_A, body)
        # Le Port re-contrôle le dernier gel sous verrou : une écriture sur
        # l'ancien gel est refusée, jamais un retour à une ancienne tête.
        with self.assertRaises(rd.DesignationError):
            self.port.commit(1617, freeze={"id": 1, "sha": SHA_A}, required=2,
                             state=rd.DESIGNATED, reviewers=[], missing=0, why="",
                             authors=self.authors)
        self.assertEqual(self.port.stale_refusals, 1)

    def test_a_freeze_that_moves_during_the_write_refuses_without_writing(self):
        self.port = MovingPort({1617: [{"id": 1, "sha": SHA_A}]},
                               [{"id": 2, "sha": SHA_B}])
        with self.assertRaises(rd.FreezeMoved):
            self.apply()
        self.assertEqual(self.port.stale_refusals, 1)
        self.assertEqual(self.port.commits, [])
        self.assertEqual(self.port.messages, [])
        self.assertEqual(self.port.records, {})

    def test_inputs_collected_for_an_older_freeze_are_refused_without_writing(self):
        # un gel arrive entre la collecte des entrées (sur A) et l'appel :
        # apply ne désigne pas B avec les chemins/auteurs de A (B1 cx2)
        self.port = FakePort({1617: [{"id": 1, "sha": SHA_A}, {"id": 2, "sha": SHA_B}]})
        with self.assertRaises(rd.FreezeMoved):
            # reproducteur cx2 : entrées de A (un chemin à un avis, auteur
            # DeepSeek) alors que B est déjà le dernier gel
            self.apply(freeze_sha=SHA_A, paths=["src/ameesh/work.py"],
                       authors=[{"name": "mty-amh-impl-02", "family": "deepseek"}])
        self.assertEqual(self.port.commits, [])
        self.assertEqual(self.port.messages, [])
        with self.assertRaises(rd.DesignationError):
            self.apply(freeze_sha="")
        self.assertEqual(self.apply(freeze_sha=SHA_B.upper())["freeze"], {"id": 2, "sha": SHA_B})

    def test_the_new_head_is_designated_only_on_a_fresh_call(self):
        self.port = MovingPort({1617: [{"id": 1, "sha": SHA_A}]},
                               [{"id": 2, "sha": SHA_B}])
        with self.assertRaises(rd.FreezeMoved):
            self.apply()
        # les entrées du nouveau gel sont fournies par l'appelant, pas reprises
        out = self.apply(policy_ref="canon@newhead")
        self.assertEqual(out["freeze"], {"id": 2, "sha": SHA_B})
        self.assertEqual(self.port.commits[-1]["policy_ref"], "canon@newhead")
        self.assertEqual(len(self.port.messages), 2)
        for _to, body in self.port.messages:
            self.assertIn(SHA_B, body)
            self.assertNotIn(SHA_A, body)

    def test_an_unfrozen_or_invalid_head_is_refused(self):
        for freezes in ({}, {1617: [{"id": 1, "sha": "main"}]},
                        {1617: [{"id": 1, "sha": ""}]}):
            with self.subTest(freezes=freezes):
                self.port = FakePort(freezes)
                with self.assertRaises(rd.DesignationError):
                    self.apply()

    def test_an_author_without_a_name_is_refused(self):
        with self.assertRaises(rd.DesignationError):
            self.apply(authors=[{"family": "deepseek"}])

    def test_waiting_is_recorded_once_with_a_capacity_notice(self):
        self.port = FakePort({1617: [{"id": 1, "sha": SHA_A}]})
        pool = candidates(row("claude-one", harness="claude", session="claude"),
                          row("claude-two", harness="claude", session="claude"))
        out = self.apply(candidates=pool)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(out["missing"], 1)
        # #3596 : la famille disponible est pourvue tout de suite ; le manque
        # est notifié au pilote, et le relecteur reçoit sa demande.
        self.assertEqual([r["name"] for r in out["reviewers"]], ["claude-one"])
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(self.port.messages[0][0], "mesh-design")
        self.assertIn("No implicit admission", self.port.messages[0][1])
        self.assertIn("Designation at freeze incomplete", self.port.messages[0][1])
        self.assertEqual(self.port.messages[1][0], "claude-one")
        again = self.apply(candidates=pool)
        self.assertTrue(again.get("replayed"))
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(len(self.port.messages), 2)

    def test_light_lot_designates_nobody(self):
        out = self.apply(paths=["docs/guide.md"], risk="light")
        self.assertEqual(out["state"], rd.NONE)
        self.assertEqual(self.port.commits, [])
        self.assertEqual(self.port.messages, [])

    def test_the_policy_reader_light_floor_designates_one_reviewer(self):
        # #2642 : le plancher déclaré au canon (light_reviews: 1) vient du
        # lecteur de politique, qui le passe à `apply` ; une règle absente ne
        # doit plus rendre un lot léger sans avis.
        out = self.apply(paths=["docs/guide.md"], risk="light", required=1)
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["required"], 1)
        self.assertEqual(len(out["messages"]), 1)
        self.assertNotEqual(out["messages"][0]["family"], "deepseek")
        self.assertEqual(len(self.port.commits), 1)

    def test_an_empty_diff_never_asks_for_the_light_floor(self):
        out = self.apply(paths=[], risk="light", required=1)
        self.assertEqual(out["state"], rd.NONE)
        self.assertEqual(out["required"], 0)

    def test_non_light_without_a_rule_is_refused(self):
        with self.assertRaises(rd.DesignationError):
            self.apply(paths=["docs/guide.md"], risk="sensitive")
        self.assertEqual(self.port.commits, [])

    def test_unknown_author_provenance_waits_instead_of_guessing(self):
        # Une preuve d'auteur (#1465) manquante : l'indépendance ne se prouve
        # pas, donc on attend — jamais un relecteur désigné au hasard.
        out = self.apply(unknown_authors=["deadbeef", "cafebabe"])
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(out["reviewers"], [])
        self.assertEqual(out["missing"], 2)
        self.assertIn("provenance unknown", out["why"])
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(len(self.port.messages), 1)
        self.assertIn("provenance", self.port.messages[0][1])
        # La preuve arrive : le même gel peut alors désigner, sans seconde
        # notification ; la désignation devient la référence.
        again = self.apply()
        self.assertEqual(again["state"], rd.DESIGNATED)
        self.assertFalse(again.get("replayed"))
        self.assertEqual(len(self.port.messages), 3)  # 1 attente + 2 courriers
        third = self.apply()
        self.assertTrue(third.get("replayed"))
        self.assertEqual(len(self.port.messages), 3)

    def test_capacity_coming_back_designates_on_the_same_freeze(self):
        pool = candidates(row("claude-one", harness="claude", session="claude"),
                          row("claude-two", harness="claude", session="claude"))
        first = self.apply(candidates=pool)
        self.assertEqual(first["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in first["reviewers"]], ["claude-one"])
        self.assertEqual(len(self.port.messages), 2)  # avis de manque + demande claude
        second = self.apply(candidates=self.pool)  # claude + codex disponibles
        self.assertEqual(second["state"], rd.DESIGNATED)
        self.assertFalse(second.get("replayed"))
        # #3596 : claude garde son siège et ne reçoit pas de seconde demande ;
        # codex est ajouté seul.
        self.assertEqual([r["name"] for r in second["reviewers"]],
                         ["claude-one", "codex-one"])
        self.assertEqual(len(self.port.messages), 3)
        self.assertEqual(self.port.messages[2][0], "codex-one")
        self.assertEqual([to for to, _body in self.port.messages].count("claude-one"), 1)
        third = self.apply(candidates=self.pool)
        self.assertTrue(third.get("replayed"))
        self.assertEqual(len(self.port.messages), 3)

    def test_no_eligible_reviewer_never_becomes_an_implicit_admission(self):
        authors = [{"name": "a", "family": "deepseek"}, {"name": "b", "family": "codex"},
                   {"name": "c", "family": "claude"}]
        out = self.apply(authors=authors)
        self.assertEqual(out["state"], rd.WAITING)
        self.assertEqual(out["reviewers"], [])
        self.assertEqual(out["required"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

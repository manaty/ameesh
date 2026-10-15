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
        mode="execute", text=""):
    return {"name": name, "team": team, "harness": harness, "session_harness": session,
            "status": status, "mode": mode, "status_text": text}


def candidates(*rows, project="ameesh", loads=None):
    return rd.candidates_from_registry(rows, project=project, loads=loads)


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
               policy_ref="", notify="", messages=()):
        latest = self.latest_freeze(lot)
        if not latest or int(latest["id"]) != int(freeze["id"]):
            self.stale_refusals += 1
            raise rd.StaleFreeze("freeze is no longer the latest one")
        out = {"state": state, "required": required,
               "reviewers": [dict(r) for r in reviewers], "missing": missing, "why": why,
               "freeze": dict(freeze), "authors": [dict(a) for a in authors],
               "policy_ref": policy_ref, "notify": notify,
               "messages": [dict(m) for m in messages]}
        self.records[(int(lot), int(freeze["id"]))] = out
        self.commits.append(out)
        self.messages.extend((m.get("to"), m.get("body")) for m in messages)
        return dict(out)


class MovingPort(FakePort):
    """Le gel bouge pendant l'écriture : le commit refuse, puis le moteur relit."""

    def __init__(self, freezes, moves):
        super().__init__(freezes)
        self.moves = list(moves)

    def commit(self, lot, **kw):
        if self.moves:
            self.freezes[int(lot)].append(self.moves.pop(0))
            self.stale_refusals += 1
            raise rd.StaleFreeze("freeze moved during the write")
        return super().commit(lot, **kw)


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
        picked = rd.select(
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
        one = rd.select(required=1, authors=authors, candidates=pool)
        self.assertEqual(one["state"], rd.DESIGNATED)
        self.assertEqual([r["name"] for r in one["reviewers"]], ["codex-one"])
        # Deux avis hors claude et hors deepseek : la seule famille restante ne suffit pas.
        two = rd.select(required=2, authors=authors, candidates=pool)
        self.assertEqual(two["state"], rd.WAITING)
        self.assertEqual(two["missing"], 1)
        self.assertEqual(two["reviewers"], [])

    def test_author_with_unknown_family_waits_instead_of_guessing(self):
        # codex-two est peut-être de la famille réelle de l'auteur : on attend.
        for family in ("", "unknown"):
            picked = rd.select(
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
        picked = rd.select(required=1,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-busy"])

    def test_load_orders_the_choice_and_is_deterministic(self):
        pool = candidates(
            row("claude-heavy", harness="claude", session="claude"),
            row("claude-light", harness="claude", session="claude"),
            row("codex-light", harness="codex", session="codex"),
            loads={"claude-heavy": 7, "claude-light": 1, "codex-light": 1})
        picked = rd.select(required=1,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-light"])
        two = rd.select(required=2,
                        authors=[{"name": "impl-02", "family": "deepseek"}],
                        candidates=pool)
        self.assertEqual([r["name"] for r in two["reviewers"]], ["claude-light", "codex-light"])
        # Un seul candidat par famille (le moins chargé) : jamais deux fois la même.
        self.assertEqual(len({r["family"] for r in two["reviewers"]}), 2)

    def test_family_comes_from_the_session_never_from_the_name(self):
        pool = candidates(
            row("codex3", harness="deepseek", session="claude"),
            row("claude-bis", harness="other", session=""),
            row("veille", harness="deepseek", session="", status="running", team="ameesh"),
        )
        picked = rd.select(required=1,
                           authors=[{"name": "impl-02", "family": "deepseek"}],
                           candidates=pool)
        # `codex3` tourne sous claude (L242b) : sa famille est claude, pas son nom.
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["codex3"])
        self.assertEqual(picked["reviewers"][0]["family"], "claude")

    def test_waiting_is_explicit(self):
        picked = rd.select(
            required=2,
            authors=[{"name": "impl-02", "family": "deepseek"}],
            candidates=candidates(row("claude-one", harness="claude", session="claude"),
                                  row("claude-two", harness="claude", session="claude")))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual(picked["missing"], 1)
        self.assertIn("claude", picked["why"])
        self.assertIn("deepseek", picked["why"])

    def test_required_must_be_zero_one_or_two(self):
        for value in (3, -1, True, "1"):
            with self.subTest(value=value), self.assertRaises(rd.DesignationError):
                rd.select(required=value, authors=[], candidates=[])

    def test_project_restricts_to_the_team(self):
        rows = [row("am-1", team="ameesh", harness="claude", session="claude"),
                row("nx-1", team="nexlink", harness="claude", session="claude"),
                row("nx-2", team="nexlink", harness="codex", session="codex")]
        self.assertEqual([c.name for c in candidates(*rows, project="nexlink")],
                         ["nx-1", "nx-2"])
        self.assertEqual(len(candidates(*rows, project="ameesh")), 1)
        self.assertEqual(len(candidates(*rows, project="")), 3)


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
        picked = rd.select(required=2,
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
        self.assertIn("frozen #42", text)
        self.assertIn("ameesh work milestone 1617 verdict ok|blocked --sha " + SHA_A, text)
        self.assertIn("git diff %s..%s" % (SHA_B[:12], SHA_A[:12]), text)
        self.assertIn("canon@deadbeef", text)
        self.assertIn("tu deviens alors auteur", text)

    def test_capacity_message_refuses_implicit_admission(self):
        text = rd.capacity_message(LOT, freeze={"id": 42, "sha": SHA_A}, required=2,
                                   missing=1, why="1 missing family")
        self.assertIn("Aucune admission implicite", text)
        self.assertIn("manquants : 1", text)
        self.assertIn("frozen #42", text)


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
        return rd.apply(LOT, **args)

    def test_designates_two_reviewers_and_hands_them_the_frozen_head(self):
        out = self.apply()
        self.assertEqual(out["state"], rd.DESIGNATED)
        self.assertEqual(out["required"], 2)
        self.assertEqual(out["freeze"], {"id": 1, "sha": SHA_A})
        self.assertEqual(len(out["messages"]), 2)
        self.assertEqual({m["to"] for m in out["messages"]}, {"claude-one", "codex-one"})
        for _to, body in self.port.messages:
            self.assertIn(SHA_A, body)
        self.assertEqual(len(self.port.commits), 1)

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

    def test_a_freeze_that_moves_during_the_write_restarts_on_the_new_head(self):
        self.port = MovingPort({1617: [{"id": 1, "sha": SHA_A}]},
                               [{"id": 2, "sha": SHA_B}])
        out = self.apply()
        self.assertEqual(out["freeze"], {"id": 2, "sha": SHA_B})
        self.assertEqual(self.port.stale_refusals, 1)
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(len(self.port.messages), 2)
        for _to, body in self.port.messages:
            self.assertIn(SHA_B, body)
            self.assertNotIn(SHA_A, body)

    def test_repeated_freeze_moves_fail_the_gesture_instead_of_looping(self):
        moves = [{"id": n, "sha": "%040x" % n} for n in (2, 3, 4)]
        self.port = MovingPort({1617: [{"id": 1, "sha": SHA_A}]}, moves)
        with self.assertRaises(rd.DesignationError):
            self.apply()
        self.assertEqual(self.port.commits, [])
        self.assertEqual(self.port.messages, [])
        self.assertEqual(self.port.stale_refusals, 3)

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
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(self.port.messages[0][0], "mesh-design")
        self.assertIn("Aucune admission implicite", self.port.messages[0][1])
        again = self.apply(candidates=pool)
        self.assertTrue(again.get("replayed"))
        self.assertEqual(len(self.port.commits), 1)
        self.assertEqual(len(self.port.messages), 1)

    def test_light_lot_designates_nobody(self):
        out = self.apply(paths=["docs/guide.md"], risk="light")
        self.assertEqual(out["state"], rd.NONE)
        self.assertEqual(self.port.commits, [])
        self.assertEqual(self.port.messages, [])

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
        self.assertIn("preuve", self.port.messages[0][1])
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
        self.assertEqual(len(self.port.messages), 1)
        second = self.apply(candidates=self.pool)  # claude + codex disponibles
        self.assertEqual(second["state"], rd.DESIGNATED)
        self.assertFalse(second.get("replayed"))
        self.assertEqual(len(self.port.messages), 3)
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

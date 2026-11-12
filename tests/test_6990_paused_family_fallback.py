# SPDX-License-Identifier: AGPL-3.0-only
"""#6990 — repli de la porte quand une famille requise est en pause budget.

Décision owner #213032 option a (signée 11/11 21:02Z) : quand la famille qui
manque est **entièrement** en pause budget et que sa reprise est à plus de
`reviews.paused_family_min_hours` heures, la porte admet un lot sur **un** avis
d'une autre famille que l'auteur, plus la CI complète du paquet ; la relecture
de la famille en pause est due après fusion (§2), et elle est la seule à
pouvoir clore la mission.

Le repli ne s'ouvre que si le canon le déclare **et** si toutes les règles à
deux avis du diff l'accordent (`paused_family_reviews: 1`). CI, SQL, secrets,
identité, paiement et prod réelle gardent leurs deux avis avant fusion.
"""
from __future__ import annotations

import contextlib
import types
import unittest
from unittest import mock

from ameesh import (debit, lot_class, mechanics, merge_queue, packet_reviews,
                    review_designation, review_family_paused, review_lot,
                    storage, work)

from .test_4779_reviewer_profile import agent_row
from .test_1104_review_lot_hors_file import _Witness
from .test_l539_review_lot import FakeOps

HEAD = "a" * 40
T = 1792812000.0
H = 3600.0
FAMILIES = ("claude", "codex", "deepseek")
MANAGER = """
schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""


def mechanics_text(paused: int | None = 12, extra: str = "") -> str:
    text = MANAGER.rstrip("\n") + "\n"
    if paused is not None:
        text = text.replace(
            "  distinct_families: true\n",
            "  distinct_families: true\n  paused_family_min_hours: %s\n" % paused)
    return text + extra


def federation(rules):
    return {"review_policies": {
        "risk_classes": {"default": "normal", "projects": {"ameesh": {
            "default": "light", "rules": []}}},
        "train_reviews": {"schema": "ameesh-train-reviews-policy/1",
                          "projects": {"ameesh": {"rules": list(rules)}}},
    }}


GATE_RULE = {"paths": ["src/ameesh/merge_queue.py", "src/ameesh/review*.py"],
             "reviews": 2, "paused_family_reviews": 1}


def canon(rules, sources=()):
    return types.SimpleNamespace(root="fixture", sources=list(sources),
                                 federation=federation(rules))


class MechanicsReaderTest(unittest.TestCase):
    """Le lecteur strict accepte les deux clés, et ne les invente jamais."""

    def test_the_global_threshold_is_optional_and_typed(self):
        self.assertEqual(
            mechanics.compile_document(mechanics_text()).paused_family_min_hours, 12)
        self.assertEqual(
            mechanics.compile_document(mechanics_text(paused=None)).paused_family_min_hours,
            0)
        for bad in (mechanics_text(-1), mechanics_text('"12"')):
            with self.subTest(bad=bad), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(bad)

    def test_the_document_emits_the_key_only_when_declared(self):
        self.assertNotIn("paused_family_min_hours",
                         mechanics.compile_document(mechanics_text(paused=None)).to_dict(
                             )["reviews"])
        self.assertEqual(
            mechanics.compile_document(mechanics_text()).to_dict(
                )["reviews"]["paused_family_min_hours"], 12)

    def test_the_rule_reader_accepts_the_fallback_key(self):
        policy, rules = mechanics._review_policy(
            federation([GATE_RULE]), "ameesh", HEAD)
        self.assertEqual(rules[0].reviews, 2)
        self.assertEqual(rules[0].paused_family_reviews, 1)
        (rule,) = rules
        self.assertEqual(rule.paused_family_reviews, 1)
        for bad in ({"paths": ["a"], "reviews": 1, "paused_family_reviews": 2},
                    {"paths": ["a"], "reviews": 2, "paused_family_reviews": 2},
                    {"paths": ["a"], "reviews": 2, "paused_family_reviews": 3},
                    {"paths": ["a"], "reviews": 2, "paused_family_reviews": True},
                    {"paths": ["a"], "reviews": 2, "other": 1}):
            with self.subTest(bad=bad), self.assertRaises(mechanics.MechanicsError):
                mechanics._review_policy(federation([bad]), "ameesh", HEAD)

    def test_designation_reader_accepts_the_key_without_applying_it(self):
        declaration = federation([GATE_RULE])["review_policies"]["train_reviews"][
            "projects"]["ameesh"]
        rules = review_designation.rules_of(declaration)
        self.assertEqual([(rule.paths, rule.reviews) for rule in rules],
                         [(("src/ameesh/merge_queue.py", "src/ameesh/review*.py"), 2)])
        for count in (2, 3):
            bad = federation([{"paths": ["a"], "reviews": 2,
                               "paused_family_reviews": count}])
            with self.subTest(count=count), \
                    self.assertRaises(review_designation.DesignationError):
                review_designation.rules_of(
                    bad["review_policies"]["train_reviews"]["projects"]["ameesh"])


class ThresholdReaderTest(unittest.TestCase):
    """`lot_class` n'ouvre le seuil que si tous les canons lisibles l'accordent."""

    def canon(self, count, *, prefix=""):
        sources = [types.SimpleNamespace(mode="git", repo="/r",
                                         commit="c%d" % index, prefix=prefix)
                   for index in range(count)]
        return types.SimpleNamespace(root="fixture", sources=sources,
                                     federation={})

    def read(self, values):
        pending = iter(values)

        def declared(repo, commit):
            value = next(pending)
            if isinstance(value, Exception):
                raise value
            return value
        with mock.patch.object(mechanics, "paused_family_min_hours_at",
                               side_effect=declared):
            return lot_class._paused_min_hours([self.canon(len(values))])

    def test_the_longest_declared_delay_wins(self):
        self.assertEqual(self.read([6, 12]), 12)

    def test_a_canon_without_the_key_closes_the_fallback(self):
        self.assertEqual(self.read([12, 0]), 0)

    def test_an_unreadable_canon_closes_the_fallback(self):
        self.assertEqual(self.read([12, mechanics.MechanicsAbsent("absent")]), 0)
        self.assertEqual(self.read([12, mechanics.MechanicsError("bad")]), 0)

    def test_no_readable_canon_opens_nothing(self):
        self.assertEqual(self.read([]), 0)
        self.assertEqual(lot_class._paused_min_hours(
            [self.canon(0), self.canon(0)]), 0)

    def test_a_prefixed_source_is_not_a_global_document(self):
        with mock.patch.object(mechanics, "paused_family_min_hours_at") as reader:
            self.assertEqual(lot_class._paused_min_hours(
                [types.SimpleNamespace(root="fixture", federation={}, sources=[
                    types.SimpleNamespace(mode="git", repo="/r", commit="c",
                                          prefix="projet/")])]), 0)
        reader.assert_not_called()


class EligibilityTest(unittest.TestCase):
    """`lot_class` ne porte le repli que si toutes les règles l'accordent."""

    def class_of(self, rules, paths, *, threshold=12):
        with mock.patch.object(lot_class, "_light_reviews",
                               return_value=(0, "")), \
                mock.patch.object(lot_class, "_paused_min_hours",
                                  return_value=threshold):
            return lot_class.class_of_paths([canon(rules)], "ameesh", paths)

    def eligible(self, rules, paths):
        return self.class_of(rules, paths, threshold=12)

    def test_a_gate_only_lot_is_eligible(self):
        klass = self.eligible([GATE_RULE], ["src/ameesh/merge_queue.py",
                                            "src/ameesh/review_lot.py"])
        self.assertEqual(klass["paused_family_min_hours"], 12)
        self.assertEqual(klass["paused_family_reviews"], 1)

    def test_a_ci_or_sql_path_keeps_the_two_reviews(self):
        mixed = [GATE_RULE, {"paths": [".github/**"], "reviews": 2}]
        for rules, paths in (
                ([{"paths": [".github/**"], "reviews": 2}],
                 [".github/workflows/tests.yml"]),
                ([{"paths": ["src/ameesh/migrations/**"], "reviews": 2}],
                 ["src/ameesh/migrations/0060_x.sql"]),
                (mixed, ["src/ameesh/merge_queue.py",
                         ".github/workflows/tests.yml"]),
                ([GATE_RULE], ["src/ameesh/merge_queue.py",
                               "src/ameesh/migrations/0060_x.sql"])):
            with self.subTest(paths=paths):
                klass = self.eligible(rules, paths)
                self.assertEqual(klass["paused_family_reviews"], 0)

    def test_a_rule_without_the_key_refuses_the_whole_diff(self):
        other = {"paths": ["src/ameesh/merge_queue.py"], "reviews": 2}
        klass = self.eligible([GATE_RULE, other],
                              ["src/ameesh/merge_queue.py"])
        self.assertEqual(klass["paused_family_reviews"], 0)

    def test_a_rule_that_admits_both_reviews_is_a_reading_doubt(self):
        klass = self.eligible([{"paths": ["src/ameesh/merge_queue.py"],
                                "reviews": 2, "paused_family_reviews": 2}],
                              ["src/ameesh/merge_queue.py"])
        self.assertTrue(klass["doubt"])
        self.assertEqual(klass.get("paused_family_reviews") or 0, 0)

    def test_no_declared_threshold_never_opens_the_fallback(self):
        klass = self.class_of([GATE_RULE], ["src/ameesh/merge_queue.py"],
                              threshold=0)
        self.assertEqual(klass["paused_family_min_hours"], 0)
        self.assertEqual(klass["paused_family_reviews"], 0)


class PausedFamilyFallbackTest(unittest.TestCase):
    """La preuve de repli : étroite, lisible, jamais supposée."""

    def designation(self, **extra):
        base = {"freeze": {"sha": HEAD, "at_ts": T - 4 * H},
                "required": 2, "acquired": 1, "shortfall": 1,
                "quorum": None, "unknown_authors": [],
                "author_families": ["deepseek"], "acquired_families": ["claude"]}
        base.update(extra)
        return base

    def registry(self, *, deadline=T + 30 * H, extra=()):
        rows = [agent_row("author", harness="deepseek"),
                agent_row("reviewer", harness="claude")]
        for name in ("cx1", "cx2"):
            row = agent_row(name, harness="codex", status="blocked")
            row.update(session_account="primary", status_text=(
                "budget : pause (account primary, seven_day 80%%) until reset@%d; cap"
                % deadline))
            rows.append(row)
        return rows + list(extra)

    def fallback(self, *, deadline=T + 30 * H, min_hours=12, designation=None,
                 rows=None, pilot=""):
        return review_family_paused.fallback(
            designation or self.designation(), rows if rows is not None else self.registry(
                deadline=deadline), project="ameesh", families=FAMILIES, now=T,
            min_hours=min_hours, pilot=pilot)

    def test_a_family_paused_beyond_the_threshold_opens_the_fallback(self):
        evidence = self.fallback()
        self.assertEqual(evidence["family"], "codex")
        self.assertEqual(evidence["resume_ts"], T + 30 * H)
        self.assertEqual(evidence["accounts"], ["primary"])
        self.assertEqual(evidence["required"], 2)
        self.assertEqual(evidence["shortfall"], 1)

    def test_a_short_pause_does_not_open_it(self):
        self.assertIsNone(self.fallback(deadline=T + 6 * H))
        self.assertIsNone(self.fallback(deadline=T + 12 * H))  # pas *plus de* N h

    def test_a_missing_deadline_does_not_open_it(self):
        rows = self.registry()
        for row in rows:
            if row["harness"] == "codex":
                row["status_text"] = "budget : pause (account primary, seven_day 80%%)"
        self.assertIsNone(self.fallback(rows=rows))

    def test_a_family_with_one_unreadable_resume_is_not_proven(self):
        rows = self.registry()
        row = agent_row("cx3", harness="codex", status="blocked")
        row.update(session_account="secondary", status_text=(
            "budget : pause (account secondary, seven_day 80%%)"))
        rows.append(row)
        self.assertIsNone(self.fallback(rows=rows))

    def test_every_missing_seat_must_be_paused(self):
        self.assertIsNone(self.fallback(designation=self.designation(shortfall=2)))
        self.assertIsNone(self.fallback(designation=self.designation(required=3)))
        self.assertIsNone(self.fallback(designation=self.designation(quorum=T)))
        self.assertIsNone(self.fallback(designation=self.designation(
            unknown_authors=["x"])))

    def test_two_paused_families_are_not_one_missing_seat(self):
        rows = [agent_row("author", harness="deepseek")]
        for name, harness in (("cx1", "codex"), ("cx2", "codex"),
                              ("cl1", "claude")):
            row = agent_row(name, harness=harness, status="blocked")
            row.update(session_account="primary", status_text=(
                "budget : pause (account primary, seven_day 80%%) until reset@%d; cap"
                % (T + 30 * H)))
            rows.append(row)
        # aucun avis acquis : les deux familles manquent, et une seule pause
        # ne couvre pas deux sièges.
        self.assertIsNone(self.fallback(
            rows=rows, designation=dict(self.designation(),
                                        acquired_families=[])))

    def test_a_reachable_family_does_not_mask_the_paused_seat(self):
        # claude est joignable (il peut rendre l'avis admis avant fusion) ;
        # le siège manquant reste codex, en pause : c'est lui que le repli
        # remplace, et sa relecture reste due après fusion.
        evidence = self.fallback()
        self.assertEqual(evidence["family"], "codex")

    def test_the_note_is_stable_and_rereadable(self):
        evidence = self.fallback()
        note = review_family_paused.fallback_note(evidence, HEAD)
        self.assertTrue(note.startswith(review_family_paused.FALLBACK_PREFIX))
        self.assertEqual(note, review_family_paused.fallback_note(dict(evidence), HEAD))
        (payload,) = review_family_paused.recorded([{"note": note}])
        self.assertEqual(payload["freeze"], HEAD)
        self.assertEqual(payload["family"], "codex")
        self.assertEqual(payload["required"], 2)


class GateFallbackTest(unittest.TestCase):
    """La porte elle-même : un avis admis seulement sur la preuve de repli."""

    KLASS = {"class": "sensitive", "doubt": False,
             "files": [{"path": "src/ameesh/merge_queue.py",
                        "class": "sensitive", "rule": ["src/ameesh/merge_queue.py"]}],
             "train_reviews": 2,
             "train_reviews_paths": {"src/ameesh/merge_queue.py": 2},
             "train_reviews_wide": 0,
             "paused_family_min_hours": 12, "paused_family_reviews": 1}

    def marks(self, actor="cx1", verdict="ok"):
        return [{"id": 1, "kind": "frozen", "sha": HEAD, "actor": "author",
                 "at_ts": T - 4 * H},
                {"id": 2, "kind": "review-designation", "sha": HEAD,
                 "state": "waiting", "required": 2, "names": ["cx1"],
                 "author_families": ["deepseek"], "at_ts": T - 4 * H},
                {"id": 3, "kind": "verdict", "sha": HEAD, "actor": actor,
                 "verdict": verdict, "at_ts": T - H}]

    def item(self):
        return {"id": 7, "app": "amesh", "assignee": "author",
                "title": "porte", "state": "qa"}

    def gate(self, klass=None, *, actor="cx1", evidence="yes", record=False,
             record_error=None, packet=None):
        evidence_value = ({"schema": review_family_paused.FALLBACK_SCHEMA,
                           "family": "codex", "resume_ts": T + 30 * H,
                           "accounts": ["primary"], "min_hours": 12,
                           "required": 2, "shortfall": 1}
                          if evidence == "yes" else evidence)
        with mock.patch.object(merge_queue, "_paused_family_fallback",
                               return_value=evidence_value), \
                mock.patch.object(merge_queue, "_exact_verdict_receipt",
                                  return_value={}), \
                mock.patch.object(merge_queue, "_record_paused_family_fallback",
                                  side_effect=record_error) as recorded:
            out = merge_queue.review_gate(
                types.SimpleNamespace(), self.marks(actor=actor), HEAD,
                klass=self.KLASS if klass is None else klass,
                item=self.item(), families=FAMILIES,
                family_of=lambda name: ({"author": "deepseek", "cx1": "codex",
                                         "r2": "deepseek", "r3": "claude"}).get(name, ""),
                record=record, packet=packet, now=T)
        return out, recorded

    def test_one_independent_ok_is_admitted_on_the_evidence(self):
        out, _ = self.gate()
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["required"], 1)
        self.assertEqual(out["fallback"]["family"], "codex")
        self.assertEqual([entry["family"] for entry in out["accepted"]], ["codex"])

    def test_a_direct_merge_keeps_both_reviews(self):
        # B1 cx1 (12/11 16:10) : une file ou un feu de lot ordinaire peut
        # fusionner sur une CI ciblée (exception incident/unblock) ; le repli
        # y est refusé, sinon la seconde barrière de #213032 a — la suite
        # complète du paquet — manquerait.
        out, recorded = self.gate(packet=False, record=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 2)
        self.assertIn("reserved to a packet admission", out["why"])
        self.assertIn("an effective merge outside a packet keeps both reviews",
                      out["why"])
        self.assertEqual(out["fallback_refused"],
                         "the paused-family fallback is reserved to a packet admission "
                         "covered by the packet's full CI (#213032); an effective merge "
                         "outside a packet keeps both reviews")
        recorded.assert_not_called()

    def test_an_effective_merge_without_context_fails_closed(self):
        # Une admission effective qui ne nomme pas son contexte (`record=True`
        # sans `packet`) ne reçoit pas le repli : le défaut n'est permissif
        # que pour les lectures.
        out, recorded = self.gate(record=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 2)
        self.assertIn("effective merge outside a packet", out["why"])
        recorded.assert_not_called()

    def test_a_packet_admission_opens_the_fallback_and_writes_it(self):
        # Une tête `integration/**` est admise par un paquet : sa suite complète
        # est la seconde barrière, le repli s'ouvre et la preuve s'écrit sur la
        # tête du membre avant la fusion du véhicule.
        out, recorded = self.gate(packet=True, record=True)
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["required"], 1)
        self.assertEqual(out["fallback"]["family"], "codex")
        recorded.assert_called_once()

    def test_without_a_canon_key_the_two_reviews_stay_due(self):
        klass = {key: value for key, value in self.KLASS.items()
                 if not key.startswith("paused_family")}
        out, _ = self.gate(klass=klass, evidence=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 2)

    def test_an_ineligible_lot_never_calls_the_fallback(self):
        klass = dict(self.KLASS, files=[{"path": ".github/workflows/tests.yml",
                                        "class": "sensitive"}],
                     train_reviews_paths={".github/workflows/tests.yml": 2})
        out, _ = self.gate(klass=klass, evidence=None)
        self.assertFalse(out["ok"])
        self.assertEqual(out["required"], 2)

    def test_an_author_verdict_is_still_not_admitted(self):
        out, _ = self.gate(actor="author")
        self.assertFalse(out["ok"])
        self.assertEqual([entry["reason"] for entry in out["rejected"]], ["author"])

    def test_the_note_is_written_only_when_the_gate_admits(self):
        out, recorded = self.gate(record=True, packet=True)
        self.assertTrue(out["ok"], out["why"])
        recorded.assert_called_once()
        out, recorded = self.gate(record=True, packet=True,
                                  record_error=work.WorkError("db"))
        self.assertFalse(out["ok"])
        self.assertIn("could not be recorded", out["why"])

    def test_a_read_only_admission_does_not_write_but_stays_ok(self):
        # B/P1 auditeur (11/11 21:42) : la sélection de candidat lit la porte
        # sans écrire ; elle doit rester `ok` pour que le lot entre au paquet,
        # et la preuve est écrite plus tard par la porte du véhicule (record).
        out, recorded = self.gate(record=False)
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual(out["required"], 1)
        recorded.assert_not_called()


class PacketGateRecordTest(unittest.TestCase):
    """#6990 (B/P1 auditeur) : la porte du véhicule écrit la preuve des membres."""

    def test_the_vehicle_gate_passes_record_to_need(self):
        with mock.patch.object(packet_reviews, "is_vehicle", return_value=True), \
                mock.patch.object(packet_reviews, "need",
                                  return_value={"ok": False, "why": "x"}) as need:
            out = merge_queue.review_gate(
                types.SimpleNamespace(), [], HEAD, cfg=object(), target="main",
                item={"id": 1, "app": "ameesh"}, record=True)
        self.assertFalse(out["ok"])
        self.assertTrue(need.call_args.kwargs["record"])

    def test_a_read_only_vehicle_gate_does_not_write(self):
        with mock.patch.object(packet_reviews, "is_vehicle", return_value=True), \
                mock.patch.object(packet_reviews, "need",
                                  return_value={"ok": False, "why": "x"}) as need:
            merge_queue.review_gate(
                types.SimpleNamespace(), [], HEAD, cfg=object(), target="main",
                item={"id": 1, "app": "ameesh"})
        self.assertFalse(need.call_args.kwargs["record"])

    def test_need_passes_record_to_each_member_gate(self):
        member = {"id": 7, "app": "ameesh", "assignee": "author"}
        marks = [{"kind": "frozen", "sha": HEAD, "actor": "author", "at_ts": T}]
        with mock.patch.object(packet_reviews, "is_vehicle", return_value=True), \
                mock.patch.object(packet_reviews, "_packet_base",
                                  return_value=("main", None)), \
                mock.patch.object(packet_reviews, "members",
                                  return_value=[{"lot": 7}]), \
                mock.patch.object(packet_reviews, "_member_freeze",
                                  return_value=(member, marks, {"sha": HEAD})), \
                mock.patch.object(packet_reviews, "_stack_order",
                                  side_effect=lambda repo, prepared: prepared), \
                mock.patch.object(packet_reviews, "own_changes",
                                  return_value={"paths": [], "commits": []}), \
                mock.patch.object(packet_reviews.plan_git, "_commit",
                                  side_effect=lambda repo, sha: sha), \
                mock.patch.object(packet_reviews, "_ancestor",
                                  return_value=True), \
                mock.patch.object(packet_reviews.plan_git, "_out",
                                  return_value="a" * 40), \
                mock.patch.object(lot_class, "of_lot", return_value={}), \
                mock.patch.object(merge_queue, "review_gate",
                                  return_value={"ok": True,
                                                "attribution_read": False}) as gate:
            out = packet_reviews.need(types.SimpleNamespace(), 999, {},
                                      [], "/repo", "a" * 40, HEAD,
                                      record=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(gate.call_args.kwargs["record"])
        # #6990 (B1 cx1) : le membre n'est admis que par l'admission d'un
        # paquet, dont la suite complète est la seconde barrière du repli.
        self.assertIs(gate.call_args.kwargs["packet"], True)


class PausedFamilyFallbackWiringTest(unittest.TestCase):
    """`_paused_family_fallback` relie désignation, registre et pauses."""

    def listing(self, deadline=T + 30 * H):
        rows = [agent_row("author", harness="deepseek"),
                agent_row("reviewer", harness="claude")]
        for name in ("cx1", "cx2"):
            row = agent_row(name, harness="codex", status="blocked")
            row.update(session_account="primary", status_text=(
                "budget : pause (account primary, seven_day 80%%) until reset@%d; cap"
                % deadline))
            rows.append(row)
        return rows

    def designation(self):
        return {"freeze": {"sha": HEAD, "at_ts": T - 4 * H},
                "required": 2, "acquired": 1, "shortfall": 1,
                "quorum": None, "unknown_authors": [],
                "author_families": ["deepseek"], "acquired_families": ["claude"]}

    def call(self, klass=None, now=T, deadline=T + 30 * H):
        store = types.SimpleNamespace(
            operations=types.SimpleNamespace(listing=lambda: self.listing(deadline)))
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(work, "events", return_value=[]), \
                mock.patch.object(debit, "designations",
                                  return_value=self.designation()), \
                mock.patch.object(review_family_paused, "exclusions",
                                  return_value=({}, set())):
            return merge_queue._paused_family_fallback(
                types.SimpleNamespace(), {"id": 7, "app": "ameesh"},
                [], klass if klass is not None else GateFallbackTest.KLASS,
                FAMILIES, None, HEAD, now)

    def test_the_wiring_reads_the_pause_and_returns_the_evidence(self):
        evidence = self.call()
        self.assertEqual(evidence["family"], "codex")
        self.assertEqual(evidence["resume_ts"], T + 30 * H)

    def test_a_short_pause_returns_none(self):
        self.assertIsNone(self.call(deadline=T + 6 * H))

    def test_a_freeze_that_is_not_the_lots_returns_none(self):
        with mock.patch.object(storage, "of", return_value=types.SimpleNamespace(
                operations=types.SimpleNamespace(listing=lambda: self.listing()))), \
                mock.patch.object(work, "events", return_value=[]), \
                mock.patch.object(debit, "designations",
                                  return_value=self.designation()), \
                mock.patch.object(review_family_paused, "exclusions",
                                  return_value=({}, set())):
            self.assertIsNone(merge_queue._paused_family_fallback(
                types.SimpleNamespace(), {"id": 7, "app": "ameesh"}, [],
                GateFallbackTest.KLASS, FAMILIES, None, "b" * 40, T))


class AfterMergeTest(unittest.TestCase):
    """La relecture due après fusion est celle de la famille en pause."""

    LOT = 25

    def setUp(self):
        self.item = {"id": self.LOT, "title": "un lot de porte", "app": "ameesh",
                     "assignee": "a1", "state": "merged", "branch_target": "main",
                     "type": "evolution"}
        self.ops = FakeOps({self.LOT: self.item})
        self.ops.milestones_rows[self.LOT] = [
            {"id": 1, "kind": "frozen", "sha": HEAD, "actor": "a1",
             "at_ts": T - 4 * H}]
        self.ops.noted = [{"work_item_id": self.LOT, "note":
                           review_family_paused.fallback_note(
                               {"family": "codex", "resume_ts": T + 30 * H,
                                "accounts": ["primary"]}, HEAD)}]
        self.ops.events_noted = lambda prefix, since, ids=None: [
            row for row in self.ops.noted
            if prefix in row["note"] and (not ids or row["work_item_id"] in ids)]
        self.store = mock.Mock(work=self.ops)
        self.turns = {"a1": "deepseek", "cx1": "codex", "r1": "deepseek"}
        for patch in (
            mock.patch.object(storage, "of", return_value=self.store),
            mock.patch.object(work, "get",
                              side_effect=lambda db, n: self.ops.items.get(int(n))),
            mock.patch.object(work, "add", side_effect=lambda db, **kw: self.ops.add(**kw)),
            mock.patch.object(work, "note",
                              side_effect=lambda db, n, text, actor="": self.ops.note(
                                  n, "", text, actor)),
            mock.patch.object(review_lot, "_turns_harness",
                              side_effect=lambda db, actor, at: self.turns.get(actor, "")),
            mock.patch.object(review_lot, "compiled_families",
                              return_value=FAMILIES),
            mock.patch.object(review_lot, "_author_at_freeze",
                              return_value={"name": "a1", "harness": "deepseek"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def open(self, klass=None, **kw):
        return review_lot.open_after_merge(
            None, lot=self.LOT,
            klass=klass or {"class": "sensitive", "why": "policy of ameesh"},
            merged_sha=HEAD, **kw)

    def verdict(self, actor, ts, sha=HEAD, kind="ok"):
        self.ops.milestones_rows[self.LOT].append(
            {"id": 10 + len(self.ops.milestones_rows[self.LOT]), "kind": "verdict",
             "sha": sha, "actor": actor, "verdict": kind, "at_ts": ts})

    def test_the_fallback_opens_one_review_from_the_paused_family(self):
        out = self.open()
        self.assertTrue(out["created"], out)
        self.assertEqual(out["reviews"], 1)
        self.assertEqual(out["fallback"]["family"], "codex")
        (item,) = self.ops.added
        self.assertEqual(item["issue_ref"], review_lot.ref_of(self.LOT))
        self.assertIn("Repli #6990", item["body"])
        self.assertIn("la famille en pause** — codex", item["body"])
        # la marque écrite au journal du lot source nomme la famille due
        self.assertIn('"families": ["codex"]', item.get("mark_note") or "")

    def test_the_pre_merge_ok_does_not_count_as_the_due_review(self):
        self.verdict("r1", T - H)       # deepseek, l'avis qui a admis le lot
        out = self.open()
        self.assertTrue(out["created"], out)
        self.assertFalse(out["already_reviewed"])

    def test_a_review_from_the_paused_family_on_the_head_closes_the_mission(self):
        self.verdict("r1", T - H)
        self.verdict("cx1", T)
        out = self.open()
        self.assertFalse(out["created"], out)
        self.assertTrue(out["already_reviewed"], out)
        self.assertEqual([entry["family"] for entry in out["reviewed_by"]], ["codex"])

    def test_without_the_mark_no_review_is_opened(self):
        self.ops.noted = []
        out = self.open()
        self.assertFalse(out["created"])
        self.assertIn("pas de lot de relecture", out["why"])


class RequiredFamilyTest(unittest.TestCase):
    """La prise d'une mission de repli suit la famille que la marque nomme.

    B1 de `ameesh-cx2` (mail #213516) : `required_family` recalculait toujours
    l'opposée de l'auteur et ignorait `families` de la marque ; la mission de
    la famille en pause n'était donc prise par personne, et la famille opposée
    la prenait pour se la voir refuser à la clôture par `guard_delivery`.
    """

    LOT = AfterMergeTest.LOT
    ITEM = {"id": 9, "issue_ref": review_lot.ref_of(AfterMergeTest.LOT)}

    def call(self, mark, families_of_canon=FAMILIES):
        with mock.patch.object(review_lot, "compiled_families",
                               return_value=families_of_canon), \
                mock.patch.object(work, "get",
                                  return_value={"id": self.LOT, "assignee": "a1"}), \
                mock.patch.object(review_lot, "requirement_of",
                                  return_value=mark), \
                mock.patch.object(review_lot, "_author_at_freeze",
                                  return_value={"name": "a1", "harness": "deepseek"}):
            required = review_lot.required_family(None, self.ITEM)
            taken = {agent: review_lot.may_take(
                None, self.ITEM, {"name": agent, "harness": agent})
                for agent in families_of_canon}
        return required, taken

    def test_the_mark_names_the_paused_family(self):
        required, taken = self.call({"families": ["codex"]})
        self.assertEqual(required, "codex")
        self.assertTrue(taken["codex"], taken)
        self.assertFalse(taken["claude"], taken)
        self.assertFalse(taken["deepseek"], taken)

    def test_without_families_the_opposite_of_the_author_is_due(self):
        required, taken = self.call(None)
        self.assertEqual(required, "codex")        # opposée de deepseek
        self.assertTrue(taken["codex"], taken)
        self.assertFalse(taken["claude"], taken)

    def test_several_named_families_are_not_guessed(self):
        required, taken = self.call({"families": ["codex", "claude"]})
        self.assertEqual(required, "")
        self.assertFalse(any(taken.values()), taken)

    def test_an_unreadable_mark_is_an_unknown_family(self):
        with mock.patch.object(review_lot, "compiled_families",
                               return_value=FAMILIES), \
                mock.patch.object(work, "get",
                                  return_value={"id": self.LOT, "assignee": "a1"}), \
                mock.patch.object(review_lot, "requirement_of",
                                  side_effect=work.WorkError("marque illisible")):
            self.assertEqual(review_lot.required_family(None, self.ITEM), "")
            self.assertFalse(review_lot.may_take(
                None, self.ITEM, {"name": "cx1", "harness": "codex"}))


class GuardDeliveryTest(unittest.TestCase):
    """La garde de clôture n'accepte que l'avis de la famille désignée."""

    def guard(self, verdicts, families=("codex",)):
        mark = review_lot.requirement_mark(
            AfterMergeTest.LOT, required=1, merged_sha=HEAD, author="a1",
            author_family="deepseek", families=families)
        item = {"id": 9, "issue_ref": review_lot.ref_of(AfterMergeTest.LOT),
                "created_ts": 0}
        source = {"id": AfterMergeTest.LOT, "assignee": "a1", "app": "ameesh"}
        rows = [{"id": index, "kind": "verdict", "actor": actor, "verdict": kind,
                 "sha": HEAD, "at_ts": index}
                for index, (actor, kind) in enumerate(verdicts, 1)]
        with mock.patch.object(review_lot, "requirement_of", return_value={
                "schema": review_lot.MARK_SCHEMA, "source": AfterMergeTest.LOT,
                "sha": HEAD, "required": 1, "author": "a1",
                "author_family": "deepseek",
                "families": list(families)}), \
                mock.patch.object(work, "get", return_value=source), \
                mock.patch.object(work, "milestones", return_value=rows), \
                mock.patch.object(review_lot, "_repo_of_agent", return_value=""), \
                mock.patch.object(lot_class, "repository", return_value=""), \
                mock.patch.object(merge_queue, "verdict_family",
                                  side_effect=lambda db, mark: {
                                      "r1": "deepseek", "cx1": "codex",
                                      "cx2": "codex"}.get(mark["actor"], "")):
            return review_lot.guard_delivery(None, item)

    def test_the_other_family_verdict_does_not_close_it(self):
        with self.assertRaises(work.WorkError) as caught:
            self.guard([("r1", "ok")])
        self.assertIn("1 avis", str(caught.exception))

    def test_the_paused_family_verdict_closes_it(self):
        self.assertEqual(self.guard([("r1", "ok"), ("cx1", "ok")])["accepted"],
                         ["cx1"])

    def test_a_blocked_verdict_still_blocks(self):
        with self.assertRaises(work.WorkError) as caught:
            self.guard([("cx1", "blocked")])
        self.assertIn("bloquant", str(caught.exception))


class OpenForMergeFallbackTest(unittest.TestCase):
    """`open_for_merge` rend le repli trouvé même si la création a échoué.

    B2 de `ameesh-cx2` (mail #213516) : `open_after_merge` avait trouvé le
    repli puis échoué à écrire la mission ; `open_for_merge` ne remontait pas
    `fallback`, et le rattrapage closait le candidat comme une classe non
    légère ordinaire — la relecture due était perdue au lieu d'être réessayée.
    """

    LOT = AfterMergeTest.LOT

    def test_the_due_fallback_survives_a_failed_creation(self):
        item = {"id": self.LOT, "title": "un lot de porte", "app": "ameesh",
                "assignee": "a1", "state": "merged", "branch_target": "main",
                "type": "evolution"}
        ops = FakeOps({self.LOT: item})
        store = mock.Mock(work=ops)
        opened = {"class": "sensitive", "merged_class": "sensitive",
                  "fallback": {"family": "codex", "resume_ts": T + 30 * H,
                               "accounts": ["primary"]},
                  "why": "lot de relecture non ouvert : WorkError: refusé"}
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(work, "get", return_value=item), \
                mock.patch.object(review_lot, "class_at_merge",
                                  return_value={"class": "sensitive",
                                                "why": "policy of ameesh"}), \
                mock.patch.object(review_lot, "open_after_merge",
                                  return_value=opened):
            out = review_lot.open_for_merge(None, self.LOT, merged_sha=HEAD)
        self.assertFalse(out["created"])
        self.assertEqual(out["fallback"]["family"], "codex", out)
        self.assertIn("non ouvert", out["why"])


class CatchUpFallbackRetryTest(_Witness):
    """Le rattrapage réessaie la mission de repli, il ne la clôt pas.

    B2 de `ameesh-cx2` (mail #213516) : échec de création puis réussite à la
    passe suivante, pour un lot non léger admis par le repli d'une famille en
    pause — la mission de cette famille reste due après fusion (#213032 a).
    """

    def fallback(self):
        return {"family": "codex", "resume_ts": T + 30 * H,
                "accounts": ["primary"]}

    def run_pass(self, *, broken=False):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                review_lot, "class_at_merge",
                return_value={"class": "sensitive", "why": "policy of ameesh"}))
            stack.enter_context(mock.patch.object(
                review_lot, "_fallback_on_merge", return_value=self.fallback()))
            if broken:
                stack.enter_context(mock.patch.object(
                    review_lot.work, "add", side_effect=work.WorkError("refusé")))
            return review_lot.catch_up(self.db, retry_s=0)

    def test_a_failed_fallback_creation_is_retried_not_closed(self):
        lot = self.merged_lot("repli non ouvert")
        first = self.run_pass(broken=True)
        self.assertEqual((first["examined"], first["missing"], first["attempted"],
                          first["terminal"]), (1, 1, 1, 0), first)
        self.assertEqual(self.review_lots(lot), [])
        second = self.run_pass()
        self.assertEqual((second["examined"], second["opened"]), (1, 1), second)
        (mission,) = self.review_lots(lot)
        self.assertEqual(mission["issue_ref"], review_lot.ref_of(lot))
        body = work.get(self.db, mission["id"])["body"]
        self.assertIn("Repli #6990", body)
        self.assertIn("famille **codex**", body)
        mark = review_lot.requirement_of(
            self.db, {"issue_ref": review_lot.ref_of(lot)})
        self.assertEqual(mark["families"], ["codex"])
        self.assertEqual(mark["required"], 1)


if __name__ == "__main__":
    unittest.main()

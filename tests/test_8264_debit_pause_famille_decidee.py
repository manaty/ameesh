# SPDX-License-Identifier: AGPL-3.0-only
"""#8264 : une pause de famille couverte par une décision owner signée n'est
pas une attente lente.

Constat de l'auditeur (25/11 01:11Z) : #8231 et #8195 sont retenus par la
pause codex que la décision owner #233013 a couvre jusqu'au 26/11 18:00Z.
`ameesh debit --check` les comptait pourtant en `slow_waits` et sortait 2,
sans geste possible. La décision signée et sa date de fin sont lues dans la
réponse : l'attente est listée à part (`decision_waits`) et redevient
ordinaire à l'échéance.
"""
from __future__ import annotations

import unittest

from ameesh import debit
from tests.test_3292_debit import event, frozen, item, run
from tests.test_4779_reviewer_profile import agent_row
from tests.test_5430_debit_waiting_mark import FAMILIES, HEAD, waiting_mark

T = 1792812000.0            # 2026-10-24 03:20:00Z
H = 3600.0
DAY = 86400.0

LABEL = ("Ouvrir la réserve critique de codex secondaire (10 points) aux seuls "
         "tours de RELECTURE des relecteurs codex désignés (ameesh-cx1, ameesh-cx2, "
         "mty-amh-impl-11), jusqu'à la remise à zéro du 26/11 vers 16:00Z ; aucun "
         "tour d'auteur sur cette réserve ; règle datée au canon "
         "(expire 26/11 18:00Z), sans overage ni dépense nouvelle")


def registry(until=None):
    until = T + H if until is None else float(until)
    rows = [agent_row("author", harness="deepseek"),
            agent_row("reviewer", harness="claude")]
    for name in ("cx1", "cx2"):
        row = agent_row(name, harness="codex", status="blocked")
        row.update(session_account="primary", status_text=
                   "budget : pause (account primary, seven_day 80%%) until reset@%d; cap"
                   % int(until))
        rows.append(row)
    return rows


def decision_view(*, ident=233013, label=LABEL, channel="signed", state="answered",
                  created=T - 14 * H, question=None, **over):
    label = str(label)
    view = {
        "id": ident,
        "state": state,
        "question": question or (
            "La famille codex est indisponible pour 2 jours : secondaire en pause "
            "jusqu'au 26/11 16:08Z ; primaire au plafond dur. Que fait-on ?"),
        "options": [{"key": "a", "label": label}],
        "created_ts": float(created),
        "closed_ts": float(created) + H,
        "answer": {
            "by": "human:smichea",
            "channel": channel,
            "text": "a — saisi par le bureau, délégation §10",
            "option": "a",
            "option_label": label,
        },
    }
    if channel == "signed":
        view["answer"]["proof"] = {"approval_id": 439, "approver": "smichea",
                                   "signature_key": "a" * 64}
    view.update(over)
    return view


def report(*, decisions=(), now=T, pause_until=None):
    return run(
        items=[item(8231, assignee="author")],
        milestones=[frozen(8231, 100, T - 4 * H, sha=HEAD)],
        events=[event(8231, 200, T - 4 * H, waiting_mark(8231, 100))],
        agents=registry(pause_until), families=FAMILIES, now=now,
        decisions=list(decisions))


class DecisionCoveredPauseTest(unittest.TestCase):
    def test_a_signed_decision_covers_the_family_pause(self):
        result = report(decisions=[decision_view()])
        self.assertEqual([row["lot"] for row in result["slow_waits"]], [])
        self.assertEqual(result["totals"]["slow_waits"], 0)
        self.assertEqual([row["lot"] for row in result["decision_waits"]], [8231])
        wait = result["decision_waits"][0]
        self.assertEqual(wait["kind"], "review_family_paused")
        self.assertFalse(wait["human"])
        self.assertTrue(wait["decided"])
        self.assertEqual(wait["decisions"][0]["id"], 233013)
        self.assertEqual(wait["decision_until_iso"], "2026-11-26T18:00:00Z")
        self.assertIn("#233013", wait["gesture"])
        self.assertIn("2026-11-26T18:00:00Z", wait["gesture"])
        text = debit.format_text(result)
        self.assertIn("décision owner", text)
        self.assertIn("#233013", text)
        self.assertIn("hors --check", text)

    def test_the_pause_itself_is_still_reported(self):
        result = report(decisions=[decision_view()])
        self.assertEqual(result["qa"][0]["review_family_paused"][0]["family"], "codex")
        self.assertEqual(result["qa"][0]["review_family_paused"][0]["decision"]["id"],
                         233013)

    def test_an_expired_decision_leaves_the_ordinary_wait(self):
        # Après le 26/11 18:00Z, la décision ne couvre plus rien : l'attente
        # redevient une attente lente, avec le geste ordinaire.
        later = T + 40 * DAY
        result = report(decisions=[decision_view()], now=later,
                        pause_until=later + H)
        self.assertEqual([row["lot"] for row in result["slow_waits"]], [8231])
        self.assertEqual(result["decision_waits"], [])
        self.assertIn("vérifier le compte de repli",
                      result["slow_waits"][0]["gesture"])

    def test_a_live_decision_still_covers_after_the_28th(self):
        # Contrôle du précédent : une décision encore vivante le 27/11 couvre
        # toujours la pause, donc ce n'est pas la date de création qui tranche.
        later = T + 40 * DAY
        label = LABEL.replace("26/11 18:00Z", "30/12 18:00Z")
        result = report(decisions=[decision_view(label=label)], now=later,
                        pause_until=later + H)
        self.assertEqual(result["slow_waits"], [])
        self.assertEqual([row["lot"] for row in result["decision_waits"]], [8231])

    def test_an_unsigned_decision_covers_nothing(self):
        result = report(decisions=[decision_view(channel="cli")])
        self.assertEqual([row["lot"] for row in result["slow_waits"]], [8231])
        self.assertEqual(result["decision_waits"], [])

    def test_a_decision_without_an_expiry_covers_nothing(self):
        result = report(decisions=[decision_view(
            label=LABEL.replace("(expire 26/11 18:00Z)", ""))])
        self.assertEqual([row["lot"] for row in result["slow_waits"]], [8231])
        self.assertEqual(result["decision_waits"], [])

    def test_a_decision_for_another_family_covers_nothing(self):
        result = report(decisions=[decision_view(
            label=LABEL.replace("codex", "claude"))])
        self.assertEqual([row["lot"] for row in result["slow_waits"]], [8231])
        self.assertEqual(result["decision_waits"], [])

    def test_a_superseded_decision_covers_nothing(self):
        view = decision_view()
        view["superseded_by"] = 233999
        result = report(decisions=[view])
        self.assertEqual([row["lot"] for row in result["slow_waits"]], [8231])
        self.assertEqual(result["decision_waits"], [])

    def test_an_iso_expiry_is_read_too(self):
        label = "(expire: 2026-11-26 18:00Z) — décision de plafond codex"
        result = report(decisions=[decision_view(label=label)])
        self.assertEqual(result["decision_waits"][0]["decision_until_iso"],
                         "2026-11-26T18:00:00Z")


class DecisionCoverageParsingTest(unittest.TestCase):
    def test_coverage_is_read_from_the_signed_answer_only(self):
        from ameesh import review_family_paused as pause
        coverage = pause.coverages([decision_view()], families=FAMILIES, now=T)
        self.assertEqual(set(coverage), {"codex"})
        self.assertEqual(coverage["codex"]["id"], 233013)
        self.assertEqual(coverage["codex"]["until_iso"], "2026-11-26T18:00:00Z")

    def test_a_year_wrap_is_read_on_the_next_year(self):
        # Une décision de décembre qui court jusqu'en janvier : l'année de
        # création est lue, puis +1 quand l'échéance est déjà passée.
        from ameesh import review_family_paused as pause
        created = 1796000000.0   # 2026-11-30
        coverage = pause.coverages(
            [decision_view(label="pause codex (expire 03/01 12:00Z)",
                           created=created)],
            families=FAMILIES, now=created)
        self.assertEqual(coverage["codex"]["until_iso"], "2027-01-03T12:00:00Z")

    def test_an_unreadable_expiry_covers_nothing(self):
        from ameesh import review_family_paused as pause
        self.assertEqual(pause.coverages(
            [decision_view(label="pause codex (expire bientôt)")],
            families=FAMILIES, now=T), {})


if __name__ == "__main__":
    unittest.main()

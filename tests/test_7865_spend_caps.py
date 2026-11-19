# SPDX-License-Identifier: AGPL-3.0-only
"""#7865 : dépenses pré-déléguées — plafonds, ledger et arrêt.

Une approbation permanente peut porter une dimension dépense signée
(plafonds mensuel et journalier, fournisseurs et objets fermés, horizon de la
prochaine revue humaine). L'action de dépense déclare sa récurrence
(`once`/`daily`), son fournisseur et son objet dans ses `args` signés ; le
ledger est la seule source du déjà-dépensé et de la projection. Un plafond
refuse nommément (`spend_*`), jamais de dépassement ; le relevé de l'owner
liste les ressources récurrentes et la commande d'arrêt d'une ligne.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from ameesh import (actions, authority, db as db_mod, decisions, receipts,
                    registry, role_timers, signing, spend, standing_policy, work)

from .test_actions import ActionsBase, ALICE, POLICY
from .test_receipts import standing
from .webauthn_soft import make_request

REPO = Path(__file__).resolve().parents[1]
AGENT = "agent:alice-impl"
SPEND = {"max_per_month": 50, "max_per_day": 10,
         "providers": ["quota"], "purposes": ["quota-reset"]}


def federation(*, horizon: int | None = 24, **rule_overrides) -> dict:
    """Un `federation.yaml` conforme OKF portant une règle de dépense.

    `horizon=None` omet `review_horizon_hours` : le canon doit alors être
    refusé dès qu'une règle porte un plafond de dépense."""
    rule = {"connector": "shell-noop", "operations": ["refund"],
            "approvers": [ALICE], "max_duration_hours": 1,
            "max_amount": 10 ** 6, "currency": "EUR",
            "allow_missing_beneficiary": False, "require_charter": False,
            "require_targets": False,
            "max_per_month": 50, "max_per_day": 10,
            "providers": ["quota"], "purposes": ["quota-reset"]}
    rule.update(rule_overrides)
    block = {"schema": "ameesh-standing-approvals/1", "rules": [rule]}
    if horizon is not None:
        block["review_horizon_hours"] = horizon
    return {"extensions": {"ameesh": {"standing_approvals": block}}}


def policy(*, canon_month: int = 10_000, canon_day: int = 10_000,
           providers=("quota",), purposes=("quota-reset",), horizon: int = 24,
           max_amount: int = 10 ** 6, operations=("refund",)) -> standing_policy.Policy:
    """La politique du canon pour les tests ; `horizon=0` simule son absence."""
    rule = standing_policy.Rule(
        connector="shell-noop", operations=tuple(operations), approvers=(ALICE,),
        max_duration=3600, max_amount=max_amount, currency="EUR",
        allow_missing_beneficiary=False, require_charter=False,
        max_per_month=canon_month, max_per_day=canon_day,
        providers=tuple(providers), purposes=tuple(purposes))
    return standing_policy.Policy(rules=(rule,), review_horizon_hours=horizon)


def grant_value(*, month: int = 50, day: int = 10, providers=("quota",),
                purposes=("quota-reset",), max_amount: int = 50) -> dict:
    return standing(operations=["refund"], max_amount=max_amount,
                    until=int(time.time()) + 1800, beneficiary=AGENT,
                    spend={"max_per_month": month, "max_per_day": day,
                           "providers": list(providers),
                           "purposes": list(purposes)})


class SpendPolicyTest(unittest.TestCase):
    """#7865 : le bloc du canon — horizon obligatoire et plafonds d'organisation."""

    def check(self, limits, **overrides):
        value = {"connector": "shell-noop", "operations": ["refund"],
                 "class": "costly", "max_amount": 50, "currency": "EUR",
                 "until": 1000 + 1800, "beneficiary": AGENT,
                 "spend": dict(SPEND)}
        value.update(overrides)
        return limits.check(approver=ALICE, standing=value, iat=1000)

    def test_horizon_obligatoire_avec_un_plafond(self):
        with self.assertRaises(standing_policy.StandingPolicyError) as ctx:
            standing_policy.from_federation(federation(horizon=None))
        self.assertIn("review_horizon_hours", str(ctx.exception))
        self.assertIn("required with a spend rule", str(ctx.exception))

    def test_horizon_sans_plafond_refuse(self):
        with self.assertRaises(standing_policy.StandingPolicyError) as ctx:
            standing_policy.from_federation(federation(
                max_per_month=0, max_per_day=0, providers=None, purposes=None))
        self.assertIn("no spend rule uses it", str(ctx.exception))

    def test_plafonds_du_canon_valides(self):
        limits = standing_policy.from_federation(federation())
        self.assertEqual(limits.review_horizon_hours, 24)
        self.assertIsNone(self.check(limits))
        described = limits.describe()[0]
        self.assertEqual((described["max_per_month"], described["max_per_day"],
                          described["providers"], described["purposes"]),
                         (50, 10, ["quota"], ["quota-reset"]))

    def test_grant_sans_dimension_depense_refuse(self):
        limits = standing_policy.from_federation(federation())
        self.assertIn("must carry its spend dimension",
                      self.check(limits, spend=None))

    def test_grant_hors_des_plafonds_du_canon_refuse(self):
        limits = standing_policy.from_federation(federation())
        self.assertIn("above the canon cap", self.check(
            limits, max_amount=51, spend=dict(SPEND, max_per_month=51)))
        self.assertIn("above the canon cap", self.check(
            limits, spend=dict(SPEND, max_per_day=11)))
        self.assertIn("outside the canon list", self.check(
            limits, spend=dict(SPEND, providers=["stripe"])))
        self.assertIn("outside the canon list", self.check(
            limits, spend=dict(SPEND, purposes=["autre"])))

    def test_recu_refuse_les_plafonds_incoherents(self):
        """La forme signée est vérifiée avant tout enregistrement."""
        from ameesh import receipts as receipts_mod
        from .webauthn_soft import SoftWebAuthn
        soft = SoftWebAuthn("ES256")

        def parse(**spend_overrides) -> None:
            value = grant_value()
            value["spend"].update(spend_overrides)
            receipt = soft.receipt(make_request(ALICE, standing=value))
            receipts_mod.parse_receipt(receipt)

        for name, overrides in (
                ("plafond mensuel nul", {"max_per_month": 0}),
                ("jour > mois", {"max_per_day": 60}),
                ("mois > total", {"max_per_month": 60}),
                ("fournisseurs vides", {"providers": []}),
                ("clé libre", {"period": "monthly"})):
            with self.subTest(case=name):
                with self.assertRaises(receipts_mod.ReceiptError):
                    parse(**overrides)

    def test_engagement_annuel_impossible(self):
        """`recurrence` n'a que `once` et `daily` : aucun champ annuel."""
        with self.assertRaises(spend.SpendError) as ctx:
            spend.normalize({"recurrence": "annual", "provider": "quota",
                             "purpose": "quota-reset"},
                            amount=5, currency="EUR", limits=policy())
        self.assertEqual(ctx.exception.code, "spend_recurrence")

    def test_refus_escalade_au_responsable_du_grant(self):
        from unittest import mock
        action = {"action_id": "act_test", "connector": "shell-noop",
                  "operation": "refund", "target": "fournisseur:quota",
                  "project": "demo", "auth_approver": ALICE, "auth_grant_id": None}
        db = type("Db", (), {"cfg": object()})()
        with mock.patch.object(spend.fil, "record") as record:
            spend.escalate(db, action, "spend_daily_cap", "cap atteint")
        self.assertEqual(record.call_args.kwargs["recipients"], [ALICE])
        self.assertIn("spend_daily_cap", record.call_args.kwargs["text"])
        self.assertIn("cap atteint", record.call_args.kwargs["text"])


class SpendLedgerTest(ActionsBase):
    """#7865 : enregistrement, plafonds au lancement, ledger et relevé."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host)
        self.owner_key, public, _fp = signing.write_keypair(
            os.path.join(self.tmp, "keys-owner"), "alice")
        authority.register_human_key(self.db, ALICE, signing.read_public(public),
                                     role="owner")
        self.lot = work.add(self.db, title="RT-7865 : revue des dépenses",
                           assignee="orch")

    # -- utilitaires -------------------------------------------------------
    def confirm_at(self, ledger: int, day) -> None:
        """Simule une confirmation HUMAINE passée, pour les jours simulés du
        balayage (l'API réelle prend l'heure de la base, jamais une date)."""
        self.db.execute(
            "UPDATE spend_ledger SET confirmed_at = "
            "(%s::date::timestamp AT TIME ZONE 'UTC'), confirmed_by = %s, "
            "confirmed_decision_id = NULL, updated_at = now() WHERE id = %s",
            (day, ALICE, ledger))

    def signed_decision(self, ledger: int, *, option: str | None = None,
                        needs_signature: bool = True, now=None) -> int:
        key = option or ("confirm-spend-%d" % ledger)
        view = decisions.ask(
            self.cfg, self.db, requester="orch", lot=str(self.lot["id"]),
            question="Confirmer la revue de la ressource récurrente #%d ?" % ledger,
            options=["%s=confirmer la revue" % key],
            needs_signature=needs_signature, now=now)
        decisions.answer(
            self.cfg, self.db, view["id"], key, human=ALICE, channel="cli",
            key_path=self.owner_key if needs_signature else None, now=now)
        return int(view["id"])

    def link_stop(self, ledger: int, stop: dict) -> list:
        return self.db.query(
            "SELECT * FROM ameesh_spend_request_stop(%s::bigint, %s::text, "
            "%s::text, %s::date, %s::text)",
            (ledger, stop["action_id"], ALICE, None, "test"))


    def register(self, *, month: int = 50, day: int = 10, providers=("quota",),
                 purposes=("quota-reset",), max_amount: int = 50,
                 horizon: int = 24, limits=None) -> int:
        receipt = self.alice.receipt(make_request(
            ALICE, standing=grant_value(month=month, day=day, providers=providers,
                                        purposes=purposes, max_amount=max_amount)))
        verdict, grant = receipts.register_standing(
            self.db, receipt, POLICY, registered_by="porte",
            limits=limits if limits is not None else policy(horizon=horizon))
        self.assertTrue(verdict.ok, verdict.reason)
        return int(grant)

    def legacy(self) -> int:
        """Un grant enregistré avant la dimension dépense (canon sans règle)."""
        receipt = self.alice.receipt(make_request(
            ALICE, standing=standing(operations=["refund"], max_amount=50,
                                     until=int(time.time()) + 1700,
                                     beneficiary=AGENT)))
        verdict, grant = receipts.register_standing(
            self.db, receipt, POLICY, registered_by="porte",
            limits=policy(canon_month=0, canon_day=0, providers=(),
                          purposes=(), horizon=0))
        self.assertTrue(verdict.ok, verdict.reason)
        return int(grant)

    def spend_action(self, *, amount: int = 5, recurrence: str = "daily",
                     provider: str = "quota", purpose: str = "quota-reset",
                     limits=None, **kwargs) -> dict:
        kwargs.setdefault("project", "demo")
        kwargs.setdefault("target", "fournisseur:quota")
        return actions.propose(
            self.db, self.noop, operation="refund", proposed_by=AGENT,
            amount=amount, currency="EUR", limits=limits,
            args={"spend": {"recurrence": recurrence, "provider": provider,
                            "purpose": purpose}}, **kwargs)

    def bound(self, action: dict) -> dict:
        return actions.approve(self.db, action["action_id"], None, standing=True,
                               by="porte")

    def launch(self, action: dict) -> dict:
        return actions.execute(self.db, action["action_id"], self.noop, by="porte")

    def ledger_count(self) -> int:
        return int(self.db.query("SELECT count(*)::int AS n FROM spend_ledger")[0]["n"])

    def confirmations(self) -> list:
        return self.db.query(
            "SELECT decision_id, ledger_id, signed_at, confirmed_at, confirmed_by "
            "FROM spend_review_confirmations ORDER BY decision_id")

    # -- enregistrement ----------------------------------------------------
    def test_grant_enregistre_les_plafonds_et_l_horizon(self):
        grant = self.register(month=50, day=10, max_amount=50, horizon=24)
        row = receipts.get_standing(self.db, grant)
        self.assertEqual((row["max_per_month"], row["max_per_day"],
                          row["review_horizon_hours"]), (50, 10, 24))
        self.assertEqual(row["spend_providers"], ["quota"])
        self.assertEqual(row["spend_purposes"], ["quota-reset"])
        listed = receipts.list_standing(self.db)
        self.assertEqual(listed[0]["spend"], SPEND)

    def test_horizon_absent_refuse_a_l_enregistrement(self):
        receipt = self.alice.receipt(make_request(ALICE, standing=grant_value()))
        verdict, grant = receipts.register_standing(
            self.db, receipt, POLICY, registered_by="porte",
            limits=policy(horizon=0))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, receipts.POLICY)
        self.assertIn("review_horizon_hours", verdict.reason)
        self.assertIsNone(grant)
        self.assertEqual(self.ledger_count(), 0)

    # -- plafond journalier ------------------------------------------------
    def test_plafond_journalier_refuse_nommement(self):
        self.register(month=50, day=10)
        first = self.spend_action(amount=6, recurrence="daily")
        self.bound(first)
        self.launch(first)
        second = self.spend_action(amount=5, recurrence="daily")
        self.bound(second)
        with self.assertRaises(actions.ActionError) as ctx:
            self.launch(second)
        self.assertEqual(ctx.exception.code, "spend_daily_cap")
        # rien n'est réservé ni écrit par le refus
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(actions.get(self.db, second["action_id"])["state"], "approved")

    # -- projection mensuelle ---------------------------------------------
    def test_projection_mensuelle_bloque_avant_le_plafond(self):
        grant = self.register(month=50, day=50, max_amount=50, horizon=72)
        first = self.spend_action(amount=10, recurrence="daily")
        self.bound(first)
        self.launch(first)
        state = actions.spend_state(self.db, grant)
        # déjà dépensé (10 aujourd'hui) + 3 jours de ressource en cours
        self.assertEqual((state["month_spent"], state["ongoing_daily"],
                          state["projected"], state["max_per_month"]),
                         (10, 10, 40, 50))
        # 10 déjà dépensé + 3 jours de la ressource + 11 ponctuels = 51 > 50
        once = self.spend_action(amount=11, recurrence="once")
        self.bound(once)
        with self.assertRaises(actions.ActionError) as ctx:
            self.launch(once)
        self.assertEqual(ctx.exception.code, "spend_monthly_cap")
        self.assertEqual(self.ledger_count(), 1)

    def test_plafond_total_inclut_le_futur_des_ressources_daily(self):
        """B cx2 #44218 : le TOTAL compte aussi le coût futur des daily en cours.

        Sans `v_extra`, une ressource commencée il y a 40 jours (410 déjà
        dépensés) laissait passer une candidate de 85 sous un total de 495 :
        410 + 85 = 495. Le coût de demain (10, jusqu'à la revue confirmée
        aujourd'hui) porte le total à 505 > 495 : refus nommé.
        """
        grant = self.register(month=400, day=400, max_amount=495, horizon=24)
        daily = self.spend_action(amount=10, recurrence="daily")
        self.bound(daily)
        self.launch(daily)
        today = spend.utc_today()
        self.db.execute(
            "UPDATE spend_ledger SET started_on = %s::date, "
            "started_at = now() - interval '40 days', confirmed_at = clock_timestamp() "
            "WHERE action_id = %s", (today - timedelta(days=40), daily["action_id"]))
        state = actions.spend_state(self.db, grant)
        extra = int(self.db.query(
            "SELECT ameesh_spend_projected_extra(%s::bigint, %s::date) AS n",
            (grant, today))[0]["n"])
        self.assertEqual((state["total_accrued"], extra), (410, 10))
        once = self.spend_action(amount=85, recurrence="once")
        self.bound(once)
        with self.assertRaises(actions.ActionError) as ctx:
            self.launch(once)
        self.assertEqual(ctx.exception.code, "spend_total_cap")
        self.assertEqual(self.ledger_count(), 1)
        self.assertEqual(actions.get(self.db, once["action_id"])["state"], "approved")

    # -- couverture d'un grant legacy -------------------------------------
    def test_action_de_depense_jamais_couverte_par_un_grant_legacy(self):
        """B cx2 #44218 : un grant sans dimension dépense ne couvre aucune dépense.

        Le grant est enregistré avant la dimension dépense (canon sans règle
        de dépense, `max_per_month = max_per_day = 0`) : le pré-filtre de
        couverture ne le propose plus, et la réservation sous verrou le
        refuserait `no_cover` ; ni ledger, ni dépassement.
        """
        grant = self.legacy()
        row = receipts.get_standing(self.db, grant)
        self.assertEqual((row["max_per_month"], row["max_per_day"]), (0, 0))
        action = self.spend_action(amount=5, recurrence="once")
        with self.assertRaises(actions.ActionError) as ctx:
            self.bound(action)
        self.assertEqual(ctx.exception.code, "no_cover")
        self.assertEqual(actions.get(self.db, action["action_id"])["state"], "proposed")
        # la réservation sous verrou refuse aussi, indépendamment du pré-filtre
        self.assertIsNone(receipts.standing_reserve(
            self.db, grant, action["action_id"], 5, connector="shell-noop",
            operation="refund", action_class="costly", currency="EUR",
            reserved_by="porte"))
        self.assertEqual(self.ledger_count(), 0)

    def test_un_grant_legacy_ne_masque_pas_le_grant_de_depense(self):
        """Un legacy vivant (échéance plus proche) ne capte pas la dépense."""
        legacy = self.legacy()
        grant = self.register(month=50, day=10)
        self.assertLess(receipts.get_standing(self.db, legacy)["until_ts"],
                        receipts.get_standing(self.db, grant)["until_ts"])
        action = self.spend_action(amount=5, recurrence="once")
        self.bound(action)
        self.assertEqual(actions.get(self.db, action["action_id"])["auth_grant_id"], grant)
        self.launch(action)
        self.assertEqual(self.ledger_count(), 1)

    # -- ledger source unique ---------------------------------------------
    def test_ledger_source_unique_du_deja_depense(self):
        grant = self.register(month=50, day=10)
        action = self.spend_action(amount=6, recurrence="daily")
        self.bound(action)
        self.launch(action)
        row, = self.db.query(
            "SELECT action_id, grant_id, approver, decided_by, provider, purpose, "
            "amount, currency, recurrence, state FROM spend_ledger")
        self.assertEqual(
            (row["action_id"], row["grant_id"], row["approver"], row["decided_by"],
             row["provider"], row["purpose"], row["amount"], row["currency"],
             row["recurrence"], row["state"]),
            (action["action_id"], grant, ALICE, AGENT, "quota", "quota-reset", 6,
             "EUR", "daily", "ongoing"))
        self.assertEqual(actions.spend_state(self.db, grant)["day_spent"], 6)
        # un échec certain retire la dépense du ledger, seule vérité du cumul
        self.db.execute("UPDATE spend_ledger SET state = 'failed' WHERE action_id = %s",
                        (action["action_id"],))
        self.assertEqual(actions.spend_state(self.db, grant)["day_spent"], 0)

    # -- relevé et arrêt ---------------------------------------------------
    def test_releve_et_arret_d_une_ressource(self):
        grant = self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        view = actions.recap(self.db, avatar=AGENT,
                             since=time.time() - 3600, until=time.time() + 3600)
        spending = view["spending"]
        self.assertEqual((len(spending["resources"]), spending["total_daily"],
                          spending["stop_required"]), (1, 10, 0))
        resource = spending["resources"][0]
        self.assertEqual(resource["stop_command"],
                         "ameesh action spend stop %d" % resource["id"])
        # l'horizon de revue s'allonge : la projection atteint le plafond
        self.db.execute("UPDATE standing_approvals SET review_horizon_hours = 144 "
                        "WHERE id = %s", (grant,))
        self.assertEqual(actions.spend_list(self.db, decided_by=AGENT)[0]
                         ["stop_required"], True)
        outcome = actions.spend_request_stop(
            self.db, resource["id"], by="human:alice", reason="quota rétabli",
            connector=self.noop)
        self.assertEqual(outcome["ledger"]["state"], "stopped")
        self.assertEqual(outcome["action"]["operation"], "stop")
        self.assertEqual(outcome["action"]["state"], "confirmed")
        self.assertEqual(self.db.query(
            "SELECT stop_reason, stopped_by FROM spend_ledger WHERE id = %s",
            (resource["id"],))[0],
            {"stop_reason": "quota rétabli", "stopped_by": "human:alice"})
        self.assertIsNone(actions.spend_request_stop(
            self.db, resource["id"], by="human:alice", connector=self.noop))
        self.assertEqual(actions.spend_list(self.db, decided_by=AGENT), [])
        now = time.time()
        self.assertEqual(actions.recap(
            self.db, avatar=AGENT, since=now - 3600,
            until=now + 3600)["spending"]["total_daily"], 0)

    # -- B1 : l'arrêt est une action du connecteur -------------------------
    def test_arret_est_une_action_liee_et_confirmee(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger, = self.db.query("SELECT id FROM spend_ledger")
        stop = actions.propose(
            self.db, self.noop, project="demo", operation="stop",
            target="fournisseur:quota",
            args={"spend_stop": {"ledger_id": int(ledger["id"]),
                                 "action_id": action["action_id"]}},
            proposed_by="human:alice")
        self.db.query(
            "SELECT * FROM ameesh_spend_request_stop(%s::bigint, %s::text, %s::text, "
            "%s::date, %s::text)",
            (int(ledger["id"]), stop["action_id"], "human:alice", None, "test"))
        self.assertEqual(self.db.query(
            "SELECT state, stop_action_id FROM spend_ledger WHERE id = %s",
            (int(ledger["id"]),))[0],
            {"state": "ongoing", "stop_action_id": stop["action_id"]})
        # la ligne ne passe stopped QU'À la confirmation du connecteur
        actions.approve(self.db, stop["action_id"], None, by="human:alice")
        actions.execute(self.db, stop["action_id"], self.noop, by="human:alice")
        row = self.db.query(
            "SELECT state, stopped_by, stop_action_id FROM spend_ledger WHERE id = %s",
            (int(ledger["id"]),))[0]
        self.assertEqual((row["state"], row["stopped_by"]),
                         ("stopped", "human:alice"))
        self.assertEqual(row["stop_action_id"], stop["action_id"])

    # -- B2 : balayage sans geste humain, zéro dépassement -----------------
    def test_balayage_arrete_avant_le_plafond_sans_geste_humain(self):
        grant = self.register(month=30, day=30, max_amount=30, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        base = spend.utc_today()
        stopped_on = None
        for offset in range(0, 6):
            day = base + timedelta(days=offset)
            month = int(self.db.query(
                "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
                (grant, day))[0]["n"])
            self.assertLessEqual(month, 30, "dépassement le jour %s" % day)
            state = self.db.query(
                "SELECT state, stopped_on FROM spend_ledger WHERE id = %s",
                (ledger,))[0]
            if state["state"] != "ongoing":
                stopped_on = state["stopped_on"]
                break
            outcome = actions.spend_sweep(self.db, today=day, connector=self.noop)
            self.assertEqual(outcome["errors"], [])
            self.assertEqual(outcome["planned"], [])
        # la revue de 24 h est dépassée le jour 1 : arrêt sans geste humain
        self.assertEqual(stopped_on, str(base + timedelta(days=1)))
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, base + timedelta(days=5)))[0]["n"]), 20)

    def test_balayage_arrete_avant_le_plafond_malgre_confirmations(self):
        grant = self.register(month=30, day=30, max_amount=30, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        base = spend.utc_today()
        stopped = None
        for offset in range(0, 5):
            day = base + timedelta(days=offset)
            if offset <= 2:
                self.confirm_at(ledger, day)
            month = int(self.db.query(
                "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
                (grant, day))[0]["n"])
            self.assertLessEqual(month, 30, "dépassement le jour %s" % day)
            outcome = actions.spend_sweep(self.db, today=day, connector=self.noop)
            self.assertEqual(outcome["errors"], [])
            if outcome["stopped"]:
                stopped = (day, month)
                break
        # jour 2 : 30 dépensés, la ressource coûterait 40 le jour 3 ; arrêt
        self.assertEqual(stopped, (base + timedelta(days=2), 30))
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, base + timedelta(days=4)))[0]["n"]), 30)

    def test_confirmation_humaine_prolonge_la_revue(self):
        grant = self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        base = spend.utc_today()
        # jour 0 : la revue est demain, pas d'arrêt
        self.assertEqual(actions.spend_sweep(
            self.db, today=base, connector=self.noop)["stopped"], [])
        # jours 1 et 2 : l'owner confirme, la revue est repoussée
        for offset in (1, 2):
            day = base + timedelta(days=offset)
            self.confirm_at(ledger, day)
            outcome = actions.spend_sweep(self.db, today=day, connector=self.noop)
            self.assertEqual(outcome["errors"], [])
            self.assertEqual(outcome["stopped"], [])
        # jour 3 : plus de confirmation, le balayage arrête avant le jour 4
        day = base + timedelta(days=3)
        outcome = actions.spend_sweep(self.db, today=day, connector=self.noop)
        self.assertEqual(len(outcome["stopped"]), 1)
        row = self.db.query(
            "SELECT state, stopped_on FROM spend_ledger WHERE id = %s",
            (ledger,))[0]
        self.assertEqual((row["state"], row["stopped_on"]), ("stopped", str(day)))
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, day))[0]["n"]), 40)

    # -- B2 : cumul par grant, jamais ligne par ligne -----------------------
    def test_balayage_cumule_les_ressources_du_meme_grant(self):
        """Deux daily de 10 sous 50, confirmées à J1 : la projection vaut 60.

        Le balayage arrête UNE ressource (pas les deux, la seconde tient le
        mois), puis la seconde à J2 faute de confirmation : le mois ne
        dépasse jamais 50, alors qu'un contrôle ligne par ligne aurait laissé
        filer 60.
        """
        grant = self.register(month=50, day=50, max_amount=50, horizon=24)
        launched = [self.spend_action(amount=10, recurrence="daily")
                    for _ in range(2)]
        for action in launched:
            self.bound(action)
            self.launch(action)
        ids = [int(row["id"]) for row in self.db.query(
            "SELECT id FROM spend_ledger ORDER BY id")]
        self.assertEqual(len(ids), 2)
        base = spend.utc_today()
        first = actions.spend_sweep(self.db, today=base, connector=self.noop)
        self.assertEqual((first["errors"], first["stopped"]), ([], []))
        day1 = base + timedelta(days=1)
        for ledger in ids:
            self.confirm_at(ledger, day1)
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, day1))[0]["n"]), 40)
        second = actions.spend_sweep(self.db, today=day1, connector=self.noop)
        self.assertEqual(second["errors"], [])
        self.assertEqual([item["ledger_id"] for item in second["stopped"]], [ids[0]])
        # une seule ligne arrêtée : 40 dépensés + 10 de la ressource gardée
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, day1))[0]["n"]), 40)
        day2 = base + timedelta(days=2)
        third = actions.spend_sweep(self.db, today=day2, connector=self.noop)
        self.assertEqual(third["errors"], [])
        self.assertEqual([item["ledger_id"] for item in third["stopped"]], [ids[1]])
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, day2))[0]["n"]), 50)
        self.assertEqual(int(self.db.query(
            "SELECT ameesh_spend_month_spent(%s::bigint, %s::date) AS n",
            (grant, base + timedelta(days=5)))[0]["n"]), 50)

    # -- B3 : un arrêt ne vise qu'une ressource -----------------------------
    def test_arret_refuse_une_autre_ressource_et_ne_se_reutilise_pas(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        launched = [self.spend_action(amount=10, recurrence="daily")
                    for _ in range(2)]
        for action in launched:
            self.bound(action)
            self.launch(action)
        ids = [int(row["id"]) for row in self.db.query(
            "SELECT id FROM spend_ledger ORDER BY id")]

        def stop_for(ledger: int, action: dict, *, target="fournisseur:quota",
                     project="demo") -> dict:
            return actions.propose(
                self.db, self.noop, project=project, operation="stop",
                target=target,
                args={"spend_stop": {"ledger_id": ledger,
                                     "action_id": action["action_id"]}},
                proposed_by=ALICE)

        # cible, projet, args ou connecteur différents : aucun lien
        self.assertEqual(self.link_stop(
            ids[0], stop_for(ids[0], launched[0], target="fournisseur:autre")), [])
        self.assertEqual(self.link_stop(
            ids[0], stop_for(ids[0], launched[0], project="autre")), [])
        self.assertEqual(self.link_stop(
            ids[0], stop_for(ids[1], launched[1])), [])
        with mock.patch.object(self.noop, "name", "autre"):
            foreign = stop_for(ids[0], launched[0])
        self.assertEqual(self.link_stop(ids[0], foreign), [])
        self.assertEqual(int(self.db.query(
            "SELECT count(*)::int AS n FROM spend_ledger "
            "WHERE stop_action_id IS NOT NULL")[0]["n"]), 0)
        # l'arrêt exact se lie une fois, puis ne peut plus viser l'autre ligne
        stop = stop_for(ids[0], launched[0])
        self.assertEqual(len(self.link_stop(ids[0], stop)), 1)
        self.assertEqual(self.link_stop(ids[1], stop), [])
        with self.assertRaises(db_mod.DbError) as caught:
            self.db.execute(
                "UPDATE spend_ledger SET stop_action_id = %s WHERE id = %s",
                (stop["action_id"], ids[1]))
        self.assertEqual(caught.exception.sqlstate, db_mod.UNIQUE_VIOLATION)
        self.assertEqual(int(self.db.query(
            "SELECT count(*)::int AS n FROM spend_ledger "
            "WHERE stop_action_id = %s", (stop["action_id"],))[0]["n"]), 1)

    # -- B4 : décision signée, heure de la base -----------------------------
    def test_confirmation_par_decision_signe_de_l_owner(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        # une réponse non signée ne confirme rien
        unsigned = self.signed_decision(ledger, needs_signature=False)
        with self.assertRaises(spend.SpendError) as caught:
            actions.spend_confirm(self.db, ledger, decision=unsigned)
        self.assertEqual(caught.exception.code, "spend_confirm")
        # une décision signée pour une AUTRE ressource ne confirme pas celle-ci
        wrong = self.signed_decision(ledger,
                                     option="confirm-spend-%d" % (ledger + 1))
        with self.assertRaises(spend.SpendError) as caught:
            actions.spend_confirm(self.db, ledger, decision=wrong)
        self.assertEqual(caught.exception.code, "spend_confirm")
        # la décision exacte, signée, confirme à l'heure réelle de la base
        decision = self.signed_decision(ledger)
        before = time.time()
        row = actions.spend_confirm(self.db, ledger, decision=decision)
        self.assertEqual((row["id"], row["state"], row["confirmed_decision_id"]),
                         (ledger, "ongoing", decision))
        stored = self.db.query(
            "SELECT confirmed_by, confirmed_decision_id, "
            "extract(epoch from confirmed_at)::float8 AS ts "
            "FROM spend_ledger WHERE id = %s", (ledger,))[0]
        self.assertEqual((stored["confirmed_by"], stored["confirmed_decision_id"]),
                         (ALICE, decision))
        self.assertLessEqual(abs(stored["ts"] - before), 60)
        self.assertEqual([(row["decision_id"], row["ledger_id"])
                          for row in self.confirmations()], [(decision, ledger)])

    def test_rejeu_decision_ne_rafraichit_pas_la_revue(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        first = self.signed_decision(ledger)
        row = actions.spend_confirm(self.db, ledger, decision=first)
        stamp = float(self.db.query(
            "SELECT extract(epoch from confirmed_at)::float8 AS ts "
            "FROM spend_ledger WHERE id = %s", (ledger,))[0]["ts"])
        # rejeu simple : même ligne, même date, aucun second usage durable
        replay = actions.spend_confirm(self.db, ledger, decision=first)
        self.assertEqual((replay["confirmed_at"], replay["confirmed_decision_id"]),
                         (row["confirmed_at"], first))
        self.assertEqual(len(self.confirmations()), 1)
        # une décision plus récente ouvre une nouvelle fenêtre ; rejouer
        # l'ancienne ensuite ne la remplace pas et ne rafraîchit rien
        second = self.signed_decision(ledger, now=max(time.time(), stamp + 0.05))
        fresh = actions.spend_confirm(self.db, ledger, decision=second)
        self.assertEqual(fresh["confirmed_decision_id"], second)
        self.assertGreaterEqual(float(self.db.query(
            "SELECT extract(epoch from confirmed_at)::float8 AS ts "
            "FROM spend_ledger WHERE id = %s", (ledger,))[0]["ts"]), stamp)
        replay = actions.spend_confirm(self.db, ledger, decision=first)
        self.assertEqual((replay["confirmed_at"], replay["confirmed_decision_id"]),
                         (fresh["confirmed_at"], second))
        self.assertEqual(len(self.confirmations()), 2)

    def test_decision_ne_confirme_qu_une_seule_ressource(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        launched = [self.spend_action(amount=10, recurrence="daily")
                    for _ in range(2)]
        for action in launched:
            self.bound(action)
            self.launch(action)
        ids = [int(row["id"]) for row in self.db.query(
            "SELECT id FROM spend_ledger ORDER BY id")]
        decision = self.signed_decision(ids[0])
        actions.spend_confirm(self.db, ids[0], decision=decision)
        with self.assertRaises(spend.SpendError) as caught:
            actions.spend_confirm(self.db, ids[1], decision=decision)
        self.assertEqual(caught.exception.code, "spend_confirm")
        rows = {int(row["id"]): row["confirmed_decision_id"]
                for row in self.db.query(
                    "SELECT id, confirmed_decision_id FROM spend_ledger")}
        self.assertEqual(rows, {ids[0]: decision, ids[1]: None})
        self.assertEqual([(row["decision_id"], row["ledger_id"])
                          for row in self.confirmations()], [(decision, ids[0])])

    def test_signature_anterieure_a_la_derniere_confirmation_refusee(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        first = self.signed_decision(ledger)
        actions.spend_confirm(self.db, ledger, decision=first)
        # la dernière confirmation est postérieure à la nouvelle signature :
        # son premier usage ne peut pas repousser la revue
        self.db.execute(
            "UPDATE spend_ledger SET confirmed_at = now() + interval '1 hour' "
            "WHERE id = %s", (ledger,))
        older = self.signed_decision(ledger)
        self.assertIsNone(actions.spend_confirm(self.db, ledger, decision=older))
        current = self.db.query(
            "SELECT confirmed_decision_id FROM spend_ledger WHERE id = %s",
            (ledger,))[0]
        self.assertEqual(current["confirmed_decision_id"], first)
        self.assertEqual([row["decision_id"] for row in self.confirmations()],
                         [first])

    def test_premier_usage_tardif_refuse(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        decision = self.signed_decision(ledger)
        # même décision valide, mais signée il y a deux heures : le garde SQL
        # refuse un premier usage tardif (fenêtre de signature de 1 h)
        rows = self.db.query(
            "SELECT * FROM ameesh_spend_confirm(%s::bigint, %s::text, %s::bigint, "
            "now() - interval '2 hours')", (ledger, ALICE, decision))
        self.assertEqual(rows, [])
        self.assertEqual(self.confirmations(), [])
        row = self.db.query(
            "SELECT confirmed_at, confirmed_decision_id FROM spend_ledger "
            "WHERE id = %s", (ledger,))[0]
        self.assertEqual((row["confirmed_at"], row["confirmed_decision_id"]),
                         (None, None))

    def test_rejeu_concurrent_une_seule_confirmation(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        decision = self.signed_decision(ledger)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []
        lock = threading.Lock()

        def attempt() -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                try:
                    result = actions.spend_confirm(db, ledger, decision=decision)
                    outcome = ("ok", result)
                except Exception as exc:  # noqa: BLE001 - le fil rapporte l'échec
                    outcome = ("error", exc)
            finally:
                db.close()
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([kind for kind, _ in outcomes], ["ok", "ok"])
        stamps = {str(result["confirmed_at"]) for _, result in outcomes}
        self.assertEqual(len(stamps), 1)
        self.assertEqual(len(self.confirmations()), 1)

    def test_cli_confirmation_sans_by_ni_on(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        for flag, value in (("--by", "human:alice"), ("--on", "2026-11-20")):
            with self.subTest(flag=flag):
                proc = self.mesh("action", "spend", "confirm", str(ledger),
                                 "--decision", "1", flag, value, env=self.env())
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("unrecognized arguments", proc.stderr)

    # -- B1 : le déclencheur quotidien appelle bien le balayage -------------
    def test_le_declencheur_quotidien_appelle_le_balayage(self):
        self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        ledger = int(self.db.query("SELECT id FROM spend_ledger")[0]["id"])
        day = spend.utc_today() + timedelta(days=1)
        proc = self.mesh("action", "spend", "sweep", "--today", str(day),
                         "--dry-run", "--json", env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcome = json.loads(proc.stdout)
        self.assertEqual([item["ledger_id"] for item in outcome["planned"]],
                         [ledger])
        self.assertIn("revue humaine", outcome["planned"][0]["reason"])

    # -- limites dures à la proposition -----------------------------------
    def test_limites_dures_a_la_proposition(self):
        limits = policy(canon_month=50, canon_day=10)
        self.refused("spend_recurrence", self.spend_action,
                     recurrence="monthly", limits=limits)
        self.refused("spend_provider", self.spend_action, provider="stripe",
                     limits=limits)
        self.refused("spend_purpose", self.spend_action, purpose="autre",
                     limits=limits)
        self.refused("spend_daily_cap", self.spend_action, amount=11, limits=limits)
        self.refused("spend_amount", self.spend_action, amount=0, limits=limits)
        # un canon sans règle de dépense n'autorise aucun argent
        self.refused("spend_policy", self.spend_action,
                     limits=policy(canon_month=0, canon_day=0))

    # -- CLI du relevé -----------------------------------------------------
    def test_cli_releve_liste_et_arret(self):
        grant = self.register(month=50, day=50, max_amount=50, horizon=24)
        action = self.spend_action(amount=10, recurrence="daily")
        self.bound(action)
        self.launch(action)
        proc = self.mesh("action", "recap", "--avatar", AGENT, "--json",
                         env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        spending = json.loads(proc.stdout)["spending"]
        self.assertEqual((spending["total_daily"], spending["stop_required"]), (10, 0))
        ledger = spending["resources"][0]["id"]
        proc = self.mesh("action", "recap", "--avatar", AGENT, env=self.env())
        self.assertIn("spending (1 resource, 10 EUR/day):", proc.stdout)
        self.assertIn("ledger #%d" % ledger, proc.stdout)
        proc = self.mesh("action", "spend", "list", env=self.env())
        self.assertIn("ledger #%d" % ledger, proc.stdout)
        proc = self.mesh("action", "show", action["action_id"], env=self.env())
        self.assertIn("spend     : daily quota / quota-reset, 10 EUR", proc.stdout)
        proc = self.mesh("action", "spend", "state", str(grant), "--json",
                         env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["projected"], 20)
        proc = self.mesh("action", "spend", "stop", str(ledger),
                         "--reason", "quota rétabli", "--by", "human:alice",
                         "--noop-dir", self.noop_dir, env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stopped", proc.stdout)
        proc = self.mesh("action", "spend", "list", env=self.env())
        self.assertIn("resources (0)", proc.stdout)

    # -- concurrence -------------------------------------------------------
    def test_deux_reservations_concurrentes_un_seul_ledger(self):
        grant = self.register(month=50, day=10)
        batch = [self.spend_action(amount=6, recurrence="daily") for _ in range(2)]
        barrier = threading.Barrier(len(batch))
        results: list[str] = []
        lock = threading.Lock()

        def attempt(action: dict) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                try:
                    receipts.standing_reserve(
                        db, grant, action["action_id"], 6, connector="shell-noop",
                        operation="refund", action_class="costly", currency="EUR",
                        reserved_by="porte")
                    outcome = "ok"
                except receipts.SpendRefusal as exc:
                    outcome = exc.code
            finally:
                db.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt, args=(action,))
                   for action in batch]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(results), ["ok", "spend_daily_cap"])
        self.assertEqual(self.ledger_count(), 1)


class SpendTimerTest(unittest.TestCase):
    """B1 : la minuterie quotidienne précède la bascule UTC et lance le sweep."""

    def spec(self) -> role_timers.TimerSpec:
        return role_timers.TimerSpec(
            role_timers.SPEND_PASS, role_timers.SPEND_PASS,
            role_timers.SPEND_INTERVAL_S, role_timers.SPEND_DESCRIPTION)

    def test_minuterie_du_bail_elu_avant_minuit_utc(self):
        from tests.test_6777_carte_role_timer import plan, row
        report = plan()
        item = next(t for t in report["timers"] if t["name"] == "spend-sweep")
        self.assertEqual((item["unit"], item["interval_s"], item["blocked"]),
                         ("ameesh-spend-sweep", 86400, ""))
        self.assertEqual(
            (REPO / "deploy/systemd/ameesh-spend-sweep.timer").read_text(encoding="utf-8"),
            role_timers.render_timer(self.spec()))
        self.assertEqual(
            (REPO / "deploy/systemd/ameesh-spend-sweep.service").read_text(encoding="utf-8"),
            role_timers.render_service(self.spec(), role="mesh-design"))
        timer = role_timers.render_timer(self.spec())
        self.assertIn("OnCalendar=*-*-* 23:30:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertNotIn("spend-sweep", [t["name"] for t in plan(
            role="auditeur",
            row_value=dict(row(), name="auditeur", roles=["auditeur"]))["timers"]])
        with tempfile.TemporaryDirectory() as directory:
            role_timers.install(report, directory=Path(directory), apply=False,
                                bin_path="/opt/ameesh/bin/ameesh")
            service = (Path(directory) / "ameesh-spend-sweep.service").read_text(
                encoding="utf-8")
            installed = (Path(directory) / "ameesh-spend-sweep.timer").read_text(
                encoding="utf-8")
        self.assertIn("role run mesh-design -- /opt/ameesh/bin/ameesh "
                      "action spend sweep", service)
        self.assertIn("OnCalendar=*-*-* 23:30:00 UTC", installed)


if __name__ == "__main__":
    unittest.main()

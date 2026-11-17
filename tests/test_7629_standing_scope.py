# SPDX-License-Identifier: AGPL-3.0-only
"""#7629 : portée d'une approbation permanente et récap par issue de charte.

Une approbation permanente nomme son bénéficiaire (un agent, ou l'avatar de
l'approbateur et ses clones), peut borner sa cible et cite sa ligne et sa
version de charte. La porte ne l'utilise que si l'action est proposée par ce
bénéficiaire, dans la cible, sous le plafond et avant l'échéance ; un grant
hors du plafond du canon est refusé à l'enregistrement.
"""
from __future__ import annotations

import time
import types
import unittest

from ameesh import actions, canon as canon_mod, jcs, receipts, standing_policy

from .support import standing_limits
from .test_actions import ActionsBase, ALICE, POLICY, STANDING_LIMITS
from .test_receipts import standing
from .webauthn_soft import make_request


def federation(block: dict) -> dict:
    """Un `federation.yaml` conforme OKF : le bloc vit sous `extensions.ameesh`."""
    return {"extensions": {"ameesh": {"standing_approvals": block}}}


class StandingScopeTest(ActionsBase):
    def setUp(self) -> None:
        super().setUp()
        # lignée des avatars : l'avatar et son clone éphémère
        self.db.execute(
            "INSERT INTO agent_registry (name, ephemeral, ephemeral_expires_at, created_by) "
            "VALUES ('alice-avatar', false, NULL, NULL), "
            "('alice-clone', true, now() + interval '1 hour', 'alice-avatar'), "
            "('bob-avatar', false, NULL, NULL), "
            "('bob-clone', true, now() + interval '1 hour', 'bob-avatar')")

    # -- utilitaires -------------------------------------------------------
    def register(self, *, approver=ALICE, soft=None, avatar="alice-avatar",
                 limits=None, **overrides) -> int:
        value = standing(**overrides)
        receipt = (soft or self.alice).receipt(make_request(approver, standing=value))
        verdict, grant = receipts.register_standing(
            self.db, receipt, POLICY, registered_by="porte",
            limits=limits if limits is not None else STANDING_LIMITS, avatar=avatar)
        self.assertTrue(verdict.ok, verdict.reason)
        return int(grant)

    def propose(self, *, operation="refund", proposed_by="agent:alice-avatar",
                target="client:42", amount=30, currency="EUR", **kwargs) -> dict:
        kwargs.setdefault("project", "demo")
        kwargs.setdefault("args", {"n": 1})
        return actions.propose(
            self.db, self.noop, operation=operation, proposed_by=proposed_by,
            target=target, amount=amount, currency=currency, **kwargs)

    def bound(self, action: dict) -> dict:
        return actions.approve(self.db, action["action_id"], None, standing=True, by="porte")

    def refused_no_cover(self, action: dict) -> None:
        with self.assertRaises(actions.ActionError) as ctx:
            self.bound(action)
        self.assertEqual(ctx.exception.code, actions.NO_COVER)

    # -- bénéficiaire ------------------------------------------------------
    def test_avatar_de_a_ne_couvre_pas_l_avatar_de_b(self) -> None:
        grant = self.register(beneficiary="avatar")
        mine = self.propose()
        bound = self.bound(mine)
        self.assertEqual((bound["state"], bound["auth_kind"], bound["auth_grant_id"]),
                         ("approved", "standing", grant))
        self.refused_no_cover(self.propose(proposed_by="agent:bob-avatar"))
        self.refused_no_cover(self.propose(proposed_by="agent:bob-clone"))
        self.refused_no_cover(self.propose(proposed_by="agent:deepseek7"))

    def test_avatar_couvre_ses_clones_par_la_lignee(self) -> None:
        self.register(beneficiary="avatar")
        clone = self.propose(proposed_by="agent:alice-clone")
        self.assertEqual(self.bound(clone)["auth_grant_id"], 1)
        # un clone de B n'est pas un clone de l'avatar de A
        self.refused_no_cover(self.propose(proposed_by="agent:bob-clone"))

    def test_avatar_ne_couvre_pas_un_agent_permanent(self) -> None:
        # un agent permanent créé par l'avatar n'est PAS un clone (spec §8.3)
        self.db.execute(
            "INSERT INTO agent_registry (name, ephemeral, ephemeral_expires_at, created_by) "
            "VALUES ('alice-dev', false, NULL, 'alice-avatar')")
        self.register(beneficiary="avatar")
        self.refused_no_cover(self.propose(proposed_by="agent:alice-dev"))

    def test_lignee_traversant_un_agent_permanent_ne_couvre_pas(self) -> None:
        self.db.execute(
            "INSERT INTO agent_registry (name, ephemeral, ephemeral_expires_at, created_by) "
            "VALUES ('alice-dev', false, NULL, 'alice-avatar'), "
            "('alice-dev-clone', true, now() + interval '1 hour', 'alice-dev')")
        self.register(beneficiary="avatar")
        # l'arête permanente rompt la lignée : son descendant n'est pas un clone
        self.refused_no_cover(self.propose(proposed_by="agent:alice-dev-clone"))
        # le clone éphémère direct de l'avatar reste couvert
        self.assertEqual(
            self.bound(self.propose(proposed_by="agent:alice-clone"))["state"], "approved")

    def test_beneficiaire_agent_nomme(self) -> None:
        self.register(beneficiary="agent:alice-avatar")
        self.assertEqual(self.bound(self.propose())["auth_kind"], "standing")
        self.refused_no_cover(self.propose(proposed_by="agent:alice-clone"))

    # -- cible -------------------------------------------------------------
    def test_action_hors_cible_non_couverte(self) -> None:
        self.register(beneficiary="avatar",
                      targets=["repo:o/r", "branch:release-*", "canal:eq-?"])
        self.assertEqual(self.bound(self.propose(target="branch:release-1"))["state"],
                         "approved")
        self.assertEqual(self.bound(self.propose(target="canal:eq-1"))["state"],
                         "approved")
        self.refused_no_cover(self.propose(target="branch:main"))
        self.refused_no_cover(self.propose(target="repo:o/autre"))
        self.refused_no_cover(self.propose(target="canal:eq-12"))

    # -- plafond, échéance, révocation ------------------------------------
    def test_action_hors_plafond_non_couverte(self) -> None:
        self.register(beneficiary="avatar", max_amount=50)
        self.refused_no_cover(self.propose(amount=60))

    def test_action_apres_echeance_non_couverte(self) -> None:
        grant = self.register(beneficiary="avatar")
        self.db.execute(
            "UPDATE standing_approvals SET valid_until = now() - interval '1 second' "
            "WHERE id = %s", (grant,))
        self.refused_no_cover(self.propose())

    def test_action_apres_revocation_non_couverte(self) -> None:
        grant = self.register(beneficiary="avatar")
        self.assertTrue(receipts.revoke_standing(self.db, grant, by=ALICE))
        self.refused_no_cover(self.propose())

    # -- plafond du canon à l'enregistrement ------------------------------
    def test_approbation_hors_plafond_refusee_a_l_enregistrement(self) -> None:
        cases = {
            "montant": (standing_limits(max_amount=50),
                        {"beneficiary": "avatar", "max_amount": 100}),
            "durée": (standing_limits(max_duration_hours=1),
                      {"beneficiary": "avatar", "until": int(time.time()) + 7200}),
            "sans bénéficiaire": (standing_limits(allow_missing_beneficiary=False),
                                  {}),
            "sans charte": (standing_limits(require_charter=True),
                            {"beneficiary": "avatar"}),
            "sans cible": (standing_limits(require_targets=True),
                           {"beneficiary": "avatar"}),
        }
        for label, (limits, overrides) in cases.items():
            with self.subTest(label=label):
                receipt = self.alice.receipt(make_request(
                    ALICE, standing=standing(**overrides)))
                verdict, grant = receipts.register_standing(
                    self.db, receipt, POLICY, registered_by="porte", limits=limits,
                    avatar="alice-avatar")
                self.assertEqual((verdict.code, grant), (receipts.POLICY, None),
                                 verdict.reason)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM standing_approvals")[0]["n"], 0)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM mesh_consumed_nonces")[0]["n"], 0)

    def test_approbation_sous_le_plafond_avec_charte(self) -> None:
        limits = standing_limits(require_charter=True, require_targets=True)
        grant = self.register(beneficiary="avatar", charter_line="L12 : fait seul",
                              charter_version="v3", targets=["client:*"], limits=limits)
        row = receipts.get_standing(self.db, grant)
        self.assertEqual((row["beneficiary_kind"], row["beneficiary"],
                          row["beneficiary_clones"]),
                         ("avatar", "agent:alice-avatar", True))
        self.assertEqual((row["charter_line"], row["charter_version"]),
                         ("L12 : fait seul", "v3"))
        self.assertEqual(row["targets"], ["client:*"])

    # -- version de charte et fil -----------------------------------------
    def test_action_couverte_enregistre_la_version_de_charte(self) -> None:
        self.register(beneficiary="avatar", charter_line="L12 : fait seul",
                      charter_version="v3", limits=standing_limits(require_charter=True))
        action = self.propose()
        before = action["digest"]
        bound = self.bound(action)
        self.assertEqual(bound["policy_version"], "v3")
        self.assertNotEqual(bound["digest"], before)
        self.assertEqual(bound["digest"], actions.digest(bound))
        self.assertIn("charter L12 : fait seul (v3)", bound["last_note"])
        # le fil dit quelle approbation a couvert, pour quel bénéficiaire
        self.assertIn("for agent:alice-avatar (avatar, clones included)",
                      bound["last_note"])

    def test_action_show_et_recap_cli(self) -> None:
        self.register(beneficiary="avatar", charter_line="L12 : fait seul",
                      charter_version="v3", limits=standing_limits(require_charter=True))
        action = self.propose()
        self.bound(action)
        shown = self.mesh("action", "show", action["action_id"], env=self.env())
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("standing grant #", shown.stdout)
        self.assertIn("agent:alice-avatar", shown.stdout)
        self.assertIn("charter L12 : fait seul (v3)", shown.stdout)
        recap = self.mesh("action", "recap", "--avatar", "alice-avatar", "--since", "1h",
                          env=self.env())
        self.assertEqual(recap.returncode, 0, recap.stderr)
        self.assertIn("fait_seul (1)", recap.stdout)
        self.assertIn(action["action_id"], recap.stdout)

    # -- récap -------------------------------------------------------------
    def test_recap_range_les_actions_par_issue(self) -> None:
        self.register(beneficiary="avatar", charter_line="L12 : fait seul",
                      charter_version="v3", limits=standing_limits(require_charter=True))
        done = self.propose()
        self.bound(done)
        submitted = self.propose()
        actions.approve(self.db, submitted["action_id"], self.receipt(submitted),
                        policy=POLICY, by=ALICE)
        refused = self.propose()
        actions.approve(self.db, refused["action_id"],
                        self.receipt(refused, decision="deny"), policy=POLICY, by=ALICE)
        pending = self.propose()
        free = self.propose(operation="note", amount=None, currency=None)
        since = time.time() - 60
        view = actions.recap(self.db, avatar="alice-avatar", since=since,
                             until=time.time() + 60)
        self.assertEqual(view["avatar"], "agent:alice-avatar")
        ids = {issue: [row["action_id"] for row in rows]
               for issue, rows in view["issues"].items()}
        self.assertEqual(ids["fait_seul"], [done["action_id"]])
        self.assertEqual(ids["soumis"], [submitted["action_id"], pending["action_id"]])
        self.assertEqual(ids["refuse"], [refused["action_id"]])
        self.assertIn(free["action_id"], ids["autre"])
        self.assertEqual(view["counts"],
                         {"fait_seul": 1, "soumis": 2, "refuse": 1, "autre": 1})
        # un autre avatar ne voit pas ces actions
        other = actions.recap(self.db, avatar="agent:bob-avatar", since=since,
                             until=time.time() + 60)
        self.assertEqual(other["counts"],
                         {"fait_seul": 0, "soumis": 0, "refuse": 0, "autre": 0})

    # -- compatibilité N-1 -------------------------------------------------
    def test_ancienne_signature_n_elargit_rien(self) -> None:
        """L'écrivain N-1 (`ameesh_standing_register` à 8 arguments) enregistre
        un grant `none` : il ne couvre RIEN, jamais un élargissement muet."""
        value = standing()
        receipt = self.alice.receipt(make_request(ALICE, standing=value))
        verdict = receipts.verify_receipt(self.db, receipt, POLICY, kind="standing")
        self.assertTrue(verdict.ok, verdict.reason)
        parsed = receipts.parse_receipt(receipt)
        request = parsed.request
        rows = self.db.query(
            "SELECT ameesh_standing_register(%s::text, %s::text, %s::text, %s::bigint, "
            "%s::text, %s::text, %s::jsonb, %s::text, %s::bigint, %s::text, %s::bigint, "
            "%s::bigint, %s::bigint, %s::integer, %s::text) AS id",
            (ALICE, request["nonce"], verdict.challenge, verdict.authenticator_id,
             jcs.dumps(parsed.document), value["connector"],
             jcs.dumps(value["operations"]), value["class"], value["max_amount"],
             value["currency"], request["exp"], request["iat"], value["until"], 120,
             "ancien-ecrivain"))
        grant = int(rows[0]["id"])
        self.assertEqual(receipts.get_standing(self.db, grant)["beneficiary_kind"], "none")
        self.assertIsNone(receipts.standing_reserve(
            self.db, grant, self.propose()["action_id"], 30, connector=value["connector"],
            operation="refund", action_class=value["class"], currency=value["currency"]))


class StandingPolicyTest(unittest.TestCase):
    """Le bloc `standing_approvals` du canon : lecture stricte et plafonds."""

    BLOCK = {
        "schema": "ameesh-standing-approvals/1",
        "rules": [{
            "connector": "shell-noop", "operations": ["refund"], "approvers": [ALICE],
            "max_duration_hours": 24, "max_amount": 100, "currency": "EUR",
            "allow_missing_beneficiary": False, "require_charter": True,
            "require_targets": False,
        }],
    }

    def check(self, policy, **overrides):
        value = {"connector": "shell-noop", "operations": ["refund"], "class": "costly",
                 "max_amount": 50, "currency": "EUR", "until": 1000 + 3600,
                 "beneficiary": "avatar", "charter_line": "L12", "charter_version": "v1"}
        value.update(overrides)
        return policy.check(approver=ALICE, standing=value, iat=1000)

    def test_bloc_absent_ou_vide_ne_permet_rien(self):
        self.assertFalse(standing_policy.empty())
        self.assertFalse(standing_policy.from_federation({}))
        self.assertIn("no standing-approval rule",
                      self.check(standing_policy.empty()))

    def test_bloc_valide(self):
        policy = standing_policy.from_federation(federation(self.BLOCK))
        self.assertTrue(policy)
        self.assertIsNone(self.check(policy))
        self.assertEqual(policy.describe()[0]["max_duration_hours"], 24)
        self.assertIn("no standing-approval rule",
                      self.check(policy, connector="git"))

    def test_plafonds_du_canon(self):
        policy = standing_policy.from_federation(federation(self.BLOCK))
        self.assertIn("too long", self.check(policy, until=25 * 3600))
        self.assertIn("above the canon cap", self.check(policy, max_amount=101))
        self.assertIn("must name its beneficiary", self.check(policy, beneficiary=None))
        self.assertIn("must cite its charter", self.check(policy, charter_line=""))
        self.assertIn("must bound its target",
                      self.check(standing_policy.parse(dict(
                          self.BLOCK,
                          rules=[dict(self.BLOCK["rules"][0], require_targets=True)])),
                          targets=[]))

    def test_bloc_illisible_refuse(self):
        for block in ({"schema": "autre", "rules": self.BLOCK["rules"]},
                      {"schema": "ameesh-standing-approvals/1", "rules": []},
                      {"schema": "ameesh-standing-approvals/1",
                       "rules": [dict(self.BLOCK["rules"][0], extra=1)]},
                      {"schema": "ameesh-standing-approvals/1",
                       "rules": [dict(self.BLOCK["rules"][0], max_amount=10,
                                      currency=None)]}):
            with self.subTest(block=block):
                with self.assertRaises(standing_policy.StandingPolicyError):
                    standing_policy.parse(block)

    def test_bloc_hors_extensions_refuse(self):
        # le schéma OKF n'admet aucune clé libre à la racine : refus nommé
        with self.assertRaises(standing_policy.StandingPolicyError) as ctx:
            standing_policy.from_federation({"standing_approvals": self.BLOCK})
        self.assertIn("extensions.ameesh", str(ctx.exception))
        self.assertFalse(standing_policy.from_federation({"extensions": {"autre": {}}}))
        # un conteneur mal formé est refusé, jamais lu comme une absence
        for value in ({"extensions": []}, {"extensions": {"ameesh": []}}):
            with self.subTest(federation=value):
                with self.assertRaises(standing_policy.StandingPolicyError):
                    standing_policy.from_federation(value)


class CanonAvatarTest(unittest.TestCase):
    """`Canon.standing_policy` et la résolution de l'avatar de l'approbateur."""

    def test_politique_et_avatar(self):
        canon = canon_mod.Canon(root="/nonexistent", federation=federation(
            StandingPolicyTest.BLOCK))
        self.assertTrue(canon.standing_policy())
        card = types.SimpleNamespace(
            title="alice-avatar", responsible=ALICE,
            fiche=types.SimpleNamespace(data={"avatar": {"children": "none"}}))
        other = types.SimpleNamespace(
            title="bob-avatar", responsible="human:bob",
            fiche=types.SimpleNamespace(data={"avatar": {}}))
        not_avatar = types.SimpleNamespace(
            title="alice-dev", responsible=ALICE, fiche=types.SimpleNamespace(data={}))
        canon.agents = [other, not_avatar, card]
        self.assertIs(canon.avatar_of(ALICE), card)
        self.assertIsNone(canon.avatar_of("human:carol"))


# SPDX-License-Identifier: AGPL-3.0-only
"""#7684 : le connecteur `journal`, les gestes réservés par la porte.

Le connecteur ne fait pas le geste : il inscrit la preuve d'exécution que le
bureau fournit (`run`, `sha`, `resource`, `ref`) après son propre geste. La
vue `reserved_gestures.journal` relit cette preuve, le grant et sa
réservation, et signale un déploiement de production que le journal externe
du module montre sans action de porte.

La signature d'une décision passe par ce même connecteur (`decision-sign`),
sous un grant dont la réservation est liée à l'empreinte de la réponse ; une
décision sur la clé owner ou la provenance (`decision-sign-key`) n'est jamais
couverte.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import (actions, actions_cli, authority, connectors, decisions,
                    module_actions, receipts, reserved_gestures, signing, storage)
from ameesh.connectors import Action
from ameesh.modules import registry
from ameesh.modules.dev import journal as journal_module

from .support import apply_authenticators, standing_limits
from .test_7631_accord_cle_fichier import (ALICE, AVATAR, POLICY, AccordBase,
                                           member)
from .test_l124_decisions import _Base
from .webauthn_soft import SoftEd25519, make_request

CLASSES = {"journal": {"prod-deploy": "irreversible", "spend": "costly",
                       "publish": "irreversible", "decision-sign": "irreversible",
                       "decision-sign-key": "irreversible"}}


def journal_policy(db, project: str) -> None:
    """La déclaration de classe du canon pour le connecteur `journal`."""
    rules = [{"kind": module_actions.RULE, "types": [], "connectors": CLASSES}]
    db.execute(
        "INSERT INTO project_modules (project, module, canon, types, schemas, rules) "
        "VALUES (%s, 'dev', '', '[]'::jsonb, '{}'::jsonb, %s::jsonb) "
        "ON CONFLICT (project, module) DO UPDATE SET rules = EXCLUDED.rules",
        (project, json.dumps(rules)))


class ConnectorTest(unittest.TestCase):
    """Déclaration, classification et preuve : aucune base."""

    def test_declared_by_dev_and_classified(self):
        self.assertEqual(registry.action_connectors()["journal"][0], "dev")
        self.assertEqual(sorted(registry.reserved_gesture_detectors()), ["prod-deploy"])
        connector = connectors.get("journal")
        self.assertEqual(connector.name, "journal")
        self.assertEqual(connector.classify("prod-deploy", {}), "irreversible")
        self.assertEqual(connector.classify("spend", {}), "costly")
        self.assertEqual(connector.classify("publish", {}), "irreversible")
        self.assertEqual(connector.classify("decision-sign", {}), "irreversible")
        self.assertEqual(connector.classify("unknown", {}), "irreversible")

    def _action(self, proof=None) -> Action:
        args = {} if proof is None else {"proof": proof}
        return Action(action_id="act-1", project="demo", connector="journal",
                      operation="prod-deploy", target="prod:ameesh", args=args,
                      action_class="irreversible")

    def test_execute_without_proof_records_nothing(self):
        outcome = connectors.get("journal").execute(self._action(), "key-1")
        self.assertEqual(outcome.state, "failed")
        self.assertIn("no execution proof", outcome.detail)

    def test_execute_records_the_canonical_proof(self):
        connector = connectors.get("journal")
        outcome = connector.execute(
            self._action({"sha": "a" * 40, "run": "https://ci.example/run/7",
                          "resource": "prod:ameesh"}), "key-1")
        self.assertEqual(outcome.state, "confirmed")
        self.assertEqual(json.loads(outcome.external_ref),
                         {"run": "https://ci.example/run/7", "sha": "a" * 40,
                          "resource": "prod:ameesh"})
        self.assertIn("run=https://ci.example/run/7", outcome.detail)
        # une chaîne vaut `resource` (essai rapide)
        quick = connector.execute(self._action("deploy-42"), "key-2")
        self.assertEqual(quick.state, "confirmed")
        self.assertEqual(json.loads(quick.external_ref), {"resource": "deploy-42"})
        self.assertIsNone(connector.reconcile(self._action()))

    def test_proof_refused_when_malformed(self):
        connector = connectors.get("journal")
        for proof in ({}, {"other": "x"}, {"run": "  "}, {"run": 7},
                      {"run": "a" * (journal_module.MAX_PROOF_VALUE + 1)},
                      {"run": "bad\u0000value"}):
            with self.subTest(proof=proof):
                outcome = connector.execute(self._action(proof), "key")
                self.assertEqual(outcome.state, "failed")
                self.assertIsNone(outcome.external_ref)

    def test_standing_request_shape(self):
        until = int(time.time()) + 3600
        request = receipts.standing_request(
            approver=ALICE, connector="journal", operations=["decision-sign"],
            action_class="irreversible", until=until, beneficiary="agent:bureau",
            targets=["human:alice"], charter_line="L12 : signer les décisions",
            charter_version="v3", charter={"frame": "sign-decisions", "version": "1"},
            requested_by="agent:bureau")
        self.assertEqual(receipts.check_request(request), "standing")
        self.assertNotIn("action_id", request)
        standing = request["standing"]
        self.assertEqual((standing["connector"], standing["operations"],
                          standing["beneficiary"], standing["targets"]),
                         ("journal", ["decision-sign"], "agent:bureau", ["human:alice"]))
        self.assertEqual(standing["charter"], {"frame": "sign-decisions", "version": "1"})

    def test_cli_composes_a_standing_request(self):
        import tempfile
        directory = tempfile.mkdtemp(prefix="ameesh-7684-")
        out = os.path.join(directory, "grant.json")
        parser = actions_cli.build_parser()
        args = parser.parse_args([
            "action", "standing", "request", "--approver", ALICE,
            "--connector", "journal", "--operation", "decision-sign",
            "--class", "irreversible", "--until", "2030-01-01",
            "--beneficiary", "agent:bureau", "--target", "human:alice",
            "--charter-line", "L12", "--charter-version", "v3",
            "--charter-frame", "sign-decisions", "--frame-version", "1",
            "--requested-by", "agent:bureau", "--out", out])
        self.assertIs(args.func, actions_cli.cmd_standing_request)
        self.assertEqual(args.func(None, args), 0)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o600)
        with open(out, encoding="utf-8") as fh:
            request = json.load(fh)
        self.assertEqual(receipts.check_request(request), "standing")
        self.assertEqual(request["standing"]["connector"], "journal")
        self.assertEqual(request["standing"]["charter"],
                         {"frame": "sign-decisions", "version": "1"})

    def test_cli_proof_values(self):
        self.assertEqual(actions_cli._proof(["run=https://ci/1", "sha=abc"]),
                         {"run": "https://ci/1", "sha": "abc"})
        self.assertIsNone(actions_cli._proof(None))
        with self.assertRaises(actions.ActionError):
            actions_cli._proof(["run"])
        with self.assertRaises(actions.ActionError):
            actions_cli._proof(["run=a", "run=b"])


class JournalActionTest(AccordBase):
    """Le geste de production passe la porte, puis la preuve est inscrite."""

    def setUp(self) -> None:
        super().setUp()
        journal_policy(self.db, "demo")
        self.connector = connectors.get("journal")
        self.LIMITS = standing_limits(connectors=("journal",),
                                      operations=("prod-deploy",))

    def propose(self, *, args=None) -> dict:
        return actions.propose(self.db, self.connector, operation="prod-deploy",
                               project="demo", proposed_by="agent:bureau",
                               target="prod:ameesh",
                               args=args or {"version": "1.58.0"},
                               action_class="irreversible")

    def approve(self, action, *, text=None) -> dict:
        accord = self.go(text=text or ("go %s" % action["action_id"]))
        request = actions.approval_request(action, ALICE, accord=accord)
        return actions.approve(self.db, action["action_id"], self.key.receipt(request),
                               policy=POLICY, by="bureau")

    def test_prod_deploy_recorded_with_proof(self):
        action = self.propose()
        self.approve(action)
        proof = {"run": "https://ci.example/run/12", "sha": "b" * 40,
                 "resource": "prod:ameesh"}
        done = actions.execute(self.db, action["action_id"], self.connector,
                               policy=POLICY, by="bureau", proof=proof)
        self.assertEqual(done["state"], "confirmed")
        self.assertEqual(json.loads(done["external_ref"]), proof)

        now = time.time()
        view = reserved_gestures.journal(self.db, since_ts=now - 3600, until_ts=now + 1)
        gesture = [g for g in view["gestures"] if g["kind"] == "action"][0]
        self.assertEqual((gesture["gesture"], gesture["operation"], gesture["state"],
                          gesture["proposed_by"], gesture["approver"], gesture["proof"]),
                         ("production", "prod-deploy", "confirmed", "agent:bureau",
                          ALICE, proof))
        self.assertTrue(gesture["receipt"])
        self.assertTrue(gesture["accord"])

    def test_execute_without_proof_leaves_failed_and_unrecorded(self):
        action = self.propose()
        self.approve(action)
        done = actions.execute(self.db, action["action_id"], self.connector,
                               policy=POLICY, by="bureau")
        self.assertEqual(done["state"], "failed")
        self.assertIsNone(done["external_ref"])
        now = time.time()
        view = reserved_gestures.journal(self.db, since_ts=now - 3600, until_ts=now + 1)
        gesture = [g for g in view["gestures"] if g["kind"] == "action"][0]
        self.assertEqual((gesture["state"], gesture["proof"]), ("failed", None))

    def test_prod_deploy_under_grant_shows_grant_and_reservation(self):
        request = receipts.standing_request(
            approver=ALICE, connector="journal", operations=["prod-deploy"],
            action_class="irreversible", until=int(time.time()) + 86400,
            beneficiary="agent:bureau", targets=["prod:ameesh"],
            requested_by="agent:bureau")
        # #7631 : une clé fichier ne vaut que sous l'accord qui nomme le
        # mandat, le connecteur et l'opération.
        request["accord"] = self.go(text="go mandat journal prod-deploy")
        verdict, grant_id = receipts.register_standing(
            self.db, self.key.receipt(request), POLICY, registered_by="bureau",
            limits=self.LIMITS)
        self.assertTrue(verdict.ok, verdict.reason)
        action = self.propose()
        bound = actions.approve(self.db, action["action_id"], standing=True, by="bureau")
        self.assertEqual(bound["auth_grant_id"], grant_id)
        done = actions.execute(self.db, action["action_id"], self.connector,
                               policy=POLICY, by="bureau", proof={"resource": "deploy-1"})
        self.assertEqual(done["state"], "confirmed")
        now = time.time()
        view = reserved_gestures.journal(self.db, since_ts=now - 3600, until_ts=now + 1)
        gesture = [g for g in view["gestures"] if g["kind"] == "action"][0]
        self.assertEqual((gesture["grant"], gesture["operation"]),
                         (grant_id, "prod-deploy"))
        self.assertIsNotNone(gesture["reservation"])
        self.assertEqual(gesture["accord"], {"grant": grant_id})

    def test_out_of_gate_prod_deploy_is_signalled(self):
        path = os.path.join(self.tmp, "deploys.jsonl")
        moment = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "deploy-9", "target": "prod:ameesh",
                                 "ref": "1.59.0", "ts": moment,
                                 "proof": {"run": "https://ci.example/run/9"}}) + "\n")
            fh.write("not json\n")
        with mock.patch.dict(os.environ,
                             {journal_module.PROD_DEPLOY_JOURNAL_ENV: path}):
            view = reserved_gestures.journal(self.db, since_ts=moment - 60,
                                             until_ts=moment + 60)
        signals = [s for s in view["signals"]
                   if s["code"] == reserved_gestures.UNRECORDED_PROD_DEPLOY]
        self.assertEqual([(s["deploy"], s["target"], s["ref"]) for s in signals],
                         [("deploy-9", "prod:ameesh", "1.59.0")])

    def test_deploy_recorded_by_its_proof_is_not_signalled(self):
        action = self.propose()
        self.approve(action)
        proof = {"run": "https://ci.example/run/9", "sha": "c" * 40}
        actions.execute(self.db, action["action_id"], self.connector,
                        policy=POLICY, by="bureau", proof=proof)
        path = os.path.join(self.tmp, "deploys.jsonl")
        moment = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "deploy-9", "target": "prod:ameesh",
                                 "ref": "1.59.0", "ts": moment,
                                 "proof": {"run": "https://ci.example/run/9"}}) + "\n")
        with mock.patch.dict(os.environ,
                             {journal_module.PROD_DEPLOY_JOURNAL_ENV: path}):
            view = reserved_gestures.journal(self.db, since_ts=moment - 3600,
                                             until_ts=moment + 1)
        self.assertEqual([s for s in view["signals"]
                          if s["code"] == reserved_gestures.UNRECORDED_PROD_DEPLOY], [])
        # sans le journal externe, aucune détection : rien n'est inventé
        self.assertEqual(journal_module.detect_prod_deploys(
            since_ts=moment - 60, until_ts=moment + 60), [])


class DecisionGrantBase(_Base):
    """Une décision signée par l'avatar sous un grant de `decision-sign`."""

    LIMITS = standing_limits(connectors=("journal",),
                             operations=("decision-sign",),
                             max_duration_hours=44 * 24)

    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE actions, action_attempts, action_events, authenticators, "
            "standing_approvals, standing_reservations, mesh_consumed_nonces, "
            "accord_claims RESTART IDENTITY CASCADE")
        self.db.execute("DELETE FROM conversation_log WHERE agent = %s", (AVATAR,))
        self.db.execute("DELETE FROM conversation_heads WHERE agent = %s", (AVATAR,))
        self.private, public, _fp = signing.write_keypair(
            os.path.join(self.tmp, "keys-alice"), "alice")
        authority.register_human_key(self.db, ALICE, signing.read_public(public),
                                     role="owner")
        # le projet est celui du lot : il nomme le canon qui classe l'action
        self.db.execute("UPDATE work_items SET app = 'ameesh' WHERE id = %s",
                        (self.lot["id"],))
        journal_policy(self.db, "ameesh")
        self.gkey = SoftEd25519()
        entry = self.gkey.entry()
        entry["accord_operations"] = ["decision-sign"]
        apply_authenticators(self.db, [member("alice", entry)])

    def standing(self, **extra) -> dict:
        value = {"connector": "journal", "operations": ["decision-sign"],
                 "class": "irreversible", "max_amount": 0, "currency": None,
                 "until": int(time.time()) + 40 * 86400,
                 "beneficiary": "agent:bureau", "targets": ["human:alice"]}
        value.update(extra)
        return value

    def mandate(self, text: str) -> dict:
        entry = storage.of(self.db).conversations.append(
            agent=AVATAR, session=None, turn=None, kind="human_message", author=ALICE,
            text=text, truncated=False, meta={}, event_id=None, project="ameesh",
            lot=None, thread_text=None)
        return {"agent": AVATAR, "seq": int(entry["seq"])}

    def grant(self, *, limits=None, **extra):
        request = make_request(ALICE, standing=self.standing(**extra))
        # #7631 : clé fichier => accord journalisé nommant le mandat.
        request["accord"] = self.mandate("go mandat journal decision-sign")
        limits = self.LIMITS if limits is None else limits
        return receipts.register_standing(self.db, self.gkey.receipt(request), POLICY,
                                          registered_by="bureau", limits=limits)

    def grant_signer(self, grant_id: int) -> dict:
        return {"by": "avatar", "agent": AVATAR, "accord": {"grant": int(grant_id)}}

    def sign(self, view, grant_id: int):
        return self.answer(view["id"], "a", key_path=self.private,
                           signer=self.grant_signer(grant_id))


class DecisionGrantTest(DecisionGrantBase):
    def test_decision_signed_under_grant_reserves_it(self):
        verdict, grant_id = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True)
        signed = self.sign(view, grant_id)
        proof = signed["decision"]["answer"]["proof"]
        self.assertEqual(proof["standing"]["grant"], grant_id)
        key = proof["standing"]["key"]
        action = actions.get(self.db, key)
        self.assertEqual((action["connector"], action["operation"], action["state"],
                          action["args"]["decision"], action["args"]["answer"]),
                         ("journal", "decision-sign", "approved", view["id"],
                          proof["digest"]))
        self.assertEqual(action["auth_grant_id"], grant_id)
        live = storage.of(self.db).grants.live_reservations(key)
        self.assertEqual([(r["grant_id"], r["connector"], r["operation"],
                           r["action_class"]) for r in live],
                         [(grant_id, "journal", "decision-sign", "irreversible")])
        self.assertTrue(decisions.verify_proof(self.db, signed["decision"])[0])
        self.assertTrue(decisions.verify_proof(self.db, signed["decision"],
                                               live=True)[0])
        # l'action de porte est liée à la décision : une autre action de
        # signature (même empreinte, autre décision) ne vaut pas pour celle-ci
        other = actions.propose(
            self.db, connectors.get("journal"), project="ameesh",
            operation="decision-sign", target="human:alice",
            args={"decision": int(view["id"]) + 1, "answer": proof["digest"]},
            proposed_by="agent:bureau", action_class="irreversible")
        forged = json.loads(json.dumps(signed["decision"]))
        forged["answer"]["proof"]["standing"]["key"] = other["action_id"]
        self.assertFalse(decisions.verify_proof(self.db, forged)[0])

        now = time.time()
        journal = reserved_gestures.journal(self.db, since_ts=now - 3600,
                                            until_ts=now + 1)
        entry = [g for g in journal["gestures"] if g["kind"] == "decision"][0]
        self.assertEqual((entry["operation"], entry["grant"], entry["reservation"],
                          entry["agent"], entry["signed_by"]),
                         ("decision-sign", grant_id, proof["standing"]["reservation"],
                          AVATAR, "avatar"))
        self.assertEqual(entry["key"], key)

        # la réservation libérée : la preuve ne vaut plus
        receipts.standing_release(self.db, proof["standing"]["reservation"],
                                  outcome="failed", reason="test")
        self.assertFalse(decisions.verify_proof(self.db, signed["decision"])[0])

    def test_cited_grant_is_the_one_bound_and_reserved(self):
        """B (relecture P1) : le grant CITE par --accord est celui qui
        approuve et réserve, jamais un autre grant couvrant."""
        verdict, first = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        verdict, second = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True)
        signed = self.sign(view, second)
        proof = signed["decision"]["answer"]["proof"]
        self.assertEqual(proof["standing"]["grant"], second)
        action = actions.get(self.db, proof["standing"]["key"])
        self.assertEqual(action["auth_grant_id"], second)
        live = storage.of(self.db).grants.live_reservations(action["action_id"])
        self.assertEqual([int(row["grant_id"]) for row in live], [second])
        self.assertTrue(decisions.verify_proof(self.db, signed["decision"])[0])
        # le meta de l'approbation dit le grant cité ; le changer (il n'est
        # pas signé) fait refuser la preuve, il ne peut pas réécrire l'accord.
        storage.of(self.db).approvals.merge_meta(
            proof["approval_id"], {"accord": {"grant": first}})
        self.assertFalse(decisions.verify_proof(self.db, signed["decision"])[0])

    def test_removed_standing_reservation_is_refused(self):
        """B (relecture cx2) : `proof.standing` n'est pas signé ; le retirer
        laissait la preuve valide sans contrôle du grant cité ni de la
        réservation. L'accord signé cite le grant : la preuve doit le porter."""
        verdict, grant_id = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True)
        signed = self.sign(view, grant_id)
        forged = json.loads(json.dumps(signed["decision"]))
        del forged["answer"]["proof"]["standing"]
        # ni l'empreinte ni la signature ne changent : seul le champ non signé
        # a disparu
        self.assertEqual(forged["answer"]["proof"]["digest"],
                         signed["decision"]["answer"]["proof"]["digest"])
        ok, why = decisions.verify_proof(self.db, forged)
        self.assertFalse(ok)
        self.assertIn("no standing reservation", why)

    def test_substituted_standing_reservation_is_refused(self):
        """B (relecture cx2) : substituer la réservation ne passe pas — le
        grant doit être celui de l'accord signé, et la réservation vivante
        exactement celle de la preuve."""
        verdict, first = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        verdict, second = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True)
        signed = self.sign(view, second)
        proof = signed["decision"]["answer"]["proof"]
        self.assertEqual(proof["standing"]["grant"], second)

        # (a) un autre grant valide, mais pas celui de l'accord signé
        forged = json.loads(json.dumps(signed["decision"]))
        forged["answer"]["proof"]["standing"]["grant"] = first
        ok, why = decisions.verify_proof(self.db, forged)
        self.assertFalse(ok)
        self.assertIn("grant cited in the signed answer", why)

        # (b) la réservation d'une AUTRE signature, sous le MÊME grant : le
        # grant cité concorde, mais l'action ne décrit pas cette réponse
        other_view = self.ask(needs_signature=True)
        other = self.sign(other_view, second)
        forged = json.loads(json.dumps(signed["decision"]))
        forged["answer"]["proof"]["standing"] = (
            other["decision"]["answer"]["proof"]["standing"])
        ok, why = decisions.verify_proof(self.db, forged)
        self.assertFalse(ok)
        self.assertIn("not tied to this answer", why)

        # (c) la réservation vivante de l'action n'est pas celle de la preuve
        forged = json.loads(json.dumps(signed["decision"]))
        forged["answer"]["proof"]["standing"]["reservation"] = (
            other["decision"]["answer"]["proof"]["standing"]["reservation"])
        ok, why = decisions.verify_proof(self.db, forged)
        self.assertFalse(ok)
        self.assertIn("not found", why)

    def test_absent_or_revoked_cited_grant_is_refused_even_with_another(self):
        """B (relecture P1) : un grant cité inexistant ou révoqué refuse la
        signature, même si un autre grant valide couvrirait la décision."""
        verdict, valid = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True)
        with self.assertRaises(decisions.DecisionError):
            self.sign(view, 999999)
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
        verdict, revoked = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        receipts.revoke_standing(self.db, revoked, by="human:alice")
        with self.assertRaises(decisions.DecisionError):
            self.sign(view, revoked)
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
        # aucun grant n'a été réservé, aucune action de porte n'est restée
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM standing_reservations")[0]["n"], 0)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM actions WHERE operation = "
            "'decision-sign'")[0]["n"], 0)
        # le grant valide, lui, signe toujours
        signed = self.sign(view, valid)
        self.assertEqual(signed["decision"]["answer"]["proof"]["standing"]["grant"],
                         valid)

    def test_other_beneficiary_or_target_grant_does_not_cover(self):
        # Un grant ENREGISTRÉ pour un autre bénéficiaire ne couvre pas la
        # décision : la porte le refuse à la signature (le canon peut
        # l'accepter pour un autre usage, pas pour celle-ci ici).
        for extra in ({"beneficiary": "agent:another"}, {"targets": ["human:bob"]}):
            with self.subTest(extra=extra):
                view = self.ask(needs_signature=True)
                # le grant est posé directement : le plafond n'est pas le sujet
                verdict, grant_id = self.grant(**extra)
                if grant_id is None:
                    self.skipTest("le canon refuse déjà ce grant: %s" % verdict.reason)
                with self.assertRaises(decisions.DecisionError):
                    self.sign(view, grant_id)
                self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")

    def test_revoked_or_expired_grant_refused(self):
        verdict, grant_id = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        receipts.revoke_standing(self.db, grant_id, by="human:alice")
        view = self.ask(needs_signature=True)
        with self.assertRaises(decisions.DecisionError) as caught:
            self.sign(view, grant_id)
        self.assertIn("no standing approval covers", str(caught.exception))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")

        verdict, grant_id = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        self.db.execute("UPDATE standing_approvals SET valid_until = now() - "
                        "interval '1 hour' WHERE id = %s", (grant_id,))
        view = self.ask(needs_signature=True)
        with self.assertRaises(decisions.DecisionError):
            self.sign(view, grant_id)

    def test_decision_on_the_owner_key_is_never_covered(self):
        verdict, grant_id = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True, key_provenance=True)
        with self.assertRaises(decisions.DecisionError) as caught:
            self.sign(view, grant_id)
        self.assertIn("never covered", str(caught.exception))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM actions WHERE operation = 'decision-sign-key'"
        )[0]["n"], 0)

    def test_unreadable_key_leaves_no_reservation(self):
        verdict, grant_id = self.grant()
        self.assertTrue(verdict.ok, verdict.reason)
        view = self.ask(needs_signature=True)
        with self.assertRaises(decisions.DecisionError):
            self.answer(view["id"], "a", key_path=os.path.join(self.tmp, "absent"),
                        signer=self.grant_signer(grant_id))
        # l'échec est certain : la transaction a rendu l'action et sa
        # réservation ; le grant reste disponible pour la vraie signature.
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM actions WHERE operation = 'decision-sign' "
            "AND args->>'decision' = %s", (str(view["id"]),))[0]["n"], 0)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM standing_reservations")[0]["n"], 0)
        signed = self.sign(view, grant_id)
        key = signed["decision"]["answer"]["proof"]["standing"]["key"]
        self.assertEqual(len(storage.of(self.db).grants.live_reservations(key)), 1)


class GrantDurationTest(DecisionGrantBase):
    def test_canon_max_duration_wins_over_the_30_day_default(self):
        # 44 jours : refusés par le défaut de `receipts.Policy` (30 j), admis
        # par la règle du canon (44 j) — c'est le canon qui tranche (#7684).
        extra = {"until": int(time.time()) + 44 * 86400 - 60}
        verdict, grant_id = self.grant(**extra)
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertIsNotNone(grant_id)
        # sans règle de canon, le défaut de 30 jours reste
        request = make_request(ALICE, standing=self.standing(**extra))
        request["accord"] = self.mandate("go mandat journal decision-sign")
        verdict, grant_id = receipts.register_standing(
            self.db, self.gkey.receipt(request), POLICY, registered_by="bureau")
        self.assertIsNone(grant_id)
        self.assertEqual(verdict.code, receipts.TTL)
        # une règle plus courte refuse un grant que le défaut accepterait
        request = make_request(ALICE, standing=self.standing(
            until=int(time.time()) + 31 * 86400))
        request["accord"] = self.mandate("go mandat journal decision-sign")
        verdict, grant_id = receipts.register_standing(
            self.db, self.gkey.receipt(request), POLICY, registered_by="bureau",
            limits=standing_limits(connectors=("journal",),
                                   operations=("decision-sign",),
                                   max_duration_hours=30 * 24))
        self.assertIsNone(grant_id)
        self.assertEqual(verdict.code, receipts.TTL)

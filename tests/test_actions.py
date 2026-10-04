# SPDX-License-Identifier: AGPL-3.0-only
"""Actions sous porte (spec §7, R5, C6) et file des décisions (C10).

Reçus produits par des authentificateurs LOGICIELS (tests/webauthn_soft.py),
registre de confiance rempli par `support.apply_authenticators`. Connecteurs :
`shell-noop` (fichiers sous le bac à sable du test) et `git-merge` avec un
FAUX `gh` (tests/fakebin/gh) — jamais le vrai, aucun réseau.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest

from ameesh import actions, fil, jcs, receipts, work
from ameesh import db as db_mod
from ameesh.actions import ActionError
from ameesh.connectors import Outcome
from ameesh.connectors import ConnectorError
from ameesh.connectors.git_merge import GitMergeConnector, parse_target, resolve_gh
from ameesh.connectors.shell_noop import ShellNoopConnector
from ameesh.db import DbError

from .support import FAKEBIN, PgTestCase, apply_authenticators
from .test_receipts import HeldTransaction, Worker
from .webauthn_soft import ORIGIN, RP_ID, SoftEd25519, SoftWebAuthn, make_request

ALICE = "human:alice"
BOB = "human:bob"
AGENT = "agent:deepseek7"
COMMIT = "a" * 40
POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))
FAKE_GH = os.path.join(FAKEBIN, "gh")
PR = "o/r#12"


def member(title: str, *entries: dict) -> dict:
    return {"title": title, "canon_ref": "members/%s.md@%s" % (title, COMMIT),
            "authenticators": list(entries)}


class Counting:
    """Enveloppe un connecteur et compte ses appels (sûr entre fils)."""

    def __init__(self, inner, delay: float = 0.0):
        self.inner = inner
        self.name = inner.name
        self.dedupe = inner.dedupe
        self.timeout = getattr(inner, "timeout", 30.0)
        self.delay = delay
        self.calls: list = []
        self.reconciles = 0
        self._lock = threading.Lock()

    def classify(self, operation, args):
        return self.inner.classify(operation, args)

    def execute(self, action, idempotency_key):
        with self._lock:
            self.calls.append((action.action_id, idempotency_key, action.attempt))
        if self.delay:
            time.sleep(self.delay)
        return self.inner.execute(action, idempotency_key)

    def reconcile(self, action):
        with self._lock:
            self.reconciles += 1
        return self.inner.reconcile(action)


class Scripted:
    """Connecteur de test au comportement imposé (exception, attente, réponse)."""

    name = "scripted"

    def __init__(self, behaviour, dedupe: str = "none", timeout: float = 0.5,
                 reconciled: Outcome | None = None):
        self.behaviour = behaviour
        self.dedupe = dedupe
        self.timeout = timeout
        self.reconciled = reconciled
        self.calls = 0

    def classify(self, operation, args):
        return "irreversible"

    def execute(self, action, idempotency_key):
        self.calls += 1
        return self.behaviour()

    def reconcile(self, action):
        return self.reconciled


class Paused:
    """Base dont la requête qui contient `marker` attend l'heure `until` avant
    de partir : pause injectée entre la vérification Python et la transaction."""

    def __init__(self, db, marker: str, until: float):
        self.db = db
        self.marker = marker
        self.until = until
        self.paused = 0

    def query(self, sql, params=()):
        if self.marker in sql:
            self.paused += 1
            while time.time() < self.until:
                time.sleep(0.05)
        return self.db.query(sql, params)

    def __getattr__(self, name):
        return getattr(self.db, name)


class ActionsBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE actions, action_attempts, action_events, authenticators, "
            "standing_approvals, standing_reservations, mesh_consumed_nonces "
            "RESTART IDENTITY CASCADE")
        self.alice = SoftWebAuthn("ES256")
        self.alice_tool = SoftEd25519()
        self.bob = SoftWebAuthn("ES256")
        self.members = [member("alice", self.alice.entry(), self.alice_tool.entry()),
                        member("bob", self.bob.entry())]
        result = apply_authenticators(self.db, self.members)
        self.assertEqual(result["errors"], [])
        self.noop_dir = os.path.join(self.tmp, "noop")
        self.noop = Counting(ShellNoopConnector(self.noop_dir, timeout=10.0))
        self.gh_state = os.path.join(self.tmp, "gh-state.json")
        self.gh_log = os.path.join(self.tmp, "gh-log.jsonl")
        with open(self.gh_state, "w", encoding="utf-8") as fh:
            json.dump({"prs": {PR: {"state": "OPEN", "mergeCommit": None}}}, fh)

    # -- utilitaires -------------------------------------------------------
    def propose(self, operation: str = "send", connector=None, **kwargs) -> dict:
        kwargs.setdefault("project", "demo")
        kwargs.setdefault("proposed_by", AGENT)
        kwargs.setdefault("target", "client:42")
        kwargs.setdefault("args", {"subject": "facture", "n": 1})
        return actions.propose(self.db, connector or self.noop, operation=operation, **kwargs)

    def receipt(self, action: dict, soft=None, approver: str = ALICE, **kwargs) -> dict:
        return (soft or self.alice).receipt(actions.approval_request(action, approver, **kwargs))

    def approve(self, action: dict, receipt=None, **kwargs) -> dict:
        return actions.approve(self.db, action["action_id"],
                               receipt if receipt is not None else self.receipt(action),
                               policy=POLICY, by=ALICE, **kwargs)

    def execute(self, action: dict, connector=None, db=None) -> dict:
        return actions.execute(db or self.db, action["action_id"], connector or self.noop,
                               policy=POLICY, by="porte")

    def get(self, action: dict) -> dict:
        return actions.get(self.db, action["action_id"])

    def refused(self, code: str, func, *args, **kwargs) -> ActionError:
        with self.assertRaises(ActionError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code, ctx.exception.reason)
        return ctx.exception

    def files(self) -> list:
        if not os.path.isdir(self.noop_dir):
            return []
        return sorted(f for f in os.listdir(self.noop_dir) if f.endswith(".json"))

    def nonce_consumed(self, receipt: dict) -> bool:
        request = receipt["request"]
        return bool(self.db.query(
            "SELECT 1 FROM mesh_consumed_nonces WHERE approver = %s AND nonce = %s",
            (request["approver"], request["nonce"])))

    def event_names(self, action: dict) -> list:
        return [event["event"] for event in actions.events(self.db, action["action_id"])]

    def gh(self, mode: str = "ok", view: str | None = None, timeout: float = 10.0):
        env = dict(os.environ)
        env.update(AMEESH_FAKE_GH_STATE=self.gh_state, AMEESH_FAKE_GH_LOG=self.gh_log,
                   AMEESH_FAKE_GH_MODE=mode)
        if view:
            env["AMEESH_FAKE_GH_VIEW"] = view
        return Counting(GitMergeConnector(gh=FAKE_GH, env=env, timeout=timeout))

    def pr(self, key: str = PR) -> dict:
        with open(self.gh_state, encoding="utf-8") as fh:
            return json.load(fh)["prs"][key]

    def set_pr(self, key: str = PR, **fields) -> None:
        with open(self.gh_state, encoding="utf-8") as fh:
            state = json.load(fh)
        state["prs"].setdefault(key, {"state": "OPEN", "mergeCommit": None}).update(fields)
        with open(self.gh_state, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def merge_calls(self) -> int:
        """Demandes de fusion soumises au faux gh (toutes actions confondues)."""
        if not os.path.exists(self.gh_log):
            return 0
        with open(self.gh_log, encoding="utf-8") as fh:
            return sum(1 for line in fh if json.loads(line)["argv"][1] == "merge")

    def register_grant(self, max_amount: int = 100) -> int:
        standing = {"connector": "shell-noop", "operations": ["refund"], "class": "costly",
                    "max_amount": max_amount, "currency": "EUR",
                    "until": int(time.time()) + 3600}
        receipt = self.alice.receipt(make_request(ALICE, standing=standing))
        verdict, grant = receipts.register_standing(self.db, receipt, POLICY, registered_by="porte")
        self.assertTrue(verdict.ok, verdict.reason)
        return grant

    def grant_consumed(self, grant: int) -> int:
        return int(receipts.get_standing(self.db, grant)["consumed_amount"])


# --------------------------------------------------------------------------
# identité, empreintes, connecteurs (sans base)
# --------------------------------------------------------------------------

class ActionModelTest(unittest.TestCase):
    def test_action_id_ulid(self):
        ids = {actions.new_action_id() for _ in range(2000)}
        self.assertEqual(len(ids), 2000)
        for action_id in list(ids)[:50]:
            self.assertRegex(action_id, r"^act_[0-9A-HJKMNP-TV-Z]{26}$")
            self.assertLessEqual(action_id[4], "7")             # 128 bits sur 130
        early = actions.new_action_id(now=1_700_000_000.0)
        late = actions.new_action_id(now=1_700_000_001.0)
        self.assertLess(early, late)                            # triable par date

    def test_empreinte_par_l6_et_empreinte_d_assomption(self):
        action = {"action_id": actions.new_action_id(), "project": "demo",
                  "connector": "git-merge", "operation": "merge", "target": PR,
                  "args": {"method": "squash"}, "amount": None, "currency": None,
                  "policy_version": "1", "class": "irreversible", "proposed_by": AGENT}
        self.assertEqual(actions.digest(action),
                         receipts.action_digest(actions.digest_fields(action)))
        assume = actions.assume_duplicate_digest(action)
        self.assertRegex(assume, r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(assume, actions.digest(action))
        other = dict(action, action_id=actions.new_action_id())
        self.assertNotEqual(actions.assume_duplicate_digest(other), assume)
        request = actions.approval_request(action, ALICE)
        self.assertEqual(receipts.check_request(request), "action")
        self.assertEqual(request["digest"], actions.digest(action))
        self.assertEqual(request["requested_by"], AGENT)
        assume_request = actions.approval_request(action, ALICE, assume_duplicate=True)
        self.assertEqual(assume_request["digest"], assume)
        self.assertNotEqual(assume_request["summary_digest"], request["summary_digest"])
        with self.assertRaises(ActionError):
            actions.approval_request(action, AGENT)            # un agent n'approuve pas

    def test_cible_git_merge(self):
        self.assertEqual(parse_target("manaty/ameesh#42"), ("manaty/ameesh", 42))
        for bad in ("ameesh#42", "o/r#0", "o/r#x", "o/r 42", "o/r#42\n", "../r#1"):
            with self.subTest(bad):
                with self.assertRaises(ConnectorError):
                    parse_target(bad)

    def test_gh_resolu_sans_jamais_le_vrai(self):
        self.assertEqual(resolve_gh(None, {"AMEESH_BIN_DIR": FAKEBIN, "PATH": ""}), FAKE_GH)
        self.assertEqual(resolve_gh(None, {"AMEESH_GH_BIN": FAKE_GH}), FAKE_GH)
        with self.assertRaises(ConnectorError):
            resolve_gh(None, {"AMEESH_GH_BIN": "/nulle/part/gh", "PATH": ""})
        with self.assertRaises(ConnectorError):
            resolve_gh(None, {"PATH": ""})

    def test_shell_noop_cle_d_idempotence_controlee(self):
        connector = ShellNoopConnector("/tmp/ameesh-jamais-utilise")
        for key in ("../x", "act_court", "act_" + "A" * 25 + "/", ""):
            with self.subTest(key):
                with self.assertRaises(ConnectorError):
                    connector.path(key)


# --------------------------------------------------------------------------
# cycle, reçus, classes
# --------------------------------------------------------------------------

class ActionsCycleTest(ActionsBase):
    def test_cycle_complet_webauthn(self):
        action = self.propose("send")
        self.assertEqual((action["state"], action["class"], action["requires_receipt"],
                          action["dedupe"]), ("proposed", "irreversible", True, "guaranteed"))
        self.assertRegex(action["action_id"], r"^act_[0-9A-Za-z]{26}$")
        self.assertEqual(action["digest"], actions.digest(action))
        receipt = self.receipt(action)
        approved = self.approve(action, receipt)
        self.assertEqual((approved["state"], approved["auth_kind"], approved["auth_approver"]),
                         ("approved", "receipt", ALICE))
        self.assertFalse(self.nonce_consumed(receipt))          # lié, pas encore consommé
        result = self.execute(action)
        self.assertEqual((result["state"], result["attempt"]), ("confirmed", 1))
        self.assertTrue(self.nonce_consumed(receipt))
        self.assertEqual(self.noop.calls, [(action["action_id"], action["action_id"], 1)])
        self.assertEqual(self.files(), [action["action_id"] + ".json"])
        with open(os.path.join(self.noop_dir, self.files()[0]), encoding="utf-8") as fh:
            written = jcs.loads(fh.read())
        self.assertEqual((written["idempotency_key"], written["args"]),
                         (action["action_id"], {"n": 1, "subject": "facture"}))
        attempt, = actions.attempts(self.db, action["action_id"])
        self.assertEqual((attempt["state"], attempt["auth_kind"], attempt["approver"],
                          attempt["receipt_id"], attempt["settled_by"]),
                         ("confirmed", "receipt", ALICE,
                          receipts.challenge(receipt["request"]).hex(), "connector"))
        self.assertEqual(self.event_names(action), ["proposed", "approved", "launched", "confirmed"])
        row = self.get(action)
        self.assertEqual((row["state"], row["attempts"]), ("confirmed", 1))
        self.refused(actions.STATE, self.execute, action)
        self.assertEqual(len(self.noop.calls), 1)

    def test_refus_sans_recu(self):
        action = self.propose("send")
        self.refused(actions.RECEIPT_REQUIRED, actions.approve, self.db, action["action_id"],
                     None, policy=POLICY)
        self.refused(actions.NO_COVER, actions.approve, self.db, action["action_id"], None,
                     standing=True, policy=POLICY)
        self.refused(actions.RECEIPT_REQUIRED, self.execute, action)
        self.assertEqual(self.noop.calls, [])
        self.assertEqual(self.get(action)["state"], "proposed")
        self.assertEqual(self.files(), [])

    def test_recu_d_une_autre_action_refuse(self):
        mine = self.propose("send", args={"n": 1})
        other = self.propose("send", args={"n": 2})
        self.refused(receipts.ACTION_MISMATCH, self.approve, mine, self.receipt(other))
        # même action_id, mais empreinte d'autres args (modifiés après signature)
        forged = dict(mine, args={"n": 999})
        self.refused(receipts.DIGEST_MISMATCH, self.approve, mine, self.receipt(forged))
        # la façade ed25519 (clé d'outil) ne fait pas autorité humaine
        self.refused(receipts.FACADE, self.approve, mine, self.receipt(mine, soft=self.alice_tool))
        # signature de bob pour une demande au nom d'alice
        self.refused(receipts.FOREIGN_AUTHENTICATOR, self.approve, mine,
                     self.receipt(mine, soft=self.bob))
        self.assertEqual(self.get(mine)["state"], "proposed")
        self.assertIn("proposed", self.event_names(mine))

    def test_rejeu_du_recu_refuse(self):
        action = self.propose("send", args={"simulate": "fail"})
        receipt = self.receipt(action)
        self.approve(action, receipt)
        self.assertEqual(self.execute(action)["state"], "failed")
        self.assertTrue(self.nonce_consumed(receipt))
        self.assertEqual(self.files(), [])                     # échec certain : aucun effet
        # nouvelle tentative avec le MÊME reçu : refusée (nonce consommé)
        self.refused(receipts.REPLAY, actions.retry, self.db, action["action_id"], receipt,
                     policy=POLICY)
        self.assertEqual(self.get(action)["state"], "failed")
        # rejeu à la liaison sur une action déjà en approved : idem
        second = self.propose("send", args={"n": 2})
        receipt2 = self.receipt(second)
        self.approve(second, receipt2)
        receipts.consume_nonce(self.db, ALICE, receipt2["request"]["nonce"], by="ailleurs",
                               exp=receipt2["request"]["exp"], iat=receipt2["request"]["iat"])
        self.refused(receipts.REPLAY, self.execute, second)
        self.assertEqual(self.get(second)["state"], "approved")
        self.assertEqual(len(self.noop.calls), 1)

    def test_lancement_garde_en_base(self):
        """La fonction de lancement revérifie : empreinte, nonce libre, authentificateur actif."""
        action = self.propose("send")
        receipt = self.receipt(action)
        row = self.approve(action, receipt)

        def launch(digest=None):
            return self.db.query(
                "SELECT * FROM ameesh_action_launch(%s::text, %s::text, %s::text, %s::text, "
                "%s::bigint, %s::text, %s::double precision)",
                (action["action_id"], digest or action["digest"], "receipt", row["auth_nonce"],
                 None, "test", 5.0))[0]

        self.assertEqual(launch(digest="sha256:" + "0" * 64)["result"], "state")
        apply_authenticators(self.db, [member("alice", self.alice_tool.entry()),
                                               member("bob", self.bob.entry())])
        self.assertEqual(launch()["result"], "revoked_authenticator")
        self.assertEqual(self.get(action)["state"], "approved")
        self.assertFalse(self.nonce_consumed(receipt))
        self.assertEqual(actions.attempts(self.db, action["action_id"]), [])

    def test_revocation_entre_approbation_et_execution(self):
        action = self.propose("send")
        self.approve(action)
        apply_authenticators(self.db, [member("alice", self.alice_tool.entry()),
                                               member("bob", self.bob.entry())])
        self.refused(receipts.REVOKED, self.execute, action)
        self.assertEqual((self.get(action)["state"], self.noop.calls), ("approved", []))
        self.assertIn("refused", self.event_names(action))

    def test_recu_expire_avant_execution(self):
        action = self.propose("send")
        receipt = self.receipt(action, now=int(time.time()) - 598, ttl=600)
        self.approve(action, receipt)
        deadline = receipt["request"]["exp"]
        self.wait_for(lambda: time.time() > deadline + 0.2, timeout=10)
        self.refused(receipts.EXPIRED, self.execute, action)
        self.assertFalse(self.nonce_consumed(receipt))
        self.assertEqual((self.get(action)["state"], self.noop.calls), ("approved", []))
        # nouvelle liaison avec un reçu frais : l'action repart
        self.approve(action)
        self.assertEqual(self.execute(action)["state"], "confirmed")

    def test_lancement_echeance_controlee_a_l_heure_reelle(self):
        """L'échéance du reçu est revérifiée DANS la transaction de lancement, à
        l'heure réelle (clock_timestamp, pas now()), après les verrous."""
        action = self.propose("send")
        receipt = self.receipt(action, now=int(time.time()) - 598, ttl=600)
        row = self.approve(action, receipt)
        # transaction ouverte AVANT l'échéance, lancement APRÈS : now() est périmé
        self.db.execute(
            "CREATE OR REPLACE FUNCTION launch_after(p_action_id text, p_digest text, p_nonce text, "
            "p_until double precision) "
            "RETURNS TABLE (result text, attempt integer, reservation bigint, detail text) "
            "LANGUAGE plpgsql AS $f$ BEGIN "
            "PERFORM pg_sleep(greatest(p_until - extract(epoch from clock_timestamp())::float8, 0)); "
            "RETURN QUERY SELECT * FROM ameesh_action_launch(p_action_id, p_digest, 'receipt', "
            "p_nonce, NULL, 'test', 5.0); END $f$")
        out = self.db.query(
            "SELECT * FROM launch_after(%s::text, %s::text, %s::text, %s::double precision)",
            (action["action_id"], action["digest"], row["auth_nonce"],
             receipt["request"]["exp"] + 0.3))[0]
        self.assertEqual(out["result"], receipts.EXPIRED, out)
        self.assertEqual(self.get(action)["state"], "approved")
        self.assertFalse(self.nonce_consumed(receipt))
        self.assertEqual(actions.attempts(self.db, action["action_id"]), [])
        # par la porte : reçu valide à la vérification Python, échu à l'appel SQL
        receipt = self.receipt(action, now=int(time.time()) - 598, ttl=600)
        self.approve(action, receipt)
        paused = Paused(self.db, "ameesh_action_launch(", until=receipt["request"]["exp"] + 0.3)
        self.refused(receipts.EXPIRED, self.execute, action, db=paused)
        self.assertEqual(paused.paused, 1)
        self.assertEqual((self.get(action)["state"], self.noop.calls), ("approved", []))
        self.assertFalse(self.nonce_consumed(receipt))
        self.assertEqual(actions.attempts(self.db, action["action_id"]), [])
        self.assertIn("refused", self.event_names(action))
        # un reçu frais : l'action part
        self.approve(action)
        self.assertEqual(self.execute(action)["state"], "confirmed")

    def test_refus_signe_annule(self):
        action = self.propose("send")
        receipt = self.receipt(action, decision="deny")
        cancelled = self.approve(action, receipt)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(self.nonce_consumed(receipt))
        self.refused(actions.STATE, self.execute, action)

    def test_approbateurs_designes(self):
        action = self.propose("send", approvers=[BOB])
        self.refused(actions.APPROVER, self.approve, action, self.receipt(action))
        approved = self.approve(action, self.receipt(action, soft=self.bob, approver=BOB))
        self.assertEqual(approved["auth_approver"], BOB)

    def test_classes_read_et_reversible_sans_recu(self):
        note = self.propose("note")
        self.assertEqual((note["class"], note["requires_receipt"]), ("reversible", False))
        self.assertEqual(self.execute(note)["state"], "confirmed")
        read = self.propose("read")
        self.assertEqual(self.execute(read)["state"], "confirmed")
        self.assertEqual(self.event_names(read), ["proposed", "approved", "launched", "confirmed"])
        # paramétrable : une politique qui garde aussi les réversibles
        gated = self.propose("note", receipt_classes={"reversible", "irreversible", "costly"})
        self.assertTrue(gated["requires_receipt"])
        self.refused(actions.RECEIPT_REQUIRED, self.execute, gated)
        # irréversible et coûteux restent gardés quoi qu'on demande
        self.assertTrue(self.propose("send", receipt_classes=())["requires_receipt"])

    def test_classe_relevable_jamais_abaissee(self):
        self.refused(actions.CLASS, self.propose, "send", action_class="read")
        self.refused(actions.CLASS, self.propose, "refund", action_class="irreversible",
                     amount=10, currency="EUR")
        raised = self.propose("note", action_class="irreversible")
        self.assertEqual((raised["class"], raised["requires_receipt"]), ("irreversible", True))
        # seule la classe du canon prime sur celle du connecteur
        canon = self.propose("send", canon_class="reversible")
        self.assertEqual((canon["class"], canon["requires_receipt"]), ("reversible", False))
        # opération inconnue du connecteur : gardée par défaut
        self.assertEqual(self.propose("inconnue")["class"], "irreversible")

    def test_propositions_invalides(self):
        cases = {
            "proposant nu": dict(proposed_by="deepseek7"),
            "args non objet": dict(args=[1, 2]),
            "args flottant": dict(args={"x": 1.5}),
            "montant sans devise": dict(amount=10),
            "montant négatif": dict(amount=-1, currency="EUR"),
            "montant booléen": dict(amount=True, currency="EUR"),
            "devise": dict(amount=1, currency="eur"),
            "approbateur agent": dict(approvers=[AGENT]),
            "projet": dict(project="a b"),
            "cible vide": dict(target=""),
            "cible trop longue": dict(target="x" * 1025),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.refused(actions.INVALID, self.propose, "send", **kwargs)
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM actions")[0]["n"], 0)

    def test_args_canonises_et_empreinte_stable(self):
        action = self.propose("send", args={"z": 1.0, "a": {"y": "é", "b": [True, None]}})
        self.assertEqual(action["args"], {"a": {"b": [True, None], "y": "é"}, "z": 1})
        self.assertEqual(actions.digest(action), action["digest"])

    def test_ligne_lue_par_ameesh_approve(self):
        """Contrat avec ameesh-approve (L7) : il relit `SELECT *` de la table,
        exige `policy_version`, `class`, `state`, une cible non vide, et recoupe
        `digest` avec son propre recalcul de l'empreinte (spec §7.1)."""
        for operation, kwargs in (("send", {}), ("refund", dict(amount=1250, currency="EUR")),
                                  ("note", dict(args={"texte": "é", "liste": [1, None, True]}))):
            with self.subTest(operation):
                action = self.propose(operation, **kwargs)
                raw, = self.db.query("SELECT * FROM actions WHERE action_id = %s",
                                     (action["action_id"],))
                for column in ("policy_version", "class", "state", "digest", "target",
                               "proposed_by", "work_item"):
                    self.assertIn(column, raw)
                self.assertIsInstance(raw["policy_version"], str)
                self.assertTrue(raw["target"])
                self.assertEqual(receipts.action_digest(
                    {name: raw[name] for name in receipts.ACTION_DIGEST_FIELDS}), raw["digest"])

    def test_identite_et_transitions_gardees_en_base(self):
        action = self.propose("send")
        action_id = action["action_id"]

        def update(sql, *params):
            return self.db.query(sql + " RETURNING action_id", (*params, action_id))

        with self.assertRaises(DbError):                        # approved sans autorisation
            update("UPDATE actions SET state = 'approved' WHERE action_id = %s")
        with self.assertRaises(DbError):                        # irréversible sans reçu
            update("UPDATE actions SET state = 'approved', auth_kind = 'none' WHERE action_id = %s")
        with self.assertRaises(DbError):                        # saut d'état
            update("UPDATE actions SET state = 'confirmed' WHERE action_id = %s")
        self.approve(action)
        with self.assertRaises(DbError):                        # args changés sous le reçu
            update("UPDATE actions SET args = %s::jsonb WHERE action_id = %s", '{"n": 2}')
        with self.assertRaises(DbError):
            update("UPDATE actions SET digest = %s WHERE action_id = %s", "sha256:" + "0" * 64)
        with self.assertRaises(DbError):                        # tentative sans lancement
            update("UPDATE actions SET attempts = 3 WHERE action_id = %s")
        self.execute(action)
        with self.assertRaises(DbError):                        # confirmée → rien
            update("UPDATE actions SET state = 'approved', auth_kind = 'none' "
                   "WHERE action_id = %s")
        with self.assertRaises(DbError):                        # journal en ajout seul
            self.db.query("UPDATE action_events SET note = 'x' WHERE action_id = %s "
                          "RETURNING id", (action_id,))
        with self.assertRaises(DbError):
            self.db.query("DELETE FROM action_events WHERE action_id = %s RETURNING id",
                          (action_id,))
        insert = ("INSERT INTO actions (action_id, project, proposed_by, connector, operation, "
                  "target, args, class, digest, dedupe, requires_receipt) VALUES (%s, 'demo', "
                  "%s, 'shell-noop', 'send', %s, '{}', 'irreversible', %s, 'none', true) "
                  "RETURNING action_id")
        good_id = actions.new_action_id()
        for bad_id, target in (("act_x", "client:42"), (good_id, "")):
            with self.assertRaises(DbError):                    # identifiant ou cible
                self.db.query(insert, (bad_id, AGENT, target, "sha256:" + "0" * 64))
        self.db.query(insert, (good_id, AGENT, "client:42", "sha256:" + "0" * 64))
        self.assertEqual(self.get(action)["state"], "confirmed")

    def test_exception_ou_delai_donnent_unknown(self):
        def boom():
            raise RuntimeError("panne au milieu de l'appel")

        def lost():
            raise ConnectionError("connexion perdue")

        def slow():
            time.sleep(3)
            return Outcome.confirmed("trop tard")

        cases = {"exception": boom, "connexion": lost, "délai": slow,
                 "réponse illisible": lambda: "ok"}
        for label, behaviour in cases.items():
            with self.subTest(label):
                connector = Scripted(behaviour)
                action = self.propose("send", connector=connector)
                self.approve(action)
                started = time.monotonic()
                result = self.execute(action, connector)
                self.assertEqual(result["state"], "unknown", result)
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertEqual(connector.calls, 1)
                row = self.get(action)
                self.assertEqual(row["state"], "unknown")
                self.assertTrue(row["last_error"])
                self.refused(actions.STATE, self.execute, action, connector)
                self.assertEqual(connector.calls, 1)
        # un échec n'est « certain » que si le connecteur le dit
        connector = Scripted(lambda: Outcome.failed("refus du système externe"))
        action = self.propose("send", connector=connector)
        self.approve(action)
        self.assertEqual(self.execute(action, connector)["state"], "failed")

    def test_cancel(self):
        proposed = self.propose("send")
        self.assertEqual(actions.cancel(self.db, proposed["action_id"], by=ALICE)["state"],
                         "cancelled")
        lost = self.propose("send", args={"simulate": "lose-response"})
        self.approve(lost)
        self.assertEqual(self.execute(lost)["state"], "unknown")
        self.refused(actions.STATE, actions.cancel, self.db, lost["action_id"])
        self.refused(actions.UNKNOWN_ACTION, actions.cancel, self.db, actions.new_action_id())


# --------------------------------------------------------------------------
# concurrence et reprise
# --------------------------------------------------------------------------

class ActionsConcurrencyTest(ActionsBase):
    def run_threads(self, targets) -> None:
        threads = [threading.Thread(target=target) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_deux_executions_concurrentes_un_seul_appel(self):
        for workers in (2, 6):
            with self.subTest(workers=workers):
                slow = Counting(ShellNoopConnector(self.noop_dir, timeout=10.0), delay=0.5)
                action = self.propose("send", args={"workers": workers})
                self.approve(action)
                barrier = threading.Barrier(workers)
                results: list = []
                lock = threading.Lock()

                def attempt() -> None:
                    db = self.connect()
                    try:
                        barrier.wait(timeout=30)
                        try:
                            outcome = ("ok", self.execute(action, slow, db=db)["state"])
                        except ActionError as exc:
                            outcome = ("refused", exc.code)
                    finally:
                        db.close()
                    with lock:
                        results.append(outcome)

                self.run_threads([attempt] * workers)
                self.assertEqual(len(slow.calls), 1, results)
                self.assertEqual([r for r in results if r[0] == "ok"], [("ok", "confirmed")])
                self.assertTrue(all(code in (actions.LAUNCHED, actions.STATE)
                                    for kind, code in results if kind == "refused"), results)
                self.assertEqual(len(actions.attempts(self.db, action["action_id"])), 1)
                self.assertEqual(self.get(action)["attempts"], 1)

    def test_lancement_concurrent_en_base(self):
        """Sans les contrôles Python : la fonction de lancement seule ne laisse
        passer qu'UNE tentative et ne consomme le nonce qu'une fois."""
        action = self.propose("send")
        receipt = self.receipt(action)
        row = self.approve(action, receipt)
        workers = 8
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def attempt() -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                out = db.query(
                    "SELECT * FROM ameesh_action_launch(%s::text, %s::text, %s::text, %s::text, "
                    "%s::bigint, %s::text, %s::double precision)",
                    (action["action_id"], action["digest"], "receipt", row["auth_nonce"], None,
                     "course", 30.0))[0]["result"]
            finally:
                db.close()
            with lock:
                results.append(out)

        self.run_threads([attempt] * workers)
        self.assertEqual(sorted(results), ["ok"] + ["state"] * (workers - 1))
        self.assertEqual(self.get(action)["attempts"], 1)
        self.assertEqual(len(actions.attempts(self.db, action["action_id"])), 1)
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM mesh_consumed_nonces WHERE nonce = %s",
            (receipt["request"]["nonce"],))[0]["n"], 1)

    def test_crash_entre_launched_et_appel(self):
        action = self.propose("send")
        receipt = self.receipt(action)
        self.approve(action, receipt)
        script = os.path.join(self.tmp, "condamne.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, signal, sys\n"
                "from ameesh import actions, config, db as db_mod, receipts\n"
                "from ameesh.connectors.shell_noop import ShellNoopConnector\n"
                "class Crash(ShellNoopConnector):\n"
                "    def execute(self, action, key):\n"
                "        os.kill(os.getpid(), signal.SIGKILL)\n"
                "db = db_mod.connect(config.load())\n"
                "policy = receipts.Policy(rp_id=sys.argv[2], origins=(sys.argv[3],))\n"
                "actions.execute(db, sys.argv[1], Crash(sys.argv[4], timeout=1.0),\n"
                "                policy=policy, by='processus-condamne')\n"
                "print('jamais atteint')\n")
        proc = subprocess.run(
            [sys.executable, script, action["action_id"], RP_ID, ORIGIN, self.noop_dir],
            capture_output=True, text=True, timeout=60, env=self.env(), cwd=self.tmp)
        self.assertEqual(proc.returncode, -9, proc.stdout + proc.stderr)
        self.assertNotIn("jamais atteint", proc.stdout)
        row = self.get(action)
        self.assertEqual((row["state"], row["attempts"]), ("launched", 1))
        self.assertTrue(self.nonce_consumed(receipt))          # consommé AVEC le lancement
        self.assertEqual(self.files(), [])
        # avant l'échéance : jamais de second appel
        self.refused(actions.LAUNCHED, self.execute, action)
        self.assertEqual(self.noop.calls, [])
        # « redémarrage » : passé l'échéance, l'action est réputée d'issue inconnue
        self.wait_for(lambda: self.get(action)["stale"], timeout=15)
        queue = actions.decisions(self.db)
        self.assertEqual([r["action_id"] for r in queue["interrupted"]], [action["action_id"]])
        self.assertEqual(actions.recover(self.db, grace=0), [action["action_id"]])
        row = self.get(action)
        self.assertEqual(row["state"], "unknown")
        attempt, = actions.attempts(self.db, action["action_id"])
        self.assertEqual((attempt["state"], attempt["settled_by"]), ("unknown", "recover"))
        self.refused(actions.STATE, self.execute, action)
        self.assertEqual(self.noop.calls, [])                   # aucun appel automatique
        # shell-noop garantit la déduplication : nouvelle tentative, MÊME action_id
        actions.retry(self.db, action["action_id"], self.receipt(action), policy=POLICY, by=ALICE)
        result = self.execute(action)
        self.assertEqual((result["state"], result["attempt"]), ("confirmed", 2))
        self.assertEqual(self.noop.calls, [(action["action_id"], action["action_id"], 2)])
        self.assertEqual(self.files(), [action["action_id"] + ".json"])

    def test_grant_plafond_respecte_en_concurrence(self):
        grant = self.register_grant(max_amount=100)
        slow = Counting(ShellNoopConnector(self.noop_dir, timeout=10.0), delay=0.2)
        batch = [self.propose("refund", connector=slow, amount=30, currency="EUR",
                              args={"order": i}) for i in range(6)]
        for action in batch:
            bound = actions.approve(self.db, action["action_id"], None, standing=True, by="porte")
            self.assertEqual((bound["auth_kind"], bound["auth_grant_id"]), ("standing", grant))
        barrier = threading.Barrier(len(batch))
        results: list = []
        lock = threading.Lock()

        def attempt(action) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                try:
                    outcome = ("ok", actions.execute(db, action["action_id"], slow,
                                                     by="porte")["state"])
                except ActionError as exc:
                    outcome = ("refused", exc.code)
            finally:
                db.close()
            with lock:
                results.append(outcome)

        self.run_threads([lambda a=a: attempt(a) for a in batch])
        self.assertEqual(sorted(results), [("ok", "confirmed")] * 3 + [("refused", "no_cover")] * 3)
        self.assertEqual(len(slow.calls), 3)
        self.assertEqual(self.grant_consumed(grant), 90)
        states = sorted(self.get(a)["state"] for a in batch)
        self.assertEqual(states, ["approved"] * 3 + ["confirmed"] * 3)
        live = self.db.query("SELECT count(*)::int AS n FROM standing_reservations "
                             "WHERE released_at IS NULL")[0]["n"]
        self.assertEqual(live, 3)

    def test_grant_libere_seulement_sur_failed(self):
        grant = self.register_grant(max_amount=100)

        def run(args):
            action = self.propose("refund", amount=30, currency="EUR", args=args)
            actions.approve(self.db, action["action_id"], None, standing=True, by="porte")
            return action, actions.execute(self.db, action["action_id"], self.noop, by="porte")

        failed, result = run({"simulate": "fail"})
        self.assertEqual((result["state"], result["released"]), ("failed", 30))
        self.assertEqual(self.grant_consumed(grant), 0)
        lost, result = run({"simulate": "lose-response"})
        self.assertEqual((result["state"], result["released"]), ("unknown", None))
        self.assertEqual(self.grant_consumed(grant), 30)       # inconnue : jamais libérée
        # nouvelle tentative de la même action (dédup garantie) : même réservation
        actions.retry(self.db, lost["action_id"], None, standing=True, by="porte")
        self.assertEqual(actions.execute(self.db, lost["action_id"], self.noop,
                                         by="porte")["state"], "confirmed")
        self.assertEqual(self.grant_consumed(grant), 30)
        reservations = self.db.query(
            "SELECT action_id, released_reason FROM standing_reservations ORDER BY id")
        self.assertEqual([(r["action_id"], (r["released_reason"] or "")[:6]) for r in reservations],
                         [(failed["action_id"], "failed"), (lost["action_id"], "")])
        # plafond : 30 + 80 > 100
        big = self.propose("refund", amount=80, currency="EUR", args={"order": "gros"})
        self.refused(actions.NO_COVER, actions.approve, self.db, big["action_id"], None,
                     standing=True)
        # après failed, la même action peut réserver de nouveau
        actions.retry(self.db, failed["action_id"], None, standing=True, by="porte")
        self.assertEqual(actions.execute(self.db, failed["action_id"], self.noop,
                                         by="porte")["state"], "failed")
        self.assertEqual(self.grant_consumed(grant), 30)


# --------------------------------------------------------------------------
# échéances recontrôlées APRÈS le dernier verrou (règle de 0011_receipts.sql)
# --------------------------------------------------------------------------

class ActionsDeadlineRaceTest(ActionsBase):
    """Courses réelles : une autre connexion tient un verrou que le lancement
    (ou le remplacement) doit prendre, par un simple `SELECT … FOR UPDATE` qui
    laisse la ligne INTACTE — PostgreSQL ne réévalue alors pas le WHERE de
    l'instruction qui attendait (EvalPlanQual) — et ne le relâche qu'APRÈS
    l'échéance du reçu (ou du grant), heure de la base. L'appel a passé la
    vérification Python avant l'échéance ; il doit pourtant être refusé :
    rien n'est écrit (nonce intact, aucune tentative, aucune réservation) et
    le connecteur n'est pas appelé. Témoin : relâché avant l'échéance, il part.
    """

    #: secondes entre la préparation de la course et l'échéance
    MARGIN = 4
    #: échéance des témoins : bien après la fin de l'attente
    LATER = 60

    def clock(self) -> float:
        """L'heure réelle de la BASE (celle des contrôles SQL)."""
        return float(self.db.query(
            "SELECT extract(epoch from clock_timestamp())::float8 AS epoch")[0]["epoch"])

    def lock_action(self, action: dict) -> str:
        return ("SELECT action_id FROM actions WHERE action_id = %s FOR UPDATE;"
                % db_mod.sql_literal(action["action_id"]))

    def lock_authenticator(self, _action: dict) -> str:
        ident = int(self.db.query(
            "SELECT id FROM authenticators WHERE credential_id = %s AND revoked_at IS NULL",
            (self.alice.credential_id,))[0]["id"])
        return "SELECT id FROM authenticators WHERE id = %d FOR UPDATE;" % ident

    def lock_grant(self, action: dict) -> str:
        return ("SELECT id FROM standing_approvals WHERE id = %d FOR UPDATE;"
                % int(self.get(action)["auth_grant_id"]))

    def race(self, statements: str, call, deadline: float, *, expires: bool):
        """`call` (sur sa connexion) attend le verrou tenu par `statements`,
        ligne intacte ; relâché après l'échéance (`expires`) ou aussitôt."""
        with HeldTransaction(self, statements) as holder:
            worker = Worker(self, call)
            self.assertTrue(holder.blocked(worker), "l'appel n'a pas attendu le verrou")
            self.assertLess(self.clock(), deadline,
                            "banc trop lent : l'échéance est passée avant l'attente")
            if expires:
                self.wait_for(lambda: self.clock() > deadline + 0.2, timeout=self.MARGIN + 10)
                self.assertTrue(worker.is_alive(), "l'appel n'attendait plus le verrou")
            holder.commit()
            result = worker.result()
        if not expires:
            self.assertLess(self.clock(), deadline, "témoin : l'échéance est passée")
        return result

    def executing(self, action: dict):
        def call(db):
            try:
                return actions.execute(db, action["action_id"], self.noop, policy=POLICY,
                                       by="porte")
            except ActionError as exc:
                return exc
        return call

    def approved_with_receipt(self, deadline: float, **args) -> tuple[dict, dict]:
        """Action approuvée par un reçu dont `exp` vaut `deadline`."""
        action = self.propose("send", args=args)
        iat = int(self.clock()) - 10
        receipt = self.receipt(action, now=iat, ttl=int(deadline) - iat)
        self.approve(action, receipt)
        return action, receipt

    def approved_with_grant(self, deadline: float, **args) -> tuple[dict, int]:
        """Action couverte par un grant dont l'échéance (until) vaut `deadline`."""
        grant = self.register_grant(max_amount=100)
        self.db.query("UPDATE standing_approvals SET valid_until = to_timestamp(%s) "
                      "WHERE id = %s RETURNING id", (deadline, grant))
        action = self.propose("refund", amount=30, currency="EUR", args=args)
        actions.approve(self.db, action["action_id"], None, standing=True, by="porte")
        return action, grant

    def assertNotLaunched(self, action: dict) -> None:
        row = self.get(action)
        self.assertEqual((row["state"], row["attempts"]), ("approved", 0))
        self.assertEqual(actions.attempts(self.db, action["action_id"]), [])
        self.assertIn("refused", self.event_names(action))

    def test_lancement_refuse_si_le_recu_echoit_pendant_l_attente(self):
        """Verrou de la ligne de l'action (pris en premier par le lancement) ou
        de l'authentificateur signataire (FOR SHARE de la consommation), tenu
        sans modification de la ligne, relâché après `exp` : refus `expired`."""
        for label, lock in (("action : SELECT … FOR UPDATE", self.lock_action),
                            ("authentificateur : SELECT … FOR UPDATE", self.lock_authenticator)):
            with self.subTest(label):
                deadline = int(self.clock()) + self.MARGIN
                action, receipt = self.approved_with_receipt(deadline, lock=label)
                out = self.race(lock(action), self.executing(action), deadline, expires=True)
                self.assertIsInstance(out, ActionError, out)
                self.assertEqual(out.code, receipts.EXPIRED, out.reason)
                self.assertNotLaunched(action)
                self.assertFalse(self.nonce_consumed(receipt))
        self.assertEqual((self.noop.calls, self.files()), ([], []))

    def test_lancement_refuse_si_le_grant_echoit_pendant_l_attente(self):
        """Même course pour un grant : verrou de l'action ou du grant, tenu sans
        modification, relâché après `until` : refus, aucune réservation."""
        for label, lock in (("action : SELECT … FOR UPDATE", self.lock_action),
                            ("grant : SELECT … FOR UPDATE", self.lock_grant)):
            with self.subTest(label):
                deadline = self.clock() + self.MARGIN
                action, grant = self.approved_with_grant(deadline, lock=label)
                out = self.race(lock(action), self.executing(action), deadline, expires=True)
                self.assertIsInstance(out, ActionError, out)
                self.assertEqual(out.code, receipts.EXPIRED, out.reason)
                self.assertNotLaunched(action)
                self.assertEqual(self.grant_consumed(grant), 0)
                self.assertEqual(self.db.query(
                    "SELECT count(*)::int AS n FROM standing_reservations WHERE grant_id = %s",
                    (grant,))[0]["n"], 0)
        self.assertEqual((self.noop.calls, self.files()), ([], []))

    def test_remplacement_refuse_si_la_decision_echoit_pendant_l_attente(self):
        """Décision « assumer le doublon » : verrou de l'action remplacée, tenu
        sans modification, relâché après `exp` : aucune action créée, nonce
        intact."""
        lossy = self.gh(mode="drop")
        action = self.propose("merge", connector=lossy, target=PR)
        self.approve(action)
        self.assertEqual(self.execute(action, lossy)["state"], "unknown")
        deadline = int(self.clock()) + self.MARGIN
        iat = int(self.clock()) - 10
        decision = self.receipt(action, assume_duplicate=True, now=iat, ttl=deadline - iat)

        def replacing(db):
            try:
                return actions.replace(db, action["action_id"], decision, policy=POLICY, by=ALICE)
            except ActionError as exc:
                return exc

        out = self.race(self.lock_action(action), replacing, deadline, expires=True)
        self.assertIsInstance(out, ActionError, out)
        self.assertEqual(out.code, receipts.EXPIRED, out.reason)
        self.assertFalse(self.nonce_consumed(decision))
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM actions")[0]["n"], 1)
        old = self.get(action)
        self.assertEqual((old["state"], old["replaced_by"]), ("unknown", None))

    def test_temoins_verrou_relache_avant_l_echeance(self):
        """Les mêmes attentes, terminées avant l'échéance : le lancement part."""
        deadline = int(self.clock()) + self.LATER
        cases = [("reçu, " + label, lock, self.approved_with_receipt)
                 for label, lock in (("action", self.lock_action),
                                     ("authentificateur", self.lock_authenticator))]
        cases += [("grant, " + label, lock, self.approved_with_grant)
                  for label, lock in (("action", self.lock_action), ("grant", self.lock_grant))]
        for label, lock, prepare in cases:
            with self.subTest(label):
                action, _auth = prepare(deadline, lock=label)
                out = self.race(lock(action), self.executing(action), deadline, expires=False)
                self.assertNotIsInstance(out, ActionError, out)
                self.assertEqual((out["state"], out["attempt"]), ("confirmed", 1))
        self.assertEqual(len(self.noop.calls), len(cases))


# --------------------------------------------------------------------------
# issue inconnue : déduplication, réconciliation, remplacement
# --------------------------------------------------------------------------

class ActionsUnknownTest(ActionsBase):
    def test_unknown_dedupe_garanti_retry_meme_action_id(self):
        action = self.propose("send", args={"simulate": "lose-response"})
        first = self.receipt(action)
        self.approve(action, first)
        self.assertEqual(self.execute(action)["state"], "unknown")
        self.assertEqual(self.files(), [action["action_id"] + ".json"])  # l'effet a eu lieu
        self.refused(actions.STATE, self.execute, action)
        self.refused(receipts.REPLAY, actions.retry, self.db, action["action_id"], first,
                     policy=POLICY)
        self.refused(actions.RECEIPT_REQUIRED, actions.retry, self.db, action["action_id"], None,
                     policy=POLICY)
        # une décision « assumer le doublon » ne vaut pas approbation d'une tentative
        self.refused(receipts.DIGEST_MISMATCH, actions.retry, self.db, action["action_id"],
                     self.receipt(action, assume_duplicate=True), policy=POLICY)
        retried = actions.retry(self.db, action["action_id"], self.receipt(action),
                                connector=self.noop, policy=POLICY, by=ALICE)
        self.assertEqual((retried["state"], retried["action_id"]), ("approved", action["action_id"]))
        result = self.execute(action)
        self.assertEqual((result["state"], result["attempt"]), ("confirmed", 2))
        self.assertIn("déjà fait", result["detail"])
        self.assertEqual([c[1] for c in self.noop.calls], [action["action_id"]] * 2)
        self.assertEqual(self.files(), [action["action_id"] + ".json"])  # un seul effet
        self.assertEqual([a["state"] for a in actions.attempts(self.db, action["action_id"])],
                         ["unknown", "confirmed"])

    def test_unknown_reconcilie_par_le_connecteur(self):
        action = self.propose("send", args={"simulate": "lose-response"})
        self.approve(action)
        self.assertEqual(self.execute(action)["state"], "unknown")
        result = actions.reconcile(self.db, action["action_id"], self.noop, by=ALICE)
        self.assertEqual((result["found"], result["state"]), (True, "confirmed"))
        self.assertEqual(self.get(action)["state"], "confirmed")
        self.assertEqual(len(self.noop.calls), 1)
        self.assertEqual(actions.attempts(self.db, action["action_id"])[0]["settled_by"],
                         "reconcile")
        self.refused(actions.STATE, actions.reconcile, self.db, action["action_id"], self.noop)

    def test_unknown_sans_dedupe_reconcile_introuvable_attend_un_humain(self):
        lossy = self.gh(mode="drop")
        action = self.propose("merge", connector=lossy, target=PR, args={"method": "squash"})
        self.assertEqual((action["class"], action["dedupe"]), ("irreversible", "none"))
        self.approve(action)
        result = self.execute(action, lossy)
        self.assertEqual(result["state"], "unknown", result)
        self.assertEqual(len(lossy.calls), 1)
        self.refused(actions.STATE, self.execute, action, lossy)
        self.refused(actions.DEDUPE, actions.retry, self.db, action["action_id"],
                     self.receipt(action), connector=lossy, policy=POLICY)
        blind = self.gh(view="absent")
        result = actions.reconcile(self.db, action["action_id"], blind, by=ALICE)
        self.assertEqual((result["found"], result["state"]), (False, "unknown"))
        self.assertEqual(self.get(action)["state"], "unknown")
        self.assertIn("reconcile_none", self.event_names(action))
        queue = actions.decisions(self.db, human=ALICE)
        unknown, = queue["unknown"]
        self.assertEqual(unknown["action_id"], action["action_id"])
        self.assertIn("replace", unknown["hint"])
        self.refused(actions.DEDUPE, actions.retry, self.db, action["action_id"],
                     self.receipt(action), policy=POLICY)
        self.assertEqual(len(lossy.calls) + len(blind.calls), 1)
        self.assertEqual(self.pr()["state"], "OPEN")
        with open(self.gh_log, encoding="utf-8") as fh:
            calls = [json.loads(line)["argv"] for line in fh]
        self.assertEqual(calls[0], ["pr", "merge", "12", "--repo", "o/r", "--squash"])

    def test_replace_exige_une_decision_qui_assume_le_doublon(self):
        lossy = self.gh(mode="drop")
        action = self.propose("merge", connector=lossy, target=PR, args={"method": "merge"})
        self.approve(action)
        self.assertEqual(self.execute(action, lossy)["state"], "unknown")
        action_id = action["action_id"]
        # sans décision ; avec un reçu d'approbation ordinaire ; pour une autre action ;
        # par une clé d'outil ; signée par un autre approbateur que celui nommé
        self.refused(actions.DECISION_REQUIRED, actions.replace, self.db, action_id, None,
                     policy=POLICY)
        self.refused(receipts.DIGEST_MISMATCH, actions.replace, self.db, action_id,
                     self.receipt(action), policy=POLICY)
        other = self.propose("merge", connector=lossy, target=PR, args={"method": "rebase"})
        self.refused(receipts.ACTION_MISMATCH, actions.replace, self.db, action_id,
                     self.receipt(other, assume_duplicate=True), policy=POLICY)
        self.refused(receipts.FACADE, actions.replace, self.db, action_id,
                     self.receipt(action, soft=self.alice_tool, assume_duplicate=True),
                     policy=POLICY)
        self.assertIsNone(self.get(action)["replaced_by"])
        decision = self.receipt(action, assume_duplicate=True)
        new = actions.replace(self.db, action_id, decision, policy=POLICY, by=ALICE)
        self.assertNotEqual(new["action_id"], action_id)
        self.assertEqual((new["state"], new["replaces"], new["replace_approver"], new["proposed_by"]),
                         ("proposed", action_id, ALICE, ALICE))
        for key in ("project", "connector", "operation", "target", "args", "class", "dedupe"):
            self.assertEqual(new[key], action[key], key)
        self.assertEqual(new["digest"], actions.digest(new))
        self.assertTrue(self.nonce_consumed(decision))
        old = self.get(action)
        self.assertEqual((old["state"], old["replaced_by"]), ("unknown", new["action_id"]))
        self.assertIn("replaced", self.event_names(action))
        # une seule fois ; plus de tentative de l'ancienne
        self.refused(actions.REPLACED, actions.replace, self.db, action_id,
                     self.receipt(action, assume_duplicate=True), policy=POLICY)
        self.refused(actions.REPLACED, actions.retry, self.db, action_id, self.receipt(action),
                     policy=POLICY)
        queue = actions.decisions(self.db)
        self.assertEqual(queue["unknown"], [])
        self.assertEqual([r["action_id"] for r in queue["approvals"]],
                         [other["action_id"], new["action_id"]])
        # la nouvelle action suit le protocole complet
        self.refused(actions.RECEIPT_REQUIRED, self.execute, new, self.gh())
        self.approve(new)
        self.assertEqual(self.execute(new, self.gh())["state"], "confirmed")
        self.assertEqual(self.pr()["state"], "MERGED")

    def test_replace_decision_echue_entre_verification_et_transaction(self):
        """Décision valide à la vérification Python, échue avant la transaction
        SQL : refus, aucune action créée, nonce intact."""
        lossy = self.gh(mode="drop")
        action = self.propose("merge", connector=lossy, target=PR)
        self.approve(action)
        self.assertEqual(self.execute(action, lossy)["state"], "unknown")
        decision = self.receipt(action, assume_duplicate=True, now=int(time.time()) - 598,
                                ttl=600)
        paused = Paused(self.db, "ameesh_action_replace(", until=decision["request"]["exp"] + 0.3)
        self.refused(receipts.EXPIRED, actions.replace, paused, action["action_id"], decision,
                     policy=POLICY, by=ALICE)
        self.assertEqual(paused.paused, 1)                     # vérifiée en Python avant l'échéance
        self.assertFalse(self.nonce_consumed(decision))
        self.assertEqual(self.db.query("SELECT count(*)::int AS n FROM actions")[0]["n"], 1)
        old = self.get(action)
        self.assertEqual((old["state"], old["replaced_by"]), ("unknown", None))
        self.assertNotIn("replaced", self.event_names(action))
        # une décision fraîche remplace
        new = actions.replace(self.db, action["action_id"],
                              self.receipt(action, assume_duplicate=True), policy=POLICY, by=ALICE)
        self.assertEqual((new["replaces"], new["state"]), (action["action_id"], "proposed"))

    def test_replace_concurrent_une_seule_nouvelle_action(self):
        lossy = self.gh(mode="drop")
        action = self.propose("merge", connector=lossy, target=PR)
        self.approve(action)
        self.assertEqual(self.execute(action, lossy)["state"], "unknown")
        decisions = [self.receipt(action, assume_duplicate=True) for _ in range(4)]
        barrier = threading.Barrier(len(decisions))
        results: list = []
        lock = threading.Lock()

        def attempt(receipt) -> None:
            db = self.connect()
            try:
                barrier.wait(timeout=30)
                try:
                    outcome = ("ok", actions.replace(db, action["action_id"], receipt,
                                                     policy=POLICY, by=ALICE)["action_id"])
                except ActionError as exc:
                    outcome = ("refused", exc.code)
            finally:
                db.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt, args=(r,)) for r in decisions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1, results)
        count = self.db.query("SELECT count(*)::int AS n FROM actions WHERE replaces = %s",
                              (action["action_id"],))[0]["n"]
        self.assertEqual(count, 1)
        consumed = sum(1 for r in decisions if self.nonce_consumed(r))
        self.assertEqual(consumed, 1)

    def test_issue_tardive_certaine_enregistree(self):
        """Délai dépassé, puis le connecteur réussit : l'issue certaine n'est pas
        perdue, elle règle l'inconnue (même tentative) avec sa référence."""
        def slow():
            time.sleep(1.5)
            return Outcome.confirmed("ref-tardive", "fait, mais après le délai")

        connector = Scripted(slow)
        action = self.propose("send", connector=connector)
        self.approve(action)
        result = self.execute(action, connector)
        self.assertEqual((result["state"], result["external_ref"]), ("unknown", None))

        def settled():
            row = self.get(action)
            return row if row["state"] != "unknown" else None

        row = self.wait_for(settled, timeout=15)
        self.assertEqual((row["state"], row["external_ref"], row["attempts"]),
                         ("confirmed", "ref-tardive", 1))
        attempt, = actions.attempts(self.db, action["action_id"])
        self.assertEqual((attempt["state"], attempt["external_ref"], attempt["settled_by"]),
                         ("confirmed", "ref-tardive", "connector-late"))
        self.assertEqual(self.event_names(action),
                         ["proposed", "approved", "launched", "unknown", "confirmed"])
        self.assertEqual(connector.calls, 1)

    def test_issue_tardive_ignoree_si_deja_reconciliee(self):
        """Une réconciliation a tranché avant l'issue tardive : rien n'est
        écrasé, l'issue tardive est seulement journalisée."""
        gate = threading.Event()

        def held():
            gate.wait(30)
            return Outcome.confirmed("ref-tardive")

        connector = Scripted(held, reconciled=Outcome.confirmed("ref-reconcile", "relu"))
        action = self.propose("send", connector=connector)
        self.approve(action)
        self.assertEqual(self.execute(action, connector)["state"], "unknown")
        result = actions.reconcile(self.db, action["action_id"], connector, by=ALICE)
        self.assertEqual((result["found"], result["state"]), (True, "confirmed"))
        gate.set()
        self.wait_for(lambda: "late_ignored" in self.event_names(action), timeout=15)
        row = self.get(action)
        self.assertEqual((row["state"], row["external_ref"]), ("confirmed", "ref-reconcile"))
        attempt, = actions.attempts(self.db, action["action_id"])
        self.assertEqual((attempt["state"], attempt["external_ref"], attempt["settled_by"]),
                         ("confirmed", "ref-reconcile", "reconcile"))
        late, = [e for e in actions.events(self.db, action["action_id"])
                 if e["event"] == "late_ignored"]
        self.assertEqual(late["attempt_no"], 1)
        self.assertIn("ref-tardive", late["note"])

    def test_issue_tardive_ignoree_apres_nouvelle_tentative(self):
        """Un humain a autorisé une nouvelle tentative (déduplication garantie)
        avant l'issue tardive de la première : elle ne règle rien."""
        gate = threading.Event()

        def held():
            gate.wait(30)
            return Outcome.failed("refus tardif")

        connector = Scripted(held, dedupe="guaranteed")
        action = self.propose("send", connector=connector)
        self.approve(action)
        self.assertEqual(self.execute(action, connector)["state"], "unknown")
        actions.retry(self.db, action["action_id"], self.receipt(action), connector=connector,
                      policy=POLICY, by=ALICE)
        gate.set()
        self.wait_for(lambda: "late_ignored" in self.event_names(action), timeout=15)
        row = self.get(action)
        self.assertEqual((row["state"], row["attempts"]), ("approved", 1))
        attempt, = actions.attempts(self.db, action["action_id"])
        self.assertEqual(attempt["state"], "unknown")

    def test_git_merge_reconcile_ouverte_reste_inconnue_fermee_echoue(self):
        lossy = self.gh(mode="drop")
        action = self.propose("merge", connector=lossy, target=PR, args={"method": "squash"})
        self.approve(action)
        self.assertEqual(self.execute(action, lossy)["state"], "unknown")
        # PR ouverte : la demande perdue est peut-être en file de fusion, rien de certain
        result = actions.reconcile(self.db, action["action_id"], self.gh(), by=ALICE)
        self.assertEqual((result["found"], result["state"]), (False, "unknown"))
        self.assertIn("ouverte", result["detail"])
        self.assertEqual(self.get(action)["state"], "unknown")
        self.refused(actions.DEDUPE, actions.retry, self.db, action["action_id"],
                     self.receipt(action), connector=self.gh(), policy=POLICY)
        # PR fermée sans fusion : la fusion n'a pas eu lieu et ne peut plus avoir lieu
        self.set_pr(state="CLOSED")
        result = actions.reconcile(self.db, action["action_id"], self.gh(), by=ALICE)
        self.assertEqual((result["found"], result["state"]), (True, "failed"))
        self.assertIn("fermée sans fusion", result["detail"])
        self.assertEqual(self.merge_calls(), 1)

    def test_git_merge_file_de_fusion_reste_inconnue_jusqu_a_la_fusion(self):
        """`gh pr merge` code 0 mais PR OPEN (file de fusion) : pas une preuve."""
        queued = self.gh(mode="queue")
        action = self.propose("merge", connector=queued, target=PR)
        self.approve(action)
        result = self.execute(action, queued)
        self.assertEqual((result["state"], result["external_ref"]), ("unknown", None), result)
        self.assertIn("file de fusion", result["detail"])
        self.assertEqual(self.pr()["state"], "OPEN")
        # toujours en file : la réconciliation ne tranche pas, aucune nouvelle soumission
        result = actions.reconcile(self.db, action["action_id"], self.gh(), by=ALICE)
        self.assertEqual((result["found"], result["state"]), (False, "unknown"))
        self.refused(actions.DEDUPE, actions.retry, self.db, action["action_id"],
                     self.receipt(action), connector=self.gh(), policy=POLICY)
        # la file aboutit : MERGED avec son commit de fusion → confirmed
        result = actions.reconcile(self.db, action["action_id"], self.gh(view="land"), by=ALICE)
        self.assertEqual((result["found"], result["state"]), (True, "confirmed"))
        oid = self.pr()["mergeCommit"]["oid"]
        self.assertEqual(result["external_ref"], oid)
        row = self.get(action)
        self.assertEqual((row["state"], row["external_ref"]), ("confirmed", oid))
        self.assertEqual((self.pr()["merges"], self.merge_calls()), (1, 1))

    def test_git_merge_reponse_perdue_puis_ouverte_puis_fusionnee(self):
        """Réponse perdue, PR encore OPEN (mise en file) : jamais `failed`, donc
        jamais de seconde soumission ; la fusion arrive plus tard."""
        lost = self.gh(mode="queue-lose")
        action = self.propose("merge", connector=lost, target=PR)
        self.approve(action)
        self.assertEqual(self.execute(action, lost)["state"], "unknown")
        result = actions.reconcile(self.db, action["action_id"], self.gh(), by=ALICE)
        self.assertEqual((result["found"], result["state"]), (False, "unknown"))
        self.assertEqual(self.get(action)["state"], "unknown")
        self.assertIn("reconcile_none", self.event_names(action))
        self.refused(actions.DEDUPE, actions.retry, self.db, action["action_id"],
                     self.receipt(action), connector=self.gh(), policy=POLICY)
        self.refused(actions.STATE, self.execute, action, self.gh())
        result = actions.reconcile(self.db, action["action_id"], self.gh(view="land"), by=ALICE)
        self.assertEqual((result["found"], result["state"]), (True, "confirmed"))
        self.assertEqual(result["external_ref"], self.pr()["mergeCommit"]["oid"])
        self.assertEqual((self.pr()["merges"], self.merge_calls()), (1, 1))

    def test_git_merge_confirmed_sur_preuve_failed_sur_echec_certain(self):
        # refus explicite et documenté de gh (contrôle préalable) : échec certain
        refused = self.gh(mode="refuse")
        action = self.propose("merge", connector=refused, target=PR)
        self.approve(action)
        result = self.execute(action, refused)
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("refus explicite", result["detail"])
        ok = self.gh()
        actions.retry(self.db, action["action_id"], self.receipt(action), connector=ok,
                      policy=POLICY, by=ALICE)
        result = self.execute(action, ok)
        self.assertEqual((result["state"], result["attempt"]), ("confirmed", 2))
        self.assertEqual(result["external_ref"], self.pr()["mergeCommit"]["oid"])
        # erreur, puis PR fermée sans fusion : échec certain
        self.set_pr("o/r#13", state="CLOSED")
        closed = self.propose("merge", connector=ok, target="o/r#13")
        self.approve(closed)
        result = self.execute(closed, ok)
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("fermée sans fusion", result["detail"])
        # code 0 mais PR illisible : pas de preuve, l'issue reste inconnue
        self.set_pr("o/r#14")
        blind = self.gh(view="absent")
        unread = self.propose("merge", connector=blind, target="o/r#14")
        self.approve(unread)
        result = self.execute(unread, blind)
        self.assertEqual((result["state"], result["external_ref"]), ("unknown", None), result)
        self.assertEqual(self.pr("o/r#14")["state"], "MERGED")
        result = actions.reconcile(self.db, unread["action_id"], ok, by=ALICE)
        self.assertEqual((result["state"], result["external_ref"]),
                         ("confirmed", self.pr("o/r#14")["mergeCommit"]["oid"]))

    def test_git_merge_reponse_perdue_apres_effet(self):
        lose = self.gh(mode="lose")
        action = self.propose("merge", connector=lose, target=PR)
        self.approve(action)
        result = self.execute(action, lose)
        self.assertEqual(result["state"], "confirmed")          # relu : MERGED
        self.assertEqual(result["external_ref"], self.pr()["mergeCommit"]["oid"])

    def test_git_merge_delai_depasse(self):
        env_hang = self.gh(mode="hang", timeout=3.0)
        env_hang.inner.env["AMEESH_FAKE_GH_SLEEP"] = "10"
        action = self.propose("merge", connector=env_hang, target=PR)
        self.approve(action)
        started = time.monotonic()
        result = self.execute(action, env_hang)
        self.assertEqual(result["state"], "unknown")
        self.assertLess(time.monotonic() - started, 8.0)
        self.wait_for(lambda: self.pr()["state"] == "MERGED")  # l'effet a eu lieu
        result = actions.reconcile(self.db, action["action_id"], self.gh(), by=ALICE)
        self.assertEqual(result["state"], "confirmed")


# --------------------------------------------------------------------------
# fil lisible (§7.1) : chaque transition y est écrite
# --------------------------------------------------------------------------

class ActionsThreadTest(ActionsBase):
    def entries(self, project: str = "demo", lot=None) -> list:
        return fil.transport_for(self.cfg).read(fil.ThreadRef(project, lot))

    def events(self, project: str = "demo", lot=None) -> list:
        return [(e.meta.get("action_id"), e.meta.get("event"))
                for e in self.entries(project, lot)]

    def test_cycle_dans_le_fil_sans_secret(self):
        secret = "s3cr3t-" + "x" * 8
        opaque = "A" * 64
        action = self.propose(args={"subject": "facture de mars", "n": 1, "api_token": secret,
                                    "blob": opaque, "lignes": [1, 2, 3]},
                              approvers=[ALICE])
        receipt = self.receipt(action)
        self.approve(action, receipt)
        self.assertEqual(self.execute(action)["state"], "confirmed")
        entries = self.entries()
        action_id = action["action_id"]
        self.assertEqual(self.events(), [(action_id, event) for event in
                                         ("proposed", "approved", "launched", "confirmed")])
        proposed, approved, launched, confirmed = entries
        self.assertEqual((proposed.author, proposed.recipient), (AGENT, ALICE))
        self.assertEqual((approved.author, launched.author), (ALICE, "ameesh:porte"))
        self.assertIn("Action %s proposée par %s : shell-noop send sur client:42 (projet demo, "
                      "classe irreversible)" % (action_id, AGENT), proposed.text)
        self.assertIn("approbation humaine (reçu) requise ; approbateurs : human:alice",
                      proposed.text)
        self.assertIn("api_token=<masqué>", proposed.text)
        self.assertIn("blob=<masqué, 64 caractères>", proposed.text)
        self.assertIn("lignes=[3 élément(s)]", proposed.text)
        self.assertIn("subject=facture de mars", proposed.text)
        self.assertIn("approuvée par human:alice (webauthn", approved.text)
        self.assertIn("lancée", launched.text)
        self.assertIn("confirmée", confirmed.text)
        everything = "\n".join(e.text + json.dumps(e.meta) for e in entries)
        request = receipt["request"]
        for leak in (secret, opaque, request["nonce"], receipt["proof"]["signature"]):
            self.assertNotIn(leak, everything)
        self.assertEqual(confirmed.meta["state"], "confirmed")
        self.assertEqual(confirmed.meta["attempt"], 1)
        # le fil est indexé comme les messages
        [row] = self.db.query("SELECT project, entries FROM thread_index")
        self.assertEqual((row["project"], row["entries"]), ("demo", 4))

    def test_lot_inconnue_reconciliation_annulation_remplacement(self):
        item = work.add(self.db, title="Fusion du lot 7")
        lossy = self.gh(mode="lose", view="absent")
        action = self.propose("merge", connector=lossy, target=PR, args={"method": "squash"},
                              work_item=int(item["id"]))
        self.approve(action)
        self.assertEqual(self.execute(action, lossy)["state"], "unknown")
        actions.reconcile(self.db, action["action_id"], self.gh(), by=ALICE)
        thread = self.entries("demo", str(item["id"]))
        self.assertEqual([e.meta["event"] for e in thread],
                         ["proposed", "approved", "launched", "unknown", "confirmed"])
        self.assertIn("Aucune nouvelle tentative automatique : ameesh action reconcile %s"
                      % action["action_id"], thread[3].text)
        self.assertIn("réconciliation", thread[4].text)
        self.assertEqual(self.entries(), [])                # le fil du lot, pas celui du projet

        cancelled = self.propose()
        actions.cancel(self.db, cancelled["action_id"], by=ALICE, note="plus utile")
        self.assertEqual(self.events()[-1], (cancelled["action_id"], "cancelled"))
        self.assertIn("plus utile", self.entries()[-1].text)

        self.set_pr("o/r#13")
        dropped = self.propose("merge", connector=self.gh(mode="drop"), target="o/r#13")
        self.approve(dropped)
        self.assertEqual(self.execute(dropped, self.gh(mode="drop"))["state"], "unknown")
        new = actions.replace(self.db, dropped["action_id"],
                              self.receipt(dropped, assume_duplicate=True), policy=POLICY,
                              by=ALICE)
        self.assertEqual(self.events()[-2:], [(dropped["action_id"], "replaced"),
                                              (new["action_id"], "proposed")])
        replaced, proposed = self.entries()[-2:]
        self.assertIn("remplacée par %s (doublon assumé par human:alice)" % new["action_id"],
                      replaced.text)
        self.assertIn("Elle remplace %s" % dropped["action_id"], proposed.text)

    def test_fil_inaccessible_ne_bloque_rien(self):
        broken = os.path.join(self.tmp, "pas-un-dossier")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("x")
        db = db_mod.connect(dataclasses.replace(self.cfg, threads_dir=broken))
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                action = actions.propose(db, self.noop, project="demo", operation="send",
                                         target="client:1", proposed_by=AGENT)
                actions.cancel(db, action["action_id"], by=ALICE)
        finally:
            db.close()
        self.assertEqual(self.get(action)["state"], "cancelled")
        self.assertIn("écriture du fil impossible", err.getvalue())

    def test_resume_des_arguments(self):
        self.assertEqual(actions.args_summary({}), "aucun")
        self.assertEqual(actions.args_summary({"password": "x", "n": None, "ok": True,
                                               "o": {"a": 1}}),
                         "n=null, o={1 clé(s)}, ok=true, password=<masqué>")
        self.assertTrue(actions.args_summary({"t": "mot " * 30}).endswith("…"))
        self.assertLessEqual(len(actions.args_summary({"k%d" % i: i for i in range(100)})), 200)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class ActionsCliTest(ActionsBase):
    def cli_env(self, **extra) -> dict:
        return self.env(AMEESH_APPROVE_RP_ID=RP_ID, AMEESH_APPROVE_ORIGINS=ORIGIN,
                        AMEESH_NOOP_DIR=self.noop_dir, AMEESH_GH_BIN=FAKE_GH,
                        AMEESH_FAKE_GH_STATE=self.gh_state, AMEESH_FAKE_GH_LOG=self.gh_log,
                        **extra)

    def run_cli(self, *args, env=None):
        return self.mesh(*args, env=env or self.cli_env())

    def write(self, receipt) -> str:
        path = os.path.join(self.tmp, "recu-%s.json" % os.urandom(4).hex())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh)
        return path

    def signed(self, action_id: str, *extra, soft=None) -> str:
        proc = self.run_cli("action", "request", action_id, "--approver", ALICE, "--local", *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.write((soft or self.alice).receipt(json.loads(proc.stdout)))

    def test_cli_cycle(self):
        proc = self.run_cli("action", "propose", "--connector", "shell-noop", "--operation",
                            "send", "--project", "demo", "--target", "client:42",
                            "--args", '{"n": 1}', "--by", AGENT, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        action = json.loads(proc.stdout)
        action_id = action["action_id"]
        proc = self.run_cli("action", "execute", action_id)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[receipt_required]", proc.stderr)
        proc = self.run_cli("action", "approve", action_id, "--receipt", self.signed(action_id))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("approved", proc.stdout)
        proc = self.run_cli("action", "execute", action_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("confirmed", proc.stdout)
        self.assertEqual(self.files(), [action_id + ".json"])
        proc = self.run_cli("action", "execute", action_id)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[state]", proc.stderr)
        proc = self.run_cli("action", "show", action_id, "--json")
        shown = json.loads(proc.stdout)
        self.assertEqual([e["event"] for e in shown["events"]],
                         ["proposed", "approved", "launched", "confirmed"])
        self.assertEqual(shown["attempts_detail"][0]["approver"], ALICE)
        proc = self.run_cli("action", "show", action_id)
        self.assertIn("empreinte : %s" % action["digest"], proc.stdout)
        proc = self.run_cli("action", "list", "--state", "confirmed", "--json")
        self.assertEqual([r["action_id"] for r in json.loads(proc.stdout)], [action_id])
        other = json.loads(self.run_cli(
            "action", "propose", "--connector", "shell-noop", "--operation", "send",
            "--project", "demo", "--target", "client:43", "--by", AGENT, "--json").stdout)
        proc = self.run_cli("action", "cancel", other["action_id"], "--note", "inutile")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.run_cli("action", "propose", "--connector", "shell-noop", "--operation",
                            "send", "--project", "demo", "--target", "client:42", "--class",
                            "read", "--by", AGENT)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[class]", proc.stderr)

    def test_cli_git_merge_inconnue_puis_remplacement(self):
        proc = self.run_cli("action", "propose", "--connector", "git-merge", "--operation",
                            "merge", "--project", "demo", "--target", PR, "--args",
                            '{"method": "squash"}', "--by", AGENT, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        action_id = json.loads(proc.stdout)["action_id"]
        self.assertEqual(self.run_cli("action", "approve", action_id, "--receipt",
                                      self.signed(action_id)).returncode, 0)
        proc = self.run_cli("action", "execute", action_id,
                            env=self.cli_env(AMEESH_FAKE_GH_MODE="drop"))
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("unknown", proc.stdout)
        # exécuter une action d'issue inconnue : refus d'état, code 1 (pas 4)
        proc = self.run_cli("action", "execute", action_id)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("refus [state] : action %s : issue inconnue" % action_id, proc.stderr)
        proc = self.run_cli("action", "reconcile", action_id,
                            env=self.cli_env(AMEESH_FAKE_GH_VIEW="absent"))
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("introuvable", proc.stdout)
        proc = self.run_cli("action", "retry", action_id, "--receipt", self.signed(action_id))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[dedupe]", proc.stderr)
        proc = self.run_cli("decisions", "--for", ALICE)
        self.assertIn("issues inconnues à trancher", proc.stdout)
        self.assertIn(action_id, proc.stdout)
        proc = self.run_cli("action", "replace", action_id, "--receipt", self.signed(action_id))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[digest_mismatch]", proc.stderr)
        proc = self.run_cli("action", "replace", action_id, "--receipt",
                            self.signed(action_id, "--assume-duplicate"), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        new = json.loads(proc.stdout)
        self.assertEqual((new["replaces"], new["state"]), (action_id, "proposed"))
        new_id = new["action_id"]
        self.assertEqual(self.run_cli("action", "approve", new_id, "--receipt",
                                      self.signed(new_id)).returncode, 0)
        proc = self.run_cli("action", "execute", new_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.pr()["state"], "MERGED")

    def test_cli_decisions(self):
        for_bob = self.propose("send", approvers=[BOB], args={"n": 1})
        anyone = self.propose("send", args={"n": 2})
        self.propose("note")                                    # sans reçu : pas une décision
        lost = self.propose("send", args={"simulate": "lose-response"})
        self.approve(lost)
        self.assertEqual(self.execute(lost)["state"], "unknown")
        mine = work.add(self.db, title="choisir le transporteur", assignee=ALICE)
        work.move(self.db, mine["id"], "waiting_human")
        theirs = work.add(self.db, title="valider la maquette", assignee="carol")
        work.move(self.db, theirs["id"], "waiting_human")

        queue = json.loads(self.run_cli("decisions", "--json").stdout)
        self.assertEqual([r["action_id"] for r in queue["approvals"]],
                         [for_bob["action_id"], anyone["action_id"]])
        self.assertEqual([r["action_id"] for r in queue["unknown"]], [lost["action_id"]])
        self.assertIn("retry", queue["unknown"][0]["hint"])
        self.assertEqual(sorted(i["id"] for i in queue["waiting_human"]),
                         sorted([mine["id"], theirs["id"]]))
        queue = json.loads(self.run_cli("decisions", "--for", ALICE, "--json").stdout)
        self.assertEqual([r["action_id"] for r in queue["approvals"]], [anyone["action_id"]])
        self.assertEqual([i["id"] for i in queue["waiting_human"]], [mine["id"]])
        queue = json.loads(self.run_cli("decisions", "--for", BOB, "--json").stdout)
        self.assertEqual(len(queue["approvals"]), 2)
        self.assertEqual(queue["waiting_human"], [])
        proc = self.run_cli("decisions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for text in ("actions à approuver", "issues inconnues à trancher",
                     "lots en attente d'un humain", "choisir le transporteur"):
            self.assertIn(text, proc.stdout)
        proc = self.run_cli("decisions", "--for", "alice")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[invalid]", proc.stderr)
        proc = self.run_cli("decisions", "--for", "human:dave")
        self.assertIn(anyone["action_id"], proc.stdout)
        self.assertNotIn(for_bob["action_id"], proc.stdout)


if __name__ == "__main__":
    unittest.main()

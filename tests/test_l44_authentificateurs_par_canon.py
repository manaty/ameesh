# SPDX-License-Identifier: AGPL-3.0-only
"""Authentificateurs PAR CANON (lot L44, décision 0031, migration 0035).

Deux canons git (ceux de L42 : `manaty-essai` par défaut, `acme-essai`), chacun
avec ses humains et leurs passkeys ; carol travaille dans les deux, avec la
MÊME passkey :

* journaux indépendants : synchroniser A, puis B, puis A (nouveaux commits)
  ne compare jamais un commit de B à un commit de A — aucun refus de
  descendance ;
* révocation bornée : synchroniser A ne révoque aucun humain ni aucune
  passkey de B ; la passkey de carol a une ligne par canon, révoquée par son
  seul canon ;
* reçus vérifiés contre le canon de l'objet approuvé : un authentificateur de
  B est refusé pour une action de A (et inversement), un grant de B ne couvre
  pas une action de A ;
* branche de confiance et `untrusted` par canon (configuration `canons`) ; une
  erreur d'un canon n'affecte ni le registre ni l'état de l'autre ;
* changement de canon par défaut : registre, journal et actions suivent leur
  canon.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import time
import types

from ameesh import actions, canon, canon_sync, config as config_mod, receipts
from ameesh import runner as runner_mod
from ameesh.actions import ActionError
from ameesh.approve import config as approve_config
from ameesh.approve.service import ApproveError, ApproveService
from ameesh.approve.sources import MemoryActionSource
from ameesh.connectors.shell_noop import ShellNoopConnector

from .test_approve import make_action, write_token_file
from .test_canon import commit_all, git, write
from .test_l42_plusieurs_canons import HOST, ID_A, ID_B, _DeuxCanons
from .webauthn_soft import ORIGIN, RP_ID, SoftWebAuthn, make_request

POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))


def member_auth(title: str, *entries: dict) -> str:
    """Fiche Member avec ses authentificateurs (JSON en flux YAML)."""
    return ("---\ntype: Member\ntitle: %s\nroles: [project-lead]\nauthenticators: %s\n---\n"
            "\n# %s\n" % (title, json.dumps(list(entries)), title))


class _Authentificateurs(_DeuxCanons):
    """A : alice et carol ; B : bob et carol (même passkey que dans A)."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE actions, action_attempts, action_events, standing_approvals, "
            "standing_reservations, mesh_consumed_nonces RESTART IDENTITY CASCADE")
        self.alice = SoftWebAuthn()
        self.alice2 = SoftWebAuthn()
        self.bob = SoftWebAuthn()
        self.bob2 = SoftWebAuthn()
        self.carol = SoftWebAuthn()
        self.commit_a = self.declare(self.a, alice=[self.alice], carol=[self.carol])
        self.commit_b = self.declare(self.b, bob=[self.bob], carol=[self.carol])
        self.noop = ShellNoopConnector(os.path.join(self.workspace, "noop"), timeout=10.0)

    # -- canon ------------------------------------------------------------
    def declare(self, clone: str, **humans) -> str:
        """Fiches Member (None : retirée) ; commit poussé, rend son SHA."""
        for title, softs in humans.items():
            text = None if softs is None else member_auth(title, *[s.entry() for s in softs])
            write(clone, "membres/%s.md" % title, text)
        return commit_all(clone, "membres")

    def sync_a(self, bootstrap_ref: str = "main") -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, self.load_a(), HOST, bootstrap_ref=bootstrap_ref)

    def sync_b(self, bootstrap_ref: str = "main") -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, self.load_b(), HOST, bootstrap_ref=bootstrap_ref)

    # -- registre ---------------------------------------------------------
    def live(self) -> dict:
        """{(canon, approbateur, credential): id} des lignes actives."""
        return {(r["canon"], r["approver"], r["credential_id"]): int(r["id"])
                for r in receipts.list_authenticators(self.db)}

    def journal(self) -> list[tuple]:
        return [(r["canon"], r["root_commit"], r["trust"]) for r in self.db.query(
            "SELECT canon, root_commit, trust FROM authenticator_syncs ORDER BY id")]

    def assertSynced(self, report: canon_sync.SyncReport) -> canon_sync.AuthenticatorSync:
        done = report.authenticators
        self.assertEqual((done.status, done.errors), (canon_sync.AUTH_SYNCED, []), done.reason)
        return done

    # -- actions ----------------------------------------------------------
    def propose(self, proposed_by: str) -> dict:
        return actions.propose(self.db, self.noop, project="demo", operation="send",
                               target="client:42", args={"n": 1}, proposed_by=proposed_by,
                               action_class="irreversible")

    def approve(self, action: dict, soft: SoftWebAuthn, approver: str) -> dict:
        receipt = soft.receipt(actions.approval_request(action, approver))
        return actions.approve(self.db, action["action_id"], receipt, policy=POLICY,
                               by=approver)


class JournalParCanonTest(_Authentificateurs):
    def test_a_b_a_b_sans_refus_de_descendance(self):
        added = self.assertSynced(self.sync_a()).result["added"]
        self.assertEqual(sorted(added), sorted([
            "human:alice/webauthn/%s" % self.alice.credential_id,
            "human:carol/webauthn/%s" % self.carol.credential_id]))
        done = self.assertSynced(self.sync_b())
        self.assertEqual(done.canon, ID_B)
        self.assertEqual(len(done.result["added"]), 2)
        # nouveaux commits dans A puis dans B : chacun descend du dernier
        # commit appliqué de SON journal (avant L44 : A refusé après B)
        commit_a2 = self.declare(self.a, alice=[self.alice, self.alice2])
        done = self.assertSynced(self.sync_a(bootstrap_ref=""))
        self.assertEqual(done.result["added"],
                         ["human:alice/webauthn/%s" % self.alice2.credential_id])
        self.assertEqual(done.trust, canon_sync.TRUST_APPLIED)
        commit_b2 = self.declare(self.b, bob=[self.bob, self.bob2])
        done = self.assertSynced(self.sync_b(bootstrap_ref=""))
        self.assertEqual(done.result["added"],
                         ["human:bob/webauthn/%s" % self.bob2.credential_id])
        self.assertEqual(done.trust, canon_sync.TRUST_APPLIED)
        # le même commit, de nouveau : rien ne change, rien n'est refusé
        self.assertEqual(self.assertSynced(self.sync_a()).result["unchanged"], 3)
        self.assertEqual(self.assertSynced(self.sync_b()).result["unchanged"], 3)
        self.assertEqual(self.journal(), [
            ("", self.commit_a, "bootstrap"), (ID_B, self.commit_b, "bootstrap"),
            ("", commit_a2, "applied"), (ID_B, commit_b2, "applied")])
        self.assertEqual(canon_sync.last_applied(self.db)["root_commit"], commit_a2)
        self.assertEqual(canon_sync.last_applied(self.db, ID_B)["root_commit"], commit_b2)
        # chaque canon a son état d'authentificateurs, ok
        states = {s["canon"]: s["auth_status"] for s in canon_sync.states(self.db, HOST)}
        self.assertEqual(states, {"": "ok", ID_B: "ok"})

    def test_monotonie_toujours_verifiee_par_canon(self):
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        self.declare(self.b, bob=[self.bob, self.bob2])
        self.assertSynced(self.sync_b())
        # B réécrit son histoire (retour au commit déjà dépassé) : refusé pour
        # B seulement, A continue
        git(self.b, "reset", "-q", "--hard", self.commit_b)
        git(self.b, "push", "-q", "--force", "origin", "HEAD:main")
        git(self.b, "fetch", "-q", "origin")
        done = self.sync_b(bootstrap_ref="").authenticators
        self.assertEqual(done.status, canon_sync.AUTH_REFUSED)
        self.assertIn("ne descend pas", done.reason)
        self.declare(self.a, alice=[self.alice, self.alice2])
        self.assertSynced(self.sync_a(bootstrap_ref=""))
        states = {s["canon"]: s["auth_status"] for s in canon_sync.states(self.db, HOST)}
        self.assertEqual(states, {"": "ok", ID_B: "error"})
        # le registre de B n'a pas bougé : bob2 toujours actif
        self.assertIn((ID_B, "human:bob", self.bob2.credential_id), self.live())


class RevocationBorneeTest(_Authentificateurs):
    def test_synchroniser_a_ne_revoque_rien_de_b(self):
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        live = self.live()
        self.assertEqual(set(live), {
            ("", "human:alice", self.alice.credential_id),
            ("", "human:carol", self.carol.credential_id),
            (ID_B, "human:bob", self.bob.credential_id),
            (ID_B, "human:carol", self.carol.credential_id)})
        # même passkey de carol, deux lignes distinctes (une par canon)
        self.assertNotEqual(live[("", "human:carol", self.carol.credential_id)],
                            live[(ID_B, "human:carol", self.carol.credential_id)])
        # bob est absent de A : synchroniser A ne le révoque pas
        done = self.assertSynced(self.sync_a())
        self.assertEqual(done.result["revoked"], [])
        self.assertEqual(self.live(), live)

    def test_humain_des_deux_canons_revoque_par_son_seul_canon(self):
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        carol_a = self.live()[("", "human:carol", self.carol.credential_id)]
        # carol quitte B : sa ligne de B est révoquée, celle de A reste
        commit = self.declare(self.b, carol=None)
        done = self.assertSynced(self.sync_b())
        self.assertEqual(done.result["revoked"],
                         ["human:carol/webauthn/%s" % self.carol.credential_id])
        live = self.live()
        self.assertNotIn((ID_B, "human:carol", self.carol.credential_id), live)
        self.assertEqual(live[("", "human:carol", self.carol.credential_id)], carol_a)
        revoked = [r for r in receipts.list_authenticators(self.db, include_revoked=True,
                                                           canon=ID_B)
                   if r.get("revoked_ts")]
        self.assertEqual(len(revoked), 1)
        self.assertEqual(revoked[0]["revoked_reason"], "absent du canon")
        # la révocation note le commit de B qui la constate (référence qualifiée)
        self.assertEqual(revoked[0]["canon_ref"],
                         "%s/home:membres/carol.md@%s" % (ID_B, commit))
        # A, resynchronisé, garde carol ; B, resynchronisé, ne la réactive pas
        self.assertEqual(self.assertSynced(self.sync_a()).result["revoked"], [])
        self.assertEqual(self.assertSynced(self.sync_b()).result["added"], [])
        self.assertIn(("", "human:carol", self.carol.credential_id), self.live())

    def test_cle_changee_dans_un_canon(self):
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        # B déclare une AUTRE clé sous le même credential de carol : seule la
        # ligne de B est remplacée
        other = SoftWebAuthn()
        other.credential_id = self.carol.credential_id
        self.declare(self.b, carol=[other])
        done = self.assertSynced(self.sync_b())
        self.assertEqual(done.result["revoked"],
                         ["human:carol/webauthn/%s" % self.carol.credential_id])
        self.assertEqual(done.result["added"],
                         ["human:carol/webauthn/%s" % self.carol.credential_id])
        keys = {r["canon"]: r["public_key"] for r in receipts.list_authenticators(
            self.db, approver="human:carol")}
        self.assertEqual(keys, {"": self.carol.public_key(), ID_B: other.public_key()})


class RecusParCanonTest(_Authentificateurs):
    def setUp(self) -> None:
        super().setUp()
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        self.ids = self.live()

    def test_canon_de_l_action(self):
        # celui de l'agent proposant ; '' pour un humain (portée non rattachable)
        self.assertEqual(self.propose("agent:a1")["canon"], "")
        self.assertEqual(self.propose("agent:b1")["canon"], ID_B)
        self.assertEqual(self.propose("human:alice")["canon"], "")
        self.assertEqual(self.propose("agent:inconnu")["canon"], "")
        # l'appelant ne choisit pas le canon : la base le fixe à l'insertion
        action = self.propose("agent:b1")
        self.assertEqual(actions.policy_for(action, POLICY).canon, ID_B)

    def test_authentificateur_de_b_refuse_pour_une_action_de_a(self):
        action_a, action_b = self.propose("agent:a1"), self.propose("agent:b1")
        with self.assertRaises(ActionError) as ctx:
            self.approve(action_a, self.bob, "human:bob")
        self.assertEqual(ctx.exception.code, receipts.CANON)
        self.assertIn("canon %s" % ID_B, ctx.exception.reason)
        with self.assertRaises(ActionError) as ctx:
            self.approve(action_b, self.alice, "human:alice")
        self.assertEqual(ctx.exception.code, receipts.CANON)
        self.assertEqual(actions.get(self.db, action_a["action_id"])["state"], "proposed")
        bound = self.approve(action_b, self.bob, "human:bob")
        self.assertEqual(bound["state"], "approved")
        self.assertEqual(bound["auth_authenticator_id"],
                         self.ids[(ID_B, "human:bob", self.bob.credential_id)])

    def test_meme_passkey_dans_deux_canons(self):
        """La passkey de carol vaut pour A avec la ligne de A, pour B avec
        celle de B ; révoquée dans B, elle ne vaut plus pour B seulement."""
        action_a, action_b = self.propose("agent:a1"), self.propose("agent:b1")
        bound = self.approve(action_a, self.carol, "human:carol")
        self.assertEqual(bound["auth_authenticator_id"],
                         self.ids[("", "human:carol", self.carol.credential_id)])
        self.declare(self.b, carol=None)
        self.assertSynced(self.sync_b())
        # sa ligne de B est révoquée : la ligne ACTIVE de A ne vaut pas pour B
        with self.assertRaises(ActionError) as ctx:
            self.approve(action_b, self.carol, "human:carol")
        self.assertEqual(ctx.exception.code, receipts.REVOKED)
        other = self.propose("agent:a2")
        self.assertEqual(self.approve(other, self.carol, "human:carol")["state"], "approved")

    def test_verification_directe_par_canon(self):
        request = make_request("human:bob")
        receipt = self.bob.receipt(request)
        self.assertTrue(receipts.verify_receipt(
            self.db, receipt, dataclasses.replace(POLICY, canon=ID_B)).ok)
        verdict = receipts.verify_receipt(self.db, receipt, POLICY)
        self.assertEqual(verdict.code, receipts.CANON, verdict.reason)
        # un approbateur sans aucune ligne nulle part reste inconnu
        verdict = receipts.verify_receipt(
            self.db, SoftWebAuthn().receipt(make_request("human:zoe")), POLICY)
        self.assertEqual(verdict.code, receipts.UNKNOWN_APPROVER, verdict.reason)
        # carol : même credential, la ligne du canon demandé
        receipt = self.carol.receipt(make_request("human:carol"))
        for key in ("", ID_B):
            with self.subTest(canon=key):
                verdict = receipts.verify_receipt(
                    self.db, receipt, dataclasses.replace(POLICY, canon=key))
                self.assertTrue(verdict.ok, verdict.reason)
                self.assertEqual(verdict.authenticator_id,
                                 self.ids[(key, "human:carol", self.carol.credential_id)])
        verdict = receipts.verify_receipt(
            self.db, receipt, dataclasses.replace(POLICY, canon="ailleurs"))
        self.assertEqual(verdict.code, receipts.CANON, verdict.reason)

    def test_grant_de_b_ne_couvre_pas_une_action_de_a(self):
        standing = {"connector": "shell-noop", "operations": ["send"],
                    "class": "irreversible", "max_amount": 0, "currency": None,
                    "until": int(time.time()) + 3600}
        receipt = self.bob.receipt(make_request("human:bob", standing=standing))
        # bob n'a aucun authentificateur dans le canon par défaut
        verdict, grant = receipts.register_standing(self.db, receipt, POLICY,
                                                    registered_by="porte")
        self.assertEqual((verdict.code, grant), (receipts.CANON, None))
        verdict, grant = receipts.register_standing(
            self.db, receipt, dataclasses.replace(POLICY, canon=ID_B), registered_by="porte")
        self.assertTrue(verdict.ok, verdict.reason)
        action_a, action_b = self.propose("agent:a1"), self.propose("agent:b1")
        with self.assertRaises(ActionError) as ctx:
            actions.approve(self.db, action_a["action_id"], None, standing=True, by="porte")
        self.assertEqual(ctx.exception.code, actions.NO_COVER)
        bound = actions.approve(self.db, action_b["action_id"], None, standing=True,
                                by="porte")
        self.assertEqual((bound["state"], bound["auth_grant_id"]), ("approved", grant))
        self.assertIsNone(receipts.standing_cover(self.db, dict(action_a, amount=0)))


class ApproveParCanonTest(_Authentificateurs):
    """ameesh-approve : credentials proposés et reçu vérifiés dans le canon
    de l'action relue (`actions.canon`)."""

    def setUp(self) -> None:
        super().setUp()
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        workdir = self.make_tmp()
        token_file, _token = write_token_file(workdir)
        acfg = approve_config.ApproveConfig(
            rp_id=RP_ID, origins=(ORIGIN,), port=0, token_file=token_file,
            state_dir=os.path.join(workdir, "state")).validate()
        self.source = MemoryActionSource()
        self.service = ApproveService(acfg, self.db, self.source, service_token="t" * 40)

    def request(self, action: dict, approver: str) -> str:
        self.source.put(action)
        reply = self.service.create_request({"action_id": action["action_id"],
                                             "approver": approver,
                                             "requested_by": "agent:b1"})
        return reply["link"].rsplit("/", 1)[1]

    def submit(self, token: str, soft: SoftWebAuthn) -> dict:
        view = self.service.link_view(token)
        challenge = receipts.b64u_decode(view["challenge_approve"])
        body = dict(soft.proof(challenge), decision="approve",
                    credential_id=soft.credential_id)
        return self.service.submit(token, body)

    def test_credentials_et_recu_du_canon_de_l_action(self):
        action_b = make_action(canon=ID_B)
        # alice n'a aucun authentificateur dans le canon de l'action
        with self.assertRaises(ApproveError) as ctx:
            self.request(action_b, "human:alice")
        self.assertEqual(ctx.exception.code, "no_authenticator")
        self.assertIn("canon %s" % ID_B, ctx.exception.message)
        token = self.request(action_b, "human:bob")
        self.assertEqual(self.service.link_view(token)["credentials"],
                         [self.bob.credential_id])
        self.assertEqual(self.submit(token, self.bob)["decision"], "approve")
        # action du canon par défaut : bob refusé dès la demande
        with self.assertRaises(ApproveError) as ctx:
            self.request(make_action(), "human:bob")
        self.assertEqual(ctx.exception.code, "no_authenticator")

    def test_passkey_revoquee_dans_le_canon_de_l_action(self):
        action_b = make_action(canon=ID_B)
        token = self.request(action_b, "human:carol")
        # carol quitte B entre la demande et la signature : refus, même si sa
        # passkey reste active dans A
        self.declare(self.b, carol=None)
        self.assertSynced(self.sync_b())
        with self.assertRaises(ApproveError) as ctx:
            self.submit(token, self.carol)
        self.assertEqual(ctx.exception.code, "no_authenticator")
        token = self.request(make_action(), "human:carol")
        self.assertEqual(self.submit(token, self.carol)["decision"], "approve")


class ConfianceParCanonTest(_Authentificateurs):
    def runner_pass(self, cfg) -> bool:
        return runner_mod.Runner.canon_sync_once(types.SimpleNamespace(cfg=cfg, host=HOST))

    def cfg2(self, *, ref_a: str = "origin/main", ref_b: str = "origin/main",
             b: str | None = None, untrusted_b: bool = False):
        return dataclasses.replace(
            self.cfg, canon=self.a, canon_ref=ref_a, canon_untrusted=False,
            extra_canons=(config_mod.CanonEntry(b or self.b, ref_b, untrusted_b),))

    def auth_states(self) -> dict:
        return {s["canon"]: s["auth_status"] for s in canon_sync.states(self.db, HOST)}

    def test_ref_de_confiance_de_chaque_canon(self):
        cfg = self.cfg2(ref_a="origin/main", ref_b="")
        b = canon.from_config(cfg, entry=cfg.extra_canons[0])
        self.assertEqual(canon_sync.configured_ref_for(cfg, b), "")
        self.assertEqual(canon_sync.configured_ref_for(cfg, canon.from_config(cfg)),
                         "origin/main")
        # B sans `ref` ni amorçage : refusé, pour B seulement
        self.runner_pass(cfg)
        self.assertEqual(self.auth_states(), {"": "ok", ID_B: "error"})
        self.assertEqual({k[0] for k in self.live()}, {""})
        state = canon_sync.state(self.db, HOST, ID_B)
        self.assertIn("--canon %s --bootstrap-ref" % ID_B, state["auth_diagnostic"])
        # la `ref` de son entrée `canons` : B synchronisé, origine « config »
        cfg = self.cfg2(ref_b="origin/main")
        self.assertEqual(canon_sync.configured_ref_for(
            cfg, canon.from_config(cfg, entry=cfg.extra_canons[0])), "origin/main")
        self.assertTrue(self.runner_pass(cfg))
        self.assertEqual(self.auth_states(), {"": "ok", ID_B: "ok"})
        self.assertEqual([(c, t) for c, _commit, t in self.journal()],
                         [("", "config"), (ID_B, "config")])
        self.assertEqual({k[0] for k in self.live()}, {"", ID_B})

    def test_mauvaise_ref_d_un_canon_n_affecte_pas_l_autre(self):
        self.assertTrue(self.runner_pass(self.cfg2()))
        before = self.live()
        self.declare(self.a, alice=[self.alice, self.alice2])
        # la `ref` de B ne désigne rien : B illisible (fermé), A synchronisé
        self.assertFalse(self.runner_pass(self.cfg2(ref_b="origin/inexistante")))
        self.assertEqual({s["canon"]: s["status"] for s in canon_sync.states(self.db, HOST)},
                         {"": "ok", ID_B: "unreadable"})
        self.assertEqual(self.auth_states()[""], "ok")
        after = self.live()
        self.assertIn(("", "human:alice", self.alice2.credential_id), after)
        self.assertEqual({k: v for k, v in after.items() if k[0] == ID_B},
                         {k: v for k, v in before.items() if k[0] == ID_B})

    def test_canon_non_approuve_n_ecrit_pas_son_registre(self):
        # B lu depuis ses fichiers de travail (copie hors git, `untrusted`)
        copy = os.path.join(self.make_tmp(), "b-travail")
        shutil.copytree(self.b, copy, ignore=shutil.ignore_patterns(".git"))
        cfg = self.cfg2(b=copy, ref_b="", untrusted_b=True)
        self.runner_pass(cfg)
        self.assertEqual(self.auth_states(), {"": "ok", ID_B: "skipped"})
        self.assertEqual({k[0] for k in self.live()}, {""})
        self.assertIn("NON APPROUVÉ", canon_sync.state(self.db, HOST, ID_B)["auth_diagnostic"])
        # le même canon, approuvé (git, `ref` configurée) : synchronisé
        self.assertTrue(self.runner_pass(self.cfg2()))
        self.assertEqual(self.auth_states(), {"": "ok", ID_B: "ok"})


class ChangementDeCanonParDefautTest(_Authentificateurs):
    def test_registre_journal_et_actions_suivent_leur_canon(self):
        self.assertSynced(self.sync_a())
        self.assertSynced(self.sync_b())
        before = self.live()
        action_a = self.propose("agent:a1")
        action_b = self.propose("agent:b1")
        # B devient le canon par défaut, A le second
        b_defaut, a_second = canon.load(self.b), canon.load(self.a, default=False)
        done = canon_sync.adopt_default(self.db, HOST, [b_defaut, a_second])
        self.assertEqual(done["authenticators"]["authenticators"], (2, 2))
        self.assertEqual(self.live(), {
            ((ID_A if c == "" else ""), a, k): i for (c, a, k), i in before.items()})
        self.assertEqual({c for c, _commit, _t in self.journal()}, {ID_A, ""})
        self.assertEqual(actions.get(self.db, action_a["action_id"])["canon"], ID_A)
        self.assertEqual(actions.get(self.db, action_b["action_id"])["canon"], "")
        # chaque canon poursuit SON journal : aucun amorçage, aucun refus
        done_b = self.assertSynced(canon_sync.sync(self.db, b_defaut, HOST))
        done_a = self.assertSynced(canon_sync.sync(self.db, a_second, HOST))
        self.assertEqual((done_b.canon, done_b.trust), ("", canon_sync.TRUST_APPLIED))
        self.assertEqual((done_a.canon, done_a.trust), (ID_A, canon_sync.TRUST_APPLIED))
        self.assertEqual((done_a.result["revoked"], done_b.result["revoked"]), ([], []))
        # les reçus suivent : bob vaut pour l'action de B, plus pour celle de A
        with self.assertRaises(ActionError):
            self.approve(action_a, self.bob, "human:bob")
        self.assertEqual(self.approve(action_b, self.bob, "human:bob")["state"], "approved")
        self.assertEqual(self.approve(action_a, self.alice, "human:alice")["state"],
                         "approved")

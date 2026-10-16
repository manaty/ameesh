# SPDX-License-Identifier: AGPL-3.0-only
"""L232d (#366) : le défi de connexion d'ameesh-approve, de bout en bout.

Le lien seul est MUET : l'URL d'autorisation et le code à usage unique ne
sortent du service qu'après une assertion WebAuthn (`view`). Le reçu signé
(`done` | `cancel`) ne les contient jamais, pas même par un condensat, et le
reçu « terminé » ne prouve pas le grant fournisseur (la santé se vérifie à
part, hors de ce fichier).

Même banc que tests/test_approve.py : serveur réel sur la boucle locale,
passkey logicielle (tests/webauthn_soft.py), horloge avançable.
"""
from __future__ import annotations

import json
import os
import stat
import unittest
from urllib.parse import urlsplit

from ameesh import receipts

from .test_approve import AGENT, ALICE, ApproveBase
from .webauthn_soft import SoftWebAuthn

CODE = "CANARY-CODE-1234"
URL = "https://github.com/login/device"


class LoginReceiptFormTest(unittest.TestCase):
    """La forme du reçu `login` (receipts.check_request), sans base."""

    @staticmethod
    def request(**overrides) -> dict:
        request = {
            "v": receipts.REQUEST_VERSION,
            "approver": ALICE,
            "login_id": "lgn_" + "a" * 26,
            "host": "ameesh-agents-1",
            "provider": "gh",
            "account": "smichea",
            "scope": "",
            "summary_digest": "sha256:" + "0" * 64,
            "requested_by": AGENT,
            "nonce": receipts.b64u_encode(os.urandom(16)),
            "iat": 100,
            "exp": 200,
            "decision": "view",
        }
        request.update(overrides)
        return request

    def test_forme_valide(self):
        self.assertEqual(receipts.check_request(self.request()), "login")
        for decision in ("done", "cancel"):
            self.assertEqual(receipts.check_request(self.request(decision=decision)), "login")

    def test_decision_inconnue_refusee(self):
        for decision in ("approve", "deny", "open"):
            with self.assertRaises(receipts.ReceiptError):
                receipts.check_request(self.request(decision=decision))

    def test_ni_action_ni_empreinte(self):
        with self.assertRaises(receipts.ReceiptError):
            receipts.check_request(self.request(action_id="act_" + "a" * 26))
        with self.assertRaises(receipts.ReceiptError):
            receipts.check_request(self.request(digest="sha256:" + "0" * 64))

    def test_login_id_et_champs_controles(self):
        for overrides in ({"login_id": "lgn_short"}, {"host": ""}, {"host": "a b"},
                          {"provider": ""}, {"account": "a\nb"}, {"scope": "- x"}):
            with self.assertRaises(receipts.ReceiptError):
                receipts.check_request(self.request(**overrides))

    def test_identifiants_vides_admis(self):
        self.assertEqual(receipts.check_request(self.request(account="", scope="")), "login")


class LoginChallengeTest(ApproveBase):
    def create_login(self, **overrides):
        body = {"host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE}
        body.update(overrides)
        status, _headers, payload = self.api("POST", "/login-requests", body)
        self.assertEqual(status, 201, payload)
        data = json.loads(payload)
        self.assertRegex(data["login_id"], r"^lgn_[0-9a-z]{26}$")
        link = urlsplit(data["link"])
        self.assertEqual("%s://%s" % (link.scheme, link.netloc), "https://approve.example.test")
        self.assertNotIn(CODE, payload.decode())
        self.assertNotIn(URL, payload.decode())
        return data, link.path

    def open_view(self, path):
        status, headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertSecurityHeaders(headers)
        page = self.page_data(text, "ameesh-login")
        status, payload = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, payload)
        return page, payload

    def fetch_login_receipt(self, login_id):
        status, _headers, payload = self.api("GET", "/login-receipts/" + login_id)
        return status, json.loads(payload)

    def verify(self, raw, *, login_id, exp, **kwargs):
        expected = {"login_id": login_id, "host": "ameesh-agents-1", "provider": "gh",
                    "account": "smichea", "exp": exp}
        policy = receipts.Policy(rp_id="approve.example.test",
                                 origins=("https://approve.example.test",))
        return receipts.verify_receipt(self.server_db, raw, policy, kind="login",
                                       expected_login=expected, **kwargs)

    # -- le lien seul est muet, l'assertion ouvre ---------------------------
    def test_lien_muet_puis_vue_privee_puis_recu(self):
        data, path = self.create_login()
        status, pending = self.fetch_login_receipt(data["login_id"])
        self.assertEqual((status, pending["status"]), (202, "pending"))

        status, headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        # ni URL ni code dans la page publique ; aucune assertion encore
        self.assertNotIn(CODE, text)
        self.assertNotIn(URL, text)
        page = self.page_data(text, "ameesh-login")
        self.assertNotIn("challenge-done", page)
        self.assertIn("challenge-view", page)
        self.assertIn("challenge-cancel", page)

        # `done` sans avoir ouvert la vue : refusé, même avec une assertion
        # correctement signée sur le défi `done` (le service ne l'expose pas
        # avant la vue ; le test le recalcule depuis la demande du service)
        record = self.service.store.get_login(data["login_id"])
        done_challenge = receipts.b64u_encode(
            receipts.challenge(dict(record["request"], decision="done")))
        status, refused = self.submit(path, self.sign(
            self.alice, {"challenge-done": done_challenge}, decision="done"))
        self.assertEqual(status, 409, refused)
        self.assertEqual(refused["error"], "not_viewed")

        # l'assertion `view` rend l'URL et le code, une seule fois dans la réponse
        status, payload = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["url"], URL)
        self.assertEqual(payload["code"], CODE)
        self.assertIn("challenge_done", payload)
        # rouvrir la vue ne consomme pas le nonce : une seconde assertion marche
        status, again = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, again)

        # `done` signé après la vue : reçu enregistré
        status, signed = self.submit(path, self.sign(
            self.alice, {"challenge-done": payload["challenge_done"]}, decision="done"))
        self.assertEqual(status, 200, signed)
        self.assertEqual(signed["decision"], "done")
        status, document = self.fetch_login_receipt(data["login_id"])
        self.assertEqual(status, 200)
        self.assertEqual(document["request"]["decision"], "done")
        self.assertEqual(document["request"]["login_id"], data["login_id"])
        self.assertEqual(document["request"]["host"], "ameesh-agents-1")
        self.assertNotIn(CODE, json.dumps(document))
        self.assertNotIn(URL, json.dumps(document))
        self.assertEqual(document["request"]["summary_digest"],
                         receipts_render_summary_digest(document["request"]))
        record = self.service.store.get_login(data["login_id"])
        self.assertIn("ameesh-login-summary/1", record["summary"])
        self.assertNotIn(CODE, record["summary"])
        self.assertNotIn(URL, record["summary"])

        # ameesh (le demandeur) VÉRIFIE puis CONSOMME le nonce lui-même
        verdict = self.verify(
            document, login_id=data["login_id"], exp=document["request"]["exp"],
            expect_decision="done", consume_by=AGENT)
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual(verdict.login_id, data["login_id"])
        self.assertEqual(verdict.approver, ALICE)
        replay = self.verify(
            document, login_id=data["login_id"], exp=document["request"]["exp"],
            expect_decision="done", consume_by=AGENT)
        self.assertFalse(replay.ok)
        self.assertEqual(replay.code, receipts.REPLAY)

        # le lien est consommé : la page ne sert plus
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410, text)

    def test_annulation_avant_vue(self):
        data, path = self.create_login()
        page = self.page_data(self.page(path)[2], "ameesh-login")
        status, result = self.submit(path, self.sign(self.alice, page, decision="cancel"))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["decision"], "cancel")
        status, raw = self.fetch_login_receipt(data["login_id"])
        self.assertEqual(status, 200)
        verdict = self.verify(raw, login_id=data["login_id"],
                              exp=raw["request"]["exp"],
                              expect_decision="cancel")
        self.assertTrue(verdict.ok, verdict.reason)

    def test_annulation_apres_vue(self):
        data, path = self.create_login()
        page, opened = self.open_view(path)
        status, result = self.submit(path, self.sign(
            self.alice, {"challenge-cancel": opened["challenge_cancel"]}, decision="cancel"))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["decision"], "cancel")
        # le code privé est effacé
        private = self.service.store.login_private(data["login_id"])
        self.assertIsNone(private)

    # -- liaison : le reçu vaut pour CE défi, pas un autre ------------------
    def test_recu_lie_au_defi(self):
        data, path = self.create_login()
        _page, opened = self.open_view(path)
        status, _signed = self.submit(path, self.sign(
            self.alice, {"challenge-done": opened["challenge_done"]}, decision="done"))
        self.assertEqual(status, 200)
        status, raw = self.fetch_login_receipt(data["login_id"])
        exp = raw["request"]["exp"]
        wrong = self.verify(raw, login_id=data["login_id"], exp=exp, expect_decision="done")
        self.assertTrue(wrong.ok)
        for overrides in ({"host": "other-host"}, {"provider": "codex"},
                          {"account": "other"}, {"exp": exp + 60},
                          {"login_id": "lgn_" + "b" * 26}):
            expected = {"login_id": data["login_id"], "host": "ameesh-agents-1",
                        "provider": "gh", "account": "smichea", "exp": exp}
            expected.update(overrides)
            verdict = receipts.verify_receipt(
                self.server_db, raw, receipts.Policy(
                    rp_id="approve.example.test",
                    origins=("https://approve.example.test",)),
                kind="login", expected_login=expected, expect_decision="done")
            self.assertFalse(verdict.ok, overrides)
            self.assertEqual(verdict.code, receipts.LOGIN_MISMATCH, overrides)

    def test_consommation_exige_la_liaison_complete(self):
        data, path = self.create_login()
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(self.alice,
                                    {"challenge-done": opened["challenge_done"]},
                                    decision="done"))
        _status, raw = self.fetch_login_receipt(data["login_id"])
        exp = raw["request"]["exp"]
        policy = receipts.Policy(rp_id="approve.example.test",
                                 origins=("https://approve.example.test",))
        verdict = receipts.verify_receipt(
            self.server_db, raw, policy, kind="login",
            expected_login={"login_id": data["login_id"]}, expect_decision="done",
            consume_by=AGENT)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, receipts.UNBOUND)

    def test_recu_login_refuse_comme_action(self):
        data, path = self.create_login()
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(self.alice,
                                    {"challenge-done": opened["challenge_done"]},
                                    decision="done"))
        _status, raw = self.fetch_login_receipt(data["login_id"])
        verdict = receipts.verify_receipt(
            self.server_db, raw, receipts.Policy(
                rp_id="approve.example.test", origins=("https://approve.example.test",)),
            kind="action")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, receipts.KIND)

    # -- refus de forme et de domaine ---------------------------------------
    def test_url_hors_domaine_refusee(self):
        for url in ("https://evil.example/login", "http://github.com/login/device",
                    "https://github.com.evil.example/x", "https://user:pw@github.com/x"):
            status, _headers, payload = self.api("POST", "/login-requests", {
                "host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": url, "code": CODE})
            self.assertEqual(status, 400, payload)

    def test_provider_et_code_controles(self):
        for overrides in ({"provider": "gemini"}, {"code": "has space"}, {"host": "a b"},
                          {"account": "a" * 200}, {"scope": "x\ny"}):
            status, _headers, payload = self.api("POST", "/login-requests", {
                "host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE,
                **overrides})
            self.assertEqual(status, 400, payload)

    def test_jeton_de_service_exige(self):
        status, _headers, _payload = self.http("POST", "/login-requests", {
            "host": "h", "provider": "gh", "account": "a", "approver": ALICE,
            "requested_by": AGENT, "url": URL, "code": CODE}, host=self.local)
        self.assertEqual(status, 401)

    def test_ttl_borne(self):
        # 15 min au plus
        _data, _path = self.create_login()
        record = self.service.store.get_login(
            next(name[:-5] for name in os.listdir(os.path.join(
                self.acfg.state_dir, "login-requests"))))
        self.assertEqual(record["request"]["exp"] - record["request"]["iat"], 900)
        # l'échéance fournisseur, plus courte, gagne
        now = int(self.clock())
        data, _path = self.create_login(provider_exp=now + 120)
        record = self.service.store.get_login(data["login_id"])
        self.assertLessEqual(record["request"]["exp"] - record["request"]["iat"], 120)
        # une échéance passée est refusée
        status, _headers, _payload = self.api("POST", "/login-requests", {
            "host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
            "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE,
            "provider_exp": now - 1})
        self.assertEqual(status, 400)

    # -- expiration et élagage ---------------------------------------------
    def test_expiration(self):
        data, path = self.create_login()
        page = self.page_data(self.page(path)[2], "ameesh-login")
        challenge = page["challenge-view"]
        self.clock.offset = 901
        self.assertEqual(self.page(path)[0], 410)
        status, payload = self.fetch_login_receipt(data["login_id"])
        self.assertEqual((status, payload["status"]), (410, "expired"))
        status, refused = self.submit(path, self.sign(
            self.alice, {"challenge-view": challenge}, decision="view"))
        self.assertEqual(status, 410, refused)

    def test_elagage(self):
        data, path = self.create_login()
        state = self.acfg.state_dir
        private = os.path.join(state, "login-private", data["login_id"] + ".json")
        self.assertTrue(os.path.exists(private))
        self.service.store.prune(self.clock() + receipts.DEFAULT_MAX_TTL + 10 * 86400)
        for path_ in (os.path.join(state, "login-requests", data["login_id"] + ".json"),
                      private,
                      os.path.join(state, "login-views", data["login_id"])):
            self.assertFalse(os.path.exists(path_), path_)
        self.assertEqual(self.page(path)[0], 404)

    # -- secrets : permissions, journaux, état privé ------------------------
    def test_permissions_et_canari(self):
        data, path = self.create_login()
        state = self.acfg.state_dir
        private = os.path.join(state, "login-private", data["login_id"] + ".json")
        public = os.path.join(state, "login-requests", data["login_id"] + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(public).st_mode), 0o600)
        with open(private, "r", encoding="utf-8") as handle:
            self.assertIn(CODE, handle.read())
        with open(public, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertNotIn(CODE, body)
        self.assertNotIn(URL, body)
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(self.alice,
                                    {"challenge-done": opened["challenge_done"]},
                                    decision="done"))
        self.assertNotIn(CODE, self.log.getvalue())
        # le code privé est effacé dès le reçu signé
        self.assertIsNone(self.service.store.login_private(data["login_id"]))
        self.assertFalse(os.path.exists(private))


def receipts_render_summary_digest(request: dict) -> str:
    """L'empreinte du résumé, recalculée depuis la demande signée."""
    from ameesh.approve import render
    return render.summary_digest(render.render_login_summary(request))

# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §16 (lot #3889) : la page d'approbation renvoie le MÊME corps signé
après une panne (réseau, 5xx, 429), sans re-signature, jusqu'à une réponse
définitive ; un refus voulu (4xx) est expliqué et jamais renvoyé.

`approve.js` tourne sous Node avec un DOM minimal (sauté sans `node`)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), os.pardir, "src", "ameesh", "approve",
                      "static", "approve.js")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [script, scenario] = [process.argv[2], JSON.parse(process.argv[3])];
const PATH = "/a/" + "t".repeat(43);
const storage = new Map(Object.entries(scenario.storage || {}));
const localStorage = {
  get length() { return storage.size; },
  key(i) { return Array.from(storage.keys())[i] ?? null; },
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};
const status = { textContent: "", className: "" };
const listeners = {};
const button = (id) => ({ id, disabled: false,
  addEventListener(_e, fn) { listeners[id] = fn; } });
const buttons = { approve: button("approve"), deny: button("deny"), enroll: button("enroll"),
                  "login-open": button("login-open"), "login-done": button("login-done"),
                  "login-cancel": button("login-cancel"), "login-private": button("login-private"),
                  "login-url": button("login-url"), "login-code": button("login-code") };
const roots = {
  "ameesh-approval": { dataset: { kind: "", rpId: "example.org", credentials: "",
                                  challengeApprove: "AA", challengeDeny: "AA" } },
  "ameesh-enroll": { dataset: { rpId: "example.org", rpName: "x", userId: "AA",
                                userName: "human:dave", challenge: "AA", exclude: "" } },
  "ameesh-wait": { dataset: { retry: "15" } },
  "ameesh-login": { dataset: { rpId: "example.org", credentials: "", challengeView: "AA",
                               challengeCancel: "AA" } },
};
const rootId = "ameesh-" + (scenario.root || "approval");
let reloads = 0;
const document = {
  getElementById(id) {
    if (id === rootId) return roots[id];
    if (id === "status") return status;
    return buttons[id] || null;
  },
  querySelectorAll() { return Object.values(buttons); },
  addEventListener(_e, fn) { this.ready = fn; },
};
const answers = scenario.answers.slice();
const sent = [], keptAtSend = [];
function fetch(_url, init) {
  sent.push(init.body);
  keptAtSend.push(storage.has("ameesh-approve:pending:" + PATH));
  const a = answers.shift();
  if (a === undefined || a === "network") return Promise.reject(new TypeError("Failed to fetch"));
  return Promise.resolve({ ok: a.status < 300, status: a.status,
                           json: () => Promise.resolve(a.body || {}) });
}
const statuses = [];
const context = {
  window: { location: { pathname: PATH, reload: () => { reloads += 1; } }, localStorage,
            PublicKeyCredential: function () {} },
  navigator: { credentials: { get: () => Promise.resolve(scenario.credential),
                              create: () => Promise.resolve(scenario.credential) } },
  document, fetch, atob, btoa, Uint8Array, JSON, Math, Date, Promise, String, Error,
  setTimeout: (fn) => { statuses.push(status.textContent); setImmediate(fn); return 0; },
  clearTimeout() {}, AbortController,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(script, "utf8"), context);
document.ready();
if (scenario.click) listeners[scenario.click]();
setTimeout(function wait(n) {
  if (answers.length && n < 50) return setImmediate(wait, (n || 0) + 1);
  setImmediate(() => setImmediate(() => {
    process.stdout.write(JSON.stringify({ sent, keptAtSend, status, statuses, reloads,
      kept: storage.get("ameesh-approve:pending:" + PATH) || null,
      storage: Array.from(storage.keys()) }), () => process.exit(0));
  }));
}, 20);
"""

BODY = {"decision": "approve", "credential_id": "Y3JlZA", "authenticatorData": "YXV0aA",
        "clientDataJSON": "Y2xpZW50", "signature": "c2ln"}
KEY = "ameesh-approve:pending:/a/" + "t" * 43


@unittest.skipUnless(shutil.which("node"), "node absent")
class ApproveJsDeliveryTest(unittest.TestCase):
    def run_page(self, scenario: dict) -> dict:
        directory = tempfile.mkdtemp(prefix="ameesh-approve-js-")
        self.addCleanup(shutil.rmtree, directory, True)
        harness = os.path.join(directory, "harness.js")
        with open(harness, "w", encoding="utf-8") as fh:
            fh.write(HARNESS)
        out = subprocess.run(["node", harness, os.path.abspath(SCRIPT), json.dumps(scenario)],
                             capture_output=True, text=True, timeout=60, cwd=directory)
        # cwd : le dossier du test — un `node` de mise ne lit pas la
        # configuration du dépôt, dont la confiance vit dans le vrai
        # XDG_STATE_HOME que tests.support déplace
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout)

    def kept(self, body=BODY, at=None):
        import time
        return {KEY: json.dumps({"at": at if at is not None else time.time() * 1000,
                                 "body": body})}

    def test_corps_garde_renvoye_tel_quel_jusqu_a_l_accuse(self):
        result = self.run_page({"storage": self.kept(), "answers": [
            "network", {"status": 503, "body": {"error": "registry", "retry": True}},
            {"status": 429, "body": {}}, {"status": 200, "body": {"decision": "approve"}}]})
        self.assertEqual([json.loads(b) for b in result["sent"]], [BODY] * 4)
        self.assertEqual((result["status"]["className"], result["kept"]), ("done", None))
        self.assertIn("recorded", result["status"]["textContent"])
        self.assertTrue(any("Sending" in s for s in result["statuses"]))
        for shown in result["statuses"] + [result["status"]["textContent"]]:
            self.assertNotIn("rror", shown)

    def test_refus_voulu_explique_et_jamais_renvoye(self):
        result = self.run_page({"storage": self.kept(), "answers": [
            {"status": 403, "body": {"error": "revoked", "message": "key revoked"}}]})
        self.assertEqual(len(result["sent"]), 1)
        self.assertEqual(result["status"]["className"], "error")
        self.assertIn("key revoked", result["status"]["textContent"])
        self.assertIsNone(result["kept"])

    def test_signature_gardee_sur_l_appareil_avant_l_envoi(self):
        credential = {"rawId": [1, 2], "response": {"authenticatorData": [3],
                                                    "clientDataJSON": [4], "signature": [5]}}
        result = self.run_page({"click": "approve", "credential": credential, "answers": [
            {"status": 500, "body": {"error": "internal", "retry": True}},
            {"status": 200, "body": {"decision": "approve"}}]})
        self.assertEqual(len(result["sent"]), 2)
        self.assertEqual(result["sent"][0], result["sent"][1])
        self.assertEqual(result["keptAtSend"], [True, True])
        self.assertEqual(result["status"]["className"], "done")
        self.assertIsNone(result["kept"])

    def kept_as(self, kind, body):
        import time
        return {KEY: json.dumps({"at": time.time() * 1000, "kind": kind, "body": body})}

    def test_enrolement_garde_la_passkey_avant_l_envoi(self):
        """B1 (cx2) : la passkey créée est gardée AVANT l'envoi ; une panne la
        laisse sur l'appareil, l'accusé l'efface."""
        credential = {"rawId": [1, 2], "response": {"clientDataJSON": [4],
                                                    "attestationObject": [5]}}
        result = self.run_page({"root": "enroll", "click": "enroll", "credential": credential,
                                "answers": [{"status": 500, "body": {"retry": True}},
                                            {"status": 200, "body": {"key_fingerprint": "ab"}}]})
        self.assertEqual(result["keptAtSend"], [True, True])
        self.assertEqual(result["sent"][0], result["sent"][1])
        self.assertEqual((result["status"]["className"], result["kept"]), ("done", None))
        self.assertIn("Passkey created", result["status"]["textContent"])

    def test_enrolement_rouvert_renvoie_la_passkey_gardee(self):
        body = {"credential_id": "Y3JlZA", "clientDataJSON": "Y2xpZW50",
                "attestationObject": "YXR0"}
        result = self.run_page({"root": "enroll", "storage": self.kept_as("enroll", body),
                                "answers": [{"status": 200, "body": {"key_fingerprint": "ab"}}]})
        self.assertEqual([json.loads(b) for b in result["sent"]], [body])
        self.assertEqual(result["status"]["className"], "done")

    def test_page_d_attente_renvoie_le_corps_garde(self):
        """B1 (cx2) : la page « Just a moment » (enrôlement en attente) ne
        recharge pas en boucle : elle renvoie le corps gardé."""
        body = {"credential_id": "Y3JlZA", "clientDataJSON": "Y2xpZW50",
                "attestationObject": "YXR0"}
        result = self.run_page({"root": "wait", "storage": self.kept_as("enroll", body),
                                "answers": [{"status": 503, "body": {"retry": True}},
                                            {"status": 200, "body": {"key_fingerprint": "ab"}}]})
        self.assertEqual([json.loads(b) for b in result["sent"]], [body, body])
        self.assertEqual((result["status"]["className"], result["kept"], result["reloads"]),
                         ("done", None, 0))
        self.assertIn("Passkey created", result["status"]["textContent"])

    def test_page_d_attente_sans_corps_se_recharge(self):
        result = self.run_page({"root": "wait", "answers": []})
        self.assertEqual((result["sent"], result["reloads"]), ([], 1))

    LOGIN = {"rawId": [1, 2], "response": {"authenticatorData": [3], "clientDataJSON": [4],
                                           "signature": [5]}}

    def test_login_final_garde_sur_l_appareil_avant_l_envoi(self):
        """B1 (cx3) : cancel signé, POST 503 : le corps reste sur l'appareil
        (la page fermée là ne perd pas la signature)."""
        result = self.run_page({"root": "login", "click": "login-cancel",
                                "credential": self.LOGIN,
                                "answers": [{"status": 503, "body": {"retry": True}}]})
        self.assertEqual(result["keptAtSend"][0], True)
        kept = json.loads(result["kept"])
        self.assertEqual((kept["kind"], kept["body"]["decision"]), ("login", "cancel"))

    def test_login_rouvert_renvoie_le_corps_garde(self):
        body = dict(BODY, decision="cancel")
        result = self.run_page({"root": "login", "storage": self.kept_as("login", body),
                                "answers": [{"status": 200, "body": {"decision": "cancel"}}]})
        self.assertEqual([json.loads(b) for b in result["sent"]], [body])
        self.assertEqual((result["status"]["className"], result["kept"]), ("done", None))
        self.assertIn("Cancellation signed", result["status"]["textContent"])

    def test_page_d_attente_renvoie_un_login_garde(self):
        body = dict(BODY, decision="done")
        result = self.run_page({"root": "wait", "storage": self.kept_as("login", body),
                                "answers": [{"status": 503, "body": {}},
                                            {"status": 200, "body": {"decision": "done"}}]})
        self.assertEqual([json.loads(b) for b in result["sent"]], [body, body])
        self.assertIn("Done signed", result["status"]["textContent"])
        self.assertEqual(result["reloads"], 0)

    def test_login_view_jamais_garde(self):
        result = self.run_page({"root": "login", "click": "login-open", "credential": self.LOGIN,
                                "answers": [{"status": 503, "body": {}}]})
        # renvoyé en mémoire (même corps), jamais écrit sur l'appareil
        self.assertTrue(result["keptAtSend"])
        self.assertFalse(any(result["keptAtSend"]))
        self.assertEqual(len(set(result["sent"])), 1)
        self.assertIsNone(result["kept"])

    def test_login_refus_voulu_non_rejoue(self):
        body = dict(BODY, decision="done")
        result = self.run_page({"root": "login", "storage": self.kept_as("login", body),
                                "answers": [{"status": 410, "body": {"error": "closed",
                                                                     "message": "closed"}}]})
        self.assertEqual(len(result["sent"]), 1)
        self.assertEqual((result["status"]["className"], result["kept"]), ("error", None))

    def test_corps_garde_trop_ancien_oublie(self):
        result = self.run_page({"storage": self.kept(at=0), "answers": []})
        self.assertEqual((result["sent"], result["storage"]), ([], []))


if __name__ == "__main__":
    unittest.main()

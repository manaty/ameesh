# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §16 (lot #3889) : la boîte d'envoi de la page de progression.

Un message est gardé sur l'appareil avec son Idempotency-Key AVANT l'envoi,
renvoyé tel quel après une panne, repris par une page rechargée, effacé à
l'accusé ; une issue inconnue garde la clé (le même texte la reprend), un
refus de saisie (4xx) guide. Le bloc `outbox:begin … outbox:end` de la page
tourne sous Node (sauté sans `node`)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

PAGE = os.path.join(os.path.dirname(__file__), os.pardir, "src", "ameesh",
                    "progress_page.html")

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [block, scenario] = [fs.readFileSync(process.argv[2], "utf8"), JSON.parse(process.argv[3])];
const storage = new Map(Object.entries(scenario.storage || {}));
const localStorage = {
  get length() { return storage.size; },
  key(i) { return Array.from(storage.keys())[i] ?? null; },
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};
const answers = scenario.answers.slice();
const sent = [];
function fetch(url, init) {
  sent.push({key: init.headers["Idempotency-Key"], body: JSON.parse(init.body),
             keptBefore: storage.has("ameesh-progress:outbox:a1")});
  const a = answers.shift();
  if (a === undefined || a === "network") return Promise.reject(new TypeError("Failed to fetch"));
  return Promise.resolve({status: a.status,
                          text: () => Promise.resolve(typeof a.body === "string" ? a.body : JSON.stringify(a.body || {}))});
}
const events = [];
const ctx = {window: {localStorage}, fetch, JSON, Math, Date, Promise, String, Error,
             CHAT: "/api/conversation.json", TOKEN: "t", RETRY_MS: [1, 1, 1, 1, 1],
             setTimeout: (fn) => setImmediate(fn), events};
vm.createContext(ctx);
vm.runInContext(block + "\n;this.Outbox = Outbox;", ctx);
const ui = {sending: (n) => events.push("sending" + (n ? "+" : "")), sent: () => events.push("sent"),
            checking: () => events.push("checking"), refused: (m) => events.push("refused:" + m)};
ctx.Outbox.watch("a1", ui);
for (const step of scenario.steps) {
  if (step === "resume") ctx.Outbox.resume();
  else ctx.Outbox.send("a1", step);
}
function done(n) {
  if (answers.length && n < 200) return setImmediate(done, n + 1);
  setTimeout(() => process.stdout.write(JSON.stringify({sent, events,
    kept: storage.get("ameesh-progress:outbox:a1") || null})), 20);
}
done(0);
"""

KEY = "ameesh-progress:outbox:a1"


@unittest.skipUnless(shutil.which("node"), "node absent")
class ProgressOutboxTest(unittest.TestCase):
    def run_outbox(self, scenario: dict) -> dict:
        with open(PAGE, encoding="utf-8") as fh:
            page = fh.read()
        block = re.search(r"// outbox:begin[^\n]*\n(.*?)// outbox:end", page, re.S)
        self.assertIsNotNone(block, "outbox block missing from the page")
        directory = tempfile.mkdtemp(prefix="ameesh-outbox-js-")
        self.addCleanup(shutil.rmtree, directory, True)
        paths = {}
        for name, text in (("harness.js", HARNESS), ("outbox.js", block.group(1))):
            paths[name] = os.path.join(directory, name)
            with open(paths[name], "w", encoding="utf-8") as fh:
                fh.write(text)
        out = subprocess.run(["node", paths["harness.js"], paths["outbox.js"],
                              json.dumps(scenario)],
                             capture_output=True, text=True, timeout=60, check=True)
        return json.loads(out.stdout)

    def kept(self, **extra):
        return {KEY: json.dumps(dict({"text": "bonjour", "key": "web-kept-0001",
                                      "at": time.time() * 1000}, **extra))}

    def test_panne_puis_accuse_meme_cle_garde_avant_l_envoi(self):
        result = self.run_outbox({"steps": ["bonjour"], "answers": [
            {"status": 503, "body": "the conversation is busy"}, "network",
            {"status": 429, "body": {}}, {"status": 200, "body": {"ok": True}}]})
        keys = {item["key"] for item in result["sent"]}
        self.assertEqual(len(result["sent"]), 4)
        self.assertEqual(len(keys), 1)
        self.assertTrue(all(item["keptBefore"] for item in result["sent"]))
        self.assertEqual(result["events"][-1], "sent")
        self.assertIsNone(result["kept"])

    def test_page_rechargee_reprend_l_envoi_avec_la_meme_cle(self):
        result = self.run_outbox({"storage": self.kept(), "steps": ["resume"],
                                  "answers": [{"status": 200, "body": {}}]})
        self.assertEqual([(s["key"], s["body"]["text"]) for s in result["sent"]],
                         [("web-kept-0001", "bonjour")])
        self.assertIsNone(result["kept"])

    def test_issue_inconnue_garde_la_cle_sans_renvoi_automatique(self):
        result = self.run_outbox({"steps": ["bonjour"], "answers": [
            {"status": 500, "body": "internal error: the message may have been delivered"}]})
        self.assertEqual(result["events"][-1], "checking")
        kept = json.loads(result["kept"])
        self.assertTrue(kept["checking"])
        self.assertEqual(kept["key"], result["sent"][0]["key"])
        # rechargée : rien n'est renvoyé de lui-même ; le même texte reprend la clé
        again = self.run_outbox({"storage": {KEY: result["kept"]}, "steps": ["resume", "bonjour"],
                                 "answers": [{"status": 409, "body": {
                                     "error": "idempotency_in_progress"}}]})
        self.assertEqual([s["key"] for s in again["sent"]], [kept["key"]])
        self.assertEqual(again["events"][-1], "checking")

    def test_autre_texte_autre_cle(self):
        result = self.run_outbox({"storage": self.kept(checking=True), "steps": ["autre"],
                                  "answers": [{"status": 200, "body": {}}]})
        self.assertNotEqual(result["sent"][0]["key"], "web-kept-0001")

    def test_saisie_refusee_guide_et_efface(self):
        result = self.run_outbox({"steps": ["bonjour"], "answers": [
            {"status": 400, "body": "agent: a name is required"}]})
        self.assertEqual(len(result["sent"]), 1)
        self.assertEqual(result["events"][-1], "refused:agent: a name is required")
        self.assertIsNone(result["kept"])

    def test_message_trop_ancien_oublie(self):
        result = self.run_outbox({"storage": self.kept(at=0), "steps": ["resume"],
                                  "answers": []})
        self.assertEqual((result["sent"], result["kept"]), ([], None))


if __name__ == "__main__":
    unittest.main()

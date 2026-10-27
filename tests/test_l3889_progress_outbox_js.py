# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §16 (lot #3889) : la boîte d'envoi de la page de progression.

Chaque message a sa propre entrée sur l'appareil, écrite avec son
Idempotency-Key AVANT l'envoi ; aucun nouvel envoi n'efface un message non
acquitté. Une panne est renvoyée telle quelle ; une issue inconnue (500, 409)
se réconcilie seule (la conversation relue, sinon la MÊME clé renvoyée),
jusqu'à l'accusé ; une page rouverte reprend tout ; un refus de saisie est
gardé et rendu à la saisie. Le bloc `outbox:begin … outbox:end` de la page
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
const PREFIX = "ameesh-progress:outbox:";
const storage = new Map(Object.entries(scenario.storage || {}));
const localStorage = {
  get length() { return storage.size; },
  key(i) { return Array.from(storage.keys())[i] ?? null; },
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};
const posts = (scenario.posts || []).slice(), reads = (scenario.reads || []).slice();
const sent = [];
function outbox() {
  return Array.from(storage.keys()).filter((k) => k.startsWith(PREFIX)).map((k) => JSON.parse(storage.get(k)));
}
function fetch(url, init) {
  if (init.method === "POST") {
    sent.push({method: "POST", key: init.headers["Idempotency-Key"], text: JSON.parse(init.body).text,
               keptBefore: outbox().some((m) => m.key === init.headers["Idempotency-Key"])});
    const a = posts.shift();
    if (a === undefined || a === "network") return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve({status: a.status,
      text: () => Promise.resolve(typeof a.body === "string" ? a.body : JSON.stringify(a.body || {}))});
  }
  sent.push({method: "GET"});
  const r = reads.shift();
  if (r === undefined || r === "network") return Promise.reject(new TypeError("Failed to fetch"));
  return Promise.resolve({ok: true, json: () => Promise.resolve({entries: r})});
}
const events = [];
const ctx = {window: {localStorage}, fetch, JSON, Math, Date, Promise, String, Error,
             encodeURIComponent, CHAT: "/api/conversation.json", TOKEN: "t",
             RETRY_MS: [1, 1, 1, 1, 1], setTimeout: (fn) => setImmediate(fn)};
vm.createContext(ctx);
vm.runInContext(block + "\n;this.Outbox = Outbox;", ctx);
const ui = {sending: (n) => events.push("sending"), sent: (m) => events.push("sent:" + m.text),
            checking: (m) => events.push("checking:" + m.text),
            refused: (m) => events.push("refused:" + m.text + ":" + m.refused)};
if (!scenario.watchLate) ctx.Outbox.watch("a1", ui);
for (const step of scenario.steps) {
  if (step === "resume") ctx.Outbox.resume();
  else ctx.Outbox.send("a1", step);
}
function done(n) {
  if ((posts.length || reads.length) && n < 500) return setImmediate(done, n + 1);
  let idle = 0;
  (function settle() {
    if (idle++ < 30) return setImmediate(settle);
    if (scenario.watchLate) ctx.Outbox.watch("a1", ui);
    const pending = ctx.Outbox.pending("a1");
    process.stdout.write(JSON.stringify({sent, events, input: pending ? pending.text : null,
                                         outbox: outbox()}),
                         () => process.exit(0));   // une réconciliation sans fin s'arrête ici
  })();
}
done(0);
"""

PREFIX = "ameesh-progress:outbox:"


def entry(text="bonjour", key="web-kept-0001", **extra):
    message = dict({"agent": "a1", "text": text, "key": key, "at": time.time() * 1000,
                    "state": "sending"}, **extra)
    return {PREFIX + "a1\n" + key: json.dumps(message)}


def human(text, ts=None):
    return {"kind": "human_message", "text": text, "author": "human:owner",
            "ts": time.time() if ts is None else ts}


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

    @staticmethod
    def posted(result):
        return [item for item in result["sent"] if item["method"] == "POST"]

    def test_panne_puis_accuse_meme_cle_garde_avant_l_envoi(self):
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 503, "body": "the conversation is busy"}, "network",
            {"status": 429, "body": {}}, {"status": 200, "body": {"ok": True}}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 4)
        self.assertEqual(len({item["key"] for item in posts}), 1)
        self.assertTrue(all(item["keptBefore"] for item in posts))
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_page_rechargee_reprend_l_envoi_avec_la_meme_cle(self):
        result = self.run_outbox({"storage": entry(), "steps": ["resume"],
                                  "posts": [{"status": 200, "body": {}}]})
        self.assertEqual([(p["key"], p["text"]) for p in self.posted(result)],
                         [("web-kept-0001", "bonjour")])
        self.assertEqual(result["outbox"], [])

    def test_issue_inconnue_se_reconcilie_seule_sans_double_effet(self):
        """B2 (cx3) : 500 puis 409 « en cours » : la page relit la conversation
        et renvoie la MÊME clé, jusqu'à voir le message déposé."""
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 500, "body": "internal error: the message may have been delivered"},
            {"status": 409, "body": {"error": "idempotency_in_progress"}},
            {"status": 409, "body": {"error": "idempotency_in_progress"}}],
            "reads": [[], [human("autre chose")], [human("bonjour")]]})
        posts = self.posted(result)
        self.assertEqual(len({p["key"] for p in posts}), 1)
        self.assertEqual(len(posts), 3)        # 500, puis 409 tant que l'issue reste inconnue
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_reconciliation_ignore_un_meme_texte_plus_ancien(self):
        old = time.time() - 3600
        result = self.run_outbox({"storage": entry(state="checking"), "steps": ["resume"],
                                  "reads": [[human("bonjour", ts=old)]],
                                  "posts": [{"status": 200, "body": {}}]})
        self.assertEqual([p["key"] for p in self.posted(result)], ["web-kept-0001"])
        self.assertEqual(result["outbox"], [])

    def test_page_rouverte_serveur_pret_reprend_une_issue_inconnue(self):
        """B2 (cx3) : une entrée « checking » reprend seule à la réouverture."""
        result = self.run_outbox({"storage": entry(state="checking"), "steps": ["resume"],
                                  "reads": [[]], "posts": [{"status": 200, "body": {}}]})
        self.assertEqual([p["key"] for p in self.posted(result)], ["web-kept-0001"])
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_second_message_avant_l_accuse_n_efface_pas_le_premier(self):
        """B2 (cx3) : chaque message garde sa clé ; les deux finissent acquittés."""
        result = self.run_outbox({"steps": ["premier", "second"], "posts": [
            {"status": 503, "body": "busy"}, {"status": 200, "body": {}},
            {"status": 200, "body": {}}]})
        posts = self.posted(result)
        keys = {}
        for item in posts:
            keys.setdefault(item["text"], set()).add(item["key"])
        self.assertEqual(sorted(keys), ["premier", "second"])
        self.assertTrue(all(len(v) == 1 for v in keys.values()))
        self.assertNotEqual(keys["premier"], keys["second"])
        self.assertEqual(sorted(e for e in result["events"] if e.startswith("sent:")),
                         ["sent:premier", "sent:second"])
        self.assertEqual(result["outbox"], [])

    def test_second_message_pendant_une_issue_inconnue_garde_les_deux(self):
        result = self.run_outbox({"steps": ["premier", "second"], "posts": [
            {"status": 500, "body": "may have been delivered"}, {"status": 200, "body": {}}],
            "reads": ["network"]})
        self.assertEqual([(m["text"], m["state"]) for m in result["outbox"]],
                         [("premier", "checking")])
        self.assertIn("sent:second", result["events"])

    def test_saisie_refusee_gardee_rendue_a_la_saisie_non_renvoyee(self):
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 400, "body": "agent: a name is required"}]})
        self.assertEqual(len(self.posted(result)), 1)
        self.assertEqual(result["events"][-1], "refused:bonjour:agent: a name is required")
        self.assertEqual(result["input"], "bonjour")
        [kept] = result["outbox"]
        again = self.run_outbox({"storage": {PREFIX + "a1\n" + kept["key"]: json.dumps(kept)},
                                 "steps": ["resume"]})
        self.assertEqual(self.posted(again), [])
        self.assertEqual(again["input"], "bonjour")

    def test_jeton_refuse_repris_par_la_page_rouverte(self):
        """B4 (dev3) : la reprise tourne avant l'ouverture de la fiche ; un 401
        (ancien jeton) garde le message, que la page rouverte renvoie."""
        result = self.run_outbox({"storage": entry(), "steps": ["resume"], "watchLate": True,
                                  "posts": [{"status": 401, "body": ""}]})
        self.assertEqual(result["input"], "bonjour")
        self.assertTrue(result["events"][-1].startswith("refused:bonjour:token refused"))
        [kept] = result["outbox"]
        self.assertEqual(kept["state"], "auth")
        again = self.run_outbox({"storage": {PREFIX + "a1\n" + kept["key"]: json.dumps(kept)},
                                 "steps": ["resume"], "posts": [{"status": 200, "body": {}}]})
        self.assertEqual([p["key"] for p in self.posted(again)], ["web-kept-0001"])
        self.assertEqual(again["outbox"], [])

    def test_meme_texte_renvoye_reprend_la_meme_cle(self):
        result = self.run_outbox({"storage": entry(state="refused", refused="x"),
                                  "steps": ["bonjour"], "posts": [{"status": 200, "body": {}}]})
        self.assertEqual([p["key"] for p in self.posted(result)], ["web-kept-0001"])

    def test_message_trop_ancien_oublie(self):
        result = self.run_outbox({"storage": entry(at=0), "steps": ["resume"]})
        self.assertEqual((result["sent"], result["outbox"]), ([], []))


if __name__ == "__main__":
    unittest.main()

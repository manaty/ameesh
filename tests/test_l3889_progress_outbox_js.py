# SPDX-License-Identifier: AGPL-3.0-only
"""Canon §16 (lot #3889) : la boîte d'envoi de la page de progression.

Chaque message a sa propre entrée sur l'appareil, écrite avec son
Idempotency-Key AVANT l'envoi ; aucun nouvel envoi n'efface un message non
acquitté. Une panne (réseau, corps illisible) est renvoyée telle quelle ; une
issue inconnue (500, 409) renvoie la MÊME clé jusqu'à l'accusé du serveur pour
cette clé — jamais sur un texte vu dans la conversation (cx3, dev3) ; une page
rouverte reprend tout ; un refus de saisie est
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
const sent = [], aborts = [];
function outbox() {
  return Array.from(storage.keys()).filter((k) => k.startsWith(PREFIX)).map((k) => JSON.parse(storage.get(k)));
}
function fetch(url, init) {
  if (init.method === "POST") {
    sent.push({method: "POST", key: init.headers["Idempotency-Key"], text: JSON.parse(init.body).text,
               keptBefore: outbox().some((m) => m.key === init.headers["Idempotency-Key"])});
    const a = posts.shift();
    if (a === undefined || a === "network") return Promise.reject(new TypeError("Failed to fetch"));
    // suspendu : ne se règle qu'à l'abandon du client (AbortController)
    const hung = () => new Promise((_, reject) => {
      if (init.signal) init.signal.addEventListener("abort", () => { aborts.push(a.hang ? "fetch" : "body"); reject(new Error("aborted")); });
    });
    if (a.hang) return hung();
    return Promise.resolve({status: a.status,
      text: () => a.bodyHangs ? hung()
        : a.bodyFails ? Promise.reject(new TypeError("network error while reading"))
        : Promise.resolve(typeof a.body === "string" ? a.body : JSON.stringify(a.body || {}))});
  }
  sent.push({method: "GET"});
  const r = reads.shift();
  if (r === undefined || r === "network") return Promise.reject(new TypeError("Failed to fetch"));
  return Promise.resolve({ok: true, json: () => Promise.resolve({entries: r})});
}
const events = [];
const ctx = {window: {localStorage}, fetch, JSON, Math, Date, Promise, String, Error,
             encodeURIComponent, CHAT: "/api/conversation.json", TOKEN: "t",
             RETRY_MS: [1, 1, 1, 1, 1], setTimeout: (fn) => setImmediate(fn),
             clearTimeout: (h) => clearImmediate(h), AbortController};
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
    process.stdout.write(JSON.stringify({sent, events, aborts, input: pending ? pending.text : null,
                                         outbox: outbox()}),
                         () => process.exit(0));   // une réconciliation sans fin s'arrête ici
  })();
}
done(0);
"""

PREFIX = "ameesh-progress:outbox:"
#: l'accusé du serveur : l'enveloppe de l'action et le message déposé
ACK = {"schema": "ameesh-action/1", "action": "chat",
       "result": {"agent": "a1", "message_id": 7}}


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
                             capture_output=True, text=True, timeout=60, cwd=directory)
        # cwd : le dossier du test — un `node` de mise ne lit pas la
        # configuration du dépôt, dont la confiance vit dans le vrai
        # XDG_STATE_HOME que tests.support déplace
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout)

    @staticmethod
    def posted(result):
        return [item for item in result["sent"] if item["method"] == "POST"]

    def test_panne_puis_accuse_meme_cle_garde_avant_l_envoi(self):
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 503, "body": "the conversation is busy"}, "network",
            {"status": 429, "body": {}}, {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 4)
        self.assertEqual(len({item["key"] for item in posts}), 1)
        self.assertTrue(all(item["keptBefore"] for item in posts))
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_page_rechargee_reprend_l_envoi_avec_la_meme_cle(self):
        result = self.run_outbox({"storage": entry(), "steps": ["resume"],
                                  "posts": [{"status": 200, "body": ACK}]})
        self.assertEqual([(p["key"], p["text"]) for p in self.posted(result)],
                         [("web-kept-0001", "bonjour")])
        self.assertEqual(result["outbox"], [])

    def test_issue_inconnue_meme_cle_jusqu_a_l_accuse_du_serveur(self):
        """B2 (cx3) : 500 puis 409 « en cours » : la MÊME clé repart jusqu'à
        l'accusé du serveur pour elle ; la conversation n'est jamais lue."""
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 500, "body": "internal error: the message may have been delivered"},
            {"status": 409, "body": {"error": "idempotency_in_progress"}},
            {"status": 409, "body": {"error": "idempotency_in_progress"}},
            {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len({p["key"] for p in posts}), 1)
        self.assertEqual(len(posts), 4)
        self.assertEqual([s for s in result["sent"] if s["method"] == "GET"], [])
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_meme_texte_recent_dans_la_conversation_n_acquitte_pas(self):
        """B1 (cx3), B (dev3) : deux gestes identiques récents, d'auteurs
        différents, dans la conversation : sans accusé du serveur pour SA
        clé, le message reste gardé (« checking ») et seule sa clé repart."""
        now = time.time()
        other = dict(human("bonjour", ts=now), author="human:autre")
        reads = [[human("bonjour", ts=now - 1), other, human("bonjour", ts=now + 1)]] * 3
        result = self.run_outbox({"storage": entry(state="checking"), "steps": ["resume"],
                                  "reads": reads, "posts": [
            {"status": 409, "body": {"error": "idempotency_in_progress"}},
            {"status": 409, "body": {"error": "idempotency_in_progress"}}]})
        self.assertEqual([p["key"] for p in self.posted(result)][:2],
                         ["web-kept-0001", "web-kept-0001"])
        self.assertNotIn("sent:bonjour", result["events"])
        [kept] = result["outbox"]
        self.assertEqual((kept["key"], kept["state"]), ("web-kept-0001", "checking"))

    def test_corps_illisible_apres_les_en_tetes_meme_cle(self):
        """B2 (cx3) : `r.text()` rejeté (même après un 200) : issue
        indéterminée, la même clé repart ; le rejeu du serveur acquitte."""
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 200, "bodyFails": True}, {"status": 503, "bodyFails": True},
            {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 3)
        self.assertEqual(len({p["key"] for p in posts}), 1)
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_200_sans_accuse_lisible_garde_et_renvoie_la_meme_cle(self):
        """B (cx1, gel #21581) : un 200 au corps HTML (mandataire) ou sans
        enveloppe n'acquitte pas : même clé, jusqu'à l'accusé valide."""
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 200, "body": "<html>proxy</html>"}, {"status": 200, "body": {}},
            {"status": 200, "body": {"schema": "ameesh-action/1", "result": {}}},
            {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 4)
        self.assertEqual(len({p["key"] for p in posts}), 1)
        self.assertEqual(result["events"].count("sent:bonjour"), 1)
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_200_d_une_autre_action_ou_d_un_autre_agent_n_acquitte_pas(self):
        """B (docs-01, gel #21693) : l'accusé est celui de CE message — action
        « chat », son agent ; sinon même clé, jusqu'à l'accusé valide."""
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 200, "body": dict(ACK, action="message")},
            {"status": 200, "body": dict(ACK, result={"agent": "a2", "message_id": 8})},
            {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 3)
        self.assertEqual(len({p["key"] for p in posts}), 1)
        self.assertEqual(result["events"].count("sent:bonjour"), 1)
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_200_html_sans_suite_reste_garde(self):
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 200, "body": "<html>proxy</html>"}]})
        self.assertNotIn("sent:bonjour", result["events"])
        [kept] = result["outbox"]
        self.assertEqual(kept["state"], "checking")

    def test_issue_inconnue_puis_jeton_refuse_passe_en_auth(self):
        """N (dev3) : une réconciliation sous un jeton périmé ne boucle pas."""
        result = self.run_outbox({"storage": entry(state="checking"), "steps": ["resume"],
                                  "posts": [{"status": 401, "body": ""}]})
        self.assertEqual(len(self.posted(result)), 1)
        [kept] = result["outbox"]
        self.assertEqual(kept["state"], "auth")
        self.assertEqual(result["input"], "bonjour")

    def test_page_rouverte_serveur_pret_reprend_une_issue_inconnue(self):
        """B2 (cx3) : une entrée « checking » reprend seule à la réouverture."""
        result = self.run_outbox({"storage": entry(state="checking"), "steps": ["resume"],
                                  "posts": [{"status": 200, "body": ACK}]})
        self.assertEqual([p["key"] for p in self.posted(result)], ["web-kept-0001"])
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_second_message_avant_l_accuse_n_efface_pas_le_premier(self):
        """B2 (cx3) : chaque message garde sa clé ; les deux finissent acquittés."""
        result = self.run_outbox({"steps": ["premier", "second"], "posts": [
            {"status": 503, "body": "busy"}, {"status": 200, "body": ACK},
            {"status": 200, "body": ACK}]})
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
            {"status": 500, "body": "may have been delivered"}, {"status": 200, "body": ACK}]})
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
                                 "steps": ["resume"], "posts": [{"status": 200, "body": ACK}]})
        self.assertEqual([p["key"] for p in self.posted(again)], ["web-kept-0001"])
        self.assertEqual(again["outbox"], [])

    def test_meme_texte_renvoye_reprend_la_meme_cle(self):
        result = self.run_outbox({"storage": entry(state="refused", refused="x"),
                                  "steps": ["bonjour"], "posts": [{"status": 200, "body": ACK}]})
        self.assertEqual([p["key"] for p in self.posted(result)], ["web-kept-0001"])

    def test_message_trop_ancien_oublie(self):
        result = self.run_outbox({"storage": entry(at=0), "steps": ["resume"]})
        self.assertEqual((result["sent"], result["outbox"]), ([], []))


    def test_requete_suspendue_abandonnee_puis_accuse_meme_cle(self):
        """B/P2 (cx1, gel #22297) : une réponse suspendue est abandonnée au
        délai ; renvoyer le même texte ou reprendre pendant l'attente ne
        double rien ; la même clé repart et l'accusé l'efface."""
        result = self.run_outbox({"steps": ["bonjour", "resume", "bonjour"], "posts": [
            {"hang": True}, {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 2)
        self.assertEqual(len({(p["key"], p["text"]) for p in posts}), 1)
        self.assertEqual(result["aborts"], ["fetch"])
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_corps_suspendu_abandonne_puis_accuse_meme_cle(self):
        result = self.run_outbox({"steps": ["bonjour"], "posts": [
            {"status": 200, "bodyHangs": True}, {"status": 200, "body": ACK}]})
        posts = self.posted(result)
        self.assertEqual(len(posts), 2)
        self.assertEqual(len({p["key"] for p in posts}), 1)
        self.assertEqual(result["aborts"], ["body"])
        self.assertEqual(result["events"][-1], "sent:bonjour")
        self.assertEqual(result["outbox"], [])

    def test_reponse_a_temps_aucun_abandon(self):
        result = self.run_outbox({"steps": ["bonjour"], "posts": [{"status": 200, "body": ACK}]})
        self.assertEqual(result["aborts"], [])
        self.assertEqual(len(self.posted(result)), 1)

if __name__ == "__main__":
    unittest.main()

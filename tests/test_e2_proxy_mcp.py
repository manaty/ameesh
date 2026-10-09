# SPDX-License-Identifier: AGPL-3.0-only
"""Étude v2 E2 : proxy MCP, point d'application des outils d'une persona."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from ameesh import mcp_proxy as mp

FAUX_SERVEUR = r'''
import json, sys
log = open(sys.argv[1], "a")
for ligne in sys.stdin:
    msg = json.loads(ligne)
    log.write(json.dumps(msg) + "\n"); log.flush()
    if "id" not in msg:
        continue
    if msg["method"] == "tools/list":
        res = {"tools": [{"name": "lire_colis"}, {"name": "supprimer_colis"}]}
    elif msg["method"] == "tools/call":
        res = {"content": [{"type": "text", "text": "fait : " + msg["params"]["name"]}]}
    else:
        res = {"capabilities": {}}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
'''


class PolitiqueTest(unittest.TestCase):
    def test_outils_permis(self):
        self.assertEqual(mp.allowed_tools(["git", "mcp:transport"], "transport"), (True, set()))
        self.assertEqual(mp.allowed_tools(["mcp:transport/lire_colis", "mcp:autre"], "transport"),
                         (False, {"lire_colis"}))
        self.assertEqual(mp.allowed_tools(None, "transport"), (False, set()))
        p = mp.Policy("a", "transport", ["mcp:transport/lire_colis"])
        self.assertTrue(p.permet("lire_colis"))
        self.assertFalse(p.permet("supprimer_colis"))


class RelaisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-e2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.serveur = os.path.join(self.tmp, "serveur.py")
        with open(self.serveur, "w", encoding="utf-8") as fh:
            fh.write(FAUX_SERVEUR)
        self.recu = os.path.join(self.tmp, "recu.jsonl")
        self.trace = os.path.join(self.tmp, "trace.jsonl")

    def dialogue(self, tools, *messages):
        entree = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        sortie = io.StringIO()
        code = mp.relayer([sys.executable, self.serveur, self.recu],
                          mp.Policy("verif-a", "transport", tools), mp.Journal(self.trace),
                          entree=entree, sortie=sortie)
        self.assertEqual(code, 0)
        return {r["id"]: r for r in map(json.loads, sortie.getvalue().splitlines())}

    def test_appel_refuse_n_atteint_pas_le_serveur(self):
        rep = self.dialogue(
            ["mcp:transport/lire_colis"],
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "lire_colis", "arguments": {"ref": "secret-123"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "supprimer_colis", "arguments": {"ref": "X"}}})
        self.assertEqual([t["name"] for t in rep[2]["result"]["tools"]], ["lire_colis"])
        self.assertEqual(rep[3]["result"]["content"][0]["text"], "fait : lire_colis")
        self.assertEqual(rep[4]["error"]["code"], mp.REFUS)
        self.assertIn("non permis", rep[4]["error"]["message"])
        with open(self.recu, encoding="utf-8") as fh:
            vus = [json.loads(l) for l in fh]
        self.assertNotIn("supprimer_colis", json.dumps(vus))          # jamais relayé
        with open(self.trace, encoding="utf-8") as fh:
            trace = [json.loads(l) for l in fh]
        self.assertEqual([(t["tool"], t["decision"]) for t in trace],
                         [("lire_colis", "permis"), ("supprimer_colis", "refusé")])
        self.assertNotIn("secret-123", json.dumps(trace))             # pas d'arguments

    def test_sans_fiche_tout_est_refuse(self):
        rep = self.dialogue([], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "lire_colis"}})
        self.assertEqual(rep[1]["result"]["tools"], [])
        self.assertIn("error", rep[2])



class ConfigurationTest(unittest.TestCase):
    def test_seuls_les_serveurs_permis_et_par_le_proxy(self):
        servers = {"transport": {"command": "node", "args": ["t.js"], "env": {"X": "1"}},
                   "paie": {"command": "paie-mcp"},
                   "casse": {"args": ["sans commande"]}}
        conf = mp.persona_config("verif-a", ["git", "mcp:transport/lire_colis"], servers)
        self.assertEqual(list(conf["mcpServers"]), ["transport"])
        t = conf["mcpServers"]["transport"]
        self.assertEqual(t["command"], "ameesh")
        self.assertEqual(t["args"], ["mcp-proxy", "--persona", "verif-a", "--server", "transport",
                                     "--", "node", "t.js"])
        self.assertEqual(t["env"], {"X": "1"})
        self.assertEqual(mp.persona_config("x", [], servers), {"mcpServers": {}})


if __name__ == "__main__":
    unittest.main()

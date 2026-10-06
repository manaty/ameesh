#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Faux agent ACP des tests (L16) : aucun réseau, aucun agent réel, aucun coût.

Le pont ACP (`ameesh.acp`) lance ce script comme n'importe quel agent ACP :
JSON-RPC 2.0 sur l'entrée/sortie standard, messages délimités par des retours à
la ligne. Son comportement se règle par l'environnement :

* `FAKE_ACP_MODE` : `normal` (défaut), `permission`, `load`, `resume`, `hang`,
  `exit`, `garbage`, `auth`, `noresume`, `fs`, `unknown`, `badresponse`,
  `mute` (ne répond à rien), `badenveloppe` (`FAKE_ACP_CASE` = `id`, `method`,
  `jsonrpc`, `params`, `errorcode`) ;
* `FAKE_ACP_LOG`  : fichier JSONL où chaque méthode reçue est tracée (preuve
  qu'un `session/load`, un `session/cancel` ou une réponse de permission a bien
  été émis par le pont) ;
* `FAKE_ACP_IGNORE_SIGTERM=1` : l'agent ignore SIGTERM (le test d'annulation
  peut alors vérifier que le pont envoie `session/cancel` avant de mourir) ;
* `FAKE_ACP_COST` : coût cumulé annoncé par `usage_update` (défaut 0.02).

Sortie : les notifications et réponses que ferait un vrai agent, jamais autre
chose sur la sortie standard (le protocole l'interdit).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def trace(entry: dict) -> None:
    path = os.environ.get("FAKE_ACP_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


class FakeAgent:
    def __init__(self, mode: str):
        self.mode = mode
        self.session_id = "sess-fake-1"
        self.prompt_id = None
        self.authenticated = False
        self.pending_permission = None
        self.pending_fs = None
        self.fs_results: list = []
        self.cancelled = False

    # -- tours -------------------------------------------------------------
    def on_prompt(self, params: dict) -> None:
        self.prompt_id = params.get("_id")
        trace({"method": "session/prompt"})
        self.update({"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": "bonjour"}})
        self.update({"sessionUpdate": "tool_call", "toolCallId": "c1",
                     "title": "lire un fichier", "kind": "read", "status": "pending"})
        if self.mode == "exit":
            sys.exit(0)
        if self.mode == "hang":
            self.update({"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": " en cours"}})
            return  # la fin viendra de session/cancel
        if self.mode == "permission":
            self.pending_permission = 900
            send({"jsonrpc": "2.0", "id": self.pending_permission,
                  "method": "session/request_permission",
                  "params": {"sessionId": self.session_id,
                             "toolCall": {"toolCallId": "c1", "title": "lire un fichier",
                                          "kind": "read"},
                             "options": [
                                 {"optionId": "allow-1", "name": "Oui",
                                  "kind": "allow_once"},
                                 {"optionId": "reject-1", "name": "Non",
                                  "kind": "reject_once"}]}})
            return
        if self.mode == "fs":
            # L'agent demande au client d'écrire puis de lire, dans le dossier
            # du tour, puis tente une lecture HORS du dossier (doit être refusée).
            self.pending_fs = "write"
            send({"jsonrpc": "2.0", "id": 800, "method": "fs/write_text_file",
                  "params": {"sessionId": self.session_id,
                             "path": os.environ["FAKE_ACP_FILE"],
                             "content": "écrit par l'agent\n"}})
            return
        self.finish()

    def on_fs_answer(self, message: dict) -> None:
        self.fs_results.append(message.get("result")
                               if "result" in message else {"error": message.get("error")})
        if self.pending_fs == "write":
            self.pending_fs = "read"
            send({"jsonrpc": "2.0", "id": 801, "method": "fs/read_text_file",
                  "params": {"sessionId": self.session_id,
                             "path": os.environ["FAKE_ACP_FILE"]}})
            return
        if self.pending_fs == "read":
            self.pending_fs = "outside"
            send({"jsonrpc": "2.0", "id": 802, "method": "fs/read_text_file",
                  "params": {"sessionId": self.session_id,
                             "path": os.environ.get("FAKE_ACP_OUTSIDE", "/etc/hostname")}})
            return
        self.pending_fs = None
        self.update({"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text",
                                 "text": " fs=" + json.dumps(self.fs_results,
                                                             ensure_ascii=False, sort_keys=True)}})
        self.finish()

    def on_permission_answer(self, message: dict) -> None:
        trace({"method": "permission_answer", "result": message.get("result")})
        self.update({"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text",
                                 "text": " réponse=" + json.dumps(
                                     message.get("result"), ensure_ascii=False,
                                     sort_keys=True)}})
        self.pending_permission = None
        self.finish()

    def finish(self, stop: str = "end_turn") -> None:
        cost = float(os.environ.get("FAKE_ACP_COST", "0.02"))
        self.update({"sessionUpdate": "usage_update", "used": 1234, "size": 200000,
                     "cost": {"amount": cost, "currency": "USD"}})
        send({"jsonrpc": "2.0", "id": self.prompt_id,
              "result": {"stopReason": stop}})
        self.prompt_id = None

    def on_cancel(self, params: dict) -> None:
        trace({"method": "session/cancel"})
        if self.prompt_id is None:
            return
        self.update({"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": " annulé"}})
        self.finish("cancelled")

    def update(self, update: dict) -> None:
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": self.session_id, "update": update}})

    # -- protocole ---------------------------------------------------------
    def handle(self, message: dict) -> None:
        method = message.get("method")
        mid = message.get("id")
        params = message.get("params") or {}
        if method is None and mid == self.pending_permission:
            self.on_permission_answer(message)
            return
        if method is None and self.pending_fs is not None and mid in (800, 801, 802):
            self.on_fs_answer(message)
            return
        if self.mode == "mute":
            return  # ne répond à rien : le pont doit conclure par un délai
        if method == "initialize" and self.mode == "badenveloppe":
            # Enveloppe JSON-RPC non conforme : le pont doit enregistrer une
            # erreur fatale et conclure, jamais attendre le délai.
            cas = os.environ.get("FAKE_ACP_CASE", "id")
            trace({"method": "badenveloppe", "case": cas})
            if cas == "id":
                send({"jsonrpc": "2.0", "id": [1], "result": {}})
            elif cas == "method":
                send({"jsonrpc": "2.0", "method": {"a": 1}, "params": {}})
            elif cas == "jsonrpc":
                send({"jsonrpc": "1.0", "id": mid, "result": {}})
            elif cas == "params":
                send({"jsonrpc": "2.0", "id": 77, "method": "x/inconnu", "params": "texte"})
            elif cas == "errorcode":
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": "pas-un-entier", "message": "m"}})
            return
        if method == "initialize":
            trace({"method": "initialize"})
            capabilities = {"loadSession": self.mode != "noresume"}
            if self.mode == "resume":
                capabilities["sessionCapabilities"] = {"resume": {}}
            auth = [{"id": "fake-auth", "type": "agent"}] if self.mode == "auth" else []
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1, "agentCapabilities": capabilities,
                "agentInfo": {"name": "faux-agent", "version": "1.0.0"},
                "authMethods": auth}})
            if self.mode == "badresponse":
                send({"jsonrpc": "2.0", "id": 999, "result": {}})
        elif method == "authenticate":
            trace({"method": "authenticate", "methodId": params.get("methodId")})
            self.authenticated = True
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/new":
            if self.mode == "auth" and not self.authenticated:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000, "message": "authentification requise"}})
                return
            trace({"method": "session/new"})
            self.session_id = "sess-fake-1"
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "sessionId": self.session_id,
                "configOptions": [{
                    "id": "model", "name": "Modèle", "category": "model",
                    "type": "select", "currentValue": "modele-0",
                    "options": [{"value": "modele-0"}, {"value": "modele-1"}]}]}})
            if self.mode == "unknown":
                send({"jsonrpc": "2.0", "method": "x/inconnu", "params": {}})
        elif method == "session/load":
            trace({"method": "session/load"})
            self.session_id = str(params.get("sessionId"))
            if self.mode == "load":
                self.update({"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": "REJEU"}})
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/resume":
            trace({"method": "session/resume"})
            self.session_id = str(params.get("sessionId"))
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/set_config_option":
            trace({"method": "session/set_config_option", "configId": params.get("configId"),
                   "value": params.get("value")})
            send({"jsonrpc": "2.0", "id": mid, "result": {"configOptions": [{
                "id": "model", "name": "Modèle", "category": "model", "type": "select",
                "currentValue": params.get("value"),
                "options": [{"value": "modele-0"}, {"value": "modele-1"}]}]}})
        elif method == "session/prompt":
            self.on_prompt(dict(params, _id=mid))
        elif method == "session/cancel":
            self.on_cancel(params)
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "méthode inconnue : %s" % method}})

    def run(self) -> None:
        if os.environ.get("FAKE_ACP_IGNORE_SIGTERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if self.mode == "garbage":
            longueur = int(os.environ.get("FAKE_ACP_GARBAGE_LEN", "0"))
            texte = "ceci n'est pas du JSON"
            if longueur > len(texte):
                texte = texte * (longueur // len(texte) + 1)
                texte = texte[:longueur]
            sys.stdout.write(texte + "\n")
            sys.stdout.flush()
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.handle(message)


if __name__ == "__main__":
    time.sleep(float(os.environ.get("FAKE_ACP_START_DELAY", "0")))
    FakeAgent(os.environ.get("FAKE_ACP_MODE", "normal")).run()

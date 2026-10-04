# SPDX-License-Identifier: AGPL-3.0-only
"""Adaptateurs des trois harnais : construire la commande, lire le flux JSONL.

Les lignes de commande reprennent exactement celles de `nexlink-agent.v0.sh` :

* **claude**   : `claude -p --output-format stream-json --verbose
                 --dangerously-skip-permissions [--resume SID] TEXTE`
* **codex**    : `codex --dangerously-bypass-hook-trust exec
                 --dangerously-bypass-approvals-and-sandbox --json
                 [resume SID] TEXTE`
* **deepseek** : `DSH_PERMISSION_MODE=danger-full-access dsh --profile agent
                 --json [--session-id SID] TEXTE`

Le binaire est résolu par `AMEESH_<HARNAIS>_BIN` (ou l'ancien
`AGENT_MESH_<HARNAIS>_BIN`), puis `AMEESH_BIN_DIR/<nom>`, puis le PATH — ce qui permet au banc de test
d'utiliser des faux harnais sans jamais toucher aux vrais.
"""
from __future__ import annotations

import json
import os
import shutil

from .config import HARNESSES

MAIL_PROMPT = (
    "Tu as des messages agent-mail : lance agent-mail inbox, lis-les, "
    "puis continue ton travail sans attendre de confirmation."
)
IDLE_PROMPT = (
    "Reprise : si ton lot n'est ni gelé ni fusionné, continue-le ; sinon prends "
    "la suite de ton affectation (board, workstream). Si tu n'as rien à faire, "
    "dis-le par agent-mail send orchestrateur puis arrête-toi."
)

BIN_ENV = {
    "claude": ("AMEESH_CLAUDE_BIN", "AGENT_MESH_CLAUDE_BIN"),
    "codex": ("AMEESH_CODEX_BIN", "AGENT_MESH_CODEX_BIN"),
    "deepseek": ("AMEESH_DSH_BIN", "AGENT_MESH_DSH_BIN"),
}
#: anciens noms acceptés en repli (renommage agent-mesh → ameesh)
BIN_DIR_ENV = ("AMEESH_BIN_DIR", "AGENT_MESH_BIN_DIR")
DEFAULT_BIN = {"claude": "claude", "codex": "codex", "deepseek": "dsh"}


class HarnessMissing(RuntimeError):
    """Le binaire du harnais n'est pas là : on ne lance pas un tour dans le vide."""


def resolve_binary(harness: str, override: str | None = None) -> str:
    keys = BIN_ENV.get(harness)
    if not keys:
        raise HarnessMissing("harnais inconnu : %r (connus : %s)" % (harness, ", ".join(HARNESSES)))
    candidates = []
    if override:
        candidates.append(override)
    for key in keys:
        if os.environ.get(key):
            candidates.append(os.environ[key])
    for nom in BIN_DIR_ENV:
        bindir = os.environ.get(nom)
        if bindir:
            candidates.append(os.path.join(bindir, DEFAULT_BIN[harness]))
    for candidate in candidates:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        if os.path.sep in path:
            raise HarnessMissing("binaire %s introuvable ou non exécutable : %s" % (harness, path))
    found = shutil.which(DEFAULT_BIN[harness])
    if not found:
        raise HarnessMissing(
            "binaire %s introuvable : installez-le, mettez %s=/chemin/vers/%s "
            "ou AMEESH_BIN_DIR" % (DEFAULT_BIN[harness], keys[0], DEFAULT_BIN[harness])
        )
    return found


class HarnessAdapter:
    """Un harnais = une commande + un lecteur de flux JSONL."""

    key = ""
    resume = ""

    def __init__(self, binary: str | None = None):
        self.binary = resolve_binary(self.key, binary)

    # -- commande ----------------------------------------------------------
    def command(self, text: str, session_id: str | None = None) -> list[str]:
        raise NotImplementedError

    def env(self) -> dict[str, str]:
        return {}

    # -- lecture -----------------------------------------------------------
    def parse(self, line: str) -> dict:
        """Renvoie {session, display:[…], cost, final, usage, error} pour une ligne."""
        try:
            event = json.loads(line)
        except ValueError:
            return {}
        if not isinstance(event, dict):
            return {}
        return self._parse_event(event)

    def _parse_event(self, event: dict) -> dict:
        raise NotImplementedError


class ClaudeAdapter(HarnessAdapter):
    key = "claude"
    resume = "claude -p --resume"

    def command(self, text: str, session_id: str | None = None) -> list[str]:
        argv = [
            self.binary, "-p", "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            argv += ["--resume", session_id]
        argv.append(text)
        return argv

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}

    def _parse_event(self, event: dict) -> dict:
        kind = event.get("type")
        out: dict = {}
        if kind == "system" and event.get("subtype") == "init":
            out["session"] = event.get("session_id")
        elif kind == "assistant":
            texts = [
                chunk.get("text", "")
                for chunk in (event.get("message") or {}).get("content") or []
                if isinstance(chunk, dict) and chunk.get("type") == "text"
            ]
            if texts:
                out["display"] = texts
        elif kind == "result":
            out["final"] = event.get("result") or ""
            out["display"] = ["== fin du tour : %s" % (event.get("result") or "")[:300]]
            if isinstance(event.get("total_cost_usd"), (int, float)):
                out["cost"] = float(event["total_cost_usd"])
            if event.get("session_id"):
                out["session"] = event["session_id"]
            if event.get("is_error"):
                out["error"] = event.get("subtype") or "erreur du harnais"
        return out


class CodexAdapter(HarnessAdapter):
    key = "codex"
    resume = "codex exec resume"

    def command(self, text: str, session_id: str | None = None) -> list[str]:
        argv = [
            self.binary, "--dangerously-bypass-hook-trust", "exec",
            "--dangerously-bypass-approvals-and-sandbox", "--json",
        ]
        if session_id:
            argv += ["resume", session_id]
        argv.append(text)
        return argv

    def _parse_event(self, event: dict) -> dict:
        kind = event.get("type")
        out: dict = {}
        if kind == "thread.started":
            out["session"] = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                out["display"] = [item["text"]]
        elif kind == "turn.completed":
            out["display"] = ["== fin du tour"]
            if isinstance(event.get("usage"), dict):
                out["usage"] = event["usage"]
        elif kind in ("turn.failed", "error"):
            out["error"] = (event.get("error") or {}).get("message") if isinstance(
                event.get("error"), dict
            ) else event.get("message") or kind
        return out


class DeepseekAdapter(HarnessAdapter):
    key = "deepseek"
    resume = "dsh --session-id"

    def command(self, text: str, session_id: str | None = None) -> list[str]:
        argv = [self.binary, "--profile", "agent", "--json"]
        if session_id:
            argv += ["--session-id", session_id]
        argv.append(text)
        return argv

    def env(self) -> dict[str, str]:
        return {"DSH_PERMISSION_MODE": "danger-full-access"}

    def _parse_event(self, event: dict) -> dict:
        kind = event.get("type")
        out: dict = {}
        if kind == "session":
            out["session"] = event.get("sessionId") or event.get("session_id")
        elif kind == "text":
            if event.get("text"):
                out["display"] = [event["text"]]
        elif kind == "final":
            text = event.get("text") or ""
            out["final"] = text
            out["display"] = ["== fin du tour : %s" % text[:300]]
            if isinstance(event.get("cost_usd"), (int, float)):
                out["cost"] = float(event["cost_usd"])
        elif kind == "error":
            out["error"] = event.get("message") or "erreur du harnais"
        return out


ADAPTERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "deepseek": DeepseekAdapter,
}


def adapter_for(harness: str, binary: str | None = None) -> HarnessAdapter:
    cls = ADAPTERS.get(harness)
    if not cls:
        raise HarnessMissing("harnais inconnu : %r (connus : %s)" % (harness, ", ".join(HARNESSES)))
    return cls(binary)

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
from dataclasses import dataclass
from typing import Sequence

from .config import HARNESSES

MAIL_PROMPT = (
    "Tu as des messages agent-mail : lance agent-mail inbox, lis-les, "
    "puis continue ton travail sans attendre de confirmation."
)
EVENT_PROMPT = (
    "Des événements sont arrivés dans ta boîte agent-mail : lance agent-mail "
    "inbox, traite-les, puis continue ton travail sans attendre de confirmation."
)
#: message prioritaire : il préempte le tour en cours (0018, R19)
PRIORITY_PROMPT = (
    "Message prioritaire de %s :\n%s\n\n"
    "Traite-le en priorité, puis reprends ton travail sans attendre de confirmation."
)
#: rotation de session : résumé de reprise produit avant d'ouvrir une session neuve
SUMMARY_PROMPT = (
    "Résume cette session pour la reprendre dans une session neuve : état du lot, "
    "décisions prises, fichiers touchés, prochaine action. Sois concis et factuel, "
    "sans outils : ce résumé sera le seul contexte de la session suivante."
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


@dataclass(frozen=True)
class HarnessSpec:
    """Descripteur **déclaratif** d'un harnais (base du lot L16).

    `headless` et `interactive` sont des argv sans session ni texte : la session
    est insérée juste avant le texte avec `session_flag`, et disparaît quand
    elle est vide. `model_flags` / `effort_flags` reçoivent le modèle et
    l'effort choisis (0019) via `{model}` / `{effort}` ; un harnais qui n'est pas
    une simple ligne de commande (DeepSeek, patch YAML) surcharge `command()`.
    """
    key: str
    binary: str
    headless: tuple[str, ...]
    interactive: tuple[str, ...]
    session_flag: tuple[str, ...]
    model_flags: tuple[str, ...] = ()
    effort_flags: tuple[str, ...] = ()
    #: niveau de service du fournisseur (`ameesh set tier=…`, L26) via `{tier}` ;
    #: vide = le harnais n'en a pas, le réglage est sans effet
    tier_flags: tuple[str, ...] = ()


SPECS: dict[str, HarnessSpec] = {
    "claude": HarnessSpec(
        key="claude", binary="claude",
        headless=("-p", "--output-format", "stream-json", "--verbose",
                  "--dangerously-skip-permissions"),
        interactive=(),
        session_flag=("--resume",),
        model_flags=("--model", "{model}"),
        effort_flags=("--effort", "{effort}"),
    ),
    "codex": HarnessSpec(
        key="codex", binary="codex",
        headless=("--dangerously-bypass-hook-trust", "exec",
                  "--dangerously-bypass-approvals-and-sandbox", "--json"),
        interactive=(),
        session_flag=("resume",),
        model_flags=("-m", "{model}"),
        effort_flags=("-c", 'model_reasoning_effort="{effort}"'),
        tier_flags=("-c", 'service_tier="{tier}"'),
    ),
    "deepseek": HarnessSpec(
        key="deepseek", binary="dsh",
        headless=("--profile", "agent", "--json"),
        interactive=("--profile", "agent"),
        session_flag=("--session-id",),
    ),
}
#: nom de binaire par défaut, dérivé de la table (une seule source).
DEFAULT_BIN = {key: spec.binary for key, spec in SPECS.items()}


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
    """Un harnais = un descripteur de commande + un lecteur de flux JSONL."""

    key = ""
    resume = ""

    def __init__(self, binary: str | None = None, *, resolve: bool = True):
        spec = SPECS.get(self.key)
        if spec is None:
            raise HarnessMissing("harnais inconnu : %r (connus : %s)"
                                 % (self.key, ", ".join(sorted(SPECS))))
        self.spec = spec
        # `resolve=False` : lecteur seul, pour relire un flux déjà écrit. Le
        # binaire n'est pas exigé — il a pu disparaître depuis le tour.
        self.binary = resolve_binary(self.key, binary) if resolve else (binary or "")

    # -- commande ----------------------------------------------------------
    def _argv(self, base: tuple[str, ...], session_id: str | None,
              text: str | None = None, extra: Sequence[str] | None = None) -> list[str]:
        argv = [self.binary, *base]
        if extra:
            argv += list(extra)
        if session_id:
            argv += [*self.spec.session_flag, session_id]
        if text is not None:
            argv.append(text)
        return argv

    def _options(self, model: str | None, effort: str | None,
                 tier: str | None = None) -> list[str]:
        """Options modèle/effort/tier, déclarées par le descripteur (0019, L26)."""
        values = {"model": model or "", "effort": effort or "", "tier": tier or ""}
        out: list[str] = []
        if model:
            out += [flag.format(**values) for flag in self.spec.model_flags]
        if effort:
            out += [flag.format(**values) for flag in self.spec.effort_flags]
        if tier:
            out += [flag.format(**values) for flag in self.spec.tier_flags]
        return out

    def supports_tier(self) -> bool:
        """Le descripteur sait-il passer un tier au harnais ?"""
        return bool(self.spec.tier_flags)

    def command(self, text: str, session_id: str | None = None,
                model: str | None = None, effort: str | None = None,
                patch: str | None = None, tier: str | None = None) -> list[str]:
        """Ligne de commande d'un tour sans tête (le texte est la consigne).

        `model`, `effort` et `tier` sont appliqués s'ils sont fournis (un
        harnais sans `tier_flags` ignore le tier) ; `patch` et l'écriture du
        patch YAML sont propres à DeepSeek (voir la sous-classe).
        """
        return self._argv(self.spec.headless, session_id, text,
                          extra=self._options(model, effort, tier))

    def interactive_command(self, session_id: str | None = None) -> list[str]:
        """Commande d'une session **interactive** sur la même session (attach, C9).

        Elle ne passe ni par `-p`/`exec` ni par `--json` : le harnais garde son
        terminal et sa session, l'humain est devant.
        """
        return self._argv(self.spec.interactive, session_id)

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

    def env(self) -> dict[str, str]:
        return {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}

    def _parse_event(self, event: dict) -> dict:
        kind = event.get("type")
        out: dict = {}
        if kind == "system" and event.get("subtype") == "init":
            out["session"] = event.get("session_id")
            if event.get("model"):
                out["model"] = event["model"]  # modèle effectif annoncé (L13 B4)
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
            if isinstance(event.get("usage"), dict):
                out["usage"] = event["usage"]  # rotation de session (0018)
            if event.get("session_id"):
                out["session"] = event["session_id"]
            if event.get("is_error"):
                out["error"] = event.get("subtype") or "erreur du harnais"
        return out


class CodexAdapter(HarnessAdapter):
    key = "codex"
    resume = "codex exec resume"

    def _parse_event(self, event: dict) -> dict:
        kind = event.get("type")
        out: dict = {}
        if kind == "thread.started":
            out["session"] = event.get("thread_id")
            if event.get("model"):
                out["model"] = event["model"]  # modèle effectif annoncé (L13 B4)
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

    def env(self) -> dict[str, str]:
        return {"DSH_PERMISSION_MODE": "danger-full-access"}

    def command(self, text: str, session_id: str | None = None,
                model: str | None = None, effort: str | None = None,
                patch: str | None = None, tier: str | None = None) -> list[str]:
        """DeepSeek n'a pas d'options modèle/effort : il reçoit un patch YAML.

        Le fichier est écrit par l'exécuteur (il connaît l'état de l'agent) ; le
        patch n'est ajouté que si un modèle ou un effort est choisi.
        """
        extra: Sequence[str] = ()
        if patch and (model or effort):
            self._write_patch(patch, model, effort)
            extra = ("--patch", patch)
        return self._argv(self.spec.headless, session_id, text, extra=extra)

    @staticmethod
    def _write_patch(path: str, model: str | None, effort: str | None) -> None:
        body = (
            "- id: agent-default-model\n"
            '  name: "@deepseek-ai/dsh-agent-default-model"\n'
            "  config:\n"
            "    provider: deepseek-official\n"
            "    model: %s\n"
            "    reasoningEffort: %s\n" % (model or "deepseek-flash", effort or "max")
        )
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        except OSError:
            pass  # pas de patch : le harnais garde ses défauts, le tour part quand même

    def _parse_event(self, event: dict) -> dict:
        kind = event.get("type")
        out: dict = {}
        if kind == "session":
            out["session"] = event.get("sessionId") or event.get("session_id")
            if event.get("model"):
                out["model"] = event["model"]  # modèle effectif annoncé (L13 B4)
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


def adapter_for(harness: str, binary: str | None = None, *,
                resolve: bool = True) -> HarnessAdapter:
    cls = ADAPTERS.get(harness)
    if not cls:
        raise HarnessMissing("harnais inconnu : %r (connus : %s)" % (harness, ", ".join(HARNESSES)))
    return cls(binary, resolve=resolve)

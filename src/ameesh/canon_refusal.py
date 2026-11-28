# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh canon refuse` : un module enregistre un canon qu'il a refusé (#8340).

Constat (manaty/ameesh#6580) : un module consommateur valide le canon à un
commit épinglé et, quand ses propres contrôles le refusent, écrit un
`canon_state` `invalid` avec son motif en important
`canon_sync.record_state(...)`. Aucun chemin public ne permettait cette
écriture ; ce module en est un, sans import interne.

L'écriture reste gouvernée :

* elle vise l'hôte qui exécute la commande (`cfg.host`, `AMEESH_HOST`) ; il
  n'y a pas d'option `--host`, donc pas d'écriture pour un autre hôte ;
* elle vise un canon CONFIGURÉ (identifiant ou racine de `canons`), jamais un
  chemin arbitraire ;
* le motif est du texte lisible, borné, et un motif qui ressemble à un secret
  est REFUSÉ (jamais caviardé en silence puis stocké) ;
* le commit, quand il est donné, est un commit complet : il est gardé dans
  `canon_state.refused_commit`, jamais dans `last_good_commit` (qui ne suit
  qu'un contrôle `ok`) ;
* l'appel est idempotent : le même refus déjà enregistré n'est pas réécrit.

L'état `invalid` ferme la réclamation des agents de ce canon (le prédicat de
réclamation exige `ok`). Une synchronisation réussie ultérieure le remplace,
comme le prévoit le contrat public.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass

from . import activity
from . import canon as canon_mod
from . import canon_sync
from . import db as db_mod
from . import turn_context
from .config import Config

#: Longueur maximale du motif stocké (le diagnostic durable est plus large,
#: mais un motif de module n'a aucune raison de l'atteindre).
MAX_REASON = 1000
#: Préfixe stable du diagnostic : `canon check` montre ainsi l'origine du refus.
DIAGNOSTIC_PREFIX = "module refusal: "
#: Même forme que `canon confirm` / `work milestone` : un commit complet, jamais
#: un nom symbolique ni une abréviation.
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


@dataclass
class Refusal:
    ok: bool
    host: str = ""
    canon: str = ""
    canon_id: str = ""
    commit: str = ""
    status: str = ""
    diagnostic: str = ""
    written: bool = False
    checked_ts: float | None = None
    last_good_commit: str | None = None
    code: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _Selection(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return left == right
    return os.path.realpath(left) == os.path.realpath(right)


def _clean_reason(raw) -> tuple[str, str, str]:
    """(motif normalisé, code d'erreur, détail) — jamais le motif en erreur."""
    if not isinstance(raw, str):
        return "", "reason-unreadable", "the reason must be plain text"
    for char in raw:
        code = ord(char)
        if (code == 0x7f or (code < 0x20 and char not in "\n\t")
                or 0xD800 <= code <= 0xDFFF):
            return ("", "reason-unreadable",
                    "the reason contains a control character; pass one line of plain text")
    text = " ".join(raw.split())
    if not text:
        return "", "empty-reason", "the reason must not be empty"
    if len(text) > MAX_REASON:
        return ("", "reason-too-long",
                "the reason is longer than %d characters" % MAX_REASON)
    if activity.redact(text) != text:
        return ("", "reason-secret",
                "the reason looks like it contains a secret; it is refused, never stored")
    for value in turn_context.environment_secrets():
        if value and value in text:
            return ("", "reason-secret",
                    "the reason contains a value from a secret environment variable; "
                    "it is refused, never stored")
    return text, "", ""


def _choose_entry(cfg, wanted: str | None):
    """L'entrée de canon configurée visée, ou une `_Selection` refusée.

    Sans `wanted` : le canon par défaut (le premier configuré). Avec : une
    racine configurée (comparaison réelle) ou l'identifiant lu du canon. Un
    dossier non configuré n'est jamais accepté : la clé `canon_state` d'un
    canon par défaut ne doit pas être détournée vers une autre racine.
    """
    entries = canon_mod.configured(cfg)
    if not entries:
        raise _Selection("no-canon", "no canon is configured on this host")
    if not wanted:
        return entries[0]
    for entry in entries:
        if _same_path(str(getattr(entry, "path", "") or ""), wanted):
            return entry
    matches = []
    for entry in entries:
        try:
            candidate = canon_mod.from_config(cfg, entry=entry)
        except canon_mod.CanonError:
            continue
        if candidate.id == wanted:
            matches.append(entry)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _Selection("ambiguous-canon",
                         "several configured canons use the id %r; fix the configuration"
                         % wanted)
    raise _Selection("unknown-canon",
                     "%r is neither a configured canon root nor a configured canon id"
                     % wanted)


def _same_refusal(previous: dict | None, diagnostic: str, commit: str | None,
                  root: str) -> bool:
    if not previous or previous.get("status") != canon_sync.CANON_INVALID:
        return False
    if previous.get("diagnostic") != diagnostic:
        return False
    if (previous.get("refused_commit") or "") != (commit or ""):
        return False
    stored = str(previous.get("root") or "")
    return _same_path(stored, root) if stored and root else stored == root


def refuse(db, cfg, commit: str | None, reason: str, *,
           canon: str | None = None) -> Refusal:
    """Enregistre (ou confirme) le refus de `reason` pour le canon de l'hôte.

    `commit` : commit complet inspecté et refusé, ou None (refus non épinglé).
    `canon` : identifiant ou racine d'un canon CONFIGURÉ ; None : le canon par
    défaut. Ne lève pas pour un refus de gouvernance : rend `ok` faux, un
    `code` et un `detail` sans secret.
    """
    host = str(getattr(cfg, "host", "") or "").strip()
    result = Refusal(False, host=host)
    if not host:
        result.code = "no-host"
        result.detail = ("this host has no configured identity (AMEESH_HOST or `host`): "
                         "a canon state cannot be attributed")
        return result
    if commit is not None:
        commit = commit.strip().lower()
        result.commit = commit
        if not _COMMIT.fullmatch(commit):
            result.code = "invalid-commit"
            result.detail = "--commit must be a full commit (40 or 64 hex)"
            return result
    text, code, detail = _clean_reason(reason)
    if code:
        result.code, result.detail = code, detail
        return result
    diagnostic = DIAGNOSTIC_PREFIX + text
    result.diagnostic = diagnostic
    try:
        entry = _choose_entry(cfg, canon)
    except _Selection as exc:
        result.code, result.detail = exc.code, exc.detail
        return result
    try:
        loaded = canon_mod.from_config(cfg, entry=entry)
    except canon_mod.CanonError as exc:
        result.code = "canon-error"
        result.detail = " ".join(str(exc).split())[:300]
        return result
    key = canon_sync.state_key(db, loaded, host)
    result.canon = key
    result.canon_id = loaded.id
    previous = canon_sync.state(db, host, loaded)
    if _same_refusal(previous, diagnostic, commit, loaded.root):
        result.ok = True
        result.written = False
        result.status = str(previous.get("status") or "")
        result.checked_ts = previous.get("checked_ts")
        result.last_good_commit = previous.get("last_good_commit")
        return result
    canon_sync.record_state(db, host, canon_sync.CANON_INVALID, diagnostic, loaded,
                            key=key, refused_commit=commit)
    after = canon_sync.state(db, host, loaded) or {}
    if after.get("status") != canon_sync.CANON_INVALID:
        # Un contrôle concurrent a écrit un autre état : ne pas prétendre que
        # le refus a été enregistré.
        result.code = "changed"
        result.detail = ("another canon check wrote %r concurrently; retry"
                         % (after.get("status") or "no state"))
        return result
    result.ok = True
    result.written = True
    result.status = str(after.get("status") or "")
    result.checked_ts = after.get("checked_ts")
    result.last_good_commit = after.get("last_good_commit")
    return result


def cmd_refuse(cfg: Config, args: argparse.Namespace) -> int:
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        result = refuse(db, cfg, args.commit, args.reason, canon=args.canon)
    finally:
        db.close()
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif result.ok:
        pinned = " at %s" % result.commit[:12] if result.commit else ""
        print("canon %s refused on %s%s (%s)"
              % (result.canon or "default", result.host, pinned,
                 "recorded" if result.written else "already recorded"))
    else:
        print("canon refuse failed (%s): %s" % (result.code, result.detail),
              file=sys.stderr)
    return 0 if result.ok else 1


def add_parser(canon_sub) -> None:
    p = canon_sub.add_parser(
        "refuse", help="record a refused canon state with a bounded reason (#8340)")
    p.add_argument("--reason", required=True,
                   help="why the module refused the canon: one line, bounded, no secret")
    p.add_argument("--commit", default=None, metavar="SHA",
                   help="full commit the module inspected and refused; omitted, the "
                        "refusal is not pinned")
    p.add_argument("--canon", default=None,
                   help="id or root of a configured canon (otherwise the default canon)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_refuse)

# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh canon confirm --commit SHA` : canon inchangé, fraîcheur confirmée (#8354).

La porte d'admission (`placement.canon_fresh`) exige un `canon_state` `ok`,
un `last_good_commit` et un `checked_at` de moins de 300 s. Sans autre
moyen, un hôte qui veut rester dans la fenêtre réapplique tout le canon
(chargement, validation, registre) même quand le commit n'a pas bougé
(manaty/ameesh#6596 : ~20 s de CPU par passe).

La confirmation ne charge ni ne valide aucune fiche. Elle prouve, à coût
constant (une lecture, un `git rev-parse` local, une écriture), que :

* l'état enregistré de ce canon sur l'hôte est `ok` au commit `SHA` donné ;
* sa racine est encore un canon configuré de l'hôte, approuvé, et la
  révision appliquée (enregistrée dans `source`) — ou la révision configurée —
  désigne toujours ce commit dans le dépôt local (aucun fetch : l'appelant
  qui veut voir l'amont le récupère lui-même, une avance le fait refuser) ;
* la dernière application complète date de moins de `MAX_SPAN_S` : une
  confirmation ne remplace jamais indéfiniment la synchro complète.

Puis seul `checked_at` avance, par une écriture conditionnelle sur la ligne
`ok` attendue (`Canon.confirm_fresh`) : aucun nouvel état d'autorité ; une
invalidation concurrente au même commit garde la porte fermée. Un refus
n'écrit rien : l'appelant lance alors `ameesh canon sync`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass

from . import canon as canon_mod
from . import db as db_mod
from . import storage
from .config import Config

#: Ancienneté maximale de la dernière application complète qu'une
#: confirmation peut prolonger. Au-delà, `canon sync` est exigé : il revoit
#: les sources sœurs, le registre et la version d'ameesh qui applique.
MAX_SPAN_S = 3600.0

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
#: `Source.describe()` d'une source git : « membre @ commit12 (révision) ».
_GIT_SOURCE = re.compile(r"(?P<member>.+?) @ (?P<short>[0-9a-f]+) \((?P<rev>.+)\)\Z")


@dataclass
class Confirmation:
    ok: bool
    host: str
    canon: str
    commit: str
    code: str = ""
    reason: str = ""
    checked_ts: float | None = None
    last_good_ts: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _same_path(left: str, right: str) -> bool:
    return bool(left) and bool(right) and os.path.realpath(left) == os.path.realpath(right)


def _row(store, host: str, wanted: str | None) -> tuple[dict | None, str]:
    """La ligne visée et sa clé : le canon par défaut ('') sans `wanted` ;
    sinon la clé `wanted`, ou la ligne par défaut dont `canon_id` vaut
    `wanted` (le canon par défaut nommé par son identifiant)."""
    if not wanted:
        return store.state(host, ""), ""
    row = store.state(host, wanted)
    if row is not None:
        return row, wanted
    row = store.state(host, "")
    if row is not None and row.get("canon_id") == wanted:
        return row, ""
    return None, wanted


def _entry(cfg, key: str, root: str):
    """L'entrée configurée de la racine enregistrée (None : plus configurée).
    La ligne par défaut ('') n'appartient qu'à la première entrée."""
    entries = canon_mod.configured(cfg)
    if not key:
        return entries[0] if entries and _same_path(entries[0].path, root) else None
    for entry in entries[1:]:
        if _same_path(entry.path, root):
            return entry
    return None


def confirm(db, cfg, host: str, commit: str, *, canon: str | None = None,
            max_span_s: float = MAX_SPAN_S) -> Confirmation:
    """Confirme que le canon appliqué sur `host` est toujours `commit` (#8354)."""
    commit = (commit or "").strip().lower()
    result = Confirmation(False, host, canon or "", commit)

    def refuse(code: str, reason: str) -> Confirmation:
        result.code, result.reason = code, reason
        return result

    if not _COMMIT.fullmatch(commit):
        return refuse("invalid-commit", "--commit must be a full commit (40 or 64 hex)")
    store = storage.of(db).canon
    row, key = _row(store, host, canon)
    result.canon = key
    if row is None:
        return refuse("no-state", "no canon state recorded on %s for this canon" % host)
    if row.get("status") != "ok":
        return refuse("not-ok", "canon state is %r: a full `ameesh canon sync` is required"
                      % (row.get("status") or "unknown"))
    applied = str(row.get("last_good_commit") or "").strip().lower()
    if applied != commit:
        return refuse("commit-mismatch",
                      "applied canon commit is %s, not %s: run `ameesh canon sync`"
                      % (applied[:12] or "none", commit[:12]))
    root = str(row.get("root") or "")
    entry = _entry(cfg, key, root)
    if entry is None:
        return refuse("not-configured",
                      "recorded root %s is not the configured canon of this key" % (root or "?"))
    if entry.untrusted:
        return refuse("untrusted", "an untrusted canon is never confirmed")
    first = str(row.get("source") or "").split("; ")[0]
    match = _GIT_SOURCE.fullmatch(first)
    if match is None or not commit.startswith(match["short"]):
        return refuse("source-unknown",
                      "recorded source %r does not name a git revision at this commit" % first)
    rev = entry.ref or match["rev"]
    if entry.ref and entry.ref != match["rev"]:
        return refuse("ref-changed", "configured ref %s differs from the applied ref %s"
                      % (entry.ref, match["rev"]))
    try:
        current = canon_mod.git_commit(root, rev)
    except canon_mod.GitError as exc:
        return refuse("git-error", "resolving %s: %s" % (rev, " ".join(str(exc).split())[:200]))
    if current != commit:
        return refuse("source-moved", "%s now resolves to %s: run `ameesh canon sync`"
                      % (rev, (current or "nothing")[:12]))
    done = store.confirm_fresh(host, key, commit=commit, root=root, max_span_s=max_span_s)
    if done is None:
        # La ligne a changé depuis la lecture, ou la dernière application
        # complète est trop ancienne : on relit seulement pour le dire.
        again = store.state(host, key) or {}
        if again.get("status") != "ok" or again.get("last_good_commit") != commit:
            return refuse("changed", "canon state changed concurrently (%s at %s)"
                          % (again.get("status") or "absent",
                             str(again.get("last_good_commit") or "none")[:12]))
        return refuse("too-old", "last full apply is older than %ds: run `ameesh canon sync`"
                      % int(max_span_s))
    result.ok = True
    result.checked_ts = done.get("checked_ts")
    result.last_good_ts = done.get("last_good_ts")
    return result


def cmd_confirm(cfg: Config, args: argparse.Namespace) -> int:
    host = args.host or cfg.host
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        result = confirm(db, cfg, host, args.commit, canon=args.canon)
    finally:
        db.close()
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif result.ok:
        print("canon confirmed on %s at %s (freshness refreshed, no reapply)"
              % (host, result.commit[:12]))
    else:
        print("canon confirm refused (%s): %s" % (result.code, result.reason), file=sys.stderr)
    return 0 if result.ok else 1


def add_parser(canon_sub) -> None:
    p = canon_sub.add_parser(
        "confirm", help="unchanged canon: refresh freshness without reapplying (#8354)")
    p.add_argument("--commit", required=True, metavar="SHA",
                   help="full commit the caller expects to be the applied canon")
    p.add_argument("--canon", default=None,
                   help="id of a configured canon (otherwise the default canon)")
    p.add_argument("--host", default=None, help="host (otherwise AMEESH_HOST / this machine)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_confirm)

# SPDX-License-Identifier: AGPL-3.0-only
"""Demandes d'humains sans lot (lot L130, « toute demande du propriétaire
devient un lot »).

  ameesh work unrecorded [--since 24h] [--json] [--limit N]

Constat du 2026-10-11 : depuis le 07/10, le propriétaire avait fait 46
demandes ; 27 n'avaient eu aucun lot (15 pourtant livrées, sans trace) et une
promesse avait été oubliée. Règle (docs/ORCHESTRATEUR.md, consigne du chat) :
toute demande d'un humain est enregistrée sur-le-champ comme lot, avec sa
phrase exacte en source, une priorité et une estimation ; la réponse à
l'humain cite le numéro du lot.

Ce contrôle, en LECTURE SEULE, liste les messages d'humains de la période qui
ne sont rattachés à aucun lot et n'en citent aucun :

* le courrier dont l'expéditeur est humain : `human:<id>`, marqué humain à
  l'envoi (`payload.human`, L125), ou nom déclaré humain (`AMEESH_HUMANS`) ;
* les réponses aux demandes de décision (L124 : courrier `reply` de
  `human:<id>`, rattaché au lot de la demande) ;
* les demandes que le chat du propriétaire transmet (`chat-<humain>`, L123).

Un message est **enregistré** quand :

1. il est rattaché à un lot existant (`--lot`, `--new-lot`, lot de la
   décision) ;
2. il cite un lot existant : `L130`, `lot 130`, `lot #130`, `ameesh-work: 130`
   (un `#130` seul ne compte pas : c'est souvent une PR ou une issue) ;
3. un lot créé depuis le début de la période (moins un jour) reprend sa
   phrase : un passage entre guillemets de la source ou du corps du lot, ou sa
   source entière, se retrouve dans le message ; ou le message entier se
   retrouve dans la source ou le corps du lot. Casse, espaces et guillemets
   ne comptent pas ; 12 caractères au moins.

Ne sont pas examinés : les accusés de réception (`--ack`, ou reconnus par la
règle prudente de L125), les copies (`--cc` : le message principal suffit) et
les demandes de décision elles-mêmes (d'un agent vers un humain). Un même
texte du même expéditeur envoyé à plusieurs destinataires (diffusion) compte
une fois.

Limite : ce qu'un humain dit directement dans une session (le chat, la
session attachée d'un orchestrateur ou d'un agent de conception) n'est pas en
base ; seul l'agent qui l'entend peut l'enregistrer, c'est la consigne. Le
contrôle voit tout le courrier, et ce que le chat transmet.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata

from . import mail, storage

SCHEMA = "ameesh-unrecorded/1"
DEFAULT_SINCE = "24h"
DEFAULT_LIMIT = 1000
#: un passage repris doit avoir au moins cette longueur (normalisé)
MIN_MATCH = 12
#: lots examinés pour la reprise d'une phrase : créés depuis le début de la
#: période moins ce délai (un lot enregistré juste avant le message compte)
LOT_LOOKBACK_S = 86400.0
#: préfixe du chat du propriétaire (`chat.CHAT_PREFIX`)
CHAT_PREFIX = "chat-"

#: une citation de lot : L130, lot 130, lot #130, lot n° 130, ameesh-work: 130
_CITE_RE = re.compile(
    r"(?<![\w-])(?:L|lot\s*(?:n°\s*|no\.?\s*|#\s*)?|ameesh-work:\s*)(\d{1,9})(?!\w)",
    re.IGNORECASE)
#: un passage entre guillemets (français, anglais, droits)
_QUOTE_RE = re.compile(r"«\s*(.+?)\s*»|“(.+?)”|\"(.+?)\"", re.DOTALL)
#: guillemets et espaces insécables, neutralisés avant comparaison
_FOLD = str.maketrans({"«": " ", "»": " ", "“": " ", "”": " ", "\"": " ", "„": " ",
                       "’": "'", "‘": "'", "\u00a0": " ", "\u202f": " "})
#: ponctuation retirée aux deux bouts d'un passage
_EDGES = " .,;:!?…-–—'()[]"


class UnrecordedError(ValueError):
    """Option refusée (période illisible)."""


# --------------------------------------------------------------------------
# texte
# --------------------------------------------------------------------------

def normalize(text) -> str:
    """Le texte à comparer : casse repliée, guillemets et espaces neutralisés,
    ponctuation des bouts retirée."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold().translate(_FOLD)
    return " ".join(folded.split()).strip(_EDGES)


def cited_lots(text) -> set[int]:
    """Les numéros de lot que cite un texte (`L130`, `lot #130`…)."""
    return {int(m.group(1)) for m in _CITE_RE.finditer(str(text or ""))}


def quotes(text) -> list[str]:
    """Les passages entre guillemets d'un texte, normalisés, assez longs."""
    out = []
    for match in _QUOTE_RE.finditer(str(text or "")):
        passage = normalize(next(g for g in match.groups() if g is not None))
        if len(passage) >= MIN_MATCH:
            out.append(passage)
    return out


def quoted_by(message: str, lot: dict) -> bool:
    """Le lot reprend-il la phrase de ce message (déjà normalisé) ?"""
    if len(message) < MIN_MATCH:
        return False
    source, body = str(lot.get("source") or ""), str(lot.get("body") or "")
    passages = quotes(source) + quotes(body)
    whole = normalize(source)
    if len(whole) >= MIN_MATCH:
        passages.append(whole)
    if any(passage in message for passage in passages):
        return True
    return message in normalize(source + "\n" + body)


def _payload(row: dict) -> dict:
    payload = mail.message_payload(row)
    return payload if isinstance(payload, dict) else {}


def channel(row: dict) -> str:
    """`decision` (réponse à une demande de décision), `chat` (transmis par
    le chat du propriétaire) ou `mail`."""
    if _payload(row).get("decision_answer"):
        return "decision"
    if str(row.get("sender") or "").startswith(CHAT_PREFIX):
        return "chat"
    return "mail"


# --------------------------------------------------------------------------
# le contrôle
# --------------------------------------------------------------------------

def scan(cfg, db, *, since_ts: float, now: float | None = None,
         limit: int = DEFAULT_LIMIT) -> dict:
    """Les messages d'humains depuis `since_ts`, enregistrés ou non (schéma
    `ameesh-unrecorded/1`). Lecture seule : deux requêtes."""
    now = time.time() if now is None else float(now)
    rows = storage.of(db).mailbox.human_messages(
        since_ts=float(since_ts), humans=sorted(getattr(cfg, "human_names", ()) or ()),
        limit=int(limit))
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        payload = _payload(row)
        if payload.get("ack") or payload.get("cc") or mail.looks_like_ack(row.get("body") or ""):
            continue
        groups.setdefault((row.get("sender"), normalize(row.get("body"))), []).append(row)
    cited: set[int] = set()
    for group in groups.values():
        cited |= cited_lots(group[0].get("body"))
    lots = storage.of(db).work.recent_or_cited(
        since_ts=float(since_ts) - LOT_LOOKBACK_S, ids=sorted(cited)) if groups else []
    existing = {int(lot["id"]) for lot in lots}
    recent = [lot for lot in lots
              if float(lot.get("created_ts") or 0.0) >= float(since_ts) - LOT_LOOKBACK_S]
    recorded, unrecorded = [], []
    for (sender, text), group in groups.items():
        first = group[0]
        entry = {
            "id": int(first["id"]),
            "ids": [int(r["id"]) for r in group],
            "created_ts": round(float(first.get("created_ts") or 0.0), 3),
            "sender": sender,
            "recipients": sorted({str(r.get("recipient")) for r in group}),
            "channel": channel(first),
            "kind": first.get("kind"),
            "text": first.get("body") or "",
        }
        how = _recorded(group, text, cited_lots(first.get("body")) & existing, recent)
        if how:
            entry["how"], entry["lot"] = how
            recorded.append(entry)
        else:
            unrecorded.append(entry)
    return {"schema": SCHEMA, "since_ts": round(float(since_ts), 3), "now_ts": round(now, 3),
            "examined": len(groups), "recorded": recorded, "unrecorded": unrecorded}


def _recorded(group: list[dict], text: str, cited: set[int], recent: list[dict]):
    """(comment, lot) si le message est enregistré, sinon None."""
    for row in group:
        if row.get("lot_id") is not None:
            return "attached", int(row["lot_id"])
    if cited:
        return "cited", min(cited)
    for lot in recent:
        if quoted_by(text, lot):
            return "source", int(lot["id"])
    return None


# --------------------------------------------------------------------------
# commande `ameesh work unrecorded`
# --------------------------------------------------------------------------

CHANNEL_LABELS = {"mail": "courrier", "decision": "décision", "chat": "chat"}
#: comment enregistrer, rappelé sous la liste
HINT = ("Pour chacune : enregistrer le lot, phrase exacte en source — ameesh work add "
        "--title \"RÉF : titre\" --source \"<qui> : « <phrase exacte> »\" --priority 1|2|3 "
        "[--assignee <agent>] —, puis citer son numéro dans la réponse à l'humain "
        "(docs/ORCHESTRATEUR.md, « Toute demande d'un humain devient un lot »).")


def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "unrecorded",
        help="demandes d'humains (courrier, réponses aux décisions, chat) sans lot (L130)")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="période : durée (2h, 24h, 7d) ou date ISO (défaut %s)"
                             % DEFAULT_SINCE)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="messages examinés au plus, les plus récents (défaut %d)"
                             % DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def since_ts(text: str | None, now: float) -> float:
    from . import progress
    try:
        return progress.parse_since(text or DEFAULT_SINCE, now)
    except ValueError as exc:
        raise UnrecordedError(str(exc)) from None


def _moment(ts) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts or 0.0)))


def _excerpt(text: str, limit: int = 140) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def render(report: dict, since_text: str) -> list[str]:
    examined, missing = report["examined"], report["unrecorded"]
    if not missing:
        return ["Aucune demande d'humain sans lot depuis %s (%d message(s) examiné(s)%s)."
                % (since_text, examined, ", tous rattachés à un lot" if examined else "")]
    lines = ["Demandes d'humains sans lot depuis %s : %d sur %d message(s) examiné(s)."
             % (since_text, len(missing), examined)]
    for entry in missing:
        recipients = entry["recipients"]
        lines.append("  n°%-7d %s  %s → %s  [%s]  « %s »" % (
            entry["id"], _moment(entry["created_ts"]), entry["sender"],
            recipients[0] + (" (+%d)" % (len(recipients) - 1) if len(recipients) > 1 else ""),
            CHANNEL_LABELS.get(entry["channel"], entry["channel"]), _excerpt(entry["text"])))
    lines.append(HINT)
    return lines


def run(cfg, db, args: argparse.Namespace) -> int:
    now = time.time()
    try:
        start = since_ts(args.since, now)
    except UnrecordedError as exc:
        print("ameesh work unrecorded : %s" % exc, file=sys.stderr)
        return 2
    if args.limit < 1:
        print("ameesh work unrecorded : --limit doit être positif", file=sys.stderr)
        return 2
    report = scan(cfg, db, since_ts=start, now=now, limit=args.limit)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print("\n".join(render(report, args.since or DEFAULT_SINCE)))
    return 0

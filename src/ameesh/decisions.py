# SPDX-License-Identifier: AGPL-3.0-only
"""Demandes de décision au propriétaire, reçues et répondues au même endroit (lot L124).

  ameesh decide ask (--lot <id|réf> | --new-lot "titre") --question "…"
                    --option a="…" [--option b="…" …] [--recommend a [--why "…"]]
                    [--urgent] [--by 2h|18:00|"2026-10-12 09:00"] [--needs-signature] [--json]
        un agent dépose une demande : rattachée au projet et au lot, le lot
        passe en `waiting_human` (il revient à son état précédent à la
        réponse), le propriétaire visé est notifié ;
  ameesh decide <id> <option | "texte libre"> [--key FICHIER [--as NOM] | --signed] [--json]
        le propriétaire répond (une identité HUMAINE seulement) ; la réponse
        part au demandeur par courrier (elle le réveille), au fil et au lot ;
  ameesh decide withdraw <id> [--why "…"] [--json]     le demandeur retire sa demande ;
  ameesh decide show <id> [--json]                     une demande, sa réponse, sa preuve ;
  ameesh decisions [--all] [--for human:ID] [--json]   la file, tous projets confondus
        (servie par `actions_cli`, avec les actions sous porte).

Constat du 2026-10-10 : les orchestrateurs attendaient des décisions du
propriétaire pendant 2 h 43 à 4 h 08 ; aucune demande ne lui était adressée,
elles partaient dans le courrier d'autres agents (parfois une boîte morte),
et il n'était joignable qu'en s'attachant à la session d'un orchestrateur.
Désormais une demande a un destinataire humain, une file, une notification,
une relance à l'échéance, et une réponse tracée.

**Destinataire** (premier trouvé) : le responsable de la fiche du plan du lot
(en remontant les parents), le responsable de l'agent demandeur, celui de
l'assigné du lot, `notify.default_human`, le seul humain déclaré
(`humans`) ; sinon tout humain du mesh (`human:*`).

**Données**, sans migration : une demande est une ligne de `agent_mailbox`
(`kind = 'request'`, destinataire `human:<id>`, objet `payload.decision`),
« non remise » tant qu'elle attend ; la réponse la clôt en une écriture
conditionnelle (un seul gagnant). Les transitions du lot sont dans
`work_item_events`. La réponse enregistre l'identité, le canal (`cli`,
`chat`, `signed`) et le texte exact.

**Identité de qui répond** : un humain, `human:<utilisateur Unix>`. Refusé :
une session d'agent (`AGENT_MAIL_NAME`, bail d'exécuteur, session liée par
`ameesh mail bind`), et une commande dont un processus ANCÊTRE porte une
identité d'agent (une variable retirée par l'agent reste visible dans
l'environnement initial de ses ancêtres). Exception : la session
`ameesh chat` du propriétaire (L123), vérifiée par son processus ancêtre
enregistré — canal `chat`. C'est une garde contre la confusion, pas une
frontière de sécurité (agents et humain partagent l'utilisateur Unix) : la
frontière, pour les gestes en production ou irréversibles, est la réponse
SIGNÉE (`--needs-signature`) par la clé du propriétaire, avec le mécanisme
d'`ameesh approve` (`mesh_approvals`, consommation unique).
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import re
import sys
import time

from . import config as config_mod
from . import db as db_mod
from . import fil, identity, platform, registry, storage, work
from .config import NAME_RE, Config

#: schéma du JSON d'une demande (`--json`, clé `requests` de `ameesh decisions`)
SCHEMA = "ameesh-decision/1"
#: clé de l'objet décision dans `agent_mailbox.payload`
PAYLOAD_KEY = "decision"
#: nature de la ligne de demande et du courrier de réponse (`agent_mailbox.kind`)
KIND = "request"
REPLY_KIND = "reply"
STATES = ("pending", "answered", "withdrawn")
#: canaux d'une réponse (`signed` : signée par la clé du propriétaire)
CHANNELS = ("cli", "chat", "signed")
CHANNEL_LABELS = {"cli": "cli", "chat": "chat", "signed": "signé"}
#: destinataire d'une demande sans responsable connu : tout humain du mesh
OWNER_ANY = "human:*"
#: échéance par défaut d'une demande (relance du propriétaire passé ce délai)
DEFAULT_DUE_S = 4 * 3600.0
#: action et nature des approbations signées qui portent une réponse
APPROVAL_ACTION = "decision"
APPROVAL_KIND = "decision"
#: durée de validité d'une approbation signée par `decide --key`
SIGN_TTL_S = 3600.0
OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")
MAX_OPTIONS = 12
MAX_QUESTION = 4000
MAX_LABEL = 600
MAX_ANSWER = 4000
#: bornes de lecture (une file, pas un export)
MAX_PENDING = 500
HISTORY_LIMIT = 50

#: variables posées par `ameesh chat` pour la session du harnais (L123)
CHAT_ENV = "AMEESH_CHAT"
CHAT_PID_ENV = "AMEESH_CHAT_PID"
#: variables d'un exécuteur (bail) : une commande qui les porte, ou dont un
#: ancêtre les porte, est celle d'un agent mené
RUNNER_ENV = ("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID")
LEASE_ENV = ("AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH")

_TIME_RE = re.compile(r"^\s*(\d{1,2})[:h](\d{2})\s*$")


class DecisionError(RuntimeError):
    """Demande ou réponse refusée — toujours avec ce qu'il faut faire."""


# --------------------------------------------------------------------------
# identité de qui répond
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Answerer:
    """Qui répond : un humain, par la ligne de commande ou par son chat."""

    human: str
    channel: str            # cli | chat
    chat: str | None = None


def unix_user() -> str:
    return os.environ.get("USER") or getpass.getuser()


def current_human() -> str:
    """L'humain d'une commande lancée hors de toute session d'agent :
    `human:<utilisateur Unix>` (même règle que `ameesh budget`)."""
    return "human:%s" % unix_user()


def agent_in_ancestry(*, allow: str | None = None, chain=None, environ_of=None) -> str | None:
    """Le premier processus de l'ascendance (celui-ci compris) dont
    l'environnement INITIAL porte une identité d'agent : bail d'exécuteur
    (`AMEESH_RUNNER_ID`) ou `AGENT_MAIL_NAME` (autre que `allow`, l'identité
    du chat vérifié). Rend une ligne de diagnostic, ou None.

    Un ancêtre illisible (disparu, autre utilisateur) est ignoré ; un OS qui
    ne dit ni l'ascendance ni l'environnement ne trouve rien (garde contre la
    confusion, la frontière est la réponse signée)."""
    try:
        pids = list(platform.ancestry() if chain is None else chain)
    except platform.NotAvailable:
        return None
    read = environ_of or platform.environ
    for pid in pids:
        try:
            env = read(pid)
        except platform.NotAvailable:
            return None
        if not env:
            continue
        name = (env.get("AGENT_MAIL_NAME") or "").strip()
        runner = next((env[key] for key in RUNNER_ENV if env.get(key)), "")
        if runner:
            return "processus %s : agent %s mené par %s" % (pid, name or "?", runner)
        if name and name != allow:
            return "processus %s : agent %s" % (pid, name)
    return None


def agent_session_reason(cfg: Config, db, *, chain=None, environ_of=None) -> str | None:
    """Pourquoi cette session est celle d'un agent ; None si c'est celle d'un humain.

    `AGENT_MAIL_NAME` posée (un agent, ou un chat dont la session n'est pas
    vérifiée), bail d'exécuteur dans l'environnement, session liée à un agent
    (`ameesh mail bind`), ancêtre qui porte une identité d'agent."""
    name = (os.environ.get("AGENT_MAIL_NAME") or "").strip()
    if name:
        return "cette session est celle de l'agent %s (AGENT_MAIL_NAME)" % name
    runner = next((os.environ[key] for key in RUNNER_ENV if os.environ.get(key)), "")
    if runner:
        return "cette session est menée par l'exécuteur %s" % runner
    binding = identity.resolve_binding(cfg, db)
    if binding.name:
        return "cette session est liée à l'agent %s (ameesh mail bind)" % binding.name
    found = agent_in_ancestry(chain=chain, environ_of=environ_of)
    if found:
        return "cette commande est lancée depuis la session d'un agent (%s)" % found
    return None


def human_session(cfg: Config, db, *, chain=None, environ_of=None) -> str:
    """L'humain de cette session (`human:<utilisateur>`), ou DecisionError si
    c'est celle d'un agent (`agent_session_reason`)."""
    reason = agent_session_reason(cfg, db, chain=chain, environ_of=environ_of)
    if reason:
        raise DecisionError("refus : %s ; seul un humain répond à une demande de décision — "
                            "réponds depuis ton propre terminal, ou dans `ameesh chat`" % reason)
    return current_human()


def chat_session(cfg: Config, *, chain=None, environ_of=None) -> Answerer | None:
    """La session `ameesh chat` du propriétaire (L123), vérifiée ; None hors
    chat. DecisionError si la session se dit chat sans l'être."""
    name = (os.environ.get(CHAT_ENV) or "").strip()
    if not name:
        return None
    if (os.environ.get("AGENT_MAIL_NAME") or "").strip() != name \
            or any(os.environ.get(key) for key in RUNNER_ENV + LEASE_ENV):
        raise DecisionError("refus : session de chat incohérente (identité ou bail d'exécuteur) "
                            "— relance `ameesh chat` depuis ton terminal")
    from . import chat as chat_mod
    record = chat_mod.verify_session(cfg, name, os.environ.get(CHAT_PID_ENV), chain=chain)
    found = agent_in_ancestry(allow=name, chain=chain, environ_of=environ_of)
    if found:
        raise DecisionError("refus : cette commande descend de la session d'un agent (%s), "
                            "pas seulement du chat %s" % (found, name))
    human = str(record.get("human") or "")
    if human != current_human():
        raise DecisionError("refus : le chat %s est celui de %s, pas de %s"
                            % (name, human or "?", current_human()))
    return Answerer(human, "chat", name)


def answering_human(cfg: Config, db, *, chain=None, environ_of=None) -> Answerer:
    """Qui répond : le chat vérifié du propriétaire, sinon l'humain du terminal."""
    chat = chat_session(cfg, chain=chain, environ_of=environ_of)
    if chat is not None:
        return chat
    return Answerer(human_session(cfg, db, chain=chain, environ_of=environ_of), "cli")


# --------------------------------------------------------------------------
# lecture
# --------------------------------------------------------------------------

def _payload(row: dict) -> dict:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return dict(payload.get(PAYLOAD_KEY) or {})


def view_of(row: dict, now: float | None = None) -> dict:
    """La demande d'une ligne de `agent_mailbox` (schéma `ameesh-decision/1`)."""
    now = time.time() if now is None else float(now)
    d = _payload(row)
    created = float(row.get("created_ts") or 0.0)
    closed = row.get("closed_ts")
    state = d.get("state") or ("pending" if closed is None else "answered")
    due = d.get("due_ts")
    due = float(due) if due is not None else None
    lot = d.get("lot")
    owner = row.get("recipient") or OWNER_ANY
    return {
        "schema": SCHEMA,
        "id": int(row["id"]),
        "state": state,
        "project": d.get("project"),
        "lot": int(lot) if str(lot if lot is not None else "").isdigit() else lot,
        "lot_title": d.get("lot_title"),
        "requester": row.get("sender"),
        "owner": None if owner == OWNER_ANY else owner,
        "owner_source": d.get("owner_source"),
        "question": d.get("question") or "",
        "options": [dict(o) for o in d.get("options") or []],
        "recommend": d.get("recommend"),
        "why": d.get("why"),
        "urgent": bool(d.get("urgent")),
        "needs_signature": bool(d.get("needs_signature")),
        "due_ts": due,
        "overdue": state == "pending" and due is not None and now >= due,
        "created_ts": round(created, 3),
        "closed_ts": round(float(closed), 3) if closed is not None else None,
        "age_s": int(max(0.0, now - created)),
        "previous_state": d.get("previous_state"),
        "host": row.get("host"),
        "answer": d.get("answer"),
        "withdrawn": d.get("withdrawn"),
    }


def get(db, decision_id: int, now: float | None = None) -> dict | None:
    row = storage.of(db).decisions.get(int(decision_id))
    return view_of(row, now) if row else None


def pending(db, now: float | None = None, *, human: str | None = None) -> list[dict]:
    """Les demandes en attente, les plus anciennes d'abord ; `human` : celles
    qui lui sont adressées, et celles sans destinataire précis."""
    rows = [view_of(r, now) for r in storage.of(db).decisions.pending(limit=MAX_PENDING)]
    if human:
        rows = [r for r in rows if r["owner"] in (None, human)]
    return rows


def history(db, now: float | None = None, *, human: str | None = None,
            limit: int = HISTORY_LIMIT) -> list[dict]:
    """Les demandes closes (répondues, retirées), les plus récentes d'abord."""
    rows = [view_of(r, now) for r in storage.of(db).decisions.history(limit=limit)]
    if human:
        rows = [r for r in rows if r["owner"] in (None, human)]
    return rows


# --------------------------------------------------------------------------
# destinataire
# --------------------------------------------------------------------------

def _human(value) -> str | None:
    from .notify import normalize_human
    return normalize_human(value)


def resolve_owner(cfg: Config, db, requester_row: dict | None, item: dict
                  ) -> tuple[str | None, str | None]:
    """(humain visé, source) : `lot` (fiche du plan), `agent` (demandeur),
    `assigne` (assigné du lot), `defaut` (`notify.default_human`),
    `humain_unique` (seul humain déclaré) ; (None, None) sinon."""
    packages = storage.of(db).packages
    ident = item.get("package_id") or item.get("package_parent")
    seen: set = set()
    while ident and ident not in seen and len(seen) < 16:
        seen.add(ident)
        package = packages.get(ident) or {}
        human = _human(package.get("responsible"))
        if human:
            return human, "lot"
        ident = package.get("parent")
    human = _human((requester_row or {}).get("responsible"))
    if human:
        return human, "agent"
    assignee = (item.get("assignee") or "").strip()
    if assignee.startswith(work.HUMAN_PREFIX):
        human = _human(assignee)
        if human:
            return human, "assigne"
    elif assignee and assignee != (requester_row or {}).get("name"):
        human = _human((registry.get(db, assignee) or {}).get("responsible"))
        if human:
            return human, "assigne"
    try:
        from .notify import NotifyConfigError, parse_config
        default = parse_config(cfg.notify).default_human
    except (NotifyConfigError, ValueError, TypeError):
        default = None
    if default:
        return default, "defaut"
    names = sorted(cfg.human_names)
    if len(names) == 1:
        human = _human(names[0])
        if human:
            return human, "humain_unique"
    return None, None


def _project(db, item: dict, requester_row: dict | None) -> str | None:
    """Le projet affiché d'une demande : celui du lot (`app`, `workstream`,
    équipe de sa fiche), sinon celui du demandeur (équipe, chantier)."""
    def text(value) -> str:
        return " ".join(str(value or "").split())
    name = text(item.get("app")) or text(item.get("workstream"))
    if not name and item.get("package_id"):
        name = text((storage.of(db).packages.get(item["package_id"]) or {}).get("team"))
    if not name:
        row = requester_row or {}
        name = text(row.get("team")) or text(row.get("chantier"))
    return name or None


# --------------------------------------------------------------------------
# demande
# --------------------------------------------------------------------------

def parse_options(values) -> list[dict]:
    """`["a=Fusionner", "b=Attendre"]` → `[{key, label}]` ; DecisionError sinon."""
    out: list[dict] = []
    for value in values or ():
        key, sep, label = str(value).partition("=")
        key, label = key.strip(), " ".join(label.split())
        if not sep or not OPTION_KEY_RE.match(key) or not label:
            raise DecisionError("option illisible : %r (attendu --option a=\"libellé\", clé de "
                                "lettres ou de chiffres, 16 caractères au plus)" % (value,))
        if any(o["key"].lower() == key.lower() for o in out):
            raise DecisionError("option %s en double" % key)
        if len(label) > MAX_LABEL:
            raise DecisionError("option %s : libellé trop long (%d caractères au plus)"
                                % (key, MAX_LABEL))
        reason = fil.unreadable_reason(label)
        if reason:
            raise DecisionError("option %s illisible — %s" % (key, reason))
        out.append({"key": key, "label": label})
    if len(out) > MAX_OPTIONS:
        raise DecisionError("%d options au plus" % MAX_OPTIONS)
    return out


def parse_due(text: str, now: float | None = None) -> float:
    """`--by` : une durée (« 30m », « 2h », « 1d »), une heure du jour
    (« 18:00 », le lendemain si elle est passée) ou une date (« 2026-10-12
    09:00 », heure locale) → instant epoch."""
    from . import authority, stagnation
    now = time.time() if now is None else float(now)
    value = str(text or "").strip()
    match = _TIME_RE.match(value)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise DecisionError("échéance illisible : %r" % value)
        local = time.localtime(now)
        moment = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0,
                              0, 0, -1))
        return moment if moment > now else moment + 86400.0
    if re.match(r"^\s*\d{4}-\d{2}-\d{2}", value):
        try:
            return authority.parse_expiry(value) / 1_000_000.0
        except authority.AuthorityError as exc:
            raise DecisionError(str(exc)) from exc
    try:
        return now + stagnation.parse_duration(value)
    except stagnation.StaleError as exc:
        raise DecisionError("--by : %s (ou une heure 18:00, une date 2026-10-12 09:00)"
                            % exc) from exc


def _local(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _owner_label(owner: str | None) -> str:
    return owner or "tout humain du mesh"


def request_text(decision: dict, *, requester: str, owner: str | None,
                 decision_id: int | None = None) -> str:
    """Le texte lisible d'une demande (fil, courrier, R12)."""
    lot = decision.get("lot")
    head = "Décision%s demandée à %s par %s — lot #%s « %s »%s :" % (
        " #%d" % decision_id if decision_id is not None else "", _owner_label(owner), requester,
        lot, decision.get("lot_title") or "",
        " (projet %s)" % decision["project"] if decision.get("project") else "")
    lines = [head, decision.get("question") or ""]
    for option in decision.get("options") or ():
        lines.append("  %s) %s%s" % (option["key"], option["label"],
                                     "   ← recommandé" if option["key"] == decision.get("recommend")
                                     else ""))
    if decision.get("recommend"):
        lines.append("Recommandation : %s%s" % (
            decision["recommend"], " — %s" % decision["why"] if decision.get("why") else ""))
    tail = ["Échéance : %s." % _local(decision["due_ts"])] if decision.get("due_ts") else []
    if decision.get("urgent"):
        tail.append("URGENT.")
    if decision.get("needs_signature"):
        tail.append("Réponse SIGNÉE exigée (geste en production ou irréversible).")
    if tail:
        lines.append(" ".join(tail))
    lines.append("Répondre : ameesh decide %s <option | \"texte\"> (ou dans ameesh chat)"
                 % (decision_id if decision_id is not None else "<numéro>"))
    return "\n".join(lines)


def _hold_lot(db, item: dict, *, note: str, actor: str) -> str:
    """Le lot passe en `waiting_human` (journal) ; rend son état."""
    state = item["state"]
    if state != "waiting_human" and "waiting_human" in work.TRANSITIONS.get(state, ()):
        try:
            return work.move(db, int(item["id"]), "waiting_human", note=note, actor=actor)["state"]
        except work.WorkError:
            state = (work.get(db, int(item["id"])) or item)["state"]
    work.note(db, int(item["id"]), note, actor=actor)
    return state


def ask(cfg: Config, db, *, requester: str, question: str, options=(), lot: str | None = None,
        new_lot: str | None = None, recommend: str | None = None, why: str | None = None,
        urgent: bool = False, due_ts: float | None = None, needs_signature: bool = False,
        now: float | None = None) -> dict:
    """Dépose une demande de décision de `requester` (un agent) ; rend la
    demande (`ameesh-decision/1`) avec `lot_state`. DecisionError si refusée
    (rien n'est déposé)."""
    now = time.time() if now is None else float(now)
    question = str(question or "").strip()
    if not question:
        raise DecisionError("question vide (--question \"…\")")
    if len(question) > MAX_QUESTION:
        raise DecisionError("question trop longue (%d caractères au plus) : résumez, et "
                            "renvoyez au fil ou au lot pour le détail" % MAX_QUESTION)
    reason = fil.unreadable_reason(question)
    if reason:
        raise DecisionError("question illisible — %s" % reason)
    opts = parse_options(options)
    recommend = (recommend or "").strip() or None
    why = " ".join(str(why or "").split()) or None
    if recommend is not None:
        if not opts:
            raise DecisionError("--recommend sans option : proposez des options (--option a=…)")
        match = [o["key"] for o in opts if o["key"].lower() == recommend.lower()]
        if not match:
            raise DecisionError("--recommend %s : pas une des options (%s)"
                                % (recommend, ", ".join(o["key"] for o in opts)))
        recommend = match[0]
    if why and recommend is None:
        raise DecisionError("--why accompagne --recommend")
    if (lot is None) == (new_lot is None):
        raise DecisionError("--lot <id|réf> OU --new-lot \"titre\" (exactement un) : une "
                            "demande est rattachée à un lot")
    if due_ts is None:
        due_ts = now + DEFAULT_DUE_S
    elif float(due_ts) <= now:
        raise DecisionError("échéance déjà passée (%s)" % _local(due_ts))
    requester_row = registry.get(db, requester)
    if new_lot is not None:
        title = " ".join(new_lot.split())
        if not title:
            raise DecisionError("--new-lot : titre vide")
        try:
            item = work.add(db, title=title, assignee=requester, actor=requester,
                            source="decision", cfg=cfg)
        except work.WorkError as exc:
            raise DecisionError("lot non créé — %s" % exc) from exc
    else:
        from . import assignments
        try:
            item = assignments.resolve_lot(db, lot)
        except assignments.AssignmentError as exc:
            raise DecisionError(str(exc)) from exc
        if item is None:
            raise DecisionError("lot %s introuvable : un numéro de lot ouvert, une référence "
                                "unique, ou --new-lot \"titre\"" % lot)
    owner, source = resolve_owner(cfg, db, requester_row, item)
    lot_id = int(item["id"])
    previous = item["state"]
    if previous == "waiting_human":
        # une autre demande tient déjà le lot : son état d'avant est le bon
        inherited = [view_of(r, now).get("previous_state")
                     for r in storage.of(db).decisions.pending_for_lot(str(lot_id))]
        previous = next((p for p in inherited if p), None)
    decision = {
        "v": 1, "state": "pending", "question": question, "options": opts,
        "recommend": recommend, "why": why, "urgent": bool(urgent),
        "due_ts": round(float(due_ts), 3), "needs_signature": bool(needs_signature),
        "owner_source": source, "project": _project(db, item, requester_row),
        "lot": lot_id, "lot_title": item.get("title") or "", "previous_state": previous,
    }
    row = storage.of(db).decisions.create(
        sender=requester, recipient=owner or OWNER_ANY,
        body=request_text(decision, requester=requester, owner=owner),
        payload={PAYLOAD_KEY: decision}, work_item_id=str(lot_id), host=cfg.host)
    decision_id = int(row["id"])
    fil.record(cfg, db, sender=requester, recipients=[owner or OWNER_ANY],
               text=request_text(decision, requester=requester, owner=owner,
                                 decision_id=decision_id),
               ts=row.get("created_ts"), project=fil.project_for(cfg, row.get("sender_project")),
               lot=lot_id, mailbox_ids=[decision_id],
               meta={"kind": KIND, "decision": decision_id, "urgent": True if urgent else None,
                     "signature": "exigée" if needs_signature else None})
    lot_state = _hold_lot(db, item, actor=requester, note="décision #%d demandée à %s : %s" % (
        decision_id, _owner_label(owner), fil.excerpt(question, 160)))
    out = get(db, decision_id, now) or {"id": decision_id}
    out["lot_state"] = lot_state
    return out


# --------------------------------------------------------------------------
# réponse
# --------------------------------------------------------------------------

def match_option(view: dict, text: str) -> str | None:
    """La clé de l'option que `text` désigne exactement (casse ignorée), sinon None."""
    wanted = str(text or "").strip().lower()
    for option in view.get("options") or ():
        if wanted == str(option.get("key") or "").lower():
            return option["key"]
    return None


def _option_label(view: dict, key: str | None) -> str | None:
    for option in view.get("options") or ():
        if option.get("key") == key:
            return option.get("label")
    return None


def answer_digest(view: dict, text: str, option: str | None, human: str) -> str:
    """L'empreinte que signe une réponse (sha256 hex) : la demande (numéro,
    demandeur, lot, question, options, heure), la réponse exacte, son auteur."""
    from . import authority
    document = {
        "v": 1, "kind": "ameesh-decision-answer", "decision": int(view["id"]),
        "request": {"requester": view.get("requester"), "lot": view.get("lot"),
                    "question": view.get("question"), "options": view.get("options") or [],
                    "created": int(float(view.get("created_ts") or 0))},
        "answer": text, "option": option, "by": human,
    }
    return authority.hash_artifact(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False))


def _approver_of(human: str, approver: str | None) -> str:
    name = (approver or human.split(":", 1)[-1]).strip()
    if not NAME_RE.match(name):
        raise DecisionError("clé du propriétaire : précisez son nom au registre (--as NOM)")
    return name


def signature_hint(view: dict, text: str, option: str | None, human: str) -> str:
    digest = answer_digest(view, text, option, human)
    answer = text if " " not in text else '"%s"' % text.replace('"', '\\"')
    return ("décision #%d : réponse SIGNÉE exigée (geste en production ou irréversible). "
            "Signe-la toi-même, dans ton terminal :\n"
            "  ameesh decide %d %s --key <clé privée du propriétaire> [--as <nom de la clé>]\n"
            "ou signe d'abord son empreinte, puis réponds avec --signed :\n"
            "  ameesh approve --key <clé> --as <nom de la clé> --action %s --kind %s --hash %s\n"
            "  ameesh decide %d %s --signed"
            % (view["id"], view["id"], answer, APPROVAL_ACTION, APPROVAL_KIND, digest,
               view["id"], answer))


def _sign(db, view: dict, text: str, option: str | None, human: str, *,
          key_path: str | None, approver: str | None) -> dict:
    """La preuve d'une réponse signée : approbation `mesh_approvals` (clé de
    rôle `owner`) sur l'empreinte de la réponse, CONSOMMÉE par la décision."""
    from . import authority, signing
    digest = answer_digest(view, text, option, human)
    if key_path:
        try:
            seed = signing.read_private(os.path.expanduser(key_path))
        except (OSError, ValueError) as exc:
            raise DecisionError("clé privée illisible : %s" % exc) from exc
        try:
            authority.create_approval(
                db, seed, approver=_approver_of(human, approver), action=APPROVAL_ACTION,
                artifact_kind=APPROVAL_KIND, artifact_hash=digest, ttl=SIGN_TTL_S,
                meta={"decision": int(view["id"]), "by": human})
        except authority.AuthorityError as exc:
            raise DecisionError("signature refusée : %s" % exc) from exc
    row, verdict = authority.find_approval(db, APPROVAL_ACTION, digest,
                                           consume_by="decision:%d" % int(view["id"]))
    if row is None or not verdict.ok:
        raise DecisionError("réponse signée refusée : %s\n%s" % (
            verdict.reason, signature_hint(view, text, option, human)))
    return {"approval_id": int(row["id"]), "approver": row.get("approver"),
            "signature_key": row.get("signature_key"), "digest": digest}


def verify_proof(db, view: dict) -> tuple[bool, str]:
    """Revérifie la preuve d'une réponse signée : approbation consommée par
    CETTE décision, empreinte recalculée, clé `owner` toujours valide,
    signature Ed25519."""
    from . import authority, signing
    answer = view.get("answer") or {}
    proof = answer.get("proof") or {}
    if not proof:
        return False, "réponse non signée"
    digest = answer_digest(view, answer.get("text") or "", answer.get("option"),
                           answer.get("by") or "")
    if digest != proof.get("digest"):
        return False, "l'empreinte ne correspond plus à la réponse enregistrée"
    rows = storage.of(db).approvals.recent(action=APPROVAL_ACTION, artifact_hash=digest, limit=20)
    row = next((r for r in rows if int(r["id"]) == int(proof.get("approval_id") or 0)), None)
    if row is None:
        return False, "approbation #%s introuvable" % proof.get("approval_id")
    if row.get("consumed_by") != "decision:%d" % int(view["id"]):
        return False, "approbation #%s non consommée par cette décision" % row["id"]
    info = authority.key_info(db, row.get("approver") or "") or {}
    if (info.get("key_role") or "") != "owner" or not info.get("public_key"):
        return False, "la clé de %s n'est pas (ou plus) une clé du propriétaire" % row.get(
            "approver")
    if info.get("key_revoked_ts"):
        return False, "clé de %s révoquée depuis" % row.get("approver")
    if info.get("public_key_fingerprint") != row.get("signature_key"):
        return False, "clé de %s changée depuis la signature" % row.get("approver")
    try:
        parts = str(row.get("signed_payload") or "").split("|", 2)
        signed = json.loads(parts[2]) if len(parts) == 3 else {}
        ok = (signed.get("artifact_hash") == digest and signed.get("action") == APPROVAL_ACTION
              and signing.verify(authority.unb64(info["public_key"]),
                                 str(row["signed_payload"]).encode("utf-8"),
                                 authority.unb64(row.get("signature") or "")))
    except (ValueError, authority.AuthorityError):
        ok = False
    if not ok:
        return False, "signature Ed25519 invalide"
    return True, "signée par la clé du propriétaire %s (%s…), approbation #%s" % (
        row.get("approver"), str(row.get("signature_key") or "")[:12], row["id"])


def _closed_reason(view: dict) -> str:
    if view["state"] == "withdrawn":
        who = (view.get("withdrawn") or {}).get("by") or view.get("requester")
        return "décision #%d retirée par %s" % (view["id"], who)
    answer = view.get("answer") or {}
    return "décision #%d déjà répondue par %s (%s) : « %s »" % (
        view["id"], answer.get("by") or "?", CHANNEL_LABELS.get(answer.get("channel"), "?"),
        fil.excerpt(answer.get("text") or "", 120))


def _release_lot(db, view: dict, *, actor: str, note: str) -> dict:
    """Après la réponse (ou le retrait) : le lot revient à son état d'avant la
    demande s'il attend encore et qu'aucune autre demande ne le tient ; sinon
    une note. Rend `{lot, state, moved, others}`."""
    lot = view.get("lot")
    if lot is None or not str(lot).isdigit():
        return {"lot": lot, "state": None, "moved": False, "others": 0}
    item = work.get(db, int(lot))
    if item is None:
        return {"lot": lot, "state": None, "moved": False, "others": 0}
    others = [r for r in storage.of(db).decisions.pending_for_lot(str(lot))
              if int(r["id"]) != int(view["id"])]
    target = view.get("previous_state")
    if item["state"] == "waiting_human" and not others and target \
            and target in work.TRANSITIONS["waiting_human"]:
        try:
            work.move(db, int(lot), target, note="%s — le lot revient en %s" % (note, target),
                      actor=actor)
            return {"lot": int(lot), "state": target, "moved": True, "others": 0}
        except work.WorkError:
            item = work.get(db, int(lot)) or item
    suffix = ""
    if item["state"] == "waiting_human":
        suffix = (" — le lot reste en attente : %d autre(s) décision(s) en attente" % len(others)
                  if others else " — le lot reste en waiting_human (il l'était avant la demande)")
    work.note(db, int(lot), note + suffix, actor=actor)
    return {"lot": int(lot), "state": item["state"], "moved": False, "others": len(others)}


def answer_text(view: dict) -> str:
    """Le courrier de réponse au demandeur (lisible, R12)."""
    answer = view.get("answer") or {}
    channel = answer.get("channel")
    if answer.get("option"):
        what = "option %s « %s »" % (answer["option"], answer.get("option_label") or "")
    else:
        what = "réponse libre"
    lines = ["Décision #%d — réponse du propriétaire %s (%s) sur le lot #%s « %s » : %s." % (
        view["id"], answer.get("by"), CHANNEL_LABELS.get(channel, channel), view.get("lot"),
        view.get("lot_title") or "", what),
        "Texte exact : « %s »" % (answer.get("text") or ""),
        "Ta question : « %s »" % fil.excerpt(view.get("question") or "", 300)]
    if channel == "signed":
        proof = answer.get("proof") or {}
        lines.append("Réponse SIGNÉE par la clé du propriétaire (approbation #%s, clé %s…), "
                     "vérifiée et consommée : `ameesh decide show %d`."
                     % (proof.get("approval_id"), str(proof.get("signature_key") or "")[:12],
                        view["id"]))
    lines.append("Cette réponse, enregistrée dans la file des décisions, vaut décision du "
                 "propriétaire : reprends le lot en conséquence (vérifiable : ameesh decide "
                 "show %d)." % view["id"])
    return "\n".join(lines)


def answer(cfg: Config, db, decision_id: int, text: str, *, human: str, channel: str,
           key_path: str | None = None, approver: str | None = None, signed: bool = False,
           now: float | None = None) -> dict:
    """Enregistre la réponse de `human` (déjà identifié : `answering_human`).

    Rend `{decision, lot, message_id, warning}`. DecisionError : demande
    inconnue ou close, humain non visé, texte illisible, signature exigée
    absente ou refusée."""
    now = time.time() if now is None else float(now)
    if channel not in CHANNELS:
        raise DecisionError("canal inconnu : %r" % channel)
    view = get(db, decision_id, now)
    if view is None:
        raise DecisionError("décision #%s introuvable (ameesh decisions)" % decision_id)
    if view["state"] != "pending":
        raise DecisionError(_closed_reason(view))
    if view["owner"] and view["owner"] != human:
        raise DecisionError("décision #%d adressée à %s : %s n'y répond pas"
                            % (view["id"], view["owner"], human))
    text = str(text or "").strip()
    if not text:
        raise DecisionError("réponse vide : une option (%s) ou un texte"
                            % (", ".join(o["key"] for o in view["options"]) or "aucune"))
    if len(text) > MAX_ANSWER:
        raise DecisionError("réponse trop longue (%d caractères au plus)" % MAX_ANSWER)
    reason = fil.unreadable_reason(text)
    if reason:
        raise DecisionError("réponse illisible — %s" % reason)
    option = match_option(view, text)
    proof = None
    if key_path or signed:
        if channel == "chat":
            raise DecisionError("une réponse signée se donne dans ton terminal, pas dans le "
                                "chat :\n" + signature_hint(view, text, option, human))
        proof = _sign(db, view, text, option, human, key_path=key_path, approver=approver)
        channel = "signed"
    elif view["needs_signature"]:
        raise DecisionError(signature_hint(view, text, option, human))
    record = {"state": "answered", "answer": {
        "by": human, "channel": channel, "text": text, "option": option,
        "option_label": _option_label(view, option), "ts": round(now, 3), "host": cfg.host,
        "proof": proof}}
    closed = storage.of(db).decisions.close(int(view["id"]), status="answered", record=record)
    if closed is None:
        latest = get(db, decision_id, now)
        raise DecisionError(_closed_reason(latest) if latest else
                            "décision #%s introuvable" % decision_id)
    view = view_of(closed, now)
    if option:
        said = "option %s « %s »" % (option, _option_label(view, option) or "")
    else:
        said = "« %s »" % fil.excerpt(text, 160)
    lot = _release_lot(db, view, actor=human, note="décision #%d répondue par %s (%s) : %s" % (
        view["id"], human, CHANNEL_LABELS[channel], said))
    out = {"decision": view, "lot": lot, "message_id": None, "warning": None}
    from . import mail
    try:
        out["message_id"] = mail.send(
            db, human, view["requester"], answer_text(view), host=cfg.host, kind=REPLY_KIND,
            work_item_id=str(view["lot"]) if view.get("lot") is not None else None,
            # L125 : le courrier d'un humain réveille tout de suite (jamais
            # retenu par la fenêtre de regroupement, jamais passif)
            payload={"human": True,
                     "decision_answer": {"decision": view["id"], "lot": view.get("lot"),
                                         "by": human, "channel": channel, "option": option,
                                         "text": text}},
            thread_meta={"decision": view["id"], "channel": channel})
    except Exception as exc:  # noqa: BLE001 - la réponse est enregistrée : le dire
        out["warning"] = ("réponse enregistrée, mais le courrier à %s n'a pas été déposé (%s) : "
                          "prévenez-le" % (view["requester"], exc))
    return out


def withdraw(cfg: Config, db, decision_id: int, *, requester: str, why: str | None = None,
             now: float | None = None) -> dict:
    """Le demandeur retire sa demande ; le lot revient à son état d'avant."""
    now = time.time() if now is None else float(now)
    view = get(db, decision_id, now)
    if view is None:
        raise DecisionError("décision #%s introuvable" % decision_id)
    if view["state"] != "pending":
        raise DecisionError(_closed_reason(view))
    if view["requester"] != requester:
        raise DecisionError("décision #%d : seul son demandeur (%s) la retire"
                            % (view["id"], view["requester"]))
    why = " ".join(str(why or "").split()) or None
    record = {"state": "withdrawn", "withdrawn": {"by": requester, "why": why,
                                                  "ts": round(now, 3)}}
    closed = storage.of(db).decisions.close(int(view["id"]), status="delivered", record=record)
    if closed is None:
        latest = get(db, decision_id, now)
        raise DecisionError(_closed_reason(latest) if latest else
                            "décision #%s introuvable" % decision_id)
    view = view_of(closed, now)
    text = "décision #%d retirée par %s%s" % (view["id"], requester, " : %s" % why if why else "")
    lot = _release_lot(db, view, actor=requester, note=text)
    fil.record(cfg, db, sender=requester, recipients=[view["owner"] or OWNER_ANY],
               text="Décision #%d retirée par %s%s — question : « %s »" % (
                   view["id"], requester, " (%s)" % why if why else "",
                   fil.excerpt(view["question"], 300)),
               project=fil.project_for(cfg, closed.get("sender_project")), lot=view.get("lot"),
               meta={"kind": KIND, "decision": view["id"], "state": "withdrawn"})
    return {"decision": view, "lot": lot}


# --------------------------------------------------------------------------
# vues : alertes, en-tête de `ameesh projects`, file de `ameesh decisions`
# --------------------------------------------------------------------------

def describe(view: dict, limit: int = 220) -> str:
    """Une ligne : numéro, demandeur, question, options, recommandation."""
    options = " ; ".join("%s) %s" % (o["key"], fil.excerpt(o["label"], 40))
                         for o in view.get("options") or ())
    return "décision #%d de %s pour %s : « %s »%s%s — répondre : ameesh decide %d <option> " \
           "(ou ameesh chat)" % (
               view["id"], view.get("requester"), _owner_label(view.get("owner")),
               fil.excerpt(view.get("question") or "", limit),
               " — %s" % options if options else "",
               " ; recommandé : %s" % view["recommend"] if view.get("recommend") else "",
               view["id"])


def board_summary(rows, *, viewer: str | None = None, now: float | None = None) -> dict:
    """L'en-tête de `ameesh projects` (clé `decisions` de `ameesh-projects/1`) :
    demandes en attente, celles qui attendent `viewer` (et celles sans
    destinataire précis), la plus ancienne, urgentes, échues."""
    now = time.time() if now is None else float(now)
    rows = list(rows or ())
    mine = [r for r in rows if viewer is None or r.get("owner") in (viewer, OWNER_ANY, None)]
    oldest = min((float(r["created_ts"]) for r in mine if r.get("created_ts") is not None),
                 default=None)
    return {
        "viewer": viewer,
        "pending": len(rows),
        "mine": len(mine),
        "oldest_ts": round(oldest, 3) if oldest is not None else None,
        "urgent": sum(1 for r in mine if r.get("urgent")),
        "overdue": sum(1 for r in mine if r.get("due_ts") is not None
                       and now >= float(r["due_ts"])),
    }


def short_span(seconds) -> str:
    """Une durée courte : 42s, 12m, 2h05, 1j03h."""
    seconds = int(max(0, seconds or 0))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dj%02dh" % (seconds // 86400, (seconds % 86400) // 3600)


def headline(summary: dict | None, now: float) -> str | None:
    """« 3 décisions t'attendent, la plus ancienne depuis 2h05 … » ; None sans objet."""
    if not summary:
        return None
    count = summary["mine"] if summary.get("viewer") else summary["pending"]
    if not count or summary.get("oldest_ts") is None:
        return None
    extra = []
    if summary.get("urgent"):
        extra.append("%d urgente%s" % (summary["urgent"], "s" if summary["urgent"] > 1 else ""))
    if summary.get("overdue"):
        extra.append("%d échue%s" % (summary["overdue"], "s" if summary["overdue"] > 1 else ""))
    plural = count > 1
    if summary.get("viewer"):
        head = "%d décision%s t'attend%s" % (count, "s" if plural else "", "ent" if plural else "")
        hint = "ameesh decisions, ou ameesh chat"
    else:
        head = "%d décision%s attend%s un humain" % (count, "s" if plural else "",
                                                   "ent" if plural else "")
        hint = "ameesh decisions"
    return "%s, la plus ancienne depuis %s%s — %s" % (
        head, short_span(now - float(summary["oldest_ts"])),
        " (dont %s)" % ", ".join(extra) if extra else "", hint)


def format_view(view: dict, now: float | None = None) -> list[str]:
    """Les lignes d'une demande dans `ameesh decisions` et `decide show`."""
    now = time.time() if now is None else float(now)
    flags = []
    if view.get("urgent"):
        flags.append("URGENT")
    if view.get("overdue"):
        flags.append("ÉCHUE")
    if view.get("needs_signature"):
        flags.append("signature exigée")
    head = "#%d  il y a %s · %s · lot #%s « %s » · de %s → %s%s" % (
        view["id"], short_span(now - float(view["created_ts"])),
        view.get("project") or "(sans projet)",
        view.get("lot"), fil.excerpt(view.get("lot_title") or "", 60), view.get("requester"),
        _owner_label(view.get("owner")), "  [%s]" % "] [".join(flags) if flags else "")
    lines = [head]
    for line in (view.get("question") or "").splitlines() or [""]:
        lines.append("      %s" % line)
    for option in view.get("options") or ():
        lines.append("        %s) %s%s" % (option["key"], option["label"],
                                           "   ← recommandé"
                                           if option["key"] == view.get("recommend") else ""))
    if view.get("recommend"):
        lines.append("      recommandation : %s%s" % (
            view["recommend"], " — %s" % view["why"] if view.get("why") else ""))
    if view["state"] == "pending":
        due = view.get("due_ts")
        lines.append("      échéance %s%s · répondre : ameesh decide %d <option | \"texte\">%s" % (
            _local(due) if due else "—",
            (" (dépassée de %s)" % short_span(now - due) if now >= due else
             " (dans %s)" % short_span(due - now)) if due else "",
            view["id"], " --key <clé>" if view.get("needs_signature") else ""))
    elif view["state"] == "answered":
        answer = view.get("answer") or {}
        lines.append("      répondue par %s (%s) le %s : « %s »" % (
            answer.get("by"), CHANNEL_LABELS.get(answer.get("channel"), "?"),
            _local(answer.get("ts") or view.get("closed_ts") or now),
            fil.excerpt(answer.get("text") or "", 200)))
    else:
        withdrawn = view.get("withdrawn") or {}
        lines.append("      retirée par %s le %s%s" % (
            withdrawn.get("by") or view.get("requester"),
            _local(withdrawn.get("ts") or view.get("closed_ts") or now),
            " : %s" % withdrawn["why"] if withdrawn.get("why") else ""))
    return lines


def queue(db, *, human: str | None = None, include_closed: bool = False,
          now: float | None = None) -> dict:
    """Les demandes pour `ameesh decisions` : `pending` (les plus anciennes
    d'abord) et, avec `include_closed`, `closed` (les plus récentes d'abord)."""
    now = time.time() if now is None else float(now)
    return {"pending": pending(db, now, human=human),
            "closed": history(db, now, human=human) if include_closed else []}


# --------------------------------------------------------------------------
# entrée : ameesh decide
# --------------------------------------------------------------------------

USAGE = ("usage : ameesh decide ask (--lot <id|réf> | --new-lot \"titre\") --question \"…\" "
         "--option a=\"…\" [--option b=\"…\"] [--recommend a [--why \"…\"]] [--urgent] "
         "[--by 2h] [--needs-signature] [--json]\n"
         "        ameesh decide <id> <option | \"texte libre\"> "
         "[--key FICHIER [--as NOM] | --signed] [--json]\n"
         "        ameesh decide withdraw <id> [--why \"…\"] [--json]\n"
         "        ameesh decide show <id> [--json]")


def _decision_id(text) -> int | None:
    value = str(text or "").strip()
    value = value[1:] if value.startswith("#") else value
    return int(value) if value.isdigit() else None


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _open(cfg: Config):
    return db_mod.open_db(cfg)


def _parser(prog: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)


def cmd_ask(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide ask")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--lot", default=None, help="lot ouvert : numéro ou référence unique")
    target.add_argument("--new-lot", default=None, help="crée le lot (assigné au demandeur)")
    parser.add_argument("--question", required=True)
    parser.add_argument("--option", action="append", default=[], metavar="CLÉ=LIBELLÉ")
    parser.add_argument("--recommend", default=None, metavar="CLÉ")
    parser.add_argument("--why", default=None, help="pourquoi cette recommandation")
    parser.add_argument("--urgent", action="store_true")
    parser.add_argument("--by", default=None,
                        help="échéance : 30m, 2h, 1d, 18:00 ou « 2026-10-12 09:00 » "
                             "(défaut : dans %d h)" % (DEFAULT_DUE_S // 3600))
    parser.add_argument("--needs-signature", action="store_true",
                        help="geste en production ou irréversible : réponse signée exigée")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get(CHAT_ENV):
        raise DecisionError("le chat transmet les demandes du propriétaire aux orchestrateurs ; "
                            "il ne dépose pas de demande de décision")
    due = parse_due(args.by) if args.by else None
    db = _open(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok:
            raise DecisionError(
                "demandeur non lié (%s) : une demande de décision est déposée par un agent, "
                "dont l'identité est posée par l'exécuteur (AGENT_MAIL_NAME)"
                % (binding.reason or "aucune identité"))
        view = ask(cfg, db, requester=binding.name, question=args.question,
                   options=args.option, lot=args.lot, new_lot=args.new_lot,
                   recommend=args.recommend, why=args.why, urgent=args.urgent, due_ts=due,
                   needs_signature=args.needs_signature)
    finally:
        db.close()
    if args.json:
        _dump(view)
        return 0
    print("décision #%d demandée à %s%s — lot #%s « %s » : %s" % (
        view["id"], _owner_label(view["owner"]),
        " (%s)" % {"lot": "responsable de la fiche du lot", "agent": "ton responsable",
                   "assigne": "responsable de l'assigné du lot",
                   "defaut": "humain par défaut des alertes",
                   "humain_unique": "seul humain déclaré"}.get(view["owner_source"], "")
        if view["owner_source"] else "",
        view["lot"], view.get("lot_title") or "", view.get("lot_state")))
    print("échéance %s%s%s ; le propriétaire est notifié, et relancé à l'échéance." % (
        _local(view["due_ts"]), " · URGENTE" if view["urgent"] else "",
        " · réponse SIGNÉE exigée" if view["needs_signature"] else ""))
    print("La réponse te sera remise par courrier (elle te réveille) : n'attends pas en "
          "bouclant, termine ton tour. Retirer la demande : ameesh decide withdraw %d"
          % view["id"])
    return 0


def cmd_answer(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide")
    parser.add_argument("id")
    parser.add_argument("answer", nargs="+", help="une option, ou un texte libre")
    proof = parser.add_mutually_exclusive_group()
    proof.add_argument("--key", default=None,
                       help="clé privée du propriétaire : signe la réponse (ameesh approve)")
    proof.add_argument("--signed", action="store_true",
                       help="la réponse est déjà signée (ameesh approve --action decision "
                            "--hash …) : la trouver et la consommer")
    parser.add_argument("--as", dest="as_name", default=None,
                        help="nom de la clé du propriétaire au registre (défaut : ton nom)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision_id = _decision_id(args.id)
    if decision_id is None:
        print(USAGE, file=sys.stderr)
        return 2
    if args.as_name and not args.key:
        print("--as n'a de sens qu'avec --key", file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        who = answering_human(cfg, db)
        result = answer(cfg, db, decision_id, " ".join(args.answer), human=who.human,
                        channel=who.channel, key_path=args.key, approver=args.as_name,
                        signed=args.signed)
    finally:
        db.close()
    if args.json:
        _dump(result)
        return 0
    view, answer_ = result["decision"], result["decision"]["answer"]
    print("décision #%d : réponse de %s (%s) enregistrée : %s« %s »" % (
        view["id"], answer_["by"], CHANNEL_LABELS[answer_["channel"]],
        "option %s — " % answer_["option"] if answer_.get("option") else "", answer_["text"]))
    lot = result["lot"]
    if lot.get("moved"):
        print("lot #%s : revient en %s" % (lot["lot"], lot["state"]))
    elif lot.get("lot") is not None and lot.get("state"):
        print("lot #%s : reste en %s%s" % (lot["lot"], lot["state"],
                                           " (%d autre(s) décision(s) en attente)"
                                           % lot["others"] if lot.get("others") else ""))
    if result.get("message_id"):
        print("réponse déposée pour %s (message #%d) : elle le réveille"
              % (view["requester"], result["message_id"]))
    if result.get("warning"):
        print("attention : %s" % result["warning"], file=sys.stderr)
    return 0


def cmd_withdraw(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide withdraw")
    parser.add_argument("id")
    parser.add_argument("--why", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision_id = _decision_id(args.id)
    if decision_id is None:
        print(USAGE, file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok or os.environ.get(CHAT_ENV):
            raise DecisionError("seul l'agent qui a demandé la décision la retire (identité "
                                "d'agent requise)")
        result = withdraw(cfg, db, decision_id, requester=binding.name, why=args.why)
    finally:
        db.close()
    if args.json:
        _dump(result)
        return 0
    lot = result["lot"]
    print("décision #%d retirée%s" % (
        result["decision"]["id"],
        " ; lot #%s %s %s" % (lot["lot"], "revient en" if lot.get("moved") else "reste en",
                              lot["state"]) if lot.get("state") else ""))
    return 0


def cmd_show(cfg: Config, argv: list[str]) -> int:
    parser = _parser("ameesh decide show")
    parser.add_argument("id")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision_id = _decision_id(args.id)
    if decision_id is None:
        print(USAGE, file=sys.stderr)
        return 2
    db = _open(cfg)
    try:
        view = get(db, decision_id)
        if view is None:
            raise DecisionError("décision #%s introuvable" % args.id)
        proof = verify_proof(db, view) if (view.get("answer") or {}).get("proof") else None
    finally:
        db.close()
    if proof is not None:
        view["proof_check"] = {"ok": proof[0], "detail": proof[1]}
    if args.json:
        _dump(view)
        return 0
    now = time.time()
    for line in format_view(view, now):
        print(line)
    if view["state"] == "answered":
        answer_ = view.get("answer") or {}
        print("      texte exact : « %s »" % (answer_.get("text") or ""))
    if proof is not None:
        print("      preuve : %s — %s" % ("VALIDE" if proof[0] else "INVALIDE", proof[1]))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    cfg = config_mod.load()
    try:
        if command == "ask":
            return cmd_ask(cfg, rest)
        if command == "withdraw":
            return cmd_withdraw(cfg, rest)
        if command == "show":
            return cmd_show(cfg, rest)
        if _decision_id(command) is not None:
            return cmd_answer(cfg, argv)
        print(USAGE, file=sys.stderr)
        return 2
    except DecisionError as exc:
        print(str(exc) if str(exc).startswith("refus") else "refus : %s" % exc,
              file=sys.stderr)
        return 1
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

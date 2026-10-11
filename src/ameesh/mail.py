# SPDX-License-Identifier: AGPL-3.0-only
"""`agent_mailbox` : messages durables entre agents.

Le dépôt d'un message est la seule écriture nécessaire : un trigger Postgres
émet `pg_notify('agent_mail', …)`, ce qui réveille l'exécuteur du destinataire.
La remise est faite par le hook du harnais, ou par l'exécuteur au lancement
d'un tour dont la consigne porte le message — toujours par le même protocole :
réserver (`reserve`), montrer, solder (`deliver`) la réservation qui porte son
jeton. Un message non remis est représenté par `delivered_at IS NULL`.

La boîte n'est que la file de distribution : chaque message déposé est aussi
écrit dans son **fil lisible** (`fil.record`, R12). Un corps illisible (vide,
JSON seul, binaire, bloc encodé) est refusé avant tout dépôt ; un fil
inaccessible ne fait jamais perdre le message.

Le SQL est dans le stockage (`storage.of(db).mailbox`, spec §10).
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any, Sequence

from . import fil, storage
from .config import NAME_RE
from .db import Db
from .storage.postgres import mailbox as _pg

#: projet du fil d'un agent (`fil.agent_project`), en SQL : son équipe s'il est
#: gouverné par le canon, sinon son chantier — pilote Postgres, alias de
#: compatibilité (voir `storage.postgres.mailbox`)
PROJECT_SQL = _pg.PROJECT_SQL

#: colonnes rendues pour un message (pilote Postgres ; alias de compatibilité)
MAIL_COLUMNS = _pg.MAIL_COLUMNS


def send(
    db: Db,
    sender: str,
    recipient: str,
    body: str,
    *,
    host: str | None = None,
    kind: str = "notify",
    payload: dict | None = None,
    work_item_id: str | None = None,
    signature: str | None = None,
    signature_key: str | None = None,
    signed_payload: str | None = None,
    nonce: str | None = None,
    created_us: int | None = None,
    expires_us: int | None = None,
    allow_structured: bool = False,
    thread: bool = True,
    thread_meta: dict | None = None,
) -> int:
    """Dépose un message pour UN destinataire, l'écrit dans son fil. Renvoie son id.

    Les champs `signed_*` viennent de `authority.sign_message` : un message
    signé fixe son propre horodatage (c'est lui qui est couvert par la
    signature) et son échéance.

    `allow_structured` lève le refus des corps illisibles (tests et outils
    seulement). `thread=False` laisse l'écriture du fil à l'appelant (envoi à
    « all » regroupé en une entrée, import déjà écrit). Projet du fil : celui
    de l'expéditeur, sinon du destinataire — son équipe (`team`) s'il est
    gouverné par le canon, sinon son chantier —, sinon AMEESH_PROJECT, sinon
    `default` ; lot : `work_item_id`.
    """
    fil.ensure_readable(body, allow_structured)
    row = storage.of(db).mailbox.send(
        sender, recipient, body, host=host, kind=kind, payload=payload,
        work_item_id=work_item_id, signature=signature, signature_key=signature_key,
        signed_payload=signed_payload, nonce=nonce, created_us=created_us,
        expires_us=expires_us)
    message_id = int(row["id"])
    if thread:
        # Le message est déposé : à partir d'ici, rien ne doit le faire perdre
        # (fil.record ne lève jamais).
        meta = {"host": host, "signature_key": signature_key}
        if kind != "notify":
            meta["kind"] = kind
        meta.update(thread_meta or {})
        fil.record(
            db.cfg, db, sender=sender, recipients=[recipient], text=body,
            ts=row.get("created_ts"),
            project=fil.project_for(db.cfg, row.get("sender_project"),
                                    row.get("recipient_project")),
            lot=work_item_id, mailbox_ids=[message_id], meta=meta)
    return message_id


#: nombre de non-lus lus par l'exécuteur à chaque sondage
UNREAD_LIMIT = 200


def unread(db: Db, recipient: str, limit: int = UNREAD_LIMIT) -> list[dict]:
    """Messages non remis, du plus ancien au plus récent (comme la v0)."""
    return storage.of(db).mailbox.unread(recipient, limit)


def unread_active(db: Db, recipient: str, limit: int = UNREAD_LIMIT) -> list[dict]:
    """L125 : les non-lus qui ouvriront un tour (hors courrier passif)."""
    return storage.of(db).mailbox.unread_active(recipient, limit)


def unread_urgent(db: Db, recipient: str, limit: int = 50) -> list[dict]:
    """Messages non remis marqués `payload.urgent` (interruption de tour, 0018)."""
    return storage.of(db).mailbox.unread_urgent(recipient, limit)


def get(db: Db, message_id: int) -> dict | None:
    """Un message par son id (l'écouteur NOTIFY ne porte que l'id)."""
    return storage.of(db).mailbox.get(message_id)


def mark_delivered(db: Db, ids: Sequence[int]) -> int:
    """Marque remis (le hook les a injectés, ou l'exécuteur a fini son tour)."""
    return storage.of(db).mailbox.mark_delivered(ids)


#: durée de vie d'une réservation (s) : l'exécuteur lance le harnais et solde
#: en quelques secondes ; le hook émet aussitôt. Passé ce délai (panne), la
#: réservation peut être reprise et le message est signalé « re-livré ».
RESERVATION_TTL = 600.0
HOOK_RESERVATION_TTL = 60.0


def new_token() -> str:
    """Jeton unique d'une réservation : seule la remise qui le porte la solde."""
    return uuid.uuid4().hex


def reserve(db: Db, recipient: str, owner: str | None, epoch: int | None, token: str,
            *, ids: Sequence[int] | None = None, porteur: str,
            ttl_seconds: float = RESERVATION_TTL, limit: int = 200) -> list[dict]:
    """Réserve atomiquement des non-lus pour UNE remise (consigne ou hook).

    Protocole unique de l'exécuteur et du hook : réserver sous un jeton,
    montrer, puis solder (`deliver`) ou annuler (`release`) cette réservation
    et elle seule. La ligne du destinataire est verrouillée dans le registre
    et le bail (owner, epoch) contrôlé dans la même instruction ; un message
    déjà réservé (réservation active) n'est jamais réservé deux fois. Chaque
    message rendu porte `deja_consigne` : une réservation antérieure n'a
    jamais été soldée (panne), il est peut-être déjà vu.
    """
    return storage.of(db).mailbox.reserve(
        recipient, owner or None, epoch if owner else None, token, ids=ids,
        porteur=porteur, ttl_seconds=ttl_seconds, limit=limit)


def deliver(db: Db, recipient: str, owner: str | None, epoch: int | None, token: str,
            ids: Sequence[int]) -> list[int]:
    """Solde la réservation `token` : remis, si le bail est toujours le sien.

    Rend les ids passés remis. Un id absent (bail perdu, réservation reprise)
    reste non remis : il sera re-livré, signalé.
    """
    return storage.of(db).mailbox.deliver(recipient, owner or None,
                                          epoch if owner else None, token, ids)


def release(db: Db, recipient: str, token: str, ids: Sequence[int]) -> int:
    """Annule la réservation `token` : rien n'a été montré."""
    return storage.of(db).mailbox.release(recipient, token, ids)


def maybe_redelivered(row: Any) -> bool:
    """Déjà réservé sans être soldé (panne) : peut-être déjà vu."""
    return bool(row.get("deja_consigne"))


#: plafond par défaut de la consigne d'un tour de courrier, en octets UTF-8
PROMPT_MAX_BYTES = 20000
#: borne sûre d'un argument de processus (MAX_ARG_STRLEN = 128 Kio sous Linux)
ARG_SAFE_BYTES = 120000
#: budget minimal : de quoi porter au moins un message tronqué
PROMPT_MIN_BYTES = 2000


def octets(text: str) -> int:
    return len(text.encode("utf-8"))


def _tronque(text: str, limite: int) -> tuple[str, int]:
    """Coupe `text` à `limite` octets UTF-8 (sans couper un caractère) ;
    rend (texte, octets retirés)."""
    brut = text.encode("utf-8")
    if len(brut) <= limite:
        return text, 0
    coupe = brut[:max(0, limite)].decode("utf-8", errors="ignore")
    return coupe, len(brut) - octets(coupe)


def _readable_body(row: dict, limite: int | None = None) -> str:
    """Le corps lisible, comme dans le fil : jamais le payload structuré."""
    body = row.get("body") or ""
    if fil.unreadable_reason(body):
        return "[corps non lisible, non reproduit : voir le fil, message n°%s]" % row.get("id")
    text = fil.clean_text(body).strip("\n")
    if limite is not None:
        text, retire = _tronque(text, limite)
        if retire:
            text += ("\n[… tronqué : %d octet(s) de plus, texte complet dans le fil, "
                     "message n°%s]" % (retire, row.get("id")))
    return text


def _verdict_note(db: Db | None, row: dict) -> str:
    """La mention d'autorité d'un message signé (R4) ; vide s'il ne l'est pas."""
    if db is None or not row.get("signature"):
        return ""
    from . import authority  # import tardif : authority n'est utile qu'aux messages signés
    try:
        verdict = authority.verify_message(db, normalize(row))
    except Exception as exc:  # une vérification impossible n'accorde rien
        return "  [⚠ signature non vérifiable : %s]" % exc
    if verdict.authority:
        return "  [autorité du propriétaire PROUVÉE (signature Ed25519)]"
    if verdict.ok:
        return "  [signé par un agent : authentique, sans autorité du propriétaire]"
    return "  [⚠ signature NON valide : %s]" % verdict.reason


def _block(db: Db | None, row: dict, limite: int | None = None, note: str | None = None
           ) -> str:
    """Le rendu d'un message : en-tête (n°, expéditeur, heure, nature,
    autorité, re-livré) puis corps lisible cité ligne à ligne."""
    nature = _nature(row)
    relivre = ("  [re-livré : déjà réservé pour une remise avant une panne, "
               "tu l'as peut-être déjà traité]" if maybe_redelivered(row) else "")
    moment = fil.iso_local(row.get("created_ts") or 0.0)
    corps = _readable_body(row, limite)
    return "— message n°%s de %s, %s%s%s%s%s :\n%s" % (
        row.get("id"), row.get("sender") or "?", moment, nature,
        _verdict_note(db, row) if note is None else note, relivre, forwarded_note(row),
        "\n".join("> " + line for line in corps.split("\n")))


def forwarded_note(row: Any) -> str:
    """La mention d'un message renvoyé d'une boîte morte (`ameesh mail
    forward`) : vide pour un message ordinaire."""
    origin = row.get("forwarded_from")
    if not origin:
        return ""
    return "  [renvoyé : d'abord adressé à %s, dont la boîte n'était plus lue]" % origin


def _assemble(kind: str, blocks: Sequence[str], remaining: int) -> str:
    from . import adapters  # import tardif : adapters ne dépend pas de la base
    header = {"mail": adapters.MAIL_HEADER, "event": adapters.EVENT_HEADER,
              "urgent": adapters.PRIORITY_HEADER}[kind]
    footer = {"mail": adapters.MAIL_FOOTER, "event": adapters.EVENT_FOOTER,
              "urgent": adapters.PRIORITY_FOOTER}[kind]
    parts = [header % len(blocks), adapters.AUTHORITY_NOTE, *blocks]
    if remaining > 0:
        parts.append(adapters.MAIL_REMAINING % remaining)
    parts.append(footer)
    return "\n\n".join(parts)


def prompt_for(db: Db | None, rows: Sequence[dict], *, kind: str,
               budget: int = PROMPT_MAX_BYTES, extra_remaining: int = 0
               ) -> tuple[str, list[dict], int]:
    """La consigne d'un tour de courrier, bornée sur son rendu RÉEL complet.

    `kind` : `mail`, `event` ou `urgent`. Les plus anciens d'abord : un message
    n'entre que si la consigne entière (en-tête, rappel d'autorité, messages
    cités, mention du reste, pied) tient dans `budget` octets UTF-8. Le
    premier message entre toujours, tronqué au besoin : un message trop long
    ne bloque jamais la boîte. Rend (texte, retenus, reste) ; `reste` (plus
    `extra_remaining`) est le nombre de messages laissés au tour suivant.
    """
    budget = max(PROMPT_MIN_BYTES, int(budget))
    total = len(rows) + extra_remaining
    blocks: list[str] = []
    for index, row in enumerate(rows):
        note = _verdict_note(db, row)  # une vérification par message, pas par essai
        bloc = _block(db, row, note=note)
        if octets(_assemble(kind, blocks + [bloc], total - index - 1)) <= budget:
            blocks.append(bloc)
            continue
        if blocks:
            break
        # Premier message trop long à lui seul : corps tronqué jusqu'à tenir.
        limite = budget
        while True:
            bloc = _block(db, row, limite, note=note)
            exces = octets(_assemble(kind, [bloc], total - 1)) - budget
            if exces <= 0 or limite == 0:
                break
            limite = max(0, limite - exces - 16)
        blocks.append(bloc)
        break
    retenus = list(rows[:len(blocks)])
    reste = total - len(blocks)
    return _assemble(kind, blocks, reste), retenus, reste


def unread_counts(db: Db) -> dict[str, int]:
    return storage.of(db).mailbox.unread_counts()


def history(db: Db, recipient: str, limit: int = 50) -> list[dict]:
    return storage.of(db).mailbox.history(recipient, limit)


def pending_recipients(db: Db) -> list[str]:
    return storage.of(db).mailbox.pending_recipients()


def normalize(row: dict) -> dict:
    """Forme commune aux deux backends : from, to, ts, text (+ id, kind, payload)."""
    return {
        "id": row.get("id"),
        "from": row.get("sender"),
        "to": row.get("recipient"),
        "ts": row.get("created_ts") or 0.0,
        "text": row.get("body") or "",
        "kind": row.get("kind") or "notify",
        "payload": row.get("payload") or {},
        "host": row.get("host"),
        # champs d'autorité (R4) : absents en repli fichier, présents en base
        "signature": row.get("signature"),
        "signature_key": row.get("signature_key"),
        "signed_payload": row.get("signed_payload"),
        "nonce": row.get("nonce"),
        "signature_expires_ts": row.get("signature_expires_ts"),
        # déjà réservé pour une remise jamais soldée (panne) : « re-livré »
        "deja_consigne": bool(row.get("deja_consigne")),
        # renvoyé d'une boîte morte (`ameesh mail forward`) : le premier destinataire
        "forwarded_from": row.get("forwarded_from"),
    }


def message_payload(row: Any) -> dict:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return {}
    return payload or {}


def is_event(row: Any) -> bool:
    return (row.get("kind") or "") == "event"


def is_urgent(row: Any) -> bool:
    """Un événement `urgent` perce le regroupement (C9)."""
    return bool(message_payload(row).get("urgent"))


# --------------------------------------------------------------------------
# L125 : courrier regroupé — ce qui réveille, ce qui attend le tour suivant
# --------------------------------------------------------------------------

#: clés du payload qui rendent un message PASSIF : déposé sans réveil, il est
#: lu au tour suivant de son destinataire (dans la consigne du tour, ou par le
#: hook pendant un tour) — accusé de réception, copie, diffusion à tous. Un
#: message `urgent` n'est jamais passif.
PASSIVE_KEYS = ("ack", "cc", "broadcast")

#: un accusé de réception reconnu à l'envoi tient en au plus ce nombre de
#: caractères…
ACK_MAX_CHARS = 80
#: … contient au moins une de ces formules (minuscules, sans accents)…
ACK_WORDS = frozenset((
    "recu", "merci", "remercie", "note", "accuse", "reception",
    "thanks", "thank", "thx", "received", "noted", "ack", "acknowledged"))
#: … et rien d'autre que ces mots de liaison, de la ponctuation et des émojis.
#: Jamais « ok », « oui », « non », « pas », « d'accord », « go », « parfait »,
#: « bon », « fait » : une réponse ou un feu vert que l'expéditeur attend
#: peut-être pour continuer doit le réveiller.
ACK_FILLERS = frozenset((
    "bien", "tres", "beaucoup", "mille", "pour", "le", "la", "les", "l", "ton", "ta",
    "tes", "votre", "vos", "ce", "cet", "cette", "c", "est", "de", "du", "des", "et",
    "je", "te", "toi", "vous", "a", "tout", "message", "messages", "retour", "info",
    "infos", "information", "informations", "precision", "precisions", "copie",
    "you", "for", "the", "your", "much", "very", "and"))


def _payload(row: Any) -> dict:
    payload = message_payload(row)
    return payload if isinstance(payload, dict) else {}


def passive_reason(row: Any) -> str:
    """L125 : `ack`, `cc` ou `broadcast` si ce message n'ouvre pas de tour à
    lui seul ; '' s'il réveille son destinataire."""
    payload = _payload(row)
    if payload.get("urgent"):
        return ""
    for key in PASSIVE_KEYS:
        if payload.get(key):
            return key
    return ""


def is_human(row: Any) -> bool:
    """L125 : message d'un humain, marqué à l'envoi — il réveille tout de suite."""
    return bool(_payload(row).get("human"))


def passive_label(row: Any) -> str:
    """Le genre d'un message passif, pour son en-tête ; '' sinon."""
    reason = passive_reason(row)
    if reason == "cc":
        cible = str(_payload(row).get("cc") or "")
        return "copie d'un message à %s" % (cible if NAME_RE.match(cible) else "?")
    return {"ack": "accusé de réception", "broadcast": "diffusion à tous"}.get(reason, "")


def _nature(row: Any) -> str:
    """La nature d'un message dans son en-tête : passif (L125), événement,
    urgent ; '' pour un message ordinaire."""
    passif = passive_label(row)
    if passif:
        return " (%s)" % passif
    if is_event(row):
        return " (événement%s)" % (", urgent" if is_urgent(row) else "")
    return " (urgent)" if is_urgent(row) else ""


def looks_like_ack(text: str) -> bool:
    """L125 : ce texte n'est-il QU'un accusé de réception ?

    Règle prudente — dans le doute, c'est un message ordinaire, qui réveille :
    au plus `ACK_MAX_CHARS` caractères ; ni chiffre ni point d'interrogation ;
    au moins une formule d'accusé (`ACK_WORDS` : « reçu », « merci »,
    « noté », « thanks »…) ; rien d'autre que ces formules, des mots de
    liaison (`ACK_FILLERS` : « bien », « pour ton retour »…), de la
    ponctuation et des émojis. « ok », « oui », « d'accord », « parfait »
    n'en sont jamais : ce sont des réponses, parfois un feu vert.
    """
    brut = (text or "").strip()
    if not brut or len(brut) > ACK_MAX_CHARS or any(ch.isdigit() for ch in brut):
        return False
    plat = "".join(ch for ch in unicodedata.normalize("NFKD", brut.casefold())
                   if not unicodedata.combining(ch))
    if "?" in plat:
        return False
    if any(ch.isalnum() for ch in re.sub(r"[a-z]+", "", plat)):
        return False  # lettres d'une autre écriture : prudence
    mots = re.findall(r"[a-z]+", plat)
    return (any(mot in ACK_WORDS for mot in mots)
            and all(mot in ACK_WORDS or mot in ACK_FILLERS for mot in mots))

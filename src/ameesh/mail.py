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
import time
import unicodedata
import uuid
from typing import Any, Sequence

from . import fastmail, fil, resident_queue, storage
from .config import NAME_RE
from .db import Db
from .storage.postgres import mailbox as _pg

#: projet du fil d'un agent (`fil.agent_project`), en SQL : son équipe s'il est
#: gouverné par le canon, sinon son chantier — pilote Postgres, alias de
#: compatibilité (voir `storage.postgres.mailbox`)
PROJECT_SQL = _pg.PROJECT_SQL

#: colonnes rendues pour un message (pilote Postgres ; alias de compatibilité)
MAIL_COLUMNS = _pg.MAIL_COLUMNS

#: #3892 : forme d'une clé d'idempotence d'envoi (stable, du client)
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def _replay_id(mailbox, message_id: int, body: str, work_item_id: str | None,
               key: str) -> int:
    """#3892 : l'id d'un message déjà déposé sous `key`, s'il est bien le même.

    Une clé réutilisée pour un autre corps ou un autre lot est un refus
    déterministe : la rendre comme « envoyé » ferait croire livrée une demande
    qui n'est jamais partie (#3889, relecture cx1 : lier la clé au corps
    original et refuser tout écart).
    """
    existing = mailbox.get(int(message_id))
    if existing is None:
        raise ValueError("idempotency key %r: message %d not found" % (key, message_id))
    same_body = (existing.get("body") or "") == (body or "")
    same_lot = (existing.get("work_item_id") or "") == (work_item_id or "")
    if not (same_body and same_lot):
        raise ValueError("idempotency key %r already used for another message" % (key,))
    return int(message_id)


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
    after_commit: list | None = None,
    idempotency_key: str | None = None,
) -> int:
    """Dépose un message pour UN destinataire, l'écrit dans son fil. Renvoie son id.

    Les champs `signed_*` viennent de `authority.sign_message` : un message
    signé fixe son propre horodatage (c'est lui qui est couvert par la
    signature) et son échéance.

    `allow_structured` lève le refus des corps illisibles (tests et outils
    seulement). `thread=False` laisse l'écriture du fil à l'appelant (envoi à
    « all » regroupé en une entrée, import déjà écrit). `after_commit` (une
    liste) : l'écriture du fil y est ajoutée, à appeler par l'appelant après
    le commit de sa transaction — un envoi annulé n'en laisse aucune trace
    dans le fil externe. Projet du fil : celui du lot (`work_items.app`, L118c)
    quand le message est lié à un lot, sinon celui de l'expéditeur, sinon du
    destinataire — son équipe (`team`) s'il est gouverné par le canon, sinon
    son chantier —, sinon AMEESH_PROJECT, sinon `default` ; lot :
    `work_item_id`.

    #3892 : `idempotency_key` (une chaîne stable du client, rangée dans
    `payload.idempotency_key`) rejoue un envoi dont la réponse s'est perdue.
    Un message déjà déposé par le même expéditeur vers le même destinataire
    sous cette clé est rendu tel quel, jamais déposé une seconde fois ; le
    dépôt reste exclusif par clé (index 0497 de #3889), y compris pour un
    exécuteur médié qui n'a pas cette lecture préalable. Une clé réutilisée
    pour un autre corps ou un autre lot est refusée (`ValueError`), jamais
    rendue comme envoyée.
    """
    from .agent_names import resolve as resolve_agent_name
    sender = resolve_agent_name(sender, db=db, operation="mail.send")
    recipient = resolve_agent_name(recipient, db=db, operation="mail.send")
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()
        if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
            raise ValueError("invalid idempotency key: %r" % idempotency_key)
        if not storage.is_remote(db):
            mailbox = storage.of(db).mailbox
            existing = mailbox.by_idempotency_key(sender, recipient, idempotency_key)
            if existing is not None:
                return _replay_id(mailbox, existing, body, work_item_id, idempotency_key)
        payload = dict(payload or {})
        if payload.get("idempotency_key") not in (None, idempotency_key):
            raise ValueError("payload.idempotency_key does not match the key")
        payload["idempotency_key"] = idempotency_key
    fil.ensure_readable(body, allow_structured)
    row = storage.of(db).mailbox.send(
        sender, recipient, body, host=host, kind=kind, payload=payload,
        work_item_id=work_item_id, signature=signature, signature_key=signature_key,
        signed_payload=signed_payload, nonce=nonce, created_us=created_us,
        expires_us=expires_us)
    message_id = int(row["id"])
    # #1689 (N1) : tout dépôt local réveille le chemin rapide du destinataire
    # (#1604), pas seulement `mail send` : approbations, exécuteur, veille,
    # notifications système passent aussi par ici. Le marqueur ne se crée
    # jamais : un destinataire d'un autre hôte, ou jamais vu sur celui-ci, n'a
    # pas de chemin rapide à invalider et reste lu par les événements complets.
    cfg = getattr(db, "cfg", None)
    if cfg is not None:
        fastmail.bump(cfg, recipient)
    if thread:
        # Le message est déposé : à partir d'ici, rien ne doit le faire perdre
        # (fil.record ne lève jamais).
        meta = {"host": host, "signature_key": signature_key}
        if kind != "notify":
            meta["kind"] = kind
        meta.update(thread_meta or {})

        def record(cfg=db.cfg, target=db) -> None:
            fil.record(
                cfg, target, sender=sender, recipients=[recipient], text=body,
                ts=row.get("created_ts"),
                project=fil.project_for(cfg, fil.lot_project(target, work_item_id),
                                        row.get("sender_project"),
                                        row.get("recipient_project")),
                lot=work_item_id, mailbox_ids=[message_id], meta=meta)
        if after_commit is not None:
            after_commit.append(record)
        else:
            record()
    return message_id


#: nombre de non-lus lus par l'exécuteur à chaque sondage
UNREAD_LIMIT = 200


def unread(db: Db, recipient: str, limit: int = UNREAD_LIMIT) -> list[dict]:
    """Messages non remis, du plus ancien au plus récent (comme la v0)."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.unread")
    return storage.of(db).mailbox.unread(recipient, limit)


def unread_active(db: Db, recipient: str, limit: int = UNREAD_LIMIT) -> list[dict]:
    """L125 : les non-lus qui ouvriront un tour (hors courrier passif).

    Path B: `mailbox.unread_active` is not in contract 1.1, so a mediated
    executor reads `mailbox.unread` (admitted) and drops passive mail here,
    with the same rule as the SQL (`passive_reason`). Only the first
    `UNREAD_LIMIT` unread messages are looked at there."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.unread_active")
    st = storage.of(db)
    if getattr(st, "driver", None) == "mediated":
        rows = st.mailbox.unread(recipient, max(int(limit), UNREAD_LIMIT))
        return [row for row in rows if not passive_reason(row)][:int(limit)]
    return st.mailbox.unread_active(recipient, limit)


def unread_urgent(db: Db, recipient: str, limit: int = 50) -> list[dict]:
    """Messages non remis marqués `payload.urgent` (interruption de tour, 0018)."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.unread_urgent")
    return storage.of(db).mailbox.unread_urgent(recipient, limit)


def unread_human(db: Db, recipient: str, limit: int = UNREAD_LIMIT, *,
                 names: Sequence[str] | None = None) -> list[dict]:
    """L77/B13 : les non-lus d'un humain ACTIF, filtrés DANS la requête — rang 2.

    Un humain (`payload.human`, ancien expéditeur `human:*`, ou nom déclaré de
    l'hôte dans `names`, None = aucun) derrière plus de `UNREAD_LIMIT` non-lus
    ordinaires n'est plus masqué par le préfixe : `_pick` le sert avant la
    suite et la consigne en attente.

    L77/B14 : les messages passifs (copie `cc`, accusé…) sont écartés par la
    requête, comme par `passive_reason` : une copie humaine ne remplit pas la
    fenêtre devant un humain actif.

    Médié : `mailbox.unread_human` est au contrat (1.5) — le serveur filtre
    dans sa requête, il n'y a plus de lecture bornée aux 200 premiers non-lus
    pour trouver un humain."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.unread_human")
    return storage.of(db).mailbox.unread_human(recipient, limit, names=names)


def unread_human_page(db: Db, recipient: str, limit: int = UNREAD_LIMIT, *,
                      after_id: int | None = None,
                      names: Sequence[str] | None = None) -> list[dict]:
    """L77/B14 : les humains actifs après `after_id` — franchit un préfixe
    de plus de `UNREAD_LIMIT` humains retenus sur un serveur médié borné.

    La première page reste `unread_human` (contrat 1.5) ; les suivantes
    passent par `mailbox.unread_human_page` (contrat 1.6). Un ancien serveur
    refuse l'opération : l'appelant garde les humains déjà trouvés et
    s'arrête (lecture comme avant 1.5)."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db,
                                   operation="mail.unread_human_page")
    return storage.of(db).mailbox.unread_human_page(
        recipient, limit, after_id=after_id, names=names)


def unread_mid_turn(db: Db, recipient: str, limit: int = UNREAD_LIMIT, *,
                    include_human: bool = True) -> list[dict]:
    """L514/#885 : les non-lus qu'une remise mi-tour peut livrer — verdict,
    arbitrage, message d'un humain, urgent (`mid_turn`) —, filtrés DANS la
    requête : un verdict derrière plus de `UNREAD_LIMIT` non-lus est vu.

    ``include_human=False`` (L77/B11) : un tour résident ne remet pas les
    messages humains par le hook ; ils restent à la file du tour, qui les
    réserve et les solde à sa fin effective (rang 1/2, au moins une fois).

    Chemin médié : `mailbox.unread_mid_turn` n'est pas dans le contrat 1.1, il
    lit `mailbox.unread` (admis) et filtre ici, comme `unread_active` — au plus
    les `UNREAD_LIMIT` premiers non-lus, la limite du contrat."""
    st = storage.of(db)
    if getattr(st, "driver", None) == "mediated":
        rows = st.mailbox.unread(recipient, max(int(limit), UNREAD_LIMIT))
        return [row for row in rows
                if mid_turn(row) and (include_human or not is_human(row))][:int(limit)]
    return st.mailbox.unread_mid_turn(recipient, limit, include_human=include_human)


def get(db: Db, message_id: int) -> dict | None:
    """Un message par son id (l'écouteur NOTIFY ne porte que l'id)."""
    return storage.of(db).mailbox.get(message_id)


def get_received(db: Db, message_id: int, recipient: str) -> dict | None:
    """#5215 : le message `message_id`, si `recipient` l'a reçu.

    Un message est déposé une fois par destinataire : une copie (`--cc`) est
    une ligne de la boîte de son destinataire, avec son propre numéro. Seul
    ce destinataire lit la ligne — ni un tiers, ni l'expéditeur. Lecture en
    base, sans toucher `delivered_at` : le message n'est pas marqué lu."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.show")
    row = get(db, message_id)
    if row is None or (row.get("recipient") or "") != recipient:
        return None
    return row


def mark_delivered(db: Db, ids: Sequence[int]) -> int:
    """Marque remis (le hook les a injectés, ou l'exécuteur a fini son tour)."""
    return storage.of(db).mailbox.mark_delivered(ids)


#: durée de vie d'une réservation (s) : l'exécuteur lance le harnais et solde
#: en quelques secondes ; le hook émet aussitôt. Passé ce délai (panne), la
#: réservation peut être reprise et le message est signalé « re-livré ».
RESERVATION_TTL = 600.0
HOOK_RESERVATION_TTL = 60.0

#: L77/B6 : plafond de la réservation d'un message humain quand le tour n'a
#: pas de durée maximale (`turn_max_seconds` = 0). La réservation doit couvrir
#: un tour réaliste sans être infinie ; au-delà, la reprise par le hook vaut
#: mieux qu'une réservation qui ne s'éteint jamais.
HUMAN_RESERVATION_MAX = 24 * 3600.0


def human_reservation_ttl(turn_max_seconds: float | int | None) -> float:
    """L77/B6 : durée de la réservation d'un message humain.

    Un message humain n'est soldé qu'à la **fin** du tour (`_solde_humains`) :
    une réservation au TTL ordinaire expirerait pendant un tour plus long que
    10 min, et le hook pourrait reprendre le message (doublon, solde final
    refusé, pas de trace L78). La réservation couvre donc tout le tour, plus
    la marge de livraison ; sans durée maximale (0), un plafond fixe la borne.
    Elle reste liée au bail (runner, epoch) : un bail perdu la rend
    inutilisable même avant son échéance.
    """
    try:
        seconds = float(turn_max_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        return seconds + RESERVATION_TTL
    return HUMAN_RESERVATION_MAX


def new_token() -> str:
    """Jeton unique d'une réservation : seule la remise qui le porte la solde."""
    return uuid.uuid4().hex


def reserve(db: Db, recipient: str, owner: str | None, epoch: int | None, token: str,
            *, ids: Sequence[int] | None = None, porteur: str,
            ttl_seconds: float = RESERVATION_TTL, limit: int = 200,
            turn_id: str | None = None) -> list[dict]:
    """Réserve atomiquement des non-lus pour UNE remise (consigne ou hook).

    Protocole unique de l'exécuteur et du hook : réserver sous un jeton,
    montrer, puis solder (`deliver`) ou annuler (`release`) cette réservation
    et elle seule. La ligne du destinataire est verrouillée dans le registre
    et le bail (owner, epoch) contrôlé dans la même instruction ; un message
    déjà réservé (réservation active) n'est jamais réservé deux fois. Chaque
    message rendu porte `deja_consigne` : une réservation antérieure n'a
    jamais été soldée (panne), il est peut-être déjà vu.

    `turn_id` (#2894) : le tour de l'exécuteur qui réserve sa consigne ;
    écrit dans `meta.consigne.turn` (additif, sans migration), il relie la
    remise au document `ameesh-turn-context/1`. Un hook ne le passe pas.
    """
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.reserve")
    options = dict(ids=ids, porteur=porteur, ttl_seconds=ttl_seconds, limit=limit)
    if turn_id is not None:
        # a hook leaves the parameter out: its golden request is unchanged
        options["turn_id"] = turn_id
    return storage.of(db).mailbox.reserve(
        recipient, owner or None, epoch if owner else None, token, **options)


def deliver(db: Db, recipient: str, owner: str | None, epoch: int | None, token: str,
            ids: Sequence[int]) -> list[int]:
    """Solde la réservation `token` : remis, si le bail est toujours le sien.

    Rend les ids passés remis. Un id absent (bail perdu, réservation reprise)
    reste non remis : il sera re-livré, signalé.
    """
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.deliver")
    return storage.of(db).mailbox.deliver(recipient, owner or None,
                                          epoch if owner else None, token, ids)


def release(db: Db, recipient: str, token: str, ids: Sequence[int]) -> int:
    """Annule la réservation `token` : rien n'a été montré."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.release")
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


def _truncation_note(row: Any, retire: int) -> str:
    """#5215 : où lire la suite. Le fil est un fichier local par hôte ; la
    base porte le message entier, et `agent-mail show <id>` le rend à son
    destinataire. Le repli fichier n'a pas de numéro : il garde son fil."""
    message_id = row.get("id")
    if message_id is None:
        return ("\n[... truncated: %d more byte(s), full text in the thread]"
                % retire)
    return ("\n[... truncated: %d more byte(s); full text: agent-mail show %s]"
            % (retire, message_id))


def _readable_body(row: dict, limite: int | None = None) -> str:
    """Le corps lisible, comme dans le fil : jamais le payload structuré."""
    body = row.get("body") or ""
    if fil.unreadable_reason(body):
        return "[unreadable body, not reproduced: see the thread, message #%s]" % row.get("id")
    # L77 : un corps ne se fait jamais passer pour l'enveloppe écrite par ameesh.
    text = resident_queue.neutralise(fil.clean_text(body).strip("\n"))
    if limite is not None:
        text, retire = _tronque(text, limite)
        if retire:
            text += _truncation_note(row, retire)
    return text


def _verdict_note(db: Db | None, row: dict) -> str:
    """La mention d'autorité d'un message signé (R4) ; vide s'il ne l'est pas."""
    if db is None or not row.get("signature"):
        return ""
    from . import authority  # import tardif : authority n'est utile qu'aux messages signés
    try:
        verdict = authority.verify_message(db, normalize(row))
    except Exception as exc:  # une vérification impossible n'accorde rien
        return "  [⚠ signature cannot be verified: %s]" % exc
    if verdict.authority:
        return "  [owner's authority PROVEN (Ed25519 signature)]"
    if verdict.ok:
        return "  [signed by an agent: authentic, without the owner's authority]"
    return "  [⚠ signature NOT valid: %s]" % verdict.reason


def _block(db: Db | None, row: dict, limite: int | None = None, note: str | None = None,
           envelope: str | None = None) -> str:
    """Le rendu d'un message : en-tête (n°, expéditeur, heure, nature,
    autorité, re-livré) puis corps lisible cité ligne à ligne.

    `envelope` (L77) : la ligne d'enveloppe écrite par ameesh pour un message
    humain, placée AVANT l'en-tête du message — elle ne vient jamais du corps.
    """
    nature = _nature(row)
    relivre = ("  [re-delivered: already reserved for delivery before an outage, "
               "you may have handled it already]" if maybe_redelivered(row) else "")
    moment = fil.iso_local(row.get("created_ts") or 0.0)
    corps = _readable_body(row, limite)
    tete = (envelope + "\n") if envelope else ""
    return "%s— message #%s from %s, %s%s%s%s%s:\n%s" % (
        tete, row.get("id"), row.get("sender") or "?", moment, nature,
        _verdict_note(db, row) if note is None else note, relivre, forwarded_note(row),
        "\n".join("> " + line for line in corps.split("\n")))


def render_received(db: Db, row: dict) -> str:
    """#5215 : le rendu complet d'un message reçu, pour `agent-mail show` :
    le même bloc que la livraison, sans troncature (texte intégral)."""
    return _block(db, row)


def forwarded_note(row: Any) -> str:
    """La mention d'un message renvoyé d'une boîte morte (`ameesh mail
    forward`) : vide pour un message ordinaire."""
    origin = row.get("forwarded_from")
    if not origin:
        return ""
    return "  [forwarded: first addressed to %s, whose mailbox was no longer read]" % origin


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
    parts.append(adapters.TMP_NOTE)  # L162 : $TMPDIR, jamais /tmp en dur
    parts.append(adapters.VENV_NOTE)  # #2763 : jamais pip dans le venv partagé
    # L159b: with a lending VM, heavy suites go there; otherwise L131, heavy
    # suites capped per host by `ameesh heavy`
    parts.append(adapters.offload_note() or adapters.HEAVY_NOTE)
    return "\n\n".join(parts)


def _envelope_for(envelopes: dict[int, str] | None, row: dict) -> str | None:
    """L'enveloppe L77 d'un message, ou None (message ordinaire)."""
    if not envelopes:
        return None
    try:
        key = int(row.get("id"))
    except (TypeError, ValueError):
        return None
    return envelopes.get(key)


def prompt_for(db: Db | None, rows: Sequence[dict], *, kind: str,
               budget: int = PROMPT_MAX_BYTES, extra_remaining: int = 0,
               envelopes: dict[int, str] | None = None
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
        env = _envelope_for(envelopes, row)
        bloc = _block(db, row, note=note, envelope=env)
        if octets(_assemble(kind, blocks + [bloc], total - index - 1)) <= budget:
            blocks.append(bloc)
            continue
        if blocks:
            break
        # Premier message trop long à lui seul : corps tronqué jusqu'à tenir.
        limite = budget
        while True:
            bloc = _block(db, row, limite, note=note, envelope=env)
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
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.history")
    return storage.of(db).mailbox.history(recipient, limit)


def staging_emission(db: Db, recipient: str, sender: str, key: str) -> int | None:
    """#4180 : retrouve le signal staging, même hors des derniers courriers."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.staging_emission")
    return storage.of(db).mailbox.staging_emission(recipient, sender, key)


def review_requests(db: Db, recipient: str, lot: int | str) -> list[dict]:
    """#2504 : les demandes de relecture du destinataire pour ce lot.

    Lues par une requête filtrée sur (destinataire, `kind = 'request'`, lot),
    jamais par `history` : cette dernière borne aux N derniers messages avant
    tout filtrage, si bien qu'une demande ancienne du même lot disparaissait
    sous un fort volume de courrier et une re-désignation en renvoyait une
    seconde. Aucune borne ici : le lot est la portée."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.review_requests")
    return storage.of(db).mailbox.review_requests(recipient, str(int(lot)))


def settle_review_requests(db: Db, recipient: str, lot: int | str) -> list[int]:
    """#4779 : solde les demandes de relecture encore non remises d'un siège.

    Le retrait d'un siège (`work reviewer <lot> <agent> --remove`) rend caduque
    la demande déposée chez le relecteur : sans ce règlement, le message reste
    en tête de sa boîte et le premier tour du canari de #4770 aurait ouvert une
    relecture que plus personne n'attend. Le message est marqué remis, comme
    une demande à échéance (`request_expires_ts`, `drop_expired`) : il reste
    dans l'historique et dans le fil, aucun tour ne démarre. Rend les ids
    effectivement vus non remis (vide si le siège n'avait aucune demande).
    """
    rows = review_requests(db, recipient, lot)
    ids = []
    for row in rows or ():
        # Une demande de décision (`payload.decision`) est un `request` lié au
        # lot, pas une demande de relecture : la solder retirerait une question
        # posée à l'owner. Seules les charges de relecture comptent.
        payload = message_payload(row)
        if not isinstance(payload, dict) or not (
                "review_request" in payload or "review_designation" in payload):
            continue
        if row.get("id") is not None and row.get("delivered_ts") is None:
            ids.append(int(row["id"]))
    if not ids:
        return []
    if not mark_delivered(db, ids):
        # Toutes les demandes ont été remises entre la lecture et l'écriture :
        # un tour les a prises, il n'y a rien à solder ni à tracer.
        return []
    return ids


def delivered_for_lot(db: Db, recipient: str, lot, limit: int) -> list[dict]:
    """#2539 : les derniers messages déjà remis de ce lot, filtrés dans la
    requête (destinataire + lot), sans fenêtre de temps. Non admis en mode
    médié (contrat fermé) : l'appelant retombe sur les non-lus."""
    from .agent_names import resolve as resolve_agent_name
    recipient = resolve_agent_name(recipient, db=db, operation="mail.delivered_for_lot")
    return storage.of(db).mailbox.delivered_for_lot(recipient, str(lot or "").strip(), limit)


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


#: #2814: the EXPLICIT request deadline, in the message payload. It is NOT
#: the signature's `signature_expires_at` (a TTL for the signature, 24 h by
#: default): a signed owner order must never disappear because its signature
#: aged. A client that wants a deadline sets this key (absolute epoch
#: seconds), e.g. `agent-mail send … --request-expires 2h`.
REQUEST_DEADLINE_KEY = "request_expires_ts"


def request_deadline(row: Any) -> float | None:
    """The explicit request deadline of a message (epoch seconds), or None.

    Only `payload.request_expires_ts` counts; `signature_expires_ts` is the
    signature's own TTL and never a request deadline. A missing, non-numeric,
    non-finite or non-positive value means "no deadline" (never guessed)."""
    value = _payload(row).get(REQUEST_DEADLINE_KEY)
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    moment = float(value)
    if moment != moment or moment <= 0:
        return None
    return moment


def request_expired(row: Any, now: float | None = None) -> bool:
    """#2814: has the message's explicit request deadline passed?

    No deadline (the ordinary case, including every signed message that does
    not carry one) means it never expires while queued."""
    deadline = request_deadline(row)
    return deadline is not None and deadline <= (time.time() if now is None else float(now))


def drop_expired(db: Db, rows: Sequence[dict]) -> int:
    """#2814 : solde des requêtes à échéance explicite dépassée, sans tour.

    Rend le nombre de messages marqués remis. L'échéance est celle que
    l'expéditeur a posée (`request_expires_ts`) ; le message reste dans
    l'historique et le fil, et l'appelant prévient l'expéditeur avant de le
    solder. Une erreur de base est laissée à l'appelant (le tour est différé,
    rien n'est consommé)."""
    ids = [int(row["id"]) for row in rows or ()
           if row.get("id") is not None and request_expired(row)]
    return mark_delivered(db, ids) if ids else 0


# --------------------------------------------------------------------------
# L125 : courrier regroupé — ce qui réveille, ce qui attend le tour suivant
# --------------------------------------------------------------------------

#: clés du payload qui rendent un message PASSIF : déposé sans réveil, il est
#: lu au tour suivant de son destinataire (dans la consigne du tour, ou par le
#: hook pendant un tour) — accusé de réception, copie, diffusion à tous,
#: conflit signalé par le radar (L160c), échec de CI peut-être hérité faute
#: d'exécution relevée de la base (L161c), information sans geste attendu
#: (#3451 : avis de prise automatique aux pilotes, compte rendu sans demande,
#: `agent-mail send --info`). Un message `urgent` n'est jamais passif.
PASSIVE_KEYS = ("ack", "cc", "broadcast", "radar", "ci_passif", "info")

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
    """L125 : `ack`, `cc`, `broadcast`, `radar`, `ci_passif` ou `info` si ce message
    n'ouvre pas de tour à lui seul ; '' s'il réveille son destinataire."""
    payload = _payload(row)
    if payload.get("urgent"):
        return ""
    for key in PASSIVE_KEYS:
        if payload.get(key):
            return key
    return ""


def is_human(row: Any) -> bool:
    """L125 : message d'un humain, marqué à l'envoi — il réveille tout de suite.

    L77/B1 : un expéditeur déjà qualifié ``human:<id>`` est humain même si une
    ancienne ligne n'a pas le marqueur (les décisions l'écrivent ainsi)."""
    sender = str(row.get("sender") or row.get("from") or "")
    return bool(_payload(row).get("human")) or sender.startswith("human:")


def human_account(row: Any) -> str:
    """L77 : la preuve d'entrée d'un message humain (``user@host``), ou ''.

    Écrite par le CLI au dépôt, à partir du compte Unix réel et de l'hôte du
    processus : le lecteur ne la recalcule jamais avec son propre compte."""
    value = _payload(row).get("human_account")
    return value.strip() if isinstance(value, str) else ""


#: L514: payload keys of a message the sender marked as a review verdict
#: (`agent-mail send --verdict`) or an arbitration (`--arbitration`); #5006:
#: the notice of a reassignment to the previous assignee (`work.assign`).
#: #5025 : `reassigned` est une clé de charge utile, jamais une option du CLI
#: (sinon un corps commençant par `--reassigned` serait avalé) : les options
#: que `cmd_send` reconnaît sont `MID_TURN_OPTIONS`, ci-dessous.
MID_TURN_KEYS = ("verdict", "arbitration", "reassigned")

#: #5025 : les marques de la L514 que `agent-mail send` accepte en option.
MID_TURN_OPTIONS = ("verdict", "arbitration")


def mid_turn(row: Any) -> bool:
    """L514: delivered DURING a dsh or codex turn, at its next safe point
    (PostToolUse hook): a verdict, an arbitration, a human's message (a
    decision answer included) or an urgent one. Anything else waits for the
    end of the turn (L125)."""
    payload = _payload(row)
    return bool(payload.get("urgent") or payload.get("human")
                or any(payload.get(key) for key in MID_TURN_KEYS))


def passive_label(row: Any) -> str:
    """Le genre d'un message passif, pour son en-tête ; '' sinon."""
    reason = passive_reason(row)
    if reason == "cc":
        cible = str(_payload(row).get("cc") or "")
        return "copy of a message to %s" % (cible if NAME_RE.match(cible) else "?")
    return {"ack": "acknowledgement", "broadcast": "broadcast to all",
            "radar": "conflict radar", "ci_passif": "CI without base reference",
            "info": "information, no action expected"}.get(reason, "")


def _nature(row: Any) -> str:
    """La nature d'un message dans son en-tête : passif (L125), événement,
    urgent ; '' pour un message ordinaire."""
    passif = passive_label(row)
    if passif:
        return " (%s)" % passif
    if is_event(row):
        return " (event%s)" % (", urgent" if is_urgent(row) else "")
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

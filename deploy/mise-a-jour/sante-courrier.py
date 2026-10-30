#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Mail health of one host, for the staging update (#1155).

Read-only checks, then one real delivery probe:

* stuck mail: an ACTIVE message (one that would open a turn) still undelivered
  past ``--souffrance`` while a DRIVEN recipient (live lease) is idle and not
  paused by the operator on the host -> KO. The live lease is required: no
  runner can open the turn of an unleased persona, so its pile is not a
  delivery-chain failure — it is named separately in ``sans_executeur``;
* operational recipient without executor: an ACTIVE message older than
  ``--souffrance`` at an executable persona (``mode=execute``) that has no
  live lease and is not deliberately paused (idle, queued, or a stale
  ``running``) -> KO too, because operational mail must reach an active
  recipient; the reason is the missing runner, never a delivery delay. The
  fix named is to start the runner or resume the agent, not to widen the
  bound. A ``blocked`` agent without a live lease and outside a budget pause
  is named with its error, never silently left out (#2899);
* human profile without executor (``mode=externe`` human sessions, owner key
  holder: ``key_role=owner`` or ``harness=none``, by construction never run):
  its pending mail is reported in ``canal_humain`` with the human it belongs
  to and the read-only command that shows it (``agent-mail inbox <name>``),
  never as an agent dead letter, and never blocking: the human channel is the
  owner alert/decision channel, not the agent mailbox (#2899). A chat session
  (``chat-<human>``) is a human channel too, but it reads its mail when the
  human opens the conversation: the entry names ``canal: "chat"`` and claims no
  pushed alert (``alerte: null``), like ``exploitation.alerts`` which excludes
  it (#2957). A profile that declares no responsible falls back to the owner's
  human channel, read from the registry (never a hard-coded identity);
* probe: a system message is dropped to one DRIVEN and FREE persona of the host
  (live lease, not in a turn, not paused by the operator); within ``--attente``
  the persona must answer it with a tool call (``agent-mail send <self> --ack
  <token>``) AND the turn that received it must end -> KO otherwise. A persona
  already in a turn only queues the message until its current turn ends, and a
  persona paused by the operator keeps ``status`` idle but its runner refuses
  the turn: probing either would measure its queue, not the host's chain, so
  neither is chosen (#2190). When every driven persona is busy or paused, the
  verdict is ``sans-sonde`` with those targets named — never a KO caused by the
  wait. A target NAMED by the operator (``SONDE_<HOTE>``) that is busy or
  paused is waited for, within ``--attente-cible`` (300 s), then probed; still
  busy at the bound, another driven and free persona of the same pool
  replaces it, and the report names the replacement and why (``remplacement``,
  #4126). Only when none is free is the verdict ``sans-sonde``. Delivery alone
  is recorded when the harness
  starts, before a refused harness exits 127 (#1460): only the answer proves a
  real turn with working tools, and a failed or blocked turn after delivery is
  KO at once. The answer alone is not enough either: a blocked harness can
  answer the tool and then stay alive until its timeout, so the turn's end
  (``last_turn_ts``) is required too — unless the target is working a lot in
  ``build``/``qa``: that long turn does real work, the report names it (``travail``)
  and the probe is conclusive (#4181);
* dead letters (recipient stopped or unknown) are reported, never blocking.

The JSON report (`--json`) is the structured proof consumed by `actif.py`
(#1323): it names the host and the commit, and carries the probe message, its
recipient, the recipient's host and its delivery delay. Every verdict, including
a probe error, is a JSON report: the staging trace and its notification keep the
structured KO instead of a bare "health failed" (#2190). A `sans-sonde` verdict
never proves a real delivery.

With `--serveur` (the mesh server, which has no executant: `PERSONAS_VM` empty,
like manaty-mesh-1), no local persona exists to receive the probe: it then
targets a DRIVEN and FREE persona of ANOTHER host, and the report names that
host (`hote_destinataire`). The delivery goes through the server's mesh
database, so `actif.py` accepts it only after the server's restart (#1323, avis
R2 B1). A server that does have executants keeps the local rule; a host with
agents and no free driven persona is still a `sans-sonde` refusal.

Run it with an ameesh venv and a route to the mesh database: the workstation,
or an agents VM (`staging.sh` runs it there when launched elsewhere, #3976).
When the configuration or the database cannot be reached from that machine,
nothing was observed on the probed host: the verdict is ``non-concluant``
(exit 3), never a KO that would roll the host back — except with
``--serveur``, whose database is the probed host's own. No secret is read or
printed.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time

from ameesh import budget, chat, config, db as db_mod, mail, registry, storage, work

#: seconds allowed for delivery, tool answer and a clean turn end (bounded)
DEFAULT_ATTENTE = 300.0
#: an active message older than this while its recipient is idle is stuck
DEFAULT_SOUFFRANCE = 900.0
#: seconds a busy or paused named target is waited for before a replacement
DEFAULT_ATTENTE_CIBLE = 300.0
#: seconds between two registry reads while the named target is waited for
INTERVALLE_CIBLE = 5.0
#: etats d'un lot qui prouvent un tour de travail (pas une simple prise, #4181)
ETATS_TRAVAIL = ("build", "qa")

PROBE_TEMPLATE = """Sonde de sante staging (#1155), hote {hote}, commit {sha}.
Message automatique du systeme : il verifie qu'un tour reel s'execute sur cet
hote, outils compris. Seule action attendue : lance exactement la commande
ci-dessous (accuse sans reveil, a toi-meme), puis termine ton tour sans rien
faire d'autre :

agent-mail send {nom} --ack {jeton}
"""


def personas(db, hote: str | None = None) -> list[dict]:
    """Non-stopped, non-retired execute personas, on ``hote`` (all hosts when None).

    Sorted by host then name: the auto probe target is deterministic, whether
    the host is the one measured or the pool of the other hosts.
    """
    rows = registry.overview(db)
    out = [r for r in rows
           if (hote is None or r.get("host") == hote)
           and (r.get("mode") or "execute") == "execute"
           and r.get("status") != "stopped"
           and not r.get("retired_ts")]
    return sorted(out, key=lambda r: (r.get("host") or "", r["name"]))


def driven(row: dict, now: float) -> bool:
    """A live lease: a runner holds this persona right now."""
    return (not row.get("retired_ts") and bool(row.get("lease_owner"))
            and float(row.get("lease_expires_ts") or 0) > now)


def libre(row: dict) -> bool:
    """The persona can open a turn now: not paused, not already in one.

    A persona in a turn only receives mail when that turn ends; a persona
    paused by the operator keeps ``status`` idle but its runner refuses the
    turn. Probing either would measure a queue, not the host's chain (#2190).
    """
    return ((row.get("status") or "idle") == "idle"
            and not row.get("operator_paused"))


def motif_non_libre(row: dict) -> str:
    """Why a persona cannot take the probe now (state named in the verdict)."""
    if row.get("operator_paused"):
        return "pause operateur"
    return row.get("status") or "?"


def pourquoi_occupees(portee: str, menes: list[dict]) -> str:
    """Why no probe target was used: every driven persona is busy or paused."""
    noms = ", ".join("%s (%s)" % (r["name"], motif_non_libre(r))
                     for r in menes)
    return ("aucune persona menee libre %s : %d non libre(s) (%s) ; "
            "la file d'un agent occupe ou en pause ne mesure pas la chaine"
            % (portee, len(menes), noms))


def cible_occupee(row: dict, nommee: bool = False) -> str:
    """A target cannot take the probe: its queue would measure nothing."""
    return ("cible %s%s occupee (%s) ; la file d'un agent occupe ou en pause "
            "ne mesure pas la chaine" % ("nommee " if nommee else "",
                                        row["name"], motif_non_libre(row)))


def stuck(db, rows: list[dict], souffrance: float, now: float) -> list[dict]:
    """Active undelivered messages older than the bound, driven idle recipients.

    A persona paused by the operator is idle but deliberately takes no turn:
    its waiting mail is expected, not a delivery failure (#2190). A persona
    without a live lease cannot open a turn at all: its pile belongs to
    ``sans_executeur``, not to the delivery chain (#2899).
    """
    out = []
    for row in rows:
        if (row.get("status") or "idle") != "idle" or row.get("operator_paused"):
            continue
        if not driven(row, now):
            continue
        for message in mail.unread_active(db, row["name"]):
            if mail.maybe_redelivered(message):
                continue  # a reservation is showing it right now
            age = now - float(message.get("created_ts") or now)
            if age >= souffrance:
                out.append({"destinataire": row["name"], "message": message.get("id"),
                            "age_s": round(age, 1)})
    return out


def sans_executeur(db, rows: list[dict], souffrance: float, now: float) -> list[dict]:
    """Active mail at an operational persona that no live lease serves.

    The recipient is an executable agent (``personas`` already filtered the
    mode and the stopped ones) and is not deliberately paused. Its mail cannot
    be delivered, not because the chain is slow but because no runner opens
    its turn: the report must name that cause and the fix (start the runner or
    ``ameesh resume`` the agent), never let it pass for a delivery delay
    (#2899). ``operator_paused`` and a budget pause are deliberate waits: their
    mail is expected to wait. A ``blocked`` agent without a live lease and
    without a budget pause is an operational failure: its mail is named too,
    with its error (never silently dropped from the report). A human profile
    that would also match ``mode=execute`` (owner key, ``harness=none``) is not
    an executable agent to restart: it belongs to ``canal_humain`` only, so it
    is never counted twice (#2957).
    """
    out = []
    for row in rows:
        if row.get("retired_ts") or humain_sans_executeur(row):
            continue
        statut = row.get("status") or "idle"
        if row.get("operator_paused") or statut in ("stopped", "dead"):
            continue
        if statut == "blocked" and (row.get("status_text") or "").startswith(
                budget.PAUSE_PREFIX):
            continue
        if driven(row, now):
            continue
        messages = [m for m in mail.unread_active(db, row["name"])
                    if not mail.maybe_redelivered(m)
                    and now - float(m.get("created_ts") or now) >= souffrance]
        if not messages:
            continue
        oldest = max(now - float(m.get("created_ts") or now) for m in messages)
        dernier_tour = float(row.get("last_turn_ts") or 0)
        motif = "aucun bail vivant"
        if statut == "blocked":
            motif = "bloqué : %s" % ((row.get("last_error") or row.get("status_text")
                                      or "sans bail")[:200])
        out.append({"destinataire": row["name"], "n": len(messages),
                    "plus_ancien_s": round(oldest, 1),
                    "statut": statut,
                    "dernier_tour_s": (round(now - dernier_tour, 1)
                                       if dernier_tour else None),
                    "motif": motif})
    return out


def humain_sans_executeur(row: dict) -> bool:
    """A human identity with no executor (never claimed by a runner).

    ``mode=externe`` is a human session (the chat, the operator's agents);
    ``key_role=owner`` is the owner's key row and ``harness=none`` the stopgap
    row the key was first registered on (#330). Such a row is never claimed by
    a runner: its mailbox is not a delivery target, and its mail must be
    surfaced on the human channel, not counted as an agent dead letter
    (#2899).
    """
    return ((row.get("mode") or "execute") != "execute"
            or (row.get("key_role") or "") == "owner"
            or (row.get("harness") or "") == "none")


def toutes_personas(db, hote: str) -> list[dict]:
    """Toutes les personas placees sur ``hote``, meme arretees : une boite
    morte d'une persona arretee doit apparaitre (avis R1 N1)."""
    return [r for r in registry.overview(db) if r.get("host") == hote]


def dead_letters(db, hote: str, rows: list[dict] | None = None) -> list[dict]:
    """Dead mailboxes of this host's operational personas.

    Human profiles without executor are excluded: their pending mail is an
    explicit human channel (``canal_humain``), not a dead agent mailbox
    (#2899)."""
    rows = toutes_personas(db, hote) if rows is None else rows
    noms = {r["name"] for r in rows if not humain_sans_executeur(r)}
    out = []
    for letter in storage.of(db).mailbox.dead_letters():
        if letter.get("recipient") in noms:
            out.append({"destinataire": letter["recipient"],
                        "n": int(letter.get("n") or 0)})
    return out


def owner_human(db) -> str | None:
    """Le canal humain du propriétaire, pour un profil sans responsable.

    Le registre porte la ligne du propriétaire (``key_role=owner``) et son
    humain responsable : aucun nom n'est codé en dur (#2957). Hors du canal
    humain, un tel profil n'a personne à qui pousser l'alerte."""
    for row in registry.overview(db):
        if (row.get("key_role") or "") == "owner":
            return row.get("responsible") or "human:%s" % row.get("name")
    return None


def canal_humain(db, hote: str, now: float,
                 rows: list[dict] | None = None) -> list[dict]:
    """Pending mail of this host's human profiles without executor.

    Non-blocking, but never hidden: the report names the human, the age, and
    the read-only command that shows the mail without marking it read. The
    owner alert (``stopped_with_mail``) is the pushed channel; this section is
    the structured, explicit one (#2899). A chat session is a human channel
    that reads its mail when the human opens it: ``canal`` says ``chat`` and
    ``alerte`` stays null, because no pushed alert covers it (#2957). A profile
    without a declared responsible falls back to the owner's channel from the
    registry."""
    rows = toutes_personas(db, hote) if rows is None else rows
    out = []
    fallback = None
    for row in rows:
        if row.get("retired_ts") or not humain_sans_executeur(row):
            continue
        messages = mail.unread_active(db, row["name"])
        if not messages:
            continue
        oldest = max(now - float(m.get("created_ts") or now) for m in messages)
        responsable = row.get("responsible") or None
        chat_session = chat.is_chat_agent(row)
        if not responsable and not chat_session:
            if fallback is None:
                fallback = owner_human(db) or ""
            responsable = fallback or None
        entry = {"destinataire": row["name"], "n": len(messages),
                 "plus_ancien_s": round(oldest, 1),
                 "responsable": responsable,
                 "canal": "chat" if chat_session else (responsable or "human:?"),
                 "alerte": (None if chat_session else
                            ("stopped_with_mail"
                             if (row.get("status") or "") == "stopped"
                             else "idle_with_mail")),
                 "lecture": "agent-mail inbox %s" % row["name"]}
        if len(messages) >= mail.UNREAD_LIMIT:
            # la lecture est bornee : le compte est un plancher, pas un total
            entry["tronque"] = True
        out.append(entry)
    return out


def choose(rows: list[dict], sonde: str, now: float):
    """(row, pourquoi) : the probe target, or (None, reason it is skipped)."""
    if sonde not in ("auto", "jamais"):
        row = next((r for r in rows if r["name"] == sonde), None)
        if row is None:
            raise SystemExit("sonde : %s n'est pas une persona de l'hote" % sonde)
        if not libre(row):
            return None, cible_occupee(row, nommee=True)
        if not driven(row, now):
            return None, "cible nommee %s sans bail vivant" % row["name"]
        return row, None
    if sonde == "jamais":
        return None, "sonde desactivee"
    menes = [r for r in rows if driven(r, now)]
    if not menes:
        return None, "aucune persona menee (bail vivant) sur cet hote"
    libres = [r for r in menes if libre(r)]
    if not libres:
        return None, pourquoi_occupees("sur cet hote", menes)
    return libres[0], None


def choisir_croise(rows: list[dict], hote: str, cible: str, now: float):
    """(row, pourquoi) : cible d'une sonde croisee pour un hote sans executant.

    Le serveur du mesh n'a aucune persona locale : sa chaine se prouve par la
    remise d'une sonde a une persona menee d'un AUTRE hote, qui traverse sa
    base (avis R2 B1 #1323). Une cible nommee doit exister sur un autre hote,
    sinon le refus est explicite — jamais de repli silencieux.
    """
    autres = [r for r in rows if r.get("host") != hote]
    if cible not in ("auto", "jamais"):
        row = next((r for r in autres if r["name"] == cible), None)
        if row is None:
            raise SystemExit(
                "sonde : %s n'est pas une persona d'un autre hote" % cible)
        if not libre(row):
            return None, cible_occupee(row, nommee=True)
        if not driven(row, now):
            return None, "cible nommee %s sans bail vivant" % row["name"]
        return row, None
    menes = [r for r in autres if driven(r, now)]
    if not menes:
        return None, "aucune persona menee (bail vivant) sur un autre hote"
    libres = [r for r in menes if libre(r)]
    if not libres:
        return None, pourquoi_occupees("sur un autre hote", menes)
    return libres[0], None


def attendre_cible(db, nom: str, borne: float):
    """(row, attendu_s, pourquoi) : the named target once free, or why not.

    #4126 : a named target in a turn (or paused) only queues the probe; it is
    re-read until it is free, within ``borne`` seconds, then probed. A target
    gone or without a live lease is not waited for: no turn will free it.
    """
    debut = time.monotonic()
    while True:
        etat = registry.get(db, nom)
        attendu = round(time.monotonic() - debut, 1)
        if etat is None or not driven(etat, time.time()):
            return None, attendu, "cible nommee %s sans bail vivant" % nom
        if libre(etat):
            return etat, attendu, None
        reste = borne - (time.monotonic() - debut)
        if reste <= 0:
            return None, attendu, "cible nommee %s occupee (%s) apres %ss d'attente" % (
                nom, motif_non_libre(etat), attendu)
        time.sleep(min(INTERVALLE_CIBLE, reste))


def revalider(db, candidats: list[dict]):
    """(row, pourquoi) : the first candidate still driven and free when re-read.

    The selection comes from a registry snapshot: a candidate may have entered
    a turn or been paused since. A last read avoids queueing the probe behind a
    busy agent (#2190); the next free candidate is then taken (#4126). With no
    candidate left, the reason is the first candidate's.
    """
    premier = None
    for row in candidats:
        etat = registry.get(db, row["name"])
        if etat is None or not driven(etat, time.time()):
            motif = "cible %s sans bail vivant" % row["name"]
        elif not libre(etat):
            motif = cible_occupee(etat)
        else:
            return row, None
        premier = premier or motif
    return None, premier


def selection(db, pool: list[dict], sonde: str, borne: float, now: float,
              croise: bool = False):
    """(row, pourquoi, remplacement) : the probe target among ``pool``.

    ``auto`` takes the first driven and free persona, re-read before use. A
    named target is waited for (``attendre_cible``) ; still not free, it is
    replaced by another driven and free persona of the pool, and
    ``remplacement`` names both and the reason (#4126, no family imposed, as
    #4089). With none free, the verdict stays ``sans-sonde`` (canon §3).
    ``croise`` : the pool is the other hosts' personas (server without
    executant, avis R2 B1 #1323).
    """
    portee = "sur un autre hote" if croise else "sur cet hote"
    if sonde == "jamais":
        return None, "sonde desactivee", None
    if sonde == "auto":
        menes = [r for r in pool if driven(r, now)]
        if not menes:
            return None, "aucune persona menee (bail vivant) %s" % portee, None
        libres = [r for r in menes if libre(r)]
        if not libres:
            return None, pourquoi_occupees(portee, menes), None
        cible, pourquoi = revalider(db, libres)
        return cible, pourquoi, None
    if not any(r["name"] == sonde for r in pool):
        raise SystemExit("sonde : %s n'est pas une persona %s" % (
            sonde, "d'un autre hote" if croise else "de l'hote"))
    etat, attendu, motif = attendre_cible(db, sonde, borne)
    if etat is not None:
        return etat, None, None
    # Re-read alternatives after the wait; the snapshot may still say busy.
    autres = [r for r in pool if r["name"] != sonde]
    cible, _ = revalider(db, autres)
    if cible is None:
        return None, "%s ; aucune autre persona menee libre" % motif, None
    remplacement = {"cible_nommee": sonde, "remplacante": cible["name"],
                    "raison": motif, "attente_s": attendu}
    return cible, "remplacante %s : %s" % (cible["name"], motif), remplacement


def answered(db, name: str, jeton: str) -> bool:
    """The persona's own ack, even if its next hook already delivered it."""
    return any(m.get("sender") == name and jeton in (m.get("body") or "")
               for m in mail.history(db, name, limit=mail.UNREAD_LIMIT))


def failed_turn(db, name: str, delivered: float, row: dict | None = None) -> str:
    """Why the turn that received the probe failed, or "" (same DB clock)."""
    row = (registry.get(db, name) or {}) if row is None else row
    if float(row.get("last_turn_ts") or 0) < delivered or attente_budget(row):
        return ""
    if row.get("last_error") or row.get("status") == "blocked":
        return str(row.get("last_error") or row.get("status_text") or "blocked")
    return ""


def attente_budget(row: dict) -> str:
    """The deliberate wait under ``budget : `` (start gap, account pause), or "".

    #4089 : the runner holds the turn after the start gap of the account
    (#3779) and writes it under ``budget.PAUSE_PREFIX``; that is a wait, not a
    failed turn (as ``sans_executeur`` already reads it).
    """
    for texte in (row.get("last_error") or "",
                  row.get("status_text") if row.get("status") == "blocked" else ""):
        if (texte or "").startswith(budget.PAUSE_PREFIX):
            return texte
    return ""


def turn_finished(db, name: str, delivered: float, row: dict | None = None) -> bool:
    """The turn that received the probe ended (same DB clock, #1460)."""
    row = (registry.get(db, name) or {}) if row is None else row
    return float(row.get("last_turn_ts") or 0) >= delivered


def travail_en_cours(db, name: str) -> dict | None:
    """Le lot que la cible travaille, ou None (#4181).

    Un agent qui a répondu à la sonde et tient un lot en ``build``/``qa`` fait
    un tour long légitime : ce tour prouve la chaîne de l'hôte, il ne prouve
    pas un harnais bloqué (#1460). Un lot seulement pris (``intake``) ne suffit
    pas : rien ne dit encore que le tour travaille, et le garde de #1460
    (accusé puis harnais vivant jusqu'au timeout) reste entier.
    """
    for item in work.open_lots(db, name):
        if item.get("state") in ETATS_TRAVAIL:
            return item
    return None


def probe(db, row: dict, sha: str, attente: float,
          hote_mesure: str | None = None) -> dict:
    """Drop one system message and wait (bounded) for the answered, ended turn.

    An answered turn still running is conclusive when the target is working a
    lot (``build``/``qa``, #4181): the long turn does real work, and the
    ``travail`` report names it. Without that work, the #1460 guard applies:
    the answer must be followed by a turn end.

    ``hote_mesure`` is the health target named in the body and the payload; it
    defaults to the recipient's host (local probe). For a server without
    executant, the recipient lives on ANOTHER host (cross probe): the report
    then also names ``hote_destinataire`` (avis R2 B1 #1323).
    """
    if hote_mesure is None:
        hote_mesure = row.get("host") or ""
    jeton = "sonde-staging-%s" % secrets.token_hex(6)
    corps = PROBE_TEMPLATE.format(hote=hote_mesure, sha=(sha or "")[:9],
                                  nom=row["name"], jeton=jeton)
    debut = time.monotonic()
    message_id = mail.send(db, work.SYSTEM_SENDER, row["name"], corps, kind="event",
                           payload={"staging": {
                               "hote": hote_mesure, "sha": sha or "",
                               "destinataire_hote": row.get("host") or ""}})
    out = {"message": message_id, "destinataire": row["name"], "delai_s": None,
           "remise_s": None, "hote_destinataire": row.get("host") or "",
           "statut_cible": row.get("status") or "idle", "tour": "non remis",
           "concluant": True}
    fin = debut + attente
    repondu = False
    budget_vu = ""
    while time.monotonic() < fin:
        message = mail.get(db, message_id)
        livre = float((message or {}).get("delivered_ts") or 0)
        if livre:
            if out["remise_s"] is None:
                out["remise_s"] = round(time.monotonic() - debut, 1)
            repondu = repondu or answered(db, row["name"], jeton)
            # one registry read for both verdicts: a turn ending in error
            # between two reads must not be taken for a clean end
            etat = registry.get(db, row["name"]) or {}
            echec = failed_turn(db, row["name"], livre, etat)
            if echec:
                return dict(out, tour="tour en echec : %s" % echec[:200])
            budget_vu = attente_budget(etat) or budget_vu
            if not repondu:
                out["tour"] = "remis, sans reponse de l'outil"
            elif turn_finished(db, row["name"], livre, etat):
                # the answer proves the tool ran, the end of the turn proves the
                # harness finished it (dev4/47164: ack then alive until timeout)
                return dict(out, delai_s=round(time.monotonic() - debut, 1), tour="ok")
            else:
                travail = travail_en_cours(db, row["name"])
                if travail is not None:
                    # #4181 : le tour long travaille un lot pris ou repris
                    # pendant la sonde ; c'est un vrai tour, pas un harnais
                    # bloque (#1460). Le rapport nomme le lot.
                    return dict(out, delai_s=round(time.monotonic() - debut, 1),
                                tour="ok (travail en cours : lot #%s)"
                                % travail.get("id"),
                                travail={"lot": travail.get("id"),
                                         "state": travail.get("state")})
                out["tour"] = "reponse vue, tour non termine"
        time.sleep(2.0)
    # #4089 (canon §3) : seul un negatif est concluant. Remis sans tour
    # termine, ou retenu par une attente budget : non concluant, nomme.
    if out["remise_s"] is not None and (budget_vu or out["tour"] != (
            "remis, sans reponse de l'outil") or not turn_finished(
                db, row["name"], livre)):
        out["concluant"] = False
        if budget_vu:
            out["tour"] = "%s (attente %s)" % (out["tour"], budget_vu[:200])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sante du courrier d'un hote (#1155)")
    parser.add_argument("--hote", required=True)
    parser.add_argument("--sonde", default="auto",
                        help="auto (premiere persona menee libre), jamais, ou un nom")
    parser.add_argument("--serveur", action="store_true",
                        help="hote sans executant : sonde croisee vers un autre hote")
    parser.add_argument("--attente", type=float, default=DEFAULT_ATTENTE)
    parser.add_argument("--souffrance", type=float, default=DEFAULT_SOUFFRANCE)
    parser.add_argument("--attente-cible", type=float, default=DEFAULT_ATTENTE_CIBLE,
                        help="attente d'une cible nommee occupee avant une remplacante")
    parser.add_argument("--sha", default="", help="commit en cours d'installation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # le rapport existe des le depart : toute erreur (base, sonde, courrier)
    # sort en JSON KO, jamais en trace vide (#2190).
    rapport = {"hote": args.hote, "verdict": "ko", "personas": 0,
               "sha": args.sha, "souffrance": [], "sans_executeur": [],
               "boites_mortes": [], "canal_humain": [],
               "sonde": None, "pourquoi": None, "remplacement": None,
               "attente_s": args.attente,
               "souffrance_s": args.souffrance}
    conn = None
    erreur_base = None
    try:
        conn = db_mod.connect(config.load())
    except Exception as exc:  # noqa: BLE001 — l'outil n'a pas pu tourner
        if not args.serveur:
            # #3976 : configuration absente ou base injoignable depuis la
            # machine qui fait tourner la sonde : rien n'a ete constate sur
            # l'hote sonde. Non concluant (canon §3), jamais un KO qui
            # remettrait l'hote en arriere. Le serveur garde le KO : la base
            # est la sienne et vient d'etre redemarree.
            rapport.update(verdict="non-concluant",
                           erreur="%s: %s" % (type(exc).__name__, exc))
            print(json.dumps(rapport, ensure_ascii=False) if args.json
                  else "courrier %s : sonde non concluante (%s)"
                  % (args.hote, rapport["erreur"]))
            return 3
        erreur_base = exc
    try:
        if erreur_base is not None:
            raise erreur_base
        now = time.time()
        rows = personas(conn, args.hote)
        rapport["personas"] = len(rows)
        souffrance = stuck(conn, rows, args.souffrance, now)
        sans = sans_executeur(conn, rows, args.souffrance, now)
        toutes = toutes_personas(conn, args.hote)
        mortes = dead_letters(conn, args.hote, toutes)
        canal = canal_humain(conn, args.hote, now, toutes)
        try:
            if args.serveur and not rows:
                # le serveur sans executant n'a aucune persona locale : sa chaine
                # se prouve par une remise a travers lui, vers une persona menee
                # d'un autre hote (avis R2 B1 #1323).
                pool = [r for r in personas(conn) if r.get("host") != args.hote]
                cible, pourquoi, remplacement = selection(
                    conn, pool, args.sonde, args.attente_cible, now, croise=True)
            else:
                cible, pourquoi, remplacement = selection(
                    conn, rows, args.sonde, args.attente_cible, now)
        except SystemExit as exc:
            # une cible nommee absente est une erreur de sonde, pas une trace
            # vide : le rapport KO la porte (#2190).
            raise RuntimeError("sonde invalide : %s" % exc) from None
        rapport["remplacement"] = remplacement
        resultat = (probe(conn, cible, args.sha, args.attente, args.hote)
                    if cible is not None else None)
        verdict = "ok"
        if souffrance or sans or (resultat is not None and resultat["delai_s"] is None
                                  and resultat.get("concluant", True)):
            verdict = "ko"
        elif resultat is not None and resultat["delai_s"] is None:
            verdict = "non-concluant"
        elif resultat is None:
            verdict = "sans-sonde"
        rapport.update(verdict=verdict, souffrance=souffrance,
                       sans_executeur=sans, boites_mortes=mortes,
                       canal_humain=canal, sonde=resultat, pourquoi=pourquoi,
                       remplacement=remplacement)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — le rapport KO porte l'erreur
        rapport.update(verdict="ko",
                       erreur="%s: %s" % (type(exc).__name__, exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — la fermeture ne masque pas le verdict
                pass

    resultat = rapport["sonde"]
    if args.json:
        print(json.dumps(rapport, ensure_ascii=False))
    else:
        print("courrier %s : %d persona(s), souffrance %s, sans executant %s, "
              "boites mortes %s, canal humain %s"
              % (args.hote, rapport["personas"], rapport["souffrance"] or "aucune",
                 rapport["sans_executeur"] or "aucun",
                 rapport["boites_mortes"] or "aucune",
                 rapport["canal_humain"] or "aucun"))
        if rapport.get("erreur"):
            print("courrier %s : KO sonde (%s)" % (args.hote, rapport["erreur"]))
        elif resultat is None:
            print("courrier %s : sonde non passee (%s)"
                  % (args.hote, rapport["pourquoi"]))
        elif resultat["delai_s"] is None:
            print("courrier %s : %s sonde a %s, %s (message %s, attente %ss)"
                  % (args.hote, "KO" if resultat.get("concluant", True)
                     else "non concluante", resultat["destinataire"], resultat["tour"],
                     resultat["message"], args.attente))
        else:
            via = ""
            if resultat["hote_destinataire"] != args.hote:
                via = " (hote %s)" % resultat["hote_destinataire"]
            travail = resultat.get("travail") or {}
            if travail:
                via += " (travail en cours : lot #%s)" % travail.get("lot")
            print("courrier %s : sonde remise et accusee par %s en %ss (message %s)%s"
                  % (args.hote, resultat["destinataire"], resultat["delai_s"],
                     resultat["message"], via))
        if rapport.get("remplacement"):
            print("courrier %s : %s" % (args.hote, rapport["pourquoi"]))
    return {"ko": 1, "non-concluant": 3}.get(rapport["verdict"], 0)


if __name__ == "__main__":
    sys.exit(main())

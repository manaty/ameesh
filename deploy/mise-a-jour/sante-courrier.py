#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Mail health of one host, for the staging update (#1155).

Read-only checks, then one real delivery probe:

* stuck mail: an ACTIVE message (one that would open a turn) still undelivered
  past ``--souffrance`` while its recipient is idle on the host -> KO;
* probe: a system message is dropped to one DRIVEN persona of the host (live
  lease); within ``--attente`` the persona must answer it with a tool call
  (``agent-mail send <self> --ack <token>``) AND the turn that received it must
  end -> KO otherwise. Delivery alone is recorded when the harness starts,
  before a refused harness exits 127 (#1460): only the answer proves a real
  turn with working tools, and a failed or blocked turn after delivery is KO at
  once. The answer alone is not enough either: a blocked harness can answer the
  tool and then stay alive until its timeout, so the turn's end
  (``last_turn_ts``) is required too;
* dead letters (recipient stopped or unknown) are reported, never blocking.

Run it on the workstation with the workstation venv, where the mesh
configuration lives. No secret is read or printed.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time

from ameesh import config, db as db_mod, mail, registry, storage, work

#: seconds to wait for the probe to be delivered (bounded, never a loop)
DEFAULT_ATTENTE = 300.0
#: an active message older than this while its recipient is idle is stuck
DEFAULT_SOUFFRANCE = 900.0

PROBE_TEMPLATE = """Sonde de sante staging (#1155), hote {hote}, commit {sha}.
Message automatique du systeme : il verifie qu'un tour reel s'execute sur cet
hote, outils compris. Seule action attendue : lance exactement la commande
ci-dessous (accuse sans reveil, a toi-meme), puis termine ton tour sans rien
faire d'autre :

agent-mail send {nom} --ack {jeton}
"""


def personas(db, hote: str) -> list[dict]:
    """Non-stopped execute personas placed on ``hote``, sorted by name."""
    rows = registry.overview(db)
    out = [r for r in rows
           if r.get("host") == hote
           and (r.get("mode") or "execute") == "execute"
           and r.get("status") != "stopped"]
    return sorted(out, key=lambda r: r["name"])


def driven(row: dict, now: float) -> bool:
    """A live lease: a runner holds this persona right now."""
    return bool(row.get("lease_owner")) and float(row.get("lease_expires_ts") or 0) > now


def stuck(db, rows: list[dict], souffrance: float, now: float) -> list[dict]:
    """Active undelivered messages older than the bound, idle recipients."""
    out = []
    for row in rows:
        if (row.get("status") or "idle") != "idle":
            continue
        for message in mail.unread_active(db, row["name"]):
            if mail.maybe_redelivered(message):
                continue  # a reservation is showing it right now
            age = now - float(message.get("created_ts") or now)
            if age >= souffrance:
                out.append({"destinataire": row["name"], "message": message.get("id"),
                            "age_s": round(age, 1)})
    return out


def toutes_personas(db, hote: str) -> list[dict]:
    """Toutes les personas placees sur ``hote``, meme arretees : une boite
    morte d'une persona arretee doit apparaitre (avis R1 N1)."""
    return [r for r in registry.overview(db) if r.get("host") == hote]


def dead_letters(db, hote: str) -> list[dict]:
    """Dead mailboxes among this host's personas, stopped ones included."""
    noms = {r["name"] for r in toutes_personas(db, hote)}
    out = []
    for letter in storage.of(db).mailbox.dead_letters():
        if letter.get("recipient") in noms:
            out.append({"destinataire": letter["recipient"],
                        "n": int(letter.get("n") or 0)})
    return out


def choose(rows: list[dict], sonde: str, now: float):
    """(row, pourquoi) : the probe target, or (None, reason it is skipped)."""
    if sonde not in ("auto", "jamais"):
        row = next((r for r in rows if r["name"] == sonde), None)
        if row is None:
            raise SystemExit("sonde : %s n'est pas une persona de l'hote" % sonde)
        return row, None
    if sonde == "jamais":
        return None, "sonde desactivee"
    menes = [r for r in rows if driven(r, now)]
    if not menes:
        return None, "aucune persona menee (bail vivant) sur cet hote"
    return menes[0], None


def answered(db, name: str, jeton: str) -> bool:
    """The persona's own ack, even if its next hook already delivered it."""
    return any(m.get("sender") == name and jeton in (m.get("body") or "")
               for m in mail.history(db, name, limit=mail.UNREAD_LIMIT))


def failed_turn(db, name: str, delivered: float, row: dict | None = None) -> str:
    """Why the turn that received the probe failed, or "" (same DB clock)."""
    row = (registry.get(db, name) or {}) if row is None else row
    if float(row.get("last_turn_ts") or 0) < delivered:
        return ""
    if row.get("last_error") or row.get("status") == "blocked":
        return str(row.get("last_error") or row.get("status_text") or "blocked")
    return ""


def turn_finished(db, name: str, delivered: float, row: dict | None = None) -> bool:
    """The turn that received the probe ended (same DB clock, #1460)."""
    row = (registry.get(db, name) or {}) if row is None else row
    return float(row.get("last_turn_ts") or 0) >= delivered


def probe(db, row: dict, sha: str, attente: float) -> dict:
    """Drop one system message and wait (bounded) for the answered, ended turn."""
    jeton = "sonde-staging-%s" % secrets.token_hex(6)
    corps = PROBE_TEMPLATE.format(hote=row.get("host") or "", sha=(sha or "")[:9],
                                  nom=row["name"], jeton=jeton)
    debut = time.monotonic()
    message_id = mail.send(db, work.SYSTEM_SENDER, row["name"], corps, kind="event",
                           payload={"staging": {"hote": row.get("host"), "sha": sha or ""}})
    out = {"message": message_id, "destinataire": row["name"], "delai_s": None,
           "tour": "non remis"}
    fin = debut + attente
    repondu = False
    while time.monotonic() < fin:
        message = mail.get(db, message_id)
        livre = float((message or {}).get("delivered_ts") or 0)
        if livre:
            repondu = repondu or answered(db, row["name"], jeton)
            # one registry read for both verdicts: a turn ending in error
            # between two reads must not be taken for a clean end
            etat = registry.get(db, row["name"]) or {}
            echec = failed_turn(db, row["name"], livre, etat)
            if echec:
                return dict(out, tour="tour en echec : %s" % echec[:200])
            if not repondu:
                out["tour"] = "remis, sans reponse de l'outil"
            elif turn_finished(db, row["name"], livre, etat):
                # the answer proves the tool ran, the end of the turn proves the
                # harness finished it (dev4/47164: ack then alive until timeout)
                return dict(out, delai_s=round(time.monotonic() - debut, 1), tour="ok")
            else:
                out["tour"] = "reponse vue, tour non termine"
        time.sleep(2.0)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sante du courrier d'un hote (#1155)")
    parser.add_argument("--hote", required=True)
    parser.add_argument("--sonde", default="auto",
                        help="auto (premiere persona menee), jamais, ou un nom")
    parser.add_argument("--attente", type=float, default=DEFAULT_ATTENTE)
    parser.add_argument("--souffrance", type=float, default=DEFAULT_SOUFFRANCE)
    parser.add_argument("--sha", default="", help="commit en cours d'installation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = db_mod.connect(config.load())
    try:
        now = time.time()
        rows = personas(conn, args.hote)
        souffrance = stuck(conn, rows, args.souffrance, now)
        mortes = dead_letters(conn, args.hote)
        cible, pourquoi = choose(rows, args.sonde, now)
        resultat = probe(conn, cible, args.sha, args.attente) if cible is not None else None
    finally:
        conn.close()

    verdict = "ok"
    if souffrance or (resultat is not None and resultat["delai_s"] is None):
        verdict = "ko"
    elif resultat is None:
        verdict = "sans-sonde"
    rapport = {"hote": args.hote, "verdict": verdict, "personas": len(rows),
               "souffrance": souffrance, "boites_mortes": mortes,
               "sonde": resultat, "pourquoi": pourquoi}
    if args.json:
        print(json.dumps(rapport, ensure_ascii=False))
    else:
        print("courrier %s : %d persona(s), souffrance %s, boites mortes %s"
              % (args.hote, len(rows), souffrance or "aucune", mortes or "aucune"))
        if resultat is None:
            print("courrier %s : sonde non passee (%s)" % (args.hote, pourquoi))
        elif resultat["delai_s"] is None:
            print("courrier %s : KO sonde a %s, %s (message %s, attente %ss)"
                  % (args.hote, resultat["destinataire"], resultat["tour"],
                     resultat["message"], args.attente))
        else:
            print("courrier %s : sonde remise et accusee par %s en %ss (message %s)"
                  % (args.hote, resultat["destinataire"], resultat["delai_s"],
                     resultat["message"]))
    return 1 if verdict == "ko" else 0


if __name__ == "__main__":
    sys.exit(main())

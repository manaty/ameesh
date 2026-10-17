#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Mail health of one host, for the staging update (#1155).

Read-only checks, then one real delivery probe:

* stuck mail: an ACTIVE message (one that would open a turn) still undelivered
  past ``--souffrance`` while its recipient is idle and not paused by the
  operator on the host -> KO;
* probe: a system message is dropped to one DRIVEN and FREE persona of the host
  (live lease, not in a turn, not paused by the operator); within ``--attente``
  the persona must answer it with a tool call (``agent-mail send <self> --ack
  <token>``) AND the turn that received it must end -> KO otherwise. A persona
  already in a turn only queues the message until its current turn ends, and a
  persona paused by the operator keeps ``status`` idle but its runner refuses
  the turn: probing either would measure its queue, not the host's chain, so
  neither is chosen (#2190). When every driven persona is busy or paused, the
  verdict is ``sans-sonde`` with those targets named — never a KO caused by the
  wait. Delivery alone is recorded when the harness
  starts, before a refused harness exits 127 (#1460): only the answer proves a
  real turn with working tools, and a failed or blocked turn after delivery is
  KO at once. The answer alone is not enough either: a blocked harness can
  answer the tool and then stay alive until its timeout, so the turn's end
  (``last_turn_ts``) is required too;
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

#: seconds allowed for delivery, tool answer and a clean turn end (bounded)
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


def personas(db, hote: str | None = None) -> list[dict]:
    """Non-stopped execute personas, on ``hote`` (all hosts when None).

    Sorted by host then name: the auto probe target is deterministic, whether
    the host is the one measured or the pool of the other hosts.
    """
    rows = registry.overview(db)
    out = [r for r in rows
           if (hote is None or r.get("host") == hote)
           and (r.get("mode") or "execute") == "execute"
           and r.get("status") != "stopped"]
    return sorted(out, key=lambda r: (r.get("host") or "", r["name"]))


def driven(row: dict, now: float) -> bool:
    """A live lease: a runner holds this persona right now."""
    return bool(row.get("lease_owner")) and float(row.get("lease_expires_ts") or 0) > now


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
    """Active undelivered messages older than the bound, idle recipients.

    A persona paused by the operator is idle but deliberately takes no turn:
    its waiting mail is expected, not a delivery failure (#2190).
    """
    out = []
    for row in rows:
        if (row.get("status") or "idle") != "idle" or row.get("operator_paused"):
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


def probe(db, row: dict, sha: str, attente: float,
          hote_mesure: str | None = None) -> dict:
    """Drop one system message and wait (bounded) for the answered, ended turn.

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
           "statut_cible": row.get("status") or "idle", "tour": "non remis"}
    fin = debut + attente
    repondu = False
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
                        help="auto (premiere persona menee libre), jamais, ou un nom")
    parser.add_argument("--serveur", action="store_true",
                        help="hote sans executant : sonde croisee vers un autre hote")
    parser.add_argument("--attente", type=float, default=DEFAULT_ATTENTE)
    parser.add_argument("--souffrance", type=float, default=DEFAULT_SOUFFRANCE)
    parser.add_argument("--sha", default="", help="commit en cours d'installation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # le rapport existe des le depart : toute erreur (base, sonde, courrier)
    # sort en JSON KO, jamais en trace vide (#2190).
    rapport = {"hote": args.hote, "verdict": "ko", "personas": 0,
               "sha": args.sha, "souffrance": [], "boites_mortes": [],
               "sonde": None, "pourquoi": None, "attente_s": args.attente,
               "souffrance_s": args.souffrance}
    conn = None
    try:
        conn = db_mod.connect(config.load())
        now = time.time()
        rows = personas(conn, args.hote)
        rapport["personas"] = len(rows)
        souffrance = stuck(conn, rows, args.souffrance, now)
        mortes = dead_letters(conn, args.hote)
        try:
            if args.serveur and not rows:
                # le serveur sans executant n'a aucune persona locale : sa chaine
                # se prouve par une remise a travers lui, vers une persona menee
                # d'un autre hote (avis R2 B1 #1323).
                if args.sonde == "jamais":
                    cible, pourquoi = None, "sonde desactivee"
                else:
                    cible, pourquoi = choisir_croise(personas(conn), args.hote,
                                                     args.sonde, now)
            else:
                cible, pourquoi = choose(rows, args.sonde, now)
        except SystemExit as exc:
            # une cible nommee absente est une erreur de sonde, pas une trace
            # vide : le rapport KO la porte (#2190).
            raise RuntimeError("sonde invalide : %s" % exc) from None
        if cible is not None:
            # la selection vient d'une photo du registre : la cible a pu entrer
            # en tour ou etre mise en pause entre-temps. Une derniere lecture
            # evite d'envoyer la sonde dans la file d'un agent non libre
            # (#2190).
            etat = registry.get(conn, cible["name"])
            if etat is None or not driven(etat, time.time()):
                cible, pourquoi = None, "cible %s sans bail vivant" % cible["name"]
            elif not libre(etat):
                cible, pourquoi = None, cible_occupee(etat)
        resultat = (probe(conn, cible, args.sha, args.attente, args.hote)
                    if cible is not None else None)
        verdict = "ok"
        if souffrance or (resultat is not None and resultat["delai_s"] is None):
            verdict = "ko"
        elif resultat is None:
            verdict = "sans-sonde"
        rapport.update(verdict=verdict, souffrance=souffrance,
                       boites_mortes=mortes, sonde=resultat, pourquoi=pourquoi)
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
        print("courrier %s : %d persona(s), souffrance %s, boites mortes %s"
              % (args.hote, rapport["personas"], rapport["souffrance"] or "aucune",
                 rapport["boites_mortes"] or "aucune"))
        if rapport.get("erreur"):
            print("courrier %s : KO sonde (%s)" % (args.hote, rapport["erreur"]))
        elif resultat is None:
            print("courrier %s : sonde non passee (%s)"
                  % (args.hote, rapport["pourquoi"]))
        elif resultat["delai_s"] is None:
            print("courrier %s : KO sonde a %s, %s (message %s, attente %ss)"
                  % (args.hote, resultat["destinataire"], resultat["tour"],
                     resultat["message"], args.attente))
        else:
            via = ""
            if resultat["hote_destinataire"] != args.hote:
                via = " (hote %s)" % resultat["hote_destinataire"]
            print("courrier %s : sonde remise et accusee par %s en %ss (message %s)%s"
                  % (args.hote, resultat["destinataire"], resultat["delai_s"],
                     resultat["message"], via))
    return 1 if rapport["verdict"] == "ko" else 0


if __name__ == "__main__":
    sys.exit(main())

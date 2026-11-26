"""#8284 : la présence d'un humain du canon sur son propre poste est relayée.

Le 25/11, deux notes écrites depuis le poste de l'owner (session non liée,
acteur « inconnu ») annonçaient sa présence et un accord conversationnel sur
une décision ouverte ; personne n'a été réveillé avant 8 h. Ce module :

- reconnaît le poste : la preuve `user@host` d'une entrée non liée (la même
  que le courrier humain, `decisions.human_account_proof`) figure dans les
  `local_accounts` d'exactement un membre du canon ;
- marque la note d'un acteur « inconnu » écrite depuis ce poste, pour que le
  journal dise d'où elle vient (l'acteur n'est pas changé : une session
  externe sur le poste n'est pas l'humain) ;
- réveille aussitôt le bureau (avatar de l'humain, sinon `report` du projet)
  et le pilote du projet, avec les gestes de cet humain en attente :
  décisions dues, logins en échec, séances d'appareil ;
- sur un accord conversationnel, demande la signature native tout de suite.

Le relais ne retient jamais la note ni le courrier : toute erreur est tue.
Un seul réveil de présence par humain et par fenêtre (`WINDOW_S`, clé
d'idempotence du courrier) ; un accord, lui, part à chaque texte distinct.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

#: une présence relayée couvre cette fenêtre : les notes suivantes du même
#: humain n'y réveillent personne une seconde fois
WINDOW_S = 1800
#: au-delà, la liste des gestes est coupée (le reste : `ameesh decisions`)
MAX_LINES = 8
EXCERPT = 240

ACCORD_CUES = re.compile(
    r"accord\s+(?:conversationnel|utilisateur|oral|verbal)"
    r"|signature\s+native\s+attendue", re.IGNORECASE)
_CITED = re.compile(r"#(\d{4,})")


@dataclass(frozen=True)
class Station:
    """Le poste d'un humain du canon : qui (`human:<id>`) et la preuve."""
    human: str
    account: str


def station(cfg, db) -> Station | None:
    """Le poste de ce processus s'il appartient à un humain du canon, ou None.

    Seule une entrée non liée produit la preuve : l'exécuteur et les sessions
    liées du poste (agents du mesh) n'en ont pas. Un compte déclaré par deux
    membres ne désigne personne."""
    try:
        from . import canon, decisions
        proof = decisions.human_account_proof(cfg, db)
        if not proof or proof.startswith("?@") or proof.endswith("@?"):
            return None
        owners = {"human:%s" % member.title
                  for entry in canon.load_configured(cfg)
                  for member in entry.members
                  if proof in {str(a).strip() for a in (member.local_accounts or ())}}
    except Exception:  # noqa: BLE001 - canon illisible : aucun poste reconnu
        return None
    if len(owners) != 1:
        return None
    return Station(human=owners.pop(), account=proof)


def mark(text: str, where: Station | None) -> str:
    """La note marquée de son poste, pour un acteur « inconnu »."""
    if where is None:
        return text
    return "%s [poste de %s : %s]" % (text, where.human, where.account)


def is_accord(text: str) -> bool:
    return bool(ACCORD_CUES.search(text or ""))


# --------------------------------------------------------------------------
# gestes en attente
# --------------------------------------------------------------------------

def _decisions(db, human: str, now: float) -> list[dict]:
    from . import decisions
    try:
        return decisions.pending(db, now, human=human)
    except Exception:  # noqa: BLE001
        return []


def _logins(db, now: float) -> list[str]:
    from . import auth_failure, storage
    try:
        rows = storage.of(db).credentials.listing()
    except Exception:  # noqa: BLE001 - base sans 0060 ou illisible
        return []
    out = []
    for row in rows:
        eff = auth_failure.effective(row, now)
        if eff and eff.get("state") == "failing":
            out.append("%s/%s/%s" % (row.get("host"), row.get("harness"), row.get("account")))
    return out


def _sessions(cfg, db, now: float) -> list[dict]:
    from . import canon, device_gates
    try:
        return device_gates.candidates(db, canons=canon.load_configured(cfg), now=now)
    except Exception:  # noqa: BLE001
        return []


def pending_gestures(cfg, db, human: str, *, now: float | None = None) -> dict:
    """Les gestes de `human` en attente : décisions, logins, séances."""
    now = time.time() if now is None else float(now)
    return {"decisions": _decisions(db, human, now), "logins": _logins(db, now),
            "sessions": _sessions(cfg, db, now)}


def _decision_line(view: dict) -> str:
    lot = " (#%s)" % view["lot"] if view.get("lot") else ""
    flags = "".join((" [signature]" if view.get("needs_signature") else "",
                     " [urgent]" if view.get("urgent") else "",
                     " [échue]" if view.get("overdue") else ""))
    question = " ".join(str(view.get("question") or "").split())[:120]
    return "- décision #%d%s%s : %s" % (int(view["id"]), lot, flags, question)


def body(where: Station, *, source: str, lot: int | None, text: str,
         gestures: dict, accord_ids: list[int], now: float) -> str:
    moment = time.strftime("%d/%m %H:%MZ", time.gmtime(now))
    origin = "%s%s" % (source, " sur #%d" % lot if lot else "")
    excerpt = " ".join((text or "").split())
    if len(excerpt) > EXCERPT:
        excerpt = excerpt[:EXCERPT - 1] + "…"
    lines = ["Présence de %s sur son poste (%s) : %s à %s — « %s »."
             % (where.human, where.account, origin, moment, excerpt)]
    if accord_ids or is_accord(text):
        cited = ", ".join("#%d" % i for i in accord_ids) or "la décision citée"
        lines.append("Accord conversationnel relevé (%s) : demander MAINTENANT la "
                     "signature native, tant que %s est présent : ameesh decide <id> "
                     "<option> --signed." % (cited, where.human))
    decisions = gestures.get("decisions") or []
    logins = gestures.get("logins") or []
    sessions = gestures.get("sessions") or []
    if not (decisions or logins or sessions):
        lines.append("Aucun geste de %s en attente relevé." % where.human)
    if decisions:
        lines.append("Décisions dues (%d) :" % len(decisions))
        lines.extend(_decision_line(v) for v in decisions[:MAX_LINES])
        if len(decisions) > MAX_LINES:
            lines.append("- … (ameesh decisions)")
    if logins:
        lines.append("Logins en échec (%d) : %s" % (len(logins), ", ".join(logins[:MAX_LINES])))
    if sessions:
        lines.append("Séances d'appareil (%d) : %s" % (len(sessions), ", ".join(
            "#%s" % s.get("lot", s.get("id", "?")) for s in sessions[:MAX_LINES])))
    lines.append("Réveil : présence de l'owner relayée (#8284) ; conduire les gestes avec "
                 "lui pendant qu'il est là.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# destinataires et envoi
# --------------------------------------------------------------------------

def recipients(cfg, db, human: str, item: dict | None) -> list[str]:
    """Le bureau (avatar de l'humain, sinon `report` du projet), puis le ou
    les pilotes du projet du lot (orchestrateurs, repli : tous)."""
    from . import alert_briefs, avatars, release, staging_deferred
    rows = staging_deferred._registry_rows(db)
    names: list[str] = []
    avatar = avatars.name_for(cfg, human, rows)
    if avatar:
        names.append(avatar)
    project = release.project_of(cfg, item or {})
    if project is not None:
        for name in staging_deferred._recipients(cfg, db, project):
            if name not in names:
                names.append(name)
    elif cfg is not None:
        try:
            pilots = alert_briefs.project_pilots(
                str((item or {}).get("app") or ""), rows, cfg=cfg, db=db)
        except Exception:  # noqa: BLE001
            pilots = []
        names.extend(n for n in pilots if isinstance(n, str) and n and n not in names)
    return names


def _key(where: Station, text: str, accord: bool, now: float) -> str:
    who = re.sub(r"[^A-Za-z0-9._-]", "-", where.human.split(":", 1)[-1]) or "x"
    if accord:
        digest = hashlib.sha256((text or "").encode()).hexdigest()[:16]
        return "owner-accord:%s:%s" % (who, digest)
    return "owner-presence:%s:%d" % (who, int(now // WINDOW_S))


def relay(cfg, db, where: Station | None, *, source: str, text: str,
          lot: int | None = None, now: float | None = None) -> list[str]:
    """Réveille bureau et pilote ; rend les destinataires effectivement saisis.

    Jamais d'exception : une note ou un courrier déjà écrit n'est pas défait
    par un relais impossible."""
    if where is None or db is None:
        return []
    now = time.time() if now is None else float(now)
    try:
        from . import mail, work
        item = work.get(db, int(lot)) if lot else None
        gestures = pending_gestures(cfg, db, where.human, now=now)
        pending_ids = {int(v["id"]) for v in gestures["decisions"]}
        accord = is_accord(text)
        accord_ids = ([int(n) for n in _CITED.findall(text or "") if int(n) in pending_ids]
                      if accord else [])
        message = body(where, source=source, lot=lot, text=text, gestures=gestures,
                       accord_ids=accord_ids, now=now)
        key = _key(where, text, accord, now)
        payload = {"urgent": True, "owner_presence": {
            "human": where.human, "account": where.account, "source": source,
            "lot": lot, "accord": accord_ids if accord else None,
            "decisions": sorted(pending_ids)}}
        targets = recipients(cfg, db, where.human, item)
    except Exception:  # noqa: BLE001
        return []
    sent = []
    for target in targets:
        try:
            if mail.send(db, work.SYSTEM_SENDER, target, message, kind="event",
                         payload=dict(payload), idempotency_key=key) is not None:
                sent.append(target)
        except ValueError:
            continue  # même clé, autre corps : présence déjà relayée dans la fenêtre
        except Exception:  # noqa: BLE001 - destinataire retiré, base indisponible
            continue
    return sent

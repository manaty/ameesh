# SPDX-License-Identifier: AGPL-3.0-only
"""#7632 : cadence de réveil déclarée par la fiche d'un agent (spec avatars §4.4).

Bloc `reveil` d'une fiche Agent/Persona :

    reveil:
      immediat: [humain, urgent, request, reply, decision]
      fenetre: 30m

* `immediat` : natures de courrier qui ouvrent un tour tout de suite ;
* `fenetre` : le reste (courrier ordinaire, événements) est regroupé et remis
  au tour suivant, au plus tard à la fin de la fenêtre (comptée depuis le plus
  ancien message en attente et depuis la fin du dernier tour).

Les deux clés sont facultatives : `reveil: {}` prend la règle de l'owner du
17/11 (`IMMEDIAT_DEFAUT`, 30 min). Sans bloc, rien ne change : `mail_batch` et
`event_coalesce` de l'exécuteur s'appliquent comme avant. Le message d'un
humain réveille toujours (L125, décision owner du 17/11), qu'il soit listé ou
non ; le courrier passif ne réveille jamais.

Fonctions pures : la fiche (canon), le registre (colonne `reveil`, 0588) et
l'exécuteur lisent la même politique.
"""
from __future__ import annotations

import json
from typing import Any

#: natures reconnues d'un courrier actif
NATURES = ("humain", "urgent", "request", "reply", "decision", "event", "notify")
#: décision owner du 17/11 : humain, urgences et demandes tout de suite
IMMEDIAT_DEFAUT = ("humain", "urgent", "request", "reply", "decision")
#: fenêtre de regroupement par défaut (30 min, spec §4.4)
FENETRE_DEFAUT_S = 1800
#: une fenêtre de plus d'un jour laisserait du courrier sans tour
FENETRE_MAX_S = 86400

_SUFFIXES = {"s": 1, "m": 60, "h": 3600}


def _seconds(value: Any) -> int:
    """`1800`, `90s`, `30m`, `1h` → secondes ; ValueError sinon."""
    if isinstance(value, bool):
        raise ValueError(value)
    if isinstance(value, (int, float)):
        number, factor = float(value), 1
    else:
        text = str(value).strip().lower()
        factor = 1
        if text and text[-1] in _SUFFIXES:
            factor, text = _SUFFIXES[text[-1]], text[:-1]
        number = float(text)
    seconds = number * factor
    if seconds != seconds or seconds < 0 or seconds > FENETRE_MAX_S:
        raise ValueError(value)
    return int(round(seconds))


def parse(value: Any) -> tuple[dict | None, list[str]]:
    """Le bloc `reveil` d'une fiche → (politique, problèmes).

    None sans bloc. Un bloc fautif rend aussi une politique (défauts pour la
    clé illisible) : la fiche est signalée en erreur, et l'agent ne retombe
    pas en silence sur un réveil par courrier."""
    if value is None:
        return None, []
    problems: list[str] = []
    if value == "" or value == {}:
        value = {}
    if not isinstance(value, dict):
        return ({"immediat": list(IMMEDIAT_DEFAUT), "fenetre_s": FENETRE_DEFAUT_S},
                ["`reveil`: mapping expected (`immediat`, `fenetre`), not %r" % (value,)])
    unknown = sorted(set(value) - {"immediat", "fenetre"})
    if unknown:
        problems.append("`reveil`: unknown key(s) %s" % ", ".join(map(str, unknown)))
    immediat = list(IMMEDIAT_DEFAUT)
    raw = value.get("immediat")
    if raw is not None:
        if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
            problems.append("`reveil.immediat`: list of natures expected (%s), not %r"
                            % (", ".join(NATURES), raw))
        else:
            names = [n.strip().lower() for n in raw if n.strip()]
            bad = [n for n in names if n not in NATURES]
            if bad:
                problems.append("`reveil.immediat`: unknown nature(s) %s (known: %s)"
                                % (", ".join(bad), ", ".join(NATURES)))
            immediat = [n for n in dict.fromkeys(names) if n in NATURES]
    fenetre = FENETRE_DEFAUT_S
    if value.get("fenetre") is not None:
        try:
            fenetre = _seconds(value["fenetre"])
        except (TypeError, ValueError):
            problems.append("`reveil.fenetre`: duration expected (`30m`, `1800`), "
                            "at most 24 h, not %r" % (value["fenetre"],))
    return {"immediat": immediat, "fenetre_s": fenetre}, problems


def of(agent: dict | None) -> dict | None:
    """La politique d'une ligne du registre (colonne `reveil`), ou None.

    Une valeur illisible vaut l'absence de bloc : le comportement d'avant."""
    raw = (agent or {}).get("reveil")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        fenetre = float(raw.get("fenetre_s", FENETRE_DEFAUT_S))
    except (TypeError, ValueError):
        return None
    immediat = raw.get("immediat")
    if not isinstance(immediat, list):
        immediat = list(IMMEDIAT_DEFAUT)
    return {"immediat": [str(n) for n in immediat], "fenetre_s": max(0.0, fenetre)}


def natures(row: dict, *, human: bool = False) -> set[str]:
    """Les natures d'un courrier actif (`human` : expéditeur jugé humain par
    l'appelant, qui connaît les noms humains de l'hôte)."""
    from . import mail
    payload = mail.message_payload(row)
    kind = (row.get("kind") or "").strip()
    out = {kind} if kind in NATURES else set()
    if human or mail.is_human(row):
        out.add("humain")
    if mail.is_urgent(row):
        out.add("urgent")
    if payload.get("decision") is not None or payload.get("decision_answer") is not None:
        out.add("decision")
    return out


def immediate(policy: dict, row: dict, *, human: bool = False) -> bool:
    """Ce courrier ouvre-t-il un tour tout de suite sous `policy` ?
    L'humain toujours (décision owner du 17/11)."""
    found = natures(row, human=human)
    return "humain" in found or bool(found & set(policy.get("immediat") or ()))


def describe(policy: dict | None) -> str:
    """Une ligne lisible : `immediate: humain, urgent… ; grouped every 30 min`."""
    if policy is None:
        return "per mail (no `reveil` declared)"
    minutes = float(policy["fenetre_s"]) / 60.0
    return "immediate: %s; the rest grouped every %s min" % (
        ", ".join(policy["immediat"]) or "humain",
        ("%d" % minutes) if minutes == int(minutes) else ("%.1f" % minutes))


def next_wake(policy: dict, pending: list[dict], *, last_turn_ts: float | None,
              now: float) -> float | None:
    """L'instant (epoch) du prochain tour groupé, d'après la base seule : fin
    de la fenêtre comptée depuis le plus ancien courrier en attente, et pas
    avant la fin de fenêtre comptée depuis le dernier tour. None sans
    courrier en attente."""
    created = [float(m["created_ts"]) for m in pending if m.get("created_ts") is not None]
    if not created:
        return None
    fenetre = float(policy["fenetre_s"])
    due = min(created) + fenetre
    if last_turn_ts:
        due = max(due, float(last_turn_ts) + fenetre)
    return due


def status(db, row: dict, *, human_names=(), now: float | None = None) -> dict | None:
    """L'état de réveil d'un agent pour `ameesh show` : sa cadence, le
    courrier actif en attente (groupé, immédiat) et l'instant du prochain
    tour groupé. None sans déclaration (rien de nouveau à montrer)."""
    import time
    from . import mail
    policy = of(row)
    if policy is None:
        return None
    pending = mail.unread_active(db, row["name"])
    immediats = [m for m in pending
                 if immediate(policy, m, human=(m.get("sender") or "") in human_names)]
    groupes = [m for m in pending if m not in immediats]
    return {"cadence": describe(policy), "immediat": list(policy["immediat"]),
            "fenetre_s": policy["fenetre_s"],
            "pending": len(groupes), "immediate_pending": len(immediats),
            "next_wake_ts": next_wake(policy, groupes, last_turn_ts=row.get("last_turn_ts"),
                                      now=time.time() if now is None else now)}

# SPDX-License-Identifier: AGPL-3.0-only
"""Le débit du mesh, mesuré sur les jalons (`ameesh debit`, canon 24/10/2026).

L'auditeur commence chaque passage par le débit ; le canon
(`processus/lots.md`, « Un goulot se résout sans l'owner ») nomme ce qu'il
mesure : lots en `qa` et en `blocked` par âge, relectures désignées sans
verdict par relecteur, avis bloquants que l'auteur n'a pas traités, holds et
réveils, agents au repos avec du travail dû, tours et fusions par heure,
durée des runs de CI. La mesure est **lue sur les jalons** — gels, avis,
fusions, journal — et **jamais sur les colonnes d'un lot** : `state`,
`reviewer`/`reviewers`, `wake_at` et `updated_at` peuvent être périmés, et
c'est l'erreur du bureau du 24/10 à 03:20Z (un lot annoncé « en attente de
relecture » alors que la colonne `reviewer` ne nommait personne de courant).

Ce module sépare la lecture (SQL, `read`) du calcul (`build`), pour que le
calcul soit témoin-testable sans base. Il ne calcule **pas** un second avis
dû : la désignation est relue dans les marques du dernier gel
(`review-designation:` / `review-designation-carried:`), les désignations et
retraits manuels (`reviewer X designated|removed`) et les verdicts qui portent
sur la tête gelée — la même règle que #3033, sans sa colonne.

Sortie : un schéma stable `ameesh-debit/1` en `--json`, un tableau court en
texte. `--check` sort 2 quand une attente de plus de `--threshold` (2 h par
défaut) **qui ne dépend pas d'un humain** reste sans geste : le passage de
l'auditeur ne peut pas dire « rien à signaler » dans ce cas. Les attentes
humaines explicites (geste owner, go encore requis, décision owner due ou non
signée, retour de l'owner, login/2FA, lot `waiting_human`) sont listées à part
et ne font pas sortir 2 : le canon ne vise que les attentes d'agent. Une
simple mention d'une règle ou d'une consigne de l'owner ne suffit pas, et un
`hold_undeployed` n'est jamais humain. #3447 : les lots réservés à un exécutant
externe déclaré (`external_executor`) sont, de même, listés à part (« attentes
externes ») : ce n'est ni un lot sans assigné ni une attente d'agent, et ce
n'est pas `--check` qui la pousse. #3498 : un hold peut nommer son exécutant
externe et le geste attendu (`hold: attente bureau : …`,
`hold: attente owner : …`) ; le débit le range dans les mêmes « attentes
externes », avec une relance à cet exécutant — à l'échéance déclarée
(`(due 2026-10-25 12:00 UTC)`), sinon après le seuil du débit — et non à
l'agent. Une attente externe, même échue, ne fait pas sortir `--check` 2 : le
rapport la relance à chaque passage. #3773 : le hold peut aussi déclarer un
tiers extérieur sans nommer le bureau ni l'owner (`hold: attente externe :
revue Apple`) ; le débit le range dans les mêmes « attentes externes », avec
la même relance à l'échéance ou au seuil. #4235 : un hold de pilote peut
déclarer une **fenêtre d'observation** — une attente voulue et datée
(`hold: fenêtre : …`, échéance armée par `--wake-at`) ; jusqu'à l'échéance,
le débit la range dans `window_waits`, hors `slow_waits`, et `--check` ne
sort pas 2 : rien n'est actionnable avant. À l'échéance, le hold redevient
une attente ordinaire et le réveil échu la signale.

Un lot relu (tous ses avis rendus) dont `branch_target` est la branche d'un
autre lot **ouvert** n'est pas une attente de paquet : il ne peut entrer
qu'avec ce lot, dont le hold couvre déjà l'attente. Le rapport le marque
`depends_on` et ne produit ni `qa_merge` ni sortie 2 pour lui (#3437).

#3256 B : la section `capacity` (spec `docs/design/3256-debit-file.md` §3) —
agents réveillables au repos, heures-agent au repos, lots démarrables, lot de
découpage, perte au reset par compte. `--check` sort aussi 2 quand un projet
a au moins `file.repos_part` de ses agents réveillables au repos depuis
`file.repos_s` (bloc mécanique du canon) et qu'aucun lot de découpage n'a moins
d'une heure : une file vide n'est pas « rien à signaler ».
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import argparse
import datetime
import json
import re
import statistics
import sys
import time
from typing import Any, Iterable, Mapping

from . import work_lifecycle

SCHEMA = "ameesh-debit/1"

#: fenêtre par défaut du débit (tours et fusions par heure)
DEFAULT_HOURS = 6
#: une attente au-delà de ce seuil est un goulot (canon §9, 2 h)
DEFAULT_WAIT_S = 2 * 3600
#: fenêtre des durées de CI et seuil d'un run lent
DEFAULT_CI_HOURS = 24
DEFAULT_CI_SLOW_S = 30 * 60
#: bornes de lecture (une passe d'auditeur reste une lecture)
MAX_ITEMS = 2000
MAX_CI_RUNS = 500
MAX_JOB_RUNS = 5
#: profondeur du journal ramené pour une lecture : les marques de désignation
#: et les holds/wakes des lots actifs ; les lots terminaux sont écartés.
EVENT_DAYS = 60
#: marque d'une désignation automatique (`_sync_reviewers` la relit)
_MARK = re.compile(
    r"^review-designation(?:-carried)?:(\d+):(\d+):\s*(\{.*\})\s*$", re.S)
_MARK_NONE = re.compile(r"^review-designation-none:(\d+):(\d+):")
_MANUAL = re.compile(r"^reviewer\s+(.+?)\s+(designated|removed)\s*$")
_WAKE = re.compile(r"^wake\s*:\s*(.*)$", re.I)
_WAKE_DELIVERED = re.compile(r"scheduled wake delivered", re.I)
_DUE = re.compile(r"\(due\s+([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}"
                  r"(?::[0-9]{2})?(?:\s+[A-Za-z][A-Za-z0-9_/+-]*)?)\)")
_PROGRAMMED = re.compile(r"\[réveil programmé\s*:\s*([^\]]+)\]", re.I)
_VERSION = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
#: une retenue qui attend un déploiement (#3497) : « pas/non/jamais déployé »,
#: le verbe (« reste à déployer main »), et le nom dans une formule d'attente
#: (« attente du déploiement », « inerte jusqu'au déploiement ») ou de
#: constat. « déploiement » seul ne suffit pas : un compte rendu de bureau
#: (« vidage de déploiement 06:02Z ») n'est pas une retenue.
_UNDEPLOYED = re.compile(
    r"(?:pas|non|jamais)(?:\s+encore)?\s+d[ée]ploy[ée]e?s?"
    r"|\bd[ée]ployer\b"
    r"|\b(?:à|reste[nt]?|attend(?:s|re|ons|ent)?|attente|en\s+attente|"
    r"jusqu'au|avant|après|dès)\b[^.;]{0,60}\bd[ée]ploiement\b"
    r"|\bd[ée]ploiement\s+(?:constat[ée]e?|non\s+constat[ée]e?|à\s+faire|"
    r"attendu|requis|dû|pas\s+fait)\b", re.I)
_DEVICE_TOKEN = re.compile(r"`([^`]+)`")
_DEVICE_PHRASE = re.compile(r"\b((?:Mac|PC|portable|ordinateur)\s+de\s+[A-ZÉÈÀÂÎÔÛ][\w-]+)",
                            re.I)
_DEVICE_RANGE = re.compile(r"du\s+(\d{2})/(\d{2})/(\d{4})\s+au\s+(\d{2})/(\d{2})/(\d{4})")
_DEVICE_LINE = re.compile(r"Appareils indisponibles en vigueur\s*:\s*(.+?)(?:\.\s|\n|$)",
                          re.I | re.S)
_FR_MONTHS = {"01": 1, "02": 2, "03": 3, "04": 4, "05": 5, "06": 6,
              "07": 7, "08": 8, "09": 9, "10": 10, "11": 11, "12": 12}

#: gestes de niveau 1 (canon §2/§9) : le texte que l'auditeur colle dans son
#: rapport pour chaque attente ; un geste de niveau 2 est nommé pour le pilote.
GESTURES = {
    "review": "relancer le relecteur {who} avec la liste de ses avis dus "
              "(le débit la porte : `agent-mail send {who} \"…\" --lot {lot}`) ; "
              "au repos, le réveiller (`ameesh resume {who}`), sinon redésigner "
              "(`ameesh work reviewer {lot} <agent>`)",
    "no_reviewer": "désigner un relecteur d'une autre famille "
                   "(`ameesh work reviewer {lot} <agent>`)",
    "qa_merge": "lot relu, en attente du paquet : rappeler au pilote de le ranger "
                "dans le prochain paquet horaire (canon §11 bis)",
    "blocking_review": "relancer l'auteur {who} sur l'avis bloquant ; sans "
                       "mouvement, le pilote réassigne (canon §9 ×2)",
    "hold_device": "hold sans objet : demander au pilote de le lever (appareil "
                   "déclaré indisponible, canon §12)",
    "hold_undeployed": "correctif fusionné non déployé : demander le staging "
                       "hors créneau (canon §11 bis) ou la levée du hold",
    "hold": "relancer le détenteur du hold ({who}) ; s'il est sans objet, "
            "demander sa levée au pilote",
    "hold_external": "relancer {actor} sur le geste attendu : {gesture} "
                     "(attente externe déclarée, pas un agent à assigner ; "
                     "canon §10)",
    "hold_external_third": "suivre le tiers extérieur sur le geste attendu : "
                           "{gesture} (attente externe déclarée, pas un agent "
                           "à assigner ; relancer le pilote à l'échéance, "
                           "canon §10)",
    "hold_window": "fenêtre voulue et datée : aucune relance avant le {due} ; "
                   "à l'échéance, {motif}",
    "wake_overdue": "réveiller l'assigné : le réveil programmé est échu",
}


def _external_gesture(declared: Mapping[str, Any]) -> str:
    """Le geste affiché pour une attente externe (#3498, #3773).

    Un exécutant nommé (bureau, owner) se relance ; un tiers extérieur
    (`externe`) se suit, et la relance va au pilote."""
    if str(declared.get("executor") or "") == "externe":
        return GESTURES["hold_external_third"].format(
            gesture=declared.get("gesture") or "")
    actor = "le bureau" if declared.get("executor") == "bureau" else "l'owner"
    return GESTURES["hold_external"].format(
        actor=actor, gesture=declared.get("gesture") or "")


def _window_gesture(declared: Mapping[str, Any], due: str | None) -> str:
    """Le geste affiché pour une fenêtre d'observation (#4235).

    Le motif est cité, borné : c'est ce qui est attendu à l'échéance ; le
    texte complet reste dans `cause`."""
    motif = " ".join(str(declared.get("motif") or "").split())
    if len(motif) > 200:
        motif = motif[:199].rstrip() + "…"
    return GESTURES["hold_window"].format(due=due or "?", motif=motif)


class DebitError(ValueError):
    """Une lecture ou une option refusée."""


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _age(now: float, ts: float | None) -> float | None:
    return None if ts is None else max(0.0, float(now) - float(ts))


def _version(text: str) -> tuple[int, int, int] | None:
    versions = _versions(text)
    return max(versions) if versions else None


def _versions(text: str) -> list[tuple[int, int, int]]:
    """Toutes les versions d'un texte : un hold peut nommer l'installée et la
    cible (« 1.20.0 → 1.21.0 ») ; c'est la plus haute qui dit le correctif dû."""
    return [tuple(int(part) for part in match.groups())  # type: ignore[misc]
            for match in _VERSION.finditer(str(text or ""))]


def installed_version() -> str:
    """La version du paquet qui exécute la commande (`ameesh --version`).

    `from . import version` rend la FONCTION `version()` de `__init__.py` (il
    n'y a pas de module `ameesh/version.py`) : l'appeler, jamais
    `version_mod.version()` — sinon l'AttributeError était avalée, la version
    restait « unknown » et `_version_gt` ne comparait jamais (#3292, constat
    auditeur du 24/10)."""
    try:
        from . import version
        return str(version())
    except Exception:  # noqa: BLE001 - une version illisible ne bloque pas
        return "unknown"


def _version_gt(left: tuple[int, int, int], right: tuple[int, int, int] | None) -> bool:
    return right is not None and left > right


def _branch_name(value: Any) -> str:
    """La branche courte d'une ref (`origin/lot/x`, `refs/heads/lot/x`)."""
    name = str(value or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _target_lot(ident: int, item: Mapping[str, Any],
                open_branches: Mapping[tuple[str, str], Iterable[int]]) -> int | None:
    """Le lot ouvert dont `branch_target` est la branche, s'il existe (#3437).

    La comparaison est faite par projet quand il est connu : deux projets
    peuvent nommer la même branche. Un lot ne dépend jamais de lui-même."""
    target = _branch_name(item.get("branch_target"))
    if not target:
        return None
    app = str(item.get("app") or "")
    found: set[int] = set()
    for (branch_app, branch), lots in open_branches.items():
        if branch != target or (app and branch_app and branch_app != app):
            continue
        found.update(int(lot) for lot in lots if int(lot) != int(ident))
    return min(found) if found else None


def _same_sha(left: Any, right: Any) -> bool:
    """Deux têtes qui nomment le même commit (préfixe d'au moins 7)."""
    a, b = str(left or "").strip().lower(), str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def _rows_by_lot(rows: Iterable[Mapping[str, Any]]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for row in rows or ():
        out.setdefault(int(row["work_item_id"]), []).append(dict(row))
    for values in out.values():
        values.sort(key=lambda r: (float(r.get("at_ts") or 0.0), int(r.get("id") or 0)))
    return out


# -- réveils -----------------------------------------------------------------

def parse_wake_due(text: str) -> float | None:
    """L'échéance d'une note de réveil : `... (due 2026-10-30 08:00 UTC)` ou
    `[réveil programmé: 2026-10-24 11:15 CEST]`.

    La note est écrite dans le fuseau de l'hôte qui a posé le réveil
    (`_wake_moment`), avec son abréviation. `UTC`/`GMT`, `CET`/`CEST` (le
    fuseau du canon) et l'heure locale sont lus ; une abréviation inconnue
    rend None, jamais une heure devinée."""
    found = _DUE.search(text or "") or _PROGRAMMED.search(text or "")
    if not found:
        return None
    raw = found.group(1).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M"):
        try:
            parsed = time.strptime(raw, fmt)
        except ValueError:
            continue
        zone = raw.split()[-1].upper() if " " in raw else ""
        if zone in ("UTC", "GMT"):
            return float(datetime.datetime(*parsed[:6],
                                            tzinfo=datetime.timezone.utc).timestamp())
        if zone in ("CET", "CEST"):
            offset = 1 if zone == "CET" else 2
            return float(datetime.datetime(*parsed[:6],
                                           tzinfo=datetime.timezone(
                                               datetime.timedelta(hours=offset)))
                         .timestamp())
        return float(time.mktime(parsed))
    return None


def wake_state(events: Iterable[Mapping[str, Any]]) -> dict | None:
    """Le réveil encore armé d'un lot, lu dans son journal (cause incluse).

    `ameesh work wake` écrit `wake: <pourquoi> (due …)`, la consommation écrit
    `wake: scheduled wake delivered …`, l'annulation `wake: cancelled …` ;
    `work note --wake-at` ajoute `[réveil programmé: …]` à sa note. La
    dernière écriture gagne. Rend `{due_ts, due, why, by, since, source}` ou
    None."""
    armed = None
    for event in sorted(events or (),
                        key=lambda e: (float(e.get("at_ts") or 0.0),
                                       int(e.get("id") or 0))):
        text = str(event.get("note") or "").strip()
        if not text:
            continue
        match = _WAKE.match(text)
        if match:
            body = match.group(1).strip()
            if _WAKE_DELIVERED.search(body) or body.lower().startswith("cancelled"):
                armed = None
                continue
            due = parse_wake_due(text)
            armed = {"due_ts": due, "due": _iso(due), "why": _WAKE_WHY.sub("", body).strip(),
                     "by": event.get("actor") or None,
                     "since": float(event.get("at_ts") or 0.0),
                     "source": "wake"}
            continue
        if _PROGRAMMED.search(text):
            due = parse_wake_due(text)
            armed = {"due_ts": due, "due": _iso(due),
                     "why": _PROGRAMMED.sub("", text).strip(),
                     "by": event.get("actor") or None,
                     "since": float(event.get("at_ts") or 0.0),
                     "source": "note"}
    return armed


_WAKE_WHY = re.compile(r"\s*\(due\s+[^)]*\)\s*$")


# -- appareils déclarés indisponibles ---------------------------------------

def parse_device_rule(text: str) -> dict:
    """La déclaration d'appareils indisponibles du canon (§12).

    Une seule phrase porte la règle : « Appareils indisponibles en vigueur :
    **Mac de Carina (`nx-carina`) et PC de Marco, du 23/10/2026 au
    30/10/2026** ». Rend `{names, phrases, start, end, error}` ; les noms sont
    pris dans les accents graves (`nx-carina`) et dans les tournures « Mac de
    X »/« PC de X ». Une date illisible ne devine rien : `error` le dit."""
    match = _DEVICE_LINE.search(str(text or ""))
    if not match:
        return {"names": [], "phrases": [], "start": None, "end": None,
                "error": "no `Appareils indisponibles en vigueur` line"}
    declaration = match.group(1)
    names = [n.strip() for n in _DEVICE_TOKEN.findall(declaration) if n.strip()]
    phrases = [" ".join(p.split()) for p in _DEVICE_PHRASE.findall(declaration)]
    start = end = None
    date = _DEVICE_RANGE.search(declaration)
    error = None
    if date:
        try:
            day, month, year, day2, month2, year2 = date.groups()
            start = datetime.date(int(year), _FR_MONTHS[month], int(day))
            end = datetime.date(int(year2), _FR_MONTHS[month2], int(day2))
        except (ValueError, KeyError):
            error = "unreadable unavailability dates"
    else:
        error = "no unavailability dates"
    if not names and not phrases:
        error = error or "no device named"
    return {"names": list(dict.fromkeys(names)), "phrases": list(dict.fromkeys(phrases)),
            "start": start, "end": end, "error": error}


def unavailable_devices(canons, now: float) -> dict:
    """Les appareils déclarés indisponibles à `now` : fiches Host du canon
    (`indisponible_jusqu_au`, #3070) et phrase de règle du §12.

    Rend `{names, phrases, sources, unreadable}`. Une déclaration illisible ou
    une fiche périmée ne lève aucune porte (comme `device_gates`) et est dite."""
    names: list[str] = []
    phrases: list[str] = []
    sources: list[str] = []
    unreadable: list[str] = []
    for canon in canons or ():
        for host in getattr(canon, "hosts", None) or ():
            try:
                if host.unavailable_at(now):
                    names.append(str(host.title))
                    sources.append("Host %s" % host.title)
            except Exception:  # noqa: BLE001 - une fiche illisible est dite
                unreadable.append("unreadable Host fiche %s" % getattr(host, "title", "?"))
        for process in getattr(canon, "processes", None) or ():
            body = str(getattr(process, "body", "") or "")
            if "Appareils indisponibles en vigueur" not in body:
                continue
            rule = parse_device_rule(body)
            today = datetime.date.fromtimestamp(float(now))
            active = rule["start"] is not None and rule["end"] is not None \
                and rule["start"] <= today <= rule["end"]
            if active:
                for name in rule["names"]:
                    if name not in names:
                        names.append(name)
                for phrase in rule["phrases"]:
                    if phrase not in phrases:
                        phrases.append(phrase)
                sources.append("canon %s" % (getattr(process, "title", "") or "Process"))
            if rule["error"]:
                unreadable.append("device rule: %s" % rule["error"])
    # Dédoublonnage sans perdre l'ordre, et phrases retirées des noms.
    unique = list(dict.fromkeys(n for n in names if n))
    phrases = list(dict.fromkeys(p for p in phrases if p))
    return {"names": unique, "phrases": phrases, "sources": list(dict.fromkeys(sources)),
            "unreadable": unreadable}


def match_device(text: str, devices: Mapping[str, Any]) -> str | None:
    """L'appareil déclaré indisponible nommé par un texte de hold, ou None."""
    haystack = _norm(text)
    for phrase in devices.get("phrases") or ():
        if _norm(phrase) in haystack:
            return phrase
    for name in devices.get("names") or ():
        if _norm(name) and _norm(name) in haystack:
            return name
    return None


# -- avis dus (calcul unique, sur les jalons) -------------------------------

def _latest_freeze(milestones: list[dict]) -> dict | None:
    freezes = [m for m in milestones if m.get("kind") == "frozen"]
    if not freezes:
        return None
    return max(freezes, key=lambda m: (float(m.get("at_ts") or 0.0),
                                       int(m.get("id") or 0)))


def _terminal_at(milestones: list[dict]) -> float | None:
    """L'instant du dernier jalon terminal (`merged`, `closed`, `delivered`).

    Un lot qui porte un de ces jalons n'est plus en revue : la colonne d'état
    n'est pas lue, et un gel écrit après la fusion (désordre de jalons observé
    sur des lots de septembre) ne rouvre pas une revue. Une correction
    `merged → build` est lue dans le journal (`_reopened`), jamais dans une
    colonne périmée."""
    moments = [float(m.get("at_ts") or 0.0) for m in milestones
               if m.get("kind") in ("merged", "closed", "delivered")]
    return max(moments) if moments else None


#: états d'une vraie reprise de revue : le lot retravaille sa tête
_OPEN_STATES = (lifecycle.REQUESTED, lifecycle.IN_PROGRESS, lifecycle.QA, lifecycle.VALIDATED)
#: états d'une retenue de fermeture : le contenu est déjà dans la cible
_CLOSURE_STATES = ("blocked", "waiting_human")
#: états terminaux (le jalon, pas seulement la colonne)
_TERMINAL_STATES = ("merged", "closed", "delivered")
#: états finis au journal (promu, fermé) : plus aucune attente (#3672)
_CLOSED_STATES = ("promoted", "delivered", "closed")


def _reopened(events: list[dict], terminal_ts: float | None) -> bool:
    """Le dernier état après le jalon décide de la réouverture.

    Une note concurrente peut porter qa après la fusion (#1982), puis les
    notes suivantes merged. Un retour à merged annule une réouverture ; un lot
    rouvert puis retenu (blocked, waiting_human) reste ouvert — mais seulement
    s'il a vraiment repris (intake/build/qa) depuis le jalon : une écriture
    `blocked` de fermeture après la fusion ne rouvre pas une revue (#3497,
    #3444 classé `qa_merge` alors que son contenu était dans main)."""
    if terminal_ts is None:
        return False
    later = [e for e in events or ()
             if float(e.get("at_ts") or 0.0) > terminal_ts and e.get("state")]
    if not later:
        return False
    resumed = False
    latest_state = None
    for event in sorted(later, key=lambda e: (float(e.get("at_ts") or 0.0),
                                              int(e.get("id") or 0))):
        state = str(event["state"])
        if state in _OPEN_STATES:
            resumed = True
        elif state in _TERMINAL_STATES:
            resumed = False
        latest_state = state
    if latest_state in _CLOSURE_STATES:
        return resumed
    return latest_state in _OPEN_STATES


def _closure_holds(events: list[dict], terminal_ts: float | None,
                   allowed=None) -> dict[str, dict]:
    """Les retenues posées après le jalon terminal d'un lot fusionné (#3497).

    Après une fusion, les écritures d'état du harnais (`merged` d'un
    review-after-merge, `blocked` de la fermeture retenue) ne sont pas des
    reprises : `active_holds` remet la retenue à zéro à chaque changement
    d'état, ce qui perdait la retenue de fermeture de #3444 (`hold:` de
    04:44:17, `merged` concurrent de 04:44:23). Une retenue posée dans cet
    intervalle tient donc jusqu'à une levée ou une vraie reprise (`intake`,
    `build`, `qa`) ; une retenue antérieure à la fusion est du ressort
    d'`active_holds` et n'est jamais ressuscitée ici.

    Les levées suivent #3219, sur l'autorité partagée `lift_authority` : un
    auteur ne lève que sa retenue, un pilote lève toute celle du lot. Sans
    prédicat (lecture brute), seul l'auteur est levé — jamais celle d'un
    autre (relecture cx1 du 25/10 : un `unhold` ordinaire ne doit pas
    effacer la retenue d'un pilote)."""
    from . import planned_waits

    if terminal_ts is None:
        return {}
    held: dict[str, dict] = {}
    for event in sorted(events or (),
                        key=lambda e: (float(e.get("at_ts") or 0.0),
                                       int(e.get("id") or 0))):
        if float(event.get("at_ts") or 0.0) <= terminal_ts:
            continue
        state = str(event.get("state") or "")
        if state in _OPEN_STATES:
            held = {}                   # vraie reprise : la retenue est finie
        text = str(event.get("note") or "").strip()
        if not text:
            continue
        who = event.get("actor") or ""
        if planned_waits.is_hold_note(text):
            held[who] = {"by": who or None,
                         "since": float(event.get("at_ts") or 0.0),
                         "note": text,
                         "pilot": bool(allowed(who)) if allowed is not None else False}
        elif planned_waits.is_lift_note(text):
            if allowed is not None and allowed(who):
                held = {}               # un pilote lève toute la retenue
            else:
                held.pop(who, None)     # l'auteur ne lève que la sienne
    return held


def _current_state(events: list[dict]) -> str | None:
    """L'état courant lu dans le journal (dernier événement), jamais la colonne."""
    state = None
    for event in events or ():
        if event.get("state"):
            state = str(event["state"])
    return state


#: Formules d'attente **explicites** d'un humain : un geste, un login/2FA, un go
#: ou une décision encore requis, un retour ou une demande à l'owner. Une
#: simple mention d'une règle, d'une consigne ou d'une décision déjà signée de
#: l'owner ne classe PAS l'attente humaine : les agents les citent sans cesse
#: (revue dev1 du 24/10, B1). Le statut `waiting_human` reste un signal à part.
_HUMAN_WAIT_PATTERNS = (
    # un geste, un login ou une 2FA que seul l'owner peut faire
    r"\bgeste\s+(?:de\s+l'|de\s+la\s+|du\s+|d'|des\s+)?(?:owner|propri[ée]taire)\b",
    r"\b(?:login|connexion|2fa|double\s+authentification|mot\s+de\s+passe|"
    r"identifiants?|credentials?)\b[^.;]{0,30}\b(?:owner|propri[ée]taire)\b",
    r"\b(?:owner|propri[ée]taire)\b[^.;]{0,30}\b(?:login|connexion|2fa|"
    r"double\s+authentification|mot\s+de\s+passe|identifiants?|credentials?)\b",
    # un go, un accord ou une signature encore requis
    r"\b(?:go|accord|feu\s+vert|validation|signature)\s+(?:de\s+l'|de\s+la\s+|"
    r"du\s+|d')?(?:owner|propri[ée]taire)\b[^.;,]{0,25}\b(?:requis|requise|"
    r"à\s+demander|demand[ée]e?|attendu|en\s+attente|à\s+obtenir|à\s+donner|"
    r"à\s+signer|non\s+sign[ée]e?|pas\s+sign[ée]e?)\b",
    # une décision encore due ou non signée (une décision signée ne l'est plus)
    r"\bd[ée]cision\s+(?:de\s+l'|de\s+la\s+|du\s+|d')?(?:owner|propri[ée]taire)\b"
    r"[^.;,]{0,25}\b(?:due|attendue|à\s+prendre|à\s+demander|à\s+signer|"
    r"non\s+sign[ée]e?|pas\s+sign[ée]e?)\b",
    # la reprise dépend d'une décision de l'owner
    r"\b(?:reprise|reprendre|d[ée]pend)\b[^.;]{0,30}\b(?:de|sur|d')\s+"
    r"(?:la\s+)?d[ée]cision\s+(?:de\s+l'|de\s+la\s+|du\s+|d')?"
    r"(?:owner|propri[ée]taire)\b",
    # retour, attente ou demande à l'owner : l'owner est l'objet direct (une
    # attente d'autre chose près d'une « consigne owner » n'est pas humaine ;
    # « demandée par l'owner » non plus, et « l'owner attend » encore moins)
    r"\b(?:retour|r[ée]ponse|attente)\s+(?:de\s+l'|de\s+la\s+|du\s+|d')"
    r"(?:owner|propri[ée]taire)\b",
    r"\b(?:attendre|attend|attendons|demander|demand[ée]e?s?)\s+"
    r"(?:à\s+l'|à\s+la\s+|au\s+|l'|le\s+|la\s+)(?:owner|propri[ée]taire)\b",
    r"\b(?:owner|propri[ée]taire)\s+(?:(?:doit|devra)\s+(?:encore\s+)?"
    r"(?:valider|signer|donner|d[ée]cider|trancher|r[ée]pondre|approuver|"
    r"fournir|faire|se\s+connecter)|est\s+attendu|reviendra|signera|donnera|"
    r"validera|fournira|transmettra|r[ée]pondra)\b",
    # gestes réservés au bureau ou au propriétaire, non réassignables à un agent
    r"\br[ée]serv[ée]s?\s+(?:au|à\s+la|à\s+l'|aux)\s+(?:bureau\s+ou\s+au\s+)?"
    r"(?:owner|propri[ée]taire)\b",
    r"\bnon\s+r[ée]assignable\s+à\s+un\s+agent\b",
)
_HUMAN_WAIT_RE = re.compile("|".join(_HUMAN_WAIT_PATTERNS), re.I)


def _human_wait(state: str | None, *texts: Any) -> bool:
    """Cette attente dépend-elle d'un humain ? (`--check` l'exclut).

    Seul un état `waiting_human` ou une formule d'attente explicite compte ;
    une règle ou une consigne de l'owner citée au passage ne suffit pas. Un
    `hold_undeployed` n'est jamais humain (canon §11 bis) — l'appelant le
    tranche avant d'appeler ce prédicat."""
    if state == "waiting_human":
        return True
    return any(_HUMAN_WAIT_RE.search(str(text or "")) for text in texts)


def _mark(event: dict) -> dict | None:
    """Une marque de désignation automatique, lue sans colonne de lot."""
    text = str(event.get("note") or "").strip()
    match = _MARK.match(text)
    if not match:
        return None
    try:
        document = json.loads(match.group(3))
    except (ValueError, TypeError):
        return {"lot": int(match.group(1)), "freeze": int(match.group(2)),
                "names": [], "authors": [], "unreadable": True}
    names = []
    for reviewer in document.get("reviewers") or ():
        if isinstance(reviewer, Mapping):
            name = str(reviewer.get("name") or "").strip()
        else:
            name = str(reviewer or "").strip()
        if name and name != "-none":
            names.append(name)
    # Les auteurs de la tête (dont un relecteur qui a poussé un correctif,
    # #2888/#2739) ne peuvent pas la relire : la marque les nomme déjà.
    authors = []
    for author in document.get("authors") or ():
        if isinstance(author, Mapping):
            name = str(author.get("name") or "").strip()
        else:
            name = str(author or "").strip()
        if name:
            authors.append(name)
    return {"lot": int(match.group(1)), "freeze": int(match.group(2)),
            "names": names, "authors": authors, "unreadable": False,
            "at": float(event.get("at_ts") or 0.0)}


def designations(milestones: list[dict], events: list[dict]) -> dict:
    """Les relecteurs désignés sur le dernier gel et leurs avis encore dus.

    Le calcul lit **les jalons et le journal**, jamais `work_items.reviewer`
    ni `reviewers` :

    * le dernier `frozen` du lot et sa tête ;
    * la dernière marque de désignation de ce gel (sélection automatique ou
      report `-carried:`), et `review-designation-none:` quand le lot léger
      n'attend aucun avis ;
    * les désignations manuelles (`reviewer X designated`) et leurs retraits,
      qui survivent aux rotations : un nom jamais porté par une marque compte
      tant qu'il n'est pas retiré ; un nom déjà porté par une marque ne compte
      sur la nouvelle tête que s'il a été redésigné depuis le gel (#3033) ;
      un auteur ou correcteur de la tête, nommé par la marque du dernier gel,
      ne compte jamais (#2888/#2739) ;
    * la dispense `verdict not required:` depuis le gel, levée par une
      désignation explicite ou son retrait (#1979) ;
    * les verdicts qui nomment la tête gelée : un `blocked` rendu compte
      l'avis (le relecteur a rendu), un verdict d'une autre tête ne compte pas ;
    * #5202 : le passage en `validated` depuis le gel, que la porte n'accorde
      qu'au quorum de la tête gelée (#4423, report compris). Comme pour la
      file et le SLO (#4851), aucun désigné n'est plus dû (#5083 : quorum
      cx1 + cx3, dev3 sans avis). Une sortie de `validated` (`validated → …`,
      retour en build) lève ce quorum. Une note du journal écrite en `qa`
      sans transition (passe de report « carried quorum pending ») ne le
      lève pas.
    """
    freeze = _latest_freeze(milestones)
    if freeze is None:
        return {"freeze": None, "designees": [], "pending": [], "missing": False,
                "none": False, "unreadable": [], "quorum": None}
    freeze_at = float(freeze.get("at_ts") or 0.0)
    freeze_sha = str(freeze.get("sha") or "").strip()
    unreadable: list[str] = []
    latest_mark = None
    none_mark = False
    waived = False
    ever: set[str] = set()
    manual: dict[str, dict] = {}
    quorum_at: float | None = None
    for event in sorted(events or (),
                        key=lambda e: (float(e.get("at_ts") or 0.0),
                                       int(e.get("id") or 0))):
        text = str(event.get("note") or "").strip()
        at = float(event.get("at_ts") or 0.0)
        if at >= freeze_at:
            if text.startswith("verdict not required:"):
                waived = True
            elif text == "reviewer designation removed" or _MANUAL.match(text):
                waived = False
        if at > freeze_at:
            state = str(event.get("state") or "")
            if state == "validated":
                if quorum_at is None:
                    quorum_at = at
            elif text.startswith("validated →") or state not in ("", "qa"):
                quorum_at = None
        parsed = _mark(event)
        if parsed is not None:
            for name in parsed["names"]:
                ever.add(_norm(name))
            if parsed.get("unreadable"):
                unreadable.append("designation mark #%s unreadable" % event.get("id"))
            if parsed["freeze"] == int(freeze.get("id") or 0):
                latest_mark = parsed
                none_mark = False
            continue
        match = _MARK_NONE.match(text)
        if match and int(match.group(2)) == int(freeze.get("id") or 0):
            none_mark = True
            continue
        manual_match = _MANUAL.match(text)
        if manual_match:
            name = manual_match.group(1).strip()
            manual[_norm(name)] = {"name": name,
                                   "kind": manual_match.group(2),
                                   "at": float(event.get("at_ts") or 0.0)}
    # #3818/#1979 : la dispense vaut pour ce gel seulement. Un nouveau
    # gel ou une désignation explicite rend la revue à nouveau due.
    if waived:
        return {"freeze": freeze, "designees": [], "pending": [],
                "missing": False, "none": True, "unreadable": [],
                "quorum": None}
    auto = list(latest_mark["names"]) if latest_mark else []
    # Un auteur de la tête — dont le relecteur qui y a poussé un correctif
    # (#2888/#2739) — ne peut pas la relire. La marque du gel courant le dit ;
    # une désignation manuelle antérieure ne doit pas le réintroduire.
    authors = {_norm(name) for name in (latest_mark or {}).get("authors") or ()}
    designees: list[tuple[str, float]] = []
    seen: set[str] = set()
    for name in auto:
        key = _norm(name)
        if key in seen or key in authors:
            continue
        # Le retrait manuel depuis la marque retire aussi un nom automatique
        # (#3033 : le pilote a retiré dev1, dev2 désigné).
        removed = manual.get(key)
        if (removed is not None and removed["kind"] == "removed"
                and removed["at"] >= float((latest_mark or {}).get("at") or freeze_at)):
            continue
        seen.add(key)
        designated = freeze_at
        if latest_mark is not None:
            designated = max(designated, float(latest_mark.get("at") or 0.0))
        designees.append((name, designated))
    for key, choice in manual.items():
        if choice["kind"] != "designated" or key in seen or key in authors:
            continue
        if key in ever and choice["at"] < freeze_at and key not in {_norm(n) for n in auto}:
            # #3033 : un nom déjà porté par une marque ne vaut sur la nouvelle
            # tête que s'il a été redésigné depuis le gel.
            continue
        seen.add(key)
        designees.append((choice["name"], max(freeze_at, choice["at"])))
    by_actor = {}
    for milestone in milestones:
        if milestone.get("kind") != "verdict":
            continue
        if not _verdict_on_head(milestone, freeze, freeze_sha, freeze_at):
            continue
        by_actor[_norm(milestone.get("actor"))] = milestone
    pending = []
    for name, designated_at in designees:
        verdict = by_actor.get(_norm(name))
        if verdict is not None:
            continue
        if quorum_at is not None:
            continue                    # #5202/#4851 : quorum acquis sans son avis
        pending.append({"name": name, "designated_ts": designated_at,
                        "designated": _iso(designated_at)})
    return {"freeze": freeze, "designees": designees, "pending": pending,
            "missing": (latest_mark is None and not none_mark and not designees
                        and not by_actor and quorum_at is None),
            "none": none_mark, "unreadable": unreadable,
            "quorum": _iso(quorum_at) if quorum_at is not None else None}


def _verdict_on_head(verdict: dict, freeze: dict, freeze_sha: str,
                     freeze_at: float) -> bool:
    sha = str(verdict.get("sha") or "").strip()
    if sha and freeze_sha:
        return _same_sha(sha, freeze_sha)
    return float(verdict.get("at_ts") or 0.0) >= freeze_at


def blocking_review(milestones: list[dict], freeze: dict | None) -> dict | None:
    """L'avis `blocked` du dernier gel sans gel plus récent (calcul de #3288)."""
    if freeze is None:
        return None
    freeze_at = float(freeze.get("at_ts") or 0.0)
    freeze_sha = str(freeze.get("sha") or "").strip()
    blocked = [m for m in milestones
               if m.get("kind") == "verdict" and m.get("verdict") == "blocked"
               and _verdict_on_head(m, freeze, freeze_sha, freeze_at)]
    if not blocked:
        return None
    latest = max(blocked, key=lambda m: (float(m.get("at_ts") or 0.0),
                                         int(m.get("id") or 0)))
    return {"at_ts": float(latest.get("at_ts") or 0.0),
            "actor": latest.get("actor") or None,
            "sha": str(latest.get("sha") or "") or freeze_sha}


# -- holds -------------------------------------------------------------------

def _holds_from_events(events: list[dict], allowed=None) -> dict[str, dict]:
    """Repli sans base : la dernière retenue, levée par toute note de levée.

    `allowed` est l'autorité de levée (#3219) ; sans elle, seul l'auteur lève
    sa retenue."""
    from . import planned_waits

    held: dict[str, dict] = {}
    previous = None
    for event in sorted(events or (),
                        key=lambda e: (float(e.get("at_ts") or 0.0),
                                       int(e.get("id") or 0))):
        state = event.get("state")
        if previous is not None and state != previous:
            held = {}
        previous = state
        text = str(event.get("note") or "").strip()
        if not text:
            continue
        if planned_waits.is_hold_note(text):
            who = event.get("actor") or ""
            held[who] = {"by": who or None, "since": float(event.get("at_ts") or 0.0),
                         "note": text,
                         "pilot": bool(allowed(who)) if allowed is not None else False}
        elif planned_waits.is_lift_note(text):
            who = event.get("actor") or ""
            if allowed is not None and allowed(who):
                held = {}               # un pilote lève toute la retenue
            else:
                held.pop(who, None)     # l'auteur ne lève que la sienne
    return held


# -- report ------------------------------------------------------------------

def _hours(now: float, hours: int) -> list[int]:
    end = int(float(now) // 3600) * 3600
    return [end - 3600 * i for i in range(int(hours) - 1, -1, -1)]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _quantiles(values: list[float]) -> dict:
    return {"count": len(values),
            "median_s": statistics.median(values) if values else None,
            "p90_s": _percentile(values, 0.9),
            "max_s": max(values) if values else None}


def build(*, items: Iterable[Mapping[str, Any]],
          milestones: Iterable[Mapping[str, Any]],
          events: Iterable[Mapping[str, Any]],
          agents: Iterable[Mapping[str, Any]] = (),
          turns: Iterable[Mapping[str, Any]] = (),
          merges: Iterable[Mapping[str, Any]] | None = None,
          ci_runs: Iterable[Mapping[str, Any]] = (),
          jobs: Iterable[Mapping[str, Any]] = (),
          now: float | None = None,
          hours: int = DEFAULT_HOURS,
          wait_s: float = DEFAULT_WAIT_S,
          ci_hours: int = DEFAULT_CI_HOURS,
          ci_slow_s: float = DEFAULT_CI_SLOW_S,
          devices: Mapping[str, Any] | None = None,
          installed: str | None = None,
          holds: Mapping[int, Mapping[str, Mapping[str, Any]]] | None = None,
          closure_allowed=None,
          ci_unreadable: Iterable[str] = (),
          packets: Iterable[Mapping[str, Any]] = (),
          zone: str = "UTC") -> dict:
    """Le rapport de débit, calculé sans I/O (témoins et tests).

    `closure_allowed` est l'autorité de levée (#3219) partagée par `read`
    (`planned_waits.lift_authority`) : un pilote lève toute la retenue de
    fermeture, un auteur ordinaire seulement la sienne. Sans elle (appel sans
    base), une levée ne retire que la retenue de son propre auteur.

    #4235 : un hold de pilote qui déclare une **fenêtre d'observation** avec
    son échéance armée (`planned_waits.observation_hold`) est listé dans
    `window_waits`, hors `slow_waits`, jusqu'à cette échéance ; il redevient
    une attente ordinaire ensuite."""
    # #3447 : le préfixe de journal est défini une fois, pour l'écrivain
    # (`work.set_external_executor`) et ce lecteur. #3498 : même lecture pour
    # la déclaration d'attente externe d'un hold.
    from .storage.interface import EXTERNAL_EXECUTOR_NOTE
    from . import planned_waits
    now = time.time() if now is None else float(now)
    installed = installed_version() if installed is None else str(installed)
    installed_triple = _version(installed)
    devices = {"names": [], "phrases": [], "sources": [], "unreadable": []} \
        if devices is None else dict(devices)
    items = [dict(row) for row in items or ()]
    by_id = {int(row["id"]): row for row in items}
    milestones_by_lot = _rows_by_lot(milestones)
    # Le journal n'est pas trié par `at_ts` : il porte son instant.
    events_by_lot: dict[int, list[dict]] = {}
    for row in events or ():
        row = dict(row)
        row.setdefault("at_ts", float(row.get("created_ts") or 0.0))
        events_by_lot.setdefault(int(row["work_item_id"]), []).append(row)
    for values in events_by_lot.values():
        values.sort(key=lambda r: (float(r.get("at_ts") or 0.0), int(r.get("id") or 0)))
    state_by_lot = {ident: _current_state(values)
                    for ident, values in events_by_lot.items()}
    #: le repli sans base ne connaît pas les rôles : il ne peut pas refuser
    #: une déclaration faute de pilote (#3498).
    holds_authoritative = holds is not None
    if holds is None:
        holds = {ident: _holds_from_events(values, allowed=closure_allowed)
                 for ident, values in events_by_lot.items()}
    hold_by_lot = {int(ident): dict(value) for ident, value in (holds or {}).items()}
    # #3497 : après un jalon terminal, une retenue de fermeture survit aux
    # écritures d'état `merged`/`blocked` du harnais (voir `_closure_holds`).
    # Un lot vraiment rouvert (build/qa après la fusion) relève d'`active_holds` :
    # ses changements d'état sont réels et lèvent la retenue.
    for ident, lot_milestones in milestones_by_lot.items():
        terminal_ts = _terminal_at(lot_milestones)
        if terminal_ts is None or _reopened(events_by_lot.get(ident, []), terminal_ts):
            continue
        closure = _closure_holds(events_by_lot.get(ident, []), terminal_ts,
                                 allowed=closure_allowed)
        entries = hold_by_lot.setdefault(ident, {})
        for who, entry in closure.items():
            current = entries.get(who)
            if current is None or float(entry["since"]) > float(
                    current.get("since") or 0.0):
                entries[who] = entry
    held_lots = {ident for ident, entries in hold_by_lot.items() if entries}
    #: branches des lots encore ouverts : une cible de lot qui en nomme une
    #: est une dépendance (le lot entre avec son parent), pas un paquet dû.
    open_branches: dict[tuple[str, str], list[int]] = {}

    qa: list[dict] = []
    blocked_reviews: list[dict] = []
    reviews_due: list[dict] = []
    holds_out: list[dict] = []
    #: #3498 : une déclaration externe vaut pour le lot, pas seulement pour le
    #: hold qui la porte ; les autres retenues du même lot sont couvertes.
    external_by_lot: dict[int, dict] = {}
    #: #4235 : même règle pour une fenêtre d'observation déclarée.
    window_by_lot: dict[int, dict] = {}
    wakes_out: list[dict] = []
    device_holds: list[dict] = []
    undeployed_holds: list[dict] = []
    external_waits: list[dict] = []
    window_waits: list[dict] = []
    unreadable: list[str] = list(devices.get("unreadable") or ())

    for ident, item in by_id.items():
        # #3672 : un lot fermé (ou promu) au journal n'attend plus rien, même
        # sans jalon terminal (#3585, fermé « superseded » puis retenu par
        # une note `hold:`) ; ses holds et réveils ne sont pas des attentes.
        # Un lot `merged` reste lu : il peut attendre son déploiement.
        if state_by_lot.get(ident) in _CLOSED_STATES:
            continue
        lot_milestones = milestones_by_lot.get(ident, [])
        lot_events = events_by_lot.get(ident, [])
        freeze = _latest_freeze(lot_milestones)
        terminal_ts = _terminal_at(lot_milestones)
        if terminal_ts is not None and _reopened(lot_events, terminal_ts):
            terminal_ts = None
        # #3447 : un lot réservé à un exécutant externe n'est pas « sans
        # assigné » ; il attend cet exécutant (bureau, owner), pas un agent.
        # La mesure reste lue sur le journal : la déclaration nommée par
        # `work.EXTERNAL_EXECUTOR_NOTE`, sinon la création du lot.
        external = str(item.get("external_executor") or "").strip()
        state = state_by_lot.get(ident)
        if external and not (item.get("assignee") or "").strip() \
                and terminal_ts is None and state in lifecycle.ACTIVE:
            since = item.get("created_ts")
            for event in lot_events:
                text = str(event.get("note") or "")
                if not text.startswith(EXTERNAL_EXECUTOR_NOTE):
                    continue
                at = event.get("at_ts")
                if at is not None and (since is None or float(at) > float(since)):
                    since = float(at)
            age = _age(now, since)
            external_waits.append({
                "lot": ident, "title": item.get("title") or "",
                "app": item.get("app") or None, "who": external,
                "kind": "external", "state": state,
                "since_ts": since, "since": _iso(since), "age_s": age,
                "gesture": "the external executor %s acts (bureau/owner, not an "
                           "agent); the pilot clears the reservation if it is no "
                           "longer wanted (`ameesh work external-executor %d "
                           "--clear`)" % (external, ident),
            })
        if terminal_ts is None:
            branch = _branch_name(item.get("branch"))
            if branch:
                open_branches.setdefault(
                    (str(item.get("app") or ""), branch), []).append(ident)
        if freeze is not None and terminal_ts is None:
            designation = designations(lot_milestones, lot_events)
            unreadable.extend("%s: %s" % (ident, text)
                              for text in designation["unreadable"])
            pending = [p for p in designation["pending"]
                       if _norm(p["name"]) != _norm(item.get("assignee"))]
            blocked = blocking_review(lot_milestones, freeze)
            age = _age(now, freeze.get("at_ts"))
            state = {
                "lot": ident, "title": item.get("title") or "",
                "app": item.get("app") or None,
                "assignee": item.get("assignee") or None,
                "frozen_ts": float(freeze.get("at_ts") or 0.0),
                "frozen": _iso(freeze.get("at_ts")),
                "sha": str(freeze.get("sha") or "")[:12] or None,
                "age_s": age, "pending_reviewers": [p["name"] for p in pending],
                "blocked_verdict": bool(blocked),
                "missing_reviewer": bool(designation["missing"]),
                "ready": not pending and not designation["missing"] and not blocked,
                "held": ident in held_lots,
            }
            qa.append(state)
            if blocked is not None:
                blocked_reviews.append({
                    "lot": ident, "title": item.get("title") or "",
                    "app": item.get("app") or None,
                    "assignee": item.get("assignee") or None,
                    "held": ident in held_lots,
                    "sha": str(blocked.get("sha") or "")[:12] or None,
                    "blocked_ts": blocked["at_ts"],
                    "blocked": _iso(blocked["at_ts"]),
                    "age_s": _age(now, blocked["at_ts"]),
                    "reviewer": blocked.get("actor") or None,
                })
            for review in designation["pending"]:
                if _norm(review["name"]) == _norm(item.get("assignee")):
                    continue
                reviews_due.append({
                    "lot": ident, "reviewer": review["name"],
                    "sha": str(freeze.get("sha") or "")[:12] or None,
                    "designated_ts": review["designated_ts"],
                    "age_s": _age(now, review["designated_ts"]),
                    "title": item.get("title") or "",
                    "task": "review",
                    "gesture": GESTURES["review"].format(
                        who=review["name"], lot=ident)})
        # holds (les lots mergés peuvent encore attendre un déploiement)
        lot_holds: list[dict] = []
        rejected_window: list[str] = []
        for hold in hold_by_lot.get(ident, {}).values():
            if terminal_ts is not None and float(hold.get("since") or 0.0) <= terminal_ts:
                continue
            note = str(hold.get("note") or "")
            age = _age(now, hold.get("since"))
            # #3498 : la déclaration externe est un geste de pilote ; un hold
            # ordinaire qui la porte ne soustrait pas son lot à `--check`.
            declared = planned_waits.external_hold(note)
            if declared is not None and holds_authoritative \
                    and hold.get("pilot") is False:
                unreadable.append(
                    "lot %d: external wait declared by %s, not a pilot hold: "
                    "it stays an agent wait (#3498)"
                    % (ident, hold.get("by") or "?"))
                declared = None
            # #4235 : une fenêtre d'observation est un geste de pilote, comme
            # la déclaration externe ; une échéance illisible ne devine rien et
            # laisse le hold en attente d'agent (le sens sûr).
            window = planned_waits.observation_hold(note)
            if window is not None:
                if holds_authoritative and hold.get("pilot") is False:
                    # Le lot peut être couvert par la fenêtre d'un autre hold :
                    # le constat n'est écrit que si aucune fenêtre ne tient.
                    rejected_window.append(hold.get("by") or "?")
                    window = None
                else:
                    due_ts = parse_wake_due("(due %s)" % window["due"])
                    if due_ts is None:
                        unreadable.append(
                            "lot %d: observation window due date unreadable: %s"
                            % (ident, window["due"]))
                        window = None
                    else:
                        window["due_ts"] = due_ts
            device = match_device(note, devices)
            versions = _versions(note)
            version = max(versions) if versions else None
            undeployed = bool(_UNDEPLOYED.search(note)) \
                or any(_version_gt(item, installed_triple) for item in versions)
            entry = {
                "lot": ident, "title": item.get("title") or "",
                "app": item.get("app") or None,
                "assignee": item.get("assignee") or None,
                "by": hold.get("by"), "since_ts": hold.get("since"),
                "since": _iso(hold.get("since")), "age_s": age,
                "cause": note, "pilot": bool(hold.get("pilot")),
                "device": device, "undeployed": undeployed,
                "version": ".".join(str(p) for p in version) if version else None,
            }
            if declared is not None:
                entry["external"] = declared
            if window is not None:
                entry["observation"] = window
            holds_out.append(entry)
            lot_holds.append(entry)
            if device:
                entry["device_unavailable"] = True
                device_holds.append(entry)
            if undeployed:
                entry["held_by_undeployed"] = True
                undeployed_holds.append(entry)
        declared_here = [entry for entry in lot_holds if entry.get("external")]
        if declared_here:
            chosen = max(declared_here,
                         key=lambda entry: float(entry.get("since_ts") or 0.0))
            external_by_lot[ident] = chosen
            for entry in lot_holds:
                entry["external_lot"] = True
        # #4235 : une fenêtre déclarée couvre les autres retenues du lot (le
        # témoin #4027 porte deux holds, dont un de l'assigné non pilote) ;
        # une attente externe, plus explicite, garde la priorité.
        window_here = [entry for entry in lot_holds if entry.get("observation")]
        if window_here and ident not in external_by_lot:
            chosen = max(window_here,
                         key=lambda entry: float(entry.get("since_ts") or 0.0))
            window_by_lot[ident] = chosen
            for entry in lot_holds:
                entry["window_lot"] = True
        elif rejected_window:
            for by in rejected_window:
                unreadable.append(
                    "lot %d: observation window declared by %s, not a pilot "
                    "hold: it is not a window (#4235)" % (ident, by))
        # réveils
        wake = wake_state(lot_events)
        if (wake is not None and wake.get("due_ts") is not None
                and not (terminal_ts is not None
                         and float(wake.get("since") or 0.0) <= terminal_ts)):
            entry = {
                "lot": ident, "title": item.get("title") or "",
                "app": item.get("app") or None,
                "assignee": item.get("assignee") or None,
                "by": wake.get("by"), "due_ts": wake["due_ts"],
                "due": _iso(wake["due_ts"]),
                "due_in_s": float(wake["due_ts"]) - now,
                "why": wake.get("why") or "", "since": _iso(wake.get("since")),
                "overdue": float(wake["due_ts"]) < now,
            }
            wakes_out.append(entry)

    # Une cible qui est la branche d'un lot encore ouvert est une dépendance :
    # le lot ne peut pas entrer seul dans un paquet, il suit le lot parent
    # (dont le hold, s'il y en a un, porte déjà l'attente) (#3437).
    dependents_by_lot: dict[int, list[int]] = {}
    for row in qa:
        parent = _target_lot(int(row["lot"]), by_id[int(row["lot"])], open_branches)
        if parent is not None:
            row["depends_on"] = parent
            dependents_by_lot.setdefault(parent, []).append(int(row["lot"]))

    # -- tableau des attentes de plus de 2 h --------------------------------
    waits: list[dict] = []

    def dependents_of(lot) -> dict:
        """Les lots relus que ce lot transporte : leur attente est la sienne."""
        children = dependents_by_lot.get(int(lot))
        return {"dependents": sorted(children)} if children else {}

    def add_wait(kind, entry, since, who, gesture, level=1, human=False, **extra):
        age = _age(now, since)
        if age is None or age < wait_s:
            return
        waits.append(dict({"kind": kind, "lot": entry.get("lot"),
                           "title": entry.get("title") or "",
                           "app": entry.get("app") or None,
                           "since_ts": since, "since": _iso(since),
                           "age_s": age, "who": who, "gesture": gesture,
                           "level": level, "human": bool(human)}, **extra))

    for row in qa:
        if row.get("held"):
            continue                    # la retenue active porte already sa cause
        state = state_by_lot.get(int(row["lot"]))
        reviewers = row["pending_reviewers"]
        if reviewers:
            add_wait("qa", row, row["frozen_ts"], ", ".join(reviewers),
                     ", ".join(GESTURES["review"].format(who=name, lot=row["lot"])
                               for name in reviewers),
                     human=_human_wait(state))
        elif row.get("blocked_verdict"):
            continue                    # l'avis bloquant a son attente dédiée
        elif row.get("missing_reviewer"):
            add_wait("qa", row, row["frozen_ts"], "aucun relecteur",
                     GESTURES["no_reviewer"].format(lot=row["lot"]),
                     human=_human_wait(state))
        else:
            if row.get("depends_on"):
                continue                # l'attente est celle du lot parent
            add_wait("qa_merge", row, row["frozen_ts"], "paquet",
                     GESTURES["qa_merge"], level=2, human=_human_wait(state),
                     **dependents_of(row["lot"]))
    for row in blocked_reviews:
        add_wait("blocking_review", row, row["blocked_ts"],
                 row.get("assignee") or row.get("reviewer") or "auteur",
                 GESTURES["blocking_review"].format(
                     who=row.get("assignee") or "l'auteur"), level=1,
                 human=_human_wait(state_by_lot.get(int(row["lot"]))))
    for row in holds_out:
        if int(row["lot"]) in external_by_lot:
            continue                    # #3498 : attente externe, pas d'agent
        window = window_by_lot.get(int(row["lot"]))
        if (window is not None
                and now < float(window["observation"]["due_ts"])):
            continue                # #4235 : fenêtre voulue et datée, à part
        state = state_by_lot.get(int(row["lot"]))
        # canon §11 bis : un correctif fusionné non déployé se traite sans
        # l'owner ; même une note qui cite une règle ou un go de l'owner ne
        # rend pas cette attente humaine (revue dev1 du 24/10, B1).
        human = False if row.get("undeployed") else _human_wait(
            state, row.get("cause"))
        if row.get("device"):
            add_wait("hold_device", row, row["since_ts"], row.get("by") or "?",
                     GESTURES["hold_device"], level=2, device=row["device"],
                     human=human, **dependents_of(row["lot"]))
        elif row.get("undeployed"):
            add_wait("hold_undeployed", row, row["since_ts"], row.get("by") or "?",
                     GESTURES["hold_undeployed"], level=2,
                     version=row.get("version"), human=human,
                     **dependents_of(row["lot"]))
        else:
            add_wait("hold", row, row["since_ts"], row.get("by") or "?",
                     GESTURES["hold"].format(who=row.get("by") or "?"), level=2,
                     human=human, **dependents_of(row["lot"]))
    # #3498 : une attente externe déclarée par un hold ne relève pas de
    # `--check` ; elle porte sa relance (échéance déclarée, sinon le seuil du
    # débit), que le rapport répète à chaque passage.
    for lot, row in external_by_lot.items():
        declared = row["external"]
        since = row.get("since_ts")
        due_ts = (parse_wake_due("(due %s)" % declared["due"])
                  if declared.get("due") else None)
        if declared.get("due") and due_ts is None:
            unreadable.append("lot %d: external hold due date unreadable: %s"
                              % (lot, declared["due"]))
        reminder_ts = due_ts
        if reminder_ts is None and since is not None:
            reminder_ts = float(since) + max(float(wait_s), 60.0)
        external_waits.append({
            "lot": lot, "title": row["title"], "app": row["app"],
            "assignee": row.get("assignee"),
            "who": declared["executor"], "kind": "hold_external",
            "state": state_by_lot.get(lot),
            "since_ts": since, "since": row.get("since"),
            "age_s": row.get("age_s"),
            "gesture": _external_gesture(declared),
            "declared": declared["gesture"], "hold_by": row.get("by"),
            "cause": row.get("cause"),
            "due_ts": due_ts, "due": _iso(due_ts),
            "reminder_ts": reminder_ts, "reminder": _iso(reminder_ts),
            "reminder_due": bool(reminder_ts is not None and now >= reminder_ts),
            **dependents_of(lot),
        })
    # #4235 : une fenêtre d'observation est une attente voulue et datée ; elle
    # est listée à part **jusqu'à son échéance**, sans relance ni sortie 2 :
    # rien n'est actionnable avant. À l'échéance, le hold redevient une attente
    # ordinaire (et le réveil échu garde son geste). Une attente externe, plus
    # explicite, reste prioritaire.
    for lot, row in window_by_lot.items():
        declared = row["observation"]
        due_ts = float(declared["due_ts"])
        if lot in external_by_lot or now >= due_ts:
            continue
        window_waits.append({
            "lot": lot, "title": row["title"], "app": row["app"],
            "assignee": row.get("assignee"),
            "who": row.get("by"), "kind": "hold_window",
            "state": state_by_lot.get(lot),
            "since_ts": row.get("since_ts"), "since": row.get("since"),
            "age_s": row.get("age_s"),
            "gesture": _window_gesture(declared, _iso(due_ts)),
            "declared": declared["motif"], "hold_by": row.get("by"),
            "cause": row.get("cause"),
            "due_ts": due_ts, "due": _iso(due_ts),
            "due_in_s": due_ts - now,
            **dependents_of(lot),
        })
    #: un réveil sur une attente externe relance l'assigné qui doit porter le
    #: geste au bureau ; ce n'est pas une attente d'agent (#3498).
    external_lots = set(external_by_lot)
    for row in wakes_out:
        if row.get("overdue") and int(row["lot"]) not in external_lots:
            add_wait("wake_overdue", row, row["due_ts"],
                     row.get("assignee") or "?",
                     GESTURES["wake_overdue"], level=1,
                     human=_human_wait(state_by_lot.get(int(row["lot"])),
                                       row.get("why")))
    waits.sort(key=lambda row: (-float(row["age_s"]), str(row["kind"]),
                                int(row["lot"] or 0)))
    #: le canon ne vise que les attentes qui ne dépendent pas d'un humain :
    #: celles de l'owner sont listées à part et ne font pas sortir `--check` 2.
    slow_waits = [row for row in waits if not row["human"]]
    human_waits = [row for row in waits if row["human"]]

    # -- avis dus par relecteur ---------------------------------------------
    per_reviewer: dict[str, dict] = {}
    for review in reviews_due:
        name = review.get("reviewer")
        if not name:
            continue
        entry = per_reviewer.setdefault(name, {"reviewer": name, "count": 0,
                                               "oldest_age_s": 0.0, "lots": []})
        entry["count"] += 1
        entry["oldest_age_s"] = max(float(entry["oldest_age_s"]),
                                    float(review.get("age_s") or 0.0))
        entry["lots"].append(review["lot"])
    reviews_by_reviewer = sorted(per_reviewer.values(),
                                 key=lambda r: (-float(r["oldest_age_s"]), r["reviewer"]))

    # -- agents au repos avec du travail dû ---------------------------------
    due_by_agent: dict[str, dict] = {}
    for review in reviews_due:
        name = review.get("reviewer")
        if not name:
            continue
        entry = due_by_agent.setdefault(name, {"reviews": [], "fixes": [],
                                               "oldest_age_s": 0.0})
        entry["reviews"].append(review["lot"])
        entry["oldest_age_s"] = max(float(entry["oldest_age_s"]),
                                    float(review.get("age_s") or 0.0))
    for row in blocked_reviews:
        name = row.get("assignee")
        if not name:
            continue
        entry = due_by_agent.setdefault(name, {"reviews": [], "fixes": [],
                                               "oldest_age_s": 0.0})
        entry["fixes"].append(row["lot"])
        entry["oldest_age_s"] = max(float(entry["oldest_age_s"]),
                                    float(row.get("age_s") or 0.0))
    idle_with_work = []
    for agent in agents or ():
        agent = dict(agent)
        if str(agent.get("status") or "") != "idle":
            continue
        due = due_by_agent.get(str(agent.get("name")))
        if not due:
            continue
        idle_with_work.append({
            "agent": agent.get("name"), "host": agent.get("host") or None,
            "harness": agent.get("harness") or None,
            "reviews": sorted(set(due["reviews"])),
            "fixes": sorted(set(due["fixes"])),
            "oldest_age_s": due["oldest_age_s"],
            "gesture": GESTURES["review"].format(who=agent.get("name"),
                                                 lot=(due["reviews"] or due["fixes"])[0]),
        })
    idle_with_work.sort(key=lambda row: (-float(row["oldest_age_s"]), row["agent"]))

    # -- tours et fusions par heure -----------------------------------------
    buckets = _hours(now, hours)
    turns_per_hour = {bucket: 0 for bucket in buckets}
    for row in turns or ():
        at = float(row.get("at_ts") or row.get("recorded_ts") or 0.0)
        bucket = int(at // 3600) * 3600
        if bucket in turns_per_hour:
            turns_per_hour[bucket] += 1
    merges_per_hour = {bucket: 0 for bucket in buckets}
    if merges is None:
        merges = [milestone for lot_milestones in milestones_by_lot.values()
                  for milestone in lot_milestones if milestone.get("kind") == "merged"]
    for milestone in merges or ():
        bucket = int(float(milestone.get("at_ts") or 0.0) // 3600) * 3600
        if bucket in merges_per_hour:
            merges_per_hour[bucket] += 1
    throughput_hours = [{"hour_ts": bucket, "hour": _iso(bucket),
                         "turns": turns_per_hour[bucket],
                         "merges": merges_per_hour[bucket]} for bucket in buckets]

    # -- CI ------------------------------------------------------------------
    cutoff = now - float(ci_hours) * 3600
    runs = []
    for row in ci_runs or ():
        row = dict(row)
        started = row.get("started_ts")
        concluded = row.get("concluded_ts")
        if started is None:
            continue
        started = float(started)
        concluded = float(concluded) if concluded is not None else None
        if (concluded if concluded is not None else now) < cutoff:
            continue
        duration = (concluded - started) if concluded is not None else (now - started)
        runs.append({"repo": row.get("repo"), "run_id": row.get("run_id"),
                     "workflow": row.get("workflow") or "",
                     "status": row.get("status") or "",
                     "branch": row.get("branch") or "",
                     "work_item_id": row.get("work_item_id"),
                     "url": row.get("url"),
                     "started_ts": started, "concluded_ts": concluded,
                     "duration_s": max(0.0, duration),
                     "running": concluded is None})
    by_workflow: dict[tuple, list[float]] = {}
    for run in runs:
        by_workflow.setdefault((run["repo"], run["workflow"]), []).append(
            float(run["duration_s"]))
    ci_groups = []
    for (repo, workflow), values in sorted(by_workflow.items()):
        group = {"repo": repo, "workflow": workflow,
                 "slow_count": sum(1 for value in values if value > ci_slow_s)}
        group.update(_quantiles(values))
        ci_groups.append(group)
    slow_runs = sorted((run for run in runs if run["duration_s"] > ci_slow_s),
                       key=lambda run: -float(run["duration_s"]))[:20]
    jobs_by_name: dict[tuple, list[float]] = {}
    slow_jobs = []
    for row in jobs or ():
        duration = float(row.get("duration_s") or 0.0)
        key = (row.get("repo"), row.get("job") or row.get("name") or "?")
        jobs_by_name.setdefault(key, []).append(duration)
        slow_jobs.append(dict(row, duration_s=duration))
    groups_jobs = []
    for (repo, name), values in sorted(jobs_by_name.items()):
        group = {"repo": repo, "job": name,
                 "slow_count": sum(1 for value in values if value > ci_slow_s)}
        group.update(_quantiles(values))
        groups_jobs.append(group)
    slow_jobs = sorted((row for row in slow_jobs
                        if float(row["duration_s"]) > ci_slow_s),
                       key=lambda row: -float(row["duration_s"]))[:20]

    # -- attente qa_merge par jour (#3256 C) --------------------------------
    from . import packet_parallel
    qa_merge = packet_parallel.daily_waits(
        packets, [mark for marks in milestones_by_lot.values() for mark in marks],
        zone=zone, now=now)

    # -- total et sortie -----------------------------------------------------
    totals = {
        "lots": len(by_id),
        "qa": len(qa),
        "blocked_reviews": len(blocked_reviews),
        "reviews_due": len([r for r in reviews_due if r.get("reviewer")]),
        "missing_reviewer": len([row for row in qa if row.get("missing_reviewer")]),
        "holds": len(holds_out),
        "wakes": len(wakes_out),
        "device_holds": len(device_holds),
        "undeployed_holds": len(undeployed_holds),
        "idle_with_work": len(idle_with_work),
        "slow_waits": len(slow_waits),
        "human_waits": len(human_waits),
        "external_waits": len(external_waits),
        "external_reminders": len([row for row in external_waits
                                   if row.get("reminder_due")]),
        "window_waits": len(window_waits),
        "slow_ci_runs": len([run for run in runs
                             if float(run["duration_s"]) > ci_slow_s]),
    }
    return {
        "schema": SCHEMA,
        "at": now, "at_iso": _iso(now),
        "window_s": int(hours) * 3600, "hours": int(hours),
        "wait_s": float(wait_s),
        "installed": installed,
        "devices": {"names": list(devices.get("names") or ()),
                    "phrases": list(devices.get("phrases") or ()),
                    "sources": list(devices.get("sources") or ()),
                    "unreadable": list(devices.get("unreadable") or ())},
        "qa_merge": qa_merge,
        "totals": totals,
        "slow_waits": slow_waits,
        "human_waits": human_waits,
        "external_waits": sorted(
            external_waits,
            key=lambda row: (-float(row.get("age_s") or 0.0), row["lot"])),
        "window_waits": sorted(
            window_waits, key=lambda row: (float(row["due_ts"]), row["lot"])),
        "qa": sorted(qa, key=lambda row: (-float(row["age_s"] or 0.0), row["lot"])),
        "blocked_reviews": sorted(blocked_reviews,
                                  key=lambda row: (-float(row["age_s"] or 0.0),
                                                   row["lot"])),
        "reviews_due": sorted(reviews_due,
                              key=lambda row: (-float(row["age_s"] or 0.0),
                                               row["lot"],
                                               str(row.get("reviewer") or ""))),
        "reviews_by_reviewer": reviews_by_reviewer,
        "holds": sorted(holds_out,
                        key=lambda row: (-float(row["age_s"] or 0.0), row["lot"])),
        "wakes": sorted(wakes_out, key=lambda row: (row["due_ts"], row["lot"])),
        "device_holds": device_holds,
        "undeployed_holds": sorted(undeployed_holds,
                                   key=lambda row: (-float(row["age_s"] or 0.0),
                                                    row["lot"])),
        "idle_agents_with_work": idle_with_work,
        "throughput": {
            "hours": throughput_hours,
            "turns_total": sum(turns_per_hour.values()),
            "merges_total": sum(merges_per_hour.values()),
        },
        "ci": {
            "window_hours": int(ci_hours),
            "slow_s": float(ci_slow_s),
            "runs": len(runs),
            "by_workflow": ci_groups,
            "slow_runs": slow_runs,
            "by_job": groups_jobs,
            "slow_jobs": slow_jobs,
            "unreadable": list(ci_unreadable),
        },
        "unreadable": list(dict.fromkeys(unreadable)),
    }


# -- capacité (#3256 B) ------------------------------------------------------

def _decoupage_project(lot: Mapping[str, Any]) -> str | None:
    """#3256 : le projet d'un lot de découpage, lu à sa `source`
    (`file-decoupage:<projet>`, #4273), sinon None."""
    from .file_decoupage import SOURCE_PREFIX
    source = str(lot.get("source") or "")
    if not source.startswith(SOURCE_PREFIX):
        return None
    return source[len(SOURCE_PREFIX):] or None


def _exhausted_project(note: Any) -> str | None:
    """Le projet d'une note `roadmap: épuisée <projet>` (#4273), sinon None."""
    from .file_decoupage import EXHAUSTED_RE
    match = EXHAUSTED_RE.match(str(note or ""))
    return match.group(1).strip(".,;:") if match else None


def _roadmap_verdicts(notes: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """#5149 : `{projet: note d'épuisement}` des projets dont la **dernière**
    note de feuille de route, posée sur le lot de découpage du même projet
    (§1), est `roadmap: épuisée <projet>` — la règle de `roadmap_state` que
    suivent le sommeil et `ameesh alerts` (#4623) : une note `roadmap:
    rouverte <projet>` postérieure la retire. Les notes sont lues dans l'ordre
    du journal (`at_ts`, puis ordre d'entrée)."""
    from .file_decoupage import REOPENED_RE
    out: dict[str, dict] = {}
    for note in sorted(notes or (), key=lambda r: float(r.get("at_ts") or 0.0)):
        text = str(note.get("note") or "")
        project = _exhausted_project(text)
        if project and _decoupage_project(note) == project:
            out[project] = {"lot": note.get("work_item_id"),
                            "at_ts": float(note.get("at_ts") or 0.0)}
            continue
        match = REOPENED_RE.match(text)
        project = match.group(1).strip(".,;:") if match else None
        if project and _decoupage_project(note) == project:
            out.pop(project, None)
    return out


#: #5149 : les motifs d'exemption du sommeil qui tiennent un siège de
#: relecture (`file_decoupage._sleep`) ; les autres (lot actionnable,
#: courrier) se lisent déjà à l'agent.
_REVIEW_SEAT_WHY = ("designated review pending", "only eligible reviewer")


def _review_seats(notes: Iterable[Mapping[str, Any]],
                  exhausted: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """#5149 : `{agent: raison}` des agents épargnés du sommeil pour un siège
    de relecture, lus aux notes `sleep exempted: <agent>: <motif>` du lot de
    découpage d'un projet épuisé, postérieures à sa note d'épuisement. Un
    siège tenu pour un avis n'est pas de la capacité au repos."""
    from .file_decoupage import SLEEP_EXEMPT_PREFIX
    out: dict[str, str] = {}
    for note in notes or ():
        text = str(note.get("note") or "").strip()
        if not text.startswith(SLEEP_EXEMPT_PREFIX):
            continue
        entry = exhausted.get(_decoupage_project(note) or "")
        if (entry is None or note.get("work_item_id") != entry["lot"]
                or float(note.get("at_ts") or 0.0) < entry["at_ts"]):
            continue
        name, _, why = text[len(SLEEP_EXEMPT_PREFIX):].partition(":")
        name, why = name.strip(), why.strip()
        if name and name != "*" and why.startswith(_REVIEW_SEAT_WHY):
            out[name] = "review seat: %s" % why
    return out


def _busy_spans(turns: Iterable[Mapping[str, Any]], start: float,
                end: float) -> dict[str, list[tuple[float, float]]]:
    """{agent: [(début, fin)]} des tours **sur un lot**, bornés à la fenêtre,
    fusionnés. Un tour de courrier sans lot ne fait pas sortir du repos : un
    agent sans lot en prend toutes les quelques minutes (N9 de #1677)."""
    spans: dict[str, list[tuple[float, float]]] = {}
    for row in turns or ():
        if row.get("work_item_id") is None or not row.get("agent"):
            continue
        begin = float(row.get("at_ts") or 0.0)
        finish = max(float(row.get("end_ts") or begin), begin)
        if finish < start or begin > end:
            continue
        spans.setdefault(str(row["agent"]), []).append((max(begin, start),
                                                        min(finish, end)))
    for name, values in spans.items():
        values.sort()
        merged = [values[0]]
        for begin, finish in values[1:]:
            if begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
            else:
                merged.append((begin, finish))
        spans[name] = merged
    return spans


def _idle_gaps(spans: list[tuple[float, float]], start: float, end: float,
               min_gap_s: float) -> float:
    """Les secondes de la fenêtre hors tour sur un lot, en ne comptant que les
    intervalles d'au moins `min_gap_s` (un agent entre deux tours d'un même
    lot, CI en cours, n'est pas « au repos depuis 30 min »)."""
    total, cursor = 0.0, start
    for begin, finish in list(spans) + [(end, end)]:
        gap = begin - cursor
        if gap > 0 and gap >= min_gap_s:
            total += gap
        cursor = max(cursor, finish)
    return total


def capacity(*, agents: Iterable[Mapping[str, Any]],
             turns: Iterable[Mapping[str, Any]] = (),
             startable: Iterable[Mapping[str, Any]] = (),
             footprint: Mapping[int, Iterable[Mapping[str, Any]]] | None = None,
             open_lots: Iterable[Mapping[str, Any]] = (),
             roadmap_notes: Iterable[Mapping[str, Any]] = (),
             losses: Iterable[Mapping[str, Any]] = (),
             now: float, window_s: float,
             repos_part: float | None, repos_s: float | None,
             canons=(), configured: bool = False,
             held: Mapping[str, str] | None = None,
             families: Iterable[str] = (),
             excluded_families: Iterable[str] = (),
             gating_ok: bool = True,
             rule_source: str = "") -> dict:
    """La section `capacity` du débit (spec #3256 §3.1), sans I/O.

    * `agents` : le relevé des agents (`operations.listing`) ; un agent
      **réveillable** l'est au sens de #4273 (`file_decoupage.wakeable` :
      exécution, ni éphémère, ni chat, ni arrêté, ni en pause, ni critique ni
      relecteur — `critical_verdict` sur `canons`, `configured`), et en
      plus ni retiré (`retired_ts`), ni sans exécuteur vivant
      (`_awake_runner`), ni tenu par son budget (`held`), ni d'une famille
      exclue des prises (`excluded_families`, #5005 : cette capacité-là est
      volontairement écartée, elle n'est pas une perte). `families` est la
      liste du canon qui lit la famille d'un harnais. Il compte dans le
      projet primaire de sa fiche, et une fois au mesh.
    * **au repos depuis `repos_s`** : sans lot actionnable (`idle_agents`)
      et sans tour sur un lot depuis `repos_s` ; `idle_share` = au repos /
      réveillables.
    * `idle_agent_hours` : sur la fenêtre, le temps hors tour sur un lot des
      agents **au repos** (les seuls comptés dans `idle`), par intervalles
      d'au moins `repos_s`. Un agent qui garde un lot actionnable, dont le
      budget tient le tour, ou qu'une famille exclut n'est pas une capacité
      disponible : ses trous ne comptent pas, pour que la perte affichée soit
      celle des agents qu'on peut réellement réveiller.
      L'état réveillable est celui de l'instant ; un agent d'un projet à la
      feuille de route épuisée ne compte d'heures ni dans son projet ni au mesh.
    * `startable` : les lots démarrables (`split_startable`) ; `footprint` :
      `{lot: [bloqueurs]}` des lots d'intake retenus par la garde d'empreinte
      (#3185, #4561) — ils ne sont jamais démarrables, ils attendent la fusion
      de leur bloqueur et se lisent à part (`footprint_lots`) ; `open_lots` :
      les lots ouverts, où le lot de découpage se lit à sa `source`.
    * `roadmap_notes` : `{note, at_ts, work_item_id, source}` ; seule une
      note posée **sur un lot de découpage du même projet** vaut (§1), et la
      dernière fait foi comme pour le sommeil et `ameesh alerts` (#5149) : une
      note `roadmap: rouverte` ou un lot démarrable de l'instant rouvrent la
      route ; un lot déjà pris ne la rouvre pas. Les notes `sleep exempted:`
      d'un siège de relecture tiennent l'agent hors de la capacité.
    * `losses` : la perte au reset par compte, comme `ameesh accounts`.
    * `gating_ok` : faux quand une lecture dont dépend l'alerte a échoué
      (lots ouverts, notes, tenues de budget, historique) ; la mesure reste
      rendue, mais aucune alerte n'est levée sur une lecture incomplète.

    Une alerte par projet quand `idle_share ≥ repos_part`, que la feuille de
    route n'est pas épuisée et qu'aucun lot de découpage n'a moins de
    `file_decoupage.FALLBACK_S` : c'est elle qui fait sortir `--check` 2."""
    from . import file_decoupage
    from . import projects as projects_mod
    from . import sous_utilisation
    now = float(now)
    start = now - float(window_s)
    held = dict(held or {})
    roadmap_notes = list(roadmap_notes or ())
    exhausted = _roadmap_verdicts(roadmap_notes)
    for name, why in _review_seats(roadmap_notes, exhausted).items():
        held.setdefault(name, why)
    min_gap = float(repos_s) if repos_s is not None else 0.0
    rows = [dict(row) for row in agents or ()]
    spans = _busy_spans(turns, start, now)
    last_lot_turn = {name: values[-1][1] for name, values in spans.items()}

    excluded = {str(name).strip().lower() for name in excluded_families or () if name}
    known_families = tuple(families or ())
    family_of = None
    if excluded:
        from .review_lot import family_of
    awake = [row for row in file_decoupage.wakeable(rows, list(canons or ()),
                                                    configured=configured)
             if not row.get("retired_ts") and sous_utilisation._awake_runner(row)
             and row["name"] not in held
             and not (family_of is not None
                      and family_of(row.get("harness"), known_families) in excluded)]
    idle_now = {row["name"] for row in sous_utilisation.idle_agents(
        awake, now, min_gap, waiting_free=True, actionable_idle=True)}
    rested = {name for name in idle_now
              if now - last_lot_turn.get(name, start) >= min_gap}

    # #4561 : les lots retenus par la garde d'empreinte ne sont pas
    # démarrables — `work assign` les refuse — et se lisent à part, avec le
    # lot qui les retient et son état.
    lots_by_id = {int(lot["id"]): dict(lot) for lot in open_lots or ()}
    footprint = {int(ident): [dict(blocker) for blocker in blockers]
                 for ident, blockers in (footprint or {}).items() if blockers}
    footprint_by: dict[str, list[dict]] = {}
    for ident, blockers in sorted(footprint.items()):
        lot = lots_by_id.get(ident) or {}
        project = projects_mod.lot_project(lot) or ""
        delayed = []
        for blocker in blockers:
            other = lots_by_id.get(int(blocker["id"])) or blocker
            last = (other.get("last_activity_ts") or other.get("updated_ts")
                    or other.get("created_ts"))
            delayed.append({
                "id": int(blocker["id"]),
                "state": other.get("state") or blocker.get("state") or "?",
                "assignee": other.get("assignee") or blocker.get("assignee") or None,
                "stalled_s": (int(max(0.0, now - float(last)))
                              if last is not None else None)})
        # #4818 : la raison de l'attente — chevauchement, ou plafond de lots
        # en vol du dépôt (tous ses occupants le retiennent alors)
        reason = ("cap" if blockers and all(b.get("reason") == "cap" for b in blockers)
                  else "overlap")
        footprint_by.setdefault(project, []).append({
            "lot": ident, "state": lot.get("state") or "",
            "reason": reason, "blockers": delayed})
    startable = [dict(lot) for lot in startable or ()
                 if int(lot["id"]) not in footprint]
    startable_by: dict[str, list[int]] = {}
    for lot in startable:
        startable_by.setdefault(projects_mod.lot_project(lot) or "",
                                []).append(int(lot["id"]))
    decoupage: dict[str, dict] = {}
    for lot in lots_by_id.values():
        project = _decoupage_project(lot)
        if not project:
            continue
        created = _epoch(lot.get("created_ts"))
        entry = {"lot": int(lot["id"]), "state": lot.get("state"),
                 "assignee": lot.get("assignee") or None,
                 "created_ts": created, "age_s": _age(now, created)}
        current = decoupage.get(project)
        if current is None or (created or 0.0) > (current.get("created_ts") or 0.0):
            decoupage[project] = entry
    for project in list(exhausted):
        # un lot en attente d'empreinte est du travail réel, pas une feuille
        # de route épuisée : le projet ne se tait pas (#4561).
        if startable_by.get(project) or footprint_by.get(project):
            del exhausted[project]

    by_project: dict[str, list[dict]] = {}
    primary: dict[str, str] = {}
    for row in awake:
        names = projects_mod.agent_projects(row)
        primary[row["name"]] = names[0] if names else ""
        by_project.setdefault(primary[row["name"]], []).append(row)

    def summary(members: list[dict], project: str | None) -> dict:
        names = sorted(row["name"] for row in members)
        idle = sorted(name for name in names if name in rested)
        # un agent d'un projet à la feuille de route épuisée ne compte
        # d'heures au repos ni dans son projet, ni au mesh ; seuls les
        # agents au repos en comptent (ni lot actionnable, ni pause budget,
        # ni famille exclue) : les trous d'un agent occupé ne sont pas de la
        # capacité disponible (#5043)
        seconds = sum(_idle_gaps(spans.get(name, []), start, now, min_gap)
                      for name in idle if primary.get(name) not in exhausted)
        share = (len(idle) / len(names)) if names else 0.0
        return {"wakeable": len(names), "idle": len(idle), "idle_agents": idle,
                "idle_share": round(share, 4),
                "idle_agent_hours": round(seconds / 3600.0, 2),
                "roadmap_exhausted": exhausted.get(project) if project else None}

    projects_out = []
    alerts = []
    for project in sorted(set(by_project) | {p for p in startable_by if p}
                          | {p for p in footprint_by if p}):
        if not project:
            continue
        entry = summary(by_project.get(project, []), project)
        entry["project"] = project
        entry["startable_lots"] = sorted(startable_by.get(project, []))
        entry["footprint_lots"] = sorted(footprint_by.get(project, []),
                                         key=lambda row: row["lot"])
        entry["decoupage"] = decoupage.get(project)
        projects_out.append(entry)
        fresh = entry["decoupage"] is not None and (
            entry["decoupage"].get("age_s") or 0.0) < file_decoupage.FALLBACK_S
        if (gating_ok and repos_part is not None and entry["wakeable"]
                and entry["idle_share"] >= float(repos_part)
                and entry["roadmap_exhausted"] is None and not fresh):
            alerts.append({
                "project": project, "idle": entry["idle"],
                "wakeable": entry["wakeable"], "idle_share": entry["idle_share"],
                "startable_lots": entry["startable_lots"][:20],
                "footprint_lots": entry["footprint_lots"][:20],
                "decoupage": entry["decoupage"],
                "gesture": _capacity_gesture(project, entry)})
    mesh = summary(awake, None)
    mesh["startable_lots"] = sum(len(v) for v in startable_by.values())
    mesh["footprint_lots"] = sum(len(v) for v in footprint_by.values())
    loss_rows = sorted((dict(row) for row in losses or ()),
                       key=lambda r: (str(r.get("harness")), str(r.get("account"))))
    return {
        "rule": ({"repos_part": float(repos_part), "repos_s": float(repos_s),
                  "source": rule_source}
                 if repos_part is not None and repos_s is not None else None),
        "window_s": float(window_s),
        "decoupage_grace_s": file_decoupage.FALLBACK_S,
        "gating_ok": bool(gating_ok),
        "mesh": mesh,
        "projects": projects_out,
        "plan_loss_at_reset": loss_rows,
        "held": [{"agent": name, "reason": reason} for name, reason in sorted(held.items())],
        "alerts": alerts,
    }


def _capacity_gesture(project: str, entry: Mapping[str, Any]) -> str:
    """Le geste d'une alerte de capacité, dans l'ordre de docs/AUDITEUR.md :
    confier d'abord les lots démarrables, puis nommer les lots en attente
    d'empreinte et le lot qui les retient (#4561), puis le repli du découpage,
    puis l'ouverture du lot de découpage au pilote."""
    head = "lots démarrables pour %d agent(s) de %s : " % (entry["idle"], project)
    if entry.get("startable_lots"):
        return head + "confier les lots démarrables (ameesh work assign)"
    if entry.get("footprint_lots"):
        return head + _footprint_gesture(entry["footprint_lots"])
    if entry.get("decoupage"):
        return head + ("le lot de découpage #%d a plus d'une heure sans lot démarrable "
                       "— l'auditeur crée les lots (niveau 1, spec §2.2)"
                       % entry["decoupage"]["lot"])
    return head + "aucun lot de découpage ouvert — l'ouvrir au pilote (spec #3256 §2.2)"


def _footprint_gesture(rows: Iterable[Mapping[str, Any]]) -> str:
    """#4561 : les lots d'intake que la garde d'empreinte (#3185) retient, et
    le geste dû. L'assignation n'est pas ce geste : `work assign` refuse le
    lot, et le temps perdu est celui du bloqueur — son verdict ou son paquet.
    Un bloqueur sans mouvement depuis 2 h est nommé comme tel."""
    parts = []
    for row in rows:
        blockers = list(row.get("blockers") or ())
        names = ", ".join(
            "#%d (%s)" % (int(blocker["id"]), blocker.get("state") or "?")
            for blocker in blockers)
        stalled = [blocker for blocker in blockers
                   if (blocker.get("stalled_s") or 0) >= 2 * 3600]
        tail = ""
        if stalled:
            tail = " ; sans mouvement depuis %s : %s" % (
                _duration(stalled[0].get("stalled_s")),
                ", ".join("#%d" % int(blocker["id"]) for blocker in stalled))
        if row.get("reason") == "cap":
            parts.append("#%d (%s) attend le plafond de lots en vol du dépôt (%s)%s"
                         % (int(row["lot"]), row.get("state") or "intake", names, tail))
            continue
        parts.append("#%d (%s) attend %s%s"
                     % (int(row["lot"]), row.get("state") or "intake", names, tail))
    return ("lots en attente d'empreinte : %s — le geste dû porte sur le lot "
            "qui les retient (verdict, paquet), pas sur l'assignation"
            % " ; ".join(parts))


def plan_losses(account_gauges: Mapping[Any, list], now: float) -> list[dict]:
    """#3256 : la part de fenêtre perdue au reset par compte de forfait, comme
    `ameesh accounts` (`accounts.lost_at_reset`, #1561 : un relevé périmé
    n'annonce aucune perte et le dit). `account_gauges` : la forme de
    `sous_utilisation.latest_account_gauges`."""
    from . import accounts as accounts_mod
    from . import cost as cost_mod
    out = []
    for (harness, account), pairs in sorted(account_gauges.items(),
                                            key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        gauges = [gauge for gauge, _ in pairs]
        stale = bool(gauges) and cost_mod.stale_reading(gauges, now)
        found = None if stale else accounts_mod.expiry(gauges, now)
        out.append({"harness": harness, "account": account, "stale": stale,
                    "lost": round(float(found[1]), 4) if found else None,
                    "window": found[2].key if found else None,
                    "resets_in_s": (float(found[0]) - now) if found else None})
    return out


# -- lecture -----------------------------------------------------------------

def _epoch(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        return float(value.timestamp())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rows(db, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in db.query(sql, params) or ()]


#: les colonnes d'un lot lues sont des **étiquettes** (id, titre, projet,
#: assigné, exécutant externe déclaré, instant de création, branche et cible) ;
#: jamais state, reviewer(s), wake_at ni updated_at (canon 24/10). L'état
#: courant d'un lot se lit au journal (`state_by_lot`), pas dans sa colonne.
_ITEMS_SQL = f"""
    SELECT w.id, w.title, w.app, w.assignee, w.priority,
           to_jsonb(w)->>'external_executor' AS external_executor,
           extract(epoch FROM w.created_at)::float8 AS created_ts,
           w.branch, w.branch_target
      FROM work_items w
     WHERE (EXISTS (SELECT 1 FROM work_item_milestones m
                     WHERE m.work_item_id = w.id AND m.at > to_timestamp(%s))
         OR EXISTS (SELECT 1 FROM work_item_events e
                     WHERE e.work_item_id = w.id AND e.created_at > to_timestamp(%s)))
       AND (
             NOT EXISTS (
               SELECT 1 FROM work_item_milestones t
                WHERE t.work_item_id = w.id
                  AND t.kind IN ('merged', 'closed', 'delivered')
                  AND t.at > coalesce(
                        (SELECT max(f.at) FROM work_item_milestones f
                          WHERE f.work_item_id = w.id AND f.kind = 'frozen'),
                        to_timestamp(0)))
             OR EXISTS (
               -- B2 (cx1) : un lot fusionné peut encore porter un hold ou une
               -- réouverture après son jalon terminal (correctif non déployé,
               -- merged → build) ; `build` les lit, `read` doit le sélectionner.
               SELECT 1 FROM work_item_events e2
                WHERE e2.work_item_id = w.id
                  AND e2.created_at > (
                        SELECT max(t.at) FROM work_item_milestones t
                         WHERE t.work_item_id = w.id
                           AND t.kind IN ('merged', 'closed', 'delivered'))
                  AND (lower(left(e2.note, 21)) LIKE 'hold:%%'
                       OR lower(left(e2.note, 20)) LIKE 'wake:%%'
                       OR e2.state IN ('{lifecycle.REQUESTED}', '{lifecycle.IN_PROGRESS}', '{lifecycle.QA}', '{lifecycle.VALIDATED}'))))
     ORDER BY w.id
     LIMIT %s
"""

_MILESTONES_SQL = """
    SELECT work_item_id, id, kind,
           extract(epoch FROM at)::float8 AS at_ts,
           sha, actor, verdict, note
      FROM work_item_milestones
     WHERE work_item_id = ANY(string_to_array(%s, ',')::bigint[])
     ORDER BY work_item_id, at, id
"""

#: le journal entier des lots retenus : les transitions y remettent les
#: retenues à zéro (`active_holds`), les marques, les désignations et les
#: réveils s'y lisent. Aucune colonne d'état du lot n'est lue.
_EVENTS_SQL = """
    SELECT work_item_id, id, state,
           extract(epoch FROM created_at)::float8 AS at_ts,
           actor, note
      FROM work_item_events
     WHERE work_item_id = ANY(string_to_array(%s, ',')::bigint[])
       AND created_at > to_timestamp(%s)
     ORDER BY work_item_id, id
"""

_AGENTS_SQL = """
    SELECT name, status, host, harness, model, effort,
           extract(epoch FROM last_turn_at)::float8 AS last_turn_ts
      FROM agent_registry
     ORDER BY name
"""

_TURNS_SQL = """
    SELECT extract(epoch FROM coalesce(started_at, recorded_at))::float8 AS at_ts,
           extract(epoch FROM recorded_at)::float8 AS end_ts,
           agent, work_item_id
      FROM turn_costs
     WHERE recorded_at > to_timestamp(%s)
       AND coalesce(void_reason, '') = ''
     ORDER BY recorded_at
"""

#: #3256 : les notes `roadmap:` avec la `source` de leur lot (seule une note
#: sur un lot de découpage du projet vaut, spec §1).
_ROADMAP_SQL = """
    SELECT e.work_item_id, e.note, w.source,
           extract(epoch FROM e.created_at)::float8 AS at_ts
      FROM work_item_events e
      JOIN work_items w ON w.id = e.work_item_id
     WHERE e.created_at > to_timestamp(%s)
       AND w.source LIKE 'file-decoupage:%%'
       AND (lower(left(ltrim(e.note), 8)) = 'roadmap:'
            OR ltrim(e.note) LIKE 'sleep exempted:%%')
     ORDER BY e.created_at
"""

#: #3256 : la `source` des lots ouverts de découpage. `open_lots_activity`
#: ne la rend pas (B6 d'impl-04 sur ac79c550) : elle est lue ici pour les
#: seuls lots ouverts qu'elle a rendus, sans élargir l'interface partagée.
_SPLIT_SOURCES_SQL = """
    SELECT id, source
      FROM work_items
     WHERE id = ANY(string_to_array(%s, ',')::bigint[])
       AND source LIKE 'file-decoupage:%%'
"""

_MERGES_SQL = """
    SELECT extract(epoch FROM at)::float8 AS at_ts
      FROM work_item_milestones
     WHERE kind = 'merged' AND at > to_timestamp(%s) AND at <= to_timestamp(%s)
     ORDER BY at
"""

_CI_SQL = """
    SELECT repo, run_id, run_attempt, workflow, status, branch, work_item_id, url,
           extract(epoch FROM
               coalesce(rc.started_at, rc.created_at, rc.first_seen_at))::float8 AS started_ts,
           extract(epoch FROM rc.concluded_at)::float8 AS concluded_ts
      FROM ci_runs rc
     WHERE coalesce(rc.concluded_at, to_timestamp(%s)) > to_timestamp(%s)
       AND coalesce(rc.started_at, rc.created_at, rc.first_seen_at) > to_timestamp(%s)
       AND coalesce(rc.started_at, rc.created_at, rc.first_seen_at) IS NOT NULL
     ORDER BY coalesce(rc.concluded_at, to_timestamp(%s)) DESC
     LIMIT %s
"""

#: #3256 C : l'ouverture de chaque paquet d'un lot, pour l'attente qa_merge.
#: La composition est datée par la création du porteur (stable sur une
#: recomposition) ; le gel effectif de l'unité est celui enregistré par
#: `work packet record`, et le jalon verdict de la même tête est lu par
#: `packet_parallel.daily_waits`. La lecture ne part **pas** des lots de
#: `_ITEMS_SQL` : un lot fusionné en est exclu, et c'est justement un lot relu
#: puis composé que la mesure doit compter (relecture auditeur du 28/10).
_PACKETS_SQL = """
    SELECT m.package_id, m.work_item_id, m.frozen_sha,
           extract(epoch FROM p.created_at)::float8 AS composed_ts
      FROM work_packet_members m
      JOIN work_items p ON p.id = m.package_id
     WHERE p.created_at > to_timestamp(%s)
     ORDER BY p.created_at, m.work_item_id
"""


def read(db, *, cfg=None, now: float | None = None, hours: int = DEFAULT_HOURS,
         wait_s: float = DEFAULT_WAIT_S, ci_hours: int = DEFAULT_CI_HOURS,
         ci_slow_s: float = DEFAULT_CI_SLOW_S, canons=None,
         installed: str | None = None, jobs: bool = True,
         api=None) -> dict:
    """Lit la base et rend le rapport (voir `build`). Une lecture illisible
    n'est jamais une file vide : elle est nommée dans `unreadable`."""
    now = time.time() if now is None else float(now)
    horizon = now - EVENT_DAYS * 86400
    unreadable: list[str] = []
    items = _rows(db, _ITEMS_SQL, (horizon, horizon, MAX_ITEMS))
    if len(items) >= MAX_ITEMS:
        unreadable.append("open-lot limit reached (%d): list truncated" % MAX_ITEMS)
    ids = [int(row["id"]) for row in items]
    # #3256 C : la mesure qa_merge part des **membres de paquet** de la
    # fenêtre, jamais des seuls lots ouverts (`_ITEMS_SQL` écarte un lot
    # fusionné, alors que son attente est justement mesurée). Les jalons de ces
    # lots sont lus au même passage.
    from . import packet_parallel
    qa_window = now - (packet_parallel.DAYS + 1) * 86400
    packets = _rows(db, _PACKETS_SQL, (qa_window,))
    packet_ids = {int(row["work_item_id"]) for row in packets}
    all_ids = sorted(set(ids) | packet_ids)
    if all_ids:
        # Tableau passé en littéral : le pilote `psql` de secours n'adapte pas
        # une liste Python en tableau PostgreSQL (le pilote psycopg si).
        packed = ",".join(str(int(ident)) for ident in all_ids)
        milestones = _rows(db, _MILESTONES_SQL, (packed,))
    else:
        milestones = []
    if ids:
        packed_open = ",".join(str(int(ident)) for ident in ids)
        events = _rows(db, _EVENTS_SQL, (packed_open, horizon))
    else:
        events = []
    # Le jour de la mesure qa_merge est le jour local du canon (§11 bis) ;
    # un canon illisible ne devine pas Europe/Paris : il dit UTC, sans
    # ajouter une lecture illisible au rapport des lots.
    zone = "UTC"
    try:
        from . import mechanics
        zone = mechanics.configured(cfg).timezone
    except Exception:  # noqa: BLE001 - zone de repli explicite, jamais devinée
        pass
    agents = _rows(db, _AGENTS_SQL)
    turns = _rows(db, _TURNS_SQL, (now - int(hours) * 3600,))
    merges = _rows(db, _MERGES_SQL,
                   (now - int(hours) * 3600, now))
    ci_runs = _rows(db, _CI_SQL,
                    (now, now - int(ci_hours) * 3600, now - int(ci_hours) * 3600,
                     now, MAX_CI_RUNS))

    # Appareils déclarés indisponibles : fiches Host + règle du canon §12.
    if canons is None:
        try:
            from . import canon as canon_mod
            canons = canon_mod.load_configured(cfg) if cfg is not None else []
        except Exception as exc:  # noqa: BLE001 - canon illisible : dit, pas deviné
            canons = []
            unreadable.append("canon unavailable: %s" % " ".join(str(exc).split())[:200])
    devices = unavailable_devices(canons, now)
    unreadable.extend(devices.get("unreadable") or ())

    # Les retenues actives : la lecture d'autorité de #3219 (un hold de pilote
    # lève toute la retenue, celui d'un auteur ne lève que la sienne).
    holds: dict[int, dict] = {}
    closure_allowed = None
    try:
        from . import planned_waits
        # `active_holds` lit `created_ts` (le nom du journal) ; la lecture SQL
        # porte `at_ts` comme le reste du module : on expose les deux.
        for event in events:
            event.setdefault("created_ts", event.get("at_ts"))
        # La reconstruction des retenues de fermeture (#3497) partage la même
        # autorité de levée que `active_holds` (#3219).
        closure_allowed = planned_waits.lift_authority(db, cfg)
        holds = planned_waits.active_holds(db, events, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - repli nommé, jamais un hold inventé
        unreadable.append("holds unreadable: %s" % " ".join(str(exc).split())[:200])

    # CI par job (#3198) : l'API GitHub est bornée aux runs les plus longs,
    # lue par le cache partagé de l'hôte (#848/860). Sans API ou sans run,
    # la durée par workflow reste, et l'absence est dite.
    jobs_rows: list[dict] = []
    ci_unreadable: list[str] = []
    if jobs and cfg is not None and ci_runs:
        reader = api
        if reader is None:
            try:
                from . import github_cli
                reader = lambda path: github_cli.read_path(cfg, path, ttl=3600)
            except Exception as exc:  # noqa: BLE001 - dit plus bas
                reader = None
                ci_unreadable.append("GitHub unavailable: %s"
                                     % " ".join(str(exc).split())[:200])
        if reader is not None:
            candidates = [run for run in ci_runs if run.get("started_ts") is not None]
            candidates = sorted(candidates, key=lambda run: -float(
                (run.get("concluded_ts") or now) - run["started_ts"]))[:MAX_JOB_RUNS]
            for run in candidates:
                path = "/repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100" % (
                    run["repo"], run["run_id"], run.get("run_attempt") or 1)
                try:
                    payload = reader(path) or {}
                except Exception as exc:  # noqa: BLE001 - un run illisible ne bloque pas
                    ci_unreadable.append("%s run %s: %s" % (
                        run["repo"], run["run_id"], " ".join(str(exc).split())[:120]))
                    continue
                for job in payload.get("jobs") or ():
                    started = job.get("started_at")
                    completed = job.get("completed_at")
                    if not started or not completed:
                        continue
                    try:
                        start_dt = datetime.datetime.fromisoformat(
                            str(started).replace("Z", "+00:00"))
                        end_dt = datetime.datetime.fromisoformat(
                            str(completed).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    jobs_rows.append({"repo": run["repo"], "run_id": run["run_id"],
                                      "job": job.get("name") or "?",
                                      "conclusion": job.get("conclusion") or "",
                                      "status": job.get("status") or "",
                                      "duration_s": max(0.0, (end_dt - start_dt).total_seconds())})

    report = build(items=items, milestones=milestones, events=events,
                   agents=agents, turns=turns, merges=merges, ci_runs=ci_runs,
                   jobs=jobs_rows,
                   now=now, hours=hours, wait_s=wait_s, ci_hours=ci_hours,
                   ci_slow_s=ci_slow_s, devices=devices, installed=installed,
                   holds=holds, closure_allowed=closure_allowed,
                   ci_unreadable=ci_unreadable, packets=packets, zone=zone)
    annotate_candidate_causes(db, cfg, report, milestones, unreadable)
    report["capacity"] = read_capacity(db, cfg=cfg, now=now, hours=hours, turns=turns,
                                       canons=canons, unreadable=unreadable)
    report["totals"]["capacity_alerts"] = len(report["capacity"]["alerts"])
    report["unreadable"] = list(dict.fromkeys(list(report["unreadable"]) + unreadable))
    return report


def annotate_candidate_causes(db, cfg, report: Mapping[str, Any],
                              milestones: Iterable[Mapping[str, Any]],
                              unreadable: list[str]) -> None:
    """#4572 : nomme la cause **côté auteur** des attentes `qa_merge`.

    L'escalade SLO lit la même cause (`exploitation._slo_breaches`) avec le
    même moteur que `scripts/paquet-candidats.py` : le débit doit dire la
    même chose, jamais « en attente du paquet » quand le composeur ne peut
    rien. Les codes sont ceux de `packet_candidates.AUTHOR_CAUSES`
    (`freeze-not-head`, `conflict`, …), le geste nomme l'auteur.

    La lecture est locale (refs du dépôt trouvé par le gel, aucun fetch) et
    ne touche que les attentes déjà retenues ; une lecture impossible laisse
    la ligne telle quelle et se consigne. Aucune écriture."""
    from . import packet_candidates, registry, storage

    waits = list(report.get("slow_waits") or ()) \
        + list(report.get("human_waits") or ())
    rows = [row for row in waits if str(row.get("kind")) == "qa_merge"]
    if not rows:
        return
    marks_by_lot = _rows_by_lot(milestones)
    try:
        overview = registry.overview(db)
    except Exception:  # noqa: BLE001 - registre illisible : lecture par lot
        overview = []
    for row in rows:
        lot = int(row.get("lot") or 0)
        try:
            item = storage.of(db).work.get(lot)
        except Exception as exc:  # noqa: BLE001 - ligne illisible : cause inconnue
            unreadable.append("lot %d: candidate cause unreadable: %s" % (lot, exc))
            continue
        if item is None or not str(item.get("branch") or "").strip():
            # Un lot sans branche n'est pas un candidat au paquet : la cause
            # `missing-branch` relève de la prise du lot, pas de la fusion.
            continue
        try:
            cause = packet_candidates.candidate_refusal(
                db, cfg, item, marks_by_lot.get(lot, ()), overview=overview)
        except Exception as exc:  # noqa: BLE001 - une lecture ne fait pas tomber le débit
            unreadable.append("lot %d: candidate cause unreadable: %s" % (lot, exc))
            continue
        if not cause.get("code"):
            continue
        # Le porteur du paquet n'est pas le débiteur : le geste est celui de
        # l'auteur, avec le code et le détail du refus (mêmes mots que la
        # liste calculée).
        author = item.get("assignee") or "l'auteur"
        detail = " (%s)" % cause["detail"] if cause.get("detail") else ""
        row["cause"] = cause
        row["who"] = author
        row["gesture"] = "cause `%s`%s — %s : %s" % (
            cause["code"], detail, author, cause.get("gesture") or "")


def read_capacity(db, *, cfg=None, now: float, hours: int, turns: list,
                  canons=None, unreadable: list) -> dict:
    """#3256 B : lit ce que `capacity` calcule. Chaque lecture en échec est
    nommée dans `unreadable` ; si l'une de celles dont dépend l'alerte manque
    (agents, lots ouverts, démarrables, notes, tenues de budget, historique),
    la mesure est rendue mais aucune alerte n'est levée (`gating_ok` faux)."""
    from . import file_decoupage
    from . import mechanics
    from . import storage
    from . import sous_utilisation
    failed: list[str] = []

    def attempt(what, read_fn, default, *, gating=True):
        try:
            return read_fn()
        except Exception as exc:  # noqa: BLE001 - nommé, jamais deviné
            unreadable.append("capacity: %s unreadable: %s"
                              % (what, " ".join(str(exc).split())[:160]))
            if gating:
                failed.append(what)
            return default

    # Le seuil absent n'est pas une lecture en échec du débit : il est dit
    # dans la section (`rule_error`), qui ne lève alors aucune alerte.
    rule, rule_error, document = None, "", None
    try:
        document = mechanics.configured(cfg) if cfg is not None else None
        rule = document.queue_fill if document is not None else None
        if rule is None:
            rule_error = ("no `file` block in the canon mechanics "
                          "(file.repos_part, file.repos_s)")
    except Exception as exc:  # noqa: BLE001 - canon illisible : dit
        rule_error = "canon mechanics unreadable: %s" % " ".join(str(exc).split())[:160]
    families = tuple(document.families) if document is not None else ()
    excluded_families = (tuple(document.auto_take_excluded_families(now))
                         if document is not None else ())
    store = storage.of(db)
    horizon = now - EVENT_DAYS * 86400
    listing = attempt("agents", store.operations.listing, [])
    lots = attempt("open lots", lambda: store.operations.open_lots_activity(None), [])
    if lots:
        sources = attempt("split lot sources", lambda: {
            int(row["id"]): row["source"] for row in _rows(
                db, _SPLIT_SOURCES_SQL, (",".join(str(int(l["id"])) for l in lots),))}, {})
        lots = [dict(lot, source=sources.get(int(lot["id"]), lot.get("source")))
                for lot in lots]
    # B de cx1 sur ac79c550 : `waiting_project_lots(db=…)` et `_budget_held`
    # absorbent l'échec de leur lecteur (contrat conservateur de leurs autres
    # appelants). Ici, les lecteurs sont appelés en direct : un échec ferme
    # l'alerte au lieu de rendre « tout démarrable » ou « personne tenu ».
    def startable_lots():
        from . import planned_waits
        waiting = sous_utilisation.waiting_project_lots(lots)
        waits = planned_waits.read(db, waiting, now=now) if waiting else {}
        # #4699 : une seule lecture d'empreinte, partagée avec `idle_capacity`
        # (#3185) ; un lot retenu n'est pas démarrable (`work assign` le
        # refuse) et se lit à part, avec ses bloqueurs et leur état (#4561).
        footprint = sous_utilisation.footprint_blockers(db, waiting, lots,
                                                        store=store)
        startable, _blocked = sous_utilisation.split_startable(waiting, waits,
                                                               footprint)
        return startable, footprint

    def budget_holds(rows):
        if cfg is None:
            return {}
        from . import account_turn
        held = {}
        for row in rows:
            reason = account_turn.turn_hold(cfg, db, row["name"], row)
            if reason:
                held[row["name"]] = reason
        return held

    startable, footprint = (attempt("startable lots", startable_lots, ([], {}))
                            if lots else ([], {}))
    # la tenue de budget ne se lit que pour les agents au repos (comme
    # `idle_capacity`) : un agent occupé n'entre pas dans la part au repos
    idle = sous_utilisation.idle_agents(
        [row for row in listing if not row.get("retired_ts")], now, 0.0,
        waiting_free=True, actionable_idle=True)
    held = attempt("budget holds", lambda: budget_holds(idle), {})
    notes = attempt("roadmap notes", lambda: _rows(db, _ROADMAP_SQL, (horizon,)), [])
    losses = attempt("account gauges", lambda: plan_losses(
        sous_utilisation.latest_account_gauges(db, now), now), [], gating=False)
    out = capacity(agents=listing, turns=turns, startable=startable,
                   footprint=footprint, open_lots=lots,
                   roadmap_notes=notes, losses=losses, now=now,
                   window_s=int(hours) * 3600,
                   repos_part=rule.repos_part if rule else None,
                   repos_s=rule.repos_s if rule else None,
                   canons=list(canons or []),
                   configured=bool(canons) or cfg is not None, held=held,
                   families=families, excluded_families=excluded_families,
                   gating_ok=bool(listing) and not failed,
                   rule_source=mechanics.PATH if rule else "")
    if rule_error:
        out["rule_error"] = rule_error
    if failed:
        out["gating_unreadable"] = failed
    return out


# -- texte -------------------------------------------------------------------

def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 3600:
        return "%d min" % round(seconds / 60)
    hours = int(seconds // 3600)
    minutes = round((seconds - hours * 3600) / 60)
    return "%d h %02d" % (hours, minutes)


def _share(value) -> str:
    return "—" if value is None else "%d %%" % round(float(value) * 100)


def _capacity_text(cap: Mapping[str, Any] | None) -> list[str]:
    """#3256 B : la capacité se lit en premier, avec les attentes."""
    if not cap:
        return []
    rule = cap.get("rule")
    mesh = cap.get("mesh") or {}
    lines = ["", "Capacité (%s) — %d/%d agent(s) réveillable(s) au repos (%s), "
             "%.1f heure(s)-agent au repos, %d lot(s) démarrable(s), "
             "%d en attente d'empreinte"
             % ("seuil %s depuis %s" % (_share(rule["repos_part"]),
                                        _duration(rule["repos_s"]))
                if rule else "sans seuil, aucune alerte : %s"
                % (cap.get("rule_error") or "bloc `file` absent du canon"),
                mesh.get("idle", 0), mesh.get("wakeable", 0),
                _share(mesh.get("idle_share")), float(mesh.get("idle_agent_hours") or 0),
                int(mesh.get("startable_lots") or 0),
                int(mesh.get("footprint_lots") or 0))]
    for row in cap.get("projects") or []:
        decoupage = row.get("decoupage")
        parts = ["  %-14s %2d/%-2d au repos (%s) · %5.1f h-agent · %d démarrable(s)"
                 % (row["project"][:14], row["idle"], row["wakeable"],
                    _share(row["idle_share"]), float(row["idle_agent_hours"]),
                    len(row.get("startable_lots") or ()))]
        if row.get("footprint_lots"):
            parts.append("%d en attente d'empreinte" % len(row["footprint_lots"]))
        if decoupage:
            parts.append("découpage #%d (%s)" % (decoupage["lot"],
                                                 _duration(decoupage.get("age_s"))))
        if row.get("roadmap_exhausted"):
            parts.append("feuille de route épuisée")
        lines.append(" · ".join(parts))
    losses = [row for row in cap.get("plan_loss_at_reset") or []]
    if losses:
        lines.append("  Perte au reset : %s" % " · ".join(
            "%s/%s %s" % (row["harness"], row.get("account") or "défaut",
                          "relevé périmé" if row.get("stale") else
                          "%s de %s dans %s" % (_share(row.get("lost")), row.get("window"),
                                                _duration(row.get("resets_in_s")))
                          if row.get("lost") is not None else "rien n'expire")
            for row in losses))
    for alert in cap.get("alerts") or []:
        lines.append("  ⚠ %s : %d/%d au repos (%s) → %s"
                     % (alert["project"], alert["idle"], alert["wakeable"],
                        _share(alert["idle_share"]), alert["gesture"]))
    return lines


def format_text(report: Mapping[str, Any]) -> str:
    """Le tableau court du passage, dans l'ordre du canon."""
    totals = report.get("totals") or {}
    lines = ["Débit %s — %d tour(s), %d fusion(s) sur %d h, %d lot(s) ouvert(s)"
             % (report.get("at_iso"), (report.get("throughput") or {}).get("turns_total", 0),
                (report.get("throughput") or {}).get("merges_total", 0),
                report.get("hours") or 0, totals.get("lots", 0))]
    lines.extend(_capacity_text(report.get("capacity")))
    waits = report.get("slow_waits") or []
    if waits:
        lines.append("")
        lines.append("Attentes de plus de %s hors humain (%d) — le geste fait est dû "
                     "pour chacune :"
                     % (_duration(report.get("wait_s")), len(waits)))
        for row in waits[:40]:
            lines.append("  #%-6d %-16s %8s  %-22s %s"
                         % (int(row.get("lot") or 0), str(row.get("kind")),
                            _duration(row.get("age_s")),
                            str(row.get("who") or "?")[:22],
                            str(row.get("title") or "")[:48]))
            lines.append("         → %s" % row.get("gesture"))
    else:
        lines.append("")
        lines.append("Attentes de plus de %s hors humain : aucune."
                     % _duration(report.get("wait_s")))
    human = report.get("human_waits") or []
    if human:
        lines.append("")
        lines.append("Attentes humaines, hors --check (%d) :" % len(human))
        for row in human[:20]:
            lines.append("  #%-6d %-16s %8s  %-22s %s"
                         % (int(row.get("lot") or 0), str(row.get("kind")),
                            _duration(row.get("age_s")),
                            str(row.get("who") or "?")[:22],
                            str(row.get("title") or "")[:48]))
    else:
        lines.append("Attentes humaines, hors --check : aucune.")
    external = report.get("external_waits") or []
    if external:
        reminders = [row for row in external if row.get("reminder_due")]
        lines.append("")
        lines.append("Attentes externes, hors --check (%d) — exécutant ou tiers "
                     "déclaré, pas un agent à assigner%s :"
                     % (len(external),
                        " ; %d relance(s) due(s)" % len(reminders)
                        if reminders else ""))
        for row in external[:20]:
            lines.append("  #%-6d %-16s %8s  %-22s %s"
                         % (int(row.get("lot") or 0), str(row.get("kind")),
                            _duration(row.get("age_s")),
                            str(row.get("who") or "?")[:22],
                            str(row.get("title") or "")[:48]))
            lines.append("         → %s" % row.get("gesture"))
            if row.get("reminder_due"):
                # #3773 : un tiers extérieur (`externe`) se suit au bureau ;
                # les autres exécutants gardent la règle #3498 (bureau, sinon
                # owner).
                lines.append("         ! relance due le %s — porter au %s (canon §10)"
                             % (row.get("reminder") or "?",
                                "bureau" if row.get("who") in ("bureau", "externe")
                                else "owner"))
    else:
        lines.append("Attentes externes, hors --check : aucune.")
    windows = report.get("window_waits") or []
    if windows:
        lines.append("")
        lines.append("Fenêtres d'observation, hors --check (%d) — attente voulue "
                     "et datée, aucune relance avant l'échéance :" % len(windows))
        for row in windows[:20]:
            lines.append("  #%-6d %-16s %8s  %-22s %s"
                         % (int(row.get("lot") or 0), str(row.get("kind")),
                            _duration(row.get("age_s")),
                            str(row.get("who") or "?")[:22],
                            str(row.get("title") or "")[:48]))
            lines.append("         → échéance %s : %s"
                         % (row.get("due") or "?", row.get("gesture")))
    else:
        lines.append("Fenêtres d'observation, hors --check : aucune.")
    reviews = report.get("reviews_by_reviewer") or []
    lines.append("")
    lines.append("Avis dus par relecteur : %s" % (
        " · ".join("%s %d (max %s)" % (row["reviewer"], row["count"],
                                       _duration(row.get("oldest_age_s")))
                   for row in reviews[:12]) or "aucun"))
    lines.append("Holds : %d · réveils : %d · fenêtres : %d · sur appareil "
                 "indisponible : %d · sur correctif non déployé : %d · relances "
                 "externes : %d"
                 % (totals.get("holds", 0), totals.get("wakes", 0),
                    totals.get("window_waits", 0),
                    totals.get("device_holds", 0), totals.get("undeployed_holds", 0),
                    totals.get("external_reminders", 0)))
    idle = report.get("idle_agents_with_work") or []
    if idle:
        lines.append("Agents au repos avec du travail dû : %s"
                     % " · ".join("%s (%d avis, %d correctif(s), max %s)"
                                  % (row["agent"], len(row["reviews"]),
                                     len(row["fixes"]), _duration(row["oldest_age_s"]))
                                  for row in idle[:10]))
    throughput = report.get("throughput") or {}
    lines.append("Tours/h : %s · Fusions/h : %s" % (
        " ".join(str(row["turns"]) for row in throughput.get("hours") or []),
        " ".join(str(row["merges"]) for row in throughput.get("hours") or [])))
    qa_merge = report.get("qa_merge") or {}
    yesterday = qa_merge.get("yesterday") or {}
    if yesterday.get("count"):
        lines.append("Attente qa_merge de la veille (%s) : médiane %s sur %d lot(s), "
                     "p90 %s" % (yesterday.get("day"), _duration(yesterday.get("median_s")),
                                 yesterday.get("count"), _duration(yesterday.get("p90_s"))))
    else:
        lines.append("Attente qa_merge de la veille (%s) : aucun lot relu mesuré"
                     % (yesterday.get("day") or "?"))
    ci = report.get("ci") or {}
    groups = ci.get("by_workflow") or []
    if groups:
        lines.append("CI (%dh) : %s" % (
            ci.get("window_hours") or 0,
            " · ".join("%s %s n=%d p90 %s max %s" % (
                row["repo"], row["workflow"], row["count"],
                _duration(row.get("p90_s")), _duration(row.get("max_s")))
                for row in groups[:6])))
    if ci.get("by_job"):
        slow = [row for row in ci.get("by_job") or [] if row.get("slow_count")]
        lines.append("CI par job : %s" % (
            " · ".join("%s %s x%d max %s" % (row["job"], "lent", row["slow_count"],
                                             _duration(row.get("max_s")))
                       for row in slow[:6])
            or ("aucun job au-delà de %s" % _duration(ci.get("slow_s")))))
    if report.get("unreadable"):
        lines.append("Lectures illisibles : %s" % "; ".join(report["unreadable"][:6]))
    return "\n".join(lines)


# -- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh debit",
        description="Débit mesuré sur les jalons (canon 24/10/2026) : lots en "
                    "qa/blocked par âge, avis dus, avis bloquants non traités, "
                    "holds et réveils, agents au repos avec du travail dû, tours "
                    "et fusions par heure, durée de CI par job.")
    parser.add_argument("--json", action="store_true", help="rapport complet (ameesh-debit/1)")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help="fenêtre des tours et fusions par heure (défaut %d)" % DEFAULT_HOURS)
    parser.add_argument("--threshold", default="2h", metavar="DURÉE",
                        help="seuil d'une attente (défaut 2h)")
    parser.add_argument("--ci-hours", type=int, default=DEFAULT_CI_HOURS,
                        help="fenêtre des durées de CI (défaut %d)" % DEFAULT_CI_HOURS)
    parser.add_argument("--ci-slow", default="30m", metavar="DURÉE",
                        help="seuil d'un run/job de CI lent (défaut 30m)")
    parser.add_argument("--no-jobs", action="store_true",
                        help="ne pas lire les jobs GitHub (durée par workflow seulement)")
    parser.add_argument("--check", action="store_true",
                        help="sortir 2 si une attente d'agent dépasse le seuil "
                             "(hors attentes humaines, externes et fenêtres "
                             "d'observation non échues, même avec une relance "
                             "due ; aucun « rien à signaler »), ou si un "
                             "projet a file.repos_part de ses agents réveillables "
                             "au repos sans lot de découpage de moins d'une heure "
                             "(#3256)")
    parser.add_argument("--canon", action="append", default=None,
                        help="racine d'un canon à lire pour les appareils (répétable)")
    parser.add_argument("--installed", default=None,
                        help="version installée à comparer (défaut : ameesh --version)")
    return parser


def _duration_arg(text: str) -> float:
    value = str(text or "").strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|min|h)?", value)
    if not match:
        raise DebitError("durée illisible : %r" % text)
    number = float(match.group(1))
    unit = match.group(2) or "s"
    if unit == "s":
        return number
    if unit in ("m", "min"):
        return number * 60
    return number * 3600


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        wait_s = _duration_arg(args.threshold)
        slow_s = _duration_arg(args.ci_slow)
        if args.hours <= 0 or args.ci_hours <= 0:
            raise DebitError("--hours et --ci-hours attendent un entier positif")
        from . import config as config_mod
        from . import db as db_mod
        cfg = config_mod.load()
        canons = None
        if args.canon:
            from . import canon as canon_mod
            canons = [canon_mod.load(root) for root in args.canon]
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db, defer=True)
            report = read(db, cfg=cfg, hours=args.hours, wait_s=wait_s,
                          ci_hours=args.ci_hours, ci_slow_s=slow_s,
                          canons=canons, installed=args.installed,
                          jobs=not args.no_jobs)
        finally:
            db.close()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_text(report))
        if args.check and (report["totals"].get("slow_waits")
                           or report["totals"].get("capacity_alerts")):
            return 2
        return 0
    except DebitError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

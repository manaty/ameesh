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
`hold_undeployed` n'est jamais humain.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import statistics
import sys
import time
from typing import Any, Iterable, Mapping

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
    "wake_overdue": "réveiller l'assigné : le réveil programmé est échu",
}


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


def _reopened(events: list[dict], terminal_ts: float | None) -> bool:
    """Une correction postérieure à la fusion rouvre le lot (journal)."""
    if terminal_ts is None:
        return False
    for event in events or ():
        if float(event.get("at_ts") or 0.0) <= terminal_ts:
            continue
        if str(event.get("state") or "") in ("intake", "build", "qa"):
            return True
    return False


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
    * les verdicts qui nomment la tête gelée : un `blocked` rendu compte
      l'avis (le relecteur a rendu), un verdict d'une autre tête ne compte pas.
    """
    freeze = _latest_freeze(milestones)
    if freeze is None:
        return {"freeze": None, "designees": [], "pending": [], "missing": False,
                "none": False, "unreadable": []}
    freeze_at = float(freeze.get("at_ts") or 0.0)
    freeze_sha = str(freeze.get("sha") or "").strip()
    unreadable: list[str] = []
    latest_mark = None
    none_mark = False
    ever: set[str] = set()
    manual: dict[str, dict] = {}
    for event in sorted(events or (),
                        key=lambda e: (float(e.get("at_ts") or 0.0),
                                       int(e.get("id") or 0))):
        text = str(event.get("note") or "").strip()
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
        pending.append({"name": name, "designated_ts": designated_at,
                        "designated": _iso(designated_at)})
    return {"freeze": freeze, "designees": designees, "pending": pending,
            "missing": (latest_mark is None and not none_mark and not designees
                        and not by_actor),
            "none": none_mark, "unreadable": unreadable}


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

def _holds_from_events(events: list[dict]) -> dict[str, dict]:
    """Repli sans base : la dernière retenue, levée par toute note de levée."""
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
                         "note": text, "pilot": False}
        elif planned_waits.is_lift_note(text):
            held.pop(event.get("actor") or "", None)
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
          ci_unreadable: Iterable[str] = ()) -> dict:
    """Le rapport de débit, calculé sans I/O (témoins et tests)."""
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
    if holds is None:
        holds = {ident: _holds_from_events(values)
                 for ident, values in events_by_lot.items()}
    hold_by_lot = {int(ident): dict(value) for ident, value in (holds or {}).items()}
    held_lots = {ident for ident, entries in hold_by_lot.items() if entries}

    qa: list[dict] = []
    blocked_reviews: list[dict] = []
    reviews_due: list[dict] = []
    holds_out: list[dict] = []
    wakes_out: list[dict] = []
    device_holds: list[dict] = []
    undeployed_holds: list[dict] = []
    unreadable: list[str] = list(devices.get("unreadable") or ())

    for ident, item in by_id.items():
        lot_milestones = milestones_by_lot.get(ident, [])
        lot_events = events_by_lot.get(ident, [])
        freeze = _latest_freeze(lot_milestones)
        terminal_ts = _terminal_at(lot_milestones)
        if terminal_ts is not None and _reopened(lot_events, terminal_ts):
            terminal_ts = None
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
        for hold in hold_by_lot.get(ident, {}).values():
            if terminal_ts is not None and float(hold.get("since") or 0.0) <= terminal_ts:
                continue
            note = str(hold.get("note") or "")
            age = _age(now, hold.get("since"))
            device = match_device(note, devices)
            versions = _versions(note)
            version = max(versions) if versions else None
            undeployed = bool(re.search(r"pas\s+d[ée]ploy[ée]|non\s+d[ée]ploy[ée]",
                                        note, re.I)) \
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
            holds_out.append(entry)
            if device:
                entry["device_unavailable"] = True
                device_holds.append(entry)
            if undeployed:
                entry["held_by_undeployed"] = True
                undeployed_holds.append(entry)
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

    # -- tableau des attentes de plus de 2 h --------------------------------
    waits: list[dict] = []

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
            add_wait("qa_merge", row, row["frozen_ts"], "paquet",
                     GESTURES["qa_merge"], level=2, human=_human_wait(state))
    for row in blocked_reviews:
        add_wait("blocking_review", row, row["blocked_ts"],
                 row.get("assignee") or row.get("reviewer") or "auteur",
                 GESTURES["blocking_review"].format(
                     who=row.get("assignee") or "l'auteur"), level=1,
                 human=_human_wait(state_by_lot.get(int(row["lot"]))))
    for row in holds_out:
        state = state_by_lot.get(int(row["lot"]))
        # canon §11 bis : un correctif fusionné non déployé se traite sans
        # l'owner ; même une note qui cite une règle ou un go de l'owner ne
        # rend pas cette attente humaine (revue dev1 du 24/10, B1).
        human = False if row.get("undeployed") else _human_wait(
            state, row.get("cause"))
        if row.get("device"):
            add_wait("hold_device", row, row["since_ts"], row.get("by") or "?",
                     GESTURES["hold_device"], level=2, device=row["device"],
                     human=human)
        elif row.get("undeployed"):
            add_wait("hold_undeployed", row, row["since_ts"], row.get("by") or "?",
                     GESTURES["hold_undeployed"], level=2,
                     version=row.get("version"), human=human)
        else:
            add_wait("hold", row, row["since_ts"], row.get("by") or "?",
                     GESTURES["hold"].format(who=row.get("by") or "?"), level=2,
                     human=human)
    for row in wakes_out:
        if row.get("overdue"):
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
        "totals": totals,
        "slow_waits": slow_waits,
        "human_waits": human_waits,
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
#: assigné) ; jamais state, reviewer(s), wake_at ni updated_at (canon 24/10).
_ITEMS_SQL = """
    SELECT w.id, w.title, w.app, w.assignee, w.priority
      FROM work_items w
     WHERE (EXISTS (SELECT 1 FROM work_item_milestones m
                     WHERE m.work_item_id = w.id AND m.at > to_timestamp(%s))
         OR EXISTS (SELECT 1 FROM work_item_events e
                     WHERE e.work_item_id = w.id AND e.created_at > to_timestamp(%s)))
       AND NOT EXISTS (
             SELECT 1 FROM work_item_milestones t
              WHERE t.work_item_id = w.id
                AND t.kind IN ('merged', 'closed', 'delivered')
                AND t.at > coalesce(
                      (SELECT max(f.at) FROM work_item_milestones f
                        WHERE f.work_item_id = w.id AND f.kind = 'frozen'),
                      to_timestamp(0)))
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
           agent
      FROM turn_costs
     WHERE recorded_at > to_timestamp(%s)
       AND coalesce(void_reason, '') = ''
     ORDER BY recorded_at
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
    if not ids:
        milestones: list[dict] = []
        events: list[dict] = []
    else:
        # Tableau passé en littéral : le pilote `psql` de secours n'adapte pas
        # une liste Python en tableau PostgreSQL (le pilote psycopg si).
        packed = ",".join(str(int(ident)) for ident in ids)
        milestones = _rows(db, _MILESTONES_SQL, (packed,))
        events = _rows(db, _EVENTS_SQL, (packed, horizon))
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
    try:
        from . import planned_waits
        # `active_holds` lit `created_ts` (le nom du journal) ; la lecture SQL
        # porte `at_ts` comme le reste du module : on expose les deux.
        for event in events:
            event.setdefault("created_ts", event.get("at_ts"))
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
                   holds=holds, ci_unreadable=ci_unreadable)
    report["unreadable"] = list(dict.fromkeys(list(report["unreadable"]) + unreadable))
    return report


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


def format_text(report: Mapping[str, Any]) -> str:
    """Le tableau court du passage, dans l'ordre du canon."""
    totals = report.get("totals") or {}
    lines = ["Débit %s — %d tour(s), %d fusion(s) sur %d h, %d lot(s) ouvert(s)"
             % (report.get("at_iso"), (report.get("throughput") or {}).get("turns_total", 0),
                (report.get("throughput") or {}).get("merges_total", 0),
                report.get("hours") or 0, totals.get("lots", 0))]
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
    reviews = report.get("reviews_by_reviewer") or []
    lines.append("")
    lines.append("Avis dus par relecteur : %s" % (
        " · ".join("%s %d (max %s)" % (row["reviewer"], row["count"],
                                       _duration(row.get("oldest_age_s")))
                   for row in reviews[:12]) or "aucun"))
    lines.append("Holds : %d · réveils : %d · sur appareil indisponible : %d · "
                 "sur correctif non déployé : %d"
                 % (totals.get("holds", 0), totals.get("wakes", 0),
                    totals.get("device_holds", 0), totals.get("undeployed_holds", 0)))
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
                        help="sortir 2 si une attente non humaine dépasse le seuil "
                             "(aucun « rien à signaler »)")
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
        if args.check and report["totals"].get("slow_waits"):
            return 2
        return 0
    except DebitError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

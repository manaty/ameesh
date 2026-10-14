# SPDX-License-Identifier: AGPL-3.0-only
"""#1270 — un 429 de débit passager n'arrête pas l'exécuteur.

Un agent qui prend un 429 de débit transitoire doit attendre puis reprendre
seul, à délais cadencés (1, 2, 5, 10 min), sans être compté comme un échec
rapide (L48 : cinq échecs rapides arrêtent l'agent et exigent `ameesh resume`).
Un **quota épuisé** (plafond de forfait, crédit) est distingué du débit
passager : le même rythme lent s'applique, mais l'état et le texte le nomment.
Dans le doute — texte illisible — on ne conclut rien sur le budget : la garde
comptable (L226b) reste seule juge, et l'agent suit la reprise cadencée.

L'état vit dans `status_text` sous une forme lisible et analysable
(``[rate_limited x2 since 2026-10-14T08:20:00Z, retry in 120s] …``) : il
survit au redémarrage de l'exécuteur, qui le relit à la construction du
worker. Un succès le fait disparaître. L'alerte #1268
`critical_turn_failures` n'est levée qu'après `PERSIST_S` (15 min) d'un
épisode de débit **entier** : un 429 d'une minute ne réveille personne.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

#: motifs d'un quota fournisseur épuisé — plus spécifiques que le débit, donc
#: examinés en premier : « insufficient_quota » ou « usage limit » nomment un
#: plafond, pas une cadence passagère.
QUOTA_PATTERNS = (
    re.compile(r"insufficient[_ -]?quota", re.I),
    re.compile(r"exceeded your current quota", re.I),
    re.compile(r"usage limit", re.I),
    re.compile(r"(?:weekly|monthly|daily) limit", re.I),
    re.compile(r"quota (?:exceeded|exhausted|reached|épuisé|epuise|atteint)", re.I),
    re.compile(r"credit balance is too low", re.I),
    re.compile(r"plafond", re.I),
    re.compile(r"billing", re.I),
)

#: motifs d'un débit passager (429, surcharge, Retry-After…)
TRANSIENT_PATTERNS = (
    re.compile(r"\b429\b"),
    re.compile(r"rate[_ -]?limit", re.I),
    re.compile(r"too many requests", re.I),
    re.compile(r"overloaded", re.I),
    re.compile(r"retry[- ]after", re.I),
    re.compile(r"temporarily unavailable", re.I),
)

#: genres rendus par `classify`
RATE_LIMITED = "rate_limited"
QUOTA = "quota"

#: délais de reprise après un 429 (1, 2, 5, 10 min) ; le dernier est le palier
DELAYS = (60.0, 120.0, 300.0, 600.0)
#: durée d'un épisode de débit avant que l'alerte #1268 puisse se lever
PERSIST_S = 900.0
#: texte de résultat lu par `classify` seulement s'il est court (comme L226b)
MAX_RESULT_CHARS = 4000

_STATE_RE = re.compile(
    r"\[(rate_limited|quota) x(\d+) since (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z),"
    r" retry in (\d+)s\]")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _texts(error=None, result=None, stderr_tail=None):
    out = [error or "", stderr_tail or ""]
    if result and len(result) <= MAX_RESULT_CHARS:
        out.append(result)
    return out


def classify(error: str | None = None, result: str | None = None,
             stderr_tail: str | None = None) -> str | None:
    """Le genre de panne fournisseur d'un tour en échec, None si aucun.

    `rate_limited` : débit passager (429, surcharge) — attente et reprise.
    `quota` : plafond/forfait/crédit épuisé — distinct, jamais déduit d'un
    simple 429 (le doute reste un débit passager ; la garde comptable L226b
    reste seule juge du budget).
    """
    for text in _texts(error, result, stderr_tail):
        if not text:
            continue
        if any(motif.search(text) for motif in QUOTA_PATTERNS):
            return QUOTA
        if any(motif.search(text) for motif in TRANSIENT_PATTERNS):
            return RATE_LIMITED
    return None


def delay(streak: int) -> float:
    """Le délai de reprise après `streak` échecs de débit consécutifs."""
    n = max(1, int(streak or 1))
    return DELAYS[min(n, len(DELAYS)) - 1]


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime(_STAMP)


def _parse_stamp(texte: str) -> float:
    return datetime.strptime(texte, _STAMP).replace(tzinfo=timezone.utc).timestamp()


def status_text(base: str, kind: str, streak: int, since: float, wait: float) -> str:
    """`status_text` d'un échec de débit : l'état complet, relisible."""
    return "[%s x%d since %s, retry in %ds] %s" % (
        kind, max(1, int(streak)), _stamp(since), int(wait), base)


def state(status_text: str | None) -> dict | None:
    """L'épisode de débit marqué dans `status_text`, None s'il n'y en a pas.

    Le marqueur peut être précédé du marqueur d'échecs #1268 ; on le cherche
    donc n'importe où dans le texte.
    """
    m = _STATE_RE.search(str(status_text or ""))
    if not m:
        return None
    try:
        since = _parse_stamp(m.group(3))
    except ValueError:
        return None
    return {"kind": m.group(1), "streak": int(m.group(2)), "since": since,
            "wait": float(m.group(4))}


def alert_suppressed(etat: dict | None, streak: int, now: float | None = None) -> bool:
    """L'alerte #1268 doit-elle taire cet épisode de débit ?

    Oui tant que l'épisode couvre **toute** la série d'échecs et dure depuis
    moins de `PERSIST_S` : un 429 passager ne réveille personne. Des causes
    mêlées (l'épisode ne couvre pas la série) laissent l'alerte se lever.
    """
    if not etat or etat["streak"] < streak:
        return False
    maintenant = time.time() if now is None else float(now)
    return maintenant - etat["since"] < PERSIST_S

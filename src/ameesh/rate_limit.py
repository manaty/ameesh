# SPDX-License-Identifier: AGPL-3.0-only
"""#1270 — un 429 de débit passager n'arrête pas l'exécuteur.

Un agent qui prend un 429 de débit transitoire doit attendre puis reprendre
seul, à délais cadencés (1, 2, 5, 10 min), sans être compté comme un échec
rapide (L48 : cinq échecs rapides arrêtent l'agent et exigent `ameesh resume`).
Les autres pannes passagères du fournisseur suivent la même reprise : 5xx
explicite (500, 502, 503, 504, 529), erreurs passagères de DeepSeek (`dsh:
MALFORMED_RESPONSE: …`, `SERVER`, `RATE_LIMIT`, flux coupé ; bureau 15/10) et
flux Codex coupé (`stream disconnected before completion`).
Un **quota épuisé** (plafond de forfait, crédit) est distingué du débit
passager : le même rythme lent s'applique, l'état et le texte le nomment, mais
L48 reste armé — cinq refus rapides arrêtent l'agent : un plafond n'est pas
repris sans fin.
Dans le doute — texte illisible — on ne conclut rien sur le budget : la garde
comptable (L226b) reste seule juge, et l'agent suit la reprise cadencée. Un
débit ne se déduit que d'un texte du fournisseur (`error`, fin de `stderr`) :
le dernier message du modèle ne conclut rien.

L'état vit dans `status_text` sous une forme lisible et analysable
(``[rate_limited x2 since 2026-10-14T08:20:00Z, retry in 120s, retry at …]``) :
il survit au redémarrage de l'exécuteur, qui le relit à la construction du
worker — y compris l'**échéance absolue** de reprise, qu'aucun courrier, reset
ou budget ne court-circuite. Un succès le fait disparaître. L'alerte #1268
`critical_turn_failures` n'est levée qu'après `PERSIST_S` (15 min) d'un
épisode de débit **entier** : un 429 d'une minute ne réveille personne.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

#: motifs d'un quota fournisseur épuisé — plus spécifiques que le débit, donc
#: examinés en premier : « insufficient_quota » ou « usage limit » nomment un
#: plafond, pas une cadence passagère. Ce sont des **signatures fournisseur
#: positives** : le seul mot « billing » (ou « plafond ») d'un texte libre ne
#: classe jamais un quota (R1 cx3 B2 : « I updated the billing page » ne doit
#: pas désarmer L48).
QUOTA_PATTERNS = (
    re.compile(r"insufficient[_ -]?quota", re.I),
    re.compile(r"exceeded your current quota", re.I),
    re.compile(r"usage limit", re.I),
    re.compile(r"(?:weekly|monthly|daily) limit", re.I),
    re.compile(r"quota (?:exceeded|exhausted|reached|épuisé|epuise|atteint)", re.I),
    re.compile(r"credit balance is too low", re.I),
    re.compile(r"insufficient (?:credits?|funds?)\b", re.I),
    re.compile(r"(?:out of|no) credits?\b", re.I),
    re.compile(r"billing (?:hard )?limit (?:reached|exceeded)", re.I),
    re.compile(r"(?:spend|spending) limit (?:reached|exceeded)", re.I),
    re.compile(r"plafond (?:de |du )?(?:forfait|cr[eé]dit)", re.I),
    # DeepSeek (dsh) : ligne d'erreur `dsh: QUOTA: …` (402, solde épuisé)
    re.compile(r"\bdsh: QUOTA\b"),
)

#: motifs d'un débit passager (429, surcharge, Retry-After…) — signatures
#: fournisseur **positives**, comme les quotas : un numéro de ligne « 429 »
#: (trace Node `cli.js:429:15`, `File "request.py", line 429`), le sujet
#: « rate limit » ou l'adjectif « overloaded » d'un texte libre ne désarment
#: pas L48 et ne taisent pas l'alerte #1268 (P2 dev1 et cx3 du 14/10 : un tour
#: réellement en échec repartait sinon toutes les 10 min sans fin).
#: Ces motifs ne lisent que les textes du **fournisseur** — `error` et
#: `stderr_tail` —, jamais le résultat libre du tour (dernier message du
#: modèle), qui peut *parler* d'un débit sans l'avoir subi.
TRANSIENT_PATTERNS = (
    # un « 429 » (ou un 5xx passager : 500, 502, 503, 504, 529) doit nommer un
    # statut HTTP explicite : en-tête (`HTTP 429`, `HTTP/1.1 503`), statut
    # (`status: 429`, `last status: 503 Service Unavailable`, `status_code=429`)
    # ou code d'erreur fournisseur (`Error code: 429`, `API Error: 500`) —
    # séparateurs et espaces, jamais de texte arbitraire entre nom et numéro
    re.compile(r"\b(?:https?(?:/\d(?:\.\d)?)?|status(?:[_ ]?code)?|error code|api error)"
               r"\b\s*[:=]?\s*(?:429|5(?:0[0234]|29))\b", re.I),
    # ou un 5xx suivi immédiatement de la panne qu'il nomme (`502 Bad Gateway`)
    re.compile(r"\b5(?:0[0234]|29)\b\s*[:=-]?\s*(?:internal server error|bad gateway"
               r"|service unavailable|gateway time-?out|overloaded)\b", re.I),
    # ou « 429 » suivi immédiatement de la panne qu'il nomme (`429 Too Many
    # Requests`, `429: rate limit reached`, `429 Client Error: Too Many
    # Requests`) — toujours pas de texte arbitraire entre les deux
    re.compile(r"\b429\b\s*(?:client error)?\s*[:=-]?\s*"
               r"(?:too many requests|rate[_ -]?limit(?:ed|s|ing)?[ _-]?"
               r"(?:error|exceeded|reached|exception))", re.I),
    # un statut « 429 » seul (texte entier) reste une signature
    re.compile(r"^\s*(?:http[\s/]*)?429\s*$", re.I),
    # « rate limit » doit nommer la panne (error/exceeded/reached), pas le sujet
    re.compile(r"rate[_ -]?limit(?:ed|s|ing)?[ _-]?"
               r"(?:error|exceeded|reached|exception)", re.I),
    re.compile(r"\btoo many requests\b", re.I),
    # « overloaded » : forme fournisseur, ou invite explicite à réessayer
    re.compile(r"overloaded[_ -]?error", re.I),
    re.compile(r"\boverloaded\b[^\n]{0,40}\b(?:try again|retry)", re.I),
    re.compile(r"\bretry[- ]after\b", re.I),
    re.compile(r"\btemporarily unavailable\b", re.I),
    # #1270 x DeepSeek (complément bureau du 15/10 03:06) : les pannes
    # passagères du fournisseur DeepSeek suivent la même reprise cadencée que
    # le 429 — flux SSE malformé, service surchargé, passerelle 5xx. Formes
    # réelles relevées dans les `stderr.log` des exécuteurs :
    # ``dsh: MALFORMED_RESPONSE: DeepSeek Messages SSE event type mismatch``
    # et ``Service is too busy. We advise users to temporarily switch…``.
    # Signatures fournisseur positives uniquement (lues dans `error` et
    # `stderr_tail`), jamais un mot libre du modèle. Le code DeepSeek est
    # ancré (`dsh: <CODE>:`) : un simple mot « MALFORMED_RESPONSE » dans un
    # texte ne classe pas (même exigence que le 429).
    re.compile(r"DeepSeek Messages SSE event type mismatch", re.I),
    re.compile(r"\bSSE\b[^\n]{0,40}\b(?:error|mismatch|failed|closed|reset)\b", re.I),
    re.compile(r"\bservice is too busy\b", re.I),
    # une passerelle nommée sans numéro reste une signature fournisseur
    re.compile(r"\b(?:internal server error|bad gateway|service unavailable|"
               r"gateway time-?out)\b", re.I),
    # DeepSeek (dsh) : ligne d'erreur `dsh: <CODE>: …` d'une panne passagère —
    # débit, serveur, flux SSE malformé, coupé ou vide, attente, transport
    # (bureau 15/10 : « dsh: MALFORMED_RESPONSE: DeepSeek Messages SSE event
    # type mismatch » laissait trois agents bloqués)
    re.compile(r"\bdsh: (?:RATE_LIMIT|SERVER|MALFORMED_RESPONSE|STREAM_CLOSED"
               r"|EMPTY_RESPONSE|TIMEOUT|TRANSPORT)\b"),
    # Codex : flux coupé au-delà de ses propres reprises, pendant du
    # `STREAM_CLOSED` de DeepSeek (`stream disconnected before completion: …`)
    re.compile(r"\bstream disconnected before completion\b", re.I),
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
    r" retry in (\d+)s(?:, retry at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z))?\]")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _texts(error=None, result=None, stderr_tail=None):
    """Textes lus pour un **quota**, résultat libre compris."""
    out = [error or "", stderr_tail or ""]
    if result and len(result) <= MAX_RESULT_CHARS:
        out.append(result)
    return out


def _directs(error=None, stderr_tail=None):
    """Textes écrits par le **fournisseur** : erreur et fin de stderr.

    Le résultat libre du tour (dernier message du modèle) n'en fait pas
    partie : un modèle qui *parle* d'un débit ne le subit pas et ne doit ni
    désarmer L48 ni taire l'alerte #1268 (P2 dev1 du 14/10).
    """
    return [t for t in (error or "", stderr_tail or "") if t]


def classify(error: str | None = None, result: str | None = None,
             stderr_tail: str | None = None) -> str | None:
    """Le genre de panne fournisseur d'un tour en échec, None si aucun.

    `rate_limited` : panne passagère — débit (429), surcharge, 5xx explicite,
    erreur passagère DeepSeek (`dsh: MALFORMED_RESPONSE`…) — attente et reprise.
    `quota` : plafond/forfait/crédit épuisé — distinct, jamais déduit d'un
    simple 429 (le doute reste un débit passager ; la garde comptable L226b
    reste seule juge du budget) ; L48 reste armé pour lui.

    Le quota prime sur le débit **tous textes confondus** : un « 429 » sur
    l'erreur et un `insufficient_quota` sur stderr classent quota (R1 cx3 B2).
    Un débit, lui, ne se déduit que d'un texte du fournisseur (`error`,
    `stderr_tail`) : le résultat libre du tour n'est jamais lu pour lui.
    Un genre ne se déduit que d'une signature fournisseur positive.
    """
    textes = [t for t in _texts(error, result, stderr_tail) if t]
    if any(motif.search(texte) for texte in textes for motif in QUOTA_PATTERNS):
        return QUOTA
    directs = _directs(error, stderr_tail)
    if any(motif.search(texte) for texte in directs for motif in TRANSIENT_PATTERNS):
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


def status_text(base: str, kind: str, streak: int, since: float, wait: float,
                retry_at: float | None = None) -> str:
    """`status_text` d'un échec de débit : l'état complet, relisible.

    `since` est l'heure du **premier** échec de l'épisode (elle ne bouge plus
    tant qu'un succès ne l'efface pas) ; `retry_at` est l'échéance absolue de
    reprise — durable, elle est relue telle quelle après un redémarrage, donc
    une tentative interrompue par un courrier, un reset ou le budget ne
    court-circuite pas l'attente (#1270 B1). Sans `retry_at`, l'échéance est
    calculée du délai nominal (`wait`) au moment de l'écriture.
    """
    if retry_at is None:
        retry_at = time.time() + float(wait)
    return "[%s x%d since %s, retry in %ds, retry at %s] %s" % (
        kind, max(1, int(streak)), _stamp(since), int(wait), _stamp(retry_at), base)


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
    retry_at = 0.0
    if m.group(5):
        try:
            retry_at = _parse_stamp(m.group(5))
        except ValueError:
            retry_at = 0.0
    return {"kind": m.group(1), "streak": int(m.group(2)), "since": since,
            "wait": float(m.group(4)), "retry_at": retry_at}


def remaining(etat: dict | None, now: float | None = None) -> float:
    """Secondes restantes avant la reprise cadencée d'un épisode, 0 sans épisode.

    L'échéance durable (`retry at`) prime ; un marqueur d'avant ce champ
    retombe sur le délai nominal. C'est la seule horloge du lancement : une
    échéance passée vaut 0, jamais un délai recalculé qui repousserait la
    reprise.
    """
    if not etat:
        return 0.0
    maintenant = time.time() if now is None else float(now)
    if etat.get("retry_at"):
        return max(0.0, float(etat["retry_at"]) - maintenant)
    return float(etat.get("wait") or 0.0)


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

# SPDX-License-Identifier: AGPL-3.0-only
"""#4164 — crédits du compte fournisseur : épuisement, garde, alerte, reprise.

Incident OpenAI IMA du 28/10 (~05:28Z, manaty/ameesh#3181) : l'organisation
n'avait plus de crédits prépayés (`429 insufficient_quota`), l'enveloppe
ameesh affichait 27,6 USD sur 500, et ameesh a continué d'accorder des tours
qui échouaient tous, sans alerter personne. Une enveloppe de dépense ne voit
pas l'état réel du compte fournisseur ; ce module le tient.

* **État** par compte logique (`harness`, `account`, '' = défaut du
  harnais) : `ok | low | exhausted`, heure du dernier contrôle (table
  `provider_account_credits`, migration 0500).
* **Détection** : un tour en échec dont le texte du **fournisseur** (erreur,
  fin de stderr — jamais le message du modèle, #1397) porte une signature
  d'épuisement de crédit, propre à chaque fournisseur, passe le compte à
  `exhausted`. Un plafond de forfait (« usage limit », limite hebdomadaire)
  n'en est pas une : il reste au `quota` de `rate_limit`, et un modèle « at
  capacity » reste passager. Un relevé de solde (DeepSeek, `balance`) à zéro
  aussi ; un solde positif le rend `ok`.
* **Garde** : un compte `exhausted` ne reçoit aucun nouveau tour. La consigne
  de l'agent n'est pas consommée (elle reste demandée, dans son ordre) ;
  l'agent est `blocked` avec le statut `provider_credits_exhausted: h/a`,
  distinct d'une panne : pas un échec rapide (L48), aucun arrêt.
* **Sonde bornée** : tant que le compte est épuisé, un seul tour par
  `PROBE_S` (10 min) est laissé partir, réservé atomiquement entre runners.
  Il échoue au premier appel s'il n'y a toujours pas de crédit (coût nul) ;
  s'il aboutit, le compte repasse `ok` et les agents retenus reprennent seuls.
* **Alerte** : `provider_credits_exhausted`, une par transition (clé : début
  de l'état), poussée à l'owner ; sa résolution est l'avis de reprise.

L'état `low` (montant prépayé déclaré, solde projeté, seuils 20 %/5 % ou
< 24 h) relève d'un lot suivant ; la colonne l'accepte déjà.
"""
from __future__ import annotations

import re
import time

from . import db as db_mod
from . import storage

OK = "ok"
LOW = "low"
EXHAUSTED = "exhausted"

#: préfixe du statut d'un agent retenu ; aussi la raison lisible du tour
#: demandé (IMA la lit pour distinguer l'épuisement d'une panne)
REASON = "provider_credits_exhausted"
STATUS_PREFIX = REASON + ":"
#: intervalle de la sonde bornée d'un compte épuisé
PROBE_S = 600.0
#: borne du texte d'erreur fournisseur gardé en base
DETAIL_MAX = 500

#: signatures communes à plusieurs fournisseurs (texte du fournisseur seul)
_COMMON = (
    re.compile(r"insufficient[_ -]?(?:credits?|funds?|balance)\b", re.I),
    re.compile(r"\b(?:no|zero) credits? (?:remaining|left)\b", re.I),
    re.compile(r"\bout of credits?\b", re.I),
    re.compile(r"credit[_ ]balance[_ ](?:is[_ ])?(?:exhausted|too[_ ]low|depleted)", re.I),
)

#: signatures propres à chaque fournisseur, par harnais
PATTERNS = {
    # OpenAI (Codex, clés API) : `insufficient_quota` est l'épuisement du
    # crédit prépayé ou du plafond de facturation de l'organisation, pas un
    # débit (« You exceeded your current quota, please check your plan and
    # billing details ») ; `credit_balance_exhausted` en est la forme récente
    "codex": (
        re.compile(r"insufficient[_ -]?quota", re.I),
        re.compile(r"exceeded your current quota", re.I),
        re.compile(r"billing[_ ](?:hard[_ ])?limit[_ ](?:reached|exceeded)", re.I),
    ),
    # Anthropic API : « Your credit balance is too low to access the
    # Anthropic API » (forme couverte par `_COMMON`) ; un « usage limit » du
    # forfait Claude n'est PAS un épuisement de crédit
    "claude": (),
    # DeepSeek (dsh) : 402 « Insufficient Balance », ligne `dsh: QUOTA:`
    "deepseek": (
        re.compile(r"\bdsh: QUOTA\b"),
        re.compile(r"\b402\b[^\n]{0,40}insufficient", re.I),
    ),
}


def classify(harness: str | None, error: str | None = None,
             stderr_tail: str | None = None) -> str | None:
    """La signature d'épuisement de crédit d'un tour en échec, None sinon.

    Ne lit que les textes du fournisseur (`error`, fin de `stderr`). Rend le
    passage reconnu (borné), pour la note et l'état ; un harnais inconnu n'a
    que les signatures communes."""
    textes = [t for t in (error or "", stderr_tail or "") if t]
    motifs = _COMMON + PATTERNS.get(harness or "", ())
    for texte in textes:
        for motif in motifs:
            trouve = motif.search(texte)
            if trouve:
                return trouve.group(0)[:80]
    return None


def detail_of(error: str | None, stderr_tail: str | None) -> str:
    """Le texte d'erreur fournisseur borné gardé en base (jamais d'invite)."""
    texte = "\n".join(t for t in (str(error or ""), stderr_tail or "") if t)
    return texte[-DETAIL_MAX:]


def who(harness: str, account: str | None) -> str:
    return "%s/%s" % (harness, account or "(default)")


def status_text(harness: str, account: str | None) -> str:
    return "%s %s" % (STATUS_PREFIX, who(harness, account))


def is_held(text: str | None) -> bool:
    return (text or "").startswith(STATUS_PREFIX)


def get(db, harness: str, account: str | None) -> dict | None:
    """La ligne du compte, None si jamais vue ou base sans migration 0500."""
    try:
        return storage.of(db).provider_credits.get(harness, account or "")
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return None


def listing(db) -> list[dict]:
    try:
        return storage.of(db).provider_credits.listing()
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return []


def exhausted(db, harness: str, account: str | None) -> dict | None:
    row = get(db, harness, account)
    return row if row and row.get("state") == EXHAUSTED else None


def mark_exhausted(db, harness: str, account: str | None, *, reason: str,
                   detail: str | None = None, source: str = "turn",
                   agent: str | None = None, host: str | None = None,
                   probe_after_s: float = PROBE_S) -> bool:
    """Pose `exhausted` ; vrai sur la transition (une seule par épisode)."""
    return storage.of(db).provider_credits.mark_exhausted(
        harness, account or "", reason=reason, detail=detail, source=source,
        agent=agent, host=host, probe_after_s=probe_after_s)


def mark_ok(db, harness: str, account: str | None, *, source: str,
            agent: str | None = None, host: str | None = None) -> bool:
    """Pose `ok` ; vrai sur la transition depuis `low`/`exhausted`."""
    return storage.of(db).provider_credits.mark_ok(
        harness, account or "", source=source, agent=agent, host=host)


def claim_probe(db, harness: str, account: str | None,
                interval_s: float = PROBE_S) -> bool:
    """Vrai pour le seul appelant qui obtient la sonde échue du compte."""
    return storage.of(db).provider_credits.claim_probe(
        harness, account or "", interval_s)


def from_balance(db, harness: str, account: str | None, total: float, currency: str,
                 *, host: str | None = None) -> bool | None:
    """Un relevé de solde (`balance.record`) : nul ou négatif → `exhausted`,
    positif → `ok` si le compte était épuisé. Rend la transition éventuelle
    (True : épuisé, False : repris, None : rien)."""
    if float(total) <= 0.0:
        return True if mark_exhausted(
            db, harness, account, reason="balance %.2f %s" % (float(total), currency),
            source="balance", host=host) else None
    row = get(db, harness, account)
    if row and row.get("state") != OK:
        return False if mark_ok(db, harness, account, source="balance", host=host) else None
    return None


def _local(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def describe(row: dict | None) -> str:
    """Une ligne lisible de l'état d'un compte (`budget`, `show`, `accounts`)."""
    if not row:
        return "ok (never exhausted)"
    state = row.get("state") or OK
    parts = ["%s since %s" % (state, _local(row["since_ts"]))]
    if row.get("checked_ts"):
        parts.append("checked %s" % _local(row["checked_ts"]))
    if state == EXHAUSTED:
        if row.get("reason"):
            parts.append("reason %s" % row["reason"])
        if row.get("next_probe_ts"):
            parts.append("next probe %s" % _local(row["next_probe_ts"]))
    return ", ".join(parts)


def alerts(db, listing_rows: list, now: float) -> list[dict]:
    """`provider_credits_exhausted` : une alerte par compte épuisé, clé sur le
    début de l'état (une par transition), poussée à l'owner. Sa résolution,
    quand le compte repasse `ok`, est l'avis de reprise."""
    from .exploitation import _alert
    out = []
    for row in listing(db):
        if row.get("state") != EXHAUSTED:
            continue
        nom = who(row["harness"], row.get("account"))
        held = sorted(r["name"] for r in listing_rows
                      if (r.get("status_text") or "") == status_text(
                          row["harness"], row.get("account")))
        detail = ("provider account %s has no credits left (%s), since %s: no new "
                  "turn is granted to its agents, their instructions stay requested "
                  "(reason %s); one probe turn every %d min, next %s; top up the "
                  "account, the agents resume on their own"
                  % (nom, row.get("reason") or "?", _local(row["since_ts"]), REASON,
                     int(PROBE_S // 60),
                     _local(row["next_probe_ts"]) if row.get("next_probe_ts") else "now"))
        if held:
            detail += "; held: %s" % ", ".join(held)
        out.append(_alert(
            "provider_credits_exhausted", None, row["since_ts"], len(held), 0, detail,
            provider=row["harness"], harness=row["harness"],
            account=row.get("account") or "", currency=None, reason=REASON,
            held=held, checked_ts=row.get("checked_ts"),
            next_probe_ts=row.get("next_probe_ts"), targets=["owner"], urgent=True))
    return out

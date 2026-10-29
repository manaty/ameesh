# SPDX-License-Identifier: AGPL-3.0-only
"""#4164 — crédits du compte fournisseur : épuisement, garde, alerte, reprise.

Incident fournisseur OpenAI du 28/10 (~05:28Z, manaty/ameesh#3181) : l'organisation
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

#4166 — l'état `low`, avant l'épuisement :

* **Montant de référence.** Pour un compte sans API de solde (OpenAI/Codex),
  un humain déclare le prépayé disponible (`ameesh budget prepaid set`) ;
  le solde est **projeté** : prépayé − dépense réglée (`turn_costs`) du
  compte depuis la déclaration. Pour un fournisseur qui publie un solde
  (DeepSeek), le solde **réel** est lu ; le prépayé déclaré (sinon le dernier
  sommet de la série) sert de référence au pourcentage.
* **Seuils** : `low` à 20 % de la référence (5 % : niveau critique, alerte
  urgente) ou quand l'épuisement est projeté à moins de 24 h au rythme de
  dépense mesuré. Une seule alerte `provider_credits_low` par épisode (clé :
  `since`) ; sa résolution est l'avis de reprise (retour à `ok`).
* **Repli** : `accounts.choose` écarte un compte `exhausted` (comme un compte
  en échec d'authentification) : un agent dont le compte est épuisé bascule
  sur un autre compte déclaré du harnais, sans geste ; s'il n'en reste aucun,
  il est retenu (sonde bornée) comme en #4164.
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
#: demandé (le demandeur le lit pour distinguer l'épuisement d'une panne)
REASON = "provider_credits_exhausted"
STATUS_PREFIX = REASON + ":"
#: #4166 : raison stable de l'alerte `low` (la clé d'alerte ne doit pas
#: changer à chaque relevé ; les chiffres sont dans `detail`)
REASON_LOW = "provider_credits_low"
#: intervalle de la sonde bornée d'un compte épuisé
PROBE_S = 600.0
#: borne du texte d'erreur fournisseur gardé en base
DETAIL_MAX = 500

#: #4166 : seuils de `low` sur le solde rapporté au montant de référence
LOW_RATIO = 0.20
#: #4166 : seuil critique (alerte urgente) — le second palier demandé
CRITICAL_RATIO = 0.05
#: #4166 : épuisement projeté à moins de 24 h — `low` quel que soit le ratio
RUNWAY_LOW_S = 24 * 3600.0
#: #4166 : fenêtre du rythme de dépense (réglée) pour projeter l'épuisement
RATE_WINDOW_S = 6 * 3600.0
#: #4166 : en deçà, la fenêtre est trop courte pour un rythme (même borne que
#: `sous_utilisation.BALANCE_MIN_SPAN_S`)
RATE_MIN_SPAN_S = 1800.0
#: #4166 : profondeur de l'historique de soldes lue pour la référence
PEAK_WINDOW_S = 30 * 86400.0

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


def exhausted_names(db, harness: str) -> set[str]:
    """#4166 : les comptes logiques de ce harnais en `exhausted`, pour le
    placement (`accounts.choose` les écarte comme un échec d'authentification).
    Lecture seule, ouverte sur une base sans migration (comme `get`)."""
    try:
        rows = listing(db)
    except db_mod.Unavailable:
        raise
    return {(r.get("account") or "") for r in rows
            if r.get("harness") == harness and r.get("state") == EXHAUSTED
            and (r.get("account") or "")}


def mark_exhausted(db, harness: str, account: str | None, *, reason: str,
                   detail: str | None = None, source: str = "turn",
                   agent: str | None = None, host: str | None = None,
                   probe_after_s: float = PROBE_S,
                   balance: float | None = None, ratio: float | None = None,
                   runway_ts: float | None = None) -> bool:
    """Pose `exhausted` ; vrai sur la transition (une seule par épisode)."""
    return storage.of(db).provider_credits.mark_exhausted(
        harness, account or "", reason=reason, detail=detail, source=source,
        agent=agent, host=host, probe_after_s=probe_after_s,
        balance_usd=balance, ratio=ratio, runway_ts=runway_ts)


def mark_low(db, harness: str, account: str | None, *, reason: str,
             detail: str | None = None, source: str = "assess",
             agent: str | None = None, host: str | None = None,
             balance: float | None = None, ratio: float | None = None,
             runway_ts: float | None = None) -> bool:
    """Pose `low` ; vrai sur la transition (depuis `ok` ou `exhausted`)."""
    return storage.of(db).provider_credits.mark_low(
        harness, account or "", reason=reason, detail=detail, source=source,
        agent=agent, host=host, balance_usd=balance, ratio=ratio,
        runway_ts=runway_ts)


def mark_ok(db, harness: str, account: str | None, *, source: str,
            agent: str | None = None, host: str | None = None,
            balance: float | None = None, ratio: float | None = None,
            runway_ts: float | None = None) -> bool:
    """Pose `ok` ; vrai sur la transition depuis `low`/`exhausted`."""
    return storage.of(db).provider_credits.mark_ok(
        harness, account or "", source=source, agent=agent, host=host,
        balance_usd=balance, ratio=ratio, runway_ts=runway_ts)


def claim_probe(db, harness: str, account: str | None,
                interval_s: float = PROBE_S) -> bool:
    """Vrai pour le seul appelant qui obtient la sonde échue du compte."""
    return storage.of(db).provider_credits.claim_probe(
        harness, account or "", interval_s)


# --------------------------------------------------------------------------
# #4166 : le prépayé déclaré, la projection et les seuils
# --------------------------------------------------------------------------

def prepaid_of(row: dict | None) -> float | None:
    """Le montant prépayé déclaré d'une ligne, None s'il n'y en a pas."""
    if not row or row.get("prepaid_usd") is None:
        return None
    try:
        return float(row["prepaid_usd"])
    except (TypeError, ValueError):
        return None


def declare(db, harness: str, account: str | None, usd: float, *, actor: str
            ) -> tuple[bool, float | None]:
    """Déclare (ou recharge) le prépayé d'un compte ; un humain seulement,
    contrôlé par l'appelant (`budget.authorize`). Rend `(changé, ancien)`."""
    return storage.of(db).provider_credits.declare_prepaid(
        harness, account or "", usd=float(usd), actor=actor)


def clear(db, harness: str, account: str | None, *, actor: str
          ) -> tuple[bool, float | None]:
    """Retire la déclaration prépayée. Rend `(changé, ancien)`."""
    return storage.of(db).provider_credits.clear_prepaid(
        harness, account or "", actor=actor)


def _settled_spend(db, harness: str, account: str | None, from_ts: float) -> float:
    """La dépense **réglée** du compte (grand livre) depuis `from_ts`.

    `turn_costs` ne compte ni les tours annulés (`void_reason`) ni les
    déclarations d'appareil médié (`source = 'device'`, déjà comptées au
    relais), comme les autres sommes de dépense."""
    rows = storage.of(db).turn_costs.paid_lines(float(from_ts), harnesses=[harness])
    cible = account or ""
    return sum(float(r["usd"]) for r in rows
               if (r.get("account") or "") == cible)


def _rate_per_hour(db, harness: str, account: str | None, start_ts: float,
                   now: float) -> float | None:
    """Dépense réglée par heure sur la fenêtre récente (None si trop courte)."""
    start = max(float(start_ts), float(now) - RATE_WINDOW_S)
    span = float(now) - start
    if span < RATE_MIN_SPAN_S:
        return None
    return _settled_spend(db, harness, account, start) / span * 3600.0


def _peak(points: list, declared: float | None) -> float | None:
    """Le montant de référence d'une série de soldes : le prépayé déclaré,
    sinon le sommet du dernier segment (depuis la dernière hausse, c.-à-d. la
    dernière recharge). Une série qui ne fait que baisser garde son sommet."""
    if declared is not None and declared > 0:
        return declared
    totals = [float(p["total"]) for p in points]
    if not totals:
        return None
    debut = 0
    for index in range(1, len(totals)):
        if totals[index] > totals[index - 1]:
            debut = index
    sommet = max(totals[debut:])
    return sommet if sommet > 0 else None


def _runway(now: float, balance: float, rate: float | None) -> float | None:
    if balance <= 0:
        return float(now)
    if not rate or rate <= 0:
        return None
    return float(now) + float(balance) / float(rate) * 3600.0


def _verdict(balance: float, reference: float | None, rate: float | None,
             now: float) -> tuple[str, str]:
    """`(état, détail)` d'un solde face à sa référence et à son rythme."""
    if balance <= 0:
        return EXHAUSTED, "no balance left"
    ratio = (float(balance) / float(reference)
             if reference is not None and reference > 0 else None)
    runway = _runway(now, balance, rate)
    low = ((ratio is not None and ratio <= LOW_RATIO)
           or (runway is not None and runway - now < RUNWAY_LOW_S))
    if not low:
        return OK, ""
    parts = ["balance %.2f" % float(balance)]
    if ratio is not None:
        parts.append("%.0f%% of %.2f USD" % (ratio * 100.0, float(reference)))
    if runway is not None:
        parts.append("projected empty in %s" % _duration(runway - now))
    if ratio is not None and ratio <= CRITICAL_RATIO:
        parts.append("CRITICAL (under %.0f%%)" % (CRITICAL_RATIO * 100.0))
    return LOW, "; ".join(parts)


def _duration(seconds: float) -> str:
    minutes = max(0, int(round(float(seconds) / 60.0)))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return "%d h %02d" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%d d %02d h" % (days, hours)


def _apply(db, harness: str, account: str | None, verdict: tuple[str, str],
           *, balance: float, reference: float | None, rate: float | None,
           now: float, source: str, host: str | None) -> bool | None:
    """Écrit l'état décidé par `_verdict` ; vrai si `exhausted` apparaît."""
    state, detail = verdict
    ratio = (float(balance) / float(reference)
             if reference is not None and reference > 0 else None)
    runway = _runway(now, balance, rate)
    if state == EXHAUSTED:
        transition = mark_exhausted(
            db, harness, account, reason=("projected balance" if source == "prepaid"
                                          else "balance %.2f" % float(balance)),
            detail=detail, source=source, host=host, balance=balance, ratio=ratio,
            runway_ts=runway)
        return True if transition else None
    if state == LOW:
        mark_low(db, harness, account, reason=REASON_LOW, detail=detail,
                 source=source, host=host, balance=balance, ratio=ratio,
                 runway_ts=runway)
        return None
    transition = mark_ok(db, harness, account, source=source, host=host,
                         balance=balance, ratio=ratio, runway_ts=runway)
    return False if transition else None


def _spendable(reading) -> bool:
    available = getattr(reading, "available", None)
    if available is not None:
        return bool(available)
    return float(reading.total) > 0.0


def _reading_field(reading, key):
    """Un champ d'un relevé (`BalanceReading`) ou d'une ligne de série (dict)."""
    if isinstance(reading, dict):
        return reading.get(key)
    return getattr(reading, key, None)


def _fresh_usable(reading) -> bool:
    """La devise est utilisable et porte un solde strictement positif.

    Un relevé à zéro marqué disponible (le fournisseur publie la
    disponibilité par compte, pas par devise) ne prouve pas l'épuisement
    quand une autre devise porte un solde : il ne sert donc pas à choisir.
    """
    available = _reading_field(reading, "available")
    if available is not None and not bool(available):
        return False
    try:
        return float(_reading_field(reading, "total")) > 0.0
    except (TypeError, ValueError):
        return False


def _choose_currency(items: dict) -> str | None:
    """La devise qui décide : USD positif s'il existe, sinon la première
    positive par ordre alphabétique. Le même choix au poll et à la
    réévaluation ; aucune conversion implicite entre devises."""
    positive = sorted(str(cur or "").upper() for cur, reading in items.items()
                      if _fresh_usable(reading))
    if not positive:
        return None
    return "USD" if "USD" in positive else positive[0]


def _history(db, harness: str, account: str | None) -> dict:
    """Les soldes lus du fournisseur, par devise, récents d'abord bornés."""
    rows = storage.of(db).operations.balances(
        provider=harness, since_s=PEAK_WINDOW_S)
    cible = account or ""
    series: dict = {}
    for row in rows:
        if (row.get("account") or "") != cible:
            continue
        series.setdefault(str(row.get("currency") or "").upper(), []).append(row)
    for points in series.values():
        points.sort(key=lambda r: float(r["observed_ts"]))
    return series


def from_readings(db, harness: str, account: str | None, readings, *,
                  host: str | None = None, now: float | None = None) -> bool | None:
    """#4166 : un relevé de solde réel décide `exhausted`, `low` ou `ok`.

    Aucune devise utilisable → `exhausted` ; sinon la devise positive (USD
    s'il est positif, sinon la première par ordre alphabétique) est comparée
    au prépayé déclaré (USD seulement) — ou au sommet de sa propre série — et
    son rythme donne l'échéance projetée. Aucune conversion implicite entre
    devises : une devise à zéro ne cache pas une autre devise utilisable.
    Rend True sur la transition vers `exhausted`, False sur un retour à `ok`,
    None sinon (les transitions `low` ne changent pas la garde)."""
    now = time.time() if now is None else float(now)
    rows = list(readings or ())
    if not rows:
        return None
    usable = [r for r in rows if _spendable(r)]
    if not usable:
        total = min(float(r.total) for r in rows)
        currency = str(getattr(rows[0], "currency", "") or "")
        transition = mark_exhausted(
            db, harness, account,
            reason="balance %.2f %s" % (total, currency), source="balance",
            host=host)
        return True if transition else None
    by_currency: dict = {}
    for reading in usable:
        currency = str(getattr(reading, "currency", "") or "").upper()
        by_currency[currency] = reading
    chosen = _choose_currency(by_currency)
    if chosen is None:
        # utilisables mais toutes à zéro : rien ne reste sur le compte
        worst = min(rows, key=lambda r: float(r.total))
        total = float(worst.total)
        currency = str(getattr(worst, "currency", "") or "")
        transition = mark_exhausted(
            db, harness, account,
            reason="balance %.2f %s" % (total, currency), source="balance",
            host=host)
        return True if transition else None
    ref = by_currency[chosen]
    currency = chosen
    try:
        series = _history(db, harness, account)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError:
        return None
    points = series.get(currency) or []
    row = get(db, harness, account)
    reference = _peak(points, prepaid_of(row) if currency == "USD" else None)
    measured = None
    if points:
        from .sous_utilisation import balance_rate
        measured = balance_rate(points)
    rate = (measured[0] / measured[1] * 3600.0) if measured else None
    return _apply(db, harness, account, _verdict(float(ref.total), reference, rate, now),
                  balance=float(ref.total), reference=reference, rate=rate, now=now,
                  source="balance", host=host)


def from_balance(db, harness: str, account: str | None, total: float, currency: str,
                 *, available: bool | None = None, host: str | None = None
                 ) -> bool | None:
    """Compatibilité #4164 : un seul relevé, sans historique (les tests)."""
    class _Reading:
        pass

    reading = _Reading()
    reading.total, reading.currency, reading.available = float(total), currency, available
    return from_readings(db, harness, account, [reading], host=host)


def _real_verdict(db, row: dict, now: float) -> tuple | None:
    """Le verdict d'un compte dont le fournisseur publie un solde.

    La devise est choisie comme au poll (`from_readings`) : une devise
    positive, USD d'abord, sinon la première par ordre alphabétique. Aucune
    devise positive → épuisé. Le prépayé déclaré n'est une référence que pour
    USD ; les autres devises se comparent à leur propre sommet."""
    series = _history(db, row["harness"], row.get("account"))
    if not series:
        return None
    latest = {currency: points[-1] for currency, points in series.items() if points}
    if not latest:
        return None
    currency = _choose_currency(latest)
    if currency is None:
        # aucune devise positive : le compte est à zéro (ou indisponible)
        currency = "USD" if "USD" in series else sorted(series)[0]
    points = series[currency]
    if not points:
        return None
    total = float(points[-1]["total"])
    reference = _peak(points, prepaid_of(row) if currency == "USD" else None)
    measured = None
    if points:
        from .sous_utilisation import balance_rate
        measured = balance_rate(points)
    rate = (measured[0] / measured[1] * 3600.0) if measured else None
    available = points[-1].get("available")
    if available is not None and not bool(available):
        total = min(0.0, total)
    return _verdict(total, reference, rate, now), total, reference, rate


def _projected_verdict(db, row: dict, now: float) -> tuple | None:
    """Le verdict d'un compte sans API de solde, depuis le prépayé déclaré."""
    prepaid = prepaid_of(row)
    declared_at = row.get("prepaid_ts")
    if prepaid is None or declared_at is None:
        return None
    balance = prepaid - _settled_spend(db, row["harness"], row.get("account"),
                                       float(declared_at))
    rate = _rate_per_hour(db, row["harness"], row.get("account"),
                          float(declared_at), now)
    return _verdict(balance, prepaid, rate, now), balance, prepaid, rate


def _source_has_balance(harness: str) -> bool:
    from . import balance
    return harness in balance.SOURCES


def assess(db, *, now: float | None = None, harness: str | None = None,
           account: str | None = None) -> list[dict]:
    """#4166 : recalcule `low`/`ok` du prépayé déclaré et des soldes réels.

    Écrit les transitions (l'état et son `since` sont la clé d'alerte) ;
    appelé par le passage d'alertes, une fois par compte. Un compte
    `exhausted` n'est pas réévalué : seule la sonde le rouvre (#4164). Un
    `low` dont la preuve a disparu (déclaration retirée, historique purgé)
    redevient `ok`. Rend les lignes dont l'état a changé (journal)."""
    now = time.time() if now is None else float(now)
    changed = []
    for row in listing(db):
        if row.get("state") == EXHAUSTED:
            continue
        if harness is not None and row.get("harness") != harness:
            continue
        if account is not None and (row.get("account") or "") != (account or ""):
            continue
        try:
            verdict = (_real_verdict(db, row, now)
                       if _source_has_balance(row["harness"])
                       else _projected_verdict(db, row, now))
            if verdict is None:
                # plus rien pour juger : aucun signe de manque ne subsiste
                if row.get("state") == LOW:
                    if mark_ok(db, row["harness"], row.get("account"), source="assess"):
                        changed.append(dict(
                            get(db, row["harness"], row.get("account")) or row,
                            previous=row.get("state")))
                continue
            applied, balance, reference, rate = verdict
            before = row.get("state")
            _apply(db, row["harness"], row.get("account"), applied,
                   balance=balance, reference=reference, rate=rate, now=now,
                   source=("balance" if _source_has_balance(row["harness"])
                           else "prepaid"),
                   host=None)
            after = get(db, row["harness"], row.get("account"))
            if after is not None and after.get("state") != before:
                changed.append(dict(after, previous=before))
        except db_mod.Unavailable:
            raise
        except db_mod.DbError:
            # une ligne refusée (valeur hors colonne, verrou) n'arrête pas
            # l'évaluation des autres comptes de la passe (B1 dev2)
            continue
    return changed


def assess_one(db, harness: str, account: str | None, *,
               now: float | None = None) -> dict | None:
    """Réévalue un seul compte (après une déclaration) ; rend sa ligne."""
    try:
        assess(db, now=now, harness=harness, account=account or "")
    except db_mod.DbError:
        pass
    return get(db, harness, account)


def _local(ts) -> str:
    if ts is None:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def describe(row: dict | None) -> str:
    """Une ligne lisible de l'état d'un compte (`budget`, `show`, `accounts`)."""
    if not row:
        return "ok (never exhausted)"
    state = row.get("state") or OK
    parts = ["%s since %s" % (state, _local(row["since_ts"]))]
    if row.get("checked_ts"):
        parts.append("checked %s" % _local(row["checked_ts"]))
    balance = row.get("balance_usd")
    if balance is not None:
        texte = "balance %.2f" % float(balance)
        prepaid = prepaid_of(row)
        if prepaid is not None:
            texte += " of %.2f" % prepaid
        if row.get("ratio") is not None:
            texte += " (%.0f%%)" % (float(row["ratio"]) * 100.0)
        if row.get("runway_ts"):
            texte += ", projected empty %s" % _local(row["runway_ts"])
        parts.append(texte)
    if state == EXHAUSTED:
        if row.get("reason"):
            parts.append("reason %s" % row["reason"])
        if row.get("next_probe_ts"):
            parts.append("next probe %s" % _local(row["next_probe_ts"]))
    elif state == LOW and row.get("detail"):
        parts.append(row["detail"])
    return ", ".join(parts)


def _low_alert(row: dict, held: list[str]) -> dict:
    from .exploitation import _alert
    ratio = row.get("ratio")
    critical = ratio is not None and float(ratio) <= CRITICAL_RATIO
    nom = who(row["harness"], row.get("account"))
    parts = ["provider account %s is low" % nom]
    if row.get("balance_usd") is not None:
        texte = "balance %.2f" % float(row["balance_usd"])
        prepaid = prepaid_of(row)
        if prepaid is not None:
            texte += " of %.2f USD" % prepaid
        if ratio is not None:
            texte += " (%.0f%%)" % (float(ratio) * 100.0)
        parts.append(texte)
    if row.get("runway_ts"):
        parts.append("projected empty %s" % _local(row["runway_ts"]))
    detail = "%s. Top up the account; a fallback account of the same harness is " \
             "used if one is declared, otherwise no new turn is granted once it is " \
             "exhausted." % ", ".join(parts)
    if critical:
        detail += " CRITICAL: under %.0f%% of the reference amount." % (
            CRITICAL_RATIO * 100.0)
    if held:
        detail += "; held: %s" % ", ".join(held)
    return _alert(
        REASON_LOW, None, row["since_ts"], ratio, LOW_RATIO, detail,
        provider=row["harness"], harness=row["harness"],
        account=row.get("account") or "", currency=None, reason=REASON_LOW,
        level="critical" if critical else "low",
        balance_usd=row.get("balance_usd"), prepaid_usd=prepaid_of(row),
        runway_ts=row.get("runway_ts"), checked_ts=row.get("checked_ts"),
        held=held, targets=["owner"], urgent=critical)


def alerts(db, listing_rows: list, now: float) -> list[dict]:
    """#4164 `provider_credits_exhausted` (une par compte épuisé, clé sur le
    début de l'état — sa résolution est l'avis de reprise) et #4166
    `provider_credits_low` (une par épisode `low`, niveau critique et urgent
    sous 5 %).

    L'évaluation `low` est faite ici, avant la lecture : c'est le passage
    d'alertes qui constate la transition et l'écrit (les transitions sont
    conditionnelles, une seule écriture gagne)."""
    from .exploitation import _alert
    try:
        assess(db, now=now)
    except db_mod.DbError:
        pass
    out = []
    for row in listing(db):
        nom = who(row["harness"], row.get("account"))
        if row.get("state") == LOW:
            held = sorted(r["name"] for r in listing_rows
                          if (r.get("status_text") or "") == status_text(
                              row["harness"], row.get("account")))
            out.append(_low_alert(row, held))
            continue
        if row.get("state") != EXHAUSTED:
            continue
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

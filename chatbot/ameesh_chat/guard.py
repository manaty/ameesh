# SPDX-License-Identifier: AGPL-3.0-only
"""Garde-fous : barème, borne de jetons, plafond de dépense durable, débit.

**Borne de jetons (B2)** — le coût maximal d'un appel est calculé à partir
d'une BORNE SUPÉRIEURE, pas d'une estimation : le nombre d'OCTETS UTF-8 de tout
le texte envoyé (consigne, passages, historique, question), plus une marge fixe
par message pour le gabarit de conversation. Le tokeniseur des modèles visés
est un BPE sur octets : chaque jeton couvre au moins un octet, donc le texte ne
peut pas produire plus de jetons qu'il n'a d'octets, quelle que soit la langue
(CJK, emoji, code). La sortie est bornée par `max_tokens`, imposé à l'API.

**Plafond durable (B1)** — l'état est un objet JSON (voir `store.py`) qui porte
les totaux dépensés ET les réservations ouvertes. Chaque changement est une
*transaction* : lecture (avec ETag) → modification → écriture CONDITIONNELLE
(`If-Match`, ou `If-None-Match: *` à la création) → relecture de contrôle ; un
conflit fait relire et rejouer. Le verrou de l'instance est tenu de la lecture
à la fin de la relecture : les écritures d'une instance sont sérialisées, celles
de plusieurs instances sont départagées par l'ETag ; le total dépensé ne recule
jamais.

* `reserve` écrit la réservation (coût maximal) de façon durable et la relit
  AVANT l'appel au modèle ; si l'écriture échoue, aucun appel (fermeture sûre) ;
* `commit` remplace la réservation par le coût réel (usage de l'API) ; usage
  absent ou mal formé = réservation pleine ; si l'écriture échoue, la
  réservation durable reste comptée et le solde est retenté à la transaction
  suivante ;
* une réservation qui n'appartient pas à l'instance (trouvée au démarrage), ou
  plus vieille que `stale_after` secondes, est comptée **dépensée en entier** :
  un arrêt entre l'appel et le solde ne fait jamais oublier une dépense ;
* les caps comparent `dépensé + réservations ouvertes + nouvelle réservation` ;
  un plafond à 0 ferme tout ;
* UNE période (mois, jour) par tentative, lue sous le verrou ; un appel qui
  franchit minuit ou la fin du mois est attribué à la période de sa
  réservation (totaux de la période close gardés dans `prev_day`/`prev_month`) ;
* un usage renvoyé au-delà des bornes envoyées : coût connu compté (jamais
  réduit), admissions fermées durablement (`closed`), à rouvrir par l'opérateur ;
  la fermeture est gardée localement (`pending_close`) tant qu'elle n'est pas
  publiée, et bloque toute admission de l'instance d'ici là ;
* trace durable des soldes (`settled` : identifiant → montant déjà compté,
  gardée deux jours) : un solde tardif, après qu'une autre instance a compté la
  réservation au montant réservé, n'ajoute que le complément positif, de façon
  idempotente ; sans trace, il est compté en entier.

**Débit** : fenêtres glissantes en mémoire, par adresse (clé = HMAC de
l'adresse avec un sel aléatoire du processus : l'adresse elle-même n'est ni
gardée ni écrite) et globales. Elles vivent le temps de l'instance.
"""
from __future__ import annotations

import collections
import json
import hashlib
import hmac
import math
import secrets
import threading
import time
from dataclasses import dataclass

from .store import StoreConflict, StoreError

#: marge par message (jetons spéciaux du gabarit de conversation, rôle) et par requête
TEMPLATE_TOKENS_PER_MESSAGE = 16
TEMPLATE_TOKENS_PER_REQUEST = 64


def _finite_nonneg(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def input_token_bound(messages: list[dict]) -> int:
    """Borne supérieure du nombre de jetons d'entrée (octets UTF-8 + gabarit)."""
    total = TEMPLATE_TOKENS_PER_REQUEST
    for m in messages:
        total += len(str(m.get("content", "")).encode("utf-8"))
        total += len(str(m.get("role", "")).encode("utf-8")) + TEMPLATE_TOKENS_PER_MESSAGE
    return total


@dataclass(frozen=True)
class Pricing:
    """USD par million de jetons ; nombres finis et positifs ou nuls."""

    input_miss: float = 0.27
    input_hit: float = 0.07
    output: float = 1.10

    def __post_init__(self):
        for name in ("input_miss", "input_hit", "output"):
            if not _finite_nonneg(getattr(self, name)):
                raise ValueError("pricing_" + name)

    def cost(self, usage) -> float | None:
        """Coût d'un appel d'après `usage` ; `None` si l'usage est absent ou incohérent
        (l'appelant garde alors la réservation pleine)."""
        if not isinstance(usage, dict):
            return None
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        hit, miss = usage.get("prompt_cache_hit_tokens", 0), usage.get("prompt_cache_miss_tokens")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                   for v in (prompt, completion, hit)):
            return None
        if miss is None:
            miss = prompt - hit
        if not isinstance(miss, int) or isinstance(miss, bool) or miss < 0 or hit + miss != prompt:
            return None
        return (miss * self.input_miss + hit * self.input_hit + completion * self.output) / 1e6

    @staticmethod
    def over_bound(usage, max_input: int, max_output: int) -> bool:
        """L'usage (exploitable) dépasse-t-il les bornes envoyées ? Contrat du
        fournisseur ou borne violés : l'appelant compte au moins ce coût connu et
        ferme les admissions."""
        try:
            return int(usage["prompt_tokens"]) > max_input or int(usage["completion_tokens"]) > max_output
        except (KeyError, TypeError, ValueError):
            return False

    def ceiling(self, input_tokens: int, output_tokens: int) -> float:
        """Coût maximal : toute l'entrée hors cache, toute la sortie permise."""
        price_in = max(self.input_miss, self.input_hit)
        return (input_tokens * price_in + output_tokens * self.output) / 1e6


class CapReached(RuntimeError):
    """Plafond (mois, jour ou nombre de requêtes du jour) atteint."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def default_daily_cap(monthly_cap: float) -> float:
    """Plafond quotidien dérivé : deux jours « moyens » d'un mois de 30 jours."""
    return round(monthly_cap * 2 / 30, 4)


_TOTALS = ("month_usd", "day_usd", "month_requests", "day_requests", "seq")


def validate_state(state) -> dict:
    """L'état lu doit être fait de nombres finis non négatifs ; sinon fermeture."""
    if not isinstance(state, dict):
        raise StoreError("store_bad_state")
    for key in _TOTALS:
        if key in state and not _finite_nonneg(state[key]):
            raise StoreError("store_bad_state")
    if "closed" in state and not isinstance(state["closed"], str):
        raise StoreError("store_bad_state")
    for key in ("prev_month", "prev_day"):
        prev = state.get(key)
        if prev is not None and not (isinstance(prev, dict) and isinstance(prev.get("period"), str)
                                     and _finite_nonneg(prev.get("usd"))):
            raise StoreError("store_bad_state")
    for key in ("reservations", "settled"):
        if not isinstance(state.get(key, {}), dict):
            raise StoreError("store_bad_state")
    for r in list(state.get("reservations", {}).values()) + list(state.get("settled", {}).values()):
        if not (isinstance(r, dict) and _finite_nonneg(r.get("usd")) and _finite_nonneg(r.get("t"))
                and isinstance(r.get("day"), str) and isinstance(r.get("month"), str)):
            raise StoreError("store_bad_state")
    return state


class SpendGuard:
    RETRIES = 8

    def __init__(self, store, *, monthly_cap: float = 20.0, daily_cap: float | None = None,
                 max_requests_per_day: int = 2000, clock=time.time, stale_after: float = 120.0):
        if not _finite_nonneg(monthly_cap) or (daily_cap is not None and not _finite_nonneg(daily_cap)):
            raise ValueError("cap")
        self.store = store
        self.monthly_cap = float(monthly_cap)
        # `is not None` : un plafond à 0 est un vrai plafond (tout fermé), pas « par défaut »
        self.daily_cap = float(daily_cap) if daily_cap is not None else default_daily_cap(self.monthly_cap)
        self.max_requests_per_day = int(max_requests_per_day)
        self.clock = clock
        self.stale_after = float(stale_after)
        self.instance = secrets.token_hex(8)
        self.lock = threading.RLock()
        self.started = False
        #: soldes dont l'écriture a échoué : id → coût réel, rejoués à la transaction suivante
        self.unsettled: dict[str, float] = {}
        #: fermeture décidée mais pas encore publiée : vérifiée avant TOUTE admission,
        #: rejouée à la prochaine transaction réussie
        self.pending_close: str | None = None

    # -- transaction -----------------------------------------------------
    def _periods(self) -> tuple[str, str]:
        now = time.gmtime(self.clock())
        return time.strftime("%Y-%m", now), time.strftime("%Y-%m-%d", now)

    #: durée de conservation de la trace des soldes (identifiant → montant compté)
    SETTLED_KEEP_S = 2 * 86400

    def _settle(self, state: dict, rid: str, r: dict, counted: float, now: float) -> None:
        """Compte `counted` pour la réservation `rid` et garde une trace DURABLE
        (montant déjà compté) : une remise tardive n'ajoutera que le complément."""
        self._add(state, r, counted)
        state["settled"][rid] = {"usd": counted, "t": now, "day": r["day"], "month": r["month"]}

    @staticmethod
    def _add(state: dict, r: dict, usd: float) -> None:
        """Attribue une dépense à la période de SA réservation (règle de minuit / fin
        de mois) : totaux de la période en cours, ou de la période précédente
        (`prev_day` / `prev_month`, gardées à la bascule) si l'appel l'a franchie."""
        for unit in ("month", "day"):
            if r.get(unit) == state[unit]:
                state[unit + "_usd"] = state[unit + "_usd"] + usd
            elif isinstance(state.get("prev_" + unit), dict) and r.get(unit) == state["prev_" + unit]["period"]:
                state["prev_" + unit]["usd"] = state["prev_" + unit]["usd"] + usd

    def _prepare(self, state: dict) -> dict:
        """UNE période (lue une fois) pour toute la tentative ; réservations orphelines
        comptées ; soldes en attente rejoués. `state["month"]`/`state["day"]` sont
        ensuite LA période de la tentative, utilisée par `mutate`."""
        month, day = self._periods()
        now = self.clock()
        state = validate_state(json.loads(json.dumps(state)))
        state["v"] = 2
        state.setdefault("seq", 0)
        for key in ("month_usd", "day_usd", "month_requests", "day_requests"):
            state.setdefault(key, 0)
        for unit, period in (("month", month), ("day", day)):
            if state.get(unit) != period:
                if state.get(unit):
                    state["prev_" + unit] = {"period": state[unit], "usd": state[unit + "_usd"]}
                state.update({unit: period, unit + "_usd": 0.0, unit + "_requests": 0})
        reservations = state["reservations"] = dict(state.get("reservations", {}))
        settled = state["settled"] = {k: v for k, v in dict(state.get("settled", {})).items()
                                      if now - v["t"] <= self.SETTLED_KEEP_S}
        applied: list[str] = []
        for rid, r in list(reservations.items()):
            if rid in self.unsettled:
                self._settle(state, rid, r, self.unsettled[rid], now)
                del reservations[rid]
                applied.append(rid)
            elif (r.get("owner") != self.instance and not self.started) or now - r["t"] > self.stale_after:
                # jamais soldée (arrêt, autre instance au démarrage) : dépensée en entier
                self._settle(state, rid, r, r["usd"], now)
                del reservations[rid]
        for rid, spent in self.unsettled.items():
            if rid in applied:
                continue
            if rid in settled:
                # déjà soldée ailleurs (au montant réservé) : seul le complément positif,
                # idempotent — la trace est portée au montant total compté
                extra = spent - settled[rid]["usd"]
                if extra > 0:
                    self._add(state, settled[rid], extra)
                    settled[rid] = dict(settled[rid], usd=spent)
            elif spent > 0:
                # trace introuvable (purgée) : compté en entier, à la période courante
                self._settle(state, rid, {"day": state["day"], "month": state["month"]}, spent, now)
            applied.append(rid)
        if self.pending_close:
            state["closed"] = self.pending_close
        state["now"] = now
        state["_applied"] = applied
        return state

    def _transact(self, mutate):
        """Lecture → `mutate(state)` → écriture conditionnelle → relecture. Verrou tenu."""
        with self.lock:
            last = "store_conflict"
            for attempt in range(self.RETRIES):
                if attempt:
                    # un autre écrivain : attente courte et aléatoire, puis relecture
                    time.sleep(secrets.randbelow(20 * attempt + 1) / 1000)
                raw, etag = self.store.load()
                state = self._prepare(raw)
                result = mutate(state)
                state.pop("now", None)
                applied = state.pop("_applied", [])
                state["seq"] = int(state["seq"]) + 1
                try:
                    self.store.save(state, etag)
                except StoreConflict:
                    continue
                back, _ = self.store.load()
                back = validate_state(back)
                if back.get("seq") == state["seq"] and back != state:
                    raise StoreError("store_readback")     # écrit mais relu autrement : on ferme
                if int(back.get("seq", 0)) < state["seq"]:
                    last = "store_readback"                # écriture perdue : on rejoue
                    continue
                self.started = True
                for rid in applied:
                    self.unsettled.pop(rid, None)
                if back.get("closed") and back.get("closed") == self.pending_close:
                    self.pending_close = None
                return result
            raise StoreError(last)

    # -- API -------------------------------------------------------------
    def reserve(self, amount: float) -> str:
        """Réservation DURABLE du coût maximal ; rend son identifiant."""
        if not _finite_nonneg(amount):
            raise ValueError("amount")
        rid = secrets.token_hex(8)
        if self.pending_close:
            # fermeture décidée localement, pas encore publiée : aucune admission ;
            # on tente seulement de la publier (avec les soldes en attente)
            try:
                self._transact(lambda s: None)
            except StoreError:
                pass
            raise CapReached("closed")

        def mutate(s):
            # la période de CETTE tentative (préparée sous verrou), jamais une capture antérieure
            month, day = s["month"], s["day"]
            if s.get("closed"):
                raise CapReached("closed")
            held = sum(r["usd"] for r in s["reservations"].values())
            held_day = sum(r["usd"] for r in s["reservations"].values() if r["day"] == day)
            if s["day_requests"] >= self.max_requests_per_day:
                raise CapReached("daily_requests")
            if s["month_usd"] + held + amount > self.monthly_cap:
                raise CapReached("monthly_cap")
            if s["day_usd"] + held_day + amount > self.daily_cap:
                raise CapReached("daily_cap")
            s["reservations"][rid] = {"usd": amount, "t": s["now"], "day": day, "month": month,
                                      "owner": self.instance}
            s["day_requests"] += 1
            s["month_requests"] += 1
            return rid
        return self._transact(mutate)

    def commit(self, rid: str, reserved: float, actual: float | None, *, close: str | None = None) -> float:
        """Solde : coût réel, ou réservation pleine si l'usage manque. Si l'écriture
        échoue, la réservation durable reste comptée et le solde est retenté.

        `close` : ferme durablement les admissions (champ `closed` de l'état, à
        retirer par l'opérateur après examen) ; le coût compté est alors AU MOINS
        le coût connu, jamais réduit à la réservation."""
        spent = reserved if actual is None or not _finite_nonneg(actual) else float(actual)
        if close:
            spent = max(spent, reserved)

        with self.lock:
            self.unsettled[rid] = spent
            if close:
                self.pending_close = close  # gardée localement tant qu'elle n'est pas publiée
            self._transact(lambda s: None)  # `_prepare` applique soldes et fermeture en attente
        return spent

    def release(self, rid: str) -> None:
        """Requête refusée par le fournisseur (non facturée) : la réservation est rendue."""
        with self.lock:
            self.unsettled[rid] = 0.0
            self._transact(lambda s: None)

    def snapshot(self) -> dict:
        with self.lock:
            raw, _ = self.store.load()
            return self._prepare_readonly(raw)

    def _prepare_readonly(self, raw: dict) -> dict:
        started, unsettled = self.started, dict(self.unsettled)
        try:
            state = self._prepare(raw)
            state.pop("_applied", None)
            state.pop("now", None)
            return state
        finally:
            self.started, self.unsettled = started, unsettled

class RateLimiter:
    """Fenêtres glissantes `[(secondes, maximum), …]` par clé (HMAC de l'adresse)."""

    MAX_KEYS = 20_000

    def __init__(self, windows: list[tuple[float, int]], clock=time.monotonic):
        self.windows = [(float(s), int(n)) for s, n in windows if n > 0]
        self.horizon = max((s for s, _ in self.windows), default=0.0)
        self.clock = clock
        self.salt = secrets.token_bytes(16)
        self.hits: "collections.OrderedDict[str, collections.deque]" = collections.OrderedDict()
        self.lock = threading.Lock()

    def key(self, raw: str) -> str:
        return hmac.new(self.salt, raw.encode("utf-8"), hashlib.sha256).hexdigest()[:24]

    def check(self, raw: str) -> float:
        """0 si admis (et compté), sinon le nombre de secondes à attendre."""
        now = self.clock()
        k = self.key(raw)
        with self.lock:
            q = self.hits.get(k)
            if q is None:
                q = collections.deque()
                self.hits[k] = q
            self.hits.move_to_end(k)
            while q and now - q[0] >= self.horizon:
                q.popleft()
            wait = 0.0
            for seconds, limit in self.windows:
                inside = [t for t in q if now - t < seconds]
                if len(inside) >= limit:
                    wait = max(wait, seconds - (now - inside[0]))
            if wait > 0:
                return wait
            q.append(now)
            while len(self.hits) > self.MAX_KEYS:
                self.hits.popitem(last=False)
            return 0.0
